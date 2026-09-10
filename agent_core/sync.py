"""Configuration-driven, validate-first runtime materialization."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import ledger
from .config import (
    ConfigError, assert_capability_sources, compose_manifests, default_config_path, load_config,
    load_manifest, posix_user_data_root_shell,
)
from .freshness import (
    inspect as inspect_freshness,
    is_repository,
    migrate_legacy_remote_state,
    record_remote_head,
    require_fresh,
)
from .materializer import (
    MaterializationReview,
    MaterializationTarget,
    file_mode_preimage,
    file_preimage,
    materialization_pending_path,
    materialization_receipt_path,
    materialize_bytes,
    publish_materialization_receipt,
    restore_file_preimage,
    review_materialization,
)
from .promote import operation_lock


@dataclass(frozen=True)
class Operation:
    target_id: str
    source_label: str
    destination: Path
    content: bytes
    executable: bool = False


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
    installed = posix_user_data_root_shell() + "/bin/agent-core"
    rendered = [
        "#!/bin/sh",
        "# agent-core-lessons-hook/1",
        "# Generated from host prompt_injection; edit the host config, not this file.",
        "stage=${1:-prompt}",
        "case \"$stage\" in prompt|pretool|completion) ;; *) echo 'WARNING lessons hook invalid stage' >&2; exit 0 ;; esac",
        "script_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd) || exit 0",
        "export AGENT_CORE_HOOK_HEARTBEAT=\"$script_dir/.lessons-hook-heartbeat.json\"",
        "export AGENT_CORE_HOOK_SCRIPT=\"$0\"",
        "if [ -n \"${AGENT_CORE_COMMAND:-}\" ]; then",
        "  agent_core=$AGENT_CORE_COMMAND",
        "elif command -v agent-core >/dev/null 2>&1; then",
        "  agent_core=$(command -v agent-core)",
        "else",
        f'  agent_core="{installed}"',
        "fi",
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
        "$eventPath = $null",
        "$eventStream = $null",
        "try {",
        "  $eventName = '.agent-core-hook-event-' + [Guid]::NewGuid().ToString('N') + '.json'",
        "  $eventPath = Join-Path ([IO.Path]::GetTempPath()) $eventName",
        "  $eventStream = [IO.File]::Open($eventPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)",
        "  try {",
        "    [Console]::OpenStandardInput().CopyTo($eventStream)",
        "    $eventStream.Flush()",
        "  } finally {",
        "    if ($eventStream) { $eventStream.Dispose(); $eventStream = $null }",
        "  }",
        "  $previousErrorActionPreference = $ErrorActionPreference",
        "  try {",
        "    $ErrorActionPreference = 'Stop'",
        f"    & $agentCore lessons hook --runtime {runtime} --stage $Stage --event-json $eventPath",
        "    $status = $LASTEXITCODE",
        "    if ($status -ne 0) { [Console]::Error.WriteLine(\"WARNING lessons hook command failed: $status\") }",
        "  } finally {",
        "    $ErrorActionPreference = $previousErrorActionPreference",
        "  }",
        "} catch {",
        "  [Console]::Error.WriteLine('WARNING lessons hook event transport unavailable')",
        "} finally {",
        "  if ($eventStream) { $eventStream.Dispose() }",
        "  if ($eventPath -and [IO.File]::Exists($eventPath)) {",
        "    try { [IO.File]::Delete($eventPath) }",
        "    catch { [Console]::Error.WriteLine('WARNING lessons hook event cleanup unavailable') }",
        "  }",
        "}",
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
            operations.append(Operation(
                target["id"], label, destination, content,
                executable=label in {"hook", "hook-windows"},
            ))
    return operations


def _git_executable_paths(source_root: Path) -> set[str]:
    discovered = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if discovered.returncode != 0:
        return set()
    repository = Path(discovered.stdout.strip()).resolve()
    indexed = subprocess.run(
        [
            "git", "-c", f"safe.directory={repository.as_posix()}",
            "-C", str(repository), "ls-files", "--stage", "-z",
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if indexed.returncode != 0:
        raise ConfigError("FAIL_SOURCE", f"cannot read Git index modes from {repository}")
    executable: set[str] = set()
    for entry in indexed.stdout.split("\0"):
        if not entry:
            continue
        metadata, separator, relative = entry.partition("\t")
        mode = metadata.split(" ", 1)[0]
        if not separator or mode not in {"100644", "100755"}:
            continue
        if mode == "100755":
            executable.add(str((repository / relative).resolve()).casefold())
    return executable


def _copy_tree_operations(
    engine_root: Path,
    state_root: Path,
    config: dict,
    composition,
) -> list[Operation]:
    operations: list[Operation] = []
    executable_by_root = {
        str(root.resolve()).casefold(): _git_executable_paths(root)
        for root in {engine_root, state_root}
    }
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
                    executable = str(path.resolve()).casefold() in executable_by_root[
                        str(source_root.resolve()).casefold()
                    ]
                    operations.append(Operation(
                        target["id"], f"skill:{skill_name}", destination, _read(path),
                        executable=executable,
                    ))
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


def _materialization_targets(
    operations: list[Operation], config: dict,
) -> list[MaterializationTarget]:
    roots = {target["id"]: _target_root(target["root"]) for target in config["targets"]}
    result: list[MaterializationTarget] = []
    for operation in operations:
        root = roots[operation.target_id]
        if root is None:
            raise ConfigError("FAIL_TARGET_UNBOUND", operation.target_id)
        result.append(MaterializationTarget(
            operation.target_id, operation.source_label, root, operation.destination,
            operation.content, executable=operation.executable,
        ))
    return result


def _operation_payload(
    operations: list[Operation], config: dict, review: MaterializationReview,
) -> list[dict[str, object]]:
    roots = {target["id"]: _target_root(target["root"]) for target in config["targets"]}
    payload: list[dict[str, object]] = []
    for operation, classified in zip(operations, review.operations, strict=True):
        root = roots[operation.target_id]
        if root is None:
            raise ConfigError("FAIL_TARGET_UNBOUND", operation.target_id)
        payload.append({
            "target_id": operation.target_id,
            "source_label": operation.source_label,
            "destination": str(operation.destination.resolve()),
            "relative_path": operation.destination.relative_to(root).as_posix(),
            "before_exists": classified["before_exists"],
            "before_sha256": classified["before_sha256"],
            "before_executable": classified["before_executable"],
            "after_sha256": classified["after_sha256"],
            "executable": classified["executable"],
            "status": classified["status"],
            "action": classified["action"],
        })
    return payload


def _repository_root_sha(repository_root: Path, head: str, layout: str) -> str | None:
    if layout != "canonical":
        return None
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repository_root.resolve().as_posix()}", "-C", str(repository_root),
         "rev-list", "--max-parents=0", head],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    roots = [line for line in result.stdout.splitlines() if line]
    if result.returncode != 0 or len(roots) != 1 or re.fullmatch(r"[0-9a-f]{40,64}", roots[0]) is None:
        raise ConfigError("FAIL_STATE_BINDING", "repository lineage is unavailable")
    return roots[0]


def _repository_review(
    operations: list[Operation], config: dict, *, config_path: Path,
    state_root: Path, control_root: Path,
) -> tuple[dict[str, object], MaterializationReview]:
    repository: dict[str, object] = {
        "repository_root": None,
        "repository_root_sha": None,
        "state_root": str(state_root.resolve()),
        "head": None,
        "remote_revision": None,
    }
    if is_repository(state_root):
        observed = inspect_freshness(state_root, control_root, fetch=False)
        root_sha = _repository_root_sha(
            observed.context.repo_root, observed.head, observed.context.layout,
        )
        repository = {
            "repository_root": str(observed.context.repo_root.resolve()),
            "repository_root_sha": root_sha,
            "state_root": str(observed.context.state_root.resolve()),
            "head": observed.head,
            "remote_revision": observed.remote,
        }
    review = review_materialization(
        config_path, config, state_root, _materialization_targets(operations, config),
        repository_root_sha=repository["repository_root_sha"],
        head=repository["head"], remote_revision=repository["remote_revision"],
    )
    return repository, review


def _plan_payload(
    operations: list[Operation],
    config: dict,
    *,
    config_path: Path,
    state_root: Path,
    control_root: Path,
) -> dict[str, object]:
    try:
        raw_config = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError("FAIL_CONFIG", f"cannot read {config_path}") from exc
    repository, review = _repository_review(
        operations, config, config_path=config_path, state_root=state_root,
        control_root=control_root,
    )
    targets = []
    for target in config["targets"]:
        root = _target_root(target["root"])
        if root is None:
            raise ConfigError("FAIL_TARGET_UNBOUND", target["id"])
        targets.append({
            "id": target["id"],
            "runtime": target["runtime"],
            "root": str(root.resolve()),
        })
    return {
        "schema": "sync-plan/1",
        "config": {
            "path": str(config_path.resolve()),
            "sha256": hashlib.sha256(raw_config).hexdigest(),
        },
        "repository": repository,
        "targets": targets,
        "ownership_receipt": {
            "exists": review.receipt_preimage_exists,
            "sha256": review.receipt_preimage_sha256,
        },
        "operations": _operation_payload(operations, config, review),
        "retained": list(review.retained),
    }


def _plan_hash(payload: dict[str, object]) -> str:
    _validate_plan_contract(payload)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_plan_contract(payload: dict[str, object]) -> None:
    if set(payload) != {
        "schema", "config", "repository", "targets", "ownership_receipt", "operations", "retained",
    } or payload.get("schema") != "sync-plan/1":
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync plan fields")
    config = payload.get("config")
    repository = payload.get("repository")
    targets = payload.get("targets")
    ownership = payload.get("ownership_receipt")
    operations = payload.get("operations")
    retained = payload.get("retained")
    if (
        not isinstance(config, dict)
        or set(config) != {"path", "sha256"}
        or not isinstance(config.get("path"), str)
        or not Path(config["path"]).is_absolute()
        or not isinstance(config.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", config["sha256"]) is None
    ):
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync config identity")
    if (
        not isinstance(repository, dict)
        or set(repository) != {"repository_root", "repository_root_sha", "state_root", "head", "remote_revision"}
        or not isinstance(repository.get("state_root"), str)
        or not Path(repository["state_root"]).is_absolute()
    ):
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync repository identity")
    repository_root = repository.get("repository_root")
    repository_root_sha = repository.get("repository_root_sha")
    head = repository.get("head")
    remote_revision = repository.get("remote_revision")
    if repository_root is not None and (
        not isinstance(repository_root, str)
        or not Path(repository_root).is_absolute()
        or (
            repository_root_sha is not None
            and (not isinstance(repository_root_sha, str) or re.fullmatch(r"[0-9a-f]{40,64}", repository_root_sha) is None)
        )
        or not isinstance(head, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", head) is None
        or not isinstance(remote_revision, str)
        or re.fullmatch(r"[0-9a-f]{40,64}", remote_revision) is None
    ):
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync repository revision")
    if repository_root is None and (repository_root_sha is not None or head is not None or remote_revision is not None):
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync repository nullability")
    if (
        not isinstance(ownership, dict) or set(ownership) != {"exists", "sha256"}
        or type(ownership.get("exists")) is not bool
        or (
            ownership.get("sha256") is not None
            and (not isinstance(ownership["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", ownership["sha256"]) is None)
        )
        or (not ownership["exists"] and ownership["sha256"] is not None)
    ):
        raise ConfigError("FAIL_PLAN_CONTRACT", "ownership receipt preimage")
    if not isinstance(targets, list) or not targets or not isinstance(operations, list) or not isinstance(retained, list):
        raise ConfigError("FAIL_PLAN_CONTRACT", "sync target or operation list")
    for target in targets:
        if (
            not isinstance(target, dict)
            or set(target) != {"id", "runtime", "root"}
            or not all(isinstance(target.get(key), str) and target[key] for key in ("id", "runtime", "root"))
            or not Path(target["root"]).is_absolute()
        ):
            raise ConfigError("FAIL_PLAN_CONTRACT", "sync target identity")
    operation_keys = {
        "target_id", "source_label", "destination", "relative_path", "before_exists",
        "before_sha256", "before_executable", "after_sha256", "executable", "status", "action",
    }
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or set(operation) != operation_keys
            or not isinstance(operation.get("destination"), str)
            or not Path(operation["destination"]).is_absolute()
            or operation.get("action") not in {"NOOP", "WRITE", "MODE", "BLOCK"}
            or operation.get("status") not in {
                "receipt-owned-identical", "managed-update", "bootstrap-identical",
                "bootstrap-managed-update", "absent", "adopt-identical", "mode-drift",
                "foreign", "indeterminate",
            }
            or type(operation.get("before_exists")) is not bool
            or not isinstance(operation.get("after_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", operation["after_sha256"]) is None
            or type(operation.get("executable")) is not bool
            or (
                operation.get("before_executable") is not None
                and type(operation["before_executable"]) is not bool
            )
        ):
            raise ConfigError("FAIL_PLAN_CONTRACT", "sync operation identity")
        before = operation.get("before_sha256")
        if (before is None) != (not operation["before_exists"]) or (
            before is not None and (not isinstance(before, str) or re.fullmatch(r"[0-9a-f]{64}", before) is None)
        ):
            raise ConfigError("FAIL_PLAN_CONTRACT", "sync operation preimage")
    for row in retained:
        if (
            not isinstance(row, dict)
            or set(row) != {
                "target_id", "source_label", "root", "path", "kind", "installed_sha256",
                "executable", "status",
            }
            or row.get("status") not in {"retained", "retained-drift"}
            or not isinstance(row.get("path"), str) or not Path(row["path"]).is_absolute()
            or not isinstance(row.get("root"), str) or not Path(row["root"]).is_absolute()
            or not isinstance(row.get("installed_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", row["installed_sha256"]) is None
            or (row.get("executable") is not None and type(row["executable"]) is not bool)
        ):
            raise ConfigError("FAIL_PLAN_CONTRACT", "sync retained row")


def _validate_operation_preimages(
    operations: list[Operation], payload: dict[str, object],
) -> None:
    planned = payload["operations"]
    if not isinstance(planned, list) or len(planned) != len(operations):
        raise ConfigError("FAIL_PLAN_HASH", "sync operation contract changed")
    for operation, item in zip(operations, planned, strict=True):
        if not isinstance(item, dict):
            raise ConfigError("FAIL_PLAN_HASH", "sync operation contract changed")
        exists, digest, executable = file_mode_preimage(operation.destination)
        if (
            exists != item["before_exists"] or digest != item["before_sha256"]
            or executable != item["before_executable"]
        ):
            raise ConfigError("FAIL_PLAN_HASH", f"current changed: {operation.destination}")


def _plan_ready(payload: dict[str, object]) -> bool:
    operations = payload["operations"]
    retained = payload["retained"]
    return (
        isinstance(operations, list)
        and isinstance(retained, list)
        and all(isinstance(item, dict) and item.get("action") != "BLOCK" for item in operations)
        and all(isinstance(item, dict) and item.get("status") == "retained" for item in retained)
    )


def _assert_no_pending(config_path: Path) -> None:
    for path, code in (
        (config_path.resolve().parent / "install-pending.json", "FAIL_INSTALL_RECOVERY"),
        (materialization_pending_path(config_path), "FAIL_MATERIALIZATION_RECOVERY"),
    ):
        if os.path.lexists(path):
            raise ConfigError(code, f"inspect and clear pending marker before replanning: {path}")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_materialization(
    config_path: Path,
    operations: list[Operation],
    config: dict,
    payload: dict[str, object],
    review: MaterializationReview,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    snapshot = config_path.resolve().parent / "rollback" / f"materialization-{uuid.uuid4().hex}"
    snapshot.mkdir(parents=True)
    roots = {target["id"]: _target_root(target["root"]) for target in config["targets"]}
    records: list[dict[str, Any]] = []
    try:
        planned = payload["operations"]
        assert isinstance(planned, list)
        for index, (operation, item) in enumerate(zip(operations, planned, strict=True)):
            assert isinstance(item, dict)
            if item["action"] not in {"WRITE", "MODE"}:
                continue
            exists, digest, executable = file_mode_preimage(operation.destination)
            if (
                exists != item["before_exists"] or digest != item["before_sha256"]
                or executable != item["before_executable"]
            ):
                raise ConfigError("FAIL_MATERIALIZER_DRIFT", f"snapshot drift: {operation.destination}")
            relative = f"runtime/{index}"
            if exists:
                destination = snapshot / relative
                _atomic_bytes(destination, operation.destination.read_bytes())
                copied_exists, copied_sha = file_preimage(destination)
                if not copied_exists or copied_sha != digest:
                    raise ConfigError("FAIL_MATERIALIZATION_SNAPSHOT", "runtime snapshot differs")
            root = roots[operation.target_id]
            if root is None:
                raise ConfigError("FAIL_TARGET_UNBOUND", operation.target_id)
            records.append({
                "path": str(operation.destination.resolve()), "root": str(root.resolve()),
                "before_exists": exists, "before_sha256": digest,
                "before_executable": executable,
                "after_sha256": item["after_sha256"],
                "after_executable": item["executable"], "snapshot_rel": relative,
            })
        receipt_record: dict[str, Any] = {
            "path": str(review.receipt_path), "before_exists": review.receipt_preimage_exists,
            "before_sha256": review.receipt_preimage_sha256,
            "after_sha256": hashlib.sha256(review.receipt_bytes).hexdigest(),
            "snapshot_rel": "receipt/preimage.json",
        }
        if review.receipt_preimage_exists:
            raw = review.receipt_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != review.receipt_preimage_sha256:
                raise ConfigError("FAIL_MATERIALIZER_DRIFT", "receipt changed during snapshot")
            _atomic_bytes(snapshot / receipt_record["snapshot_rel"], raw)
        manifest = {
            "schema": "materialization-snapshot/1", "runtime": records,
            "receipt": receipt_record,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _atomic_bytes(snapshot / "snapshot.json", manifest_bytes)
        pending = {
            "schema": "materialization-pending/1", "snapshot_path": str(snapshot),
            "snapshot_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        _atomic_bytes(
            materialization_pending_path(config_path),
            (json.dumps(pending, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        return snapshot, records, receipt_record
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _rollback_materialization(
    snapshot: Path,
    records: list[dict[str, Any]],
    receipt_record: dict[str, Any],
    *,
    control_root: Path,
    lock_token: object,
) -> None:
    rollback_root = snapshot / ".rollback"
    for index, record in enumerate(reversed(records)):
        path = Path(record["path"])
        exists, digest, executable = file_mode_preimage(path)
        if (
            exists == record["before_exists"] and digest == record["before_sha256"]
            and executable == record["before_executable"]
        ):
            continue
        before = (
            (snapshot / record["snapshot_rel"]).read_bytes()
            if record["before_exists"] else None
        )
        restore_file_preimage(
            path, Path(record["root"]), record["after_sha256"], before,
            rollback_root, f"runtime-{index}", control_root=control_root,
            lock_token=lock_token,
            expected_executable=record["after_executable"],
            before_executable=record["before_executable"],
        )
    receipt_path = Path(receipt_record["path"])
    exists, digest = file_preimage(receipt_path)
    if exists != receipt_record["before_exists"] or digest != receipt_record["before_sha256"]:
        before = (
            (snapshot / receipt_record["snapshot_rel"]).read_bytes()
            if receipt_record["before_exists"] else None
        )
        restore_file_preimage(
            receipt_path, receipt_path.parent, receipt_record["after_sha256"], before,
            rollback_root, "receipt", control_root=control_root, lock_token=lock_token,
        )


def _clear_materialization_transaction(config_path: Path, snapshot: Path) -> None:
    shutil.rmtree(snapshot)
    materialization_pending_path(config_path).unlink(missing_ok=True)


def execute(
    engine_root: Path,
    config_path: Path | None,
    explicit_state: Path | None,
    apply: bool,
    require_versioned: bool = False,
    plan_hash: str | None = None,
) -> list[str]:
    if plan_hash is not None and not apply:
        raise ConfigError("FAIL_PLAN_HASH", "--plan-hash requires --apply")
    if apply and plan_hash is None:
        raise ConfigError("FAIL_PLAN_HASH", "sync --apply requires a reviewed plan hash")
    config_path = (config_path or default_config_path(engine_root)).resolve()
    _assert_no_pending(config_path)
    config = load_config(config_path)
    output = [f"PLAN target={target['id']} runtime={target['runtime']}" for target in config["targets"]]
    state_root = _state_root(config, explicit_state)
    if not apply:
        if state_root is None:
            output.append(
                f"DRY_RUN writes=0 targets={len(config['targets'])} "
                "planned_writes=unknown reason=state_unbound"
            )
            return output
        operations = collect_operations(engine_root, config, state_root)
        control_root = config_path.parent / "txn"
        payload = _plan_payload(
            operations, config, config_path=config_path, state_root=state_root,
            control_root=control_root,
        )
        ready = _plan_ready(payload)
        if ready:
            output.append(f"PLAN_HASH {_plan_hash(payload)}")
        output.extend(
            f"PLAN_OP {item['action']:<5} target={item['target_id']} path={item['relative_path']} status={item['status']}"
            for item in payload["operations"]
        )
        output.extend(
            f"RETAIN target={item['target_id']} path={item['path']} status={item['status']}"
            for item in payload["retained"]
        )
        planned_writes = sum(
            item["action"] in {"WRITE", "MODE"} for item in payload["operations"]
        )
        output.append(
            f"DRY_RUN writes=0 targets={len(config['targets'])} "
            f"planned_writes={planned_writes} ready={'true' if ready else 'false'}"
        )
        return output
    if state_root is None:
        raise ConfigError("FAIL_STATE_UNBOUND", "sync --apply requires a concrete state root")
    if require_versioned and not is_repository(state_root):
        raise ConfigError("FAIL_STATE_REPOSITORY", str(state_root))
    backup_value = config["backup_root"]
    if backup_value.startswith("<") and backup_value.endswith(">"):
        raise ConfigError("FAIL_BACKUP_UNBOUND", "sync --apply requires a concrete backup root")
    control_root = config_path.parent / "txn"
    with operation_lock(control_root) as lock_token:
        _assert_no_pending(config_path)
        migrate_legacy_remote_state(config_path, state_root, lock_token=lock_token)
        if is_repository(state_root):
            freshness = require_fresh(state_root, "sync", control_root)
            record_remote_head(control_root, freshness.remote or "")
        locked_config = load_config(config_path)
        locked_state_root = _state_root(locked_config, explicit_state)
        if locked_state_root is None:
            raise ConfigError("FAIL_PLAN_HASH", "state root changed after reviewed plan")
        operations = collect_operations(engine_root, locked_config, locked_state_root)
        payload = _plan_payload(
            operations, locked_config, config_path=config_path, state_root=locked_state_root,
            control_root=control_root,
        )
        actual_plan_hash = _plan_hash(payload)
        if plan_hash != actual_plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", f"planned={plan_hash} actual={actual_plan_hash}")
        _validate_operation_preimages(operations, payload)
        if not _plan_ready(payload):
            raise ConfigError("SYNC_CONFLICT", "sync plan ready=false")
        repository, review = _repository_review(
            operations, locked_config, config_path=config_path,
            state_root=locked_state_root, control_root=control_root,
        )
        if (
            review.receipt_preimage_exists != payload["ownership_receipt"]["exists"]
            or review.receipt_preimage_sha256 != payload["ownership_receipt"]["sha256"]
            or not review.ready
        ):
            raise ConfigError("FAIL_PLAN_HASH", "ownership authority changed")
        planned_operations = payload["operations"]
        changed = [
            operation
            for operation, item in zip(operations, planned_operations, strict=True)
            if item["action"] in {"WRITE", "MODE"}
        ]
        backup = None
        if changed or review.receipt_changed:
            snapshot, snapshot_records, receipt_record = _snapshot_materialization(
                config_path, operations, locked_config, payload, review,
            )
            try:
                backup = (
                    _backup(changed, Path(backup_value).expanduser().resolve())
                    if changed else None
                )
                transaction_root = snapshot / ".transaction"
                if changed:
                    assert backup is not None
                    transaction_root = snapshot / ".transaction"
                for index, (operation, item) in enumerate(
                    (
                        pair for pair in zip(operations, planned_operations, strict=True)
                        if pair[1]["action"] in {"WRITE", "MODE"}
                    )
                ):
                    materialize_bytes(
                        operation.destination,
                        next(
                            _target_root(target["root"])
                            for target in locked_config["targets"]
                            if target["id"] == operation.target_id
                        ),
                        item["before_sha256"], operation.content, transaction_root,
                        f"operation-{index}", control_root=control_root,
                        lock_token=lock_token,
                        expected_executable=item["before_executable"],
                        executable=operation.executable,
                    )
                for operation, item in zip(operations, planned_operations, strict=True):
                    exists, actual, executable = file_mode_preimage(operation.destination)
                    if not exists or actual != item["after_sha256"] or (
                        os.name != "nt" and executable != item["executable"]
                    ):
                        raise ConfigError("FAIL_SHA256", str(operation.destination))
                for item in review.retained:
                    exists, actual, executable = file_mode_preimage(Path(item["path"]))
                    if not exists or actual != item["installed_sha256"] or (
                        os.name != "nt" and item.get("executable") is not None
                        and executable != item["executable"]
                    ):
                        raise ConfigError("FAIL_MATERIALIZER_DRIFT", f"retained path changed: {item['path']}")
                if review.receipt_changed:
                    publish_materialization_receipt(
                        review, transaction_root, control_root=control_root,
                        lock_token=lock_token,
                    )
                _clear_materialization_transaction(config_path, snapshot)
            except Exception as exc:
                try:
                    _rollback_materialization(
                        snapshot, snapshot_records, receipt_record,
                        control_root=control_root, lock_token=lock_token,
                    )
                    if not (isinstance(exc, ConfigError) and exc.code == "FAIL_MATERIALIZER_RACE"):
                        _clear_materialization_transaction(config_path, snapshot)
                except Exception as rollback_exc:
                    if (
                        isinstance(exc, ConfigError) and exc.code == "FAIL_MATERIALIZER_RACE"
                    ) or (
                        isinstance(rollback_exc, ConfigError)
                        and rollback_exc.code == "FAIL_MATERIALIZER_RACE"
                    ):
                        raise ConfigError(
                            "FAIL_MATERIALIZER_RACE",
                            f"raced bytes preserved; snapshot retained at {snapshot}",
                        ) from None
                    raise ConfigError("FAIL_MATERIALIZATION_ROLLBACK", str(rollback_exc)) from rollback_exc
                if isinstance(exc, ConfigError):
                    raise
                raise ConfigError("FAIL_MATERIALIZATION", str(exc)) from exc
        output.append(
            "BACKUP files="
            + str(sum(
                bool(item["before_exists"])
                for item in planned_operations if item["action"] == "WRITE"
            ))
        )
        if backup is not None:
            transaction_root = backup / ".transaction"
            if transaction_root.exists():
                shutil.rmtree(transaction_root, ignore_errors=True)
            if backup.is_dir() and not any(backup.iterdir()):
                backup.rmdir()
        for operation in operations:
            actual = hashlib.sha256(operation.destination.read_bytes()).digest()
            expected = hashlib.sha256(operation.content).digest()
            if actual != expected or (
                os.name != "nt"
                and file_mode_preimage(operation.destination)[2] != operation.executable
            ):
                raise ConfigError("FAIL_SHA256", str(operation.destination))
        output.append(f"APPLIED writes={len(changed)} targets={len(locked_config['targets'])}")
        output.append(f"PASS backup_created={backup is not None and backup.exists()}")
        return output
