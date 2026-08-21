from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_core import cli


ROOT = Path(__file__).resolve().parents[1]
FROZEN_COMMANDS = (
    (["candidate", "publish"], "candidate"),
    (["promote"], "promote"),
    (["rollback"], "rollback"),
    (["recover"], "recover"),
    (["state", "init"], "state"),
    (["migrate"], "migrate"),
    (["engine", "upgrade"], "engine upgrade"),
    (["check"], "check"),
    (["manifest", "compose"], "manifest"),
    (["fingerprint"], "fingerprint"),
    (["parity"], "parity"),
    (["docs", "verify"], "docs"),
    (["privacy", "scan"], "privacy"),
    (["uninstall"], "uninstall"),
)


def test_top_level_help_only_lists_v01_product_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "{install,sync,doctor,lessons}" in output
    for command in ("candidate", "promote", "rollback", "recover", "state", "migrate",
                    "engine", "check", "manifest", "fingerprint", "parity", "docs",
                    "privacy", "uninstall"):
        assert command not in output


@pytest.mark.parametrize(("argv", "command"), FROZEN_COMMANDS)
def test_frozen_commands_fail_before_old_handlers(
        argv: list[str], command: str, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "C:" + "/PRIVATE_PATH_MARKER/secret"

    assert cli.main([*argv, "--state", marker]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"FAIL_COMMAND_FROZEN {command}\n"
    assert marker not in captured.err
    assert str(ROOT) not in captured.err


def test_frozen_uninstall_does_not_call_installer(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    called = False

    def old_handler(*_args: object, **_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli.installer, "main", old_handler)

    assert cli.main(["uninstall", "--config", "C:" + "/PRIVATE_PATH_MARKER/config.json"]) == 2
    assert called is False
    assert capsys.readouterr().err == "FAIL_COMMAND_FROZEN uninstall\n"


def test_unknown_command_is_rejected_by_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["promtoe"])

    assert exc_info.value.code == 2
    assert "invalid choice: 'promtoe'" in capsys.readouterr().err


def test_wrapper_version_from_temporary_cwd(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["AGENT_CORE_PYTHON"] = os.environ.get("ACCEPTANCE_PYTHON", os.sys.executable)
    if os.name == "nt":
        command = ["cmd.exe", "/d", "/c", str(ROOT / "agent-core.cmd"), "--version"]
    else:
        command = ["sh", str(ROOT / "agent-core"), "--version"]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0.dev0"
