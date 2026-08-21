"""Configuration-driven, validate-first runtime materialization."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import ledger
from .config import (
    ConfigError, assert_capability_sources, compose_manifests, default_config_path, load_config,
    load_manifest,
)
from .freshness import is_repository, record_remote_head, require_fresh


@dataclass(frozen=True)
class Operation:
    target_id: str
    source_label: str
    destination: Path
    content: bytes


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError("FAIL_SOURCE", f"cannot read {path}: {exc}") from exc


def _runtime_head(engine_root: Path, runtime: str) -> bytes:
    path = engine_root / "runtimes" / runtime / "head.md"
    if not path.is_file():
        path = engine_root / "runtimes" / "generic" / "head.md"
    return _read(path)


def _hook_content(lines: list[str], runtime: str) -> bytes:
    rendered = [
        "#!/bin/sh",
        "# agent-core-lessons-hook/1",
        "# Generated from host prompt_injection; edit the host config, not this file.",
        "stage=${1:-prompt}",
        "case \"$stage\" in prompt|pretool|completion) ;; *) echo 'WARNING lessons hook invalid stage' >&2; exit 0 ;; esac",
        "script_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd) || exit 0",
        "export AGENT_CORE_HOOK_HEARTBEAT=\"$script_dir/.lessons-hook-heartbeat.json\"",
        "export AGENT_CORE_HOOK_SCRIPT=\"$0\"",
        "agent_core=${AGENT_CORE_COMMAND:-agent-core}",
        "if [ \"$stage\" = prompt ]; then",
    ]
    rendered.extend(f"  printf '%s\\n' {shlex.quote(line)}" for line in lines)
    rendered.extend([
        "fi",
        f'"$agent_core" lessons hook --runtime {shlex.quote(runtime)} --stage "$stage"',
        "status=$?",
        "if [ \"$status\" -ne 0 ]; then echo \"WARNING lessons hook command failed: $status\" >&2; fi",
        "exit 0",
    ])
    return ("\n".join(rendered) + "\n").encode("utf-8")


def _powershell_hook_content(lines: list[str], runtime: str) -> bytes:
    rendered = [
        "# agent-core-lessons-hook/1",
        "param([ValidateSet('prompt','pretool','completion')][string]$Stage = 'prompt')",
        "$scriptDir = Split-Path -Parent $PSCommandPath",
        "$env:AGENT_CORE_HOOK_HEARTBEAT = Join-Path $scriptDir '.lessons-hook-heartbeat.json'",
        "$env:AGENT_CORE_HOOK_SCRIPT = $PSCommandPath",
        "if ($Stage -eq 'prompt') {",
    ]
    rendered.extend(
        "  [Console]::Out.WriteLine('" + line.replace("'", "''") + "')"
        for line in lines
    )
    rendered.extend([
        "}",
        "$agentCore = $env:AGENT_CORE_COMMAND",
        "if (-not $agentCore) { $agentCore = Join-Path $env:LOCALAPPDATA 'agent-core\\bin\\agent-core.cmd' }",
        f"& $agentCore lessons hook --runtime {runtime} --stage $Stage",
        "$status = $LASTEXITCODE",
        "if ($status -ne 0) { [Console]::Error.WriteLine(\"WARNING lessons hook command failed: $status\") }",
        "exit 0",
    ])
    return ("\n".join(rendered) + "\n").encode("utf-8")


def _state_root(config: dict, explicit_state: Path | None) -> Path | None:
    if explicit_state is not None:
        return explicit_state.resolve()
    value = config["state_root"]
    if value.startswith("<") and value.endswith(">"):
        return None
    return Path(value).expanduser().resolve()


def _target_root(raw: str) -> Path | None:
    if raw.startswith("<") and raw.endswith(">"):
        return None
    return Path(raw).expanduser().resolve()


def _validate_ledgers(state_root: Path) -> None:
    global_path = state_root / "experience" / "LESSONS.md"
    sources, errors, warns = ledger.resolve_sources(str(global_path), all_profiles=True)
    _defined, store_errors, store_warns = ledger.validate_sources(sources)
    errors.extend(store_errors)
    warns.extend(store_warns)
    if errors:
        raise ConfigError("FAIL_LEDGER", "; ".join(errors))


def build_operations(engine_root: Path, config: dict, state_root: Path) -> list[Operation]:
    rules = _read(state_root / "rules" / "global.md")
    lessons = _read(state_root / "experience" / "LESSONS.md")
    case_law = _read(state_root / "experience" / "CASE_LAW.md")
    operations: list[Operation] = []
    for target in config["targets"]:
        root = _target_root(target["root"])
        if root is None:
            raise ConfigError("FAIL_TARGET_UNBOUND", target["id"])
        rendered_rules = _runtime_head(engine_root, target["runtime"]) + b"\n" + rules
        hook = _hook_content(config["prompt_injection"]["lines"], target["runtime"])
        values = (
            ("rules", target["rules_target"], rendered_rules),
            ("lessons", target["lessons_target"], lessons),
            ("case-law", target["case_law_target"], case_law),
            ("hook", target["hook_target"], hook),
            (
                "hook-windows",
                str(Path(target["hook_target"]).with_suffix(".ps1")),
                _powershell_hook_content(config["prompt_injection"]["lines"], target["runtime"]),
            ),
        )
        for label, relative, content in values:
            destination = (root / relative).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise ConfigError("FAIL_PATH", f"target escaped root: {target['id']}:{relative}") from exc
            operations.append(Operation(target["id"], label, destination, content))
    return operations


def _copy_tree_operations(
    engine_root: Path,
    state_root: Path,
    config: dict,
    composition,
) -> list[Operation]:
    operations: list[Operation] = []
    for capability in composition.capabilities:
        if capability["kind"] != "skill" or capability["state"] != "active":
            continue
        source_root = engine_root if capability["origin"] == "engine" else state_root
        source = source_root / capability["source"]
        for target in config["targets"]:
            if target["runtime"] not in capability["runtimes"]:
                continue
            root = _target_root(target["root"])
            if root is None:
                raise ConfigError("FAIL_TARGET_UNBOUND", target["id"])
            skill_name = source.name
            for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
                if path.is_file():
                    destination = root / target["skills_root"] / skill_name / path.relative_to(source)
                    operations.append(Operation(target["id"], f"skill:{skill_name}", destination, _read(path)))
    return operations


def _backup(operations: Iterable[Operation], backup_root: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"sync-{stamp}-{os.getpid()}"
    for operation in operations:
        if not operation.destination.exists():
            continue
        relative = Path(operation.target_id) / operation.destination.name
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(operation.destination, target)
    return backup


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_operations(
    engine_root: Path,
    config: dict,
    state_root: Path,
) -> list[Operation]:
    """Validate sources and return the complete deterministic materialization set."""
    _validate_ledgers(state_root)
    state_manifest = state_root / "manifest.yaml"
    if state_manifest.is_file():
        untrusted = [
            item["id"] for item in load_manifest(state_manifest, "state") if not item["trusted"]
        ]
        if untrusted:
            raise ConfigError("FAIL_UNTRUSTED_CAPABILITY", ",".join(sorted(untrusted)))
    composition = compose_manifests(
        engine_root / "manifest.yaml",
        state_manifest if state_manifest.is_file() else None,
        config,
    )
    assert_capability_sources(composition, engine_root, state_root)
    operations = build_operations(engine_root, config, state_root)
    operations.extend(_copy_tree_operations(engine_root, state_root, config, composition))
    return operations


def execute(
    engine_root: Path,
    config_path: Path | None,
    explicit_state: Path | None,
    apply: bool,
    require_versioned: bool = False,
) -> list[str]:
    config_path = config_path or default_config_path(engine_root)
    config = load_config(config_path)
    output = [f"PLAN target={target['id']} runtime={target['runtime']}" for target in config["targets"]]
    if not apply:
        output.append(f"DRY_RUN writes=0 targets={len(config['targets'])}")
        return output
    state_root = _state_root(config, explicit_state)
    if state_root is None:
        raise ConfigError("FAIL_STATE_UNBOUND", "sync --apply requires a concrete state root")
    if require_versioned and not is_repository(state_root):
        raise ConfigError("FAIL_STATE_REPOSITORY", str(state_root))
    if is_repository(state_root):
        control_root = Path.home() / ".agent-core"
        freshness = require_fresh(state_root, "sync", control_root)
        record_remote_head(control_root, freshness.remote or "")
    operations = collect_operations(engine_root, config, state_root)
    backup_value = config["backup_root"]
    if backup_value.startswith("<") and backup_value.endswith(">"):
        raise ConfigError("FAIL_BACKUP_UNBOUND", "sync --apply requires a concrete backup root")
    changed: list[Operation] = []
    for operation in operations:
        try:
            if (
                not operation.destination.exists()
                or operation.destination.read_bytes() != operation.content
            ):
                changed.append(operation)
        except OSError as exc:
            raise ConfigError("FAIL_TARGET_READ", str(operation.destination)) from exc
    backup = (
        _backup(changed, Path(backup_value).expanduser().resolve())
        if changed else None
    )
    output.append(f"BACKUP files={sum(op.destination.exists() for op in changed)}")
    for operation in changed:
        _atomic_write(operation.destination, operation.content)
    for operation in operations:
        actual = hashlib.sha256(operation.destination.read_bytes()).digest()
        expected = hashlib.sha256(operation.content).digest()
        if actual != expected:
            raise ConfigError("FAIL_SHA256", str(operation.destination))
    output.append(f"APPLIED writes={len(changed)} targets={len(config['targets'])}")
    output.append(f"PASS backup_created={backup is not None and backup.exists()}")
    return output
