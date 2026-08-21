from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.doctor import check_remote_parity, run as run_doctor
from agent_core.freshness import record_remote_head, require_fresh
from agent_core.sync import execute as execute_sync


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def repository(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", f"test{chr(64)}invalid")
    (seed / "experience").mkdir()
    (seed / "experience" / "LESSONS.md").write_text(
        "# Lessons Ledger\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] Existing rule.** 触发: existing trigger. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", f"test{chr(64)}invalid")
    return remote, seed, clone


def test_freshness_rejects_behind_ahead_dirty_and_unmerged(tmp_path: Path) -> None:
    _remote, seed, clone = repository(tmp_path)
    control = tmp_path / "control"
    (seed / "remote.txt").write_text("remote", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "remote")
    git(seed, "push", "-q")
    with pytest.raises(ConfigError, match="FAIL_STALE"):
        require_fresh(clone, "promote", control)
    git(clone, "pull", "-q", "--ff-only")
    (clone / "local.txt").write_text("local", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "local")
    with pytest.raises(ConfigError, match="FAIL_DIVERGED"):
        require_fresh(clone, "promote", control)
    git(clone, "reset", "--hard", "origin/main")
    (clone / "experience" / "LESSONS.md").write_text("dirty", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_DIRTY"):
        require_fresh(clone, "promote", control)
    git(clone, "reset", "--hard", "origin/main")
    git(clone, "switch", "-q", "-c", "conflict")
    ledger_path = clone / "experience" / "LESSONS.md"
    ledger_path.write_text(ledger_path.read_text(encoding="utf-8").replace("Existing rule", "Branch rule"), encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "branch")
    git(clone, "switch", "-q", "main")
    ledger_path.write_text(ledger_path.read_text(encoding="utf-8").replace("Existing rule", "Main rule"), encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "main")
    git(clone, "merge", "conflict", check=False)
    with pytest.raises(ConfigError, match="FAIL_CONFLICT"):
        require_fresh(clone, "promote", control)


def test_remote_rewind_and_offline_fail_closed(tmp_path: Path) -> None:
    remote, seed, clone = repository(tmp_path)
    control = tmp_path / "control"
    first = git(clone, "rev-parse", "origin/main").stdout.strip()
    (seed / "next.txt").write_text("next", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "next")
    git(seed, "push", "-q")
    current = git(seed, "rev-parse", "HEAD").stdout.strip()
    record_remote_head(control, current)
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", first], check=True)
    with pytest.raises(ConfigError, match="FAIL_REMOTE_REWIND"):
        require_fresh(clone, "doctor", control)
    record_remote_head(control, first)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        require_fresh(clone, "promote", control)
    with pytest.raises(ConfigError, match="FAIL_REMOTE_PARITY"):
        check_remote_parity(clone, control)


def test_schema_valid_untracked_inbox_is_the_only_dirty_exception(tmp_path: Path) -> None:
    _remote, _seed, clone = repository(tmp_path)
    control = tmp_path / "control"
    inbox = clone / "inbox"
    inbox.mkdir()
    candidate = {
        "schema": "candidate/1", "id": "desk-20260811T000000Z-" + "a" * 32,
        "created_utc": "2026-08-11T00:00:00Z", "host": "desk", "agent": "codex",
        "base_revision": git(clone, "rev-parse", "HEAD").stdout.strip(),
        "rule": "Keep writes transactional", "trigger": "promoting a lesson",
        "cost": "lost updates", "sink": "checks/promote.md", "scope_hint": "global",
        "evidence": "synthetic:test",
    }
    (inbox / f"{candidate['id']}.md").write_text(json.dumps(candidate), encoding="utf-8")
    assert require_fresh(clone, "promote", control).behind == 0
    (clone / "other.tmp").write_text("outside", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_DIRTY"):
        require_fresh(clone, "promote", control)


def test_offline_sync_apply_is_rejected(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / "ac1"
    state = tmp_path / "state"
    shutil.copytree(fixtures / "state", state)
    shutil.copy2(fixtures / "manifests" / "valid-state.json", state / "manifest.yaml")
    git(state, "init", "-q", "-b", "main")
    git(state, "config", "user.name", "Test")
    git(state, "config", "user.email", f"test{chr(64)}invalid")
    git(state, "add", ".")
    git(state, "commit", "-q", "-m", "state")
    payload = json.loads((root / "tests" / "fixtures" / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backup")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(tmp_path / f"runtime-{index}")
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        execute_sync(root, config, state, apply=True)


def test_product_sync_rejects_an_unversioned_state(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / "ac1"
    state = tmp_path / "state"
    shutil.copytree(fixtures / "state", state)
    payload = json.loads((root / "tests" / "fixtures" / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backup")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(tmp_path / f"runtime-{index}")
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_REPOSITORY"):
        execute_sync(root, config, state, apply=True, require_versioned=True)


def test_sync_and_doctor_consume_healthy_remote_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / "ac1"
    state = tmp_path / "state"
    shutil.copytree(fixtures / "state", state)
    shutil.copy2(fixtures / "manifests" / "valid-state.json", state / "manifest.yaml")
    git(state, "init", "-q", "-b", "main")
    git(state, "config", "user.name", "Test")
    git(state, "config", "user.email", f"test{chr(64)}invalid")
    git(state, "add", ".")
    git(state, "commit", "-q", "-m", "state")
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(state, "remote", "add", "origin", str(remote))
    git(state, "push", "-q", "-u", "origin", "main")

    payload = json.loads((root / "tests" / "fixtures" / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backup")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(tmp_path / f"runtime-{index}")
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    assert any(line.startswith("APPLIED ") for line in execute_sync(root, config, state, apply=True))
    lines = run_doctor(root, config, state, state / "manifest.yaml")
    assert any(line.startswith("PASS git_remote_parity=") for line in lines)
