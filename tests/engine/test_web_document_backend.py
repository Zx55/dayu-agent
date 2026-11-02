"""网页 Docling worker 的真实 spawn、取消、回收和结果契约测试。"""

from __future__ import annotations

import asyncio
import json
import math
import multiprocessing
import os
import signal
import time
from functools import partial
from multiprocessing.context import SpawnProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest

from dayu.contracts.agent_types import JsonValue
from dayu.contracts.cancellation import CancelledError, CancellationToken
from dayu.docling_runtime import DoclingRuntimeInitializationError
from dayu.engine.tools import web_document_backend as backend


def _controlled_worker(directory: str, stream_name: str) -> None:
    """以真实子进程模拟成功、崩溃和无法协作取消的原生计算。

    Args:
        directory: 私有 IPC 目录。
        stream_name: 测试情形。
    Returns:
        无。
    Raises:
        OSError: IPC 文件无法写入。
    """
    root = Path(directory)
    if stream_name == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "started").write_text(str(os.getpid()))
    if stream_name == "crash":
        os._exit(7)
    if stream_name in {"hang", "ignore_term"}:
        time.sleep(8)
        return
    data: dict[str, JsonValue] = {
        "ok": True, "title": "Fixture", "markdown": "正文" * (1000000 if stream_name == "large" else 1),
        "source": "docling",
    }
    temporary = root / "result.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(root / "result.json")
    if stream_name == "success_hang":
        time.sleep(8)


@pytest.fixture
def isolated_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """只替代转换内容，保留生产进程监控和回收。

    Args:
        tmp_path: 隔离目录。
        monkeypatch: 注入可 pickle 的顶层 worker。
    Returns:
        IPC 目录的父目录。
    Raises:
        无。
    """
    monkeypatch.setattr(backend, "_document_worker_entry", _controlled_worker)
    monkeypatch.setattr(backend, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    return tmp_path


def _wait_until_worker_started(root: Path, token: CancellationToken) -> None:
    """确认 worker 启动后再取消，防止只测到启动前拒绝。

    Args:
        root: IPC 父目录。
        token: 本次调用的取消令牌。
    Returns:
        无。
    Raises:
        无，未启动时也会取消以保证测试有界。
    """
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if list(root.glob("*/started")):
            break
        time.sleep(0.005)
    token.cancel()


@pytest.mark.parametrize("scenario", ["success", "large", "success_hang", "crash", "hang", "ignore_term"])
def test_real_worker_lifecycle(isolated_worker: Path, scenario: str) -> None:
    """成功、异常、超时和取消都不留下 worker 或 IPC 目录。

    Args:
        isolated_worker: 启用真实 spawn 的隔离目录。
        scenario: 子进程行为。
    Returns:
        无。
    Raises:
        AssertionError: 回收或数据传递不符合契约。
    """
    children = {process.pid for process in multiprocessing.active_children()}
    token = CancellationToken()
    canceller: Thread | None = None
    if scenario == "ignore_term":
        canceller = Thread(target=_wait_until_worker_started, args=(isolated_worker, token))
        canceller.start()
    started = time.monotonic()
    try:
        if scenario in {"success", "large", "success_hang"}:
            title, markdown, source = backend.convert_web_document_to_markdown(
                b"fixture", scenario, timeout_seconds=5, cancellation_token=token,
            )
            assert title == "Fixture" and source == "docling"
            assert markdown == "正文" * (1000000 if scenario == "large" else 1)
        else:
            expected = CancelledError if scenario == "ignore_term" else TimeoutError if scenario == "hang" else RuntimeError
            with pytest.raises(expected):
                backend.convert_web_document_to_markdown(
                    b"fixture", scenario, timeout_seconds=1.5 if scenario == "hang" else 5, cancellation_token=token,
                )
    finally:
        if canceller is not None:
            canceller.join(timeout=4)
    assert time.monotonic() - started < 6
    assert {process.pid for process in multiprocessing.active_children()} == children
    assert not list(isolated_worker.iterdir())
    # 上次取消不能污染下一次转换。
    assert backend.convert_web_document_to_markdown(
        b"fixture", "success", timeout_seconds=5, cancellation_token=CancellationToken(),
    ) == ("Fixture", "正文", "docling")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_never_starts_worker(isolated_worker: Path, timeout: float) -> None:
    """无预算时不启动进程。参数为隔离目录和超时，返回空，断言失败时抛 AssertionError。"""
    with pytest.raises(ValueError if not math.isfinite(timeout) else TimeoutError):
        backend.convert_web_document_to_markdown(b"fixture", "hang", timeout_seconds=timeout, cancellation_token=None)
    assert not list(isolated_worker.iterdir())


def test_cancelled_before_start_never_creates_ipc(isolated_worker: Path) -> None:
    """已取消调用不创建 IPC。参数为隔离目录，返回空，断言失败时抛 AssertionError。"""
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancelledError):
        backend.convert_web_document_to_markdown(b"fixture", "hang", timeout_seconds=5, cancellation_token=token)
    assert not list(isolated_worker.iterdir())


def test_start_failure_cleans_resources(isolated_worker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动异常也清理 IPC。参数为隔离目录和 patch，返回空，断言失败时抛 AssertionError。"""
    def fail_start(self: SpawnProcess) -> None:
        """模拟启动失败。参数为进程，返回空，始终抛 OSError。"""
        raise OSError("cannot spawn")

    monkeypatch.setattr(SpawnProcess, "start", fail_start)
    with pytest.raises(OSError, match="cannot spawn"):
        backend.convert_web_document_to_markdown(b"fixture", "success", timeout_seconds=5, cancellation_token=None)
    assert not list(isolated_worker.iterdir())


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("invalid json", "不是有效 JSON"),
        ("[]", "必须是对象"),
        ('{"ok": true}', "缺少有效 Markdown"),
        ('{"ok": false, "message": "broken"}', "broken"),
    ],
)
def test_invalid_worker_result(tmp_path: Path, reply: str, expected: str) -> None:
    """拒绝损坏结果。参数为临时目录、结果和错误，返回空，断言失败时抛 AssertionError。"""
    path = tmp_path / "result.json"
    path.write_text(reply)
    with pytest.raises(RuntimeError, match=expected):
        backend._read_document_result(path)


@pytest.mark.parametrize("initialization", [False, True])
def test_worker_serializes_conversion_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialization: bool) -> None:
    """转换错误保留分类。参数为目录、patch 和错误类型，返回空，断言失败时抛 AssertionError。"""
    (tmp_path / "input").write_bytes(b"fixture")

    def convert(raw_bytes: bytes, stream_name: str) -> tuple[str, str, str]:
        """模拟转换异常。参数为文档和名称，返回三元组，始终抛指定异常。"""
        raise DoclingRuntimeInitializationError("init failed") if initialization else RuntimeError("conversion failed")

    monkeypatch.setattr(backend, "_convert_document", convert)
    backend._document_worker_entry(str(tmp_path), "fixture.pdf")
    with pytest.raises(DoclingRuntimeInitializationError if initialization else RuntimeError):
        backend._read_document_result(tmp_path / "result.json")


def test_worker_converts_and_publishes_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """转换和结果发布使用同一协议。参数为目录和 patch，返回空，断言失败时抛 AssertionError。"""
    class Document:
        """只模拟 Docling 的导出接口。"""

        def export_to_markdown(self) -> str:
            """返回带标题 Markdown。无参数，返回文本，无异常。"""
            return " # 标题\n\n正文 "

    class Result:
        """只模拟 Docling 转换结果。"""
        document = Document()

    monkeypatch.setattr(backend, "convert_pdf_bytes_with_docling", lambda *args, **kwargs: Result())
    (tmp_path / "input").write_bytes(b"fixture")
    backend._document_worker_entry(str(tmp_path), "fixture.pdf")
    assert backend._read_document_result(tmp_path / "result.json") == ("标题", "# 标题\n\n正文", "docling")


def test_timed_out_async_tool_does_not_block_executor_shutdown(isolated_worker: Path) -> None:
    """异步工具超时后，真实默认线程池有界退出。参数为隔离目录，返回空，断言失败时抛 AssertionError。"""
    children = {process.pid for process in multiprocessing.active_children()}

    async def request() -> None:
        """模拟 Runner 的线程超时与 token 取消。无参数，返回空，断言失败时抛 AssertionError。"""
        token = CancellationToken()
        task = asyncio.create_task(asyncio.to_thread(
            backend.convert_web_document_to_markdown, b"fixture", "hang", timeout_seconds=30, cancellation_token=token,
        ))
        deadline = time.monotonic() + 3
        while not list(isolated_worker.glob("*/started")) and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert list(isolated_worker.glob("*/started"))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=0.02)
        finally:
            token.cancel()

    started = time.monotonic()
    asyncio.run(request())
    assert time.monotonic() - started < 5
    assert {process.pid for process in multiprocessing.active_children()} == children
    assert not list(isolated_worker.iterdir())
