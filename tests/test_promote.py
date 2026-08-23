from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.promote import (
    PROJECT_CONSUMED,
    PROJECT_INBOX,
    PROJECT_LEDGER,
    apply_project_promote,
    apply_local_promote,
    apply_prepared,
    create_candidate,
    plan_project_promote,
    plan_local_promote,
    plan_promote,
    plan_publish,
    prepare_promote,
    prepare_publish,
    rollback,
    assert_txn_path,
    operation_lock,
    _archive_superseded,
)
from agent_core.cli import main as cli_main
from agent_core.freshness import load_candidate
from agent_core.match import parse_markdown
from agent_core.project_promote import main as project_promote_main


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


def setup_pair(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", f"test{chr(64)}invalid")
    (seed / "experience").mkdir()
    (seed / "experience" / "LESSONS.md").write_text(
        "# Lessons Ledger\n<!-- next id: L-999 -->\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] Existing rule.** 触发: existing trigger. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    profile = seed / "experience" / "profiles" / "example-domain"
    profile.mkdir(parents=True)
    (profile / "LESSONS.md").write_text(
        "# Example Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: example-domain -->\n\n"
        "## 活跃\n\n- **[[lesson:EXAMPLE-1]] [pending·领域] Profile rule.** "
        "触发: profile trigger. 代价: profile cost. sink → checks/profile.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    clones = []
    for name in ("alpha", "beta"):
        clone = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
        git(clone, "config", "user.name", "Test")
        git(clone, "config", "user.email", f"test{chr(64)}invalid")
        clones.append(clone)
    return remote, seed, clones[0], clones[1]


def candidate(repo: Path, control: Path, suffix: str = "one", base: str | None = None) -> str:
    return create_candidate(
        repo, control, host="desk", agent="codex", rule=f"Transactional rule {suffix}",
        trigger=f"promote {suffix}", cost="lost updates", sink=f"checks/{suffix}.md",
        scope_hint="global", evidence=f"synthetic:{suffix}", base_revision=base,
    ).stem


def publish(repo: Path, control: Path, candidate_id: str) -> str:
    plan = plan_publish(repo, control, candidate_id)
    prepared = prepare_publish(repo, control, plan, plan.plan_hash, plan.expected_remote_sha)
    return apply_prepared(prepared).sha


def remote_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "origin/main").stdout.strip()


def setup_project(tmp_path: Path, project_id: str = "sample-app") -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", f"test{chr(64)}invalid")
    agents = repo / ".agents"
    agents.mkdir()
    (agents / "lessons.json").write_text(
        json.dumps({
            "schema": "lessons-routing/1",
            "project_id": project_id,
            "profiles": [],
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prefix = project_id.split("-", 1)[0].upper()
    (agents / "LESSONS.md").write_text(
        "# Project Lessons\n"
        "<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: project -->\n"
        f"<!-- lessons-project: {project_id} -->\n\n"
        "## 活跃\n\n"
        f"- **[[lesson:{prefix}-1]] [pending·项目] Existing project rule.** "
        "触发: existing trigger. 代价: existing cost. sink → checks/existing.md.\n\n"
        "## 归档\n",
        encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "seed")
    return repo, tmp_path / "control"


def project_candidate(
    repo: Path, control: Path, suffix: str = "one", *, project_id: str = "sample-app",
    rule: str | None = None, when: str | None = None,
) -> str:
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    return create_candidate(
        repo, control, host="desk", agent="codex",
        rule=rule or f"Project transaction rule {suffix}", trigger=f"project promote {suffix}",
        cost="lost project lesson", sink=f"checks/{suffix}.md",
        scope_hint=f"project:{project_id}", evidence=f"synthetic:{suffix}",
        base_revision=f"{head} unverified", inbox_path=repo / PROJECT_INBOX,
        require_state_freshness=False, allow_project=True, when=when,
    ).stem


def project_host_config(tmp_path: Path, state_root: Path | str = "<STATE>") -> Path:
    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "host.example.json").read_text(encoding="utf-8")
    )
    payload["state_root"] = str(state_root)
    path = tmp_path / "host.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def setup_unified_local(tmp_path: Path) -> tuple[Path, Path, Path]:
    """One Git root containing project and bound-state ledgers for C8 local promotion."""
    repo, _unused = setup_project(tmp_path)
    (repo / "experience").mkdir()
    (repo / "experience" / "LESSONS.md").write_text(
        "# Lessons Ledger\n<!-- next id: L-999 -->\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] Existing global rule.** 触发: existing trigger. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    profile = repo / "experience" / "profiles" / "example-domain"
    profile.mkdir(parents=True)
    (profile / "LESSONS.md").write_text(
        "# Profile Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: example-domain -->\n\n"
        "## 活跃\n\n"
        "- **[[lesson:EXAMPLE-1]] [pending·领域] Existing profile rule.** 触发: existing trigger. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    routing = json.loads((repo / ".agents" / "lessons.json").read_text(encoding="utf-8"))
    routing["profiles"] = ["example-domain"]
    (repo / ".agents" / "lessons.json").write_text(json.dumps(routing, sort_keys=True) + "\n", encoding="utf-8")
    git(repo, "add", "experience", ".agents/lessons.json")
    git(repo, "commit", "-q", "-m", "add state ledger")
    return repo, project_host_config(tmp_path, repo), tmp_path / "legacy-control"


def local_state_candidate(repo: Path, control: Path, suffix: str, *, rule: str,
                          scope_hint: str = "global") -> str:
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    return create_candidate(
        repo, control, host="desk", agent="codex", rule=rule,
        trigger=f"local {suffix}", cost="lost local lesson", sink=f"checks/{suffix}.md",
        scope_hint=scope_hint, evidence=f"synthetic:{suffix}", base_revision=f"{head} unverified",
        require_state_freshness=False,
    ).stem


def setup_monorepo_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Canonical root/state layout with no project routing at the repository root."""
    repo = tmp_path / "private"; repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", f"test{chr(64)}invalid")
    (repo / "engine").mkdir()
    global_ledger = repo / "state" / "experience" / "LESSONS.md"
    global_ledger.parent.mkdir(parents=True)
    global_ledger.write_text(
        "# Lessons Ledger\n<!-- next id: L-999 -->\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] Existing global rule.** 触发: existing trigger. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n", encoding="utf-8")
    profile = repo / "state" / "experience" / "profiles" / "example-domain" / "LESSONS.md"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "# Profile Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: example-domain -->\n\n"
        "## 活跃\n\n- **[[lesson:EXAMPLE-1]] [pending·领域] Profile rule.** "
        "触发: profile trigger. 代价: profile cost. sink → checks/profile.md.\n\n## 归档\n", encoding="utf-8")
    git(repo, "add", "state")
    git(repo, "commit", "-q", "-m", "seed state")
    return repo, project_host_config(tmp_path, repo / "state"), tmp_path / "candidate-control"


def monorepo_state_candidate(repo: Path, control: Path, suffix: str, *, rule: str) -> str:
    return create_candidate(
        repo, control, host="desk", agent="codex", rule=rule,
        trigger=f"state {suffix}", cost="lost state lesson", sink=f"checks/{suffix}.md",
        scope_hint="global", evidence=f"synthetic:{suffix}",
        base_revision=f"{git(repo, 'rev-parse', 'HEAD').stdout.strip()} unverified",
        inbox_path=repo / "state" / "inbox", require_state_freshness=False,
    ).stem


def test_concurrent_inbox_writers_never_overwrite(tmp_path: Path) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda n: candidate(alpha, control, str(n)), range(2)))
    assert len(set(ids)) == 2
    assert all((alpha / "inbox" / f"{item}.md").is_file() for item in ids)


def test_i1_i2_i3_i4_i5_and_rollback(tmp_path: Path) -> None:
    _remote, seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    original = (alpha / "experience" / "LESSONS.md").read_bytes()
    stale_id = candidate(alpha, control, "stale")
    (seed / "advance.txt").write_text("advance", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "advance")
    git(seed, "push", "-q")
    with pytest.raises(ConfigError, match="FAIL_STALE"):
        plan_promote(alpha, control, stale_id, force_new=True)
    assert (alpha / "experience" / "LESSONS.md").read_bytes() == original
    git(alpha, "pull", "-q", "--ff-only")

    bad_base = candidate(alpha, control, "bad-base", base="f" * 40)
    with pytest.raises(ConfigError, match="FAIL_STALE_BASE"):
        plan_promote(alpha, control, bad_base, force_new=True)
    old = git(alpha, "rev-parse", "HEAD~1").stdout.strip()
    old_base = candidate(alpha, control, "old-base", base=old)
    with pytest.raises(ConfigError, match="REVIEW_REQUIRED"):
        plan_promote(alpha, control, old_base, force_new=True)

    lesson_id = candidate(alpha, control, "chosen")
    publish(alpha, control, lesson_id)
    assert not (control / "txn" / "last_committed.json").exists()
    plan = plan_promote(alpha, control, lesson_id, force_new=True, reviewed_against=remote_sha(alpha))
    prepared = prepare_promote(alpha, control, plan, plan.plan_hash, plan.expected_remote_sha)
    result = apply_prepared(prepared)
    ledger = (alpha / "experience" / "LESSONS.md").read_text(encoding="utf-8")
    assert "L-2" in ledger and "L-999" not in ledger.split("Transactional rule chosen")[0].splitlines()[-1]
    assert f"from: {lesson_id}" in ledger
    promoted = next(item for item in parse_markdown(ledger, "global", "LESSONS.md") if item.lesson_id == "L-2")
    assert promoted.sink == "checks/chosen.md"
    assert (alpha / "inbox" / "consumed" / f"{lesson_id}.md").is_file()
    with pytest.raises(ConfigError, match=r"FAIL_ALREADY_PROMOTED L-2"):
        plan_promote(alpha, control, lesson_id, force_new=True)

    rollback_plan = rollback(alpha, control, result.rollback_id, apply=False)
    rollback(alpha, control, result.rollback_id, apply=True,
             plan_hash=rollback_plan.plan_hash, expected_remote_sha=rollback_plan.expected_remote_sha)
    assert (alpha / "experience" / "LESSONS.md").read_bytes() == original


def test_remote_cas_race_and_publish_retry(tmp_path: Path) -> None:
    _remote, _seed, alpha, beta = setup_pair(tmp_path)
    controls = (tmp_path / "control-a", tmp_path / "control-b")
    first = candidate(alpha, controls[0], "first")
    second = candidate(beta, controls[1], "second")
    p1 = plan_publish(alpha, controls[0], first)
    p2 = plan_publish(beta, controls[1], second)
    t1 = prepare_publish(alpha, controls[0], p1, p1.plan_hash, p1.expected_remote_sha)
    t2 = prepare_publish(beta, controls[1], p2, p2.plan_hash, p2.expected_remote_sha)
    apply_prepared(t1)
    apply_prepared(t2, retry_inbox_race=True)
    git(alpha, "fetch", "-q", "origin")
    tree = git(alpha, "ls-tree", "-r", "--name-only", "origin/main").stdout
    assert f"inbox/{first}.md" in tree and f"inbox/{second}.md" in tree

    git(alpha, "pull", "-q", "--ff-only")
    git(beta, "pull", "-q", "--ff-only")
    third = candidate(alpha, controls[0], "third")
    publish(alpha, controls[0], third)
    git(beta, "pull", "-q", "--ff-only")
    promote_a = plan_promote(alpha, controls[0], third, force_new=True, reviewed_against=remote_sha(alpha))
    promote_b = plan_promote(beta, controls[1], third, force_new=True, reviewed_against=remote_sha(beta))
    prepared_a = prepare_promote(alpha, controls[0], promote_a, promote_a.plan_hash, promote_a.expected_remote_sha)
    prepared_b = prepare_promote(beta, controls[1], promote_b, promote_b.plan_hash, promote_b.expected_remote_sha)
    apply_prepared(prepared_a)
    with pytest.raises(ConfigError, match="FAIL_REMOTE_RACE"):
        apply_prepared(prepared_b)


def test_push_is_never_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_core import promote as promote_module
    prepared = promote_module.Prepared(
        repo=tmp_path / "repo", control_root=tmp_path / "control", txn=tmp_path / "txn",
        sha="a" * 40, expected_remote_sha="b" * 40, operation="promote",
        candidate_id="desk-20260811T000000Z-" + "c" * 32, changed_paths=(),
    )
    captured: list[tuple[str, ...]] = []

    def fake_git(_repo: Path, *args: str, **_kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(promote_module, "_git", fake_git)
    assert promote_module._push(prepared).returncode == 0
    assert len(captured) == 1 and captured[0][:2] == ("push", "origin")
    assert "--force" not in captured[0] and "-f" not in captured[0]
    assert not any(argument.startswith("+") for argument in captured[0])


def test_cas_precheck_rejects_moved_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _remote, seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    item = candidate(alpha, control, "precheck")
    plan = plan_publish(alpha, control, item)
    prepared = prepare_publish(alpha, control, plan, plan.plan_hash, plan.expected_remote_sha)
    (seed / "moved.txt").write_text("remote moved", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "move remote")
    git(seed, "push", "-q")
    calls = 0

    def forbidden_push(_prepared) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(("push",), 0, "", "")

    monkeypatch.setattr("agent_core.promote._push", forbidden_push)
    with pytest.raises(ConfigError, match="FAIL_REMOTE_RACE"):
        apply_prepared(prepared)
    assert calls == 0


def test_remote_success_local_stale_is_recoverable_without_second_push(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    item = candidate(alpha, control, "recover")
    plan = plan_publish(alpha, control, item)
    prepared = prepare_publish(alpha, control, plan, plan.plan_hash, plan.expected_remote_sha)
    before = git(alpha, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    from agent_core import promote as promote_module
    original_fast_forward = promote_module.fast_forward_local
    monkeypatch.setattr("agent_core.promote.fast_forward_local", lambda *_args: (_ for _ in ()).throw(RuntimeError("forced")))
    with pytest.raises(ConfigError, match="REMOTE_COMMITTED_LOCAL_STALE") as exc:
        apply_prepared(prepared)
    sha = str(exc.value).split()[-1]
    assert sha != before
    anchor = control / "txn" / "last_committed.json"
    payload = json.loads(anchor.read_text(encoding="utf-8"))
    assert payload == {
        "sha": sha, "operation": "publish", "candidate_id": item, "utc": payload["utc"],
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["utc"])
    remote_before_recovery = git(alpha, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]
    monkeypatch.setattr("agent_core.promote.fast_forward_local", original_fast_forward)
    monkeypatch.setattr("agent_core.promote._push", lambda *_args, **_kwargs: pytest.fail("recover must not push"))
    assert cli_main(["recover", "--state", str(alpha), "--control-root", str(control)]) == 0
    output = capsys.readouterr().out
    assert f"RECOVERY_SOURCE {anchor}" in output
    assert f"PASS local_recovered={sha}" in output
    assert not anchor.exists()
    assert git(alpha, "rev-parse", "HEAD").stdout.strip() == sha
    assert git(alpha, "ls-remote", "origin", "refs/heads/main").stdout.split()[0] == remote_before_recovery


def test_similarity_requires_explicit_decision(tmp_path: Path) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    item = create_candidate(
        alpha, control, host="desk", agent="codex", rule="Existing rule",
        trigger="existing trigger", cost="existing cost", sink="checks/existing.md",
        scope_hint="global", evidence="synthetic:similar",
    ).stem
    publish(alpha, control, item)
    with pytest.raises(ConfigError, match="FAIL_SIMILAR_REVIEW"):
        plan_promote(alpha, control, item, reviewed_against=remote_sha(alpha))
    plan = plan_promote(alpha, control, item, force_new=True, reviewed_against=remote_sha(alpha))
    assert any(line.startswith("SIMILAR L-1") for line in plan.lines)


def test_state_rejects_superseding_active_but_dissimilar_lesson(tmp_path: Path) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    item = candidate(alpha, control, "unrelated")
    publish(alpha, control, item)
    ledger_path = alpha / "experience" / "LESSONS.md"
    before = ledger_path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match=r"FAIL_SUPERSEDES L-1"):
        plan_promote(
            alpha, control, item, supersedes="L-1", reviewed_against=remote_sha(alpha),
        )
    after = ledger_path.read_text(encoding="utf-8")
    active, archived = after.split("## 归档", 1)
    assert after == before
    assert "- **L-1 " in active and "- **L-1 " not in archived


def test_plan_tokens_txn_boundary_and_offline_capture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    item = candidate(alpha, control, "cli")
    assert cli_main(["candidate", "publish", "--state", str(alpha),
                     "--control-root", str(control), "--id", item]) == 0
    output = capsys.readouterr().out
    assert "PLAN_HASH" in output and "EXPECTED_REMOTE_SHA" in output
    plan = plan_publish(alpha, control, item)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        prepare_publish(alpha, control, plan, "0" * 64, plan.expected_remote_sha)
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE"):
        plan_publish(alpha, control, "../outside")
    with pytest.raises(ConfigError, match="FAIL_ROLLBACK"):
        rollback(alpha, control, "../outside", apply=False)
    with pytest.raises(ConfigError, match="FAIL_TXN_PATH"):
        assert_txn_path(control, tmp_path / "outside")
    with operation_lock(control):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            with operation_lock(control):
                pass

    git(alpha, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    offline = candidate(alpha, control, "offline")
    payload = (alpha / "inbox" / f"{offline}.md").read_text(encoding="utf-8")
    assert "unverified" in payload
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        plan_publish(alpha, control, offline)


@pytest.mark.parametrize("flag", ["--plan", "--dry-run"])
@pytest.mark.parametrize("command", [
    ["candidate", "publish", "--id", "synthetic"],
    ["promote", "--id", "synthetic"],
    ["rollback", "--to", "synthetic"],
])
def test_state_transaction_dead_plan_flags_are_removed(
    command: list[str], flag: str, tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli_main([*command, "--state", str(tmp_path / "state"), flag])


def test_profile_ids_and_supersedes_are_applied_to_the_selected_ledger(tmp_path: Path) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    profile_item = create_candidate(
        alpha, control, host="desk", agent="codex", rule="Second profile rule",
        trigger="profile promotion", cost="profile collision", sink="checks/profile-two.md",
        scope_hint="profile:example-domain", evidence="synthetic:profile",
    ).stem
    publish(alpha, control, profile_item)
    profile_plan = plan_promote(
        alpha, control, profile_item, force_new=True, reviewed_against=remote_sha(alpha))
    apply_prepared(prepare_promote(
        alpha, control, profile_plan, profile_plan.plan_hash, profile_plan.expected_remote_sha))
    profile_text = (alpha / "experience" / "profiles" / "example-domain" / "LESSONS.md").read_text(encoding="utf-8")
    assert "[[lesson:EXAMPLE-2]]" in profile_text

    replacement = create_candidate(
        alpha, control, host="desk", agent="codex", rule="Existing rule",
        trigger="existing trigger", cost="existing cost", sink="checks/existing.md",
        scope_hint="global", evidence="synthetic:supersedes",
    ).stem
    publish(alpha, control, replacement)
    replacement_plan = plan_promote(
        alpha, control, replacement, supersedes="L-1", reviewed_against=remote_sha(alpha))
    apply_prepared(prepare_promote(
        alpha, control, replacement_plan, replacement_plan.plan_hash,
        replacement_plan.expected_remote_sha))
    global_text = (alpha / "experience" / "LESSONS.md").read_text(encoding="utf-8")
    active, archived = global_text.split("## 归档", 1)
    assert "- **L-1 " not in active and "- **L-1 " in archived
    assert "supersedes: L-1" in active and "superseded_by: L-2" in archived


def test_project_promote_moves_untracked_candidate_and_preserves_unrelated_index(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "untracked")
    source = repo / PROJECT_INBOX / f"{item}.md"
    original_candidate = source.read_bytes()
    (repo / "unrelated.txt").write_text("keep staged\n", encoding="utf-8")
    git(repo, "add", "unrelated.txt")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    calls: list[tuple[str, ...]] = []
    from agent_core import promote as promote_module
    original_git = promote_module._git

    def recording_git(path: Path, *args: str, **kwargs):
        calls.append(args)
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(promote_module, "_git", recording_git)
    plan = plan_project_promote(repo, control, item)
    result = apply_project_promote(repo, control, plan, plan.plan_hash)
    consumed = repo / PROJECT_CONSUMED / f"{item}.md"
    assert result.lesson_id == "SAMPLE-2"
    assert not source.exists() and consumed.read_bytes() == original_candidate
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    staged = set(git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
    assert {".agents/LESSONS.md", consumed.relative_to(repo).as_posix(), "unrelated.txt"} <= staged
    assert source.relative_to(repo).as_posix() not in staged
    assert f"from: {item}" in (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    assert not any(args and args[0] in {"commit", "push", "fetch", "reset"} for args in calls)


def test_project_promote_renders_candidate_v2_when_at_line_tail(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    when = '{"paths":["agent_core/**"]}'
    item = project_candidate(repo, control, "predicate", when=when)
    candidate = load_candidate(repo / PROJECT_INBOX / f"{item}.md", allow_project=True)
    assert candidate["schema"] == "candidate/2" and candidate["when"] == when

    plan = plan_project_promote(repo, control, item)
    assert apply_project_promote(repo, control, plan, plan.plan_hash).lesson_id == "SAMPLE-2"
    rendered = (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    assert rendered.endswith(f"sink → checks/predicate.md. when: {when}\n\n## 归档\n")
    assert parse_markdown(rendered, "project", str(PROJECT_LEDGER))[-1].when == {
        "paths": ("agent_core/**",),
    }


def test_project_promote_rejects_noncanonical_candidate_v2_when(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "invalid-when", when='{"paths":["agent_core/**"]}')
    candidate_path = repo / PROJECT_INBOX / f"{item}.md"
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["when"] = '{"paths": ["agent_core/**"]}'
    candidate_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="candidate when: when must be canonical JSON"):
        plan_project_promote(repo, control, item)


def test_project_promote_stages_tracked_source_deletion(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "tracked")
    source_relative = (PROJECT_INBOX / f"{item}.md").as_posix()
    git(repo, "add", source_relative)
    git(repo, "commit", "-q", "-m", "track candidate")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    plan = plan_project_promote(repo, control, item)
    apply_project_promote(repo, control, plan, plan.plan_hash)
    status = git(repo, "diff", "--cached", "--name-status", "--no-renames").stdout
    assert f"D\t{source_relative}" in status
    assert f"A\t{(PROJECT_CONSUMED / f'{item}.md').as_posix()}" in status
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_project_promote_cli_requires_and_applies_exact_plan_hash(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "cli")
    args = ["lessons", "promote", "--workspace", str(repo),
            "--config", str(project_host_config(tmp_path)), "--id", item,
            "--force-new"]
    canonical = repo / PROJECT_LEDGER
    candidate_path = repo / PROJECT_INBOX / f"{item}.md"
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (canonical, candidate_path)
    }
    assert cli_main(args) == 0
    output = capsys.readouterr().out
    plan_hash = next(line.split()[1] for line in output.splitlines()
                     if line.startswith("PLAN_HASH "))
    assert "CANDIDATE_SHA256" in output and "CANONICAL_SHA256" in output
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == value
               for path, value in before.items())
    assert not control.exists()
    with pytest.raises(SystemExit, match="2"):
        cli_main(args + ["--plan"])
    assert cli_main(args + ["--apply", "--plan-hash", "0" * 64]) == 1
    assert "FAIL_INPUT_CHANGED" in capsys.readouterr().err
    assert cli_main(args + ["--apply", "--plan-hash", plan_hash]) == 0
    output = capsys.readouterr().out
    assert "PASS project_promoted=SAMPLE-2" in output


def test_project_promote_cli_reports_concrete_state_cross_scope_advisory(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, control = setup_project(tmp_path)
    _remote, _seed, state, _beta = setup_pair(tmp_path / "state")
    item = project_candidate(repo, control, "config-cross-scope", rule="Existing rule.")
    ledger_path = repo / PROJECT_LEDGER
    source = repo / PROJECT_INBOX / f"{item}.md"
    before = (ledger_path.read_bytes(), source.read_bytes(),
              git(repo, "diff", "--cached", "--binary").stdout)
    assert project_promote_main([
        "--workspace", str(repo),
        "--config", str(project_host_config(tmp_path, state)), "--id", item, "--force-new",
    ]) == 0
    assert "EXACT scope=global store=global id=L-1" in capsys.readouterr().out
    assert (ledger_path.read_bytes(), source.read_bytes(),
            git(repo, "diff", "--cached", "--binary").stdout) == before


def test_project_promote_cli_rejects_concrete_state_without_global_ledger(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "missing-configured-global")
    state = tmp_path / "concrete-state"
    state.mkdir()
    git(state, "init", "-q", "-b", "main")
    ledger_path = repo / PROJECT_LEDGER
    source = repo / PROJECT_INBOX / f"{item}.md"
    before = (ledger_path.read_bytes(), source.read_bytes(),
              git(repo, "diff", "--cached", "--binary").stdout)
    assert project_promote_main([
        "--workspace", str(repo), "--control-root", str(control),
        "--config", str(project_host_config(tmp_path, state)), "--id", item, "--force-new",
    ]) == 1
    assert "FAIL_LESSON_ROUTING" in capsys.readouterr().err
    assert (ledger_path.read_bytes(), source.read_bytes(),
            git(repo, "diff", "--cached", "--binary").stdout) == before


def test_project_exact_duplicate_rejects_create_and_force_new_without_writes(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "similar", rule="Existing project rule.")
    before = (repo / PROJECT_LEDGER).read_bytes()
    with pytest.raises(ConfigError, match=r"FAIL_EXACT_DUPLICATE project:sample-app:SAMPLE-1"):
        plan_project_promote(repo, control, item)
    with pytest.raises(ConfigError, match=r"FAIL_EXACT_DUPLICATE project:sample-app:SAMPLE-1"):
        plan_project_promote(repo, control, item, force_new=True)
    assert (repo / PROJECT_LEDGER).read_bytes() == before
    assert (repo / PROJECT_INBOX / f"{item}.md").is_file()


def test_project_exact_rule_is_case_and_punctuation_sensitive_but_nfc_whitespace_exact(
        tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    for suffix, rule in (("case", "existing project rule."), ("punctuation", "Existing project rule")):
        item = project_candidate(repo, control, suffix, rule=rule)
        assert plan_project_promote(repo, control, item).candidate_id == item

    ledger_path = repo / PROJECT_LEDGER
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").replace("Existing project rule.", "Å  rule."),
        encoding="utf-8",
    )
    item = project_candidate(repo, control, "nfc-space", rule="A\u030a\t rule.")
    with pytest.raises(ConfigError, match=r"FAIL_EXACT_DUPLICATE project:sample-app:SAMPLE-1"):
        plan_project_promote(repo, control, item)


def test_project_promote_choice_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit, match="2"):
        project_promote_main([
            "--id", "synthetic", "--update", "SAMPLE-1", "--supersedes", "SAMPLE-2",
        ])


def test_local_promote_placeholder_config_uses_install_lock_and_cli_rejects_bare_update(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, legacy_control = setup_project(tmp_path)
    config = project_host_config(tmp_path)
    item = project_candidate(repo, legacy_control, "placeholder-lock")
    plan = plan_local_promote(repo, None, item, force_new=True, config_path=config)
    assert plan.payload["control_root"] == str((config.parent / "txn").resolve())
    before = ((repo / PROJECT_LEDGER).read_bytes(),
              (repo / PROJECT_INBOX / f"{item}.md").read_bytes(),
              git(repo, "diff", "--cached", "--binary").stdout)
    assert project_promote_main([
        "--workspace", str(repo), "--config", str(config), "--id", item,
        "--update", "SAMPLE-1",
    ]) == 1
    assert "FAIL_UPDATE_TARGET" in capsys.readouterr().err
    assert ((repo / PROJECT_LEDGER).read_bytes(),
            (repo / PROJECT_INBOX / f"{item}.md").read_bytes(),
            git(repo, "diff", "--cached", "--binary").stdout) == before


def test_project_update_preserves_identity_and_consumes_candidate(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "update", rule="Rewritten distinct project rule")
    before = (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    plan = plan_project_promote(repo, control, item, update="SAMPLE-1")
    assert plan.payload["lesson_id"] == "SAMPLE-1" and plan.payload["update"] == "SAMPLE-1"
    result = apply_project_promote(repo, control, plan, plan.plan_hash)
    after = (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    assert result.lesson_id == "SAMPLE-1"
    assert after.count("[[lesson:SAMPLE-1]]") == 1 and "Rewritten distinct project rule" in after
    assert "Existing project rule" not in after and before.count("[[lesson:SAMPLE-1]]") == 1
    assert not (repo / PROJECT_INBOX / f"{item}.md").exists()
    assert (repo / PROJECT_CONSUMED / f"{item}.md").is_file()
    assert set(git(repo, "diff", "--cached", "--name-only").stdout.splitlines()) == {
        ".agents/LESSONS.md", f".agents/inbox/consumed/{item}.md",
    }


def test_project_update_rejects_missing_or_archived_target_before_writes(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "bad-update")
    before = (repo / PROJECT_LEDGER).read_bytes()
    with pytest.raises(ConfigError, match="FAIL_UPDATE_TARGET"):
        plan_project_promote(repo, control, item, update="SAMPLE-404")
    assert (repo / PROJECT_LEDGER).read_bytes() == before

    text = (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    target = next(line for line in text.splitlines() if "[[lesson:SAMPLE-1]]" in line)
    active, archived = text.split("\n## 归档\n", 1)
    (repo / PROJECT_LEDGER).write_text(
        active.replace(target + "\n", "") + "\n## 归档\n" + target + "\n" + archived,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="FAIL_UPDATE_TARGET"):
        plan_project_promote(repo, control, item, update="SAMPLE-1")


@pytest.mark.parametrize("involved", ("ledger", "source", "consumed"))
def test_project_involved_index_conflict_fails_before_write(tmp_path: Path, involved: str) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "index-conflict")
    ledger_path = repo / PROJECT_LEDGER
    source = repo / PROJECT_INBOX / f"{item}.md"
    consumed = repo / PROJECT_CONSUMED / source.name
    source_relative = source.relative_to(repo).as_posix()
    git(repo, "add", source_relative)
    git(repo, "commit", "-q", "-m", "track candidate")
    target = {"ledger": ledger_path, "source": source, "consumed": consumed}[involved]
    if involved == "ledger":
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        git(repo, "add", target.relative_to(repo).as_posix())
    elif involved == "consumed":
        target.parent.mkdir(parents=True)
        target.write_text("staged destination\n", encoding="utf-8")
        git(repo, "add", target.relative_to(repo).as_posix())
    elif involved == "source":
        git(repo, "rm", "--cached", "--", source_relative)
        assert git(repo, "diff", "--cached", "--name-status", "--no-renames", "--", source_relative).stdout.strip() == (
            f"D\t{source_relative}"
        )
    before = (
        ledger_path.exists(), ledger_path.read_bytes(), source.exists(), source.read_bytes(),
        consumed.exists(), consumed.read_bytes() if consumed.exists() else None,
        git(repo, "diff", "--cached", "--binary").stdout,
    )
    with pytest.raises(ConfigError, match="FAIL_INDEX_CONFLICT"):
        plan_project_promote(repo, control, item)
    assert (
        ledger_path.exists(), ledger_path.read_bytes(), source.exists(), source.read_bytes(),
        consumed.exists(), consumed.read_bytes() if consumed.exists() else None,
        git(repo, "diff", "--cached", "--binary").stdout,
    ) == before


@pytest.mark.parametrize("involved", ("ledger", "source", "consumed"))
def test_project_apply_rechecks_involved_index_before_any_write(
        tmp_path: Path, involved: str) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "apply-index")
    source = repo / PROJECT_INBOX / f"{item}.md"
    source_relative = source.relative_to(repo).as_posix()
    git(repo, "add", source_relative)
    git(repo, "commit", "-q", "-m", "track candidate")
    plan = plan_project_promote(repo, control, item)
    ledger_path = repo / PROJECT_LEDGER
    consumed = repo / PROJECT_CONSUMED / source.name
    target = {"ledger": ledger_path, "source": source, "consumed": consumed}[involved]
    if involved == "consumed":
        target.parent.mkdir(parents=True)
        target.write_text("staged destination\n", encoding="utf-8")
        git(repo, "add", target.relative_to(repo).as_posix())
    elif involved == "source":
        git(repo, "rm", "--cached", "--", source_relative)
        assert git(repo, "diff", "--cached", "--name-status", "--no-renames", "--", source_relative).stdout.strip() == (
            f"D\t{source_relative}"
        )
    else:
        target.write_bytes(target.read_bytes() + b"\n")
        git(repo, "add", target.relative_to(repo).as_posix())
    before = (ledger_path.exists(), ledger_path.read_bytes(), source.exists(), source.read_bytes(), consumed.exists(),
              consumed.read_bytes() if consumed.exists() else None,
              git(repo, "diff", "--cached", "--binary").stdout)
    with pytest.raises(ConfigError, match="FAIL_INDEX_CONFLICT"):
        apply_project_promote(repo, control, plan, plan.plan_hash)
    assert (ledger_path.exists(), ledger_path.read_bytes(), source.exists(), source.read_bytes(), consumed.exists(),
            consumed.read_bytes() if consumed.exists() else None,
            git(repo, "diff", "--cached", "--binary").stdout) == before


@pytest.mark.parametrize("choice", ({}, {"force_new": True}, {"update": "SAMPLE-1"}))
def test_project_cross_scope_exact_rule_requires_review(tmp_path: Path, choice: dict[str, object]) -> None:
    repo, control = setup_project(tmp_path)
    _remote, _seed, state, _beta = setup_pair(tmp_path / "state")
    item = project_candidate(repo, control, "cross-scope", rule="Existing rule.")
    before = (repo / PROJECT_LEDGER).read_bytes()
    with pytest.raises(ConfigError, match="FAIL_SCOPE_REVIEW"):
        plan_project_promote(repo, control, item, state_root=state, **choice)
    assert (repo / PROJECT_LEDGER).read_bytes() == before


def test_project_cross_scope_fuzzy_only_is_advisory(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    _remote, _seed, state, _beta = setup_pair(tmp_path / "state")
    global_ledger = state / "experience" / "LESSONS.md"
    global_ledger.write_text(
        global_ledger.read_text(encoding="utf-8").replace("Existing rule.", "alpha beta gamma."),
        encoding="utf-8",
    )
    item = project_candidate(repo, control, "cross-fuzzy", rule="alpha beta gamma delta")
    plan = plan_project_promote(repo, control, item, state_root=state, force_new=True)
    assert plan.candidate_id == item
    assert "SIMILAR scope=global store=global id=L-1 score=0.750" in plan.lines


def test_project_create_round_trip_preserves_rule_identity(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    rule = "No terminal punctuation exact identity"
    first = project_candidate(repo, control, "round-trip-first", rule=rule)
    plan = plan_project_promote(repo, control, first)
    apply_project_promote(repo, control, plan, plan.plan_hash)
    git(repo, "commit", "-q", "-m", "promote no-punctuation rule")
    assert f"{rule}**" in (repo / PROJECT_LEDGER).read_text(encoding="utf-8")

    second = project_candidate(repo, control, "round-trip-second", rule=rule)
    with pytest.raises(ConfigError, match="FAIL_EXACT_DUPLICATE"):
        plan_project_promote(repo, control, second)
    punctuated = project_candidate(repo, control, "round-trip-punctuation", rule=f"{rule}.")
    assert plan_project_promote(repo, control, punctuated).candidate_id == punctuated


def test_project_update_does_not_exclude_same_id_in_profile_store(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    _remote, _seed, state, _beta = setup_pair(tmp_path / "state")
    profile = state / "experience" / "profiles" / "sample-app"
    profile.mkdir(parents=True)
    (profile / "LESSONS.md").write_text(
        "# Profile\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: sample-app -->\n\n## 活跃\n\n"
        "- **[[lesson:SAMPLE-1]] [pending·领域] Profile exact rule** 触发: test. "
        "代价: test. sink → checks/profile.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    routing = repo / ".agents" / "lessons.json"
    payload = json.loads(routing.read_text(encoding="utf-8"))
    payload["profiles"] = ["sample-app"]
    routing.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    item = project_candidate(repo, control, "same-id-profile", rule="Profile exact rule")
    with pytest.raises(ConfigError, match=r"FAIL_SCOPE_REVIEW profile:sample-app:SAMPLE-1"):
        plan_project_promote(repo, control, item, update="SAMPLE-1", state_root=state)


def test_project_update_target_drift_is_rechecked_under_lock(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "target-drift")
    plan = plan_project_promote(repo, control, item, update="SAMPLE-1")
    ledger_path = repo / PROJECT_LEDGER
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").replace("- **[[lesson:SAMPLE-1]]", "- **[[lesson:SAMPLE-1-ARCHIVED]]"),
        encoding="utf-8",
    )
    before = (ledger_path.read_bytes(), (repo / PROJECT_INBOX / f"{item}.md").read_bytes())
    with pytest.raises(ConfigError, match="FAIL_UPDATE_TARGET"):
        apply_project_promote(repo, control, plan, plan.plan_hash)
    assert (ledger_path.read_bytes(), (repo / PROJECT_INBOX / f"{item}.md").read_bytes()) == before


def test_project_rejects_superseding_active_but_dissimilar_lesson(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "unrelated")
    ledger_path = repo / PROJECT_LEDGER
    before = ledger_path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match=r"FAIL_SUPERSEDES SAMPLE-1"):
        plan_project_promote(repo, control, item, supersedes="SAMPLE-1")
    after = ledger_path.read_text(encoding="utf-8")
    active, archived = after.split("## 归档", 1)
    assert after == before
    assert "[[lesson:SAMPLE-1]]" in active and "[[lesson:SAMPLE-1]]" not in archived


def test_archive_superseded_rejects_missing_active_id_without_mutation() -> None:
    text = (
        "# Lessons\n\n## Active\n\n"
        "- **L-1 [pending·通用] Active rule.** 触发: test. 代价: test. sink → checks/test.md.\n\n"
        "## Archived\n"
    )
    before = text
    with pytest.raises(ConfigError, match=r"FAIL_SUPERSEDES L-404"):
        _archive_superseded(text, "L-404", "L-2")
    assert text == before


def test_project_plan_rejects_scope_identity_mismatch(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "plan-mismatch", project_id="other-app")
    with pytest.raises(ConfigError, match=r"FAIL_PROJECT_MISMATCH phase=plan"):
        plan_project_promote(repo, control, item)


def test_project_promote_rejects_missing_identity_without_fallback(tmp_path: Path) -> None:
    repo = tmp_path / "unconfigured"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", f"user.email=test{chr(64)}invalid",
        "commit", "-q", "-m", "seed")
    candidate_id = "desk-20260812T000000Z-" + "a" * 32
    with pytest.raises(ConfigError, match=r"REJECTED scope project_identity_unavailable"):
        plan_project_promote(repo, tmp_path / "control", candidate_id)


def test_project_apply_rechecks_scope_identity_independently(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "apply-mismatch")
    plan = plan_project_promote(repo, control, item)
    config_path = repo / ".agents" / "lessons.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["project_id"] = "other-app"
    config_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"FAIL_PROJECT_MISMATCH phase=apply"):
        apply_project_promote(repo, control, plan, plan.plan_hash)


def test_state_candidate_validation_stays_fail_closed_for_project_scope(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "state-closed")
    path = repo / PROJECT_INBOX / f"{item}.md"
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE"):
        load_candidate(path)
    assert load_candidate(path, allow_project=True)["scope_hint"] == "project:sample-app"

    _remote, _seed, state, _beta = setup_pair(tmp_path / "state-case")
    state_head = git(state, "rev-parse", "HEAD").stdout.strip()
    state_item = create_candidate(
        state, control, host="desk", agent="codex", rule="Wrong-store project candidate",
        trigger="state publish", cost="scope pollution", sink="checks/state.md",
        scope_hint="project:sample-app", evidence="synthetic:state-closed",
        base_revision=f"{state_head} unverified", inbox_path=state / "inbox",
        require_state_freshness=False, allow_project=True,
    ).stem
    with pytest.raises(ConfigError, match="FAIL_DIRTY"):
        plan_publish(state, control, state_item)


def test_project_promote_lock_rejects_concurrent_apply(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "locked")
    plan = plan_project_promote(repo, control, item)
    from agent_core import promote as promote_module
    original_atomic = promote_module._atomic_write_text
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_first(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        original_atomic(path, text)

    monkeypatch.setattr(promote_module, "_atomic_write_text", slow_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply_project_promote, repo, control, plan, plan.plan_hash)
        assert entered.wait(5)
        second = pool.submit(apply_project_promote, repo, control, plan, plan.plan_hash)
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).lesson_id == "SAMPLE-2"


@pytest.mark.parametrize("changed", ["candidate", "canonical"])
def test_project_apply_binds_candidate_and_canonical_hashes(
        tmp_path: Path, changed: str) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, f"hash-{changed}")
    plan = plan_project_promote(repo, control, item)
    path = (repo / PROJECT_INBOX / f"{item}.md") if changed == "candidate" else repo / PROJECT_LEDGER
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_project_promote(repo, control, plan, plan.plan_hash)


def test_project_promote_atomic_failure_keeps_canonical_and_candidate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "atomic")
    source = repo / PROJECT_INBOX / f"{item}.md"
    canonical = repo / PROJECT_LEDGER
    before = canonical.read_bytes()
    plan = plan_project_promote(repo, control, item)

    def fail_atomic(_path: Path, _text: str) -> None:
        raise OSError("injected atomic failure")

    monkeypatch.setattr("agent_core.promote._atomic_write_text", fail_atomic)
    with pytest.raises(OSError, match="injected atomic failure"):
        apply_project_promote(repo, control, plan, plan.plan_hash)
    assert canonical.read_bytes() == before
    assert source.is_file()
    assert not (repo / PROJECT_CONSUMED / source.name).exists()


def test_project_promote_never_calls_state_cas_functions(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "no-cas")
    plan = plan_project_promote(repo, control, item)

    def forbidden(*_args, **_kwargs):
        pytest.fail("project promotion must not call state freshness or remote CAS")

    monkeypatch.setattr("agent_core.promote.require_fresh", forbidden)
    monkeypatch.setattr("agent_core.promote.plan_promote", forbidden)
    monkeypatch.setattr("agent_core.promote.prepare_promote", forbidden)
    monkeypatch.setattr("agent_core.promote._push", forbidden)
    assert apply_project_promote(repo, control, plan, plan.plan_hash).lesson_id == "SAMPLE-2"


def test_project_promote_ignores_remote_advancement(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    remote = tmp_path / "project.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", f"test{chr(64)}invalid")
    item = project_candidate(repo, control, "remote-advanced")
    plan = plan_project_promote(repo, control, item)
    (other / "remote.txt").write_text("advanced\n", encoding="utf-8")
    git(other, "add", "remote.txt")
    git(other, "commit", "-q", "-m", "advance remote")
    git(other, "push", "-q")
    assert apply_project_promote(repo, control, plan, plan.plan_hash).lesson_id == "SAMPLE-2"


def test_local_promote_global_scoped_update_moves_candidate_under_install_lock(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "global-update", rule="Replacement global rule")
    source_relative = f"inbox/{item}.md"
    git(repo, "add", source_relative)
    git(repo, "commit", "-q", "-m", "track local candidate")
    plan = plan_local_promote(
        repo, None, item, scope_override="global", update="global:global:L-1",
        state_root=repo, config_path=config,
    )
    assert plan.payload["target_scope"] == "global"
    assert plan.payload["action"] == "update"
    assert plan.payload["control_root"] == str((config.parent / "txn").resolve())
    result = apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    source = repo / "inbox" / f"{item}.md"
    consumed = repo / "inbox" / "consumed" / f"{item}.md"
    assert result.lesson_id == "L-1" and not source.exists() and consumed.is_file()
    assert f"from: {item}." in (repo / "experience" / "LESSONS.md").read_text(encoding="utf-8")
    assert set(result.changed_paths) == {"experience/LESSONS.md", f"inbox/consumed/{item}.md", source_relative}
    assert f"D\t{source_relative}" in git(repo, "diff", "--cached", "--name-status", "--no-renames").stdout


def test_local_promote_same_repository_override_and_cross_repository_rejection(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = project_candidate(repo, control, "same-root", rule="Project rule promoted globally")
    plan = plan_local_promote(
        repo, None, item, scope_override="global", force_new=True,
        state_root=repo, config_path=config,
    )
    assert plan.payload["source_kind"] == "project" and plan.payload["target_scope"] == "global"
    result = apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert (repo / PROJECT_CONSUMED / f"{item}.md").is_file()
    assert "experience/LESSONS.md" in result.changed_paths

    cross_root = tmp_path / "cross"; cross_root.mkdir()
    other, other_control = setup_project(cross_root)
    bound_root = tmp_path / "bound-state"; bound_root.mkdir()
    _remote, _seed, state, _beta = setup_pair(bound_root)
    cross_item = project_candidate(other, other_control, "cross-root")
    before = (other / PROJECT_LEDGER).read_bytes(), (other / PROJECT_INBOX / f"{cross_item}.md").read_bytes()
    with pytest.raises(ConfigError, match="FAIL_SCOPE_REPOSITORY"):
        plan_local_promote(
            other, None, cross_item, scope_override="global", force_new=True,
            state_root=state, config_path=project_host_config(cross_root, state),
        )
    assert ((other / PROJECT_LEDGER).read_bytes(), (other / PROJECT_INBOX / f"{cross_item}.md").read_bytes()) == before


def test_local_promote_global_to_declared_profile_override_binds_scope(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "profile", rule="Global source promoted to profile")
    global_plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                                     state_root=repo, config_path=config)
    profile_plan = plan_local_promote(repo, None, item, scope_override="profile:example-domain", force_new=True,
                                      state_root=repo, config_path=config)
    assert global_plan.plan_hash != profile_plan.plan_hash
    assert profile_plan.payload["target_scope"] == "profile"
    result = apply_local_promote(repo, None, profile_plan, profile_plan.plan_hash, config_path=config)
    assert result.lesson_id == "EXAMPLE-2"
    assert (repo / "experience" / "profiles" / "example-domain" / "LESSONS.md").read_text(encoding="utf-8").count(f"from: {item}.") == 1


def test_local_promote_scoped_project_update_accepts_no_fuzzy_shortlist(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = project_candidate(repo, control, "project-update", rule="Distinct scoped replacement")
    plan = plan_local_promote(
        repo, None, item, scope_override="project:sample-app",
        update="project:sample-app:SAMPLE-1", state_root=repo, config_path=config,
    )
    assert plan.payload["update"] == "SAMPLE-1"
    assert plan.payload["choice"] == "update"
    assert not any(line.startswith("SIMILAR ") for line in plan.lines)
    assert apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config).lesson_id == "SAMPLE-1"


def test_local_promote_blocks_target_exact_and_advises_cross_layer_exact(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    target_item = local_state_candidate(repo, control, "target-exact", rule="Existing global rule.")
    with pytest.raises(ConfigError, match="FAIL_EXACT_DUPLICATE"):
        plan_local_promote(repo, None, target_item, scope_override="global", force_new=True,
                           state_root=repo, config_path=config)

    cross_item = project_candidate(repo, control, "cross-advisory", rule="Existing global rule.")
    plan = plan_local_promote(repo, None, cross_item, scope_override="project:sample-app", force_new=True,
                              state_root=repo, config_path=config)
    assert "EXACT scope=global store=global id=L-1" in plan.lines
    assert apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config).lesson_id == "SAMPLE-2"


def test_local_promote_rerun_converges_after_canonical_write_fault(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "converge", rule="Convergent global rule")
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo, config_path=config)

    def interrupt_after_canonical() -> None:
        raise OSError("injected post-write interruption")

    monkeypatch.setattr("agent_core.promote._after_local_promote_canonical_write", interrupt_after_canonical)
    with pytest.raises(OSError, match="post-write"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert (repo / "inbox" / f"{item}.md").is_file()
    monkeypatch.setattr("agent_core.promote._after_local_promote_canonical_write", lambda: None)
    converged = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                                   state_root=repo, config_path=config)
    assert converged.payload["action"] == "converge"
    assert apply_local_promote(repo, None, converged, converged.plan_hash, config_path=config).lesson_id == "L-2"
    text = (repo / "experience" / "LESSONS.md").read_text(encoding="utf-8")
    assert text.count(f"from: {item}.") == 1


def test_local_promote_never_calls_frozen_remote_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "no-remote", rule="Local only rule")

    def forbidden(*_args, **_kwargs):
        pytest.fail("local lessons promote must not call a remote transaction path")

    monkeypatch.setattr("agent_core.promote.require_fresh", forbidden)
    monkeypatch.setattr("agent_core.promote.plan_promote", forbidden)
    monkeypatch.setattr("agent_core.promote.prepare_promote", forbidden)
    monkeypatch.setattr("agent_core.promote._push", forbidden)
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo, config_path=config)
    assert apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config).lesson_id == "L-2"


def test_local_promote_uses_monorepo_state_root_without_project_routing(tmp_path: Path) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    item = monorepo_state_candidate(repo, control, "root-global", rule="Root state update")
    plan = plan_local_promote(
        repo, None, item, scope_override="global", update="global:global:L-1",
        state_root=repo / "state", config_path=config,
    )
    assert plan.payload["operation_root"] == str(repo.resolve())
    assert plan.payload["source_path"] == f"state/inbox/{item}.md"
    assert plan.payload["target_path"] == "state/experience/LESSONS.md"
    result = apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert result.lesson_id == "L-1"
    assert (repo / "state" / "inbox" / "consumed" / f"{item}.md").is_file()


def test_local_promote_external_routing_selects_monorepo_profile(tmp_path: Path) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    workspace_root = tmp_path / "workspace"; workspace_root.mkdir()
    workspace, _legacy_control = setup_project(workspace_root)
    routing_path = workspace / ".agents" / "lessons.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["profiles"] = ["example-domain"]
    routing_path.write_text(json.dumps(routing, sort_keys=True) + "\n", encoding="utf-8")
    item = monorepo_state_candidate(repo, control, "external-routing", rule="Routed state profile")
    plan = plan_local_promote(
        workspace, None, item, scope_override="profile:example-domain", force_new=True,
        state_root=repo / "state", config_path=config,
    )
    assert plan.payload["operation_root"] == str(repo.resolve())
    assert plan.payload["target_path"] == "state/experience/profiles/example-domain/LESSONS.md"


def test_local_promote_rejects_rendered_ledger_errors_before_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    item = monorepo_state_candidate(repo, control, "render-errors", rule="Reject invalid render")
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo / "state", config_path=config)
    before = (repo / "state" / "experience" / "LESSONS.md").read_bytes()
    from agent_core import ledger as ledger_module
    original_parse = ledger_module.parse_ledger
    calls = 0

    def render_only_error(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {}, ["invalid"], []
        return original_parse(*args, **kwargs)

    monkeypatch.setattr("agent_core.promote.ledger.parse_ledger", render_only_error)
    with pytest.raises(ConfigError, match="FAIL_LEDGER"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert (repo / "state" / "experience" / "LESSONS.md").read_bytes() == before
    assert (repo / "state" / "inbox" / f"{item}.md").is_file()


def test_local_promote_rejects_nested_state_root_before_write_or_stage(tmp_path: Path) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    item = monorepo_state_candidate(repo, control, "nested", rule="Nested root must reject")
    nested = repo / "state" / "nested"; nested.mkdir()
    ledger_path = repo / "state" / "experience" / "LESSONS.md"
    before = (ledger_path.read_bytes(), git(repo, "diff", "--cached", "--binary").stdout)
    with pytest.raises(ConfigError, match="FAIL_SCOPE_REPOSITORY"):
        plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                           state_root=nested, config_path=config)
    assert (ledger_path.read_bytes(), git(repo, "diff", "--cached", "--binary").stdout) == before


def test_local_promote_rejects_candidate_drift_and_binds_cross_exact_facts(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = project_candidate(repo, control, "drift", rule="Cross layer fact.")
    plan = plan_local_promote(repo, None, item, scope_override="project:sample-app", force_new=True,
                              state_root=repo, config_path=config)
    candidate_path = repo / PROJECT_INBOX / f"{item}.md"
    payload = json.loads(candidate_path.read_text(encoding="utf-8")); payload["rule"] = "Changed after review"
    candidate_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    before = (repo / PROJECT_LEDGER).read_bytes()
    with pytest.raises(ConfigError, match="FAIL_INPUT_CHANGED"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert (repo / PROJECT_LEDGER).read_bytes() == before

    payload["rule"] = "Cross layer fact."
    candidate_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    initial = plan_local_promote(repo, None, item, scope_override="project:sample-app", force_new=True,
                                 state_root=repo, config_path=config)
    global_ledger = repo / "experience" / "LESSONS.md"
    global_ledger.write_text(
        global_ledger.read_text(encoding="utf-8").replace("Existing global rule.", "Cross layer fact."),
        encoding="utf-8",
    )
    changed = plan_local_promote(repo, None, item, scope_override="project:sample-app", force_new=True,
                                 state_root=repo, config_path=config)
    assert initial.plan_hash != changed.plan_hash


def test_local_promote_rejects_existing_consumed_before_canonical_write(tmp_path: Path) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    item = monorepo_state_candidate(repo, control, "collision", rule="Consumed collision")
    source = repo / "state" / "inbox" / f"{item}.md"
    consumed = repo / "state" / "inbox" / "consumed" / source.name
    consumed.parent.mkdir(); consumed.write_bytes(source.read_bytes())
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo / "state", config_path=config)
    ledger_path = repo / "state" / "experience" / "LESSONS.md"; before = ledger_path.read_bytes()
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE_STATE"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert ledger_path.read_bytes() == before and source.is_file() and consumed.is_file()


def test_local_promote_converges_after_git_add_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, config, control = setup_monorepo_state(tmp_path)
    item = monorepo_state_candidate(repo, control, "add-failure", rule="Retry after git add")
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo / "state", config_path=config)
    from agent_core import promote as promote_module
    original_git = promote_module._git

    def fail_add(path: Path, *args: str, **kwargs):
        if args and args[0] == "add":
            raise ConfigError("FAIL_GIT", "injected add failure")
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(promote_module, "_git", fail_add)
    with pytest.raises(ConfigError, match="injected add failure"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    consumed = repo / "state" / "inbox" / "consumed" / f"{item}.md"
    assert consumed.is_file() and not (repo / "state" / "inbox" / f"{item}.md").exists()
    monkeypatch.setattr(promote_module, "_git", original_git)
    rerun = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                               state_root=repo / "state", config_path=config)
    assert rerun.payload["action"] == "converge"
    assert apply_local_promote(repo, None, rerun, rerun.plan_hash, config_path=config).lesson_id == "L-2"
