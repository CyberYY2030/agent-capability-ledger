from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core import migrate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "state-v1"


def state_copy(tmp_path: Path) -> Path:
    target = tmp_path / "state"
    shutil.copytree(FIXTURE, target)
    return target


def hashes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_plan_is_zero_write_and_apply_preserves_each_store_count(tmp_path: Path) -> None:
    state = state_copy(tmp_path)
    before = hashes(state)
    plan = migrate.plan_migration(state)
    lines = migrate.render_plan(plan)
    assert hashes(state) == before
    assert lines[-1] == "DRY_RUN writes=0 planned_writes=3"
    assert sum(line.startswith("SOURCE ") for line in lines) == 2

    result = migrate.apply_migration(state, tmp_path / "control", plan.plan_hash)
    assert result[0] == "APPLIED migration to=lessons-ledger/2 writes=3"
    for path in (state / "experience").rglob("LESSONS.md"):
        assert "lessons-schema: lessons-ledger/2" in path.read_text(encoding="utf-8")
    lock = json.loads((state / "agent-core.lock.json").read_text(encoding="utf-8"))
    assert lock["schema_version"] == "lessons-ledger/2"
    second = migrate.plan_migration(state)
    assert second.changed == ()
    assert migrate.render_plan(second)[-1] == "DRY_RUN writes=0 planned_writes=0"
    assert migrate.apply_migration(state, tmp_path / "control", second.plan_hash) == [
        "PASS migration to=lessons-ledger/2 no_changes=true"
    ]


def test_count_guard_rejects_a_corrupt_transform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_copy(tmp_path)
    original = migrate._transform_ledger

    def drop_one_rule(text: str, target: str) -> str:
        rendered = original(text, target)
        return "\n".join(
            line for line in rendered.splitlines() if "Keep the first rule" not in line
        ) + "\n"

    monkeypatch.setattr(migrate, "_transform_ledger", drop_one_rule)
    with pytest.raises(ConfigError, match="FAIL_MIGRATION_COUNTS"):
        migrate.plan_migration(state)


def test_stale_plan_and_backward_target_fail_before_writes(tmp_path: Path) -> None:
    state = state_copy(tmp_path)
    plan = migrate.plan_migration(state)
    ledger = state / "experience" / "LESSONS.md"
    ledger.write_text(ledger.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    before = hashes(state)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        migrate.apply_migration(state, tmp_path / "control", plan.plan_hash)
    assert hashes(state) == before
    with pytest.raises(ConfigError, match="FAIL_MIGRATION_DIRECTION"):
        migrate.plan_migration(state, "lessons-ledger/1")


def test_partial_write_failure_restores_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_copy(tmp_path)
    before = hashes(state)
    plan = migrate.plan_migration(state)
    real_write = migrate._atomic_write
    calls = 0

    def fail_second(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        real_write(path, content)

    monkeypatch.setattr(migrate, "_atomic_write", fail_second)
    with pytest.raises(ConfigError, match="FAIL_MIGRATION"):
        migrate.apply_migration(state, tmp_path / "control", plan.plan_hash)
    assert hashes(state) == before


def test_cli_defaults_to_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = state_copy(tmp_path)
    before = hashes(state)
    assert migrate.main(["--from", str(state), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "PLAN_HASH " in output and "DRY_RUN writes=0" in output
    assert hashes(state) == before
