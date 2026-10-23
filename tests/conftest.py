from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.engine_fixture import create_engine_repo


@pytest.fixture(scope="session")
def standalone_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    engine = create_engine_repo(tmp_path_factory.mktemp("standalone-engine") / "engine")
    yield engine
    status = subprocess.run(
        ["git", "-C", str(engine), "status", "--porcelain", "--untracked-files=all"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    assert not status.stdout, f"shared engine fixture is dirty:\n{status.stdout}"
