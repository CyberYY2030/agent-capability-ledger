"""Synthetic standalone state repositories for engine tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_core.state import _copy_state_seed


def build_full_state(engine_root: Path, target: Path) -> None:
    target.mkdir(mode=0o700)
    _copy_state_seed(engine_root, target)
    fixture = Path(__file__).resolve().parent / "fixtures" / "state-full"
    shutil.copytree(fixture, target, dirs_exist_ok=True)
    (target / "inbox").mkdir()


def create_state_repo(
    engine_root: Path, target: Path, *, git_name: str, git_email: str,
) -> None:
    target.mkdir(mode=0o700)
    _copy_state_seed(engine_root, target)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", f"safe.directory={target.resolve().as_posix()}", "-C", str(target), *args],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", git_name)
    git("config", "user.email", git_email)
    git("add", ".")
    git("commit", "-q", "-m", "state: initialize agent-core state")
