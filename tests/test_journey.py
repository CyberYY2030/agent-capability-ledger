from __future__ import annotations

import errno
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_core import ledger
from tests.test_prepare_join import public_source


def _machine_env(home: Path) -> dict[str, str]:
    shim = home / "python-bin"
    shim.mkdir()
    for name in ("python3", "python3.11", "python3.12", "python3.13", "python"):
        (shim / name).symlink_to(sys.executable)
    directories = [str(shim)]
    for name in ("git", "sh"):
        executable = shutil.which(name, path=os.defpath)
        assert executable is not None, f"required executable missing: {name}"
        directory = str(Path(executable).parent)
        if directory not in directories:
            directories.append(directory)
    for directory in directories:
        candidate = Path(directory) / "agent-core"
        assert not (candidate.is_file() and os.access(candidate, os.X_OK)), candidate
    temporary = home / "tmp"
    temporary.mkdir()
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "LOCALAPPDATA": str(home / ".local/share"),
        "PATH": os.pathsep.join(directories),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }


def _quickstart_commands(document: str, machine: str, remote: Path) -> list[str]:
    opening = f"<!-- journey:{machine} -->"
    closing = f"<!-- /journey:{machine} -->"
    assert document.count(opening) == document.count(closing) == 1
    section = document.split(opening)[1].split(closing)[0]
    blocks = re.findall(r"^```bash\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)
    assert blocks, f"no bash commands in {machine}"
    commands = []
    for block in blocks:
        command = block.strip().replace("<你的私有远端地址>", shlex.quote(str(remote)))
        assert len(command.splitlines()) == 1, f"expected one command per block: {command}"
        assert re.search(r"<[^>]+>", command) is None, f"unresolved placeholder: {command}"
        commands.append(command)
    return commands


def _confirmed_install(command: str, home: Path, environment: dict[str, str]) -> str:
    import pty

    master, slave = pty.openpty()
    process = None
    output = bytearray()
    confirmed = False
    deadline = time.monotonic() + 180
    prompt = "确认执行？[y/N]".encode()
    try:
        process = subprocess.Popen(
            ["sh", "-c", command], cwd=home, env=environment,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        while True:
            remaining = deadline - time.monotonic()
            detail = f"{command}\n{output.decode('utf-8', errors='replace')}"
            assert remaining > 0, f"install deadline exceeded:\n{detail}"
            readable, _, _ = select.select([master], [], [], remaining)
            assert readable, f"install timed out:\n{detail}"
            try:
                chunk = os.read(master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                chunk = b""
            if not chunk:
                break
            output.extend(chunk)
            if not confirmed and prompt in output:
                plan = output.split(prompt)[0].decode("utf-8")
                assert "ready=true" in plan, f"{command}\n{plan}"
                assert not any(line.startswith("NEXT ") for line in plan.splitlines()), plan
                os.write(master, b"y\n")
                confirmed = True
        rendered = output.decode("utf-8")
        detail = f"{command}\n{rendered}"
        assert confirmed, f"install closed before confirmation:\n{detail}"
        assert process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0, detail
        assert "APPLIED install" in rendered and "PASS" in rendered, detail
        return rendered
    finally:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        os.close(master)
        if slave != -1:
            os.close(slave)


def _field(output: str, name: str) -> str:
    values = [
        line.removeprefix(name + " ")
        for line in output.splitlines() if line.startswith(name + " ")
    ]
    assert len(values) == 1, f"expected exactly one {name}:\n{output}"
    return values[0]


def _prompt_command(runtime_root: Path, config_name: str) -> str:
    config = json.loads((runtime_root / config_name).read_text(encoding="utf-8"))
    hook = runtime_root / "hooks/user_prompt.sh"
    commands = [
        handler["command"]
        for group in config["hooks"]["UserPromptSubmit"]
        for handler in group["hooks"]
        if handler.get("type") == "command"
        and str(hook) in shlex.split(handler["command"])
        and shlex.split(handler["command"])[-1] == "prompt"
    ]
    assert len(commands) == 1, config
    return commands[0]


@pytest.mark.skipif(os.name == "nt", reason="QUICKSTART uses POSIX sh commands and a pseudo-terminal")
def test_two_machine_quickstart_journey(
    tmp_path: Path, record_property: Callable[[str, object], None],
) -> None:
    def run(
        home: Path, command: str, *, payload: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> str:
        environment = environments[home]
        transcript = [f"{home.name}$ {command}"]
        try:
            if "install.sh" in command:
                output = _confirmed_install(command, home, environment)
            else:
                options = (
                    {"stdin": subprocess.DEVNULL} if payload is None
                    else {"input": json.dumps(payload)}
                )
                result = subprocess.run(
                    ["sh", "-c", command], cwd=cwd or home, env=environment,
                    capture_output=True, text=True, encoding="utf-8", timeout=300,
                    check=False, **options,
                )
                output = result.stdout + result.stderr
                assert result.returncode == 0, f"{command}\nexit={result.returncode}\n{output}"
            transcript.append(output)
            return output
        finally:
            record_property("journey_command", "\n".join(transcript))

    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    home_a.mkdir()
    home_b.mkdir()
    (home_a / ".claude").mkdir()
    (home_b / ".codex").mkdir()
    environments = {home: _machine_env(home) for home in (home_a, home_b)}
    source = public_source(tmp_path)
    source = source.rename(home_a / "agent-core")
    document = (source / "docs/QUICKSTART.md").read_text(encoding="utf-8")
    remote = home_a / "agent-core-remote.git"
    first = _quickstart_commands(document, "first-machine", remote)
    second = _quickstart_commands(document, "second-machine", remote)
    assert len(first) <= 6, f"QUICKSTART exceeds six commands: {first}"
    record_property("first_machine_command_count", len(first))
    record_property("second_machine_command_count", len(second))
    trial = "git init --bare --initial-branch=main ~/agent-core-remote.git"
    assert f"`{trial}`" in document.split("<!-- journey:first-machine -->")[0]
    run(home_a, trial)

    project = tmp_path / "profile-project"
    project.mkdir()
    run(home_a, f"git init --initial-branch=main {shlex.quote(str(project))}")
    (project / ".agents").mkdir()
    (project / ".agents/lessons.json").write_text(json.dumps({
        "schema": ledger.ROUTING_SCHEMA, "project_id": "journey-project",
        "profiles": ["example-domain"],
    }) + "\n", encoding="utf-8")

    assert shlex.split(first[-1])[1] == "doctor", first[-1]
    for command in first:
        run(home_a, command)

    payload = {
        "session_id": "journey-a", "hook_event_name": "UserPromptSubmit",
        "prompt": "public fixture", "cwd": str(project),
    }
    hook_a = _prompt_command(home_a / ".claude", "settings.json")
    claude = run(home_a, hook_a, payload=payload, cwd=project)
    assert "EXAMPLE-1" in claude and "WARNING" not in claude, claude
    wrapper = '"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core"'
    codex = run(home_a, f"{wrapper} lessons hook --runtime codex --stage prompt",
                payload={**payload, "session_id": "journey-a-codex"}, cwd=project)
    assert "EXAMPLE-1" in codex and "WARNING" not in codex, codex
    doctor_a = run(home_a, first[-1])
    assert "PASS lessons_hook target=claude-code retrieval_nonempty stage=prompt" in doctor_a, doctor_a
    assert all(line.startswith("PASS ") for line in doctor_a.splitlines()), doctor_a

    captured = run(home_a, f"{wrapper} lessons capture --config ~/.agent-core/host.json "
                   "--workspace ~/agent-core-private --agent codex --scope global "
                   "--rule '当要写数据库迁移时，先在本地副本上演练回滚' "
                   "--trigger '写数据库迁移' --cost '迁移上线后无法回滚，只能手工修数据' "
                   "--sink 'docs/migration-checklist.md' --evidence 'synthetic:two-machine-journey' "
                   "--when '{\"text\":[\"迁移\"]}'")
    candidate = Path(_field(captured, "CAPTURED"))
    assert candidate.is_relative_to(home_a / "agent-core-private"), captured
    promote = (f"{wrapper} lessons promote --config ~/.agent-core/host.json "
               f"--workspace ~/agent-core-private --id {shlex.quote(candidate.stem)} --force-new")
    plan = run(home_a, promote)
    lesson_id = _field(plan, "RESULT_ID")
    record_property("promoted_lesson_id", lesson_id)
    promoted = run(home_a, f"{promote} --apply --plan-hash {shlex.quote(_field(plan, 'PLAN_HASH'))}")
    assert f"PASS project_promoted={lesson_id}" in promoted, promoted
    staged = [
        line.removeprefix("STAGED ")
        for line in promoted.splitlines() if line.startswith("STAGED ")
    ]
    expected = {
        "state/experience/LESSONS.md",
        (candidate.relative_to(home_a / "agent-core-private").parent / "consumed" / candidate.name).as_posix(),
    }
    assert set(staged) == expected, promoted
    status = run(home_a, "git -C ~/agent-core-private status --porcelain --untracked-files=all")
    assert {line[3:] for line in status.splitlines()} == expected, status
    assert all(line[:2] in {"A ", "M "} for line in status.splitlines()), status
    run(home_a, "git -C ~/agent-core-private commit -m 'lessons: add migration rollback lesson'")
    run(home_a, "git -C ~/agent-core-private push")

    assert shlex.split(second[-1])[1] == "doctor", second[-1]
    for command in second:
        run(home_b, command)
    sync = f"{wrapper} sync --config ~/.agent-core/host.json"
    preview = run(home_b, sync)
    writes = re.search(r"planned_writes=(\d+) ready=true", preview)
    assert writes is not None, preview
    branch = "already-converged"
    if int(writes[1]):
        branch = "applied-then-converged"
        run(home_b, f"{sync} --apply --plan-hash {shlex.quote(_field(preview, 'PLAN_HASH'))}")
        preview = run(home_b, sync)
    assert "planned_writes=0 ready=true" in preview, preview
    record_property("machine_b_sync_branch", branch)
    hook_b = _prompt_command(home_b / ".codex", "hooks.json")
    output = run(home_b, hook_b, payload={
        "session_id": "journey-b", "hook_event_name": "UserPromptSubmit",
        "prompt": "给 users 表加一个非空的 email 字段，顺便写好迁移", "cwd": str(project),
    }, cwd=project)
    record_property("machine_b_hook_output", output)
    assert lesson_id in output and "WARNING" not in output, output
    doctor_b = run(home_b, second[-1])
    assert "PASS lessons_hook target=codex retrieval_nonempty stage=prompt" in doctor_b, doctor_b
    assert all(line.startswith("PASS ") for line in doctor_b.splitlines()), doctor_b
    for home in (home_a, home_b):
        status = run(home, "git -C ~/agent-core-private status --porcelain --untracked-files=all")
        assert not status.strip(), f"{home.name} private checkout is dirty:\n{status}"
