"""真实 init 配置穿过 Service/Host 边界的 Responses 集成回归。"""

from __future__ import annotations

import json
import time
from argparse import Namespace
from functools import partial
from pathlib import Path

import pytest
from aiohttp import web

from dayu.auth.chatgpt import PLAN_SCOPE
from dayu.auth.oauth import PendingAuthorization
from dayu.auth.store import ChatGPTRegistration, CredentialStore
from dayu.cli import chatgpt_setup
from dayu.cli.commands import init
from dayu.contracts.agent_types import JsonValue
from dayu.contracts.cancellation import CancellationToken
from dayu.contracts.model_credentials import ModelCredentialProvider
from dayu.engine import async_responses_runner as runner_module
from dayu.engine.events import EventType
from dayu.engine.tool_registry import ToolRegistry
from dayu.execution.options import ExecutionOptions, build_base_execution_options
from dayu.host.agent_builder import build_async_runner
from dayu.host.host_execution import HostedRunContext
from dayu.host.scene_preparer import DefaultScenePreparer
from dayu.services.contract_preparation import prepare_execution_contract
from dayu.services.internal.write_pipeline.models import serialize_scene_models
from dayu.services.conversation_policy_reader import ConversationPolicyReader
from dayu.services.scene_definition_reader import SceneDefinitionReader
from dayu.services.scene_execution_acceptance import SceneExecutionAcceptancePreparer
from dayu.startup.config_file_resolver import ConfigFileResolver
from dayu.startup.config_loader import ConfigLoader
from dayu.startup.model_catalog import ConfigLoaderModelCatalog
from dayu.startup.prompt_assets import FilePromptAssetStore
from dayu.startup.workspace import WorkspaceResources


def _authorize(store: CredentialStore, *, replace_account: bool = False) -> PendingAuthorization:
    """在授权边界提供已验证候选，不替换配置提交逻辑。

    Args:
        store: 隔离凭据仓储。
        replace_account: 本用例首次初始化，不需要换账号。
    Returns:
        无真实秘密的候选授权。
    Raises:
        无。
    """
    return PendingAuthorization(
        ChatGPTRegistration(
            "fixture-client",
            "fixture-host",
            "fixture-user",
            "fixture@example.test",
            "fixture-access",
            "fixture-refresh",
            "fixture-id",
            time.time() + 3600,
            (PLAN_SCOPE,),
            "fixture-generation",
        ),
        expected_generation=None,
    )


async def _models(provider: ModelCredentialProvider) -> list[dict[str, JsonValue]]:
    """模拟模型目录，保留候选凭据的真实获取边界。

    Args:
        provider: 初始化准备的凭据提供器。
    Returns:
        只有模型和窗口、没有温度档位的目录。
    Raises:
        ModelAuthenticationError: 候选授权不可用。
    """
    await provider.acquire()
    return [
        {"slug": "other-model", "display_name": "Other", "context_window": 2048},
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "context_window": 8192},
    ]


@pytest.fixture
def initialized_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspaceResources:
    """执行完整 init，网络授权与用户交互之外均使用生产实现。

    Args:
        tmp_path: 独立工作目录。
        monkeypatch: 隔离网络和用户配置写入。
    Returns:
        从 init 产物重新加载的工作区。
    Raises:
        AssertionError: 初始化或配置不符合契约。
    """
    store_factory = partial(CredentialStore, root=tmp_path / "auth")
    monkeypatch.setattr(chatgpt_setup, "CredentialStore", store_factory)
    monkeypatch.setattr("dayu.auth.credentials.CredentialStore", store_factory)
    monkeypatch.setattr(chatgpt_setup, "authorize", _authorize)
    monkeypatch.setattr(chatgpt_setup, "list_models", _models)
    # init 会更新当前进程环境，先登记恢复，避免影响后续测试的 provider 默认值。
    monkeypatch.setenv(init._INIT_PROVIDER_OPTION_ENV, "")
    for key, value in init._THIRD_PARTY_OUTPUT_QUIET_ENV:
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(init, "_detect_shell_profile", lambda: (tmp_path / "shell-profile", True))
    monkeypatch.setattr(init, "_prompt_provider_selection", lambda: "chatgpt")
    monkeypatch.setattr(init, "_prompt_optional_search_keys", lambda: [])
    monkeypatch.setattr(init, "_prompt_huggingface_config", lambda: [])
    monkeypatch.setattr(init, "_prompt_sec_user_agent", lambda: None)
    answers = iter(["", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    base = tmp_path / "workspace"
    assert init.run_init_command(Namespace(base=str(base), provider=None, reset=False, overwrite=False)) == 0
    resolver = ConfigFileResolver(base / "config")
    loader = ConfigLoader(resolver)
    assert "runtime_hints" not in loader.load_llm_model("chatgpt-thinking")
    return WorkspaceResources(
        workspace_dir=base,
        config_root=base / "config",
        output_dir=base / "output",
        config_loader=loader,
        prompt_asset_store=FilePromptAssetStore(resolver),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [None, 0.2])
@pytest.mark.parametrize(
    "scene_name,model_name,effort,override_name",
    [
        ("prompt_mt", "chatgpt-thinking", "high", None),
        ("overview", "chatgpt", "low", None),
        *[
            (scene, name, effort, name)
            for scene in ("prompt", "prompt_mt", "interactive")
            for name, effort in (("chatgpt", "low"), ("chatgpt-thinking", "high"))
        ],
    ],
)
async def test_init_to_scene_host_and_http_omits_unsupported_temperature(
    initialized_workspace: WorkspaceResources,
    monkeypatch: pytest.MonkeyPatch,
    temperature: float | None,
    scene_name: str,
    model_name: str,
    effort: str,
    override_name: str | None,
) -> None:
    """初始化产物无需温度 profile，None 贯穿 Service、Host 与 HTTP 请求。

    Args:
        initialized_workspace: 完整 init 产生的隔离工作区。
        monkeypatch: 仅重定向模型 endpoint 到本地 HTTP 服务。
        temperature: 无覆盖和显式覆盖均不适用于 Responses。
        scene_name: 覆盖初始化的深度与快速场景。
        model_name: 场景对应的默认模型配置名。
        effort: 来自包内模板的推理档位。
        override_name: 显式 CLI 模型选择，为空时使用角色默认项。
    Returns:
        无。
    Raises:
        AssertionError: 契约或 HTTP payload 违反温度适用性。
    """
    workspace = initialized_workspace
    catalog = ConfigLoaderModelCatalog(workspace.config_loader)
    base = build_base_execution_options(
        workspace_dir=workspace.workspace_dir,
        run_config=workspace.config_loader.load_run_config(),
    )
    service = SceneExecutionAcceptancePreparer(
        workspace_dir=workspace.workspace_dir,
        base_execution_options=base,
        model_catalog=catalog,
        scene_definition_reader=SceneDefinitionReader(workspace.prompt_asset_store),
        conversation_policy_reader=ConversationPolicyReader(),
    )
    accepted = service.prepare(scene_name, ExecutionOptions(temperature=temperature, model_name=override_name))
    assert accepted.accepted_execution_spec.model.model_name == model_name
    assert accepted.resolved_temperature is None
    assert accepted.scene_model.temperature is None
    assert accepted.accepted_execution_spec.model.temperature is None
    assert serialize_scene_models({scene_name: accepted.scene_model}) == {
        scene_name: {"name": model_name, "temperature": None}
    }
    contract = prepare_execution_contract(
        service_name="chat_turn",
        scene_name=scene_name,
        accepted_execution_spec=accepted.accepted_execution_spec,
        prompt_contributions={},
        user_message="只回答 OK",
        session_key=None,
        business_concurrency_lane=None,
        selected_toolsets=("web",),
    )
    host = DefaultScenePreparer(
        workspace=workspace,
        model_catalog=catalog,
        default_execution_options=base,
        tool_registry_factory=ToolRegistry,
    )
    cancellation = CancellationToken()
    prepared = await host.prepare(contract, HostedRunContext(run_id="fixture-run", cancellation_token=cancellation))
    args = prepared.agent_input.agent_create_args
    assert args.temperature is None
    assert args.runner_type == "openai_responses"
    assert "temperature" not in args.runner_params
    assert prepared.agent_input.tools is not None
    if scene_name == "prompt_mt":
        assert prepared.agent_input.tools.get_schemas()
    requests: list[dict[str, JsonValue]] = []

    async def handle(request: web.Request) -> web.Response:
        """接收真实 Runner HTTP 请求并返回完整终态。

        Args:
            request: 本地模型请求。
        Returns:
            有效 Responses SSE 完成事件。
        Raises:
            ValueError: 请求 JSON 无效。
        """
        requests.append(await request.json())
        event: dict[str, JsonValue] = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]},
                ],
            },
        }
        return web.Response(text="data: " + json.dumps(event) + "\n\n", content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/responses", handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(runner_module, "RESPONSES_ENDPOINT", f"http://127.0.0.1:{server.addresses[0][1]}/responses")
    runner = build_async_runner(args, cancellation_token=cancellation)
    try:
        events = [event async for event in runner.call(prepared.agent_input.messages)]
        assert any(event.type == EventType.DONE for event in events)
        assert not any(event.type == EventType.ERROR for event in events)
        assert len(requests) == 1
        assert "temperature" not in requests[0]
        assert requests[0]["model"] == "gpt-6.1-sol"
        assert requests[0]["reasoning"] == {"effort": effort}
    finally:
        await runner.close()
        await server.cleanup()
    # Host 自有的压缩场景也必须使用同一温度规则，而非只修复 Service 入口。
    compaction = host._prepare_host_owned_scene_state(scene_name="conversation_compaction", execution_options=None)
    assert compaction.agent_create_args.temperature is None


def test_init_only_allows_two_chatgpt_roles_in_cli_chat_scenes(
    initialized_workspace: WorkspaceResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """初始化只放宽三个 CLI 对话场景，不扩展内部场景权限或移除既有候选。

    Args:
        initialized_workspace: 从包内默认配置完整初始化的隔离工作区。
        monkeypatch: 为第二次定向初始化提供默认回答。
    Returns:
        无。
    Raises:
        AssertionError: 其他场景被放宽、原候选丢失或初始化不幂等。
    """
    directory = initialized_workspace.config_root / "prompts" / "manifests"
    before = {path: path.read_bytes() for path in directory.glob("*.json")}
    answers = iter(["", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert init.run_init_command(
        Namespace(base=str(initialized_workspace.workspace_dir), provider="chatgpt", reset=False, overwrite=False)
    ) == 0
    assert all(path.read_bytes() == content for path, content in before.items())
    aliases = {"chatgpt", "chatgpt-thinking"}
    for path in directory.glob("*.json"):
        data = json.loads(path.read_text())
        model = data.get("model")
        if not isinstance(model, dict):
            continue
        allowed = model.get("allowed_names", [])
        expected = (
            aliases if path.stem in {"prompt", "prompt_mt", "interactive"}
            else aliases & {model["default_name"]}
        )
        assert aliases & set(allowed) == expected, path.name
        assert len(allowed) == len(set(allowed)), path.name
        original = json.loads(ConfigFileResolver().read_text(f"prompts/manifests/{path.name}"))["model"].get(
            "allowed_names", []
        )
        assert set(original) <= set(allowed), path.name
