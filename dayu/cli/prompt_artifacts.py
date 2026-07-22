"""CLI prompt Markdown 产物持久化。

本模块只负责把 prompt 已完成的最终回答可靠写入用户指定文件，
不参与事件消费、终端渲染或 Agent 执行。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_prompt_markdown(output_path: Path, content: str) -> Path:
    """以原子替换方式写入 prompt 最终 Markdown。

    Args:
        output_path: 用户指定的 Markdown 输出文件路径。
        content: Dayu 最终回答正文。

    Returns:
        展开用户目录并解析后的绝对输出路径。

    Raises:
        OSError: 创建目录、写入临时文件或原子替换失败时抛出。
    """

    resolved_path = output_path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path_text = tempfile.mkstemp(
        prefix=f".{resolved_path.name}.",
        suffix=".tmp",
        dir=resolved_path.parent,
    )
    temp_path = Path(temp_path_text)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, resolved_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
    return resolved_path


__all__ = ["write_prompt_markdown"]
