from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_core.cli import main as cli_main
from agent_core.config import ConfigError
from agent_core.promote import create_candidate, plan_promote, plan_publish
from agent_core.state import (
    apply_attach,
    apply_init,
    binding_receipt_path,
    plan_attach,
    plan_init,
)
from agent_core.sync import execute as execute_sync


ROOT = Path(__file__).resolve().parents[1]
TEST_EMAIL = "owner" + chr(64) + "invalid"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def host_config(tmp_path: Path, state_root: str = "<STATE>") -> Path:
    payload = json.loads((ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    payload["state_root"] = state_root
    payload["backup_root"] = str(tmp_path / "backups")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(tmp_path / f"runtime-{index}")
    path = tmp_path / "host.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def initialized_state(tmp_path: Path, name: str = "state") -> Path:
    state = tmp_path / name
    apply_init(ROOT, state, git_name="Synthetic Owner", git_email=TEST_EMAIL)
    return state


def attached_clone(tmp_path: Path) -> tuple[Path, Path]:
    source = initialized_state(tmp_path, "source")
    remote = tmp_path / "private-state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "-q", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    return clone, remote


def candidate(state: Path, control: Path) -> str:
    path = create_candidate(
        state,
        control,
        host="desk",
        agent="codex",
        rule="Keep state writes remotely synchronized.",
        trigger="publishing or promoting state",
        cost="divergent private ledgers",
        sink="checks/state.md",
        scope_hint="global",
        evidence="synthetic:state-bootstrap",
    )
    return path.stem


def test_state_init_plan_is_zero_write(tmp_path: Path) -> None:
    target = tmp_path / "state"
    before = set(tmp_path.iterdir())
    lines = plan_init(ROOT, target)
    assert lines[-1] == "DRY_RUN writes=0"
    assert set(tmp_path.iterdir()) == before
    assert not target.exists()


def test_state_init_apply_creates_seeded_clean_main_repo(tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.mkdir()
    assert cli_main([
        "state", "init", "--path", str(target), "--apply",
        "--git-name", "Synthetic Owner", "--git-email", TEST_EMAIL,
    ]) == 0
    assert git(target, "branch", "--show-current").stdout.strip() == "main"
    assert git(target, "status", "--porcelain").stdout == ""
    assert git(target, "remote").stdout == ""
    assert git(target, "config", "--local", "user.name").stdout.strip() == "Synthetic Owner"
    assert git(target, "config", "--local", "user.email").stdout.strip() == TEST_EMAIL
    assert (target / "experience" / "LESSONS.md").is_file()
    assert (target / "experience" / "CASE_LAW.md").is_file()
    assert (target / "experience" / "profiles" / "example-domain" / "LESSONS.md").is_file()
    assert (target / "rules" / "global.md").is_file()
    assert json.loads((target / "manifest.yaml").read_text(encoding="utf-8")) == {
        "schema": "capability-manifest/1", "capabilities": [],
    }
    lock = json.loads((target / "agent-core.lock.json").read_text(encoding="utf-8"))
    assert set(lock) == {"engine_version", "engine_source", "schema_version", "pinned_at"}
    assert lock["schema_version"] == "lessons-ledger/2"
    assert lock["engine_source"] == f"local@{lock['engine_version']}"
    assert lock["pinned_at"].endswith("Z")


def test_state_init_missing_identity_leaves_target_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_global = tmp_path / "empty-gitconfig"
    isolated_global.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_global))
    missing_identity = tmp_path / "missing-identity"
    with pytest.raises(ConfigError, match="FAIL_GIT_IDENTITY"):
        apply_init(ROOT, missing_identity)
    assert not missing_identity.exists()


def test_state_init_injected_commit_failure_preserves_existing_empty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:

    existing_empty = tmp_path / "atomic"
    existing_empty.mkdir()
    monkeypatch.setattr(
        "agent_core.state._commit_state",
        lambda _repo: (_ for _ in ()).throw(ConfigError("INJECTED_COMMIT_FAILURE", "synthetic")),
    )
    with pytest.raises(ConfigError, match="INJECTED_COMMIT_FAILURE"):
        apply_init(
            ROOT, existing_empty,
            git_name="Synthetic Owner", git_email=TEST_EMAIL,
        )
    assert existing_empty.is_dir()
    assert list(existing_empty.iterdir()) == []
    assert not list(tmp_path.glob(".state-bootstrap-*"))


def test_state_attach_requires_explicit_private_confirmation(tmp_path: Path) -> None:
    clone, _remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    before = config.read_bytes()
    with pytest.raises(ConfigError, match="PRIVATE_REMOTE_CONFIRMATION_REQUIRED"):
        plan_attach(clone, config, confirm_private_remote=False)
    assert config.read_bytes() == before
    assert not binding_receipt_path(config).exists()


def test_state_attach_plan_is_zero_write_and_apply_binds_valid_clone(tmp_path: Path) -> None:
    clone, remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    before = config.read_bytes()
    remote_ref_before = git(clone, "rev-parse", "origin/main").stdout.strip()
    lines = plan_attach(clone, config, confirm_private_remote=True)
    assert lines[-1] == "DRY_RUN writes=0"
    assert config.read_bytes() == before
    assert not binding_receipt_path(config).exists()
    assert git(clone, "rev-parse", "origin/main").stdout.strip() == remote_ref_before

    result = apply_attach(clone, config, confirm_private_remote=True)
    assert result[-1].startswith("PASS revision=")
    assert json.loads(config.read_text(encoding="utf-8"))["state_root"] == str(clone.resolve())
    receipt = json.loads(binding_receipt_path(config).read_text(encoding="utf-8"))
    assert receipt["schema"] == "state-binding/1"
    assert receipt["confirmed_private_remote"] is True
    assert receipt["remote_name"] == "origin"
    assert receipt["remote_revision"] == remote_ref_before
    assert str(remote.resolve()) not in binding_receipt_path(config).read_text(encoding="utf-8")
    assert len(receipt["remote_url_sha256"]) == 64


def test_state_attach_rejects_invalid_lock_without_touching_overlay(tmp_path: Path) -> None:
    clone, _remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    before = config.read_bytes()
    lock_path = clone / "agent-core.lock.json"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_LOCK"):
        apply_attach(clone, config, confirm_private_remote=True)
    assert config.read_bytes() == before
    assert not binding_receipt_path(config).exists()


def test_state_attach_second_write_failure_restores_overlay_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone, _remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    receipt_path = binding_receipt_path(config)
    previous_receipt = b'{"previous":true}\n'
    receipt_path.write_bytes(previous_receipt)
    before = config.read_bytes()
    from agent_core import state as state_module

    original = state_module._atomic_write
    calls = 0

    def fail_second_write(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("INJECTED_RECEIPT_WRITE_FAILURE")
        original(path, content)

    monkeypatch.setattr(state_module, "_atomic_write", fail_second_write)
    with pytest.raises(ConfigError, match="FAIL_STATE_ATTACH"):
        apply_attach(clone, config, confirm_private_remote=True)
    assert config.read_bytes() == before
    assert receipt_path.read_bytes() == previous_receipt


def test_publish_without_fetchable_origin_is_remote_required(tmp_path: Path) -> None:
    state = initialized_state(tmp_path)
    item = candidate(state, tmp_path / "control")
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        plan_publish(state, tmp_path / "control", item)


def test_promote_without_fetchable_origin_is_remote_required(tmp_path: Path) -> None:
    state = initialized_state(tmp_path)
    item = candidate(state, tmp_path / "control")
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        plan_promote(state, tmp_path / "control", item)


def test_sync_apply_without_fetchable_origin_is_remote_required(tmp_path: Path) -> None:
    state = initialized_state(tmp_path)
    config = host_config(tmp_path, str(state))
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        execute_sync(ROOT, config, state, apply=True, require_versioned=True)
