from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_core.config import ConfigError, compose_manifests, load_config


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ac1"
CONFIG = Path(__file__).resolve().parent / "fixtures" / "config"


def test_composition_is_deterministic() -> None:
    config = load_config(CONFIG / "two-runtime.json")
    first = compose_manifests(ROOT / "manifest.yaml", FIXTURES / "manifests" / "valid-state.json", config)
    second = compose_manifests(ROOT / "manifest.yaml", FIXTURES / "manifests" / "valid-state.json", config)
    assert first.composition_hash == second.composition_hash
    assert first.capabilities == second.capabilities


@pytest.mark.parametrize("fixture", ["conflict-id.json", "conflict-path.json"])
def test_manifest_conflicts_fail_closed(fixture: str) -> None:
    with pytest.raises(ConfigError, match="FAIL_MANIFEST_CONFLICT"):
        compose_manifests(ROOT / "manifest.yaml", FIXTURES / "manifests" / fixture)


def test_host_cannot_disable_required_capability(tmp_path: Path) -> None:
    payload = json.loads((CONFIG / "two-runtime.json").read_text(encoding="utf-8"))
    payload["capability_overrides"] = [{"id": "skill:dispatching-task-cards", "state": "disabled"}]
    path = tmp_path / "host.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_REQUIRED_DISABLED"):
        compose_manifests(ROOT / "manifest.yaml", None, load_config(path))


def test_host_may_disable_optional_capability(tmp_path: Path) -> None:
    payload = json.loads((CONFIG / "two-runtime.json").read_text(encoding="utf-8"))
    payload["capability_overrides"] = [{"id": "skill:user-check", "state": "disabled"}]
    path = tmp_path / "host.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    composition = compose_manifests(ROOT / "manifest.yaml", FIXTURES / "manifests" / "valid-state.json", load_config(path))
    selected = {item["id"]: item for item in composition.capabilities}
    assert selected["skill:user-check"]["state"] == "disabled"
