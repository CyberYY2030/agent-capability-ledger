from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_core.materializer as materializer_module
import agent_core.promote as promote_module
from agent_core.config import ConfigError
from agent_core.materializer import (
    MaterializationTarget,
    materialize_bytes,
    publish_materialization_receipt,
    require_lock_token,
    review_materialization,
)
from agent_core.promote import operation_lock


def _materialize(
    target: Path,
    root: Path,
    transaction_root: Path,
    control_root: Path,
    token: object,
) -> bool:
    return materialize_bytes(
        target,
        root,
        None,
        b"desired\n",
        transaction_root,
        "target",
        control_root=control_root,
        lock_token=token,
    )


def test_materializer_requires_a_live_lock_token_bound_to_the_control_root(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "runtime"
    target = target_root / "LESSONS.md"
    transaction_root = tmp_path / "transaction"
    control = tmp_path / "host" / "txn"

    with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
        _materialize(target, target_root, transaction_root, control, None)

    with operation_lock(control) as token:
        with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
            _materialize(target, target_root, transaction_root, tmp_path / "other", token)
        assert _materialize(target, target_root, transaction_root, control, token) is True
        assert target.read_bytes() == b"desired\n"

    target.unlink()
    with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
        _materialize(target, target_root, transaction_root, control, token)
    assert not target.exists()


def test_lock_token_has_no_module_issuer_or_mutable_authority(
    tmp_path: Path,
) -> None:
    control_a = tmp_path / "host-a" / "txn"
    control_b = tmp_path / "host-b" / "txn"

    for module in (materializer_module, promote_module):
        assert not hasattr(module, "_issue_lock_token")
        assert not hasattr(module, "_expire_lock_token")
        assert not hasattr(module, "_make_lock_authority")
    assert not hasattr(materializer_module, "_HeldLockToken")

    with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
        require_lock_token(object(), control_a)

    with operation_lock(control_a) as token:
        with pytest.raises(AttributeError):
            token.control_root = control_b
        with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
            require_lock_token(token, control_b)
        require_lock_token(token, control_a)

    with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
        require_lock_token(token, control_a)


def test_same_source_partial_shrink_retains_reproves_and_reactivates(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    payload = {
        "targets": [{"id": "codex", "root": str(runtime), "skills_root": "skills"}],
    }
    config.write_text(json.dumps(payload), encoding="utf-8")
    targets = [
        MaterializationTarget(
            "codex", "skill:multi", runtime,
            runtime / "skills" / "multi" / "SKILL.md", b"skill\n",
        ),
        MaterializationTarget(
            "codex", "skill:multi", runtime,
            runtime / "skills" / "multi" / "references" / "detail.md", b"detail\n",
        ),
    ]
    for target in targets:
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_bytes(target.content)
    control = config.parent / "txn"
    initial = review_materialization(
        config, payload, state, targets, repository_root_sha=None,
        head=None, remote_revision=None,
    )
    with operation_lock(control) as token:
        publish_materialization_receipt(
            initial, tmp_path / "transaction-1", control_root=control, lock_token=token,
        )

    shrink = review_materialization(
        config, payload, state, targets[:1], repository_root_sha=None,
        head=None, remote_revision=None,
    )
    assert shrink.ready is True
    assert len(shrink.retained) == 1
    assert shrink.retained[0]["path"] == str(targets[1].path.resolve())
    assert shrink.retained[0]["status"] == "retained"
    with operation_lock(control) as token:
        publish_materialization_receipt(
            shrink, tmp_path / "transaction-2", control_root=control, lock_token=token,
        )

    targets[1].path.write_bytes(b"unknown-writer\n")
    drift = review_materialization(
        config, payload, state, targets[:1], repository_root_sha=None,
        head=None, remote_revision=None,
    )
    assert drift.ready is False
    assert drift.retained[0]["status"] == "retained-drift"

    targets[1].path.write_bytes(targets[1].content)
    reactivated = review_materialization(
        config, payload, state, targets, repository_root_sha=None,
        head=None, remote_revision=None,
    )
    assert reactivated.ready is True
    assert [item["status"] for item in reactivated.operations] == [
        "receipt-owned-identical", "receipt-owned-identical",
    ]


def test_removed_skill_redirected_retained_row_is_indeterminate(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    payload = {
        "targets": [{"id": "codex", "root": str(runtime), "skills_root": "skills"}],
    }
    config.write_text(json.dumps(payload), encoding="utf-8")
    target = MaterializationTarget(
        "codex", "skill:removed", runtime,
        runtime / "skills" / "removed" / "SKILL.md", b"skill\n",
    )
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(target.content)
    initial = review_materialization(
        config, payload, state, [target], repository_root_sha=None,
        head=None, remote_revision=None,
    )
    control = config.parent / "txn"
    with operation_lock(control) as token:
        publish_materialization_receipt(
            initial, tmp_path / "transaction", control_root=control, lock_token=token,
        )
    receipt = config.parent / "materialization-receipt.json"
    redirected = json.loads(receipt.read_text(encoding="utf-8"))
    redirected["rows"][0]["path"] = str((runtime / "redirected.md").resolve())
    receipt.write_text(json.dumps(redirected), encoding="utf-8")

    reviewed = review_materialization(
        config, payload, state, [], repository_root_sha=None,
        head=None, remote_revision=None,
    )

    assert reviewed.ready is False
    assert reviewed.receipt_error is not None
    assert "path identity mismatch" in reviewed.receipt_error
