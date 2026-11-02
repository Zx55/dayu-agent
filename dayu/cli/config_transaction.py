"""CLI 配置提交的进程内回滚，不承担账号授权或模型选择语义。"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def write_config_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """原子替换单个配置文件。

    Args:
        path: 目标文件。
        data: 待写入内容。
        mode: 目标权限，恢复已有配置时保留原权限。
    Returns:
        无。
    Raises:
        OSError: 写入或替换失败。
    """
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dayu-config-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def configuration_transaction(paths: tuple[Path, ...]) -> Iterator[None]:
    """在调用方持有配置及凭据锁期间保存快照，异常时恢复本次写入。

    Args:
        paths: 本次提交可能修改的确切文件。
    Yields:
        无。提交由 with 体完成。
    Raises:
        BaseException: 恢复配置后传播原始失败或取消。
        OSError: 文件系统故障导致恢复失败。
    """
    snapshots = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None for path in paths}
    try:
        yield
    except BaseException:
        for path, snapshot in snapshots.items():
            if snapshot is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != snapshot[0]:
                write_config_bytes(path, snapshot[0], mode=snapshot[1])
        raise
