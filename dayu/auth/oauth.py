"""系统浏览器 OAuth 与 loopback 回调，交互仅由 provider 初始化调用。"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import httpx
import jwt

from dayu.auth.chatgpt import API_RESOURCE, HTTP_TIMEOUT_SECONDS, PLAN_SCOPE, TOKEN_ENDPOINT, require_text
from dayu.auth.store import ChatGPTRegistration, CredentialStore
from dayu.contracts.agent_types import JsonValue
from dayu.contracts.model_credentials import ModelAuthenticationError, ModelCredential

ISSUER = "https://auth.openai.com"
AUTHORIZE_ENDPOINT = f"{ISSUER}/api/accounts/authorize"
DISCOVERY_ENDPOINT = f"{ISSUER}/.well-known/openid-configuration"
CALLBACK_PATH = "/auth/callback"
LOGIN_TIMEOUT_SECONDS = 180
IDENTITY_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


@dataclass(frozen=True, repr=False)
class PendingAuthorization:
    """已校验但未激活的授权，可在配置准备期间只读访问模型目录。"""

    registration: ChatGPTRegistration
    expected_generation: str | None

    async def acquire(self, *, refresh: bool = False) -> ModelCredential:
        """读取候选凭据，过期后要求重新授权，不修改正式连接。

        Args:
            refresh: 候选授权不支持自动轮换。
        Returns:
            候选凭据和注册身份。
        Raises:
            ModelAuthenticationError: 候选凭据过期或请求刷新。
        """
        if refresh or self.registration.expires_at <= time.time():
            raise ModelAuthenticationError("候选授权已过期，请重新登录")
        return ModelCredential(self.registration.access_token, self.registration.identity)


@dataclass(repr=False)
class AuthorizationAttempt:
    """仅在一次授权期间存在的回调状态，不允许输出 repr。"""

    state: str
    nonce: str
    verifier: str
    callback: dict[str, str] | None = None


class CallbackServer(HTTPServer):
    """携带一次性授权状态的本地监听器。"""

    attempt: AuthorizationAttempt


class CallbackHandler(BaseHTTPRequestHandler):
    """校验回调来源和 state，不输出请求 URL 或授权码。"""

    def do_GET(self) -> None:
        """处理 loopback 回调。

        Args:
            无。
        Returns:
            无。
        Raises:
            OSError: 浏览器连接意外断开。
        """
        server = self.server
        if not isinstance(server, CallbackServer):
            raise RuntimeError("无效的授权回调服务")
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        state = query.get("state", [""])[0]
        valid = parsed.path == CALLBACK_PATH and secrets.compare_digest(state, server.attempt.state)
        if valid and server.attempt.callback is None:
            server.attempt.callback = {key: values[0] for key, values in query.items()}
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(("授权结果已收到，可以关闭窗口。" if valid else "无效的授权回调。").encode())

    def log_message(self, format: str, *args: str | int) -> None:
        """禁止 HTTP 服务打印包含 code 或 state 的路径。

        Args:
            format: 标准库日志格式。
            args: 标准库日志参数。
        Returns:
            无。
        Raises:
            无。
        """


def validate_identity(id_token: str, *, client_id: str, nonce: str, client: httpx.Client) -> dict[str, JsonValue]:
    """通过发现文档的 JWKS 验证 ID token。

    Args:
        id_token: 待验证 ID token。
        client_id: 本次签发的注册 ID。
        nonce: 本次授权 nonce。
        client: 有超时的 HTTP 客户端。
    Returns:
        通过验签的身份字段。
    Raises:
        ModelAuthenticationError: 签名、身份或 nonce 无效。
        httpx.HTTPError: 发现文档或 JWKS 不可用。
    """
    discovery = client.get(DISCOVERY_ENDPOINT)
    discovery.raise_for_status()
    document: dict[str, JsonValue] = discovery.json()
    jwks_uri = require_text(document, "jwks_uri")
    if not jwks_uri.startswith(f"{ISSUER}/"):
        raise ModelAuthenticationError("OpenAI JWKS 地址不受信任")
    response = client.get(jwks_uri)
    response.raise_for_status()
    try:
        header = jwt.get_unverified_header(id_token)
        keys = response.json()["keys"]
        key = next(row for row in keys if row.get("kid") == header.get("kid"))
        claims: dict[str, JsonValue] = jwt.decode(
            id_token,
            jwt.PyJWK.from_dict(key).key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "nonce", "aud", "iss"]},
        )
        if claims.get("nonce") != nonce:
            raise ModelAuthenticationError("ChatGPT 授权 nonce 校验失败")
        require_text(claims, "sub")
        return claims
    except (jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError):
        raise ModelAuthenticationError("ChatGPT 身份验证失败") from None


def authorize(store: CredentialStore, *, replace_account: bool = False) -> PendingAuthorization:
    """准备候选授权，不激活或覆盖正式凭据。

    Args:
        store: 目标连接。
        replace_account: 是否建立新的账号注册。
    Returns:
        已校验的新注册及提交时必须比较的旧版本。
    Raises:
        ModelAuthenticationError: 拒绝授权、回调不完整或身份不匹配。
        TimeoutError: 用户未在期限内完成授权。
        httpx.HTTPError: 网络失败。
    """
    with store.lock():
        previous = store.load()
        host_id = previous.host_id if previous else store.host_id()
    returning = previous is not None and not replace_account
    attempt = AuthorizationAttempt(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()
    with CallbackServer(("127.0.0.1", 0), CallbackHandler) as server:
        server.attempt = attempt
        server.timeout = 0.5
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        parameters = {
            "client_id": previous.client_id if returning and previous else "dynamic_agent_client",
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": IDENTITY_SCOPES,
            "resource": API_RESOURCE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if returning and previous:
            if previous.id_token:
                parameters["id_token_hint"] = previous.id_token
        else:
            parameters["agent_name_hint"] = "Dayu"
        # 授权 URL 可能含 id_token_hint，不允许写日志或终端。
        if not webbrowser.open(f"{AUTHORIZE_ENDPOINT}?{urlencode(parameters)}"):
            raise ModelAuthenticationError("无法打开系统浏览器，请在本地桌面环境运行 init")
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while attempt.callback is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("ChatGPT 登录超时，旧配置保持不变")
            server.handle_request()
    callback = attempt.callback
    if callback.get("error") or not callback.get("code"):
        raise ModelAuthenticationError("ChatGPT 授权未完成，旧配置保持不变")
    client_id = callback.get("client_id") or (previous.client_id if returning and previous else "")
    if (
        not client_id
        or client_id == "dynamic_agent_client"
        or (returning and previous and client_id != previous.client_id)
    ):
        raise ModelAuthenticationError("ChatGPT 注册标识不匹配")
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = client.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": callback["code"],
                "code_verifier": attempt.verifier,
                "redirect_uri": redirect_uri,
                "resource": API_RESOURCE,
            },
        )
        response.raise_for_status()
        data: dict[str, JsonValue] = response.json()
        id_token = require_text(data, "id_token")
        claims = validate_identity(id_token, client_id=client_id, nonce=attempt.nonce, client=client)
    scopes = tuple(require_text(data, "scope").split())
    if PLAN_SCOPE not in scopes:
        raise ModelAuthenticationError("尚未授予 ChatGPT 套餐调用权限，旧配置保持不变")
    subject = require_text(claims, "sub")
    if returning and previous and previous.subject != subject:
        raise ModelAuthenticationError("返回账号与原注册不匹配，请选择更换账号")
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise ModelAuthenticationError("ChatGPT 返回的凭据有效期无效")
    registration = ChatGPTRegistration(
        client_id,
        host_id,
        subject,
        str(claims.get("email", "")),
        require_text(data, "access_token"),
        require_text(data, "refresh_token"),
        id_token,
        time.time() + expires_in,
        scopes,
        uuid4().hex,
    )
    return PendingAuthorization(registration, previous.generation if previous else None)
