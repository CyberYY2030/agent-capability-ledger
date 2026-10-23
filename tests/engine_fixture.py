"""Standalone Git engine payloads for integration tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agent_core.provenance import EngineLayout, classify_engine_layout


def create_engine_repo(target: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source, target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git"),
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "."),
        ("-c", "user.name=Synthetic Engine", "-c", "user.email=engine@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "engine fixture"),
    ):
        subprocess.run(
            ["git", "-c", f"safe.directory={target.resolve().as_posix()}", "-C", str(target), *args],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
    assert classify_engine_layout(target) is EngineLayout.STANDALONE
    return target
