from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core import __version__, cli, launcher, lock


ROOT = Path(__file__).resolve().parents[1]
REMOVED_COMMANDS = (
    ["candidate"], ["check"], ["docs"], ["fingerprint"], ["manifest"],
    ["migrate"], ["parity"], ["privacy"], ["promote"], ["recover"],
    ["rollback"], ["state"], ["uninstall"], ["engine", "upgrade"],
    ["install", "uninstall"],
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


def test_lessons_help_discovers_all_public_and_retrieval_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["lessons", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "{match,eval,hook}" in output
    assert "public commands routed by agent-core: capture, promote, reject, retire" in output


@pytest.mark.parametrize("argv", (["install", "--help"], ["sync", "--help"], ["doctor", "--help"]))
def test_product_command_help_exits_successfully(
    argv: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: agent-core ")
    assert captured.err == ""


@pytest.mark.parametrize("argv", REMOVED_COMMANDS, ids=lambda argv: " ".join(argv))
def test_removed_commands_argparse_fail_without_side_effects(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "C:" + "/PRIVATE_PATH_MARKER/secret"
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("removed command reached a handler, lock, or subprocess")

    handlers = []
    for module, name in (
        (cli, "execute_sync"), (cli, "run_doctor"),
        (cli.capture, "main"), (cli.project_promote, "main"),
        (cli.reject, "main"), (cli.retire, "main"), (cli.lesson_match, "main"),
        (lock, "operation_lock"), (cli.installer, "operation_lock"),
        (cli.installer, "plan_install"), (cli.installer, "apply_install"),
        (cli.installer, "load_config"), (subprocess, "run"), (subprocess, "Popen"),
    ):
        handler = Mock(side_effect=forbidden)
        monkeypatch.setattr(module, name, handler)
        handlers.append(handler)
    installer_main = Mock(wraps=cli.installer.main) if argv == ["install", "uninstall"] else Mock(side_effect=forbidden)
    installer_parser = Mock(wraps=cli.installer._parser)
    monkeypatch.setattr(cli.installer, "main", installer_main)
    monkeypatch.setattr(cli.installer, "_parser", installer_parser)
    arguments = [*argv, "--config", marker, "--state", marker]

    with pytest.raises(SystemExit) as exc_info:
        cli.main(arguments)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "PRIVATE_PATH_MARKER" not in captured.err
    assert str(ROOT) not in captured.err
    for handler in handlers:
        handler.assert_not_called()
    if argv == ["install", "uninstall"]:
        installer_main.assert_called_once_with(arguments, cli.ENGINE_ROOT)
        installer_parser.assert_called_once_with(cli.ENGINE_ROOT.resolve())
        assert "unrecognized arguments: uninstall" in captured.err
    else:
        installer_main.assert_not_called()
        installer_parser.assert_not_called()
        assert f"invalid choice: '{argv[0]}'" in captured.err
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("argv", REMOVED_COMMANDS[:-1], ids=lambda argv: " ".join(argv))
def test_launcher_does_not_inject_state_into_removed_commands(argv: list[str], tmp_path: Path) -> None:
    assert launcher._inject_state(argv, tmp_path / "PRIVATE_PATH_MARKER") == argv


@pytest.mark.skipif(os.name == "nt", reason="POSIX wrapper contract")
def test_posix_wrapper_propagates_removed_command_exit_status(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["AGENT_CORE_PYTHON"] = os.sys.executable
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = subprocess.run(
        ["sh", str(ROOT / "agent-core"), "recover", "--state", "PRIVATE_PATH_MARKER"],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid choice: 'recover'" in result.stderr
    assert "PRIVATE_PATH_MARKER" not in result.stderr
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


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
    assert result.stdout.strip() == __version__
