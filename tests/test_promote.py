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
    apply_prepared,
    create_candidate,
    plan_project_promote,
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
            "--control-root", str(control), "--id", item]
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


def test_project_similarity_requires_state_equivalent_decision(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "similar", rule="Existing project rule")
    with pytest.raises(ConfigError, match=r"FAIL_SIMILAR_REVIEW SAMPLE-1:1\.000"):
        plan_project_promote(repo, control, item)
    plan = plan_project_promote(repo, control, item, supersedes="SAMPLE-1")
    assert plan.payload["supersedes"] == "SAMPLE-1"
    assert plan.lines[0] == "SIMILAR SAMPLE-1 1.000"
    apply_project_promote(repo, control, plan, plan.plan_hash)
    text = (repo / PROJECT_LEDGER).read_text(encoding="utf-8")
    active, archived = text.split("## 归档", 1)
    assert "[[lesson:SAMPLE-1]]" not in active
    assert "[[lesson:SAMPLE-1]]" in archived
    assert "supersedes: SAMPLE-1" in active


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
