from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core import freshness as freshness_module
from agent_core.config import ConfigError
from agent_core.doctor import check_remote_parity, run as run_doctor
from agent_core.freshness import migrate_legacy_remote_state, record_remote_head, require_fresh
from agent_core.promote import operation_lock
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


def reviewed_sync(root: Path, config: Path, state: Path, *, require_versioned: bool = False) -> list[str]:
    preview = execute_sync(root, config, state, apply=False)
    token = next(line.removeprefix("PLAN_HASH ") for line in preview if line.startswith("PLAN_HASH "))
    return execute_sync(
        root, config, state, apply=True, require_versioned=require_versioned,
        plan_hash=token,
    )


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
    with pytest.raises(ConfigError, match="FAIL_REMOTE_FETCH.*git fetch"):
        check_remote_parity(clone, control)


def test_doctor_remote_failures_keep_distinct_actionable_causes(tmp_path: Path) -> None:
    control = tmp_path / "control"

    no_origin = tmp_path / "no-origin"
    no_origin.mkdir()
    git(no_origin, "init", "-q", "-b", "main")
    git(no_origin, "config", "user.name", "Test")
    git(no_origin, "config", "user.email", f"test{chr(64)}invalid")
    (no_origin / "tracked").write_text("one", encoding="utf-8")
    git(no_origin, "add", ".")
    git(no_origin, "commit", "-q", "-m", "one")
    with pytest.raises(ConfigError, match="FAIL_REMOTE_ORIGIN.*git remote get-url"):
        check_remote_parity(no_origin, control)

    missing_remote = tmp_path / "missing-remote"
    subprocess.run(["git", "clone", "-q", str(no_origin), str(missing_remote)], check=True)
    git(missing_remote, "remote", "set-url", "origin", str(tmp_path / "absent.git"))
    with pytest.raises(ConfigError, match="FAIL_REMOTE_FETCH.*git fetch"):
        check_remote_parity(missing_remote, control)

    empty_bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", "-q", str(empty_bare)], check=True)
    missing_main = tmp_path / "missing-main"
    subprocess.run(["git", "clone", "-q", str(no_origin), str(missing_main)], check=True)
    git(missing_main, "remote", "set-url", "origin", str(empty_bare))
    git(missing_main, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(ConfigError, match="FAIL_REMOTE_REF.*git rev-parse --verify"):
        check_remote_parity(missing_main, control)

    _remote, seed, clone = repository(tmp_path / "diverged")
    (seed / "remote-change").write_text("remote", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "remote")
    git(seed, "push", "-q")
    (clone / "local-change").write_text("local", encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "local")
    with pytest.raises(
        ConfigError, match="FAIL_REMOTE_DIVERGED ahead=1 behind=1",
    ):
        check_remote_parity(clone, control)


def test_doctor_remote_parity_preserves_non_remote_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise ConfigError("FAIL_CONFIG", "broken host config")

    monkeypatch.setattr("agent_core.doctor.require_fresh", fail)
    with pytest.raises(ConfigError, match="FAIL_CONFIG broken host config") as captured:
        check_remote_parity(tmp_path, tmp_path / "control")
    assert captured.value.code == "FAIL_CONFIG"


def test_git_failure_detail_is_bounded_and_redacts_url_credentials() -> None:
    result = subprocess.CompletedProcess(
        ["git", "fetch"], 128, "",
        "fatal: unable to access 'https://user:secret@example.invalid/repo.git/'\n"
        "credential-bearing second line",
    )
    detail = freshness_module._git_failure(("fetch", "origin"), result)
    assert detail.startswith("git fetch origin: fatal:")
    assert "secret" not in detail and "https://***@example.invalid" in detail
    assert "second line" not in detail
    assert freshness_module._confirmed_offline(subprocess.CompletedProcess(
        ["git", "fetch"], 128, "", "fatal: could not resolve host: example.invalid",
    )) is True
    assert freshness_module._confirmed_offline(subprocess.CompletedProcess(
        ["git", "fetch"], 128, "", "fatal: Authentication failed",
    )) is False


def test_default_legacy_baseline_migrates_under_lock_without_deleting_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote, _seed, clone = repository(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    config = home / ".agent-core" / "host.json"
    config.parent.mkdir(parents=True)
    known = git(clone, "rev-parse", "origin/main").stdout.strip()
    legacy = config.parent / "remote-state.json"
    legacy.write_text(json.dumps({"last_known_good": known}, sort_keys=True) + "\n", encoding="utf-8")
    before = legacy.read_bytes()
    control = config.parent / "txn"

    with operation_lock(control) as token:
        assert migrate_legacy_remote_state(config, clone, lock_token=token) is True

    current = control / "remote-state.json"
    assert current.read_bytes() == before
    assert legacy.read_bytes() == before
    assert require_fresh(clone, "doctor", control).remote == known


def test_custom_config_does_not_copy_legacy_global_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote, _seed, clone = repository(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    legacy = home / ".agent-core" / "remote-state.json"
    legacy.parent.mkdir(parents=True)
    known = git(clone, "rev-parse", "origin/main").stdout.strip()
    legacy.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    config = tmp_path / "custom" / "host.json"
    control = config.parent / "txn"

    with operation_lock(control) as token:
        assert migrate_legacy_remote_state(config, clone, lock_token=token) is False

    assert not (control / "remote-state.json").exists()
    assert legacy.is_file()


def test_corrupt_default_legacy_baseline_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote, _seed, clone = repository(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    config = home / ".agent-core" / "host.json"
    config.parent.mkdir(parents=True)
    legacy = config.parent / "remote-state.json"
    legacy.write_text("corrupt\n", encoding="utf-8")
    control = config.parent / "txn"

    with operation_lock(control) as token:
        with pytest.raises(ConfigError, match="FAIL_REMOTE_STATE"):
            migrate_legacy_remote_state(config, clone, lock_token=token)
    assert legacy.read_text(encoding="utf-8") == "corrupt\n"
    assert not (control / "remote-state.json").exists()


def test_legacy_baseline_copy_race_fails_closed_without_replacing_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote, _seed, clone = repository(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    config = home / ".agent-core" / "host.json"
    config.parent.mkdir(parents=True)
    known = git(clone, "rev-parse", "origin/main").stdout.strip()
    legacy = config.parent / "remote-state.json"
    legacy.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    control = config.parent / "txn"
    raced = (json.dumps({"last_known_good": known}, sort_keys=True) + "\n").encode("utf-8")

    def race(_source: Path, destination: Path) -> None:
        Path(destination).write_bytes(raced)
        raise FileExistsError(destination)

    monkeypatch.setattr(freshness_module.os, "link", race)
    with operation_lock(control) as token:
        with pytest.raises(ConfigError, match="FAIL_REMOTE_STATE_RACE"):
            migrate_legacy_remote_state(config, clone, lock_token=token)
    assert (control / "remote-state.json").read_bytes() == raced
    assert legacy.is_file()


def test_doctor_remote_consumer_is_read_only(tmp_path: Path) -> None:
    _remote, _seed, clone = repository(tmp_path)
    control = tmp_path / "host" / "txn"
    known = git(clone, "rev-parse", "origin/main").stdout.strip()
    record_remote_head(control, known)
    baseline = control / "remote-state.json"
    before = (baseline.read_bytes(), baseline.stat().st_mtime_ns, baseline.stat().st_ino)

    assert check_remote_parity(clone, control) == known
    assert (baseline.read_bytes(), baseline.stat().st_mtime_ns, baseline.stat().st_ino) == before


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
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(state, "remote", "add", "origin", str(remote))
    git(state, "push", "-q", "-u", "origin", "main")
    git(state, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    payload = json.loads((root / "tests" / "fixtures" / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state)
    payload["backup_root"] = str(tmp_path / "backup")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(tmp_path / f"runtime-{index}")
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    preview = execute_sync(root, config, state, apply=False)
    token = next(line.removeprefix("PLAN_HASH ") for line in preview if line.startswith("PLAN_HASH "))
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        execute_sync(root, config, state, apply=True, plan_hash=token)


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
    preview = execute_sync(root, config, state, apply=False)
    token = next(line.removeprefix("PLAN_HASH ") for line in preview if line.startswith("PLAN_HASH "))
    with pytest.raises(ConfigError, match="FAIL_STATE_REPOSITORY"):
        execute_sync(root, config, state, apply=True, require_versioned=True, plan_hash=token)


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

    assert any(line.startswith("APPLIED ") for line in reviewed_sync(root, config, state))
    lines = run_doctor(root, config, state, state / "manifest.yaml")
    assert any(line.startswith("PASS git_remote_parity=") for line in lines)


def test_sync_reviewed_remote_revision_drift_writes_nothing(tmp_path: Path) -> None:
    _remote, seed, clone = repository(tmp_path)
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests" / "fixtures" / "ac1"
    shutil.copytree(fixtures / "state", clone, dirs_exist_ok=True)
    shutil.copy2(fixtures / "manifests" / "valid-state.json", clone / "manifest.yaml")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "materialization state")
    git(clone, "push", "-q")
    payload = json.loads((root / "tests" / "fixtures" / "config" / "two-runtime.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(clone)
    payload["backup_root"] = str(tmp_path / "backup")
    targets = [tmp_path / "runtime-a", tmp_path / "runtime-b"]
    for target, target_root in zip(payload["targets"], targets, strict=True):
        target["root"] = str(target_root)
    config = tmp_path / "host.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    preview = execute_sync(root, config, clone, apply=False)
    token = next(line.removeprefix("PLAN_HASH ") for line in preview if line.startswith("PLAN_HASH "))

    git(seed, "pull", "-q", "--ff-only")
    (seed / "remote-drift.txt").write_text("drift\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "remote drift")
    git(seed, "push", "-q")

    with pytest.raises(ConfigError, match="FAIL_DIVERGED"):
        execute_sync(root, config, clone, apply=True, plan_hash=token)
    assert not any(target.exists() for target in targets)
    assert not (tmp_path / "backup").exists()
