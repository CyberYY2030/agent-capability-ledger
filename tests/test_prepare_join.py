from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.config import load_config
from agent_core.installer import build_release_manifest
from tests.test_public_setup import run_helper


def public_source(tmp_path: Path) -> Path:
    source = tmp_path / "public-source"
    shutil.copytree(
        Path(__file__).resolve().parents[1], source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".git"),
    )
    (source / "release-manifest.json").write_text(
        json.dumps(build_release_manifest(source), sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    return source


def machine_env(machine: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(machine / "home"),
        "USERPROFILE": str(machine / "home"),
        "XDG_DATA_HOME": str(machine / "data"),
        "LOCALAPPDATA": str(machine / "data"),
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        cwd=repo, env=os.environ.copy(), check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=15,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith(("AGENT_CORE_", "GIT_")):
            monkeypatch.delenv(key)
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(key, machine_env(tmp_path)[key])
    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def cloned_workspace(tmp_path: Path) -> tuple[Path, Path]:
    machine_a = tmp_path / "a"
    machine_a.mkdir()
    public_source(machine_a)
    prepared = run_helper(machine_a, "--apply", env=machine_env(machine_a))
    assert prepared.returncode == 0, prepared.stderr
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(machine_a / "private", "remote", "add", "origin", str(remote))
    git(machine_a / "private", "push", "origin", "main")
    machine_b = tmp_path / "b"
    machine_b.mkdir()
    workspace = machine_b / "private"
    git(machine_b, "clone", str(remote), str(workspace))
    (machine_b / "host").mkdir()
    (machine_b / "runtime").mkdir()
    return machine_b, workspace


def join(
    machine: Path, workspace: Path, *extra: str, script: Path | None = None,
    config: Path | None = None, runtime_root: Path | None = None, runtime: str = "codex",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(script or workspace / "engine/examples/prepare_private.py"),
         "--join", "--workspace", str(workspace),
         "--config", str(config or machine / "host/host.json"),
         "--runtime", runtime, "--runtime-root", str(runtime_root or machine / "runtime"), *extra],
        cwd=machine, env=machine_env(machine), check=False, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )


def snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_join_clone_script_dry_run_apply_and_repeat(
    cloned_workspace: tuple[Path, Path], runtime: str,
) -> None:
    machine, workspace = cloned_workspace
    config = machine / "host/host.json"
    revision = git(workspace, "rev-parse", "HEAD")
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == ""
    before = snapshot(machine)
    dry_run = join(machine, workspace, runtime=runtime)
    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run.stdout.splitlines() == [
        f"PLAN operation=join-private workspace={workspace.resolve()}",
        f"PLAN config={config.resolve()}",
        f"PLAN runtime={runtime} runtime_root={(machine / 'runtime').resolve()}",
        "DRY_RUN writes=0 ready=true",
    ]
    assert not config.exists()
    assert snapshot(machine) == before
    assert git(workspace, "rev-parse", "HEAD") == revision
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == ""
    before = snapshot(machine)

    applied = join(machine, workspace, "--apply", runtime=runtime)
    assert applied.returncode == 0, applied.stderr
    assert applied.stdout.splitlines() == [
        f"APPLIED config={config.resolve()}", "PASS joined=true installed=false",
    ]
    payload = load_config(config)
    assert payload["state_root"] == str((workspace / "state").resolve())
    assert len(payload["targets"]) == 1
    assert payload["targets"][0]["runtime"] == runtime
    assert payload["targets"][0]["root"] == str((machine / "runtime").resolve())
    wrapper = machine / "data/agent-core/bin" / ("agent-core.cmd" if os.name == "nt" else "agent-core")
    assert any(str(wrapper.resolve()) in line for line in payload["prompt_injection"]["lines"])
    after = snapshot(machine)
    assert after.pop(config.relative_to(machine)) == config.read_bytes()
    assert after == before
    assert git(workspace, "rev-parse", "HEAD") == revision
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == ""

    before = snapshot(machine)
    repeated = join(machine, workspace, "--apply", runtime=runtime)
    assert repeated.returncode != 0
    assert "FAIL_PREPARE_EXISTS" in repeated.stderr
    assert snapshot(machine) == before


def test_join_from_public_checkout_ignores_git_identity(cloned_workspace: tuple[Path, Path]) -> None:
    machine, workspace = cloned_workspace
    script = public_source(machine) / "examples/prepare_private.py"
    before = snapshot(machine)
    result = join(machine, workspace, "--git-name", "", "--git-email", "", script=script)
    assert result.returncode == 0, result.stderr
    assert "PLAN operation=join-private" in result.stdout
    assert "DRY_RUN writes=0 ready=true" in result.stdout
    assert snapshot(machine) == before


@pytest.mark.parametrize(
    ("problem", "code", "detail"),
    [
        ("missing-workspace", "FAIL_PREPARE_PATH", "workspace"),
        ("plain-directory", "FAIL_PREPARE_GIT", ""),
        ("nested-directory", "FAIL_PREPARE_GIT", "top level"),
        ("missing-manifest", "FAIL_PREPARE_CLONE", "engine/release-manifest.json"),
        ("tampered-manifest", "FAIL_PREPARE_CLONE", "engine/release-manifest.json"),
        ("missing-lock", "FAIL_PREPARE_CLONE", "state/agent-core.lock.json"),
        ("directory-lock", "FAIL_PREPARE_CLONE", "state/agent-core.lock.json"),
        ("config-in-workspace", "FAIL_PREPARE_OVERLAP", "config"),
        ("config-in-engine", "FAIL_PREPARE_OVERLAP", "config"),
        ("runtime-in-workspace", "FAIL_PREPARE_OVERLAP", "runtime"),
        ("runtime-contains-config", "FAIL_PREPARE_OVERLAP", "runtime"),
        ("missing-runtime", "FAIL_PREPARE_RUNTIME", "runtime"),
        ("bad-host-label", "FAIL_PREPARE_INPUT", "host label"),
    ],
)
def test_join_rejections_write_nothing(
    cloned_workspace: tuple[Path, Path], problem: str, code: str, detail: str,
) -> None:
    machine, clone = cloned_workspace
    workspace = clone
    script = public_source(machine) / "examples/prepare_private.py"
    config = machine / "host/host.json"
    runtime = machine / "runtime"
    extra = ["--apply"]
    if problem == "missing-workspace":
        workspace = machine / "missing"
    elif problem == "plain-directory":
        workspace = machine / "plain"
        workspace.mkdir()
    elif problem == "nested-directory":
        workspace = clone / "state"
    elif problem in {"missing-manifest", "tampered-manifest"}:
        manifest = clone / "engine/release-manifest.json"
        if problem == "missing-manifest":
            manifest.unlink()
        else:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["artifact_sha256"] = "tampered"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif problem in {"missing-lock", "directory-lock"}:
        lock = clone / "state/agent-core.lock.json"
        lock.unlink()
        if problem == "directory-lock":
            lock.mkdir()
    elif problem == "config-in-workspace":
        config = clone / "host.json"
    elif problem == "config-in-engine":
        config = script.parent / "host.json"
    elif problem == "runtime-in-workspace":
        runtime = clone / "state"
    elif problem == "runtime-contains-config":
        config = runtime / "host.json"
    elif problem == "missing-runtime":
        runtime = machine / "missing-runtime"
    elif problem == "bad-host-label":
        extra += ["--host-label", "Invalid Label"]
    revision = git(clone, "rev-parse", "HEAD")
    status = git(clone, "status", "--porcelain", "--untracked-files=all")
    before = snapshot(machine)
    result = join(machine, workspace, *extra, script=script, config=config, runtime_root=runtime)
    assert result.returncode != 0
    assert code in result.stderr
    assert detail in result.stderr
    assert "ready=true" not in result.stdout
    assert not config.exists()
    assert snapshot(machine) == before
    assert git(clone, "rev-parse", "HEAD") == revision
    assert git(clone, "status", "--porcelain", "--untracked-files=all") == status


@pytest.mark.parametrize("parent_alias", [False, True])
def test_join_rejects_workspace_alias(
    cloned_workspace: tuple[Path, Path], parent_alias: bool,
) -> None:
    machine, clone = cloned_workspace
    alias = machine / "alias"
    alias.symlink_to(clone.parent if parent_alias else clone, target_is_directory=True)
    workspace = alias / clone.name if parent_alias else alias
    before = snapshot(machine)
    result = join(machine, workspace, "--apply")
    assert result.returncode != 0
    assert "FAIL_PREPARE_ALIAS" in result.stderr
    assert snapshot(machine) == before
    assert not (machine / "host/host.json").exists()


@pytest.mark.parametrize("identity", [
    ["--git-email", "synthetic"],
    ["--git-name", "Synthetic"],
    ["--git-name", " ", "--git-email", "synthetic"],
    ["--git-name", "Synthetic", "--git-email", " "],
])
def test_create_still_requires_nonempty_git_identity(tmp_path: Path, identity: list[str]) -> None:
    script = public_source(tmp_path) / "examples/prepare_private.py"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    before = snapshot(tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", str(script), "--workspace", str(tmp_path / "private"),
         "--config", str(tmp_path / "host.json"), "--runtime", "codex",
         "--runtime-root", str(runtime), "--apply", *identity],
        cwd=tmp_path, env=machine_env(tmp_path), check=False, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode != 0
    assert "FAIL_PREPARE_INPUT" in result.stderr
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "host.json").exists()
    assert snapshot(tmp_path) == before
