"""网页文档转换的进程边界，不允许原生 OCR 拖住工具线程的退出。

输入和结果通过私有临时目录传递，避免大型文档或半写入 IPC 消息阻塞取消。
父进程拥有 deadline、取消、终止和回收，子进程只负责 Docling 转换。
"""

from __future__ import annotations

import json
import math
import multiprocessing
import time
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory

from dayu.contracts.agent_types import JsonValue
from dayu.contracts.cancellation import CancellationToken
from dayu.docling_runtime import DoclingRuntimeInitializationError, convert_pdf_bytes_with_docling

_POLL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 1.0
_INPUT_NAME = "input"
_RESULT_NAME = "result.json"
_TEMP_RESULT_NAME = "result.tmp"


def _convert_document(raw_bytes: bytes, stream_name: str) -> tuple[str, str, str]:
    """在 worker 中完成现有 Docling 转换及 Markdown 导出。

    Args:
        raw_bytes: 下载完成的文档。
        stream_name: 含扩展名的文档名称。
    Returns:
        标题、Markdown 和提取来源。
    Raises:
        DoclingRuntimeInitializationError: Docling 初始化失败。
        Exception: 原生转换或导出失败。
    """
    result = convert_pdf_bytes_with_docling(
        raw_bytes, stream_name=stream_name, do_ocr=True, do_table_structure=True,
        table_mode="accurate", do_cell_matching=True,
    )
    markdown = result.document.export_to_markdown().strip()
    title = next((line.strip().lstrip("#").strip() for line in markdown.splitlines() if line.strip().startswith("#")), "")
    return title, markdown, "docling"


def _document_worker_entry(directory: str, stream_name: str) -> None:
    """子进程只读取文档并原子发布结果，不持有 Host 或工具注册表。

    Args:
        directory: 父进程创建的私有临时目录。
        stream_name: 解析所用文档名称。
    Returns:
        无。
    Raises:
        OSError: 输入读取或结果写入失败，由父进程报告异常退出。
    """
    root = Path(directory)
    reply: dict[str, JsonValue]
    try:
        title, markdown, source = _convert_document((root / _INPUT_NAME).read_bytes(), stream_name)
        reply = {"ok": True, "title": title, "markdown": markdown, "source": source}
    except Exception as exc:
        reply = {
            "ok": False, "message": str(exc),
            "initialization_error": isinstance(exc, DoclingRuntimeInitializationError),
        }
    temporary = root / _TEMP_RESULT_NAME
    temporary.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
    temporary.replace(root / _RESULT_NAME)


def _reap_document_worker(process: BaseProcess) -> None:
    """回收 worker，SIGTERM 无效时升级 SIGKILL，不依赖 worker 自身线程退出。

    Args:
        process: 本次转换拥有的进程。
    Returns:
        无。
    Raises:
        RuntimeError: 强制终止后仍未回收，不能声称清理成功。
        OSError: 操作系统拒绝终止进程。
    """
    if process.pid is not None:
        if process.is_alive():
            process.terminate()
            process.join(timeout=_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            raise RuntimeError("Docling worker 未能终止")
        process.join(timeout=0)
    process.close()


def _read_document_result(path: Path) -> tuple[str, str, str]:
    """校验 worker 的数据结果，不反序列化任意 Python 对象。

    Args:
        path: 原子发布的 JSON 结果。
    Returns:
        标题、Markdown 和提取来源。
    Raises:
        DoclingRuntimeInitializationError: worker 初始化失败。
        RuntimeError: 转换失败或结果不符合契约。
        OSError: 读取失败。
    """
    try:
        data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError("Docling worker 结果不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Docling worker 结果必须是对象")
    if data.get("ok") is not True:
        message = data.get("message")
        if data.get("initialization_error") is True:
            raise DoclingRuntimeInitializationError(str(message))
        raise RuntimeError(f"Docling 转换失败: {message}")
    title, markdown, source = data.get("title"), data.get("markdown"), data.get("source")
    if not isinstance(title, str) or not isinstance(markdown, str) or not isinstance(source, str):
        raise RuntimeError("Docling worker 缺少有效 Markdown 结果")
    return title, markdown, source


def convert_web_document_to_markdown(
    raw_bytes: bytes,
    stream_name: str,
    *,
    timeout_seconds: float,
    cancellation_token: CancellationToken | None,
) -> tuple[str, str, str]:
    """在独立进程转换网页文档，所有返回和异常路径均回收 worker。

    Args:
        raw_bytes: 已下载文档。
        stream_name: 解析使用的名称。
        timeout_seconds: 当前工具调用剩余的总预算，包含 worker 启动。
        cancellation_token: 由工具执行上下文传入的取消令牌。
    Returns:
        标题、Markdown 和提取来源。
    Raises:
        TimeoutError: 预算耗尽。
        CancelledError: 宿主或工具取消。
        ValueError: 超时配置不是有限数。
        RuntimeError: worker 转换、协议或回收失败。
        OSError: worker 启动或文件操作失败。
    """
    if cancellation_token is not None:
        cancellation_token.raise_if_cancelled()
    if not math.isfinite(timeout_seconds):
        raise ValueError("Docling 转换超时必须是有限数")
    if timeout_seconds <= 0:
        raise TimeoutError("Docling 转换预算已耗尽")
    deadline = time.monotonic() + timeout_seconds
    with TemporaryDirectory(prefix="dayu-web-docling-") as directory:
        root = Path(directory)
        (root / _INPUT_NAME).write_bytes(raw_bytes)
        result = root / _RESULT_NAME
        process = multiprocessing.get_context("spawn").Process(
            target=_document_worker_entry, args=(directory, stream_name), daemon=True,
        )
        try:
            process.start()
            while True:
                if cancellation_token is not None:
                    cancellation_token.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Docling 转换超时")
                if result.exists():
                    return _read_document_result(result)
                if not process.is_alive():
                    # 文件发布与进程退出可能发生在两次检查之间，先接收最终结果。
                    if result.exists():
                        return _read_document_result(result)
                    raise RuntimeError(f"Docling worker 异常退出，exitcode={process.exitcode}")
                wait = min(_POLL_SECONDS, remaining)
                if cancellation_token is not None:
                    cancellation_token.wait(timeout=wait)
                else:
                    time.sleep(wait)
        finally:
            # 即使已经拿到 Markdown，Docling 自有线程也可能仍在收尾，不能无限 join。
            _reap_document_worker(process)
