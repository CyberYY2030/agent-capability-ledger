from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.local_promote import PROJECT_CONSUMED, PROJECT_INBOX, PROJECT_LEDGER, apply_local_promote, plan_local_promote, _archive_superseded
from agent_core.candidates import create_candidate
from agent_core.lock import operation_lock
from agent_core.cli import main as cli_main
from agent_core.freshness import load_candidate
from agent_core.match import Query, match_lessons, parse_markdown, render
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


def test_operation_lock_and_offline_capture(tmp_path: Path) -> None:
    _remote, _seed, alpha, _beta = setup_pair(tmp_path)
    control = tmp_path / "control"
    with operation_lock(control):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            with operation_lock(control):
                pass

    git(alpha, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    offline = candidate(alpha, control, "offline")
    payload = (alpha / "inbox" / f"{offline}.md").read_text(encoding="utf-8")
    assert "unverified" in payload


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


def test_state_candidate_validation_stays_fail_closed_for_project_scope(tmp_path: Path) -> None:
    repo, control = setup_project(tmp_path)
    item = project_candidate(repo, control, "state-closed")
    path = repo / PROJECT_INBOX / f"{item}.md"
    with pytest.raises(ConfigError, match="FAIL_CANDIDATE"):
        load_candidate(path)
    assert load_candidate(path, allow_project=True)["scope_hint"] == "project:sample-app"


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


def test_local_promote_force_new_previews_exact_added_entry_after_plan_hash(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "preview-force-new", rule="Preview force-new rule")
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo, config_path=config)
    added = [line for line in plan.lines if line.startswith("POSTIMAGE_ADDED ")]
    removed = [line for line in plan.lines if line.startswith("POSTIMAGE_REMOVED ")]
    assert added == [
        f"POSTIMAGE_ADDED - **L-2 [pending·通用] Preview force-new rule.** "
        f"触发: local preview-force-new. 代价: lost local lesson. from: {item}. "
        "sink → checks/preview-force-new.md."
    ]
    assert removed == []
    assert plan.lines.index(added[0]) > next(
        index for index, line in enumerate(plan.lines) if line.startswith("PLAN_HASH "))


def test_local_promote_update_preview_exposes_removed_when_field(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    ledger_path = repo / "experience" / "LESSONS.md"
    ledger_text = ledger_path.read_text(encoding="utf-8")
    ledger_path.write_text(
        ledger_text.replace(
            "sink → checks/existing.md.",
            'sink → checks/existing.md. when: {"paths":["engine/**"]}',
            1,
        ),
        encoding="utf-8",
    )
    item = local_state_candidate(repo, control, "preview-update", rule="Replacement without predicate")
    assert "when" not in load_candidate(repo / "inbox" / f"{item}.md", allow_project=True)
    plan = plan_local_promote(
        repo, None, item, scope_override="global", update="global:global:L-1",
        state_root=repo, config_path=config,
    )
    removed = [line for line in plan.lines if line.startswith("POSTIMAGE_REMOVED ")]
    added = [line for line in plan.lines if line.startswith("POSTIMAGE_ADDED ")]
    assert any("when:" in line for line in removed)
    assert added and all("when:" not in line for line in added)


def test_local_promote_supersedes_preview_shows_archive_move(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "preview-supersedes", rule="Replacement with archive")
    plan = plan_local_promote(repo, None, item, scope_override="global", supersedes="L-1",
                              state_root=repo, config_path=config)
    removed = [line for line in plan.lines if line.startswith("POSTIMAGE_REMOVED ")]
    added = [line for line in plan.lines if line.startswith("POSTIMAGE_ADDED ")]
    assert any("- **L-1 " in line for line in removed)
    assert any("- **L-1 " in line and "superseded_by: L-2." in line for line in added)
    assert any(f"from: {item}. supersedes: L-1." in line for line in added)


def test_local_promote_payload_key_set_is_frozen(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "payload-keys", rule="Payload key guard")
    plan = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                              state_root=repo, config_path=config)
    assert set(plan.payload) == {
        "operation", "candidate_id", "expected_remote_sha", "source_path", "candidate_path",
        "source_kind", "candidate_sha256", "target_scope", "target_store", "target_path",
        "operation_root", "target_git_root", "source_git_root", "canonical_sha256",
        "expected_postimage_sha256", "effective_when", "action", "choice", "update", "supersedes", "result_id",
        "project_id", "state_root", "config_path", "config_sha256", "control_root", "stage_paths",
        "candidate_identity", "target_identity", "resolved_sources", "exact_facts_sha256", "layout",
        "state_prefix",
    }


@pytest.mark.parametrize(("predicate", "value", "query"), [
    ("text", "atomic ledger update", Query(stage="prompt", text="atomic ledger update")),
    ("tasks", "build", Query(stage="dispatch", task="build")),
    ("cmds", "git status", Query(stage="pretool", cmds=("git status --short",))),
    ("paths", "src/*.py", Query(stage="pretool", paths=("src/example.py",))),
])
def test_local_promote_when_override_is_plan_bound_rendered_and_retrievable(
        tmp_path: Path, predicate: str, value: str, query: Query) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, f"when-{predicate}", rule=f"Predicate {predicate}")
    candidate_path = repo / "inbox" / f"{item}.md"
    candidate_before = candidate_path.read_bytes()
    when = json.dumps({predicate: [value]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan = plan_local_promote(
        repo, None, item, scope_override="global", force_new=True, when=when,
        state_root=repo, config_path=config,
    )
    assert plan.payload["effective_when"] == when
    assert f"EFFECTIVE_WHEN source=cli value={when}" in plan.lines
    assert any(line.startswith(f"EFFECTIVE_WHEN_TOKENS predicate={predicate}") for line in plan.lines)
    result = apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    consumed = repo / "inbox" / "consumed" / f"{item}.md"
    assert consumed.read_bytes() == candidate_before
    lessons = parse_markdown((repo / "experience" / "LESSONS.md").read_text(encoding="utf-8"), "global", "test")
    promoted = next(lesson for lesson in lessons if lesson.lesson_id == result.lesson_id)
    assert promoted.when == {predicate: (value,)}
    hits, ignored = match_lessons([promoted], query)
    assert result.lesson_id in render(hits, ignored, 20000)


def test_local_promote_when_override_replaces_candidate_when_without_mutating_candidate(tmp_path: Path) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    original = '{"text":["original predicate"]}'
    replacement = '{"text":["replacement predicate"]}'
    item = project_candidate(repo, control, "when-replace", when=original)
    candidate_path = repo / PROJECT_INBOX / f"{item}.md"
    before = candidate_path.read_bytes()
    plan = plan_local_promote(
        repo, None, item, scope_override="project:sample-app", force_new=True, when=replacement,
        state_root=repo, config_path=config,
    )
    assert f"CANDIDATE_WHEN {original}" in plan.lines
    apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    consumed = repo / PROJECT_CONSUMED / f"{item}.md"
    assert consumed.read_bytes() == before
    lesson = next(item for item in parse_markdown(
        (repo / PROJECT_LEDGER).read_text(encoding="utf-8"), "project", "test",
    ) if item.lesson_id == "SAMPLE-2")
    assert lesson.when == {"text": ("replacement predicate",)}


@pytest.mark.parametrize("when", [
    '{"text": ["not canonical"]}',
    '{"unknown":["value"]}',
    '{"text":["first"],"text":["second"]}',
    '{"text":["a"]}',
])
def test_local_promote_when_rejects_invalid_or_tokenless_values_without_writes(
        tmp_path: Path, when: str) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "when-invalid", rule="Invalid predicate")
    before = (repo / "experience" / "LESSONS.md").read_bytes()
    with pytest.raises(ConfigError, match="FAIL_WHEN"):
        plan_local_promote(
            repo, None, item, scope_override="global", force_new=True, when=when,
            state_root=repo, config_path=config,
        )
    assert (repo / "experience" / "LESSONS.md").read_bytes() == before
    assert (repo / "inbox" / f"{item}.md").is_file()


def test_local_promote_cli_binds_when_into_plan_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "when-cli", rule="CLI predicate")
    original = '{"text":["original predicate"]}'
    changed = '{"text":["changed predicate"]}'
    base = ["--workspace", str(repo), "--config", str(config), "--state", str(repo),
            "--id", item, "--scope", "global", "--force-new"]
    assert project_promote_main([*base, "--when", original]) == 0
    plan_hash = next(line.split(" ", 1)[1] for line in capsys.readouterr().out.splitlines()
                     if line.startswith("PLAN_HASH "))
    before = (repo / "experience" / "LESSONS.md").read_bytes()
    assert project_promote_main([*base, "--when", changed, "--apply", "--plan-hash", plan_hash]) == 1
    assert "FAIL_INPUT_CHANGED" in capsys.readouterr().err
    assert (repo / "experience" / "LESSONS.md").read_bytes() == before


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

    monkeypatch.setattr("agent_core.local_promote._after_local_promote_canonical_write", interrupt_after_canonical)
    with pytest.raises(OSError, match="post-write"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    assert (repo / "inbox" / f"{item}.md").is_file()
    monkeypatch.setattr("agent_core.local_promote._after_local_promote_canonical_write", lambda: None)
    converged = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                                   state_root=repo, config_path=config)
    assert converged.payload["action"] == "converge"
    assert not any(line.startswith("POSTIMAGE_") for line in converged.lines)
    assert apply_local_promote(repo, None, converged, converged.plan_hash, config_path=config).lesson_id == "L-2"
    text = (repo / "experience" / "LESSONS.md").read_text(encoding="utf-8")
    assert text.count(f"from: {item}.") == 1


def test_local_promote_never_calls_frozen_remote_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, config, control = setup_unified_local(tmp_path)
    item = local_state_candidate(repo, control, "no-remote", rule="Local only rule")

    def forbidden(*_args, **_kwargs):
        pytest.fail("local lessons promote must not call a remote transaction path")

    monkeypatch.setattr("agent_core.candidates.require_fresh", forbidden)
    from agent_core import local_promote as local_promote_module
    original_git = local_promote_module._git

    def local_git(repo: Path, *args: str, **kwargs):
        if args and args[0] in {"fetch", "push", "pull", "ls-remote", "worktree"}:
            forbidden()
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(local_promote_module, "_git", local_git)
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

    monkeypatch.setattr("agent_core.local_promote.ledger.parse_ledger", render_only_error)
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
    from agent_core import local_promote as local_promote_module
    original_git = local_promote_module._git

    def fail_add(path: Path, *args: str, **kwargs):
        if args and args[0] == "add":
            raise ConfigError("FAIL_GIT", "injected add failure")
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(local_promote_module, "_git", fail_add)
    with pytest.raises(ConfigError, match="injected add failure"):
        apply_local_promote(repo, None, plan, plan.plan_hash, config_path=config)
    consumed = repo / "state" / "inbox" / "consumed" / f"{item}.md"
    assert consumed.is_file() and not (repo / "state" / "inbox" / f"{item}.md").exists()
    monkeypatch.setattr(local_promote_module, "_git", original_git)
    rerun = plan_local_promote(repo, None, item, scope_override="global", force_new=True,
                               state_root=repo / "state", config_path=config)
    assert rerun.payload["action"] == "converge"
    assert apply_local_promote(repo, None, rerun, rerun.plan_hash, config_path=config).lesson_id == "L-2"
