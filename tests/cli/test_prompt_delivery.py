"""真实事件流和默认线程池关闭之间的 prompt 交付回归。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Event, Thread
from typing import AsyncIterator, cast

import pytest

from dayu.cli import interactive_ui as ui
from dayu.contracts.events import AppEvent, AppEventType
from dayu.services.contracts import ChatTurnRequest, ChatTurnSubmission, PromptRequest, PromptSubmission
from dayu.services.protocols import ChatServiceProtocol, PromptServiceProtocol


@pytest.mark.parametrize("labeled", [False, True])
@pytest.mark.parametrize("failed_stream,failed_write", [(False, False), (True, False), (False, True)])
def test_prompt_delivers_before_default_executor_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labeled: bool, failed_stream: bool, failed_write: bool
) -> None:
    """工具线程未退出时交付已完成答案，流失败时保留旧文件。

    Args:
        tmp_path: 隔离输出目录。
        monkeypatch: 只替代服务提交和动画，不替代 asyncio 清理。
        labeled: 覆盖单轮与 label 入口。
        failed_stream: 最终事件后服务流是否异常结束。
        failed_write: 模拟原子写入失败。
    Returns:
        无。
    Raises:
        AssertionError: 交付或状态被线程池关闭拖延，或错误覆盖了旧文件。
    """
    destination = tmp_path / "answer.md"
    destination.write_text("旧答案", encoding="utf-8")
    worker_release, worker_started, stream_finished, status_stopped = Event(), Event(), Event(), Event()
    observed: list[tuple[str, bool]] = []
    writes: list[str] = []
    original_write = ui.write_prompt_markdown

    def write(path: Path, content: str) -> Path:
        """记录交付次数。参数为路径和正文，返回路径，故障时抛 OSError。"""
        writes.append(content)
        if failed_write:
            raise OSError("写入失败")
        return original_write(path, content)

    monkeypatch.setattr(ui, "write_prompt_markdown", write)

    class Status:
        """用事件记录状态已停止，不创建动画线程。"""

        def update(self, text: str) -> None:
            """接收状态。参数为显示文本，返回空，无异常。"""

        def pause(self) -> None:
            """暂停状态。无参数，返回空，无异常。"""

        def stop(self) -> None:
            """记录停止。无参数，返回空，无异常。"""
            status_stopped.set()

    def worker() -> None:
        """模拟未完成工具。无参数，返回空，无异常。"""
        worker_started.set()
        worker_release.wait(timeout=3)

    def observer() -> None:
        """在释放工具线程前检查交付。无参数，返回空，无异常。"""
        try:
            if not stream_finished.wait(timeout=2):
                return
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                if status_stopped.is_set() and (failed_stream or failed_write or destination.read_text() == "完整答案"):
                    break
                time.sleep(0.005)
            observed.append((destination.read_text(), status_stopped.is_set()))
        finally:
            worker_release.set()

    class Service:
        """保留真实 CLI 消费逻辑，仅提供受控服务事件。"""

        async def events(self) -> AsyncIterator[AppEvent]:
            """生成完成或失败事件。无参数，返回事件流，失败时抛 RuntimeError。"""
            task = asyncio.create_task(asyncio.to_thread(worker))
            while not worker_started.is_set():
                await asyncio.sleep(0.001)
            # 模拟工具等待已取消，但底层线程仍未退出。
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            yield AppEvent(AppEventType.FINAL_ANSWER, {"content": "完整答案"})
            stream_finished.set()
            if failed_stream:
                raise RuntimeError("服务流失败")
            yield AppEvent(AppEventType.DONE, {})

        async def submit(self, request: PromptRequest) -> PromptSubmission:
            """提交单轮。参数为请求，返回事件流，无异常。"""
            return PromptSubmission(session_id="fixture", event_stream=self.events())

        async def submit_turn(self, request: ChatTurnRequest) -> ChatTurnSubmission:
            """提交 label 轮次。参数为请求，返回事件流，无异常。"""
            return ChatTurnSubmission(session_id="fixture", event_stream=self.events())

    monkeypatch.setattr(ui, "_StatusLineController", Status)
    monitor = Thread(target=observer)
    monitor.start()
    try:
        if labeled:
            code = ui.conversation_prompt(
                cast(ChatServiceProtocol, Service()), "问题", label="fixture", session_id="fixture",
                scene_name="prompt_mt", output_path=destination,
            )
        else:
            code = ui.prompt(cast(PromptServiceProtocol, Service()), "问题", output_path=destination)
    finally:
        worker_release.set()
        monitor.join(timeout=3)
    assert observed == [("旧答案" if failed_stream or failed_write else "完整答案", True)]
    assert code == (2 if failed_stream or failed_write else 0)
    assert writes == ([] if failed_stream else ["完整答案"])
