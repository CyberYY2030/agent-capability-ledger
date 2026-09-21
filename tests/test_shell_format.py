from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest


ENGINE_ROOT = Path(__file__).resolve().parents[1]
CHECKER = ENGINE_ROOT / "templates" / "check_shell_format.py"


def run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *(str(path) for path in paths)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


@pytest.mark.parametrize("ending", [b"\r", b"\r\n"])
def test_rejects_carriage_returns_in_shell_files(tmp_path: Path, ending: bytes) -> None:
    script = tmp_path / "bad.sh"
    script.write_bytes(b"#!/bin/sh" + ending + b"echo bad" + ending)

    result = run_checker(script)

    assert result.returncode == 1
    assert f"FAIL {script} reason=carriage-return" in result.stderr
    assert result.stdout == ""


def test_accepts_lf_and_utf8_in_shell_files(tmp_path: Path) -> None:
    script = tmp_path / "good.sh"
    script.write_bytes("#!/bin/sh\nprintf '中文正常\\n'\n".encode())

    result = run_checker(script)

    assert result.returncode == 0
    assert result.stdout == f"PASS {script}\n"
    assert result.stderr == ""


def test_regular_non_shell_files_are_not_applicable(tmp_path: Path) -> None:
    document = tmp_path / "notes.md"
    powershell = tmp_path / "setup.ps1"
    document.write_bytes(b"documentation\r\n")
    powershell.write_text("Write-Output '中文'\r\n", encoding="utf-8", newline="")

    result = run_checker(document, powershell)

    assert result.returncode == 0
    assert result.stdout == f"SKIP {document} reason=not-shell\nSKIP {powershell} reason=not-shell\n"
    assert result.stderr == ""


def test_missing_path_fails_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sh"

    result = run_checker(missing)

    assert result.returncode == 1
    assert result.stderr == f"FAIL {missing} reason=missing\n"


def test_non_regular_path_fails_clearly(tmp_path: Path) -> None:
    directory = tmp_path / "directory.sh"
    directory.mkdir()

    result = run_checker(directory)

    assert result.returncode == 1
    assert result.stderr == f"FAIL {directory} reason=not-regular-file\n"


def test_symbolic_link_fails_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target.sh"
    target.write_bytes(b"#!/bin/sh\n")
    link = tmp_path / "link.sh"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    result = run_checker(link)

    assert result.returncode == 1
    assert result.stderr == f"FAIL {link} reason=symlink\n"


def test_space_in_path_is_one_explicit_input(tmp_path: Path) -> None:
    script = tmp_path / "final artifact.sh"
    script.write_bytes(b"#!/bin/sh\necho ok\n")

    result = run_checker(script)

    assert result.returncode == 0
    assert result.stdout == f"PASS {script}\n"


def test_read_only_file_is_checked_without_mutation(tmp_path: Path) -> None:
    script = tmp_path / "read only.sh"
    original = b"#!/bin/sh\necho unchanged\n"
    script.write_bytes(original)
    script.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    before = script.stat()

    result = run_checker(script)

    after = script.stat()
    assert result.returncode == 0
    assert script.read_bytes() == original
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime_ns == before.st_mtime_ns
