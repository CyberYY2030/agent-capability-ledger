from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.state import (
    apply_attach,
    binding_receipt_path,
)
from agent_core.sync import execute as execute_sync
from tests.state_fixture import create_state_repo


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
    create_state_repo(ROOT, state, git_name="Synthetic Owner", git_email=TEST_EMAIL)
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


def test_state_attach_requires_explicit_private_confirmation(tmp_path: Path) -> None:
    clone, _remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    before = config.read_bytes()
    with pytest.raises(ConfigError, match="PRIVATE_REMOTE_CONFIRMATION_REQUIRED"):
        apply_attach(clone, config, confirm_private_remote=False)
    assert config.read_bytes() == before
    assert not binding_receipt_path(config).exists()


def test_state_attach_apply_binds_valid_clone(tmp_path: Path) -> None:
    clone, remote = attached_clone(tmp_path)
    config = host_config(tmp_path)
    remote_ref_before = git(clone, "rev-parse", "origin/main").stdout.strip()

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


def test_sync_apply_without_fetchable_origin_is_remote_required(
    standalone_engine: Path, tmp_path: Path,
) -> None:
    state = initialized_state(tmp_path)
    config = host_config(tmp_path, str(state))
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        execute_sync(
            standalone_engine, config, state, apply=True, require_versioned=True,
            plan_hash="0" * 64,
        )
