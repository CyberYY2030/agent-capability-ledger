from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core import sync as sync_module
from agent_core.cli import main as cli_main
from agent_core.config import ConfigError, load_config
from agent_core.doctor import run as run_doctor
from agent_core.promote import operation_lock
from agent_core.sync import _plan_hash, _plan_payload, collect_operations, execute


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
    assert output[-1].startswith("DRY_RUN writes=0")
    assert not any(line.startswith(("BACKUP ", "APPLIED ")) for line in output)


def test_shipped_example_dry_run_is_explicitly_unbound() -> None:
    output = execute(ROOT, ROOT / "examples" / "host.example.json", None, apply=False)
    assert output == [
        "PLAN target=claude-code runtime=claude-code",
        "PLAN target=codex runtime=codex",
        "DRY_RUN writes=0 targets=2 planned_writes=unknown reason=state_unbound",
    ]
    assert not any(line.startswith(("PLAN_HASH ", "PLAN_OP ")) for line in output)


def test_three_runtime_plan_lists_all_targets() -> None:
    output = execute(ROOT, CONFIG / "three-runtime.json", None, apply=False)
    assert len([line for line in output if line.startswith("PLAN ")]) == 3
    assert output[-1] == (
        "DRY_RUN writes=0 targets=3 planned_writes=unknown reason=state_unbound"
    )


def _plan_hash_from(output: list[str]) -> str:
    return next(line.removeprefix("PLAN_HASH ") for line in output if line.startswith("PLAN_HASH "))


def _target_bytes(targets: list[Path]) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for target in targets
        if target.exists()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }


def reviewed_apply(
    config: Path,
    state: Path,
    *,
    require_versioned: bool = False,
) -> list[str]:
    token = _plan_hash_from(execute(ROOT, config, state, apply=False))
    return execute(
        ROOT, config, state, apply=True, require_versioned=require_versioned,
        plan_hash=token,
    )


def test_git_index_mode_and_generated_hooks_feed_executable_plan_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexed = tmp_path / "indexed"
    script = indexed / "scripts" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(indexed)], check=True)
    subprocess.run(["git", "-C", str(indexed), "add", "scripts/run.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(indexed), "update-index", "--chmod=+x", "scripts/run.sh"],
        check=True,
    )
    assert str(script.resolve()).casefold() in sync_module._git_executable_paths(indexed)

    state, config, _targets = concrete_environment(tmp_path)
    state_script = state / "skills" / "user-check" / "scripts" / "run.sh"
    state_script.parent.mkdir(parents=True)
    state_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_module, "_git_executable_paths",
        lambda root: {str(state_script.resolve()).casefold()}
        if root.resolve() == state.resolve() else set(),
    )
    payload = load_config(config)
    operations = collect_operations(ROOT, payload, state)
    script_operation = next(
        item for item in operations if item.destination.name == "run.sh"
    )
    assert script_operation.executable is True
    assert all(
        item.executable is True
        for item in operations if item.source_label in {"hook", "hook-windows"}
    )
    plan = _plan_payload(
        operations, payload, config_path=config, state_root=state,
        control_root=config.parent / "txn",
    )
    planned_script = next(
        item for item in plan["operations"] if item["destination"] == str(script_operation.destination.resolve())
    )
    assert planned_script["executable"] is True
    assert planned_script["before_executable"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode contract")
def test_sync_mode_only_upgrade_is_visible_transactional_and_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    state_script = state / "skills" / "user-check" / "scripts" / "run.sh"
    state_script.parent.mkdir(parents=True)
    state_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_module, "_git_executable_paths",
        lambda root: {str(state_script.resolve()).casefold()}
        if root.resolve() == state.resolve() else set(),
    )
    reviewed_apply(config, state)
    for root in targets:
        hook = root / "hooks" / "user_prompt.sh"
        assert os.access(hook, os.X_OK)

    destination = targets[0] / "skills" / "user-check" / "scripts" / "run.sh"
    before = destination.read_bytes()
    destination.chmod(0o644)
    receipt_path = config.parent / "materialization-receipt.json"
    legacy = json.loads(receipt_path.read_text(encoding="utf-8"))
    for row in legacy["rows"]:
        row.pop("executable")
    receipt_path.write_text(json.dumps(legacy), encoding="utf-8")
    receipt_before = receipt_path.read_bytes()

    planned = execute(ROOT, config, state, apply=False)
    mode_lines = [line for line in planned if line.startswith("PLAN_OP MODE ")]
    assert mode_lines == [
        "PLAN_OP MODE  target=claude-code path=skills/user-check/scripts/run.sh status=mode-drift",
    ]
    assert planned[-1] == "DRY_RUN writes=0 targets=2 planned_writes=1 ready=true"

    original_publish = sync_module.publish_materialization_receipt
    monkeypatch.setattr(
        sync_module, "publish_materialization_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError("FAIL_TEST", "receipt fault")),
    )
    with pytest.raises(ConfigError, match="FAIL_TEST"):
        execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(planned))
    assert destination.read_bytes() == before
    assert not os.access(destination, os.X_OK)
    assert receipt_path.read_bytes() == receipt_before

    monkeypatch.setattr(sync_module, "publish_materialization_receipt", original_publish)
    planned = execute(ROOT, config, state, apply=False)
    execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(planned))
    assert destination.read_bytes() == before
    assert os.access(destination, os.X_OK)
    assert execute(ROOT, config, state, apply=False)[-1] == (
        "DRY_RUN writes=0 targets=2 planned_writes=0 ready=true"
    )


def test_bound_dry_run_reports_all_operations_as_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    monkeypatch.setattr(
        "agent_core.sync.require_fresh",
        lambda *_args, **_kwargs: pytest.fail("dry-run called require_fresh"),
    )
    monkeypatch.setattr(
        "agent_core.sync.record_remote_head",
        lambda *_args, **_kwargs: pytest.fail("dry-run called record_remote_head"),
    )

    output = execute(ROOT, config, state, apply=False)
    operations = [line for line in output if line.startswith("PLAN_OP ")]
    assert operations
    assert all(line.startswith("PLAN_OP NOOP  ") for line in operations)
    assert output[-1] == "DRY_RUN writes=0 targets=2 planned_writes=0 ready=true"
    assert len([line for line in output if line.startswith("PLAN_HASH ")]) == 1


def test_bound_dry_run_reports_one_perturbed_file_as_foreign(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    (targets[0] / "LESSONS.md").write_text("perturbed", encoding="utf-8")

    output = execute(ROOT, config, state, apply=False)
    blocked = [line for line in output if line.startswith("PLAN_OP BLOCK ")]
    assert blocked == [
        "PLAN_OP BLOCK target=claude-code path=LESSONS.md status=foreign",
    ]
    assert not any(line.startswith("PLAN_HASH ") for line in output)
    assert output[-1] == "DRY_RUN writes=0 targets=2 planned_writes=0 ready=false"


def test_bound_dry_run_plan_hash_is_stable(tmp_path: Path) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)

    first = execute(ROOT, config, state, apply=False)
    second = execute(ROOT, config, state, apply=False)
    assert _plan_hash_from(first) == _plan_hash_from(second)


def test_plan_distinguishes_targets_sharing_a_relative_path(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    (targets[0] / "LESSONS.md").write_text("perturbed", encoding="utf-8")

    output = execute(ROOT, config, state, apply=False)
    lesson_lines = [line for line in output if "path=LESSONS.md " in line]
    assert lesson_lines == [
        "PLAN_OP BLOCK target=claude-code path=LESSONS.md status=foreign",
        "PLAN_OP NOOP  target=codex path=LESSONS.md status=receipt-owned-identical",
    ]
    payload = load_config(config)
    plan = _plan_payload(
        collect_operations(ROOT, payload, state), payload, config_path=config,
        state_root=state, control_root=config.parent / "txn",
    )
    lessons = [item for item in plan["operations"] if item["relative_path"] == "LESSONS.md"]
    assert lessons[0] != lessons[1]
    assert set(lessons[0]) == {
        "target_id", "source_label", "destination", "relative_path", "before_exists",
        "before_sha256", "before_executable", "after_sha256", "executable",
        "status", "action",
    }
    assert [(item["target_id"], item["action"]) for item in lessons] == [
        ("claude-code", "BLOCK"),
        ("codex", "NOOP"),
    ]


def test_plan_hash_rejects_before_state_drift_without_writes(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    planned = _plan_hash_from(execute(ROOT, config, state, apply=False))
    (targets[0] / "LESSONS.md").write_text("changed after preview", encoding="utf-8")
    before = _target_bytes(targets)

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True, plan_hash=planned)
    assert _target_bytes(targets) == before


def test_plan_hash_rejects_raw_config_drift_before_backup_or_runtime_write(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    planned = _plan_hash_from(execute(ROOT, config, state, apply=False))
    config.write_bytes(config.read_bytes() + b"\n")

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True, plan_hash=planned)
    assert _target_bytes(targets) == {}
    assert not (tmp_path / "backups").exists()


def test_plan_hash_rejects_target_root_drift_before_backup_or_runtime_write(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    planned = _plan_hash_from(execute(ROOT, config, state, apply=False))
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["targets"][0]["root"] = str(tmp_path / "different-runtime")
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True, plan_hash=planned)
    assert _target_bytes(targets) == {}
    assert not (tmp_path / "different-runtime").exists()
    assert not (tmp_path / "backups").exists()


def test_install_held_lock_blocks_sync_before_backup_or_runtime_write(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    planned = _plan_hash_from(execute(ROOT, config, state, apply=False))
    with operation_lock(config.parent / "txn"):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            execute(ROOT, config, state, apply=True, plan_hash=planned)
    assert _target_bytes(targets) == {}
    assert not (tmp_path / "backups").exists()


def test_stale_plan_hash_writes_nothing(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    before = _target_bytes(targets)

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True, plan_hash="0" * 64)
    assert _target_bytes(targets) == before
    assert not (tmp_path / "backups").exists()


def test_sync_apply_without_a_reviewed_hash_writes_nothing(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True)
    assert _target_bytes(targets) == {}
    assert not (tmp_path / "backups").exists()


def test_sync_plan_contract_binds_config_repository_targets_and_absolute_operations(
    tmp_path: Path,
) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    payload = load_config(config)
    plan = _plan_payload(
        collect_operations(ROOT, payload, state), payload, config_path=config,
        state_root=state, control_root=config.parent / "txn",
    )
    assert Path(plan["config"]["path"]).is_absolute()
    assert len(plan["config"]["sha256"]) == 64
    assert Path(plan["repository"]["state_root"]).is_absolute()
    assert all(Path(target["root"]).is_absolute() for target in plan["targets"])
    assert all(Path(item["destination"]).is_absolute() for item in plan["operations"])
    assert plan["ownership_receipt"] == {"exists": False, "sha256": None}

    plan["config"].pop("sha256")
    with pytest.raises(ConfigError, match="FAIL_PLAN_CONTRACT"):
        _plan_hash(plan)


def test_plan_hash_without_apply_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _state, config, targets = concrete_environment(tmp_path)

    assert cli_main(["sync", "--config", str(config), "--plan-hash", "0" * 64]) == 1
    assert "FAIL_PLAN_HASH" in capsys.readouterr().err
    assert _target_bytes(targets) == {}


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
        execute(ROOT, config, state, apply=False)
    assert [path.read_text(encoding="utf-8") for path in sentinel_paths] == ["before-0", "before-1"]
    assert not (tmp_path / "backups").exists()


def test_apply_backs_up_writes_and_verifies_content(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    lessons = state / "experience" / "LESSONS.md"
    lessons.write_bytes(lessons.read_bytes() + b"\n")
    output = reviewed_apply(config, state)
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
    first = reviewed_apply(config, state)
    assert first[-1] == "PASS backup_created=False"

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
    output = reviewed_apply(config, state)
    assert f"APPLIED writes=0 targets={len(targets)}" in output
    assert output[-1] == "PASS backup_created=False"
    assert materialized_records() == before


def test_sync_publishes_materialization_receipt_and_uses_it_for_noop(
    tmp_path: Path,
) -> None:
    state, config, targets = concrete_environment(tmp_path)

    first = reviewed_apply(config, state)

    receipt_path = config.parent / "materialization-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {
        "schema", "config", "state", "generation", "transaction_id", "rows",
    }
    assert receipt["schema"] == "materialization-receipt/1"
    assert receipt["config"] == {
        "path": str(config.resolve()),
        "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    }
    assert receipt["generation"] == 1
    assert len(receipt["transaction_id"]) == 64
    assert receipt["rows"]
    assert set(receipt["state"]) == {
        "root", "repository_root_sha", "head", "remote_revision",
    }
    assert all(set(row) == {
        "target_id", "source_label", "root", "path", "kind",
        "installed_sha256", "executable", "status",
    } for row in receipt["rows"])
    assert {row["status"] for row in receipt["rows"]} == {"active"}
    assert first[-1] == "PASS backup_created=False"

    second = execute(ROOT, config, state, apply=False)
    operation_lines = [line for line in second if line.startswith("PLAN_OP ")]
    assert operation_lines
    assert all("status=receipt-owned-identical" in line for line in operation_lines)
    assert second[-1] == "DRY_RUN writes=0 targets=2 planned_writes=0 ready=true"


def test_sync_exact_unowned_bytes_are_visibly_adopted_without_runtime_backup(
    tmp_path: Path,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    payload = load_config(config)
    operations = collect_operations(ROOT, payload, state)
    for operation in operations:
        operation.destination.parent.mkdir(parents=True, exist_ok=True)
        operation.destination.write_bytes(operation.content)

    planned = execute(ROOT, config, state, apply=False)
    operation_lines = [line for line in planned if line.startswith("PLAN_OP ")]
    assert operation_lines
    assert all("status=adopt-identical" in line for line in operation_lines)
    token = _plan_hash_from(planned)
    applied = execute(ROOT, config, state, apply=True, plan_hash=token)

    assert applied[-1] == "PASS backup_created=False"
    assert not (tmp_path / "backups").exists()
    assert (config.parent / "materialization-receipt.json").is_file()
    assert _target_bytes(targets)


def test_sync_path_set_shrink_retains_reactivates_and_blocks_retained_drift(
    tmp_path: Path,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    state_manifest = state / "manifest.yaml"
    payload = json.loads(state_manifest.read_text(encoding="utf-8"))
    original_capabilities = payload["capabilities"]
    payload["capabilities"] = []
    state_manifest.write_text(json.dumps(payload), encoding="utf-8")

    shrink = execute(ROOT, config, state, apply=False)
    retained_lines = [line for line in shrink if line.startswith("RETAIN ")]
    assert len(retained_lines) == 2
    assert all("status=retained" in line and "user-check" in line for line in retained_lines)
    shrink_result = execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(shrink))
    assert shrink_result[-1] == "PASS backup_created=False"

    payload["capabilities"] = original_capabilities
    state_manifest.write_text(json.dumps(payload), encoding="utf-8")
    reactivated = execute(ROOT, config, state, apply=False)
    user_check = [line for line in reactivated if "skills/user-check/" in line]
    assert user_check and all("status=receipt-owned-identical" in line for line in user_check)
    reviewed = _plan_hash_from(reactivated)
    execute(ROOT, config, state, apply=True, plan_hash=reviewed)

    payload["capabilities"] = []
    state_manifest.write_text(json.dumps(payload), encoding="utf-8")
    shrink_again = execute(ROOT, config, state, apply=False)
    execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(shrink_again))
    retained_path = targets[0] / "skills" / "user-check" / "SKILL.md"
    retained_path.write_bytes(b"unknown-writer\n")
    blocked = execute(ROOT, config, state, apply=False)
    assert not any(line.startswith("PLAN_HASH ") for line in blocked)
    assert any(
        line.startswith("RETAIN ") and "status=retained-drift" in line
        for line in blocked
    )
    assert blocked[-1].endswith("ready=false")


def test_sync_corrupt_receipt_is_indeterminate_and_never_ignored(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    before = _target_bytes(targets)
    receipt = config.parent / "materialization-receipt.json"
    receipt.write_text("{}", encoding="utf-8")

    planned = execute(ROOT, config, state, apply=False)

    assert not any(line.startswith("PLAN_HASH ") for line in planned)
    assert all(
        "status=indeterminate" in line
        for line in planned if line.startswith("PLAN_OP ")
    )
    assert planned[-1].endswith("ready=false")
    assert receipt.read_text(encoding="utf-8") == "{}"
    assert _target_bytes(targets) == before


@pytest.mark.parametrize("redirect", ("config", "root", "path"))
def test_sync_redirected_receipt_identity_is_indeterminate(
    tmp_path: Path, redirect: str,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    receipt_path = config.parent / "materialization-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if redirect == "config":
        receipt["config"]["path"] = str((tmp_path / "other-host.json").resolve())
    elif redirect == "root":
        receipt["rows"][0]["root"] = str((tmp_path / "other-runtime").resolve())
    else:
        receipt["rows"][0]["path"] = str(
            (Path(receipt["rows"][0]["root"]) / "redirected.md").resolve()
        )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _target_bytes(targets)

    planned = execute(ROOT, config, state, apply=False)

    assert not any(line.startswith("PLAN_HASH ") for line in planned)
    assert all(
        "status=indeterminate" in line
        for line in planned if line.startswith("PLAN_OP ")
    )
    assert planned[-1].endswith("ready=false")
    assert _target_bytes(targets) == before


def test_sync_receipt_preimage_drift_rejects_before_runtime_write(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    planned = execute(ROOT, config, state, apply=False)
    token = _plan_hash_from(planned)
    receipt = config.parent / "materialization-receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    before = _target_bytes(targets)

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        execute(ROOT, config, state, apply=True, plan_hash=token)
    assert _target_bytes(targets) == before
    assert not (config.parent / "txn" / "materialization-pending.json").exists()


def test_sync_unknown_writer_matching_new_desired_is_foreign_and_zero_write(
    tmp_path: Path,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    receipt = config.parent / "materialization-receipt.json"
    receipt_before = receipt.read_bytes()
    global_rules = state / "rules" / "global.md"
    global_rules.write_bytes(global_rules.read_bytes() + b"\nunknown-writer-generation-b\n")
    payload = load_config(config)
    changed = next(
        item for item in collect_operations(ROOT, payload, state)
        if item.target_id == payload["targets"][0]["id"] and item.source_label == "rules"
    )
    changed.destination.write_bytes(changed.content)
    runtime_before = {
        path: path.read_bytes()
        for root in targets for path in root.rglob("*") if path.is_file()
    }

    planned = execute(ROOT, config, state, apply=False)

    changed_line = next(
        line for line in planned
        if line.startswith("PLAN_OP ")
        and f"target={changed.target_id}" in line
        and f"path={changed.destination.relative_to(targets[0]).as_posix()}" in line
    )
    assert "status=foreign" in changed_line
    assert not any(line.startswith("PLAN_HASH ") for line in planned)
    assert planned[-1].endswith("ready=false")
    assert receipt.read_bytes() == receipt_before
    assert {path: path.read_bytes() for path in runtime_before} == runtime_before


@pytest.mark.parametrize("fault", ("runtime", "receipt", "cleanup"))
def test_sync_transaction_faults_restore_runtime_receipt_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    planned = execute(ROOT, config, state, apply=False)
    token = _plan_hash_from(planned)
    original_materialize = sync_module.materialize_bytes
    original_clear = sync_module._clear_materialization_transaction
    calls = 0

    if fault == "runtime":
        def fail_runtime(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ConfigError("FAIL_TEST", "runtime fault")
            return original_materialize(*args, **kwargs)
        monkeypatch.setattr(sync_module, "materialize_bytes", fail_runtime)
    elif fault == "receipt":
        monkeypatch.setattr(
            sync_module, "publish_materialization_receipt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError("FAIL_TEST", "receipt fault")),
        )
    else:
        def fail_cleanup_once(config_path: Path, snapshot: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("cleanup fault")
            original_clear(config_path, snapshot)
        monkeypatch.setattr(sync_module, "_clear_materialization_transaction", fail_cleanup_once)

    with pytest.raises(ConfigError):
        execute(ROOT, config, state, apply=True, plan_hash=token)

    assert _target_bytes(targets) == {}
    assert not (config.parent / "materialization-receipt.json").exists()
    assert not (config.parent / "txn" / "materialization-pending.json").exists()


def test_sync_partial_snapshot_cleanup_keeps_marker_and_blocks_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    rules = state / "rules" / "global.md"
    rules.write_bytes(rules.read_bytes() + b"\npartial-cleanup-generation\n")
    planned = execute(ROOT, config, state, apply=False)
    original_rmtree = sync_module.shutil.rmtree
    injected = False

    def partial_cleanup(path, *args, **kwargs):
        nonlocal injected
        snapshot = Path(path)
        if not injected and snapshot.name.startswith("materialization-"):
            victim = next(item for item in (snapshot / "runtime").rglob("*") if item.is_file())
            victim.unlink()
            injected = True
            raise OSError("partial snapshot cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(sync_module.shutil, "rmtree", partial_cleanup)
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_ROLLBACK"):
        execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(planned))

    assert injected is True
    pending = config.parent / "txn" / "materialization-pending.json"
    assert pending.is_file()
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECOVERY"):
        execute(ROOT, config, state, apply=False)


def test_sync_pending_unlink_failure_keeps_marker_and_blocks_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    rules = state / "rules" / "global.md"
    rules.write_bytes(rules.read_bytes() + b"\npending-unlink-generation\n")
    planned = execute(ROOT, config, state, apply=False)
    pending = config.parent / "txn" / "materialization-pending.json"
    original_unlink = Path.unlink

    def fail_pending_unlink(path: Path, *args, **kwargs):
        if path.resolve() == pending.resolve():
            raise OSError("pending unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_unlink)
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_ROLLBACK"):
        execute(ROOT, config, state, apply=True, plan_hash=_plan_hash_from(planned))

    assert pending.is_file()
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECOVERY"):
        execute(ROOT, config, state, apply=False)


def test_sync_receipt_race_preserves_racer_snapshot_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, _targets = concrete_environment(tmp_path)
    planned = execute(ROOT, config, state, apply=False)
    token = _plan_hash_from(planned)
    racer = b'{"racer":true}\n'

    def race(review, *_args, **_kwargs):
        review.receipt_path.write_bytes(racer)
        raise ConfigError("FAIL_MATERIALIZER_RACE", "synthetic receipt race")

    monkeypatch.setattr(sync_module, "publish_materialization_receipt", race)
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZER_RACE") as caught:
        execute(ROOT, config, state, apply=True, plan_hash=token)

    assert (config.parent / "materialization-receipt.json").read_bytes() == racer
    pending = config.parent / "txn" / "materialization-pending.json"
    assert pending.is_file()
    assert "snapshot retained" in str(caught.value)


def test_sync_refuses_install_pending_before_plan_or_runtime_write(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    pending = config.parent / "install-pending.json"
    pending.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY"):
        execute(ROOT, config, state, apply=False)

    assert _target_bytes(targets) == {}
    assert pending.read_text(encoding="utf-8") == "{}"


def test_prompt_injection_is_config_driven(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["prompt_injection"]["lines"] = ["Synthetic configured line."]
    config.write_text(json.dumps(payload), encoding="utf-8")
    reviewed_apply(config, state)
    for root in targets:
        hook = (root / "hooks" / "user_prompt.sh").read_text(encoding="utf-8")
        assert "Synthetic configured line." in hook
        assert "Read matched lessons" not in hook
        assert "# agent-core-lessons-hook/1" in hook
        assert "lessons hook" in hook


def test_doctor_proves_required_skill_consumers(tmp_path: Path) -> None:
    state, config, targets = concrete_environment(tmp_path)
    reviewed_apply(config, state)
    lines = run_doctor(ROOT, config, state, state / "manifest.yaml")
    assert any("capability=skill:dispatching-task-cards" in line for line in lines)
    missing = targets[0] / "skills" / "dispatching-task-cards" / "SKILL.md"
    missing.unlink()
    with pytest.raises(ConfigError, match="FAIL_CONSUMER_MISSING"):
        run_doctor(ROOT, config, state, state / "manifest.yaml")
