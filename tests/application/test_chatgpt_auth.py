"""OAuth 验签、回调、刷新与凭据替换的隔离测试，不使用真实账号。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from dayu.auth import chatgpt, oauth
from dayu.auth.chatgpt import ChatGPTCredentialProvider, PLAN_SCOPE, list_models
from dayu.auth.store import ChatGPTRegistration, CredentialStore
from dayu.auth.credentials import build_credential_provider
from dayu.contracts.model_credentials import ModelAuthenticationError


def registration(*, expires_at: float = 0, generation: str = "g1", subject: str = "subject") -> ChatGPTRegistration:
    """生成不含真实秘密的凭据 fixture。"""
    return ChatGPTRegistration(
        "issued-client",
        "host",
        subject,
        "fixture@example.com",
        "old-access",
        "old-refresh",
        "old-id",
        expires_at,
        (PLAN_SCOPE,),
        generation,
    )


def test_store_atomic_permissions_and_invalid_reference(tmp_path: Path) -> None:
    """凭据原子替换且拒绝路径注入和不安全权限。"""
    store = CredentialStore(root=tmp_path / "private")
    with store.lock():
        store.save(registration())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.load() == registration()
    assert "old-access" not in repr(registration())
    with pytest.raises(ValueError):
        CredentialStore("../invalid", root=tmp_path)
    store.path.chmod(0o644)
    with pytest.raises(ModelAuthenticationError):
        store.load()
    store.path.chmod(0o600)
    store.path.write_text("{}")
    with pytest.raises(ModelAuthenticationError):
        store.load()


@pytest.mark.asyncio
async def test_credential_factory_and_api_key_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """认证与协议分离，API key 轮换后续接身份改变。"""
    provider = build_credential_provider({"type": "api_key", "api_key_env": "DAYU_TEST_ONLY_KEY"})
    monkeypatch.delenv("DAYU_TEST_ONLY_KEY", raising=False)
    with pytest.raises(ModelAuthenticationError):
        await provider.acquire()
    monkeypatch.setenv("DAYU_TEST_ONLY_KEY", "fixture-one")
    first = await provider.acquire()
    monkeypatch.setenv("DAYU_TEST_ONLY_KEY", "fixture-two")
    second = await provider.acquire()
    assert first.identity != second.identity
    assert first.access_token not in first.identity
    assert "fixture" not in repr(first)
    assert isinstance(
        build_credential_provider({"type": "chatgpt", "account_ref": "fixture"}), ChatGPTCredentialProvider
    )
    with pytest.raises(ValueError):
        build_credential_provider({})


def test_host_id_stable_across_connection_names(tmp_path: Path) -> None:
    """同安装内连接共享稳定 host ID，文件不可被其他用户读取。"""
    first = CredentialStore(root=tmp_path)
    second = CredentialStore("another", root=tmp_path)
    with first.lock():
        identifier = first.host_id()
    with second.lock():
        assert second.host_id() == identifier
    assert identifier.startswith("urn:uuid:")
    assert (tmp_path / "host-id").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_concurrent_refresh_and_rotating_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发消费者只刷新一次，后续拒绝被替换为不同账号。"""
    store = CredentialStore(root=tmp_path)
    with store.lock():
        store.save(registration())
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        )

    monkeypatch.setattr(
        chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    )
    first, second = ChatGPTCredentialProvider(store), ChatGPTCredentialProvider(store)
    values = await asyncio.gather(first.acquire(), second.acquire())
    assert len(requests) == 1
    assert all(value.access_token == "new-access" for value in values)
    saved = store.load()
    assert saved is not None and saved.refresh_token == "new-refresh"
    # 因其他进程刷新而更换的 access token 不应再次触发强制刷新。
    with store.lock():
        store.save(replace(saved, access_token="another-access", generation="g3"))
    assert (await first.acquire(refresh=True)).access_token == "another-access"
    assert len(requests) == 1
    with store.lock():
        store.save(replace(saved, subject="other-account", generation="g4"))
    with pytest.raises(ModelAuthenticationError, match="账号已改变"):
        await first.acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status,cleared",
    [
        ("invalid_grant", 400, True),
        ("refresh_token_reused", 400, True),
        ("invalid_client", 401, False),
        ("temporarily_unavailable", 503, False),
    ],
)
async def test_refresh_error_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, status: int, cleared: bool
) -> None:
    """只有终止性刷新错误清空 token，保留注册身份。"""
    store = CredentialStore(root=tmp_path)
    with store.lock():
        store.save(registration())
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"error": code}))
    monkeypatch.setattr(chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    with pytest.raises((ModelAuthenticationError, httpx.HTTPError)):
        await ChatGPTCredentialProvider(store).acquire()
    saved = store.load()
    assert saved is not None
    assert (not saved.refresh_token) is cleared
    assert saved.client_id == "issued-client"
    assert saved.subject == "subject"


@pytest.mark.asyncio
async def test_models_and_missing_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录仅展示可见项，未授权账号不能调用。"""
    store = CredentialStore(root=tmp_path)
    with pytest.raises(ModelAuthenticationError):
        await ChatGPTCredentialProvider(store).acquire()
    with store.lock():
        store.save(registration(expires_at=time.time() + 3600))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"models": [{"slug": "visible", "visibility": "list"}, {"slug": "hidden"}]}
        )
    )
    monkeypatch.setattr(chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    assert [model["slug"] for model in await list_models(ChatGPTCredentialProvider(store))] == ["visible"]


@pytest.mark.asyncio
async def test_refresh_commit_survives_cancellation_during_client_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端轮换成功后取消 cleanup，必须保留新凭据并释放锁。"""
    store = CredentialStore(root=tmp_path)
    with store.lock():
        store.save(registration())
    closing = asyncio.Event()
    requests: list[httpx.Request] = []

    class PausingTransport(httpx.AsyncBaseTransport):
        """在响应完整返回后的资源关闭阶段暂停。"""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            )

        async def aclose(self) -> None:
            closing.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=PausingTransport()))
    task = asyncio.create_task(ChatGPTCredentialProvider(store).acquire())
    await asyncio.wait_for(closing.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.load()
    assert saved is not None and saved.refresh_token == "new-refresh"
    assert (await ChatGPTCredentialProvider(store).acquire()).access_token == "new-access"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503])
@pytest.mark.parametrize("body", ["", "<html>temporarily unavailable</html>"])
async def test_non_json_refresh_failure_keeps_http_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: str
) -> None:
    """HTML 与空错误体不影响 HTTP 分类，也不能清除凭据。"""
    store = CredentialStore(root=tmp_path)
    with store.lock():
        store.save(registration())
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    monkeypatch.setattr(chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    with pytest.raises(httpx.HTTPStatusError) as failure:
        await ChatGPTCredentialProvider(store).acquire()
    assert failure.value.response.status_code == status
    assert store.load() == registration()


class OAuthFixture:
    """真实本地回调配合签名 ID token 与模拟 OpenAI 服务。"""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.parameters: dict[str, list[str]] = {}
        self.threads: list[Thread] = []
        self.deny = False
        self.bad_nonce = False
        self.missing_scope = False
        self.subject = "subject"

    def open_browser(self, url: str) -> bool:
        """异步请求真实 loopback，不记录授权 URL。"""
        self.parameters = parse_qs(urlparse(url).query)
        query = {"state": self.parameters["state"][0], "client_id": "issued-client"}
        query.update({"error": "access_denied"} if self.deny else {"code": "fixture-code"})
        callback = self.parameters["redirect_uri"][0] + "?" + urlencode(query)
        thread = Thread(target=self.send_callback, args=(callback,), daemon=True)
        self.threads.append(thread)
        thread.start()
        return True

    def send_callback(self, callback: str) -> None:
        """向测试监听器提交授权结果。"""
        with urlopen(callback, timeout=2) as response:
            assert response.status == 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        """提供发现文档、JWKS 与 token 交换结果。"""
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": oauth.ISSUER + "/jwks"})
        if request.url.path == "/jwks":
            jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
            jwk["kid"] = "fixture-key"
            return httpx.Response(200, json={"keys": [jwk]})
        fields = parse_qs(request.content.decode())
        assert fields["redirect_uri"] == self.parameters["redirect_uri"]
        verifier = fields["code_verifier"][0]
        assert len(verifier) >= 43
        claims = {
            "iss": oauth.ISSUER,
            "aud": "issued-client",
            "sub": self.subject,
            "email": "fixture@example.com",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "nonce": "bad" if self.bad_nonce else self.parameters["nonce"][0],
        }
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture-key"})
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-access",
                "refresh_token": "fixture-refresh",
                "id_token": token,
                "expires_in": 3600,
                "scope": "openid" if self.missing_scope else oauth.IDENTITY_SCOPES,
            },
        )


@pytest.mark.parametrize("mode", ["success", "new", "denied", "nonce", "scope", "identity"])
def test_oauth_loopback_identity_and_failed_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """授权、验签、scope 与失败替换一起验证，失败保持旧凭据。"""
    fixture = OAuthFixture()
    fixture.deny = mode == "denied"
    fixture.bad_nonce = mode == "nonce"
    fixture.missing_scope = mode == "scope"
    fixture.subject = "other" if mode == "identity" else "subject"
    store = CredentialStore(root=tmp_path)
    previous = registration()
    if mode != "new":
        with store.lock():
            store.save(previous)
    monkeypatch.setattr(oauth.webbrowser, "open", fixture.open_browser)
    monkeypatch.setattr(oauth.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(fixture.handle)))
    if mode in {"success", "new"}:
        pending = oauth.authorize(store)
        saved = pending.registration
        assert saved.access_token == "fixture-access"
        assert store.load() == (previous if mode != "new" else None)
        assert pending.expected_generation == (previous.generation if mode != "new" else None)
        assert saved.generation != previous.generation
    else:
        with pytest.raises(ModelAuthenticationError):
            oauth.authorize(store)
        assert store.load() == previous
    for thread in fixture.threads:
        thread.join(timeout=2)
    assert fixture.parameters["client_id"] == (["dynamic_agent_client"] if mode == "new" else ["issued-client"])
