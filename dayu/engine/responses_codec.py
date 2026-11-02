"""Responses 消息及工具映射，供应商状态仅在协议边界解释。"""

from __future__ import annotations

import json

from dayu.contracts.agent_types import AgentMessage, JsonValue, ModelContinuation
from dayu.engine.tool_batch_executor import ToolCall

TOOL_NAMESPACE = "dayu"
SUPPORTED_OUTPUT_TYPES = frozenset({"message", "function_call", "reasoning"})


def encode_messages(messages: list[AgentMessage], *, identity: str, model: str) -> list[JsonValue]:
    """编码完整客户端历史，原始输出优先，避免重复注入。

    Args:
        messages: Agent 维护的完整历史。
        identity: 当前账号或 API 凭据身份。
        model: 当前模型。
    Returns:
        有序 Responses input items。
    Raises:
        ValueError: continuation 属于其他账号或模型。
    """
    items: list[JsonValue] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            continuation = message.get("model_continuation")
            if continuation:
                if continuation["identity"] != identity or continuation["model"] != model:
                    raise ValueError("模型续接状态不属于当前账号或模型")
                items.extend(continuation["output"])
                continue
            if message.get("content"):
                items.append({"role": "assistant", "content": str(message.get("content"))})
            for call in message.get("tool_calls", []):
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "namespace": TOOL_NAMESPACE,
                        "arguments": call["function"]["arguments"],
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content", ""),
                }
            )
        else:
            items.append({"role": "developer" if role == "system" else "user", "content": message.get("content", "")})
    return items


def encode_tools(schemas: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """将现有函数 schema 放入 Dayu namespace，不改变工具签名。

    Args:
        schemas: ToolRegistry 的 Chat Completions 工具定义。
    Returns:
        namespace 工具数组。
    Raises:
        ValueError: schema 不是函数工具。
    """
    tools: list[JsonValue] = []
    for schema in schemas:
        function = schema.get("function")
        if schema.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("Responses Runner 只接受 Dayu 函数工具")
        # 非 strict 模式保留现有可选参数含义，不能将所有字段强制 required。
        tools.append({**function, "type": "function", "strict": False})
    return (
        [{"type": "namespace", "name": TOOL_NAMESPACE, "description": "Dayu 本地研究工具", "tools": tools}]
        if tools
        else []
    )


def decode_output(output: JsonValue) -> tuple[str, list[ToolCall], list[JsonValue]]:
    """校验完成响应并提取文本与本地工具请求。

    Args:
        output: response.completed 中的 output。
    Returns:
        正文、工具批次、可回传的完整原始 items。
    Raises:
        ValueError: 调用不完整、命名空间异常或输出不受支持。
    """
    if not isinstance(output, list):
        raise ValueError("Responses 响应缺少 output")
    text: list[str] = []
    calls: list[ToolCall] = []
    ids: set[str] = set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") not in SUPPORTED_OUTPUT_TYPES:
            raise ValueError("Responses 返回了不支持的输出类型")
        if item.get("type") == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                raise ValueError("Responses 正文格式无效")
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}:
                    value = part.get("text") or part.get("refusal")
                    if isinstance(value, str):
                        text.append(value)
        elif item.get("type") == "function_call":
            if item.get("status") not in {None, "completed"}:
                raise ValueError("Responses 工具请求尚未完成")
            call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in ids
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, str)
            ):
                raise ValueError("Responses 工具请求身份或参数不完整")
            if item.get("namespace") not in (None, TOOL_NAMESPACE):
                raise ValueError("Responses 工具命名空间不受信任")
            parsed: JsonValue = json.loads(arguments)
            if not isinstance(parsed, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            ids.add(call_id)
            calls.append(ToolCall(id=call_id, name=name, arguments=parsed, index_in_iteration=len(calls)))
    return "".join(text), calls, output


def build_continuation(output: list[JsonValue], *, identity: str, model: str) -> ModelContinuation:
    """构造与 assistant 步骤同生命周期的续接状态。

    Args:
        output: 完整模型输出。
        identity: 当前注册身份。
        model: 当前模型。
    Returns:
        强类型续接状态。
    Raises:
        无。
    """
    return {"identity": identity, "model": model, "output": output}
