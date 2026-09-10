from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.installer import build_release_manifest
from agent_core.provenance import validate_engine_provenance


ROOT = Path(__file__).resolve().parents[1]
TEST_EMAIL = "owner" + chr(64) + "invalid"


def public_source(tmp_path: Path) -> Path:
    source = tmp_path / "public-source"
    if not source.exists():
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        (source / "release-manifest.json").write_text(
            json.dumps(build_release_manifest(source), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return source


def run_helper(
    tmp_path: Path, *extra: str, runtime: str = "codex",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "private"
    config = tmp_path / "host" / "host.json"
    runtime_root = tmp_path / "runtime"
    config.parent.mkdir(exist_ok=True)
    runtime_root.mkdir(exist_ok=True)
    environment = (os.environ if env is None else env).copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_DATA_HOME"] = str(tmp_path / "data")
    return subprocess.run(
        [
            sys.executable,
            str(public_source(tmp_path) / "examples" / "prepare_private.py"),
            "--workspace", str(workspace),
            "--config", str(config),
            "--runtime", runtime,
            "--runtime-root", str(runtime_root),
            "--git-name", "Synthetic Owner",
            "--git-email", TEST_EMAIL,
            *extra,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_plan_is_zero_write_and_locates_engine_without_cwd(
    tmp_path: Path, runtime: str,
) -> None:
    sentinel = tmp_path / "host" / "host.json.validate"
    sentinel.parent.mkdir(exist_ok=True)
    sentinel.write_bytes(b"keep-sentinel\n")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = run_helper(tmp_path, runtime=runtime)
    assert result.returncode == 0, result.stderr
    assert "PLAN operation=prepare-private" in result.stdout
    assert "DRY_RUN writes=0 ready=true" in result.stdout
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "host" / "host.json").exists()
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    # The test-owned public-source fixture is the only allowed addition.
    assert {key: value for key, value in after.items() if key.parts[0] != "public-source"} == before


@pytest.mark.parametrize(
    ("runtime_name", "rules_target"),
    [("codex", "AGENTS.md"), ("claude-code", "CLAUDE.md")],
)
def test_apply_creates_clean_private_repo_and_external_config(
    tmp_path: Path, runtime_name: str, rules_target: str,
) -> None:
    runtime = tmp_path / "runtime"
    before_runtime = list(runtime.iterdir()) if runtime.exists() else []
    result = run_helper(tmp_path, "--apply", runtime=runtime_name)
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "private"
    config = tmp_path / "host" / "host.json"
    assert git(workspace, "status", "--porcelain") == ""
    assert git(workspace, "remote") == ""
    assert git(workspace, "config", "--local", "core.autocrlf") == "false"
    assert git(workspace, "log", "--format=%s", "-1") == "state: initialize private agent-core workspace"
    assert (workspace / "engine" / "release-manifest.json").is_file()
    assert (workspace / "state" / "experience" / "LESSONS.md").is_file()
    assert (workspace / "engine.provenance.json").is_file()
    assert not (workspace / "engine" / "AGENTS.md").exists()
    assert not (workspace / "engine" / ".githooks").exists()
    assert not (workspace / "audit").exists()
    assert validate_engine_provenance(workspace / "engine").sequence == 1
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert "<INSTALL_ROOT>" not in config.read_text(encoding="utf-8")
    assert str(tmp_path / "data" / "agent-core" / "bin" / "agent-core") in config.read_text(encoding="utf-8")
    assert payload["state_root"] == str((workspace / "state").resolve())
    assert payload["targets"] == [{
        "id": runtime_name, "runtime": runtime_name, "root": str(runtime.resolve()),
        "rules_target": rules_target, "lessons_target": "LESSONS.md",
        "case_law_target": "CASE_LAW.md", "skills_root": "skills",
        "hook_target": "hooks/user_prompt.sh",
    }]
    assert list(runtime.iterdir()) == before_runtime


def test_either_existing_target_blocks_all_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "private"
    workspace.write_text("occupied", encoding="utf-8")
    result = run_helper(tmp_path, "--apply")
    assert result.returncode != 0
    assert workspace.read_text(encoding="utf-8") == "occupied"
    assert not (tmp_path / "host" / "host.json").exists()

    workspace.unlink()
    config = tmp_path / "host" / "host.json"
    config.write_text("occupied", encoding="utf-8")
    result = run_helper(tmp_path, "--apply")
    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == "occupied"
    assert not workspace.exists()


def test_repeated_apply_refuses_without_changing_first_result(tmp_path: Path) -> None:
    first = run_helper(tmp_path, "--apply")
    assert first.returncode == 0, first.stderr
    workspace = tmp_path / "private"
    config = tmp_path / "host" / "host.json"
    revision = git(workspace, "rev-parse", "HEAD")
    config_bytes = config.read_bytes()
    second = run_helper(tmp_path, "--apply")
    assert second.returncode != 0
    assert git(workspace, "rev-parse", "HEAD") == revision
    assert config.read_bytes() == config_bytes


def test_source_and_target_overlaps_are_rejected_without_writes(tmp_path: Path) -> None:
    source = public_source(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = tmp_path / "host.json"
    result = subprocess.run([
        sys.executable, str(source / "examples" / "prepare_private.py"),
        "--workspace", str(source / "private"), "--config", str(config),
        "--runtime", "codex", "--runtime-root", str(runtime),
        "--git-name", "Synthetic Owner", "--git-email", TEST_EMAIL, "--apply",
    ], check=False, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert not (source / "private").exists()
    assert not config.exists()


def test_apply_overrides_global_autocrlf_for_clean_provenance(tmp_path: Path) -> None:
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    result = run_helper(tmp_path, "--apply", env=environment)
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "private"
    assert git(workspace, "status", "--porcelain") == ""
    assert validate_engine_provenance(workspace / "engine").sequence == 1


def test_apply_disables_inherited_git_hooks(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o700)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    result = run_helper(tmp_path, "--apply", env=environment)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_invalid_plan_inputs_never_report_ready_or_write(tmp_path: Path) -> None:
    result = run_helper(tmp_path, "--host-label", "INVALID LABEL")
    assert result.returncode != 0
    assert "ready=true" not in result.stdout
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "host" / "host.json").exists()


def test_parser_rejects_unsupported_runtime_without_writes(tmp_path: Path) -> None:
    result = run_helper(tmp_path, runtime="unsupported")
    assert result.returncode == 2
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "host" / "host.json").exists()


def test_prepared_repo_completes_supported_local_install_journey(tmp_path: Path) -> None:
    prepared = run_helper(tmp_path, "--apply")
    assert prepared.returncode == 0, prepared.stderr
    workspace = tmp_path / "private"
    config = tmp_path / "host" / "host.json"
    runtime = tmp_path / "runtime"
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(workspace), "push", "-q", "-u", "origin", "main"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True,
    )
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_DATA_HOME"] = str(tmp_path / "data")
    checkout = public_source(tmp_path)
    environment["PYTHONPATH"] = str(checkout)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_core.cli", *args], cwd=checkout,
            env=environment, check=False, capture_output=True, text=True, encoding="utf-8",
        )

    common = (
        "--config", str(config), "--state", str(workspace / "state"),
        "--source", str(workspace / "engine"),
        "--artifact-manifest", str(workspace / "engine" / "release-manifest.json"),
        "--confirm-private-remote",
    )
    plan = cli("install", *common)
    assert plan.returncode == 0, plan.stderr
    token = next(line.split()[1] for line in plan.stdout.splitlines() if line.startswith("PLAN_HASH "))
    applied = cli("install", *common, "--apply", "--plan-hash", token)
    assert applied.returncode == 0, applied.stderr
    assert "DRY_RUN" not in prepared.stdout
    installed = tmp_path / "data" / "agent-core" / "bin" / "agent-core"
    doctor = subprocess.run(
        [str(installed), "doctor", "--config", str(config), "--state", str(workspace / "state")],
        env=environment, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert doctor.returncode == 0, doctor.stderr
    lesson = subprocess.run(
        [str(installed), "lessons", "match", "--ledger",
         str(workspace / "state" / "experience" / "profiles" / "example-domain" / "LESSONS.md"),
         "--workspace", "seed", "--stage", "prompt", "--text", "public fixture"],
        env=environment, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert lesson.returncode == 0, lesson.stderr
    assert "EXAMPLE-1" in lesson.stdout
    sync = subprocess.run(
        [str(installed), "sync", "--config", str(config), "--state", str(workspace / "state")],
        env=environment, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert sync.returncode == 0, sync.stderr
    assert "DRY_RUN writes=0" in sync.stdout
