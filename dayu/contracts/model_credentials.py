"""模型认证的最小运行时契约，不包含交互或配置文件知识。"""

from dataclasses import dataclass
from typing import Protocol


class ModelAuthenticationError(RuntimeError):
    """认证不可恢复，宿主必须终止本次运行而非强制生成答案。"""


@dataclass(frozen=True, repr=False)
class ModelCredential:
    """一次请求使用的凭据与稳定注册身份，禁止打印 token。"""

    access_token: str
    identity: str


class ModelCredentialProvider(Protocol):
    """提供可刷新的模型凭据。"""

    async def acquire(self, *, refresh: bool = False) -> ModelCredential:
        """获取凭据。

        Args:
            refresh: 是否强制尝试刷新当前凭据。
        Returns:
            凭据及注册身份。
        Raises:
            ModelAuthenticationError: 需要重新配置认证。
        """
        ...
