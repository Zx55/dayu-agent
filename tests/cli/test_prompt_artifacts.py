"""CLI prompt Markdown 产物写入测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dayu.cli import prompt_artifacts


@pytest.mark.unit
def test_write_prompt_markdown_creates_parent_and_replaces_content(tmp_path: Path) -> None:
    """验证 Markdown 产物会创建父目录并替换既有内容。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        无。

    Raises:
        AssertionError: 输出路径或内容不符合预期时抛出。
    """

    output_path = tmp_path / "nested" / "answer.md"
    written_path = prompt_artifacts.write_prompt_markdown(output_path, "# 第一版")
    prompt_artifacts.write_prompt_markdown(output_path, "# 第二版")

    assert written_path == output_path.resolve()
    assert output_path.read_text(encoding="utf-8") == "# 第二版"


@pytest.mark.unit
def test_write_prompt_markdown_cleans_temp_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证原子替换失败时会清理临时文件。

    Args:
        monkeypatch: pytest monkeypatch 工具。
        tmp_path: pytest 临时目录。

    Returns:
        无。

    Raises:
        AssertionError: 临时文件未清理时抛出。
    """

    output_path = tmp_path / "answer.md"

    def _raise_replace_error(_source: Path, _target: Path) -> None:
        """模拟原子替换失败。

        Args:
            _source: 临时文件路径。
            _target: 目标文件路径。

        Returns:
            无。

        Raises:
            OSError: 始终抛出模拟错误。
        """

        raise OSError("replace failed")

    monkeypatch.setattr(prompt_artifacts.os, "replace", _raise_replace_error)

    with pytest.raises(OSError, match="replace failed"):
        prompt_artifacts.write_prompt_markdown(output_path, "# 报告")

    assert list(tmp_path.iterdir()) == []
