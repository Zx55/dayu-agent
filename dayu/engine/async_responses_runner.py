"""原生 Responses Runner，只执行一次模型回合并复用 Dayu 工具执行器。"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Awaitable, TypeVar, cast
from uuid import uuid4

import aiohttp
import httpx

from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.cancellation import CancellationToken
from dayu.contracts.model_credentials import ModelAuthenticationError, ModelCredentialProvider
from dayu.contracts.protocols import ToolExecutor
from dayu.engine.cancellation import await_or_cancel, create_cancellation_waiter
from dayu.engine.events import StreamEvent, content_complete, content_delta, done_event, error_event
from dayu.engine.responses_codec import build_continuation, decode_output, encode_messages, encode_tools
from dayu.engine.responses_stream_parser import ResponsesFailure, parse_events
from dayu.engine.tool_batch_executor import ToolBatchExecutor

_WaitResult = TypeVar("_WaitResult")
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
AUTH_REFRESH_CODES = frozenset({"token_expired", "access_token_expired", "invalid_token"})
TEMPORARY_CODES = frozenset({"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"})
CONTEXT_OVERFLOW_CODES = frozenset({"context_length_exceeded", "context_window_exceeded"})
CONTINUABLE_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "max_messages"})
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


class AsyncResponsesRunner:
    """以完整客户端历史调用 Responses，不保存隐藏的会话状态。"""

    def __init__(
        self,
        *,
        model: str,
        credentials: ModelCredentialProvider,
        reasoning_effort: str = "medium",
        timeout: float = 3600,
        stream_idle_timeout: float = 120,
        tool_timeout: float = 90,
        max_retries: int = 2,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        """绑定显式协议参数。

        Args:
            model: 账号目录中的模型标识。
            credentials: 运行时认证提供器。
            reasoning_effort: 推理档位。
            timeout: 单次请求总超时。
            stream_idle_timeout: SSE 空闲超时。
            tool_timeout: 单工具超时。
            max_retries: 尚未输出内容时临时失败的重试次数。
            cancellation_token: 宿主取消令牌。
        Returns:
            无。
        Raises:
            ValueError: 显式配置非法。
        """
        if (
            not model
            or reasoning_effort not in REASONING_EFFORTS
            or min(timeout, stream_idle_timeout, tool_timeout) <= 0
            or max_retries < 0
        ):
            raise ValueError("Responses 模型或运行参数非法")
        self.model = model
        self.credentials = credentials
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.stream_idle_timeout = stream_idle_timeout
        self.max_retries = max_retries
        self.cancellation_token = cancellation_token
        self._tool_batch = ToolBatchExecutor(None, timeout=tool_timeout, cancellation_token=cancellation_token)
        self._session: aiohttp.ClientSession | None = None

    def set_tools(self, executor: ToolExecutor | None) -> None:
        """设置本轮允许的工具，None 表示降级为纯文本。

        Args:
            executor: 已受宿主权限约束的工具注册表。
        Returns:
            无。
        Raises:
            无。
        """
        self._tool_batch.executor = executor

    def is_supports_tool_calling(self) -> bool:
        """声明本地函数调用能力。

        Args:
            无。
        Returns:
            True。
        Raises:
            无。
        """
        return True

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

    async def _wait(self, operation: Awaitable[_WaitResult]) -> _WaitResult:
        """在每个阻塞边界响应取消并注销回调。

        Args:
            operation: 当前网络或退避操作。
        Returns:
            操作结果。
        Raises:
            CancelledError: 宿主或外层 task 取消。
            Exception: 原始操作异常。
        """
        waiter, unregister = create_cancellation_waiter(self.cancellation_token)
        try:
            return await await_or_cancel(
                operation,
                operation_name="responses",
                cancellation_waiter=waiter,
                cancellation_token=self.cancellation_token,
                raise_if_cancelled=self._check_cancelled,
                log_prefix="responses",
            )
        finally:
            if unregister:
                unregister()
            if waiter is not None and not waiter.done():
                waiter.cancel()

    async def close(self) -> None:
        """关闭本次运行复用的 HTTP 会话。

        Args:
            无。
        Returns:
            无。
        Raises:
            无。
        """
        if self._session:
            await self._session.close()
            self._session = None

    async def call(
        self, messages: list[AgentMessage], *, stream: bool = True, **extra_payloads: JsonValue
    ) -> AsyncIterator[StreamEvent]:
        """生成一轮输出，成功终态后才提交完整工具批次。

        Args:
            messages: 当前完整历史。
            stream: 保留契约，账号路径始终使用 SSE。
            extra_payloads: 仅接受内部 trace_context，不透传未知字段。
        Returns:
            Dayu Engine 事件。
        Raises:
            CancelledError: 取消必须直接向宿主传播。
        """
        trace_value = extra_payloads.pop("trace_context", None)
        trace = (
            {key: value for key, value in trace_value.items() if isinstance(value, str)}
            if isinstance(trace_value, dict)
            else {}
        )
        trace.setdefault("request_id", uuid4().hex)
        if extra_payloads:
            yield error_event(
                "Responses 不接受未声明的扩展参数", recoverable=False, error_type="responses_invalid_config"
            )
            return
        response: aiohttp.ClientResponse | None = None
        emitted = False
        completed: dict[str, JsonValue] | None = None
        credential_identity = ""
        incomplete_reason: str | None = None
        refreshed = False
        request_refresh = False
        for attempt in range(self.max_retries + 2):
            self._check_cancelled()
            try:
                credential = await self._wait(self.credentials.acquire(refresh=request_refresh))
                request_refresh = False
                credential_identity = credential.identity
                executor = self._tool_batch.executor
                payload: dict[str, JsonValue] = {
                    "model": self.model,
                    "input": encode_messages(messages, identity=credential.identity, model=self.model),
                    "store": False,
                    "stream": True,
                    "reasoning": {"effort": self.reasoning_effort},
                    "include": ["reasoning.encrypted_content"],
                }
                if executor:
                    payload["tools"] = encode_tools(cast(list[dict[str, JsonValue]], executor.get_schemas()))
                if self._session is None:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.timeout, sock_read=self.stream_idle_timeout)
                    )
                response = await self._wait(
                    self._session.post(
                        RESPONSES_ENDPOINT, json=payload, headers={"Authorization": f"Bearer {credential.access_token}"}
                    )
                )
                if response.status >= 400:
                    try:
                        data: JsonValue = json.loads(await self._wait(response.text()))
                    except ValueError:
                        data = {}
                    error = data.get("error") if isinstance(data, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    raise ResponsesFailure(
                        str(code or f"http_{response.status}"),
                        response.status,
                        response.headers.get("x-request-id", ""),
                    )
                iterator = parse_events(response).__aiter__()
                while True:
                    try:
                        event = await self._wait(anext(iterator))
                    except StopAsyncIteration:
                        break
                    kind = event.get("type")
                    if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
                        emitted = True
                        yield content_delta(str(event["delta"]), **trace)
                    elif kind == "response.completed":
                        value = event.get("response")
                        if not isinstance(value, dict) or value.get("status") != "completed":
                            raise ValueError("Responses 完成终态无效")
                        completed = value
                        break
                    elif kind == "response.incomplete":
                        value = event.get("response")
                        details = value.get("incomplete_details") if isinstance(value, dict) else None
                        reason = details.get("reason") if isinstance(details, dict) else None
                        if (
                            not isinstance(value, dict)
                            or value.get("status") != "incomplete"
                            or not isinstance(reason, str)
                        ):
                            raise ValueError("Responses incomplete 终态无效")
                        if reason not in CONTINUABLE_INCOMPLETE_REASONS and reason != "content_filter":
                            raise ResponsesFailure("response_incomplete_unsupported_reason")
                        incomplete_reason = reason
                        completed = value
                        break
                    elif kind in {"response.failed", "error"}:
                        value = event.get("response")
                        error = value.get("error") if isinstance(value, dict) else event
                        code = error.get("code") if isinstance(error, dict) else None
                        raise ResponsesFailure(str(code or kind), request_id=response.headers.get("x-request-id", ""))
                if completed is None:
                    raise ValueError("Responses 流未收到 response.completed")
                break
            except ModelAuthenticationError as exc:
                yield error_event(str(exc), recoverable=False, error_type="model_authentication_required")
                return
            except ResponsesFailure as exc:
                if not emitted and exc.code in AUTH_REFRESH_CODES and not refreshed:
                    refreshed = True
                    request_refresh = True
                    continue
                transient = exc.code in TEMPORARY_CODES or exc.status in {429, 500, 502, 503, 504}
                if exc.code == "subscription_sharing_usage_limit_exceeded":
                    transient = False
                if transient and not emitted and attempt < self.max_retries:
                    await self._wait(asyncio.sleep(2**attempt))
                    continue
                yield error_event(
                    f"Responses 请求失败（{exc.code}），请检查账号授权、额度或模型配置",
                    recoverable=False,
                    error_type="context_overflow" if exc.code in CONTEXT_OVERFLOW_CODES else exc.code,
                    provider_error_code=exc.code,
                    http_status=exc.status,
                    provider_request_id=exc.request_id,
                )
                return
            except (aiohttp.ClientError, httpx.HTTPError, TimeoutError) as exc:
                transient = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if transient and not emitted and attempt < self.max_retries:
                    await self._wait(asyncio.sleep(2**attempt))
                    continue
                yield error_event(
                    f"Responses 网络请求失败（{type(exc).__name__}）",
                    recoverable=False,
                    error_type="responses_transport_error",
                )
                return
            except ValueError:
                yield error_event(
                    "Responses 配置或响应协议校验失败", recoverable=False, error_type="responses_protocol_error"
                )
                return
            finally:
                if response:
                    response.release()
                    response = None
        if completed is None:
            yield error_event("Responses 重试次数耗尽", recoverable=False, error_type="responses_retry_exhausted")
            return
        try:
            text, calls, output = decode_output(completed.get("output"))
        except ValueError:
            yield error_event(
                "Responses 工具或输出协议校验失败", recoverable=False, error_type="responses_protocol_error"
            )
            return
        if calls and incomplete_reason is not None:
            yield error_event(
                "未完成响应中的工具请求不可执行", recoverable=False, error_type="responses_incomplete_tool_call"
            )
            return
        if not text and not calls and incomplete_reason != "content_filter":
            yield error_event(
                "Responses 完成但没有正文或工具请求", recoverable=False, error_type="responses_empty_output"
            )
            return
        if calls and self._tool_batch.executor is None:
            yield error_event("禁用工具时收到工具调用", recoverable=False, error_type="tool_executor_missing")
            return
        yield content_complete(
            text, model_continuation=build_continuation(output, identity=credential_identity, model=self.model), **trace
        )
        async for event in self._tool_batch.execute_batch(calls, trace["request_id"], trace):
            yield event
        usage = completed.get("usage")
        mapped_usage = (
            {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            if isinstance(usage, dict)
            else {}
        )
        is_filtered = incomplete_reason == "content_filter"
        is_truncated = incomplete_reason in CONTINUABLE_INCOMPLETE_REASONS
        finish_reason = (
            "content_filter" if is_filtered else "length" if is_truncated else "tool_calls" if calls else "stop"
        )
        yield done_event(
            {
                "finish_reason": finish_reason,
                "truncated": is_truncated,
                "content_filtered": is_filtered,
                "usage": mapped_usage,
            },
            **trace,
        )
