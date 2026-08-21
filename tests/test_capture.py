from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_core import capture, privacy
from agent_core.cli import main as cli_main
from agent_core.config import ConfigError
from agent_core.freshness import load_candidate
from agent_core.promote import candidate_id, create_candidate, plan_project_promote


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
    assert "SIMILAR SAMPLE-1 1.000" in output
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
