from __future__ import annotations

import errno
import os
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_core import installer
from tests.test_install import installed_fixture


PROMPT = "确认执行？[y/N] "


@pytest.fixture
def install_cli(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], dict[str, str], Path, Path, Path]:
    for name in tuple(os.environ):
        if name.startswith("AGENT_CORE_"):
            monkeypatch.delenv(name)
    state, config, manifest, install_root = installed_fixture(
        standalone_engine, tmp_path, monkeypatch,
    )
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONPATH=str(standalone_engine), PYTHONDONTWRITEBYTECODE="1")
    command = [
        sys.executable, "-P", "-m", "agent_core.cli", "install",
        "--config", str(config), "--state", str(state),
        "--source", str(standalone_engine), "--artifact-manifest", str(manifest),
    ]
    return command, environment, state, config, install_root


def _targets(config: Path, install_root: Path) -> dict[str, tuple[bytes | None, int]]:
    roots = [config.parent, install_root, config.parent.parent / "runtimes"]
    return {
        str(path): (path.read_bytes() if path.is_file() else None, path.stat().st_mode)
        for root in roots if root.exists()
        for path in [root, *root.rglob("*")]
    }


@contextmanager
def _terminal(
    command: list[str], environment: dict[str, str], *, piped: str | None = None,
) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    import pty

    master, slave = pty.openpty()
    process = None
    try:
        process = subprocess.Popen(
            command, env=environment, cwd=environment["HOME"],
            stdin=subprocess.PIPE if piped == "stdin" else slave,
            stdout=subprocess.PIPE if piped == "stdout" else slave, stderr=slave,
        )
        os.close(slave)
        slave = -1
        yield process, master
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        os.close(master)
        if slave != -1:
            os.close(slave)


def _read_terminal(master: int, until: str | None = None) -> str:
    output = bytearray()
    deadline = time.monotonic() + 60
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"terminal deadline exceeded: {output.decode('utf-8', errors='replace')}"
        readable, _, _ = select.select([master], [], [], remaining)
        assert readable, f"terminal timed out: {output.decode('utf-8', errors='replace')}"
        try:
            chunk = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            chunk = b""
        if not chunk:
            assert until is None, f"terminal closed before {until!r}: {output.decode('utf-8')}"
            return output.decode("utf-8")
        output.extend(chunk)
        if until is not None and until.encode("utf-8") in output:
            return output.decode("utf-8")


def test_pipe_never_confirms_install(install_cli) -> None:
    command, environment, _state, config, install_root = install_cli
    before = _targets(config, install_root)
    result = subprocess.run(
        command, input="y\n", capture_output=True, text=True, encoding="utf-8",
        env=environment, cwd=environment["HOME"], timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PLAN_HASH " in result.stdout
    assert "DRY_RUN writes=0 ready=true" in result.stdout
    assert PROMPT not in result.stdout
    assert "APPLIED" not in result.stdout
    assert not (config.parent / "install-receipt.json").exists()
    assert _targets(config, install_root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal required")
@pytest.mark.parametrize("piped", ["stdin", "stdout"])
def test_one_piped_stream_never_confirms_install(install_cli, piped: str) -> None:
    command, environment, _state, config, install_root = install_cli
    before = _targets(config, install_root)
    with _terminal(command, environment, piped=piped) as (process, master):
        if piped == "stdin":
            assert process.stdin is not None
            process.stdin.write(b"y\n")
            process.stdin.close()
            process.stdin = None
            output = _read_terminal(master)
        else:
            os.write(master, b"y\n")
            stdout, _ = process.communicate(timeout=60)
            output = stdout.decode("utf-8") + _read_terminal(master)
        assert process.wait(timeout=10) == 0, output
    assert "PLAN_HASH " in output
    assert "DRY_RUN writes=0 ready=true" in output
    assert PROMPT not in output
    assert "APPLIED" not in output
    assert not (config.parent / "install-receipt.json").exists()
    assert _targets(config, install_root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal required")
@pytest.mark.parametrize("answer", [b"y\n", b"  YeS  \n", b"n\n", b"\n", b"\x04"])
def test_terminal_install_confirmation(install_cli, answer: bytes) -> None:
    command, environment, _state, config, install_root = install_cli
    before = _targets(config, install_root)
    with _terminal(command, environment) as (process, master):
        output = _read_terminal(master, PROMPT)
        assert "PLAN_HASH " in output
        assert "DRY_RUN writes=0 ready=true" in output
        assert _targets(config, install_root) == before
        os.write(master, answer)
        output += _read_terminal(master)
        result = process.wait(timeout=10)
    receipt = config.parent / "install-receipt.json"
    if answer.strip().lower() in {b"y", b"yes"}:
        assert result == 0, output
        assert "APPLIED install" in output
        assert "PASS artifact_sha256=" in output
        assert receipt.is_file()
        engine = install_root / "engine" / installer.__version__
        installer.verify_release_manifest(engine, engine / "release-manifest.json")
    else:
        assert result == 1, output
        assert "CANCELLED writes=0" in output
        assert "APPLIED" not in output
        assert not receipt.exists()
        assert _targets(config, install_root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal required")
def test_terminal_confirmation_revalidates_drift(install_cli) -> None:
    command, environment, _state, config, install_root = install_cli
    with _terminal(command, environment) as (process, master):
        output = _read_terminal(master, PROMPT)
        foreign = install_root / "engine-pin.json"
        assert f"status=absent action=WRITE path={foreign}" in output
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign bytes\n")
        before = _targets(config, install_root)
        os.write(master, b"y\n")
        output += _read_terminal(master)
        assert process.wait(timeout=10) != 0, output
    assert "FAIL_PLAN_HASH install plan hash differs from reviewed plan" in output
    assert "APPLIED" not in output
    assert not (config.parent / "install-receipt.json").exists()
    assert foreign.read_bytes() == b"foreign bytes\n"
    assert _targets(config, install_root) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal required")
def test_terminal_blocked_plan_does_not_prompt(install_cli) -> None:
    command, environment, _state, config, install_root = install_cli
    install_root.mkdir(parents=True)
    (install_root / "engine-pin.json").write_bytes(b"foreign bytes\n")
    before = _targets(config, install_root)
    with _terminal(command, environment) as (process, master):
        output = _read_terminal(master)
        assert process.wait(timeout=10) == 0, output
    assert "DRY_RUN writes=0 ready=false" in output
    assert "PLAN_HASH " not in output
    assert PROMPT not in output
    assert "APPLIED" not in output
    assert not (config.parent / "install-receipt.json").exists()
    assert _targets(config, install_root) == before
