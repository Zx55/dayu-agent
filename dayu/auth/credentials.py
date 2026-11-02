"""Host 装配使用的凭据工厂，协议选择与认证来源分离。"""

import os
import hashlib

from dayu.auth.chatgpt import ChatGPTCredentialProvider
from dayu.auth.store import CredentialStore, DEFAULT_ACCOUNT_REF
from dayu.contracts.model_config import ResponsesAuthConfig
from dayu.contracts.model_credentials import ModelAuthenticationError, ModelCredential, ModelCredentialProvider


class EnvironmentCredentialProvider:
    """每次请求从指定环境变量读取 API key，不进入执行快照。"""

    def __init__(self, variable: str) -> None:
        """保存环境变量名。

        Args:
            variable: 凭据变量名。
        Returns:
            无。
        Raises:
            无。
        """
        self.variable = variable

    async def acquire(self, *, refresh: bool = False) -> ModelCredential:
        """读取 API key。

        Args:
            refresh: API key 无自动刷新，此参数不改变行为。
        Returns:
            当前 API key。
        Raises:
            ModelAuthenticationError: 变量缺失。
        """
        token = os.environ.get(self.variable, "")
        if not token:
            raise ModelAuthenticationError(f"请配置环境变量 {self.variable}")
        return ModelCredential(token, f"api_key:{self.variable}:{hashlib.sha256(token.encode()).hexdigest()}")


def build_credential_provider(config: ResponsesAuthConfig) -> ModelCredentialProvider:
    """从纯配置构造凭据提供器。

    Args:
        config: 认证类型和连接引用。
    Returns:
        不依赖 Host 的凭据提供器。
    Raises:
        ValueError: 认证配置非法。
    """
    if config.get("type") == "chatgpt":
        return ChatGPTCredentialProvider(CredentialStore(config.get("account_ref", DEFAULT_ACCOUNT_REF)))
    if config.get("type") == "api_key" and config.get("api_key_env"):
        return EnvironmentCredentialProvider(config.get("api_key_env", ""))
    raise ValueError("Responses auth 必须配置 chatgpt 连接或 api_key_env")
