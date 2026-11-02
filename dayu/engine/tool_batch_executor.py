"""协议无关的工具批次执行，两个模型 Runner 共享取消和结果契约。"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, NotRequired, TypedDict, cast

from dayu.contracts.agent_types import ExtraContentPayload, JsonValue
from dayu.contracts.cancellation import CancellationToken
from dayu.contracts.protocols import ToolExecutor
from dayu.contracts.protocols import ToolExecutionContext
from dayu.engine.cancellation import await_or_cancel, create_cancellation_waiter
from dayu.engine.events import (
    StreamEvent,
    error_event,
    tool_call_dispatched,
    tool_call_result,
    tool_calls_batch_done,
    tool_calls_batch_ready,
)
from dayu.engine.tool_result import build_error, get_error_code, is_tool_success, validate_tool_result_contract


class ToolCall(TypedDict):
    """已完成参数校验的工具请求。"""

    id: str
    name: str
    arguments: dict[str, JsonValue]
    index_in_iteration: int
    extra_content: NotRequired[ExtraContentPayload]


class ToolCallResult(ToolCall):
    """带原始请求身份的工具结果。"""

    result: dict[str, JsonValue]


class ToolBatchExecutor:
    """仅依赖 ToolExecutor，不包含模型传输或业务知识。"""

    def __init__(
        self, executor: ToolExecutor | None, *, timeout: float | None, cancellation_token: CancellationToken | None
    ) -> None:
        """绑定工具执行环境。

        Args:
            executor: 工具注册表。
            timeout: 单工具超时。
            cancellation_token: 宿主取消令牌。
        Returns:
            无。
        Raises:
            无。
        """
        self.executor = executor
        self.timeout = timeout
        self.cancellation_token = cancellation_token

    def _check_cancelled(self) -> None:
        """观察宿主取消。

        Args:
            无。
        Returns:
            无。
        Raises:
            CancelledError: 宿主已取消。
        """
        if self.cancellation_token:
            self.cancellation_token.raise_if_cancelled()

    async def execute_one(self, tool_call: ToolCall, request_id: str, trace_meta: dict[str, str]) -> ToolCallResult:
        """执行单工具，超时仍保留后台线程可能继续的事实。

        Args:
            call: 完整请求。
            trace: 运行与迭代身份。
        Returns:
            规范工具结果。
        Raises:
            RuntimeError: 未设置执行器。
            CancelledError: 宿主已取消。
        """
        call, trace = tool_call, trace_meta
        self._check_cancelled()
        executor = self.executor
        if executor is None:
            raise RuntimeError("未设置工具执行器")
        child = CancellationToken()
        unregister = self.cancellation_token.on_cancel(child.cancel) if self.cancellation_token else None
        context = ToolExecutionContext(
            run_id=trace.get("run_id"),
            iteration_id=trace.get("iteration_id"),
            tool_call_id=call["id"],
            index_in_iteration=call["index_in_iteration"],
            timeout_seconds=self.timeout,
            cancellation_token=child,
        )
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(executor.execute, name=call["name"], arguments=call["arguments"], context=context),
                self.timeout,
            )
        except asyncio.TimeoutError:
            child.cancel()
            cooperative = bool(executor.get_execution_context_param_name(call["name"]))
            timeout_meta: dict[str, JsonValue] = {"execution_may_continue": not cooperative}
            if not cooperative:
                timeout_meta["orphan_thread_warning"] = True
            result = build_error(
                "tool_execution_timeout",
                "tool execution timeout",
                hint=(
                    "tool execution timed out and cancellation was requested"
                    if cooperative
                    else "tool execution may still be running; do not blindly retry"
                ),
                meta=timeout_meta,
            )
        except asyncio.CancelledError:
            child.cancel()
            result = build_error("cancelled", "tool execution cancelled")
        except Exception as exc:
            result = build_error("execution_error", str(exc))
        finally:
            if unregister:
                unregister()
        contract_error = validate_tool_result_contract(result)
        if contract_error:
            result = build_error("invalid_result", contract_error)
        meta = result.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta.update(tool=call["name"], latency_ms=int((time.monotonic() - start) * 1000))
        result["meta"] = meta
        completed: ToolCallResult = {**call, "result": cast(dict[str, JsonValue], result)}
        return completed

    async def _execute_all(self, calls: list[ToolCall], request_id: str, trace: dict[str, str]) -> list[ToolCallResult]:
        """在取消观察已建立后调度工具，避免预先创建无人等待的 Future。

        Args:
            calls: 已排序工具请求。
            request_id: 模型请求身份。
            trace: 运行与迭代身份。
        Returns:
            按请求顺序排列的结果。
        Raises:
            CancelledError: 外层取消会传播到整个 gather。
        """
        return list(await asyncio.gather(*(self.execute_one(call, request_id, trace) for call in calls)))

    async def execute_batch(
        self, tool_calls: list[ToolCall], request_id: str, trace_meta: dict[str, str]
    ) -> AsyncIterator[StreamEvent]:
        """执行一批请求并产出稳定 Engine 事件。

        Args:
            calls: 已校验请求。
            trace: 运行与请求身份。
        Returns:
            派发、结果和完成事件。
        Raises:
            CancelledError: 宿主取消，所有异步等待收口。
        """
        calls, trace = tool_calls, trace_meta
        self._check_cancelled()
        if not calls:
            return
        executor = self.executor
        if executor is None:
            yield error_event("tool executor is not set", recoverable=False, error_type="tool_executor_missing")
            return
        ordered = sorted(calls, key=lambda call: call["index_in_iteration"])
        ids = [call["id"] for call in ordered]
        for call in ordered:
            display, params = executor.get_tool_display_info(call["name"])
            event = tool_call_dispatched(
                tool_call_id=call["id"],
                name=call["name"],
                arguments=call["arguments"],
                index_in_iteration=call["index_in_iteration"],
                display_name=display,
                summary_params=params,
            )
            if "extra_content" in call:
                event.metadata["extra_content"] = call["extra_content"]
            event.metadata.update(trace)
            yield event
        yield tool_calls_batch_ready(ids, **trace)
        waiter, unregister = create_cancellation_waiter(self.cancellation_token)
        try:
            results = await await_or_cancel(
                self._execute_all(ordered, request_id, trace),
                operation_name="tool_batch",
                cancellation_waiter=waiter,
                cancellation_token=self.cancellation_token,
                raise_if_cancelled=self._check_cancelled,
                log_prefix="tool_batch",
            )
        finally:
            if unregister:
                unregister()
            if waiter is not None and not waiter.done():
                waiter.cancel()
        counts = {"ok": 0, "error": 0, "timeout": 0, "cancelled": 0}
        for result in results:
            code = get_error_code(result["result"])
            category = (
                "ok"
                if is_tool_success(result["result"])
                else "timeout" if code == "tool_execution_timeout" else "cancelled" if code == "cancelled" else "error"
            )
            counts[category] += 1
            display, _ = executor.get_tool_display_info(result["name"])
            event = tool_call_result(
                tool_call_id=result["id"],
                name=result["name"],
                arguments=result["arguments"],
                index_in_iteration=result["index_in_iteration"],
                result=result["result"],
                display_name=display,
            )
            if "extra_content" in result:
                event.data["extra_content"] = result["extra_content"]
            event.metadata.update(trace)
            yield event
        event = tool_calls_batch_done(
            ids, ok=counts["ok"], error=counts["error"], timeout=counts["timeout"], cancelled=counts["cancelled"]
        )
        event.metadata.update(trace)
        yield event
