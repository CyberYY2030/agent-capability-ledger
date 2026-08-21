from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent_core.fingerprint import canonical_json, generate, parity
from agent_core.sync import execute


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def environment(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    state = tmp_path / "state"
    shutil.copytree(FIXTURES / "ac1" / "state", state)
    shutil.copy2(FIXTURES / "ac1" / "manifests" / "valid-state.json", state / "manifest.yaml")
    targets = [tmp_path / "runtime-a", tmp_path / "runtime-b"]
    payload = json.loads((FIXTURES / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backups")
    for target, root in zip(payload["targets"], targets, strict=True):
        target["root"] = str(root)
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return state, config, targets


def test_fingerprint_is_deterministic_and_parity_detects_required_drift(tmp_path: Path) -> None:
    state, config, targets = environment(tmp_path)
    execute(ROOT, config, state, apply=True)
    first = generate(ROOT, config, state, state / "manifest.yaml")
    second = generate(ROOT, config, state, state / "manifest.yaml")
    assert canonical_json(first) == canonical_json(second)
    assert parity(first, second) == ("PASS", [])
    skill = targets[0] / "skills" / "dispatching-task-cards" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    drifted = generate(ROOT, config, state, state / "manifest.yaml")
    verdict, reasons = parity(first, drifted)
    assert verdict == "FAIL"
    assert any("required hash mismatch" in reason for reason in reasons)
