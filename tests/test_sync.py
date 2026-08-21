from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.doctor import run as run_doctor
from agent_core.sync import execute


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ac1"
CONFIG = Path(__file__).resolve().parent / "fixtures" / "config"


def concrete_environment(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    state = tmp_path / "state"
    shutil.copytree(FIXTURES / "state", state)
    shutil.copy2(FIXTURES / "manifests" / "valid-state.json", state / "manifest.yaml")
    targets = [tmp_path / "runtime-a", tmp_path / "runtime-b"]
    payload = json.loads((CONFIG / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backups")
    for target, root in zip(payload["targets"], targets, strict=True):
        target["root"] = str(root)
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return state, config, targets


def test_default_sync_is_dry_run() -> None:
    output = execute(ROOT, None, None, apply=False)
    assert output == [
        "PLAN target=claude-code runtime=claude-code",
        "PLAN target=codex runtime=codex",
        "DRY_RUN writes=0 targets=2",
    ]


def test_three_runtime_plan_lists_all_targets() -> None:
    output = execute(ROOT, CONFIG / "three-runtime.json", None, apply=False)
    assert len([line for line in output if line.startswith("PLAN ")]) == 3
    assert output[-1] == "DRY_RUN writes=0 targets=3"


def test_invalid_ledger_writes_nothing(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    sentinel_paths = []
    for index, root in enumerate(targets):
        path = root / "LESSONS.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"before-{index}", encoding="utf-8")
        sentinel_paths.append(path)
    ledger_path = state / "experience" / "LESSONS.md"
    ledger_path.write_text(ledger_path.read_text(encoding="utf-8").replace("lessons-scope: global", "lessons-scope: project"), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_LEDGER"):
        execute(ROOT, config, state, apply=True)
    assert [path.read_text(encoding="utf-8") for path in sentinel_paths] == ["before-0", "before-1"]
    assert not (tmp_path / "backups").exists()


def test_apply_backs_up_writes_and_verifies_content(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    for root in targets:
        root.mkdir(parents=True)
        (root / "LESSONS.md").write_text("old", encoding="utf-8")
    output = execute(ROOT, config, state, apply=True)
    assert any(line.startswith("BACKUP ") for line in output)
    assert output[-1] == "PASS backup_created=True"
    source = (state / "experience" / "LESSONS.md").read_bytes()
    for root in targets:
        assert (root / "LESSONS.md").read_bytes() == source
        for skill in ("dispatching-task-cards", "adversarial-audit", "first-divergence-debugging", "user-check"):
            assert (root / "skills" / skill / "SKILL.md").is_file()
    assert list((tmp_path / "backups").rglob("LESSONS.md"))


def test_identical_second_apply_writes_nothing(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    targets[0].mkdir()
    (targets[0] / "LESSONS.md").write_text("old", encoding="utf-8")
    first = execute(ROOT, config, state, apply=True)
    assert first[-1] == "PASS backup_created=True"

    def materialized_records() -> dict[str, tuple[bytes, int, int, int]]:
        records = {}
        for root in targets:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                stat = path.stat()
                records[str(path)] = (
                    path.read_bytes(), stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                )
        return records

    before = materialized_records()
    assert before
    output = execute(ROOT, config, state, apply=True)
    assert f"APPLIED writes=0 targets={len(targets)}" in output
    assert output[-1] == "PASS backup_created=False"
    assert materialized_records() == before


def test_prompt_injection_is_config_driven(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["prompt_injection"]["lines"] = ["Synthetic configured line."]
    config.write_text(json.dumps(payload), encoding="utf-8")
    execute(ROOT, config, state, apply=True)
    for root in targets:
        hook = (root / "hooks" / "user_prompt.sh").read_text(encoding="utf-8")
        assert "Synthetic configured line." in hook
        assert "Read matched lessons" not in hook
        assert "# agent-core-lessons-hook/1" in hook
        assert "lessons hook" in hook


def test_doctor_proves_required_skill_consumers(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    execute(ROOT, config, state, apply=True)
    lines = run_doctor(ROOT, config, state, state / "manifest.yaml")
    assert any("capability=skill:dispatching-task-cards" in line for line in lines)
    missing = targets[0] / "skills" / "dispatching-task-cards" / "SKILL.md"
    missing.unlink()
    with pytest.raises(ConfigError, match="FAIL_CONSUMER_MISSING"):
        run_doctor(ROOT, config, state, state / "manifest.yaml")
