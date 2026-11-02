"""登录型 provider 走既有 init 与配置目录，不启动无关交互。"""

import json
import time
from contextlib import contextmanager
from typing import Iterator
from dataclasses import replace
from argparse import Namespace
from pathlib import Path

import pytest

from dayu.cli.commands import init
from dayu.cli import chatgpt_setup
from dayu.auth.store import CredentialStore, ChatGPTRegistration
from dayu.auth.oauth import PendingAuthorization
from dayu.auth.chatgpt import PLAN_SCOPE
from dayu.state_dir_lock import StateDirSingleInstanceLock
from dayu.auth.chatgpt import ChatGPTCredentialProvider
from dayu.contracts.agent_types import JsonValue
from dayu.contracts.model_credentials import ModelAuthenticationError, ModelCredentialProvider
from dayu.startup.config_file_resolver import ConfigFileResolver
from dayu.startup.config_loader import ConfigLoader


def prepare(base: Path) -> Path:
    """建立已有用户配置，避免真实工作区写入。"""
    config = base / "config"
    manifests = config / "prompts" / "manifests"
    manifests.mkdir(parents=True)
    (config / "llm_models.json").write_text(json.dumps({"existing": {"model": "old"}}))
    (manifests / "prompt.json").write_text(
        json.dumps(
            {"model": {"default_name": "gpt-5.4", "allowed_names": ["gpt-5.4"], "_init_model_role": "non_thinking"}}
        )
    )
    return config


def test_provider_only_skips_unrelated_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """定向配置不复制或迁移工作区，不询问第三方 Key。"""
    config = prepare(tmp_path)
    monkeypatch.setattr(init, "_persist_env_var", lambda *args: (tmp_path, True))
    monkeypatch.setattr(chatgpt_setup, "configure_chatgpt", _prepared_setup)

    def unexpected(*args: str) -> None:
        pytest.fail("定向配置不应执行公共初始化步骤")

    monkeypatch.setattr(init, "_copy_config", unexpected)
    monkeypatch.setattr(init, "_prompt_optional_search_keys", unexpected)
    monkeypatch.setattr(init, "apply_all_workspace_migrations", unexpected)
    assert init.run_init_command(Namespace(base=str(tmp_path), provider="chatgpt", reset=False, overwrite=False)) == 0
    manifest = json.loads((config / "prompts/manifests/prompt.json").read_text())
    assert manifest["model"]["default_name"] == "chatgpt"
    assert "existing" in json.loads((config / "llm_models.json").read_text())


def test_login_failure_preserves_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """认证失败不触碰原默认项。"""
    config = prepare(tmp_path)
    manifest = config / "prompts/manifests/prompt.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(init, "_persist_env_var", lambda *args: (tmp_path, True))

    def fail(path: Path, *, workspace_lock: StateDirSingleInstanceLock) -> Iterator[tuple[str, str]]:
        raise ModelAuthenticationError("登录失效")

    monkeypatch.setattr(chatgpt_setup, "configure_chatgpt", fail)
    assert init.run_init_command(Namespace(base=str(tmp_path), provider="chatgpt", reset=False, overwrite=False)) == 1
    assert manifest.read_bytes() == before


def test_login_mode_is_extensible_and_no_key_required() -> None:
    """新登录 provider 无需伪造 API key 字段。"""
    provider = init._ProviderOption("future", "未来登录服务", "", "plain", "thinking", init.ProviderAuthMode.LOGIN)
    assert provider.auth_mode == init.ProviderAuthMode.LOGIN
    with pytest.raises(ValueError):
        init._ProviderOption("invalid", "缺 Key", "", "plain", "thinking")


@pytest.mark.parametrize(
    "answers,valid",
    [
        (["", "2048", ""], True),
        (["0"], False),
        (["wrong"], False),
        (["", "0"], False),
        (["", "2048", "invalid"], False),
    ],
)
def test_chatgpt_model_selection_writes_only_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: list[str], valid: bool
) -> None:
    """账号目录模型选择及错误输入，配置中不得写入凭据。"""
    config = prepare(tmp_path)
    store = CredentialStore(root=tmp_path / "auth")
    monkeypatch.setattr(chatgpt_setup, "CredentialStore", lambda: store)
    monkeypatch.setattr(chatgpt_setup, "authorize", lambda selected: _pending())

    async def models(provider: ModelCredentialProvider) -> list[dict[str, JsonValue]]:
        return [{"slug": "fixture-model", "display_name": "测试模型"}]

    monkeypatch.setattr(chatgpt_setup, "list_models", models)
    sequence = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(sequence))
    before = (config / "llm_models.json").read_bytes()
    if not valid:
        with pytest.raises(ValueError):
            with chatgpt_setup.configure_chatgpt(config, workspace_lock=_workspace_lock(config)):
                pass
        assert (config / "llm_models.json").read_bytes() == before
        return
    with chatgpt_setup.configure_chatgpt(config, workspace_lock=_workspace_lock(config)) as names:
        assert names == ("chatgpt", "chatgpt-thinking")
    data = json.loads((config / "llm_models.json").read_text())
    assert data["existing"]["model"] == "old"
    assert data["chatgpt"]["runner_type"] == "openai_responses"
    assert data["chatgpt"]["auth"] == {"type": "chatgpt", "account_ref": "chatgpt"}
    assert "token" not in (config / "llm_models.json").read_text().replace("max_context_tokens", "")


def test_provider_only_rejects_reset_and_uninitialized_workspace(tmp_path: Path) -> None:
    """定向配置不能隐式覆盖或创建完整工作区。"""
    assert init.run_init_command(Namespace(base=str(tmp_path), provider="chatgpt", reset=False, overwrite=False)) == 1
    prepare(tmp_path)
    assert init.run_init_command(Namespace(base=str(tmp_path), provider="chatgpt", reset=False, overwrite=True)) == 1


def test_package_chatgpt_templates_define_two_effort_roles() -> None:
    """包内配置是两个订阅预设的真源，且只携带账号引用。"""
    models = ConfigLoader(ConfigFileResolver()).load_llm_models()
    for name, effort in (("chatgpt", "low"), ("chatgpt-thinking", "high")):
        assert {
            key: models[name].get(key)
            for key in ("runner_type", "name", "model", "auth", "reasoning_effort", "max_context_tokens")
        } == {
            "runner_type": "openai_responses",
            "name": name,
            "model": "gpt-6.1-sol",
            "auth": {"type": "chatgpt", "account_ref": "chatgpt"},
            "reasoning_effort": effort,
            "max_context_tokens": 500000,
        }


@pytest.mark.parametrize(
    "slugs,choice,selected",
    [
        (["other-model", "preferred-model"], "", "preferred-model"),
        (["preferred-model", "other-model"], "2", "other-model"),
        (["other-model"], "", "other-model"),
    ],
)
def test_provider_clones_templates_and_selects_available_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    slugs: list[str],
    choice: str,
    selected: str,
) -> None:
    """模型偏好与档位取自模板，目录顺序、手动选择和不可见情况均可控。

    Args:
        tmp_path: 独立目录。
        monkeypatch: 隔离授权、目录查询与输入。
        capsys: 检查首选模型不可见时的提示。
        slugs: 账号模型目录顺序。
        choice: 用户选择，空字符串采用模板偏好。
        selected: 最终应生成的模型。
    Returns:
        无。
    Raises:
        AssertionError: 模板字段丢失、默认漂移或配置被意外改动。
    """
    config = prepare(tmp_path)
    package = tmp_path / "package"
    package.mkdir()
    templates = {
        name: {
            "runner_type": "openai_responses",
            "model": "preferred-model",
            "auth": {"type": "chatgpt", "account_ref": "chatgpt"},
            "reasoning_effort": effort,
            "timeout": timeout,
            "stream_idle_timeout": 45.0,
            "max_context_tokens": 9999,
        }
        for name, effort, timeout in (("chatgpt", "low", 812), ("chatgpt-thinking", "medium", 913))
    }
    package_file = package / "llm_models.json"
    package_file.write_text(json.dumps(templates))
    template_bytes = package_file.read_bytes()
    monkeypatch.setattr("dayu.startup.config_file_resolver.resolve_package_config_path", lambda: package)
    store = CredentialStore(root=tmp_path / "auth")
    monkeypatch.setattr(chatgpt_setup, "CredentialStore", lambda: store)
    monkeypatch.setattr(chatgpt_setup, "authorize", lambda selected: _pending())

    async def models(provider: ModelCredentialProvider) -> list[dict[str, JsonValue]]:
        return [{"slug": slug, "context_window": 4096} for slug in slugs]

    monkeypatch.setattr(chatgpt_setup, "list_models", models)
    answers = iter([choice, "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with chatgpt_setup.configure_chatgpt(config, workspace_lock=_workspace_lock(config)):
        pass
    written = json.loads((config / "llm_models.json").read_text())
    assert written["existing"] == {"model": "old"}
    for name, template in templates.items():
        assert written[name] == {**template, "model": selected, "max_context_tokens": 4096}
    assert package_file.read_bytes() == template_bytes
    if "preferred-model" not in slugs:
        assert "默认模板模型" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing", "wrong_runner", "invalid_effort"])
def test_invalid_package_template_stops_before_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """损坏的包内模板必须先报错，不能打开浏览器或部分写入配置。

    Args:
        tmp_path: 隔离目录。
        monkeypatch: 提供损坏的模板和禁止调用的授权边界。
        failure: 缺失、协议不符或非法默认档位。
    Returns:
        无。
    Raises:
        AssertionError: 模板错误未在授权前被拒绝。
    """
    templates = ConfigLoader(ConfigFileResolver()).load_llm_models()
    if failure == "missing":
        del templates["chatgpt"]
    elif failure == "wrong_runner":
        templates["chatgpt"] = {"runner_type": "openai_compatible"}
    else:
        templates["chatgpt-thinking"] = {
            "runner_type": "openai_responses",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "invalid",
        }
    package = tmp_path / "package"
    package.mkdir()
    (package / "llm_models.json").write_text(json.dumps(templates))
    monkeypatch.setattr("dayu.startup.config_file_resolver.resolve_package_config_path", lambda: package)
    monkeypatch.setattr(chatgpt_setup, "authorize", lambda selected: pytest.fail("模板错误时不应发起授权"))
    config = prepare(tmp_path)
    before = (config / "llm_models.json").read_bytes()
    with pytest.raises(ValueError, match="模板"):
        with chatgpt_setup.configure_chatgpt(config, workspace_lock=_workspace_lock(config)):
            pass
    assert (config / "llm_models.json").read_bytes() == before


@contextmanager
def _prepared_setup(path: Path, *, workspace_lock: StateDirSingleInstanceLock) -> Iterator[tuple[str, str]]:
    """模拟已准备的登录型配置，默认项仍由 init 写入。"""
    yield "chatgpt", "chatgpt-thinking"


def _workspace_lock(config: Path) -> StateDirSingleInstanceLock:
    """使用真实初始化锁验证事务期间的工作区排他。"""
    return StateDirSingleInstanceLock(
        state_dir=config.parent,
        lock_file_name=init._INIT_WORKSPACE_LOCK_FILE_NAME,
        lock_name=init._INIT_WORKSPACE_LOCK_NAME,
    )


def _pending(previous: ChatGPTRegistration | None = None) -> PendingAuthorization:
    """构造已校验但尚未激活的替换账号。"""
    value = ChatGPTRegistration(
        "new-client",
        "host",
        "new-subject",
        "fixture",
        "fixture-access",
        "fixture-refresh",
        "fixture-id",
        time.time() + 3600,
        (PLAN_SCOPE,),
        "new-generation",
    )
    return PendingAuthorization(value, previous.generation if previous else None)


@pytest.mark.parametrize(
    "failure", ["directory", "input", "interrupt", "manifest", "credential", "concurrent_refresh", "success"]
)
def test_login_setup_commits_together_or_preserves_existing_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """候选授权成功后，任一步失败均不能切换账号或留下部分配置。"""
    config = prepare(tmp_path)
    store = CredentialStore(root=tmp_path / "auth")
    old = replace(_pending().registration, client_id="old-client", subject="old-account", generation="old-generation")
    with store.lock():
        store.save(old)
    monkeypatch.setattr(chatgpt_setup, "CredentialStore", lambda: store)
    monkeypatch.setattr(chatgpt_setup, "authorize", lambda selected, *, replace_account=False: _pending(old))
    paths = [config / "llm_models.json", config / "prompts/manifests/prompt.json"]
    before = {path: path.read_bytes() for path in paths}
    refreshed = replace(old, generation="background-refresh", access_token="refreshed-access")

    async def models(provider: ModelCredentialProvider) -> list[dict[str, JsonValue]]:
        assert store.load() == old
        if failure == "directory":
            raise ValueError("模型目录失败")
        if failure == "concurrent_refresh":
            with store.lock():
                store.save(refreshed)
        return [{"slug": "fixture-model", "display_name": "测试模型"}]

    monkeypatch.setattr(chatgpt_setup, "list_models", models)
    answers = iter(["change", "1", "2048", "high"])

    def answer(prompt: str) -> str:
        value = next(answers)
        if value == "1" and failure == "input":
            return "invalid"
        if value == "1" and failure == "interrupt":
            raise KeyboardInterrupt()
        return value

    monkeypatch.setattr("builtins.input", answer)
    original_save = store.save

    def save_then_fail(value: ChatGPTRegistration) -> None:
        original_save(value)
        if value.subject == "new-subject":
            raise OSError("模拟凭据提交失败")

    if failure == "credential":
        monkeypatch.setattr(store, "save", save_then_fail)

    def execute() -> None:
        with chatgpt_setup.configure_chatgpt(config, workspace_lock=_workspace_lock(config)) as names:
            assert store.load() == old
            init._update_manifest_default_models(config, *names)
            if failure == "manifest":
                raise OSError("模拟默认项写入后失败")

    if failure == "success":
        execute()
        saved = store.load()
        assert saved is not None and saved.subject == "new-subject"
        assert json.loads(paths[1].read_text())["model"]["default_name"] == "chatgpt"
    else:
        with pytest.raises((ValueError, ModelAuthenticationError, OSError, KeyboardInterrupt)):
            execute()
        assert all(path.read_bytes() == data for path, data in before.items())
        assert store.load() == (refreshed if failure == "concurrent_refresh" else old)
    # 成功与失败后均能重新获取同一工作区锁。
    check_lock = _workspace_lock(config)
    check_lock.acquire()
    check_lock.release()
