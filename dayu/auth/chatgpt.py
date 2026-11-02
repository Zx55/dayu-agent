"""ChatGPT 套餐凭据刷新与模型目录，严格区分授权和临时错误。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import httpx
from filelock import Timeout

from dayu.auth.store import CredentialStore
from dayu.contracts.agent_types import JsonValue
from dayu.contracts.model_credentials import ModelAuthenticationError, ModelCredential, ModelCredentialProvider

TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
API_RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
REFRESH_MARGIN_SECONDS = 60
LOCK_POLL_SECONDS = 0.05
LOCK_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30
TERMINAL_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


def require_text(data: dict[str, JsonValue], key: str) -> str:
    """提取非空协议文本。

    Args:
        data: 协议对象。
        key: 必填键。
    Returns:
        非空文本。
    Raises:
        ValueError: 服务端返回结构不完整。
    """
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"认证响应缺少字段 {key}")
    return value


class ChatGPTCredentialProvider:
    """按需刷新，身份在同一运行期间固定，凭据引用不携带秘密。"""

    def __init__(self, store: CredentialStore) -> None:
        """绑定存储。

        Args:
            store: 当前连接的凭据仓储。
        Returns:
            无。
        Raises:
            无。
        """
        self.store = store
        self._identity: str | None = None
        self._last_token: str | None = None

    async def acquire(self, *, refresh: bool = False) -> ModelCredential:
        """读取当前凭据，锁内刷新并原子提交。

        Args:
            refresh: 请求拒绝后是否要求刷新。
        Returns:
            当前有效凭据及身份。
        Raises:
            ModelAuthenticationError: 未登录、权限缺失或刷新终止。
            TimeoutError: 锁长时间被其他进程占用。
            httpx.HTTPError: 临时网络或服务异常。
        """
        lock = self.store.lock()
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise TimeoutError("ChatGPT 凭据正被其他进程更新，请稍后重试") from None
                await asyncio.sleep(LOCK_POLL_SECONDS)
        try:
            registration = self.store.load()
            if registration is None or not registration.refresh_token:
                raise ModelAuthenticationError("ChatGPT 登录已失效，请运行 dayu-cli init --provider chatgpt 重新登录")
            if self._identity is not None and self._identity != registration.identity:
                raise ModelAuthenticationError("ChatGPT 账号已改变，请重新发起研究")
            if PLAN_SCOPE not in registration.scopes:
                raise ModelAuthenticationError("ChatGPT 尚未授权套餐调用，请通过 init 重新授权")
            force_refresh = refresh and self._last_token == registration.access_token
            if force_refresh or registration.expires_at <= time.time() + REFRESH_MARGIN_SECONDS:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        TOKEN_ENDPOINT,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": registration.client_id,
                            "refresh_token": registration.refresh_token,
                            "resource": API_RESOURCE,
                        },
                    )
                    # 临时 HTTP 错误不依赖 JSON，代理的 HTML/空响应也必须进入退避路径。
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    try:
                        raw: JsonValue = response.json()
                    except ValueError:
                        response.raise_for_status()
                        raise ValueError("认证响应不是有效 JSON") from None
                    if not isinstance(raw, dict):
                        response.raise_for_status()
                        raise ValueError("认证响应必须是 JSON 对象")
                    data = raw
                    if response.is_error:
                        code = data.get("error")
                        if isinstance(code, str) and code in TERMINAL_REFRESH_ERRORS:
                            self.store.save(
                                replace(registration, access_token="", refresh_token="", id_token="", expires_at=0)
                            )
                            raise ModelAuthenticationError(
                                "ChatGPT 授权已失效，请运行 dayu-cli init --provider chatgpt 重新登录"
                            )
                        if code == "invalid_client":
                            raise ModelAuthenticationError("ChatGPT 客户端注册无效，请重新配置 provider")
                        response.raise_for_status()
                    expires_in = data.get("expires_in")
                    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
                        raise ValueError("认证响应缺少有效 expires_in")
                    scopes = data.get("scope")
                    registration = replace(
                        registration,
                        access_token=require_text(data, "access_token"),
                        refresh_token=require_text(data, "refresh_token"),
                        expires_at=time.time() + expires_in,
                        scopes=tuple(scopes.split()) if isinstance(scopes, str) else registration.scopes,
                        generation=uuid4().hex,
                    )
                    self.store.save(registration)
                    if PLAN_SCOPE not in registration.scopes:
                        raise ModelAuthenticationError("ChatGPT 套餐调用授权已缺失，请通过 init 重新授权")
            self._identity = registration.identity
            self._last_token = registration.access_token
            return ModelCredential(registration.access_token, registration.identity)
        finally:
            lock.release()


async def list_models(provider: ModelCredentialProvider) -> list[dict[str, JsonValue]]:
    """读取账号可见的模型目录，不将目录视为推理成功证明。

    Args:
        provider: 已登录账号的凭据提供器。
    Returns:
        可展示的模型信息。
    Raises:
        ModelAuthenticationError: 无有效授权。
        httpx.HTTPError: 模型请求失败。
        ValueError: 目录格式不正确。
    """
    credential = await provider.acquire()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{API_RESOURCE}/models", headers={"Authorization": f"Bearer {credential.access_token}"}
        )
    response.raise_for_status()
    data: dict[str, JsonValue] = response.json()
    models = data.get("models")
    if not isinstance(models, list):
        raise ValueError("ChatGPT 模型目录结构不正确")
    return [row for row in models if isinstance(row, dict) and row.get("visibility") == "list"]
