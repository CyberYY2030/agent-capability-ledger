from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.installer import apply_install, apply_uninstall, plan_uninstall
from tests.test_install import ROOT, installed_fixture


def test_uninstall_retains_materialized_runtime_and_removes_install_owned_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    runtime.mkdir(parents=True)
    rules = runtime / payload["targets"][0]["rules_target"]
    unrelated = runtime / "notes.txt"
    unrelated.write_bytes(b"not managed\n")
    apply_install(ROOT, config, state, ROOT, manifest, force=True)
    installed_rules = rules.read_bytes()
    materialization_receipt = config.parent / "materialization-receipt.json"
    materialization_receipt_before = materialization_receipt.read_bytes()
    receipt_path = config.parent / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    planned = plan_uninstall(config)
    assert any(line == f"RETAIN path={rules}" for line in planned)
    assert planned[-1] == "DRY_RUN writes=0"
    applied = apply_uninstall(config)
    assert any(line == f"RETAIN path={rules}" for line in applied)
    assert applied[-1] == "PASS uninstall"
    assert rules.read_bytes() == installed_rules
    assert materialization_receipt.read_bytes() == materialization_receipt_before
    assert unrelated.read_bytes() == b"not managed\n"
    assert not receipt_path.exists()
    assert not (install_root / "engine" / receipt["engine_version"]).exists()
    assert not (install_root / "engine-pin.json").exists()


def test_uninstall_conflict_preserves_user_change_and_every_other_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    receipt_path = config.parent / "install-receipt.json"
    receipt_before = receipt_path.read_bytes()
    wrapper = install_root / "bin" / "agent-core.cmd"
    wrapper.write_bytes(wrapper.read_bytes() + b"user-change\r\n")
    pin_before = (install_root / "engine-pin.json").read_bytes()
    with pytest.raises(ConfigError, match="UNINSTALL_CONFLICT"):
        apply_uninstall(config)
    assert wrapper.read_bytes().endswith(b"user-change\r\n")
    assert (install_root / "engine-pin.json").read_bytes() == pin_before
    assert receipt_path.read_bytes() == receipt_before


def test_uninstall_unknown_runtime_drift_is_retained_and_never_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    payload = json.loads(config.read_text(encoding="utf-8"))
    rules = Path(payload["targets"][0]["root"]) / payload["targets"][0]["rules_target"]
    drift = b"unknown-writer-after-install\n"
    rules.write_bytes(drift)
    materialization_receipt = config.parent / "materialization-receipt.json"
    receipt_before = materialization_receipt.read_bytes()

    result = apply_uninstall(config)

    assert result[-1] == "PASS uninstall"
    assert rules.read_bytes() == drift
    assert materialization_receipt.read_bytes() == receipt_before
    assert not (install_root / "engine-pin.json").exists()


def test_uninstall_validates_snapshot_before_changing_any_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    runtime.mkdir(parents=True)
    rules = runtime / payload["targets"][0]["rules_target"]
    apply_install(ROOT, config, state, ROOT, manifest, force=True)
    receipt = json.loads((config.parent / "install-receipt.json").read_text(encoding="utf-8"))
    snapshot_manifest_path = Path(receipt["snapshot_path"]) / "snapshot.json"
    snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
    snapshot_record = next(
        item for item in snapshot_manifest["objects"] if item["path"] == str(rules)
    )
    snapshot_record["installed_sha256"] = "f" * 64
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest), encoding="utf-8")
    wrapper = install_root / "bin" / "agent-core.cmd"
    wrapper_before = wrapper.read_bytes()
    rules_installed = rules.read_bytes()
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECEIPT"):
        apply_uninstall(config)
    assert rules.read_bytes() == rules_installed
    assert wrapper.read_bytes() == wrapper_before


def test_uninstall_rejects_receipt_root_outside_declared_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    receipt_path = config.parent / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["objects"][0]["root"] = str(tmp_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    snapshot_manifest_path = Path(receipt["snapshot_path"]) / "snapshot.json"
    snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
    snapshot_manifest["objects"][0]["root"] = str(tmp_path)
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest), encoding="utf-8")
    engine = install_root / "engine" / "0.1.0.dev9"
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECEIPT"):
        apply_uninstall(config)
    assert engine.is_dir()
