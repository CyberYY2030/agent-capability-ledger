from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_core.config import ConfigError, posix_user_data_root_shell
from agent_core.doctor import hook_retrieval_status
from agent_core.installer import apply_install, apply_uninstall, build_release_manifest, plan_install
from agent_core.match import _hook_source_signature, main as match_main
from agent_core.runtime_config import render_fragment, runtime_hook_path
from agent_core.sync import _hook_content, _powershell_hook_content
from tests.test_capture import capture_block, completion_payload, config_file, project_repo, state_repo
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
    unused_profile = state / "experience" / "profiles" / "unused" / "LESSONS.md"
    unused_profile.parent.mkdir(parents=True)
    unused_profile.write_text(
        "# LESSONS\n"
        "<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n"
        "<!-- lessons-profile: unused -->\n\n"
        "## 活跃区\n\n"
        "- **[[lesson:UNUSED-1]] [pending·领域] Undeclared profile lesson.** "
        "触发: shared constant. 代价: unrelated injection. sink → checks/unused.md. "
        "when: {\"text\":[\"shared constant\"]}\n\n"
        "## 归档区\n",
        encoding="utf-8",
    )
    git(state, "add", ".")
    git(state, "commit", "-q", "-m", "synthetic retrieval lesson")
    git(state, "push", "-q")


def hook_contract_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "LESSONS.md"
    ledger.write_text(
        "# Lessons\n"
        "<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n"
        "## 活跃\n\n"
        "- **L-1 [pending·通用] 当输入含“引号”时，保留上下文。** "
        "触发: shared constant. 代价: missed context. sink → checks/prompt.md. "
        "when: {\"text\":[\"shared constant\"]}\n"
        "- **L-2 [pending·通用] Pretool “中文” synthetic lesson.** "
        "触发: source path. 代价: missed context. sink → checks/pretool.md. "
        "when: {\"paths\":[\"src/**\"]}\n"
        "- **L-3 [pending·通用] Completion synthetic lesson.** "
        "触发: review complete. 代价: noisy stdout. sink → checks/completion.md. "
        "when: {\"text\":[\"review complete\"]}\n\n"
        "## 归档\n",
        encoding="utf-8",
    )
    return ledger


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
@pytest.mark.parametrize(
    ("stage", "payload", "expected_id"),
    [
        ("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "shared constant"}, "L-1"),
        ("pretool", {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "src/a.py"}}, "L-2"),
        ("completion", {
            "hook_event_name": "Stop", "last_assistant_message": "review complete",
            "stop_hook_active": False,
        }, "L-3"),
    ],
)
def test_hook_stdout_contract_for_each_runtime_and_event(
    runtime: str,
    stage: str,
    payload: dict[str, object],
    expected_id: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = hook_contract_ledger(tmp_path)
    event = tmp_path / f"{runtime}-{stage}.json"
    event.write_text(json.dumps(payload), encoding="utf-8")

    assert match_main([
        "hook", "--runtime", runtime, "--stage", stage,
        "--ledger", str(ledger), "--event-json", str(event),
    ]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if stage == "prompt":
        assert captured.out.startswith(f"LESSON {expected_id}:")
        assert "“引号”" in captured.out
        assert captured.out.endswith("\n")
    elif stage == "pretool":
        assert json.loads(captured.out) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    "LESSON L-2: Pretool “中文” synthetic lesson. sink=checks/pretool.md\n"
                ),
            },
        }
    else:
        assert captured.out == ""


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
@pytest.mark.parametrize("stage", ["pretool", "completion"])
def test_hook_with_no_rendered_lessons_has_empty_stdout(
    runtime: str, stage: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = hook_contract_ledger(tmp_path)
    event = tmp_path / f"{runtime}-empty-{stage}.json"
    payload = (
        {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "docs/guide.md"}}
        if stage == "pretool"
        else {
            "hook_event_name": "Stop", "last_assistant_message": "unrelated",
            "stop_hook_active": False,
        }
    )
    event.write_text(json.dumps(payload), encoding="utf-8")

    assert match_main([
        "hook", "--runtime", runtime, "--stage", stage,
        "--ledger", str(ledger), "--event-json", str(event),
    ]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def heartbeat_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    hook = tmp_path / "hooks" / "user_prompt.sh"
    hook.parent.mkdir()
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        hook.chmod(0o755)
    heartbeat = hook.parent / ".lessons-hook-heartbeat.json"
    payload = {
        "schema": "lessons-hook-heartbeat/1",
        "runtime": "codex",
        "stage": "prompt",
        "status": "warning",
        "retrieval_invoked": True,
        "result_nonempty": False,
        "validation_ran": False,
        "source_mtime_sha256": None,
        "hook_sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
        "observed_utc": "2026-08-27T00:00:00Z",
    }
    return hook, heartbeat, payload


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


def test_runtime_fragments_expose_prompt_pretool_and_completion(tmp_path: Path) -> None:
    expected = {"UserPromptSubmit": "prompt", "PreToolUse": "pretool", "Stop": "completion"}
    hook = tmp_path / "runtime hooks" / "user_prompt.sh"
    for runtime in ("claude-code", "codex"):
        fragment = ROOT / "runtimes" / runtime / "hook.fragment.json"
        payload = json.loads(fragment.read_text(encoding="utf-8"))
        assert payload["schema"] == "hook-fragment/1"
        assert set(payload["hooks"]) == set(expected)
        for event, stage in expected.items():
            group = payload["hooks"][event][0]
            assert set(group) == {"hooks"}
            handler = group["hooks"][0]
            assert handler["type"] == "command"
            assert "{{HOOK_TARGET}}" in handler["command"]
            assert stage in handler["command"]
        rendered = render_fragment(fragment, hook, windows=False)
        assert [rendered[event][0]["hooks"][0]["command"] for event in expected] == [
            f'"{hook}" {stage}' for stage in expected.values()
        ]


def test_posix_hook_command_falls_back_to_the_shared_install_root() -> None:
    rendered = _hook_content([], "codex").decode("utf-8")
    fallback = posix_user_data_root_shell() + "/bin/agent-core"
    assert rendered.index("AGENT_CORE_COMMAND") < rendered.index("command -v agent-core")
    assert rendered.index("command -v agent-core") < rendered.index(fallback)
    assert f'agent_core="{fallback}"' in rendered


def test_doctor_distinguishes_missing_disconnected_and_connected_unobserved(tmp_path: Path) -> None:
    hook = tmp_path / "hooks" / "user_prompt.sh"
    with pytest.raises(ConfigError, match="FAIL_HOOK_MISSING"):
        hook_retrieval_status(hook)
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nprintf static\n", encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
    with pytest.raises(ConfigError, match="FAIL_RETRIEVAL_DISCONNECTED"):
        hook_retrieval_status(hook)
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    assert hook_retrieval_status(hook) == ("WARN", "retrieval_connected_unobserved")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode contract")
def test_doctor_rejects_non_executable_managed_hook(tmp_path: Path) -> None:
    hook = tmp_path / "hooks" / "user_prompt.sh"
    hook.parent.mkdir()
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    hook.chmod(0o644)
    with pytest.raises(ConfigError, match="FAIL_HOOK_NOT_EXECUTABLE"):
        hook_retrieval_status(hook)


def test_doctor_accepts_stored_v1_hook_heartbeat(tmp_path: Path) -> None:
    hook, heartbeat, payload = heartbeat_fixture(tmp_path)
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    assert hook_retrieval_status(hook) == ("WARN", "retrieval_warning stage=prompt")


def test_doctor_rejects_v2_hook_heartbeat_without_session_id(tmp_path: Path) -> None:
    hook, heartbeat, payload = heartbeat_fixture(tmp_path)
    payload["schema"] = "lessons-hook-heartbeat/2"
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="FAIL_HOOK_HEARTBEAT"):
        hook_retrieval_status(hook)


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
    assert "UNUSED-1" not in prompt.stdout
    heartbeat_path = hook.parent / ".lessons-hook-heartbeat.json"
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "prompt"
    assert heartbeat["result_nonempty"] is True
    assert heartbeat["validation_ran"] is True
    assert heartbeat["source_mtime_sha256"] == _hook_source_signature(
        state / "experience" / "LESSONS.md", str(state), False,
    )[0]
    state_signature = heartbeat["source_mtime_sha256"]
    assert hook_retrieval_status(hook) == ("PASS", "retrieval_nonempty stage=prompt")

    workspace_signatures = []
    for name in ("alpha-project", "beta-project"):
        workspace = project_repo(tmp_path / name, project_id=name)
        routed = run_configured_hook(prompt_group["hooks"][0], runtime, {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Please inspect the project incident before editing.",
            "cwd": str(workspace),
        })
        assert routed.returncode == 0
        assert f"{name.split('-', 1)[0].upper()}-1" in routed.stdout
        routed_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        expected_signature = _hook_source_signature(
            state / "experience" / "LESSONS.md", str(workspace), False,
        )[0]
        assert routed_heartbeat["source_mtime_sha256"] == expected_signature
        workspace_signatures.append(expected_signature)
    assert len({state_signature, *workspace_signatures}) == 3

    pretool = run_configured_hook(pretool_group["hooks"][0], runtime, {
        "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": "src/constants.py"},
        "cwd": str(workspace),
    })
    assert pretool.returncode == 0
    pretool_payload = json.loads(pretool.stdout)
    assert pretool_payload == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": pretool_payload["hookSpecificOutput"]["additionalContext"],
        },
    }
    assert "L-1" in pretool_payload["hookSpecificOutput"]["additionalContext"]
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "pretool"
    assert heartbeat["result_nonempty"] is True
    assert heartbeat["validation_ran"] is False
    assert heartbeat["source_mtime_sha256"] == workspace_signatures[-1]
    assert hook_retrieval_status(hook) == ("PASS", "retrieval_nonempty stage=pretool")

    completion = run_configured_hook(completion_group["hooks"][0], runtime, {
        "hook_event_name": "Stop",
        "last_assistant_message": "Review the shared constant prompt.",
        "session_id": f"{runtime}-completion",
        "cwd": str(state),
    })
    assert completion.returncode == 0
    assert completion.stdout == ""
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "completion"
    assert heartbeat["result_nonempty"] is False


def test_claude_code_stop_heartbeat_records_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _config, _install_root, payload = install_runtime_hooks(tmp_path, monkeypatch)
    target = next(item for item in payload["targets"] if item["runtime"] == "claude-code")
    hook = runtime_hook_path(Path(target["root"]) / target["hook_target"])
    runtime_config = json.loads((Path(target["root"]) / "settings.json").read_text(encoding="utf-8"))
    stop_handler = runtime_config["hooks"]["Stop"][-1]["hooks"][0]

    completed = run_configured_hook(stop_handler, "claude-code", {
        "hook_event_name": "Stop",
        "session_id": "claude-stop-session",
        "cwd": str(state),
    })

    assert completed.returncode == 0
    assert completed.stdout == ""
    heartbeat = json.loads((hook.parent / ".lessons-hook-heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["schema"] == "lessons-hook-heartbeat/2"
    assert heartbeat["stage"] == "completion"
    assert heartbeat["session_id"] == "claude-stop-session"


def test_lessons_hook_heartbeat_records_null_for_truncated_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook, heartbeat, _payload = heartbeat_fixture(tmp_path)
    event = tmp_path / "truncated.json"
    event.write_text(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "shared constant",
        "cwd": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_HOOK_SCRIPT", str(hook))
    monkeypatch.setenv("AGENT_CORE_HOOK_HEARTBEAT", str(heartbeat))

    assert match_main([
        "hook", "--runtime", "claude-code", "--stage", "prompt",
        "--ledger", str(ROOT / "seed" / "LESSONS.md"), "--event-json", str(event),
    ]) == 0
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["schema"] == "lessons-hook-heartbeat/2"
    assert payload["session_id"] is None


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


def test_equivalent_unowned_runtime_hooks_are_foreign_without_a_force_bypass(
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
    ambiguous = json.loads(json.dumps(desired))
    ambiguous["PreToolUse"].append(ambiguous["PreToolUse"][0])
    before = json.dumps({"hooks": ambiguous}, separators=(",", ":")).encode("utf-8")
    settings.write_bytes(before)
    plan = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(
        line.startswith("TARGET runtime-config:claude-code status=foreign ") for line in plan
    )
    assert plan[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=True)
    assert settings.read_bytes() == before
    assert not (_install_root / "engine-pin.json").exists()
    assert not (config.parent / "install-receipt.json").exists()


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


@pytest.mark.skipif(os.name != "nt", reason="requires generated Windows PowerShell hook")
def test_windows_generated_hook_preserves_raw_event_bytes_and_cleans_temp(
    tmp_path: Path,
) -> None:
    """The Windows hook must transport stdin without a text decode/re-encode step."""
    rendered = _powershell_hook_content([], "claude-code")
    assert b"[Console]::OpenStandardInput().CopyTo($eventStream)" in rendered
    assert b"[IO.FileMode]::CreateNew" in rendered
    assert b"--event-json $eventPath" in rendered
    assert b"} finally {" in rendered
    assert b"WARNING lessons hook event cleanup unavailable" in rendered
    assert _hook_content([], "claude-code") == (
        b'#!/bin/sh\n'
        b'# agent-core-lessons-hook/1\n'
        b'# Generated from host prompt_injection; edit the host config, not this file.\n'
        b'stage=${1:-prompt}\n'
        b'case "$stage" in prompt|pretool|completion) ;; *) echo \'WARNING lessons hook invalid stage\' >&2; exit 0 ;; esac\n'
        b'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0\n'
        b'export AGENT_CORE_HOOK_HEARTBEAT="$script_dir/.lessons-hook-heartbeat.json"\n'
        b'export AGENT_CORE_HOOK_SCRIPT="$0"\n'
        b'if [ -n "${AGENT_CORE_COMMAND:-}" ]; then\n'
        b'  agent_core=$AGENT_CORE_COMMAND\n'
        b'elif command -v agent-core >/dev/null 2>&1; then\n'
        b'  agent_core=$(command -v agent-core)\n'
        b'else\n'
        b'  agent_core="${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core"\n'
        b'fi\n'
        b'if [ "$stage" = prompt ]; then\n'
        b'fi\n'
        b'"$agent_core" lessons hook --runtime claude-code --stage "$stage"\n'
        b'status=$?\n'
        b'if [ "$status" -ne 0 ]; then echo "WARNING lessons hook command failed: $status" >&2; fi\n'
        b'exit 0\n'
    )
    hook = tmp_path / "lessons-hook.ps1"
    hook.write_bytes(rendered)
    captured = tmp_path / "captured-event.bin"
    recorded = tmp_path / "event-path.txt"
    reader = tmp_path / "event-reader.py"
    reader.write_text(
        """import os
import pathlib
import sys

event = pathlib.Path(sys.argv[sys.argv.index('--event-json') + 1])
pathlib.Path(os.environ['EVENT_CAPTURE']).write_bytes(event.read_bytes())
pathlib.Path(os.environ['EVENT_PATH_RECORD']).write_text(str(event), encoding='utf-8')
raise SystemExit(int(os.environ.get('EVENT_READER_EXIT', '0')))
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "event-reader.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{reader}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )
    raw = (
        json.dumps(
            {
                "hook_event_name": "Stop",
                "nested": {"text": "繁體測試" * 320, "items": [1, {"value": "x"}]},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\r\n"
    )
    environment = os.environ.copy()
    environment.update({
        "AGENT_CORE_COMMAND": str(wrapper),
        "EVENT_CAPTURE": str(captured),
        "EVENT_PATH_RECORD": str(recorded),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "completion"],
        input=raw,
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0
    assert captured.read_bytes() == raw
    event_path = Path(recorded.read_text(encoding="utf-8"))
    assert not event_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires generated Windows PowerShell hook")
@pytest.mark.parametrize(
    ("raw", "validate_json", "reader_exit", "expected_warning"),
    [
        (b'{"broken":', True, 9, "WARNING lessons hook command failed: 23"),
        (b'{"hook_event_name":"Stop"}', False, 9, "WARNING lessons hook command failed: 9"),
    ],
)
def test_windows_generated_hook_fails_open_for_event_rejection_and_wrapper_failure(
    tmp_path: Path,
    raw: bytes,
    validate_json: bool,
    reader_exit: int,
    expected_warning: str,
) -> None:
    hook = tmp_path / "lessons-hook.ps1"
    hook.write_bytes(_powershell_hook_content([], "claude-code"))
    captured = tmp_path / "captured-event.bin"
    recorded = tmp_path / "event-path.txt"
    reader = tmp_path / "event-reader.py"
    reader.write_text(
        """import json
import os
import pathlib
import sys

event = pathlib.Path(sys.argv[sys.argv.index('--event-json') + 1])
raw = event.read_bytes()
pathlib.Path(os.environ['EVENT_CAPTURE']).write_bytes(raw)
pathlib.Path(os.environ['EVENT_PATH_RECORD']).write_text(str(event), encoding='utf-8')
if os.environ.get('EVENT_VALIDATE_JSON') == '1':
    try:
        json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit(23)
raise SystemExit(int(os.environ['EVENT_READER_EXIT']))
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "event-reader.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{reader}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({
        "AGENT_CORE_COMMAND": str(wrapper),
        "EVENT_CAPTURE": str(captured),
        "EVENT_PATH_RECORD": str(recorded),
        "EVENT_READER_EXIT": str(reader_exit),
        "EVENT_VALIDATE_JSON": "1" if validate_json else "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "completion"],
        input=raw,
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0
    assert captured.read_bytes() == raw
    assert not (tmp_path / "state" / "inbox").exists()
    event_path = Path(recorded.read_text(encoding="utf-8"))
    assert not event_path.exists()
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert expected_warning in stderr
    assert raw.decode("utf-8", errors="replace") not in stderr
    assert str(event_path) not in stderr


@pytest.mark.skipif(os.name != "nt", reason="requires generated Windows PowerShell hook")
def test_windows_generated_hook_bounds_missing_wrapper_invocation_error(tmp_path: Path) -> None:
    hook = tmp_path / "lessons-hook.ps1"
    hook.write_bytes(_powershell_hook_content([], "claude-code"))
    missing_wrapper = tmp_path / "missing-agent-core.cmd"
    raw = b'{"hook_event_name":"Stop","marker":"private-event-content"}\r\n'
    environment = os.environ.copy()
    environment.update({
        "AGENT_CORE_COMMAND": str(missing_wrapper),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "completion"],
        input=raw,
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert stderr.strip() == "WARNING lessons hook event transport unavailable"
    assert raw.decode("utf-8") not in stderr
    assert str(missing_wrapper) not in stderr
    assert not list(tmp_path.glob(".agent-core-hook-event-*.json"))


@pytest.mark.skipif(os.name != "nt", reason="requires generated Windows PowerShell hook")
def test_windows_generated_hook_malformed_event_reaches_real_python_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _config, _install_root, payload = install_runtime_hooks(tmp_path, monkeypatch)
    target = next(item for item in payload["targets"] if item["runtime"] == "claude-code")
    hook = runtime_hook_path(Path(target["root"]) / target["hook_target"])
    inbox = state / "inbox"
    before = sorted(path.name for path in inbox.glob("*.md"))
    environment = os.environ.copy()
    environment.update({
        "AGENT_CORE_PYTHON": os.fspath(Path(os.sys.executable)),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "completion"],
        input=b'{"broken":',
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "WARNING lessons hook unavailable:" in completed.stderr.decode("utf-8", errors="replace")
    assert sorted(path.name for path in inbox.glob("*.md")) == before
    assert not list(tmp_path.glob(".agent-core-hook-event-*.json"))


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file sharing semantics")
def test_windows_generated_hook_cleanup_failure_is_bounded_and_fail_open(tmp_path: Path) -> None:
    hook = tmp_path / "lessons-hook.ps1"
    hook.write_bytes(_powershell_hook_content([], "claude-code"))
    captured = tmp_path / "captured-event.bin"
    recorded = tmp_path / "event-path.txt"
    ready = tmp_path / "lock-ready"
    holder = tmp_path / "event-holder.py"
    holder.write_text(
        """import ctypes
import pathlib
import sys
import time

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
handle = kernel32.CreateFileW(sys.argv[1], 0x80000000, 0, None, 3, 0x00000080, None)
if handle == ctypes.c_void_p(-1).value:
    raise SystemExit(2)
pathlib.Path(sys.argv[2]).write_bytes(b'1')
time.sleep(1.5)
kernel32.CloseHandle(handle)
""",
        encoding="utf-8",
    )
    reader = tmp_path / "event-reader.py"
    reader.write_text(
        """import os
import pathlib
import subprocess
import sys
import time

event = pathlib.Path(sys.argv[sys.argv.index('--event-json') + 1])
pathlib.Path(os.environ['EVENT_CAPTURE']).write_bytes(event.read_bytes())
pathlib.Path(os.environ['EVENT_PATH_RECORD']).write_text(str(event), encoding='utf-8')
subprocess.Popen([sys.executable, '-B', os.environ['EVENT_HOLDER'], str(event), os.environ['EVENT_LOCK_READY']], env=os.environ.copy())
deadline = time.monotonic() + 5
while not pathlib.Path(os.environ['EVENT_LOCK_READY']).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
raise SystemExit(0 if pathlib.Path(os.environ['EVENT_LOCK_READY']).exists() else 88)
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "event-reader.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{reader}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )
    raw = b'{"hook_event_name":"Stop","nested":{"value":"cleanup"}}\r\n'
    environment = os.environ.copy()
    environment.update({
        "AGENT_CORE_COMMAND": str(wrapper),
        "EVENT_CAPTURE": str(captured),
        "EVENT_PATH_RECORD": str(recorded),
        "EVENT_HOLDER": str(holder),
        "EVENT_LOCK_READY": str(ready),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook), "completion"],
        input=raw,
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )
    event_path = Path(recorded.read_text(encoding="utf-8"))
    try:
        assert completed.returncode == 0
        assert captured.read_bytes() == raw
        assert event_path.exists()
        stderr = completed.stderr.decode("utf-8", errors="replace")
        assert "WARNING lessons hook event cleanup unavailable" in stderr
        assert raw.decode("utf-8") not in stderr
        assert str(event_path) not in stderr
    finally:
        time.sleep(1.6)
        event_path.unlink(missing_ok=True)


@pytest.mark.parametrize("runtime", ["claude-code", "codex"])
def test_lessons_hook_parse_failure_is_fail_open_and_heartbeat_is_explainable(
    runtime: str,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    hook = tmp_path / "hook.sh"
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        hook.chmod(0o755)
    heartbeat = tmp_path / ".lessons-hook-heartbeat.json"
    event = tmp_path / "broken.json"
    event.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_HOOK_SCRIPT", str(hook))
    monkeypatch.setenv("AGENT_CORE_HOOK_HEARTBEAT", str(heartbeat))
    assert match_main([
        "hook", "--runtime", runtime, "--stage", "prompt",
        "--ledger", str(ROOT / "seed" / "LESSONS.md"), "--event-json", str(event),
    ]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "WARNING lessons hook unavailable" in captured.err
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["schema"] == "lessons-hook-heartbeat/2"
    assert payload["session_id"] is None
    assert payload["status"] == "warning"
    assert payload["retrieval_invoked"] is True
    assert payload["result_nonempty"] is False
    assert hook_retrieval_status(hook) == ("WARN", "retrieval_warning stage=prompt")


def test_completion_hook_auto_captures_only_for_claude_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    claude_event = tmp_path / "claude-stop.json"
    claude_event.write_text(json.dumps(completion_payload(capture_block())), encoding="utf-8")
    monkeypatch.setattr("agent_core.capture.default_config_path", lambda _root: config)

    assert match_main([
        "hook", "--runtime", "claude-code", "--stage", "completion",
        "--ledger", str(ledger), "--event-json", str(claude_event),
    ]) == 0
    assert len(list((state / "inbox").glob("*.md"))) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "WARNING automatic lesson capture" not in captured.err

    codex_event = tmp_path / "codex-stop.json"
    changed = completion_payload(capture_block({
        "rule": "当检索范围变化时，先重新确认边界", "trigger": "search scope changed",
        "cost": "wrong files", "sink": "checks/scope.md",
    }))
    codex_event.write_text(json.dumps(changed), encoding="utf-8")
    assert match_main([
        "hook", "--runtime", "codex", "--stage", "completion",
        "--ledger", str(ledger), "--event-json", str(codex_event),
    ]) == 0
    assert len(list((state / "inbox").glob("*.md"))) == 1
    assert capsys.readouterr().out == ""


def test_completion_hook_reports_bounded_gate_warning_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    before = ledger.read_bytes()
    event = tmp_path / "claude-stop.json"
    payload = completion_payload(capture_block())
    del payload["stop_hook_active"]
    event.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("agent_core.capture.default_config_path", lambda _root: config)

    assert match_main([
        "hook", "--runtime", "claude-code", "--stage", "completion",
        "--ledger", str(ledger), "--event-json", str(event),
    ]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "WARNING automatic lesson capture unavailable gate=payload-active-missing"
    )
    assert ledger.read_bytes() == before
    assert not (state / "inbox").exists()
