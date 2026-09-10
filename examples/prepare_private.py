#!/usr/bin/env python3
"""Prepare a fresh private agent-core workspace without installing it."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT))

from agent_core import __version__  # noqa: E402
from agent_core.config import ConfigError, HOST_LABEL_RE, load_config, user_data_root  # noqa: E402
from agent_core.installer import _move_no_replace, verify_release_manifest  # noqa: E402
from agent_core.state import _copy_state_seed  # noqa: E402


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-c", f"safe.directory={repo.resolve().as_posix()}",
            "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args,
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    if result.returncode:
        raise ConfigError("FAIL_PREPARE_GIT", result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def _fresh_target(path: Path, label: str) -> Path:
    requested = path.absolute()
    if requested.parent == requested or not requested.parent.is_dir():
        raise ConfigError("FAIL_PREPARE_PATH", f"{label} parent must exist: {requested.parent}")
    if os.path.lexists(requested):
        raise ConfigError("FAIL_PREPARE_EXISTS", f"{label} already exists: {requested}")
    current = requested.parent
    while current != current.parent:
        junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(junction) and junction()):
            raise ConfigError("FAIL_PREPARE_ALIAS", f"{label} parent contains an alias")
        current = current.parent
    return requested.parent.resolve() / requested.name


def _manifest() -> tuple[object, list[tuple[Path, Path]]]:
    artifact = verify_release_manifest(ENGINE_ROOT, ENGINE_ROOT / "release-manifest.json")
    copies: list[tuple[Path, Path]] = []
    for item in artifact.entries:
        relative = item["path"]
        rel = Path(relative)
        source = ENGINE_ROOT / rel
        copies.append((source, rel))
    return artifact, copies


def _config(workspace: Path, config_path: Path, runtime: str, runtime_root: Path, host: str) -> bytes:
    payload = json.loads((ENGINE_ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    payload["host_label"] = host
    payload["state_root"] = str((workspace / "state").resolve())
    payload["backup_root"] = str((config_path.parent / "backups").resolve())
    wrapper = user_data_root() / "bin" / ("agent-core.cmd" if os.name == "nt" else "agent-core")
    payload["prompt_injection"]["lines"] = [
        line.replace("<INSTALL_ROOT>/bin/agent-core", str(wrapper))
        for line in payload["prompt_injection"]["lines"]
    ]
    payload["targets"] = [target for target in payload["targets"] if target["runtime"] == runtime]
    payload["targets"][0]["root"] = str(runtime_root.resolve())
    return _json_bytes(payload)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _reject_overlap(workspace: Path, config: Path, runtime: Path) -> None:
    source = ENGINE_ROOT.resolve()
    if _inside(workspace, source) or _inside(source, workspace):
        raise ConfigError("FAIL_PREPARE_OVERLAP", "workspace overlaps the public engine")
    if _inside(config, source) or _inside(config, workspace):
        raise ConfigError("FAIL_PREPARE_OVERLAP", "config must stay outside engine and workspace")
    if _inside(runtime, source) or _inside(source, runtime):
        raise ConfigError("FAIL_PREPARE_OVERLAP", "runtime root overlaps the public engine")
    if _inside(runtime, workspace) or _inside(workspace, runtime) or _inside(config, runtime):
        raise ConfigError("FAIL_PREPARE_OVERLAP", "workspace/config overlaps runtime root")


def prepare(args: argparse.Namespace) -> list[str]:
    if HOST_LABEL_RE.fullmatch(args.host_label) is None:
        raise ConfigError("FAIL_PREPARE_INPUT", "host label must be privacy-safe kebab-case")
    if not args.git_name.strip() or not args.git_email.strip():
        raise ConfigError("FAIL_PREPARE_INPUT", "Git identity values must be non-empty")
    workspace = _fresh_target(args.workspace, "workspace")
    config_path = _fresh_target(args.config, "config")
    runtime_root = args.runtime_root.absolute()
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ConfigError("FAIL_PREPARE_RUNTIME", "runtime root must be an existing ordinary directory")
    runtime_root = runtime_root.resolve()
    _reject_overlap(workspace, config_path, runtime_root)
    artifact, copies = _manifest()
    config_bytes = _config(workspace, config_path, args.runtime, runtime_root, args.host_label)
    lines = [
        f"PLAN operation=prepare-private workspace={workspace}",
        f"PLAN config={config_path}",
        f"PLAN runtime={args.runtime} runtime_root={runtime_root}",
    ]
    if not args.apply:
        return lines + ["DRY_RUN writes=0 ready=true"]

    staging = Path(tempfile.mkdtemp(prefix=".agent-core-private-", dir=workspace.parent))
    config_staging: Path | None = None
    workspace_published = False
    try:
        engine = staging / "engine"
        for source, relative in copies:
            destination = engine / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        shutil.copy2(ENGINE_ROOT / "release-manifest.json", engine / "release-manifest.json")
        verify_release_manifest(engine, engine / "release-manifest.json")
        state = staging / "state"
        state.mkdir()
        _copy_state_seed(ENGINE_ROOT, state)
        validation = staging / ".host-config.validate"
        validation.write_bytes(config_bytes)
        try:
            load_config(validation)
        finally:
            validation.unlink(missing_ok=True)
        _git(staging, "init", "-q", "-b", "main")
        _git(staging, "config", "core.autocrlf", "false")
        _git(staging, "config", "user.name", args.git_name)
        _git(staging, "config", "user.email", args.git_email)
        _git(staging, "add", "engine", "state")
        root_tree = _git(staging, "write-tree")
        engine_tree = _git(staging, "rev-parse", f"{root_tree}:engine")
        (staging / "engine.provenance.json").write_bytes(_json_bytes({
            "schema": "engine-provenance/1",
            "sequence": 1,
            "previous_record_sha256": None,
            "engine_tree_oid": engine_tree,
            "release_artifact_sha256": artifact.artifact_sha256,
        }))
        _git(staging, "add", "engine.provenance.json")
        _git(staging, "commit", "-q", "-m", "state: initialize private agent-core workspace")
        _move_no_replace(staging, workspace)
        workspace_published = True
        handle, name = tempfile.mkstemp(prefix=config_path.name + ".", suffix=".prepare", dir=config_path.parent)
        config_staging = Path(name)
        with os.fdopen(handle, "wb") as stream:
            stream.write(config_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _move_no_replace(config_staging, config_path)
        return [
            f"APPLY operation=prepare-private workspace={workspace}",
            f"APPLIED workspace={workspace}",
            f"APPLIED config={config_path}",
            "PASS prepared=true remote=false installed=false",
        ]
    except Exception as exc:
        if not workspace_published:
            shutil.rmtree(staging, ignore_errors=True)
        residue = workspace if workspace_published else staging
        raise ConfigError(
            "FAIL_PREPARE_APPLY",
            f"inspect retained preparation residue before retrying: {residue}; {exc}",
        ) from exc
    finally:
        if config_staging is not None:
            config_staging.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--runtime", choices=("claude-code", "codex"), required=True)
    result.add_argument("--runtime-root", type=Path, required=True)
    result.add_argument("--git-name", required=True)
    result.add_argument("--git-email", required=True)
    result.add_argument("--host-label", default="local")
    result.add_argument("--apply", action="store_true")
    return result


def main() -> int:
    try:
        for line in prepare(parser().parse_args()):
            print(line)
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
