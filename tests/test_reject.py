from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.cli import main as cli_main
from agent_core.promote import apply_local_reject, create_candidate, operation_lock, plan_local_reject
from agent_core.reject import main as reject_main


ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def host_config(tmp_path: Path, state_root: Path | str) -> Path:
    payload = json.loads((ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    payload["state_root"] = str(state_root)
    path = tmp_path / "host.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def setup_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "private"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", f"test{chr(64)}invalid")
    (repo / "engine").mkdir()
    ledger = repo / "state" / "experience" / "LESSONS.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n## 归档\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "seed")
    return repo, repo / "state", host_config(tmp_path, repo / "state")


def setup_project(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", f"test{chr(64)}invalid")
    agents = repo / ".agents"
    agents.mkdir()
    (agents / "lessons.json").write_text(
        json.dumps({"schema": "lessons-routing/1", "project_id": "sample-app", "profiles": []}) + "\n",
        encoding="utf-8",
    )
    (agents / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: project -->\n<!-- lessons-project: sample-app -->\n\n"
        "## 活跃\n\n## 归档\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "seed")
    return repo, host_config(tmp_path, "<STATE>")


def make_candidate(repo: Path, state: Path | None, suffix: str, *, project: bool = False,
                   scope_hint: str | None = None) -> tuple[str, Path]:
    inbox = repo / ".agents" / "inbox" if project else state / "inbox"
    item = create_candidate(
        repo, repo / "control", host="desk", agent="codex", rule=f"Reject {suffix}",
        trigger=f"reject {suffix}", cost="queue noise", sink=f"checks/{suffix}.md",
        scope_hint=scope_hint or ("project:sample-app" if project else "global"), evidence=f"synthetic:{suffix}",
        base_revision=f"{git(repo, 'rev-parse', 'HEAD').stdout.strip()} unverified",
        inbox_path=inbox, require_state_freshness=False, allow_project=project,
    ).stem
    return item, inbox / f"{item}.md"


@pytest.mark.parametrize("tracked", [False, True])
def test_state_reject_moves_exact_bytes_and_stages_exact_paths(tmp_path: Path, tracked: bool) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, f"state-{tracked}")
    if tracked:
        git(repo, "add", source.relative_to(repo).as_posix())
        git(repo, "commit", "-q", "-m", "track candidate")
    raw = source.read_bytes()
    ledger_before = (state / "experience" / "LESSONS.md").read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    rejected = state / "inbox" / "rejected" / source.name
    assert not rejected.parent.exists()
    assert plan.payload["source_tracked"] is tracked
    assert len(plan.payload["control_filesystem_sha256"]) == 64
    result = apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert not source.exists() and rejected.read_bytes() == raw
    assert (state / "experience" / "LESSONS.md").read_bytes() == ledger_before
    expected = {rejected.relative_to(repo).as_posix()}
    if tracked:
        expected.add(source.relative_to(repo).as_posix())
    assert set(result.changed_paths) == expected
    assert set(git(repo, "diff", "--cached", "--name-only", "--no-renames").stdout.splitlines()) == expected


def test_project_reject_cli_dry_run_then_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, config = setup_project(tmp_path)
    item, source = make_candidate(repo, None, "project", project=True)
    raw = source.read_bytes()
    base = ["--workspace", str(repo), "--config", str(config), "--id", item]
    assert reject_main(base) == 0
    output = capsys.readouterr().out
    plan_hash = next(line.split(" ", 1)[1] for line in output.splitlines() if line.startswith("PLAN_HASH "))
    assert all(label in output for label in (
        "SOURCE_IDENTITY ", "REPOSITORY root=", "STATE root=", "CONFIG path=",
        "CONTROL root=", "DESTINATION ", "STAGE_PATHS ", "CANDIDATE_SHA256 ",
    ))
    assert source.is_file() and not (source.parent / "rejected").exists()
    assert reject_main([*base, "--apply", "--plan-hash", plan_hash]) == 0
    applied = capsys.readouterr().out
    destination = source.parent / "rejected" / source.name
    assert destination.read_bytes() == raw and not source.exists()
    assert f"PASS lesson_rejected={item}" in applied


def test_state_profile_candidate_uses_the_same_safe_queue(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "profile", scope_hint="profile:example-domain")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert (state / "inbox" / "rejected" / source.name).read_bytes() == raw


def test_lessons_reject_help_is_publicly_routed() -> None:
    with pytest.raises(SystemExit, match="0"):
        cli_main(["lessons", "reject", "--help"])


def test_reject_rejects_invalid_missing_and_malformed_candidates(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE"):
        plan_local_reject(repo, None, "../bad", state_root=state, config_path=config)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_MISSING"):
        plan_local_reject(repo, None, "desk-20260904T000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", state_root=state,
                          config_path=config)
    item, source = make_candidate(repo, state, "malformed")
    source.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE"):
        plan_local_reject(repo, None, item, state_root=state, config_path=config)


@pytest.mark.parametrize("collision", ["consumed", "rejected-file", "rejected-directory", "hardlink"])
def test_reject_collisions_fail_closed_without_adoption(tmp_path: Path, collision: str) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, collision)
    raw = source.read_bytes()
    if collision == "consumed":
        target = source.parent / "consumed" / source.name
        target.parent.mkdir()
        target.write_bytes(raw)
    else:
        target = source.parent / "rejected" / source.name
        target.parent.mkdir()
        if collision == "rejected-directory":
            target.mkdir()
        elif collision == "hardlink":
            os.link(source, target)
        else:
            target.write_bytes(raw)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        plan_local_reject(repo, None, item, state_root=state, config_path=config)
    assert source.exists() and source.read_bytes() == raw


def test_reject_duplicate_project_and_state_sources_fail_closed(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "duplicate")
    project_source = repo / ".agents" / "inbox" / source.name
    project_source.parent.mkdir(parents=True)
    project_source.write_bytes(source.read_bytes())
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        plan_local_reject(repo, None, item, state_root=state, config_path=config)


@pytest.mark.parametrize("kind", ["directory", "hardlink", "symlink"])
def test_reject_source_must_be_an_ordinary_single_link_file(tmp_path: Path, kind: str) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, f"source-{kind}")
    raw = source.read_bytes()
    if kind == "directory":
        source.unlink()
        source.mkdir()
    elif kind == "hardlink":
        os.link(source, tmp_path / "second-link.md")
    else:
        source.unlink()
        target = tmp_path / "symlink-target.md"
        target.write_bytes(raw)
        try:
            source.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        plan_local_reject(repo, None, item, state_root=state, config_path=config)


def test_reject_revalidates_head_candidate_config_and_index_before_move(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "drift")
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    before = source.read_bytes()
    source.write_bytes(before + b" ")
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    source.write_bytes(before)
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    (repo / "unrelated.txt").write_text("head drift\n", encoding="utf-8")
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-q", "-m", "head drift")
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    config.write_text(config.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)


def test_reject_revalidates_same_bytes_identity_and_index_before_move(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "identity")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    source.unlink()
    source.write_bytes(raw)
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.read_bytes() == raw

    git(repo, "add", source.relative_to(repo).as_posix())
    git(repo, "commit", "-q", "-m", "track candidate")
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    source.write_bytes(raw + b" ")
    git(repo, "add", source.relative_to(repo).as_posix())
    source.write_bytes(raw)
    with pytest.raises(ConfigError, match="FAIL_INDEX_CONFLICT"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.read_bytes() == raw


def test_reject_destination_race_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "race")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    destination = state / "inbox" / "rejected" / source.name

    def race(_source: Path, target: Path) -> None:
        target.write_bytes(b"racer")

    monkeypatch.setattr("agent_core.promote._before_local_reject_move", race)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.read_bytes() == raw and destination.read_bytes() == b"racer"


def test_reject_parent_replacement_fails_before_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "parent-race")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)

    def replace_parent(_source: Path, destination: Path) -> None:
        parent = destination.parent
        os.rename(parent, parent.with_name("rejected-detached"))
        parent.mkdir()

    monkeypatch.setattr("agent_core.promote._before_local_reject_move", replace_parent)
    with pytest.raises(ConfigError, match="rejected parent changed"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.read_bytes() == raw


def test_reject_git_add_failure_restores_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "add-failure")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    from agent_core import promote as promote_module
    original_git = promote_module._git

    def fail_add(path: Path, *args: str, **kwargs):
        if args and args[0] == "add":
            raise ConfigError("FAIL_GIT", "injected add failure")
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(promote_module, "_git", fail_add)
    with pytest.raises(ConfigError, match="injected add failure"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.read_bytes() == raw
    assert not (state / "inbox" / "rejected" / source.name).exists()


def test_reject_recovery_race_preserves_both_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "recovery-race")
    raw = source.read_bytes()
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    from agent_core import promote as promote_module
    original_git = promote_module._git

    def fail_add(path: Path, *args: str, **kwargs):
        if args and args[0] == "add":
            source.write_bytes(b"racer")
            raise ConfigError("FAIL_GIT", "injected add failure")
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(promote_module, "_git", fail_add)
    with pytest.raises(ConfigError, match="FAIL_REJECT_RECOVERY"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    destination = state / "inbox" / "rejected" / source.name
    assert source.read_bytes() == b"racer" and destination.read_bytes() == raw


def test_reject_same_plan_cannot_apply_twice(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, _source = make_candidate(repo, state, "twice")
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)


def test_reject_uses_shared_operation_lock(tmp_path: Path) -> None:
    repo, state, config = setup_state(tmp_path)
    item, source = make_candidate(repo, state, "shared-lock")
    plan = plan_local_reject(repo, None, item, state_root=state, config_path=config)
    with operation_lock(Path(plan.payload["control_root"])):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            apply_local_reject(repo, None, plan, plan.plan_hash, config_path=config)
    assert source.is_file()
