from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.doctor import hook_retrieval_status
from agent_core.installer import apply_install, apply_uninstall, build_release_manifest
from agent_core.match import main as match_main
from agent_core.runtime_config import render_fragment, runtime_hook_path
from tests.test_install import ROOT, git, installed_fixture


def shell_path() -> Path:
    direct = shutil.which("sh")
    if direct:
        return Path(direct)
    git_exec = subprocess.run(
        ["git", "--exec-path"], check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    candidate = Path(git_exec).parents[2] / "bin" / "sh.exe"
    if not candidate.is_file():
        pytest.fail("Git shell adapter is unavailable")
    return candidate


def add_retrieval_lesson(state: Path) -> None:
    ledger = state / "experience" / "LESSONS.md"
    text = ledger.read_text(encoding="utf-8")
    entry = (
        "- **L-9 [pending·通用] Prompt retrieval synthetic lesson.** "
        "触发: shared constant prompt. 代价: missed prompt retrieval. "
        "sink → checks/prompt.md. when: {\"text\":[\"shared constant\"]}\n\n"
    )
    ledger.write_text(text.replace("## 归档", entry + "## 归档"), encoding="utf-8")
    git(state, "add", ".")
    git(state, "commit", "-q", "-m", "synthetic retrieval lesson")
    git(state, "push", "-q")


def install_runtime_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, dict]:
    state, config, _manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    add_retrieval_lesson(state)
    manifest = tmp_path / "hook-release-manifest.json"
    manifest.write_text(json.dumps(build_release_manifest(ROOT)), encoding="utf-8")
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    return state, config, install_root, json.loads(config.read_text(encoding="utf-8"))


def run_configured_hook(
    handler: dict,
    runtime: str,
    payload: dict,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["AGENT_CORE_PYTHON"] = os.fspath(Path(os.sys.executable))
    if os.name == "nt" and runtime == "claude-code":
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Restricted",
            "-Command", handler["command"],
        ]
    elif os.name == "nt":
        command = handler["commandWindows"]
    else:
        command = [str(shell_path()), "-c", handler["command"]]
    return subprocess.run(
        command,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=30,
        shell=isinstance(command, str),
    )


def test_runtime_fragments_expose_prompt_pretool_and_completion() -> None:
    expected = {"UserPromptSubmit": "prompt", "PreToolUse": "pretool", "Stop": "completion"}
    for runtime in ("claude-code", "codex"):
        payload = json.loads((ROOT / "runtimes" / runtime / "hook.fragment.json").read_text(encoding="utf-8"))
        assert payload["schema"] == "hook-fragment/1"
        assert set(payload["hooks"]) == set(expected)
        for event, stage in expected.items():
            group = payload["hooks"][event][0]
            assert set(group) == {"hooks"}
            handler = group["hooks"][0]
            assert handler["type"] == "command"
            assert "{{HOOK_TARGET}}" in handler["command"]
            assert stage in handler["command"]


def test_doctor_distinguishes_missing_disconnected_and_connected_unobserved(tmp_path: Path) -> None:
    hook = tmp_path / "hooks" / "user_prompt.sh"
    with pytest.raises(ConfigError, match="FAIL_HOOK_MISSING"):
        hook_retrieval_status(hook)
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nprintf static\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_RETRIEVAL_DISCONNECTED"):
        hook_retrieval_status(hook)
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    assert hook_retrieval_status(hook) == ("WARN", "retrieval_connected_unobserved")


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_installed_prompt_and_pretool_hooks_retrieve_and_prove_mtime_gate(
    runtime: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _config, _install_root, payload = install_runtime_hooks(tmp_path, monkeypatch)
    target = next(item for item in payload["targets"] if item["runtime"] == runtime)
    hook = runtime_hook_path(Path(target["root"]) / target["hook_target"])
    config_name = "settings.json" if runtime == "claude-code" else "hooks.json"
    runtime_config = json.loads((Path(target["root"]) / config_name).read_text(encoding="utf-8"))
    prompt_group = runtime_config["hooks"]["UserPromptSubmit"][-1]
    pretool_group = runtime_config["hooks"]["PreToolUse"][-1]
    completion_group = runtime_config["hooks"]["Stop"][-1]
    if runtime == "claude-code":
        handlers = [group["hooks"][0] for group in (
            prompt_group, pretool_group, completion_group,
        )]
        if os.name == "nt":
            assert all(handler["shell"] == "powershell" for handler in handlers)
            assert [handler["command"] for handler in handlers] == [
                "& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy "
                f"Bypass -File '{hook}' prompt",
                "& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy "
                f"Bypass -File '{hook}' pretool",
                "& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy "
                f"Bypass -File '{hook}' completion",
            ]
        else:
            assert [handler["command"] for handler in handlers] == [
                f'"{hook}" prompt', f'"{hook}" pretool', f'"{hook}" completion',
            ]
    else:
        handlers = [group["hooks"][0] for group in (
            prompt_group, pretool_group, completion_group,
        )]
        assert all(handler["type"] == "command" for handler in handlers)
        field = "commandWindows" if os.name == "nt" else "command"
        assert all(str(hook) in handler[field] for handler in handlers)
    prompt = run_configured_hook(prompt_group["hooks"][0], runtime, {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Please inspect the shared constant before editing.",
        "cwd": str(state),
    })
    assert prompt.returncode == 0
    assert "L-9" in prompt.stdout
    heartbeat_path = hook.parent / ".lessons-hook-heartbeat.json"
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "prompt"
    assert heartbeat["result_nonempty"] is True
    assert heartbeat["validation_ran"] is True
    assert hook_retrieval_status(hook) == ("PASS", "retrieval_nonempty stage=prompt")

    pretool = run_configured_hook(pretool_group["hooks"][0], runtime, {
        "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": "src/constants.py"},
        "cwd": str(state),
    })
    assert pretool.returncode == 0
    assert "L-1" in pretool.stdout
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "pretool"
    assert heartbeat["result_nonempty"] is True
    assert heartbeat["validation_ran"] is False
    assert hook_retrieval_status(hook) == ("PASS", "retrieval_nonempty stage=pretool")


def test_runtime_config_merge_and_uninstall_preserve_unowned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    target = payload["targets"][0]
    runtime_root = Path(target["root"])
    runtime_root.mkdir(parents=True)
    settings = runtime_root / "settings.json"
    before = (
        b'{\r\n  "theme" : {"palette":[1, 2]},\r\n'
        b'  "hooks" : {"Foreign":[{"command":"keep"}]}\r\n}\r\n'
    )
    settings.write_bytes(before)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    installed = settings.read_bytes()
    assert b'"theme" : {"palette":[1, 2]}' in installed
    assert b'"Foreign":[{"command":"keep"}]' in installed
    assert apply_uninstall(config)[-1] == "PASS uninstall"
    assert settings.read_bytes() == before


def test_uninstall_preserves_post_install_unowned_runtime_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    target = payload["targets"][0]
    settings = Path(target["root"]) / "settings.json"
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    current = json.loads(settings.read_text(encoding="utf-8"))
    current["user_after"] = "keep-this-value"
    current["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": "user hook"}]})
    settings.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
    apply_uninstall(config)
    remaining = json.loads(settings.read_text(encoding="utf-8"))
    assert remaining["user_after"] == "keep-this-value"
    assert remaining["hooks"]["PreToolUse"] == [
        {"hooks": [{"type": "command", "command": "user hook"}]},
    ]


def test_uninstall_rejects_modified_owned_runtime_hook_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    target = payload["targets"][0]
    settings = Path(target["root"]) / "settings.json"
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    runtime_config = json.loads(settings.read_text(encoding="utf-8"))
    runtime_config["hooks"]["PreToolUse"][-1]["hooks"][0]["command"] = "user replacement"
    settings.write_text(json.dumps(runtime_config), encoding="utf-8")
    with pytest.raises(ConfigError, match="UNINSTALL_CONFLICT"):
        apply_uninstall(config)
    assert "user replacement" in settings.read_text(encoding="utf-8")


def test_equivalent_unowned_runtime_hooks_require_force_and_survive_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    target = payload["targets"][0]
    runtime_root = Path(target["root"])
    runtime_root.mkdir(parents=True)
    settings = runtime_root / "settings.json"
    desired = render_fragment(
        ROOT / "runtimes" / "claude-code" / "hook.fragment.json",
        runtime_root / target["hook_target"],
    )
    before = json.dumps({"hooks": desired}, separators=(",", ":")).encode("utf-8")
    settings.write_bytes(before)
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    apply_install(ROOT, config, state, ROOT, manifest, force=True)
    apply_uninstall(config)
    assert settings.read_bytes() == before


def test_generated_hook_is_fail_open_when_wrapper_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state, _config, install_root, payload = install_runtime_hooks(tmp_path, monkeypatch)
    target = payload["targets"][0]
    hook = runtime_hook_path(Path(target["root"]) / target["hook_target"])
    environment = os.environ.copy()
    failing = tmp_path / ("failing.cmd" if os.name == "nt" else "failing")
    if os.name == "nt":
        failing.write_text("@exit /b 9\r\n", encoding="utf-8")
    else:
        failing.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        failing.chmod(0o755)
    environment["AGENT_CORE_COMMAND"] = str(failing)
    command = (
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "prompt"]
        if os.name == "nt" else [str(shell_path()), str(hook), "prompt"]
    )
    completed = subprocess.run(
        command,
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "shared constant"}),
        check=False, capture_output=True, text=True, encoding="utf-8", env=environment, timeout=30,
    )
    assert completed.returncode == 0
    assert "WARNING lessons hook command failed: 9" in completed.stderr
    assert "Read matched lessons before acting." in completed.stdout
    assert not (hook.parent / ".lessons-hook-heartbeat.json").exists()


def test_lessons_hook_parse_failure_is_fail_open_and_heartbeat_is_explainable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    hook = tmp_path / "hook.sh"
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    heartbeat = tmp_path / ".lessons-hook-heartbeat.json"
    event = tmp_path / "broken.json"
    event.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_HOOK_SCRIPT", str(hook))
    monkeypatch.setenv("AGENT_CORE_HOOK_HEARTBEAT", str(heartbeat))
    assert match_main([
        "hook", "--runtime", "codex", "--stage", "prompt",
        "--ledger", str(ROOT / "seed" / "LESSONS.md"), "--event-json", str(event),
    ]) == 0
    assert "WARNING lessons hook unavailable" in capsys.readouterr().err
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["status"] == "warning"
    assert payload["retrieval_invoked"] is True
    assert payload["result_nonempty"] is False
    assert hook_retrieval_status(hook) == ("WARN", "retrieval_warning stage=prompt")
