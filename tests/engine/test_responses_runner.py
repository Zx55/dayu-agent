"""Responses 的真实 HTTP framing 与 Agent 工具循环集成测试。"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

import pytest
import httpx
from aiohttp import web

from dayu.contracts.agent_types import AgentMessage, JsonValue
from dayu.contracts.cancellation import CancellationToken, CancelledError
from dayu.contracts.model_credentials import ModelCredential, ModelAuthenticationError
from dayu.contracts.protocols import ToolExecutor
from dayu.engine.async_agent import AsyncAgent, AgentRunningConfig, _compact_messages
from dayu.engine.async_responses_runner import AsyncResponsesRunner
from dayu.engine import async_responses_runner as runner_module
from dayu.engine.events import EventType
from dayu.engine.responses_codec import decode_output, encode_messages, encode_tools
from dayu.auth import chatgpt
from dayu.auth.chatgpt import ChatGPTCredentialProvider, PLAN_SCOPE
from dayu.auth.store import ChatGPTRegistration, CredentialStore


class Credentials:
    """无真实秘密的认证替身。"""

    def __init__(self) -> None:
        self.refreshes: list[bool] = []
        self.fail = False

    async def acquire(self, *, refresh: bool = False) -> ModelCredential:
        self.refreshes.append(refresh)
        if self.fail:
            raise ModelAuthenticationError("需要重新登录")
        return ModelCredential("fixture-token", "fixture-account")


class Tools:
    """记录工具执行，并复用生产 ToolRegistry 的结果行为。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, name: str, arguments: dict[str, JsonValue], context: None = None) -> dict[str, JsonValue]:
        self.calls.append(name)
        return {"ok": True, "value": {"number": arguments.get("number", 0)}}

    def get_schemas(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "parameters": {"type": "object", "properties": {"number": {"type": "integer"}}},
                },
            }
        ]

    def get_tool_display_info(self, name: str) -> tuple[str, list[str] | None]:
        return name, None

    def get_execution_context_param_name(self, name: str) -> None:
        return None

    def get_dup_call_spec(self, name: str) -> None:
        return None

    def clear_cursors(self) -> None:
        pass


def complete(output: list[JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        },
    }


def text_item(text: str = "答案") -> dict[str, JsonValue]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_item(call_id: str = "c1", number: int = 1) -> dict[str, JsonValue]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "echo",
        "namespace": "dayu",
        "arguments": json.dumps({"number": number}),
    }


@asynccontextmanager
async def server(
    events: list[list[dict[str, JsonValue]]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    delay: float = 0,
    status_codes: list[int] | None = None,
) -> AsyncIterator[list[dict[str, JsonValue]]]:
    requests: list[dict[str, JsonValue]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        requests.append(await request.json())
        selected = events[min(len(requests) - 1, len(events) - 1)]
        response_status = status_codes[min(len(requests) - 1, len(status_codes) - 1)] if status_codes else status
        if response_status != 200:
            return web.json_response(selected[0], status=response_status)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if delay:
            await asyncio.sleep(delay)
        try:
            for event in selected:
                raw = ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()
                # 分块覆盖多字节字符跨边界，不能依赖 TCP chunk == SSE event。
                for chunk in (raw[:17], raw[17:]):
                    await response.write(chunk)
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    app = web.Application()
    app.router.add_post("/responses", handler)
    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, "127.0.0.1", 0)
    await site.start()
    port = app_runner.addresses[0][1]
    monkeypatch.setattr(runner_module, "RESPONSES_ENDPOINT", f"http://127.0.0.1:{port}/responses")
    try:
        yield requests
    finally:
        await app_runner.cleanup()


@pytest.mark.asyncio
async def test_agent_loop_preserves_reasoning_and_executes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reasoning: JsonValue = {"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "opaque-state"}
    tools = Tools()
    async with server(
        [[complete([reasoning, function_item()])], [complete([function_item("c2", 2)])], [complete([text_item()])]],
        monkeypatch,
    ) as requests:
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials())
        agent = AsyncAgent(
            runner=runner, tool_executor=cast(ToolExecutor, tools), running_config=AgentRunningConfig(max_iterations=5)
        )
        result = [event async for event in agent.run("问题", run_id="test-run")]
    assert tools.calls == ["echo", "echo"]
    assert any(event.type == EventType.FINAL_ANSWER and event.data["content"] == "答案" for event in result)
    assert len(requests) == 3
    encoded = json.dumps(requests[2], ensure_ascii=False)
    assert encoded.count("opaque-state") == 1
    assert encoded.count('"call_id": "c1"') == 2
    assert '"function_call_output"' in encoded
    assert all(request["store"] is False and request["stream"] is True for request in requests)
    assert all("temperature" not in request and "previous_response_id" not in request for request in requests)
    assert not any("opaque-state" in json.dumps(event.to_dict(), ensure_ascii=False) for event in result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}],
        [complete([{"type": "function_call", "call_id": "bad", "name": "echo", "arguments": "{"}])],
    ],
)
async def test_unfinished_or_invalid_output_never_executes_tools(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, JsonValue]]
) -> None:
    tools = Tools()
    async with server([events], monkeypatch):
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials(), max_retries=0)
        runner.set_tools(cast(ToolExecutor, tools))
        try:
            result = [event async for event in runner.call([])]
        finally:
            await runner.close()
    assert tools.calls == []
    assert any(event.type == EventType.ERROR and event.data["recoverable"] is False for event in result)
    assert not any(event.type == EventType.DONE for event in result)


@pytest.mark.asyncio
async def test_auth_failure_does_not_force_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = Credentials()
    credentials.fail = True
    runner = AsyncResponsesRunner(model="fixture", credentials=credentials)
    agent = AsyncAgent(runner=runner, running_config=AgentRunningConfig(max_iterations=3))
    result = [event async for event in agent.run("问题", run_id="auth-test")]
    assert any(event.type == EventType.ERROR for event in result)
    assert not any(event.type == EventType.FINAL_ANSWER for event in result)


@pytest.mark.asyncio
async def test_force_answer_hides_internal_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    """达到工具预算后的降级分支也不能向外泄露模型续接状态。"""
    tools = Tools()
    batches = [[complete([function_item(f"c{number}", number)])] for number in range(3)]
    batches.append([complete([{"type": "reasoning", "encrypted_content": "fallback-opaque"}, text_item()])])
    async with server(batches, monkeypatch):
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials())
        agent = AsyncAgent(
            runner=runner,
            tool_executor=cast(ToolExecutor, tools),
            running_config=AgentRunningConfig(max_iterations=3, max_compactions=0),
        )
        result = [event async for event in agent.run("问题", run_id="fallback-test")]
    assert any(event.type == EventType.FINAL_ANSWER and event.data["degraded"] for event in result)
    assert not any("fallback-opaque" in json.dumps(event.to_dict(), ensure_ascii=False) for event in result)


@pytest.mark.asyncio
async def test_waiting_stream_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    token = CancellationToken()
    async with server([[complete([text_item()])]], monkeypatch, delay=0.3):
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials(), cancellation_token=token)

        async def consume() -> None:
            async for _ in runner.call([]):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        token.cancel()
        try:
            with pytest.raises(CancelledError):
                await asyncio.wait_for(task, 0.15)
        finally:
            await runner.close()


@pytest.mark.asyncio
async def test_only_expired_token_gets_single_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = Credentials()
    async with server([[{"error": {"code": "token_expired"}}]], monkeypatch, status=401) as requests:
        runner = AsyncResponsesRunner(model="fixture", credentials=credentials, max_retries=0)
        try:
            result = [event async for event in runner.call([])]
        finally:
            await runner.close()
    assert credentials.refreshes == [False, True]
    assert len(requests) == 2
    assert result[-1].type == EventType.ERROR


def test_codec_and_compaction_preserve_atomic_groups() -> None:
    with pytest.raises(ValueError):
        decode_output([function_item(), function_item()])
    with pytest.raises(ValueError):
        encode_tools([{"type": "web_search"}])
    assistant: AgentMessage = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
        "model_continuation": {
            "identity": "a",
            "model": "m",
            "output": [{"type": "reasoning", "encrypted_content": "opaque"}, function_item()],
        },
    }
    messages: list[AgentMessage] = [
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "旧历史"},
        assistant,
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    compacted, _ = _compact_messages(messages, recent_keep=1)
    assert assistant in compacted
    with pytest.raises(ValueError):
        encode_messages(compacted, identity="other", model="m")


@pytest.mark.asyncio
async def test_context_overflow_enters_agent_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 HTTP 超限码必须驱动 Agent 压缩，再用缩短的历史重试。"""
    batches = [[{"error": {"code": "context_length_exceeded", "param": "input"}}], [complete([text_item()])]]
    async with server(batches, monkeypatch, status_codes=[400, 200]) as requests:
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials(), max_retries=0)
        agent = AsyncAgent(runner=runner, running_config=AgentRunningConfig(max_iterations=4, max_compactions=2))
        messages: list[AgentMessage] = [{"role": "user", "content": "任务"}]
        for _ in range(8):
            messages.extend([{"role": "assistant", "content": "历史"}, {"role": "user", "content": "补充"}])
        events = [event async for event in agent.run_messages(messages, run_id="overflow-fix")]
    assert len(requests) == 2
    first_input, second_input = requests[0]["input"], requests[1]["input"]
    assert isinstance(first_input, list) and isinstance(second_input, list)
    assert len(second_input) < len(first_input)
    assert any(event.type == EventType.WARNING and "压缩" in str(event.data) for event in events)
    assert any(event.type == EventType.FINAL_ANSWER for event in events)


def incomplete(output: list[JsonValue], reason: str) -> dict[str, JsonValue]:
    """构造有明确原因的终态，不与断流混淆。"""
    return {
        "type": "response.incomplete",
        "response": {"status": "incomplete", "incomplete_details": {"reason": reason}, "output": output},
    }


@pytest.mark.asyncio
async def test_truncation_continues_and_keeps_reasoning_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯文本截断经 Agent 续写，前半段状态原样回传且不泄露。"""
    reasoning: JsonValue = {"type": "reasoning", "encrypted_content": "truncated-opaque", "summary": []}
    async with server(
        [[incomplete([reasoning, text_item("上半")], "max_output_tokens")], [complete([text_item("下半")])]],
        monkeypatch,
    ) as requests:
        agent = AsyncAgent(
            runner=AsyncResponsesRunner(model="fixture", credentials=Credentials()),
            running_config=AgentRunningConfig(max_iterations=3, max_compactions=0),
        )
        events = [event async for event in agent.run("问题", run_id="length-fix")]
    assert len(requests) == 2
    assert "truncated-opaque" in json.dumps(requests[1])
    assert any(event.type == EventType.FINAL_ANSWER and event.data["content"] == "上半下半" for event in events)
    assert not any("truncated-opaque" in json.dumps(event.to_dict()) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["部分正文", ""])
async def test_filtered_incomplete_returns_filtered_answer(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """过滤信号保留正文和终态标记，不发起续写。"""
    async with server([[incomplete([text_item(text)], "content_filter")]], monkeypatch) as requests:
        agent = AsyncAgent(runner=AsyncResponsesRunner(model="fixture", credentials=Credentials()))
        events = [event async for event in agent.run("问题", run_id="filter-fix")]
    assert len(requests) == 1
    final = next(event for event in events if event.type == EventType.FINAL_ANSWER)
    assert final.data["filtered"] is True
    assert final.data["degraded"] is True
    assert final.data["content"] == text


@pytest.mark.asyncio
async def test_incomplete_never_executes_even_complete_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整的参数也不能绕过响应终态未完成的执行闸门。"""
    tools = Tools()
    async with server([[incomplete([function_item()], "max_output_tokens")]], monkeypatch):
        runner = AsyncResponsesRunner(model="fixture", credentials=Credentials())
        runner.set_tools(cast(ToolExecutor, tools))
        try:
            events = [event async for event in runner.call([])]
        finally:
            await runner.close()
    assert tools.calls == []
    assert any(event.type == EventType.ERROR for event in events)
    assert not any(event.type == EventType.DONE for event in events)


@pytest.mark.asyncio
async def test_html_refresh_503_is_retried_before_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刷新返回 HTML 503 后有限重试，成功时只请求一次模型。"""
    store = CredentialStore(root=tmp_path)
    with store.lock():
        store.save(
            ChatGPTRegistration(
                "client", "host", "subject", "fixture", "access", "refresh", "id", 0, (PLAN_SCOPE,), "g1"
            )
        )
    calls: list[httpx.Request] = []

    def token(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, text="<html>temporarily unavailable</html>")
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        )

    monkeypatch.setattr(chatgpt.httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(token)))
    async with server([[complete([text_item()])]], monkeypatch) as requests:
        runner = AsyncResponsesRunner(model="fixture", credentials=ChatGPTCredentialProvider(store), max_retries=1)
        try:
            events = [event async for event in runner.call([])]
        finally:
            await runner.close()
    assert len(calls) == 2 and len(requests) == 1
    assert any(event.type == EventType.DONE for event in events)
    saved = store.load()
    assert saved is not None and saved.refresh_token == "new-refresh"
