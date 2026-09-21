from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_core import capture, privacy
from agent_core.cli import main as cli_main
from agent_core.config import ConfigError
from agent_core.freshness import load_candidate
from agent_core.match import main as match_main
from agent_core.promote import candidate_id, create_candidate, operation_lock, plan_project_promote


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


def project_repo(tmp_path: Path, project_id: str = "sample-project", *, commit: bool = True) -> Path:
    root = tmp_path / "workspace"
    (root / ".agents").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", f"test{chr(64)}invalid")
    (root / ".agents" / "lessons.json").write_text(json.dumps({
        "schema": "lessons-routing/1", "project_id": project_id, "profiles": [],
    }) + "\n", encoding="utf-8")
    (root / ".agents" / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: project -->\n"
        f"<!-- lessons-project: {project_id} -->\n\n## 活跃\n\n"
        f"- **[[lesson:{project_id.split('-', 1)[0].upper()}-1]] [pending·项目] 当项目捕获失败，先检查项目台账.** "
        "触发: project incident. 代价: repeated failure. sink → checks/project.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    if commit:
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "seed")
    return root


def state_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", f"test{chr(64)}invalid")
    (seed / "experience").mkdir()
    (seed / "experience" / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] Existing rule.** 触发: existing incident. "
        "代价: existing cost. sink → checks/existing.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    state = tmp_path / "state"
    subprocess.run(["git", "clone", "-q", str(remote), str(state)], check=True)
    return state


def config_file(tmp_path: Path, state: Path) -> Path:
    path = tmp_path / "host.json"
    path.write_text(json.dumps({
        "schema": "agent-core.config/1", "host_label": "desk",
        "state_root": str(state), "backup_root": str(tmp_path / "backup"),
        "prompt_injection": {"lines": ["Read matched lessons."]},
        "targets": [{
            "id": "generic", "runtime": "generic", "root": str(tmp_path / "runtime"),
            "rules_target": "AGENTS.md", "lessons_target": "LESSONS.md",
            "case_law_target": "CASE_LAW.md", "skills_root": "skills",
            "hook_target": "hooks/prompt.sh",
        }],
        "capability_overrides": [],
    }) + "\n", encoding="utf-8")
    return path


def argv(config: Path, workspace: Path, *, scope: str = "auto", **overrides: str) -> list[str]:
    values = {
        "agent": "codex", "rule": "当项目捕获失败，先检查项目台账",
        "trigger": "project capture failure", "cost": "lost evidence",
        "sink": "checks/capture.md", "evidence": "synthetic:capture",
    }
    values.update(overrides)
    result = ["--config", str(config), "--workspace", str(workspace), "--scope", scope]
    for key, value in values.items():
        result.extend([f"--{key}", value])
    return result


@pytest.mark.parametrize("field", ["cost", "trigger", "sink"])
def test_missing_gate_is_rejected(field: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace, **{field: ""})) == 1
    error = capsys.readouterr().err
    assert f"REJECTED {field} missing" in error and "RETRY agent-core lessons capture" in error
    assert not (workspace / ".agents" / "inbox").exists()


def test_project_auto_captures_in_dirty_workspace_without_state_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    head = git(workspace, "rev-parse", "HEAD").stdout.strip()
    canonical = workspace / ".agents" / "LESSONS.md"
    canonical_hash = hashlib.sha256(canonical.read_bytes()).hexdigest()
    (workspace / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr("agent_core.promote.require_fresh", lambda *_args, **_kwargs: pytest.fail("project capture used require_fresh"))
    original_run = subprocess.run

    def no_fetch(command, *args, **kwargs):
        assert "fetch" not in command
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr("agent_core.capture.subprocess.run", no_fetch)
    assert cli_main(["lessons", "capture", *argv(config, workspace, rule="当项目捕获失败，先检查项目台账")]) == 0
    output = capsys.readouterr().out
    assert "SIMILAR scope=project store=sample-project id=SAMPLE-1 score=1.000" in output
    candidates = list((workspace / ".agents" / "inbox").glob("*.md"))
    assert len(candidates) == 1
    payload = load_candidate(candidates[0], allow_project=True)
    assert payload["schema"] == "candidate/1" and "when" not in payload
    assert payload["scope_hint"] == "project:sample-project"
    assert payload["base_revision"] == f"{head} unverified"
    assert payload["id"].startswith("desk-") and len(payload["id"].rsplit("-", 1)[1]) == 32
    assert str(tmp_path) not in candidates[0].read_text(encoding="utf-8")
    assert hashlib.sha256(canonical.read_bytes()).hexdigest() == canonical_hash
    assert git(workspace, "diff", "--cached", "--name-only").stdout == ""


def test_project_capture_uses_all_resolved_lessons_stores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    state = state_repo(tmp_path)
    (state / "experience" / "LESSONS.md").write_text(
        "# Lessons\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        "- **L-1 [pending·通用] 当项目捕获失败，先检查项目台账.** "
        "触发: test. 代价: test. sink → checks/test.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    profile = state / "experience" / "profiles" / "example-domain"
    profile.mkdir(parents=True)
    (profile / "LESSONS.md").write_text(
        "# Profile\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: example-domain -->\n\n## 活跃\n\n"
        "- **[[lesson:EXAMPLE-1]] [pending·领域] 当项目捕获失败，先检查项目台账.** "
        "触发: test. 代价: test. sink → checks/test.md.\n\n## 归档\n",
        encoding="utf-8",
    )
    routing = workspace / ".agents" / "lessons.json"
    payload = json.loads(routing.read_text(encoding="utf-8"))
    payload["profiles"] = ["example-domain"]
    routing.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    config = config_file(tmp_path, state)
    assert capture.main(argv(config, workspace)) == 0
    output = capsys.readouterr().out
    assert "SIMILAR scope=global store=global id=L-1 score=" in output
    assert "SIMILAR scope=profile store=example-domain id=EXAMPLE-1 score=" in output
    assert "SIMILAR scope=project store=sample-project id=SAMPLE-1 score=" in output


@pytest.mark.parametrize("failure", ("missing-profile", "unreadable-profile"))
def test_project_capture_resolves_all_sources_before_creating_candidate(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: str) -> None:
    workspace = project_repo(tmp_path)
    state = state_repo(tmp_path)
    routing = workspace / ".agents" / "lessons.json"
    payload = json.loads(routing.read_text(encoding="utf-8"))
    payload["profiles"] = ["example-domain"]
    routing.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    if failure == "unreadable-profile":
        profile = state / "experience" / "profiles" / "example-domain"
        profile.mkdir(parents=True)
        (profile / "LESSONS.md").write_bytes(b"\xff")
    config = config_file(tmp_path, state)
    inbox = workspace / ".agents" / "inbox"
    before_status = git(workspace, "status", "--short").stdout
    before_cached = git(workspace, "diff", "--cached", "--binary").stdout
    assert cli_main(["lessons", "capture", *argv(config, workspace)]) == 1
    assert "FAIL_LESSON_ROUTING" in capsys.readouterr().err
    assert not inbox.exists()
    assert git(workspace, "status", "--short").stdout == before_status
    assert git(workspace, "diff", "--cached", "--binary").stdout == before_cached


@pytest.mark.parametrize("field", ["rule", "trigger", "cost", "sink", "evidence"])
@pytest.mark.parametrize("style", ["windows", "posix", "home"])
def test_project_capture_rejects_absolute_paths_in_every_free_text_field(
    field: str, style: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    values = {
        "windows": "C" + ":/Users/example/private/notes.md",
        "posix": "/" + "absolute/notes.md",
        "home": "~" + "/" + "private/notes.md",
    }
    assert capture.main(argv(config, workspace, **{field: f"see {values[style]} now"})) == 1
    error = capsys.readouterr().err
    assert f"REJECTED privacy absolute_path {field}" in error
    assert f"RETRY replace --{field} absolute path" in error
    assert not (workspace / ".agents" / "inbox").exists()


@pytest.mark.parametrize("value", [
    "/" + "x",
    "/" + "opt/x/y.md",
    "/" + "dev/null",
    "/" + "etc/hosts",
])
def test_project_capture_rejects_non_identity_posix_absolute_paths(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace, rule=f"证据见{value}，不要外传")) == 1
    assert "REJECTED privacy absolute_path rule" in capsys.readouterr().err
    assert not (workspace / ".agents" / "inbox").exists()


@pytest.mark.parametrize("value", [
    "统一 创建/更新/晋升 三个动作的口径",
    "判定 是/否 时不要跳过复核",
    "买/卖 两侧都要记录",
])
def test_project_capture_allows_cjk_slash_prose(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace, rule=f"当 {value}，先检查单一规则")) == 0
    assert "CAPTURED " in capsys.readouterr().out
    assert len(list((workspace / ".agents" / "inbox").glob("*.md"))) == 1


@pytest.mark.parametrize("value", [
    "/" + "用户/私密/记录.md",
    "证据见 /" + "用户/私密/记录.md",
])
def test_project_capture_rejects_unambiguous_unicode_absolute_paths(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace, rule=value)) == 1
    error = capsys.readouterr().err
    assert "REJECTED privacy absolute_path rule" in error
    assert "looks like an absolute path" in error
    assert not (workspace / ".agents" / "inbox").exists()


def test_project_capture_rejects_workspace_root_when_generic_rules_do_not_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    value = f"refx{workspace}/x.md"
    assert not any(rule.regex.search(value) for rule in capture.PROJECT_CAPTURE_RULES)
    assert capture.main(argv(config, workspace, rule=value)) == 1
    assert "REJECTED privacy absolute_path rule" in capsys.readouterr().err
    assert not (workspace / ".agents" / "inbox").exists()


@pytest.mark.parametrize("scope", ["global", "profile:example-domain"])
@pytest.mark.parametrize("field", ["agent", "rule", "trigger", "cost", "sink", "evidence"])
@pytest.mark.parametrize("style", ["windows", "posix"])
def test_state_capture_rejects_identity_paths_in_every_free_text_field(
    scope: str, field: str, style: str, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = state_repo(tmp_path)
    workspace = project_repo(tmp_path / "project-case")
    config = config_file(tmp_path, state)
    values = {
        "windows": "C" + ":/Users/example/private/notes.md",
        "posix": "/" + "Users/example/private/notes.md",
    }
    assert capture.main(argv(
        config, workspace, scope=scope, **{field: f"see {values[style]} now"},
    )) == 1
    error = capsys.readouterr().err
    assert f"REJECTED privacy absolute_path {field}" in error
    assert f"RETRY replace --{field} absolute path" in error
    assert not (state / "inbox").exists()


@pytest.mark.parametrize("scope", ["global", "profile:example-domain"])
@pytest.mark.parametrize("home", [
    "~" + "/.claude/LESSONS.md",
    "%" + "USERPROFILE" + "%/LESSONS.md",
    "$" + "HOME/LESSONS.md",
])
def test_state_capture_allows_identity_free_home_references(
    scope: str, home: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = state_repo(tmp_path)
    workspace = project_repo(tmp_path / "project-case")
    config = config_file(tmp_path, state)
    assert capture.main(argv(config, workspace, scope=scope, sink=home)) == 0
    output = capsys.readouterr().out
    candidate = next((state / "inbox").glob("*.md"))
    assert f"CAPTURED {candidate}" in output
    assert json.loads(candidate.read_text(encoding="utf-8"))["sink"] == home


def test_state_capture_identity_rules_reuse_scanner_objects_without_home() -> None:
    scanner = {rule.rule_id: rule for rule in privacy.ABSOLUTE_PATH_RULES}
    selected = {rule.rule_id: rule for rule in capture.STATE_CAPTURE_PATH_RULES}
    assert set(selected) == {"absolute_windows_path", "absolute_unix_path"}
    assert all(selected[rule_id] is scanner[rule_id] for rule_id in selected)


@pytest.mark.parametrize("scope", ["auto", "global", "profile:example-domain"])
@pytest.mark.parametrize(("rule_id", "value"), [
    ("machine_name", "DESKTOP-" + "FIXTURE123"),
    ("email_address", "owner" + "@" + "example.invalid"),
    ("credential_token", "ghp_" + "A" * 24),
])
def test_all_capture_scopes_reject_sensitive_identity_values(
    scope: str, rule_id: str, value: str, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path / "project-case")
    if scope == "auto":
        state = tmp_path / "unused-state"
    else:
        state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    assert capture.main(argv(
        config, workspace, scope=scope, rule=f"证据见{value}，不要外传",
    )) == 1
    assert f"REJECTED privacy {rule_id} rule" in capsys.readouterr().err
    inbox_root = workspace / ".agents" if scope == "auto" else state
    assert not (inbox_root / "inbox").exists()


def test_capture_sensitive_rules_reuse_scanner_objects_in_both_scope_paths() -> None:
    scanner = {rule.rule_id: rule for rule in privacy.SENSITIVE_IDENTITY_RULES}
    state = {rule.rule_id: rule for rule in capture.STATE_CAPTURE_RULES}
    project = {rule.rule_id: rule for rule in capture.PROJECT_CAPTURE_RULES}
    for rule_id, rule in scanner.items():
        assert state[rule_id] is rule
        assert project[rule_id] is rule


def test_capture_and_promote_share_project_schema_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace, rule="当候选结构需要对称时，先检查项目 schema")) == 0
    candidate = next((workspace / ".agents" / "inbox").glob("*.md")).stem
    capsys.readouterr()
    routing_path = workspace / ".agents" / "lessons.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["schema"] = "lessons-routing/999"
    routing_path.write_text(json.dumps(routing) + "\n", encoding="utf-8")

    assert capture.main(argv(config, workspace, rule="Second candidate")) == 1
    capture_error = capsys.readouterr().err.strip()
    with pytest.raises(ConfigError) as caught:
        plan_project_promote(workspace, tmp_path / "control", candidate)
    assert capture_error == str(caught.value)
    assert capture_error == "REJECTED scope project_identity_unavailable invalid_schema"


@pytest.mark.parametrize("case", ["no-root", "no-config", "bad-id"])
def test_project_identity_failures_never_fall_back(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    if case == "no-root":
        workspace = tmp_path / "plain"
        workspace.mkdir()
    else:
        workspace = project_repo(tmp_path, project_id="bad_id" if case == "bad-id" else "sample-project")
        if case == "no-config":
            (workspace / ".agents" / "lessons.json").unlink()
    assert capture.main(argv(config, workspace)) == 1
    assert "REJECTED scope project_identity_unavailable" in capsys.readouterr().err
    assert not (state / "inbox").exists()


def test_empty_project_repository_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = project_repo(tmp_path, commit=False)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, workspace)) == 1
    assert "REJECTED scope base_revision_unavailable" in capsys.readouterr().err


def test_global_and_profile_keep_state_writer_and_global_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    state = state_repo(tmp_path)
    profile = state / "experience" / "profiles" / "example-domain"
    profile.mkdir(parents=True)
    (profile / "LESSONS.md").write_text(
        "# Profile\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: profile -->\n<!-- lessons-profile: example-domain -->\n"
        "## 活跃\n\n## 归档\n", encoding="utf-8",
    )
    config = config_file(tmp_path, state)
    assert capture.main(argv(config, workspace, scope="global", rule="当 sample-project 发布时，先检查项目作用域")) == 0
    assert "SCOPE_WARNING project scope may be narrower" in capsys.readouterr().out
    assert capture.main(argv(config, workspace, scope="profile:example-domain")) == 0
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in (state / "inbox").glob("*.md")]
    assert {item["scope_hint"] for item in payloads} == {"global", "profile:example-domain"}
    assert not (workspace / ".agents" / "inbox").exists()


def test_capture_rejects_inline_markdown_rule_on_every_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")

    assert capture.main(argv(
        config, workspace, rule="当项目捕获失败，先检查台账**并写报告",
    )) == 1
    error = capsys.readouterr().err
    assert "REJECTED rule inline_markdown" in error and "RETRY use --rule" in error
    assert not (workspace / ".agents" / "inbox").exists()


def test_capture_rejects_non_executable_rule_only_when_a_predicate_is_supplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")

    assert capture.main(argv(
        config, workspace, rule="检查项目台账", when='{"paths":["agent_core/**"]}',
    )) == 1
    error = capsys.readouterr().err
    assert "REJECTED rule format" in error and "RETRY use --rule" in error
    assert not (workspace / ".agents" / "inbox").exists()


def test_capture_warns_instead_of_rejecting_legacy_rule_prose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """candidate/1 predates the executable form; the frozen seed corpus must still pass."""
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")

    assert capture.main(argv(config, workspace, rule="检查项目台账")) == 0
    assert "RULE_FORMAT_WARNING" in capsys.readouterr().out
    assert next((workspace / ".agents" / "inbox").glob("*.md"), None) is not None


def test_capture_writes_candidate_v2_when_and_scans_its_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = project_repo(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    when = '{"paths":["agent_core/**"]}'

    assert capture.main(argv(
        config, workspace, rule="当修改引擎源码时，先检查受影响消费者", when=when,
    )) == 0
    candidate = next((workspace / ".agents" / "inbox").glob("*.md"))
    payload = load_candidate(candidate, allow_project=True)
    assert payload["schema"] == "candidate/2" and payload["when"] == when

    absolute = "C" + ":/Users/example/private"
    assert capture.main(argv(
        config, workspace, rule="当修改引擎源码时，先检查受影响消费者",
        when=f'{{"text":["{absolute}"]}}',
    )) == 1
    assert "REJECTED privacy absolute_path when" in capsys.readouterr().err


def test_full_uuid_generation_is_unique_and_reproducible() -> None:
    created = dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)
    ids = [candidate_id("desk", created, uuid.UUID(int=value, version=4)) for value in range(100_000)]
    assert len(set(ids)) == 100_000
    assert ids == [candidate_id("desk", created, uuid.UUID(int=value, version=4)) for value in range(100_000)]
    assert all(len(item.rsplit("-", 1)[1]) == 32 for item in ids)


def test_repeated_uuid_never_overwrites_and_partial_file_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_repo(tmp_path)
    control = tmp_path / "control"
    fixed = uuid.UUID("12345678-1234-4234-8234-123456789abc")
    fixed_time = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time

    original_uuid4 = uuid.uuid4
    monkeypatch.setattr("agent_core.promote.dt.datetime", FrozenDateTime)
    monkeypatch.setattr("agent_core.promote.uuid.uuid4", lambda: fixed)
    first = create_candidate(
        state, control, host="desk", agent="codex", rule="First",
        trigger="first", cost="cost", sink="checks/first.md",
        scope_hint="global", evidence="synthetic:first",
    )
    before = first.read_bytes()
    with pytest.raises(Exception, match="FAIL_CANDIDATE_COLLISION"):
        create_candidate(
            state, control, host="desk", agent="codex", rule="Second",
            trigger="second", cost="cost", sink="checks/second.md",
            scope_hint="global", evidence="synthetic:second",
        )
    assert first.read_bytes() == before and len(list((state / "inbox").glob("*.md"))) == 1

    monkeypatch.setattr("agent_core.promote.load_candidate", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("schema fail")))
    monkeypatch.setattr("agent_core.promote.uuid.uuid4", original_uuid4)
    with pytest.raises(ValueError, match="schema fail"):
        create_candidate(
            state, control, host="desk", agent="codex", rule="Partial",
            trigger="partial", cost="cost", sink="checks/partial.md",
            scope_hint="global", evidence="synthetic:partial",
        )
    assert len(list((state / "inbox").glob("*.md"))) == 1


CAPTURE_FIELDS = {
    "rule": "当文本检索需要精确定位时，先使用 rg",
    "trigger": "text search needs exact locations",
    "cost": "repeated slow searches",
    "sink": "checks/search.md",
}


def capture_block(fields: dict[str, str] | None = None, *, pretty: bool = False) -> str:
    payload = fields or CAPTURE_FIELDS
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=not pretty, separators=None if pretty else (",", ":"))
    return f"Answer complete.\nagent-core-capture\n{rendered}\n"


def completion_payload(message: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hook_event_name": "Stop", "stop_hook_active": False,
        "last_assistant_message": message, "cwd": "ignored-private-workspace",
        "session_id": "ignored-private-session", "transcript_path": "ignored.jsonl",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("message,accepted", [
    ("plain response", False),
    (capture_block(), True),
    (capture_block() + capture_block(), None),
    ("prefix agent-core-capture\n" + json.dumps(CAPTURE_FIELDS), False),
    ("```text\nagent-core-capture\n" + json.dumps(CAPTURE_FIELDS) + "\n```", False),
])
def test_completion_capture_parser_block_count_anchor_and_fence(message: str, accepted: bool | None) -> None:
    if accepted is None:
        with pytest.raises(ConfigError, match="block count"):
            capture.parse_completion_capture(message)
    else:
        assert (capture.parse_completion_capture(message) is not None) is accepted


@pytest.mark.parametrize("raw", [
    '{"rule":"x","rule":"y","trigger":"t","cost":"c","sink":"s"}',
    '{"rule":"x","trigger":"t","cost":"c"}',
    '{"rule":"x","trigger":"t","cost":"c","sink":"s","extra":"x"}',
    '{"rule":{"nested":true},"trigger":"t","cost":"c","sink":"s"}',
    '{"rule":"","trigger":"t","cost":"c","sink":"s"}',
    '{"rule":"x","trigger":"t","cost":"c","sink":"s"} trailing',
    '{broken',
    '["not-object"]',
])
def test_completion_capture_parser_rejects_strict_json_failures(raw: str) -> None:
    with pytest.raises(ConfigError, match="automatic capture"):
        capture.parse_completion_capture(f"agent-core-capture\n{raw}")


@pytest.mark.parametrize("case", ["trailing", "size", "non-utf8"])
def test_completion_capture_parser_rejects_trailing_size_and_non_utf8(case: str) -> None:
    if case == "trailing":
        message = "agent-core-capture\n" + json.dumps(CAPTURE_FIELDS) + "\ntrailing"
    elif case == "size":
        message = "x" * (capture.CAPTURE_MESSAGE_LIMIT + 1)
    else:
        message = "agent-core-capture\n" + json.dumps(dict(CAPTURE_FIELDS, rule="\ud800"))
    with pytest.raises(ConfigError, match="automatic capture"):
        capture.parse_completion_capture(message)


def test_completion_capture_digest_is_canonical_and_output_hash_is_exact() -> None:
    reordered = {"sink": CAPTURE_FIELDS["sink"], "cost": CAPTURE_FIELDS["cost"],
                 "trigger": CAPTURE_FIELDS["trigger"], "rule": CAPTURE_FIELDS["rule"]}
    first = capture.parse_completion_capture(capture_block())
    second = capture.parse_completion_capture(capture_block(reordered, pretty=True))
    assert first is not None and second is not None
    assert capture._request_sha256(first) == capture._request_sha256(second)
    changed = dict(first, sink="checks/other.md")
    assert capture._request_sha256(first) != capture._request_sha256(changed)
    message = capture_block() + "\n"
    assert hashlib.sha256(message.encode("utf-8")).hexdigest() != hashlib.sha256(
        capture_block().encode("utf-8")
    ).hexdigest()


def test_completion_capture_creates_once_deduplicates_consumed_and_recaptures_after_deletion(
    tmp_path: Path,
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    first_message = capture_block()
    second_message = "Different prose.\n" + capture_block(CAPTURE_FIELDS, pretty=True)
    assert capture.automatic_completion_capture(
        completion_payload(first_message), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    candidate = next((state / "inbox").glob("*.md"))
    payload = load_candidate(candidate)
    request_digest = capture._request_sha256(CAPTURE_FIELDS)
    assert payload["agent"] == "claude-code" and payload["scope_hint"] == "global"
    assert payload["base_revision"] == f"{git(state, 'rev-parse', 'HEAD').stdout.strip()} unverified"
    assert payload["evidence"] == (
        f"claude-code-stop request-sha256:{request_digest} "
        f"assistant-output-sha256:{hashlib.sha256(first_message.encode('utf-8')).hexdigest()}"
    )
    assert all(private not in candidate.read_text(encoding="utf-8") for private in (
        "ignored-private-workspace", "ignored-private-session", "ignored.jsonl", str(tmp_path),
    ))
    assert capture.automatic_completion_capture(
        completion_payload(second_message), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert len(list((state / "inbox").glob("*.md"))) == 1
    changed_fields = dict(CAPTURE_FIELDS, sink="checks/changed.md")
    assert capture.automatic_completion_capture(
        completion_payload(capture_block(changed_fields)), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert len(list((state / "inbox").glob("*.md"))) == 2
    changed_candidate = next(
        path for path in (state / "inbox").glob("*.md") if path != candidate
    )
    changed_candidate.unlink()
    consumed = state / "inbox" / "consumed"
    consumed.mkdir()
    moved = consumed / candidate.name
    candidate.replace(moved)
    assert capture.automatic_completion_capture(
        completion_payload(second_message), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert not list((state / "inbox").glob("*.md"))
    moved.unlink()
    assert capture.automatic_completion_capture(
        completion_payload(second_message), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert len(list((state / "inbox").glob("*.md"))) == 1


@pytest.mark.parametrize(("case", "gate"), [
    ("event", "payload-event"),
    ("active-missing", "payload-active-missing"),
    ("active", "payload-active"),
    ("missing", "payload-output"),
    ("parse", "parse"),
    ("identity", "identity"),
    ("budget", "scan"),
    ("lock", "lock"),
    ("privacy", "capture"),
    ("writer", "capture"),
])
def test_completion_capture_rejections_are_fail_open_and_zero_write(
    case: str, gate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    canonical_ledger = ledger
    before = ledger.read_bytes()
    fields = dict(CAPTURE_FIELDS)
    payload = completion_payload(capture_block(fields))
    secret = "secret-token session-123 C:/Users/__AGENT_CORE_SYNTHETIC__/fixture.txt"
    if case == "event":
        payload["hook_event_name"] = secret
    elif case == "active-missing":
        del payload["stop_hook_active"]
    elif case == "privacy":
        fields["sink"] = secret
        payload = completion_payload(capture_block(fields))
    elif case == "active":
        payload["stop_hook_active"] = secret
    elif case == "missing":
        payload["last_assistant_message"] = ""
    elif case == "parse":
        payload["last_assistant_message"] = f"agent-core-capture\n{{broken {secret}"
    elif case == "identity":
        ledger = tmp_path / secret / "LESSONS.md"
    elif case == "budget":
        (state / "inbox").mkdir()
        (state / "inbox" / "existing.md").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(capture, "CAPTURE_SCAN_FILE_LIMIT", 0)
    elif case == "writer":
        monkeypatch.setattr(
            capture, "create_candidate",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(secret)),
        )
    inbox_before = {
        path.name: path.read_bytes() for path in (state / "inbox").glob("*.md")
    }
    if case == "lock":
        with operation_lock(config.parent / "txn"):
            warning = capture.automatic_completion_capture(
                payload, runtime="claude-code", stage="completion", ledger_path=ledger, config_path=config,
            )
    else:
        warning = capture.automatic_completion_capture(
            payload, runtime="claude-code", stage="completion", ledger_path=ledger, config_path=config,
        )
    assert warning == f"{capture.CAPTURE_WARNING} gate={gate}"
    assert secret not in warning
    assert len(warning.encode("ascii")) <= 80
    assert canonical_ledger.read_bytes() == before
    assert {
        path.name: path.read_bytes() for path in (state / "inbox").glob("*.md")
    } == inbox_before


def test_completion_capture_warning_gate_set_is_fixed() -> None:
    assert capture.AUTOMATIC_CAPTURE_GATES == frozenset({
        "payload-event", "payload-active-missing", "payload-active", "payload-output",
        "parse", "identity", "lock", "scan", "capture",
    })


def test_completion_capture_samples_head_inside_lock_before_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    locked = False
    head_sampled = False

    class BoundLock:
        def __enter__(self):
            nonlocal locked
            locked = True

        def __exit__(self, *_args):
            nonlocal locked
            locked = False

    def local_head(_state: Path) -> str:
        nonlocal head_sampled
        assert locked
        head_sampled = True
        return "a" * 40

    def duplicate(_state: Path, _token: str) -> bool:
        assert locked and head_sampled
        return True

    monkeypatch.setattr(capture, "operation_lock", lambda _root: BoundLock())
    monkeypatch.setattr(capture, "_local_head", local_head)
    monkeypatch.setattr(capture, "_duplicate_request", duplicate)

    assert capture.automatic_completion_capture(
        completion_payload(capture_block()), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert head_sampled and not locked


def test_completion_capture_uses_only_local_git_and_never_shells_model_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_repo(tmp_path)
    config = config_file(tmp_path, state)
    ledger = state / "experience" / "LESSONS.md"
    marker = tmp_path / "must-not-exist"
    fields = dict(CAPTURE_FIELDS, sink=f"$(touch {marker.name})")
    original = subprocess.run
    seen: list[list[str]] = []

    def local_only(command, *args, **kwargs):
        seen.append(list(command))
        assert "fetch" not in command and "push" not in command and "ls-remote" not in command
        return original(command, *args, **kwargs)

    monkeypatch.setattr(capture.subprocess, "run", local_only)
    assert capture.automatic_completion_capture(
        completion_payload(capture_block(fields)), runtime="claude-code", stage="completion",
        ledger_path=ledger, config_path=config,
    ) is None
    assert len(seen) == 1 and seen[0][-2:] == ["rev-parse", "HEAD"]
    assert not marker.exists()


def test_codex_completion_remains_retrieval_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_repo(tmp_path)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(completion_payload(capture_block())), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert match_main([
        "hook", "--runtime", "codex", "--stage", "completion",
        "--ledger", str(state / "experience" / "LESSONS.md"), "--event-json", str(event),
    ]) == 0
    assert not (state / "inbox").exists()
