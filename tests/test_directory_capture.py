"""Directory capture acceptance uses disposable projects, never the bound state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core import capture, ledger, local_promote, project
from agent_core.cli import main as cli_main
from agent_core.config import ConfigError
from agent_core.freshness import load_candidate, parse_candidate_bytes
from tests.test_capture import argv, config_file, git, project_repo, state_repo
from tests.test_lesson_routing import ledger_text

WHEN = '{"text":["capture"]}'


def directory_project(tmp_path: Path, *, name: str = "directory") -> Path:
    root = tmp_path / name
    (root / ".agents").mkdir(parents=True)
    (root / ".agents" / "lessons.json").write_text(json.dumps({
        "schema": "lessons-routing/2", "workspace_kind": "directory",
        "project_id": "sample-directory", "profiles": [],
    }), encoding="utf-8")
    return root


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def captured(config: Path, workspace: Path) -> Path:
    assert cli_main(["lessons", "capture", *argv(config, workspace, when=WHEN)]) == 0
    return next((workspace / ".agents" / "inbox").glob("*.md"))


def test_first_candidate_and_repeated_submission_do_not_create_rules_or_overwrite(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    before = files(tmp_path)
    first = captured(config, root)
    raw = first.read_bytes()
    payload = load_candidate(first, allow_project=True, allow_directory=True)
    assert payload["schema"] == "candidate/3" and payload["source_kind"] == "directory"
    assert "base_revision" not in payload
    assert payload["scope_hint"] == "project:sample-directory"
    assert payload["agent"] == "codex" and payload["when"] == WHEN
    assert str(tmp_path) not in raw.decode()
    captured(config, root)
    candidates = list((root / ".agents" / "inbox").glob("*.md"))
    assert len(candidates) == 2 and first.read_bytes() == raw
    after = files(tmp_path)
    assert set(after) - set(before) == {p.relative_to(tmp_path).as_posix() for p in candidates}
    assert all(after[k] == v for k, v in before.items())
    assert not (root / ".agents" / "LESSONS.md").exists()
    assert not (root / ".git").exists()
    assert not (tmp_path / "unused-state").exists()


def test_sources_keep_global_declared_profile_and_project_order_without_writing_them(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    state = tmp_path / "state"
    global_path = state / "experience" / "LESSONS.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(ledger_text("global"))
    profile_path = state / "experience" / "profiles" / "example-domain" / "LESSONS.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(ledger_text("profile", "example-domain"))
    local = root / ".agents" / "LESSONS.md"
    local.write_text(ledger_text("project", "sample-directory"))
    routing = root / ".agents" / "lessons.json"
    data = json.loads(routing.read_text()); data["profiles"] = ["example-domain"]
    routing.write_text(json.dumps(data))
    sources, errors, warnings = ledger.resolve_sources(str(global_path), str(root))
    assert errors == [] and warnings == []
    assert [(scope, name) for scope, name, _ in sources] == [
        ("global", "global"), ("profile", "example-domain"), ("project", "sample-directory"),
    ]
    config = config_file(tmp_path, state)
    before = files(tmp_path)
    item = captured(config, root)
    after = files(tmp_path)
    assert set(after) - set(before) == {item.relative_to(tmp_path).as_posix()}
    assert all(after[k] == v for k, v in before.items())


@pytest.mark.parametrize("bad", [
    [], {"schema": "lessons-routing/1"}, {"schema": "lessons-routing/999"},
    {"workspace_kind": "git"}, {"project_id": "bad_id"}, {"project_id": "../other"},
    {"profiles": "example"}, {"profiles": ["bad_id"]}, {"profiles": [{}]},
    {"profiles": ["example", "example"]}, {"extra": "ignored?"},
])
def test_bad_identity_rejects_before_candidate_creation(tmp_path: Path, bad: object) -> None:
    root = directory_project(tmp_path)
    routing = root / ".agents" / "lessons.json"
    data = json.loads(routing.read_text())
    if isinstance(bad, dict):
        data.update(bad)
    else:
        data = bad
    routing.write_text(json.dumps(data))
    config = config_file(tmp_path, tmp_path / "unused-state")
    before = files(tmp_path)
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert files(tmp_path) == before and not (root / ".agents" / "inbox").exists()


def test_duplicate_identity_keys_rejected(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    routing = root / ".agents" / "lessons.json"
    routing.write_text(routing.read_text().replace('"project_id":', '"project_id":"other", "project_id":'))
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert not (root / ".agents" / "inbox").exists()


@pytest.mark.parametrize("overrides", [
    {}, {"when": "2026-10-17T00:00:00Z"}, {"when": WHEN, "scope": "project:other"},
    {"when": WHEN, "rule": "Missing executable trigger"},
    {"when": WHEN, "evidence": "/" + "private/project/secrets.txt"},
])
def test_parameter_and_privacy_gates_remain_before_write(tmp_path: Path, overrides: dict) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    before = files(tmp_path)
    assert capture.main(argv(config, root, **overrides)) == 1
    assert files(tmp_path) == before and not (root / ".agents" / "inbox").exists()


def test_cwd_default_does_not_enable_directory_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    args = argv(config, root, when=WHEN)
    index = args.index("--workspace"); del args[index:index + 2]
    monkeypatch.chdir(root)
    assert capture.main(args) == 1
    assert not (root / ".agents" / "inbox").exists()


def test_directory_child_does_not_inherit_parent_identity(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    child = root / "child"; child.mkdir()
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, child, when=WHEN)) == 1
    assert not (root / ".agents" / "inbox").exists()


@pytest.mark.parametrize("schema,kind", [
    ("lessons-routing/2", "directory"), ("lessons-routing/999", "directory"),
    (None, "directory"), ("lessons-routing/2", None), ("lessons-routing/999", None),
    ("lessons-routing/1", "directory"), ("other-tool/1", "directory"),
])
def test_directory_identity_inside_git_parent_is_not_borrowed(
    tmp_path: Path, schema: str | None, kind: str | None, capsys: pytest.CaptureFixture[str],
) -> None:
    parent = project_repo(tmp_path)
    root = directory_project(parent)
    routing = root / ".agents" / "lessons.json"
    data = json.loads(routing.read_text())
    if schema is None:
        del data["schema"]
    else:
        data["schema"] = schema
    if kind is None:
        del data["workspace_kind"]
    else:
        data["workspace_kind"] = kind
    routing.write_text(json.dumps(data))
    config = config_file(tmp_path, tmp_path / "unused-state")
    before = files(parent)
    with pytest.raises(ConfigError, match="scope directory_inside_git"):
        project.resolve_capture_project(root)
    _, errors, _ = ledger.resolve_sources(str(tmp_path / "LESSONS.md"), str(root))
    assert errors == ["REJECTED scope directory_inside_git"]
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert "directory_inside_git" in capsys.readouterr().err
    assert files(parent) == before
    assert not (parent / ".agents" / "inbox").exists()


def test_git_v1_subdirectory_capture_preserves_head_and_scope(tmp_path: Path) -> None:
    root = project_repo(tmp_path)
    child = root / "child"; child.mkdir()
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, child, when=WHEN)) == 0
    item = next((root / ".agents" / "inbox").glob("*.md"))
    data = load_candidate(item, allow_project=True)
    assert data["schema"] == "candidate/2" and data["scope_hint"] == "project:sample-project"
    assert data["base_revision"] == git(root, "rev-parse", "HEAD").stdout.strip() + " unverified"
    assert not (child / ".agents").exists()


@pytest.mark.parametrize("endpoint", [".agents", ".agents/lessons.json", ".agents/inbox", ".agents/LESSONS.md"])
def test_linked_endpoints_are_zero_write_rejections(tmp_path: Path, endpoint: str) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    path = root / endpoint
    outside = tmp_path / "outside"
    if path.exists():
        path.rename(outside)
    elif endpoint.endswith("inbox"):
        outside.mkdir()
    else:
        outside.write_text(ledger_text("project", "other-project"))
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    before = files(tmp_path)
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert files(tmp_path) == before
    if endpoint.endswith("inbox"):
        assert list(outside.iterdir()) == []


def test_linked_workspace_rejected(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    alias = tmp_path / "alias"; alias.symlink_to(root, target_is_directory=True)
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, alias, when=WHEN)) == 1
    assert not (root / ".agents" / "inbox").exists()


@pytest.mark.parametrize("failure", [OSError("missing git"), subprocess.TimeoutExpired("git", 10)])
def test_git_probe_failure_does_not_enable_directory_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(ledger.subprocess, "run", fail)
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert not (root / ".agents" / "inbox").exists()


@pytest.mark.parametrize("change", [
    {"source_kind": "git"}, {"scope_hint": "global"}, {"scope_hint": "profile:example"},
    {"base_revision": "a" * 40}, {"when": "invalid"}, {"scope_hint": "project:bad_id"},
])
def test_directory_candidate_contract_rejects_tampering(tmp_path: Path, change: dict) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    item = captured(config, root)
    data = json.loads(item.read_text()); data.update(change)
    with pytest.raises(ConfigError):
        parse_candidate_bytes(json.dumps(data).encode(), item.stem, allow_project=True, allow_directory=True)


def test_default_candidate_reader_and_promote_reject_directory_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    settings = json.loads(config.read_text()); settings["state_root"] = "<STATE_ROOT>"
    config.write_text(json.dumps(settings))
    item = captured(config, root)
    capsys.readouterr()
    monkeypatch.setattr(local_promote, "operation_lock", lambda *_a, **_kw: pytest.fail("promotion took a lock"))
    with pytest.raises(ConfigError, match="capture-only"):
        load_candidate(item, allow_project=True)
    before = files(tmp_path)
    for scope in (None, "global", "profile:example", "project:sample-directory"):
        for apply in (False, True):
            args = ["lessons", "promote", "--workspace", str(root), "--config", str(config),
                    "--id", item.stem, "--force-new"]
            if scope:
                args.extend(["--scope", scope])
            if apply:
                args.extend(["--apply", "--plan-hash", "untrusted"])
            assert cli_main(args) == 1
            assert "directory candidates are capture-only; promotion unsupported" in capsys.readouterr().err
    assert files(tmp_path) == before


@pytest.mark.parametrize("destination", ["project", "state"])
def test_copied_directory_candidate_cannot_be_promoted_in_git(
    tmp_path: Path, destination: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    settings = json.loads(config.read_text()); settings["state_root"] = "<STATE_ROOT>"
    config.write_text(json.dumps(settings))
    item = captured(config, root)
    capsys.readouterr()
    monkeypatch.setattr(local_promote, "operation_lock", lambda *_a, **_kw: pytest.fail("promotion took a lock"))
    if destination == "project":
        repo = project_repo(tmp_path)
        inbox = repo / ".agents" / "inbox"
    else:
        repo = state_repo(tmp_path)
        config = config_file(tmp_path, repo)
        inbox = repo / "inbox"
    inbox.mkdir()
    (inbox / item.name).write_bytes(item.read_bytes())
    before = files(repo)
    for scope in (None, "global", "profile:example"):
        args = ["lessons", "promote", "--workspace", str(repo), "--config", str(config),
                "--id", item.stem, "--force-new"]
        if scope:
            args.extend(["--scope", scope])
        assert cli_main(args) == 1
        assert "directory candidates are capture-only; promotion unsupported" in capsys.readouterr().err
    assert files(repo) == before
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""


@pytest.mark.parametrize("version", ["candidate/1", "candidate/2"])
def test_git_candidate_sha_requirement_is_not_relaxed(tmp_path: Path, version: str) -> None:
    root = directory_project(tmp_path)
    config = config_file(tmp_path, tmp_path / "unused-state")
    item = captured(config, root)
    data = json.loads(item.read_text())
    data["schema"] = version
    del data["source_kind"]
    if version == "candidate/1":
        del data["when"]
    data["base_revision"] = "directory"
    with pytest.raises(ConfigError, match="Git sha"):
        parse_candidate_bytes(json.dumps(data).encode(), item.stem, allow_project=True, allow_directory=True)


def test_unknown_declared_profile_blocks_directory_capture(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    state = tmp_path / "state"
    global_path = state / "experience" / "LESSONS.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(ledger_text("global"))
    routing = root / ".agents" / "lessons.json"
    data = json.loads(routing.read_text()); data["profiles"] = ["missing"]
    routing.write_text(json.dumps(data))
    config = config_file(tmp_path, state)
    before = files(tmp_path)
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert files(tmp_path) == before and not (root / ".agents" / "inbox").exists()


def test_damaged_git_marker_does_not_enable_directory_capture(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    (root / ".git").write_text("invalid git directory reference")
    config = config_file(tmp_path, tmp_path / "unused-state")
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert not (root / ".agents" / "inbox").exists()


def test_standalone_ledger_checker_uses_same_directory_routing(tmp_path: Path) -> None:
    root = directory_project(tmp_path)
    global_path = tmp_path / "LESSONS.md"
    global_path.write_text(ledger_text("global"))
    local = root / ".agents" / "LESSONS.md"
    local.write_text(ledger_text("project", "sample-directory"))
    checker = tmp_path / "check_lessons.py"
    checker.write_bytes(Path(ledger.__file__).read_bytes())
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-B", "-I", str(checker), str(global_path), "--workspace", str(root)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SOURCE project:sample-directory" in result.stdout


# Frozen pre-feature Git behavior; run these cases against the baseline as well.
@pytest.mark.parametrize("local_config", [
    "not json", "[]", '{"unrelated":1,"unrelated":2}',
    '{"schema":"some-other-tool/1"}',
    '{"schema":"lessons-routing/1","project_id":"child-project","profiles":[]}',
    '{"project_id":"child-project"}',
    '{"schema":"lessons-routing/999","schema":"lessons-routing/1"}',
])
def test_git_child_legacy_config_uses_root(tmp_path: Path, local_config: str) -> None:
    root = project_repo(tmp_path)
    child = root / "child"
    (child / ".agents").mkdir(parents=True)
    (child / ".agents" / "lessons.json").write_text(local_config)
    _assert_git_root_capture(tmp_path, root, child)


def _assert_git_root_capture(tmp_path: Path, root: Path, workspace: Path) -> None:
    assert project.resolve_project_context(workspace) == (root, "sample-project")
    global_path = tmp_path / "LESSONS.md"
    global_path.write_text(ledger_text("global"))
    sources, errors, warnings = ledger.resolve_sources(str(global_path), str(workspace))
    assert errors == [] and warnings == []
    assert sources == [("global", "global", str(global_path)),
                       ("project", "sample-project", str(root / ".agents" / "LESSONS.md"))]
    config = config_file(tmp_path, tmp_path / "unused-state")
    before = files(tmp_path)
    assert capture.main(argv(config, workspace, when=WHEN)) == 0
    items = list((root / ".agents" / "inbox").glob("*.md"))
    assert len(items) == 1
    payload = load_candidate(items[0], allow_project=True)
    assert payload["schema"] == "candidate/2"
    assert payload["scope_hint"] == "project:sample-project"
    assert payload["base_revision"] == git(root, "rev-parse", "HEAD").stdout.strip() + " unverified"
    after = files(tmp_path)
    assert set(after) - set(before) == {items[0].relative_to(tmp_path).as_posix()}
    assert all(after[k] == v for k, v in before.items())


def test_git_root_duplicate_keys_preserve_last_value(tmp_path: Path) -> None:
    root = project_repo(tmp_path)
    (root / ".agents" / "lessons.json").write_text(
        '{"schema":"lessons-routing/999","schema":"lessons-routing/1",'
        '"project_id":"other-project","project_id":"sample-project",'
        '"profiles":null,"profiles":[],"extra":1,"extra":2}')
    _assert_git_root_capture(tmp_path, root, root)


@pytest.mark.parametrize("unbound", [False, True])
def test_git_missing_local_and_global_ledger_preserves_rejection(
    tmp_path: Path, unbound: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    root = project_repo(tmp_path)
    (root / ".agents" / "LESSONS.md").unlink()
    config = config_file(tmp_path, tmp_path / "unused-state")
    if unbound:
        settings = json.loads(config.read_text())
        settings["state_root"] = "<STATE_ROOT>"
        config.write_text(json.dumps(settings))
    assert project.resolve_project_context(root) == (root, "sample-project")
    before = files(tmp_path)
    assert capture.main(argv(config, root, when=WHEN)) == 1
    assert capsys.readouterr().err.strip() == "FAIL_LESSON_ROUTING configured lessons source"
    assert files(tmp_path) == before and not (root / ".agents" / "inbox").exists()
