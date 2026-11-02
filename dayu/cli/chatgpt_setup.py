"""登录型 provider 初始化，独立于 API key 与公共工作区初始化。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from typing import Iterator
from pathlib import Path

from dayu.auth.chatgpt import ChatGPTCredentialProvider, list_models
from dayu.auth.oauth import authorize, PendingAuthorization
from dayu.auth.store import CredentialStore
from dayu.contracts.agent_types import JsonValue
from dayu.contracts.model_credentials import ModelAuthenticationError
from dayu.state_dir_lock import StateDirSingleInstanceLock
from dayu.cli.config_transaction import configuration_transaction, write_config_bytes

CHATGPT_FAST_MODEL_KEY = "chatgpt"
CHATGPT_THINKING_MODEL_KEY = "chatgpt-thinking"


@contextmanager
def configure_chatgpt(config_dir: Path, *, workspace_lock: StateDirSingleInstanceLock) -> Iterator[tuple[str, str]]:
    """准备登录型配置，在默认项写入成功后一起激活授权。

    Args:
        config_dir: 已有工作区配置目录。
        workspace_lock: 与完整 init/reset 共用的工作区锁。
    Yields:
        普通与推理角色的模型配置名。
    Raises:
        ValueError: 交互或模型目录不完整。
        ModelAuthenticationError: 授权无效。
        OSError: 配置落盘失败。
    """
    store = CredentialStore()
    with store.lock():
        previous = store.load()
    pending: PendingAuthorization | None = None
    if previous and previous.refresh_token:
        print(f"当前 ChatGPT 账号：{previous.email or previous.subject}")
        action = input("回车继续使用，输入 login 重新登录，输入 change 更换账号：").strip()
        if action not in {"", "login", "change"}:
            raise ValueError("无效的账号操作")
        if action:
            pending = authorize(store, replace_account=action == "change")
    else:
        pending = authorize(store)
    provider = pending if pending is not None else ChatGPTCredentialProvider(store)
    models = asyncio.run(list_models(provider))
    if not models:
        raise ValueError("当前账号没有可展示模型")
    for index, model in enumerate(models, 1):
        print(f"  {index}. {model.get('display_name', model.get('slug', ''))}")
    raw = input("选择模型 [1]：").strip() or "1"
    try:
        selected = int(raw) - 1
        if selected < 0:
            raise ValueError("模型编号不能小于 1")
        model = models[selected]
    except (ValueError, IndexError):
        raise ValueError("无效的模型编号") from None
    slug = model.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ValueError("模型目录缺少 slug")
    # 目录不保证提供上下文上限，不按模型名字推测。
    context = model.get("context_window")
    prompt = (
        f"模型最大上下文 tokens（目录值 {context}，回车采用）:"
        if isinstance(context, int) and context > 0
        else "模型最大上下文 tokens（请根据模型文档填写整数）："
    )
    raw_context = input(prompt).strip()
    tokens = int(raw_context) if raw_context else context
    if not isinstance(tokens, int) or tokens <= 0:
        raise ValueError("必须填写经确认的上下文上限")
    effort = input("推理档位 low/medium/high/xhigh [high]：").strip() or "high"
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("无效的推理档位")
    # 浏览器和交互阶段不持锁。提交时按固定顺序取得工作区锁及连接锁。
    workspace_lock.acquire()
    try:
        with store.lock():
            current = store.load()
            if pending is not None:
                if (current.generation if current else None) != pending.expected_generation:
                    raise ModelAuthenticationError("登录期间凭据已更新，请重新配置")
                if pending.registration.expires_at <= time.time():
                    raise ModelAuthenticationError("候选授权已过期，请重新登录")
            elif current is None or previous is None or current.identity != previous.identity:
                raise ModelAuthenticationError("配置期间账号已改变，请重新配置")
            path = config_dir / "llm_models.json"
            data: JsonValue = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("模型配置必须是 JSON 对象")
            for key, reasoning in ((CHATGPT_FAST_MODEL_KEY, "low"), (CHATGPT_THINKING_MODEL_KEY, effort)):
                data[key] = {
                    "runner_type": "openai_responses",
                    "model": slug,
                    "auth": {"type": "chatgpt", "account_ref": "chatgpt"},
                    "reasoning_effort": reasoning,
                    "max_context_tokens": tokens,
                }
            manifests = tuple((config_dir / "prompts" / "manifests").glob("*.json"))
            with store.preserve_registration(), configuration_transaction((path, *manifests)):
                write_config_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
                yield CHATGPT_FAST_MODEL_KEY, CHATGPT_THINKING_MODEL_KEY
                # 调用方已完成默认项更新。保存失败会在持锁期间恢复文件快照。
                if pending is not None:
                    store.save(pending.registration)
    finally:
        workspace_lock.release()
