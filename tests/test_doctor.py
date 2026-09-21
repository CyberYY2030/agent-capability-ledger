from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import __version__
from agent_core.config import ConfigError
from agent_core import doctor as doctor_module
from agent_core.cli import main as cli_main
from agent_core.installer import build_release_manifest
from agent_core.provenance import EngineLayout
from agent_core.materializer import (
    MaterializationTarget,
    doctor_materialization_receipt,
    materialization_pending_path,
    publish_materialization_receipt,
    review_materialization,
)
from agent_core.promote import operation_lock
from agent_core.doctor import (
    assert_repository_separation,
    assert_remote_role,
    hook_retrieval_status,
    run,
)
from agent_core.privacy import DEFAULT_MAX_BLOB_BYTES


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def repo_with_remote(tmp_path: Path, url: str, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", url)
    return repo


@pytest.mark.parametrize(
    ("status", "result_nonempty", "hash_matches", "expected"),
    (
        ("pass", False, True, ("PASS", "retrieval_empty stage=prompt")),
        ("warning", False, True, ("WARN", "retrieval_warning stage=prompt")),
        ("pass", False, False, ("WARN", "retrieval_connected_current_version_unobserved")),
    ),
)
def test_hook_health_uses_status_and_keeps_empty_result_detail(
    tmp_path: Path, status: str, result_nonempty: bool,
    hash_matches: bool, expected: tuple[str, str],
) -> None:
    hook = tmp_path / "hooks" / "user_prompt.sh"
    hook.parent.mkdir()
    hook.write_text(
        "#!/bin/sh\n# agent-core-lessons-hook/1\nagent-core lessons hook\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        hook.chmod(0o755)
    heartbeat = hook.parent / ".lessons-hook-heartbeat.json"
    payload = {
        "schema": "lessons-hook-heartbeat/1",
        "runtime": "codex",
        "stage": "prompt",
        "status": status,
        "retrieval_invoked": True,
        "result_nonempty": result_nonempty,
        "validation_ran": status == "pass",
        "source_mtime_sha256": "1" * 64 if status == "pass" else None,
        "hook_sha256": (
            hashlib.sha256(hook.read_bytes()).hexdigest() if hash_matches else "0" * 64
        ),
        "observed_utc": "2026-09-04T00:00:00Z",
    }
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")
    assert hook_retrieval_status(hook) == expected


def installed_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    install_root = tmp_path / "agent-core"
    config = tmp_path / "host" / "host.json"
    version = __version__
    engine = install_root / "engine" / version
    (engine / "agent_core").mkdir(parents=True)
    (engine / "agent_core" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = build_release_manifest(engine)
    (engine / "release-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    pin = {
        "schema": "engine-pin/1",
        "version": version,
        "artifact_sha256": manifest["artifact_sha256"],
        "config_path": str(config.resolve()),
    }
    pin_path = install_root / "engine-pin.json"
    pin_path.write_text(json.dumps(pin, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return engine, config, pin_path, pin


def materialization_fixture(
    tmp_path: Path,
) -> tuple[Path, dict, Path, list[MaterializationTarget]]:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    payload = {"targets": [{
        "id": "codex", "root": str(runtime),
        "rules_target": "AGENTS.md", "lessons_target": "LESSONS.md",
    }]}
    config.write_text(json.dumps(payload), encoding="utf-8")
    targets = [
        MaterializationTarget("codex", "rules", runtime, runtime / "AGENTS.md", b"rules\n"),
        MaterializationTarget("codex", "lessons", runtime, runtime / "LESSONS.md", b"lessons\n"),
    ]
    for target in targets:
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_bytes(target.content)
    review = review_materialization(
        config, payload, state, targets, repository_root_sha=None,
        head=None, remote_revision=None,
    )
    control = config.parent / "txn"
    with operation_lock(control) as token:
        publish_materialization_receipt(
            review, tmp_path / "transaction", control_root=control, lock_token=token,
        )
    return config, payload, state, targets


def test_doctor_materialization_receipt_healthy_missing_corrupt_and_drift(
    tmp_path: Path,
) -> None:
    config, payload, state, targets = materialization_fixture(tmp_path)
    healthy = doctor_materialization_receipt(config, payload, state, targets)
    assert healthy[0].startswith("PASS materialization_receipt schema=materialization-receipt/1 ")

    targets[0].path.write_bytes(b"unknown-writer\n")
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_DRIFT"):
        doctor_materialization_receipt(config, payload, state, targets)
    targets[0].path.write_bytes(targets[0].content)

    receipt = config.parent / "materialization-receipt.json"
    raw = receipt.read_bytes()
    receipt.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECEIPT"):
        doctor_materialization_receipt(config, payload, state, targets)
    receipt.write_bytes(raw)
    receipt.unlink()
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECEIPT"):
        doctor_materialization_receipt(config, payload, state, targets)


def test_doctor_rejects_config_bytes_and_reproves_canonical_lineage(tmp_path: Path) -> None:
    fixture_root = tmp_path / "config-drift"
    fixture_root.mkdir()
    config, payload, state, targets = materialization_fixture(fixture_root)
    receipt = config.parent / "materialization-receipt.json"
    receipt_before = receipt.read_bytes()
    runtime_before = {target.path: target.path.read_bytes() for target in targets}
    config.write_bytes(config.read_bytes() + b"\n")

    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECEIPT"):
        doctor_materialization_receipt(config, payload, state, targets)
    assert receipt.read_bytes() == receipt_before
    assert {path: path.read_bytes() for path in runtime_before} == runtime_before
    control = config.parent / "txn"
    semantic_noop = review_materialization(
        config, payload, state, targets, repository_root_sha=None,
        head=None, remote_revision=None,
    )
    assert semantic_noop.ready is True
    assert semantic_noop.receipt_changed is True
    with operation_lock(control) as token:
        publish_materialization_receipt(
            semantic_noop, fixture_root / "transaction-semantic-noop",
            control_root=control, lock_token=token,
        )
    assert doctor_materialization_receipt(config, payload, state, targets)[0].startswith(
        "PASS materialization_receipt"
    )

    payload["targets"][0]["runtime"] = "codex"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECEIPT"):
        doctor_materialization_receipt(config, payload, state, targets)
    legal_change = review_materialization(
        config, payload, state, targets, repository_root_sha=None,
        head=None, remote_revision=None,
    )
    assert legal_change.ready is True
    assert legal_change.receipt_changed is True
    with operation_lock(control) as token:
        publish_materialization_receipt(
            legal_change, fixture_root / "transaction-legal-change",
            control_root=control, lock_token=token,
        )
    assert doctor_materialization_receipt(config, payload, state, targets)[0].startswith(
        "PASS materialization_receipt"
    )

    repository = tmp_path / "canonical"
    canonical_state = repository / "state"
    (repository / "engine").mkdir(parents=True)
    canonical_state.mkdir()
    (repository / "engine" / "marker.txt").write_text("engine\n", encoding="utf-8")
    (canonical_state / "marker.txt").write_text("state\n", encoding="utf-8")
    git(repository, "init", "-q")
    git(repository, "add", "engine/marker.txt", "state/marker.txt")
    git(
        repository, "-c", "user.name=G19b Test", "-c", "user.email=g19b@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial",
    )
    root_sha = subprocess.run(
        [
            "git", "-c", f"safe.directory={repository.resolve().as_posix()}",
            "-C", str(repository), "rev-list", "--max-parents=0", "HEAD",
        ],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    runtime = tmp_path / "canonical-runtime"
    canonical_config = tmp_path / "canonical-host" / "host.json"
    canonical_config.parent.mkdir()
    canonical_payload = {"targets": [{
        "id": "codex", "root": str(runtime), "rules_target": "AGENTS.md",
    }]}
    canonical_config.write_text(json.dumps(canonical_payload), encoding="utf-8")
    canonical_targets = [
        MaterializationTarget("codex", "rules", runtime, runtime / "AGENTS.md", b"rules\n"),
    ]
    canonical_targets[0].path.parent.mkdir(parents=True)
    canonical_targets[0].path.write_bytes(canonical_targets[0].content)
    review = review_materialization(
        canonical_config, canonical_payload, canonical_state, canonical_targets,
        repository_root_sha=root_sha, head=root_sha, remote_revision=None,
    )
    control = canonical_config.parent / "txn"
    with operation_lock(control) as token:
        publish_materialization_receipt(
            review, tmp_path / "canonical-transaction",
            control_root=control, lock_token=token,
        )
    assert doctor_materialization_receipt(
        canonical_config, canonical_payload, canonical_state, canonical_targets,
    )[0].startswith("PASS materialization_receipt")

    canonical_receipt = canonical_config.parent / "materialization-receipt.json"
    changed = json.loads(canonical_receipt.read_text(encoding="utf-8"))
    changed["state"]["repository_root_sha"] = "0" * 40
    canonical_receipt.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECEIPT"):
        doctor_materialization_receipt(
            canonical_config, canonical_payload, canonical_state, canonical_targets,
        )


def test_doctor_materialization_pending_and_retained_are_visible(tmp_path: Path) -> None:
    config, payload, state, targets = materialization_fixture(tmp_path)
    control = config.parent / "txn"
    review = review_materialization(
        config, payload, state, targets[:1], repository_root_sha=None,
        head=None, remote_revision=None,
    )
    with operation_lock(control) as token:
        publish_materialization_receipt(
            review, tmp_path / "transaction-2", control_root=control, lock_token=token,
        )
    lines = doctor_materialization_receipt(config, payload, state, targets[:1])
    assert any(line == f"RETAIN materialization_receipt path={targets[1].path}" for line in lines)

    pending = materialization_pending_path(config)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECOVERY"):
        doctor_materialization_receipt(config, payload, state, targets[:1])


def test_doctor_rejects_install_pending_without_writes(tmp_path: Path) -> None:
    config, payload, state, targets = materialization_fixture(tmp_path)
    receipt = config.parent / "materialization-receipt.json"
    receipt_before = receipt.read_bytes()
    runtime_before = {target.path: target.path.read_bytes() for target in targets}
    pending = config.parent / "install-pending.json"
    pending.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY"):
        doctor_materialization_receipt(config, payload, state, targets)

    assert pending.read_text(encoding="utf-8") == "{}"
    assert receipt.read_bytes() == receipt_before
    assert {path: path.read_bytes() for path in runtime_before} == runtime_before


@pytest.mark.parametrize(
    ("marker_kind", "entrypoint", "expected"),
    (
        ("install", "run", "FAIL_INSTALL_RECOVERY"),
        ("materialization", "cli", "FAIL_MATERIALIZATION_RECOVERY"),
    ),
)
def test_doctor_top_level_pending_precedes_consumers_and_preserves_remote_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    marker_kind: str, entrypoint: str, expected: str,
) -> None:
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    config.write_text("{}\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    runtime = tmp_path / "runtime"
    payload = {"targets": [{
        "id": "codex", "runtime": "codex", "root": str(runtime),
        "skills_root": "skills", "hook_target": "hooks/user_prompt.sh",
    }]}
    monkeypatch.setattr(doctor_module, "load_config", lambda _path: payload)

    def masked(*_args, **_kwargs):
        raise AssertionError("pending guard must precede composition and consumers")

    monkeypatch.setattr(doctor_module, "compose_manifests", masked)
    txn = config.parent / "txn"
    txn.mkdir()
    remote = txn / "remote-state.json"
    remote.write_bytes(b'{"baseline":"preserve"}\n')
    marker = (
        config.parent / "install-pending.json"
        if marker_kind == "install"
        else txn / "materialization-pending.json"
    )
    marker.write_text("{}", encoding="utf-8")

    if entrypoint == "run":
        with pytest.raises(ConfigError, match=expected):
            run(tmp_path / "engine", config, state, state / "manifest.yaml")
    else:
        assert cli_main([
            "doctor", "--config", str(config), "--state", str(state),
            "--state-manifest", str(state / "manifest.yaml"),
        ]) == 1
        assert expected in capsys.readouterr().err
    assert marker.read_text(encoding="utf-8") == "{}"
    assert remote.read_bytes() == b'{"baseline":"preserve"}\n'


@pytest.mark.parametrize(
    ("config_state", "marker_kind", "entrypoint", "expected"),
    (
        ("missing", "install", "run", "FAIL_INSTALL_RECOVERY"),
        ("invalid", "materialization", "cli", "FAIL_MATERIALIZATION_RECOVERY"),
    ),
)
def test_doctor_pending_precedes_missing_or_invalid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_state: str,
    marker_kind: str, entrypoint: str, expected: str,
) -> None:
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    if config_state == "invalid":
        config.write_text("{invalid", encoding="utf-8")
    txn = config.parent / "txn"
    txn.mkdir()
    marker = (
        config.parent / "install-pending.json"
        if marker_kind == "install"
        else txn / "materialization-pending.json"
    )
    marker.write_text("{}", encoding="utf-8")

    if entrypoint == "run":
        with pytest.raises(ConfigError, match=expected):
            run(tmp_path / "engine", config, None, None)
    else:
        assert cli_main(["doctor", "--config", str(config)]) == 1
        assert expected in capsys.readouterr().err
    assert marker.read_text(encoding="utf-8") == "{}"


def test_remote_role_rejects_matching_hosted_identity_without_leaking_url(tmp_path: Path) -> None:
    token = "synthetic-token"
    engine_url = "https://automation:" + token + chr(64) + "github.com/frozen-owner/capability-ledger.git"
    state_url = "git" + chr(64) + "github.com:frozen-owner/capability-ledger.git"
    engine = repo_with_remote(tmp_path, engine_url, "engine")
    state = repo_with_remote(tmp_path, state_url, "state")

    with pytest.raises(ConfigError) as caught:
        assert_remote_role(engine, state)

    assert caught.value.code == "FAIL_ENGINE_STATE_REMOTE_OVERLAP"
    message = str(caught.value)
    assert "(github.com, frozen-owner, capability-ledger)" in message
    assert token not in message
    assert engine_url not in message
    assert state_url not in message


def test_remote_role_allows_different_owners_with_same_repository_name(tmp_path: Path) -> None:
    engine = repo_with_remote(
        tmp_path, "https://github.com/engine-owner/capability-ledger.git", "engine",
    )
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == [
        "PASS remote_role=verified "
        "engine=(github.com, engine-owner, capability-ledger) "
        "state=(github.com, state-owner, capability-ledger)",
    ]


def test_remote_role_marks_missing_engine_remote_unverified_or_red(tmp_path: Path) -> None:
    engine = repo_with_remote(tmp_path, "https://github.com/unused/unused.git", "engine")
    git(engine, "remote", "remove", "origin")
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == ["UNVERIFIED remote_role=engine_remote_missing"]
    with pytest.raises(ConfigError, match="FAIL_ENGINE_REMOTE_UNVERIFIED"):
        assert_remote_role(engine, state, require_versioned=True)


def test_remote_role_marks_non_repository_engine_unverified_or_red(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == ["UNVERIFIED remote_role=engine_not_repository"]
    with pytest.raises(ConfigError, match="FAIL_ENGINE_REPOSITORY"):
        assert_remote_role(engine, state, require_versioned=True)


def test_doctor_rejects_engine_implementation_in_state(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    implementation = state / "agent_core" / "cli.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_CONTAINS_ENGINE"):
        run(engine, tmp_path / "missing-host.json", state, None)


def test_doctor_rejects_nested_engine_implementation_in_state(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    implementation = state / "agent-core" / "agent_core" / "cli.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_CONTAINS_ENGINE") as caught:
        run(engine, tmp_path / "missing-host.json", state, None)
    assert "agent-core/agent_core/cli.py" in str(caught.value).replace("\\", "/")


def test_doctor_rejects_machine_identity_in_engine(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    private_label = "DESKTOP-" + "PRIVATE123"
    (engine / "notes.txt").write_text(private_label + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_ENGINE_KNOWN_HOST_LABEL"):
        run(engine, tmp_path / "missing-host.json", None, None)


@pytest.mark.parametrize(
    ("name", "content", "rule_id"),
    [
        ("id_rsa", b"synthetic private key fixture\n", "private_key_file"),
        ("opaque.bin", b"before\x00after", "binary_unscanned"),
        ("large.txt", b"x" * (DEFAULT_MAX_BLOB_BYTES + 1), "oversize_unscanned"),
    ],
    ids=["private-key", "binary", "oversize"],
)
def test_doctor_surfaces_structural_engine_findings(
    tmp_path: Path, name: str, content: bytes, rule_id: str,
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / name).write_bytes(content)
    with pytest.raises(ConfigError, match="FAIL_ENGINE_PUBLIC_SCAN") as caught:
        run(engine, tmp_path / "missing-host.json", None, None)
    assert rule_id in str(caught.value)


def test_repository_separation_allows_private_enforcement_data(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    verifier = state / "enforcement" / "verifiers.json"
    verifier.parent.mkdir(parents=True)
    verifier.write_text('{"schema":"verifier-manifest/1","verifiers":[]}\n', encoding="utf-8")
    assert_repository_separation(engine, state)


def test_doctor_canonical_layout_requires_provenance_before_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr(doctor_module, "classify_engine_layout", lambda _root: EngineLayout.CANONICAL)
    monkeypatch.setattr(
        doctor_module, "validate_engine_provenance",
        lambda _root: (_ for _ in ()).throw(ConfigError("FAIL_ENGINE_PROVENANCE", "synthetic")),
    )
    with pytest.raises(ConfigError, match="FAIL_ENGINE_PROVENANCE"):
        doctor_module.run(engine, tmp_path / "missing-host.json", None, None)


def test_doctor_installed_artifact_uses_pin_manifest_not_source_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, config, _pin_path, pin = installed_fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("source Git helper reached for installed artifact")

    monkeypatch.setattr(doctor_module, "classify_engine_layout", forbidden)
    monkeypatch.setattr(doctor_module, "validate_engine_provenance", forbidden)
    monkeypatch.setattr(doctor_module, "assert_remote_role", forbidden)
    monkeypatch.setattr(doctor_module, "load_config", lambda _path: {"targets": []})
    monkeypatch.setattr(
        doctor_module, "compose_manifests",
        lambda *_args: SimpleNamespace(composition_hash="c2", capabilities=()),
    )
    monkeypatch.setattr(doctor_module, "assert_capability_sources", lambda *_args: None)

    lines = doctor_module.run(engine, config, None, None, require_versioned=True)
    assert (
        f"PASS installed_artifact version={pin['version']} "
        f"artifact_sha256={pin['artifact_sha256']} pin=verified"
    ) in lines
    assert not any("engine_provenance" in line or "remote_role" in line for line in lines)


@pytest.mark.parametrize(
    "mutation",
    ["digest", "config", "version", "shape", "pin-missing", "pin-directory", "engine-alias"],
)
def test_doctor_installed_artifact_mismatch_fails_without_source_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    engine, config, pin_path, pin = installed_fixture(tmp_path)
    if mutation == "digest":
        pin["artifact_sha256"] = "A" * 43
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "config":
        pin["config_path"] = str((tmp_path / "other.json").resolve())
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "version":
        pin["version"] = "different-version"
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "shape":
        moved = engine.parent.parent / "payload" / engine.name
        moved.parent.mkdir()
        engine.rename(moved)
        engine = moved
    elif mutation == "pin-missing":
        pin_path.unlink()
    elif mutation == "pin-directory":
        pin_path.unlink()
        pin_path.mkdir()
    else:
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink",
            lambda path: path == engine or original_is_symlink(path),
        )

    monkeypatch.setattr(
        doctor_module, "classify_engine_layout",
        lambda _root: (_ for _ in ()).throw(AssertionError("installed failure downgraded")),
    )
    with pytest.raises(
        ConfigError, match="^FAIL_INSTALLED_ARTIFACT verification failed$",
    ):
        doctor_module.run(engine, config, None, None)


def test_doctor_standalone_skips_provenance_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr(doctor_module, "classify_engine_layout", lambda _root: EngineLayout.STANDALONE)
    monkeypatch.setattr(
        doctor_module, "validate_engine_provenance",
        lambda _root: (_ for _ in ()).throw(AssertionError("provenance")),
    )
    with pytest.raises(ConfigError, match="FAIL_CONFIG"):
        doctor_module.run(engine, tmp_path / "missing-host.json", None, None)


def test_doctor_duplicate_scan_reports_identity_only_and_writes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    global_ledger = state / "experience" / "LESSONS.md"
    global_ledger.parent.mkdir(parents=True)
    global_ledger.write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n<!-- lessons-scope: global -->\n"
        "\n## 活跃\n- **L-1 [pending·通用] Private duplicate body.** 触发:x. 代价:y. sink → z.\n\n## 归档\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    (workspace / ".agents").mkdir(parents=True)
    git(workspace, "init", "-q")
    (workspace / ".agents" / "lessons.json").write_text(
        json.dumps({"schema": "lessons-routing/1", "project_id": "sample-app", "profiles": []}),
        encoding="utf-8",
    )
    (workspace / ".agents" / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n<!-- lessons-scope: project -->\n"
        "<!-- lessons-project: sample-app -->\n\n## 活跃\n"
        "- **[[lesson:SAMPLE-1]] [pending·项目] Private duplicate body.** 触发:x. 代价:y. sink → z.\n\n## 归档\n",
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (global_ledger, workspace / ".agents" / "LESSONS.md")}
    with pytest.raises(ConfigError, match="FAIL_LESSON_DUPLICATE") as caught:
        doctor_module._lesson_duplicate_line(state, workspace)
    assert "global:global:L-1" in str(caught.value)
    assert "project:sample-app:SAMPLE-1" in str(caught.value)
    assert "Private duplicate body" not in str(caught.value)
    assert {path: path.read_bytes() for path in before} == before

    (workspace / ".agents" / "LESSONS.md").write_text(
        (workspace / ".agents" / "LESSONS.md").read_text(encoding="utf-8").replace(
            "Private duplicate body.", "A distinct project rule.",
        ), encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in before}
    assert doctor_module._lesson_duplicate_line(state, workspace) == "PASS lesson_duplicates=none"
    assert {path: path.read_bytes() for path in before} == before


def test_doctor_workspace_cli_reports_duplicates_and_skips_without_state(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    global_ledger = state / "experience" / "LESSONS.md"
    global_ledger.parent.mkdir(parents=True)
    global_ledger.write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n<!-- lessons-scope: global -->\n"
        "\n## 活跃\n- **L-1 [pending·通用] Hidden duplicate body.** 触发:x. 代价:y. sink → z.\n\n## 归档\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    (workspace / ".agents").mkdir(parents=True)
    git(workspace, "init", "-q")
    (workspace / ".agents" / "lessons.json").write_text(
        json.dumps({"schema": "lessons-routing/1", "project_id": "sample-app", "profiles": []}),
        encoding="utf-8",
    )
    (workspace / ".agents" / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n<!-- lessons-scope: project -->\n"
        "<!-- lessons-project: sample-app -->\n\n## 活跃\n"
        "- **[[lesson:SAMPLE-1]] [pending·项目] Hidden duplicate body.** 触发:x. 代价:y. sink → z.\n\n## 归档\n",
        encoding="utf-8",
    )
    config = tmp_path / "host.json"
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(doctor_module, "classify_engine_layout", lambda _root: EngineLayout.STANDALONE)
    monkeypatch.setattr(doctor_module, "assert_repository_separation", lambda *_args: None)
    monkeypatch.setattr(doctor_module, "assert_remote_role", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(doctor_module, "load_config", lambda _path: {"targets": []})
    monkeypatch.setattr(doctor_module, "compose_manifests", lambda *_args: SimpleNamespace(composition_hash="synthetic"))
    monkeypatch.setattr(doctor_module, "assert_capability_sources", lambda *_args: None)
    monkeypatch.setattr(doctor_module, "is_repository", lambda _path: True)
    monkeypatch.setattr(doctor_module, "check_remote_parity", lambda *_args: "synthetic")
    before = {
        path: path.read_bytes()
        for path in (global_ledger, workspace / ".agents" / "LESSONS.md")
    }
    cached = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--cached", "--binary"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    assert cli_main(["doctor", "--config", str(config), "--state", str(state),
                     "--workspace", str(workspace)]) == 1
    error = capsys.readouterr().err
    assert "FAIL_LESSON_DUPLICATE" in error and "global:global:L-1" in error
    assert "Hidden duplicate body" not in error
    assert {path: path.read_bytes() for path in before} == before
    assert subprocess.run(
        ["git", "-C", str(workspace), "diff", "--cached", "--binary"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout == cached

    (workspace / ".agents" / "LESSONS.md").write_text(
        (workspace / ".agents" / "LESSONS.md").read_text(encoding="utf-8").replace(
            "Hidden duplicate body.", "Different project rule.",
        ), encoding="utf-8",
    )
    assert cli_main(["doctor", "--config", str(config), "--workspace", str(workspace)]) == 0
    assert "FAIL_LESSON_DUPLICATE" not in capsys.readouterr().err
