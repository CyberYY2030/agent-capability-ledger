from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path

import pytest

from agent_core import installer
from agent_core.config import ConfigError


def _linux_move(source: Path, destination: Path) -> None:
    installer._linux_move_no_replace(source, destination)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux renameat2")
def test_linux_directory_move_places_absent_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "payload").write_text("kept\n", encoding="utf-8")

    installer._move_no_replace(source, destination)

    assert not source.exists()
    assert (destination / "payload").read_text(encoding="utf-8") == "kept\n"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux renameat2")
@pytest.mark.parametrize(
    "collision", ["file", "empty-directory", "nonempty-directory", "dangling-symlink"],
)
def test_linux_directory_move_preserves_every_destination_collision(
    tmp_path: Path,
    collision: str,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "payload").write_text("source\n", encoding="utf-8")
    if collision == "file":
        destination.write_text("destination\n", encoding="utf-8")
    elif collision == "empty-directory":
        destination.mkdir()
    elif collision == "nonempty-directory":
        destination.mkdir()
        (destination / "sentinel").write_text("destination\n", encoding="utf-8")
    else:
        destination.symlink_to(tmp_path / "missing")

    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE"):
        _linux_move(source, destination)

    assert (source / "payload").read_text(encoding="utf-8") == "source\n"
    if collision == "file":
        assert destination.read_text(encoding="utf-8") == "destination\n"
    elif collision == "empty-directory":
        assert destination.is_dir() and not any(destination.iterdir())
    elif collision == "nonempty-directory":
        assert (destination / "sentinel").read_text(encoding="utf-8") == "destination\n"
    else:
        assert destination.is_symlink()
        assert os.readlink(destination) == str(tmp_path / "missing")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux renameat2")
def test_linux_directory_move_rejects_destination_created_after_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "payload").write_text("source\n", encoding="utf-8")
    destination.mkdir()
    (destination / "sentinel").write_text("destination\n", encoding="utf-8")
    real_lexists = installer.os.path.lexists

    monkeypatch.setattr(
        installer.os.path,
        "lexists",
        lambda path: False if Path(path) == destination else real_lexists(path),
    )

    with pytest.raises(ConfigError, match="destination was recreated during install"):
        installer._move_no_replace(source, destination)

    assert (source / "payload").read_text(encoding="utf-8") == "source\n"
    assert (destination / "sentinel").read_text(encoding="utf-8") == "destination\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication contract")
def test_linux_dispatch_keeps_regular_file_hard_link_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("payload\n", encoding="utf-8")
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(
        installer,
        "_linux_move_no_replace",
        lambda *_args: pytest.fail("regular file used the Linux directory branch"),
    )

    installer._move_no_replace(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "payload\n"


class _RenameAt2:
    def __init__(self, observed_errno: int) -> None:
        self.observed_errno = observed_errno
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *_args: object) -> int:
        ctypes.set_errno(self.observed_errno)
        return -1


class _LibC:
    def __init__(self, observed_errno: int) -> None:
        self.renameat2 = _RenameAt2(observed_errno)


@pytest.mark.parametrize(
    ("observed_errno", "message"),
    [
        (errno.EEXIST, "destination was recreated during install"),
        (errno.ENOSYS, "no-replace placement is unavailable"),
        (errno.EINVAL, "no-replace placement is unavailable"),
        (errno.EXDEV, "no-replace placement failed"),
        (errno.EACCES, "no-replace placement failed"),
    ],
)
def test_linux_renameat2_errors_fail_closed_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_errno: int,
    message: str,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("overwrite-capable fallback was called")

    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: _LibC(observed_errno))
    monkeypatch.setattr(installer.os, "rename", forbidden)
    monkeypatch.setattr(installer.os, "replace", forbidden)

    with pytest.raises(ConfigError, match=message):
        _linux_move(source, destination)

    assert source.is_dir()
    assert not os.path.lexists(destination)


def test_linux_renameat2_missing_symbol_fails_closed_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("overwrite-capable fallback was called")

    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(installer.os, "rename", forbidden)
    monkeypatch.setattr(installer.os, "replace", forbidden)

    with pytest.raises(ConfigError, match="no-replace placement is unavailable"):
        _linux_move(source, destination)

    assert source.is_dir()
    assert not os.path.lexists(destination)
