"""Responses SSE framing，只有终态成功才允许提交工具批次。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

from aiohttp import ClientResponse

from dayu.contracts.agent_types import JsonValue

MAX_EVENT_BYTES = 16 * 1024 * 1024


@dataclass
class ResponsesFailure(Exception):
    """保留机器错误码和请求身份，不回显可能含秘密的原始响应。"""

    code: str
    status: int = 0
    request_id: str = ""


async def parse_events(response: ClientResponse) -> AsyncIterator[dict[str, JsonValue]]:
    """读取 SSE data 帧，正确处理分块、CRLF 和多行 data。

    Args:
        response: 已建立的流式 HTTP 响应。
    Returns:
        解码后的事件对象。
    Raises:
        ValueError: JSON 或事件结构无效、事件过大。
    """
    buffer = b""
    data: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_any():
        buffer += chunk
        if len(buffer) + size > MAX_EVENT_BYTES:
            raise ValueError("Responses SSE 事件超出大小上限")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data:
                    raw = b"\n".join(data)
                    data, size = [], 0
                    if raw == b"[DONE]":
                        continue
                    event: JsonValue = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("Responses SSE 事件必须是对象")
                    yield event
            elif line.startswith(b"data:"):
                value = line[5:].lstrip(b" ")
                data.append(value)
                size += len(value)
    if buffer.strip() or data:
        raise ValueError("Responses SSE 在未完成事件中断开")
