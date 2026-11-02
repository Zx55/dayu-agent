"""受保护的账号注册与凭据存储，锁覆盖刷新及手动替换。"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4
from typing import Iterator

from filelock import FileLock

from dayu.contracts.model_credentials import ModelAuthenticationError

DEFAULT_ACCOUNT_REF = "chatgpt"


@dataclass(frozen=True, repr=False)
class ChatGPTRegistration:
    """单个连接的完整注册，清空 token 后仍保留重新授权身份。"""

    client_id: str
    host_id: str
    subject: str
    email: str
    access_token: str
    refresh_token: str
    id_token: str
    expires_at: float
    scopes: tuple[str, ...]
    generation: str

    @property
    def identity(self) -> str:
        """返回注册与身份的组合键。

        Args:
            无。
        Returns:
            非秘密身份键。
        Raises:
            无。
        """
        return f"{self.client_id}:{self.subject}"


class CredentialStore:
    """每个连接一份凭据，文件写入在同一目录内原子替换。"""

    def __init__(self, account_ref: str = DEFAULT_ACCOUNT_REF, *, root: Path | None = None) -> None:
        """定位存储。

        Args:
            account_ref: 只含安全文件名字符的连接名。
            root: 测试或部署显式指定的凭据目录。
        Returns:
            无。
        Raises:
            ValueError: 连接名非法。
        """
        if not account_ref or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in account_ref
        ):
            raise ValueError("凭据连接名仅允许字母、数字、下划线与连字符")
        self.root = root or Path.home() / ".config" / "dayu" / "auth"
        self.path = self.root / f"{account_ref}.json"

    def lock(self) -> FileLock:
        """建立同连接跨进程锁。

        Args:
            无。
        Returns:
            有界等待的锁对象。
        Raises:
            OSError: 无法创建私有目录。
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        return FileLock(str(self.path.with_suffix(".lock")), timeout=30, mode=0o600)

    def load(self) -> ChatGPTRegistration | None:
        """读取凭据，不记录异常原文中的秘密。

        Args:
            无。
        Returns:
            已保存注册，缺失时为 None。
        Raises:
            ModelAuthenticationError: 文件不完整或不安全。
        """
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ModelAuthenticationError("ChatGPT 凭据文件权限不安全，请设为 0600")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return ChatGPTRegistration(
                client_id=str(data["client_id"]),
                host_id=str(data["host_id"]),
                subject=str(data["subject"]),
                email=str(data["email"]),
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                id_token=str(data["id_token"]),
                expires_at=float(data["expires_at"]),
                scopes=tuple(data["scopes"]),
                generation=str(data["generation"]),
            )
        except (ValueError, KeyError, TypeError, OSError):
            raise ModelAuthenticationError("ChatGPT 凭据不可读取，请通过 init 重新配置") from None

    def save(self, registration: ChatGPTRegistration) -> None:
        """原子保存完整注册，调用方持有连接锁。

        Args:
            registration: 已校验的新注册或刷新结果。
        Returns:
            无。
        Raises:
            OSError: 写入失败，旧文件保持完整。
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        descriptor, name = tempfile.mkstemp(dir=self.root, prefix=".credential-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(asdict(registration), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    @contextmanager
    def preserve_registration(self) -> Iterator[None]:
        """在调用方持连接锁期间保护旧注册，配置提交失败时恢复。

        Args:
            无。
        Yields:
            无。调用方在提交配置后通过 save 激活候选注册。
        Raises:
            BaseException: 恢复后传播原始异常，包括用户中断。
            OSError: 文件系统故障导致恢复失败。
        """
        previous = self.load()
        snapshot = self.path.read_bytes() if previous is not None else None
        try:
            yield
        except BaseException:
            if previous is None:
                self.path.unlink(missing_ok=True)
            elif not self.path.exists() or self.path.read_bytes() != snapshot:
                self.save(previous)
            raise

    def host_id(self) -> str:
        """读取或创建该安装稳定的 host ID，调用方持锁。

        Args:
            无。
        Returns:
            不含个人信息的主机标识。
        Raises:
            OSError: 持久化失败。
        """
        path = self.root / "host-id"
        try:
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(f"urn:uuid:{uuid4()}")
        except FileExistsError:
            pass
        return path.read_text(encoding="utf-8").strip()
