"""Transactional, receipt-driven installation for immutable engine artifacts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import ConfigError, load_config
from .freshness import inspect, require_fresh
from .doctor import hook_retrieval_status
from .runtime_config import (
    merge_owned_hooks,
    remove_owned_hooks,
    render_fragment,
    runtime_config_path,
    runtime_hook_path,
)
from .state import BindingEvidence, binding_receipt_path, validate_state_binding
from .sync import collect_operations


RELEASE_SCHEMA = "release-manifest/1"
RECEIPT_SCHEMA = "install-receipt/1"
SNAPSHOT_SCHEMA = "install-snapshot/1"
PIN_SCHEMA = "engine-pin/1"
PAYLOAD_DIRS = ("agent_core", "enforcement", "examples", "runtimes", "seed", "skills", "templates")
PAYLOAD_FILES = (
    "LICENSE", "NOTICE", "install.ps1", "install.sh", "manifest.yaml", "privacy_rules.default.json",
)


@dataclass(frozen=True)
class Artifact:
    version: str
    artifact_sha256: str
    entries: tuple[dict[str, str], ...]
    manifest_bytes: bytes


@dataclass(frozen=True)
class ManagedObject:
    label: str
    path: Path
    root: Path
    kind: str
    installed_sha256: str
    content: bytes | None = None


@dataclass(frozen=True)
class RuntimeBinding:
    target_id: str
    runtime: str
    path: Path
    root: Path
    desired: dict[str, list[Any]]


@dataclass(frozen=True)
class InstallPlan:
    config_path: Path
    config: dict[str, Any]
    state_root: Path
    source_root: Path
    artifact: Artifact
    install_root: Path
    engine_root: Path
    receipt_path: Path
    objects: tuple[ManagedObject, ...]
    hook_bindings: tuple[RuntimeBinding, ...]
    binding: BindingEvidence


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_b64(content: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(code, f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(code, f"root must be an object: {path}")
    return payload


def _artifact_candidates(source_root: Path) -> list[Path]:
    paths: list[Path] = []
    for relative in PAYLOAD_FILES:
        path = source_root / relative
        if path.is_file():
            paths.append(path)
    for relative in PAYLOAD_DIRS:
        root = source_root / relative
        if not root.is_dir():
            continue
        paths.extend(
            path for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.relative_to(source_root).parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(paths, key=lambda item: item.relative_to(source_root).as_posix())


def build_release_manifest(source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    entries: list[dict[str, str]] = []
    for path in _artifact_candidates(source_root):
        if path.is_symlink():
            raise ConfigError("FAIL_ARTIFACT_PATH", f"symbolic link in artifact: {path}")
        entries.append({
            "path": path.relative_to(source_root).as_posix(),
            "sha256": _sha256_b64(path.read_bytes()),
        })
    aggregate = _sha256_b64(json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    return {
        "schema": RELEASE_SCHEMA,
        "version": __version__,
        "artifact_sha256": aggregate,
        "files": entries,
    }


def verify_release_manifest(
    source_root: Path, manifest_path: Path, *, expected_version: str | None = None,
) -> Artifact:
    source_root = source_root.resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload: Any = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_ARTIFACT_MANIFEST", f"cannot read {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema", "version", "artifact_sha256", "files",
    }:
        raise ConfigError("FAIL_ARTIFACT_MANIFEST", "release manifest fields mismatch")
    if payload.get("schema") != RELEASE_SCHEMA:
        raise ConfigError("FAIL_ARTIFACT_MANIFEST", f"schema must be {RELEASE_SCHEMA}")
    required_version = __version__ if expected_version is None else expected_version
    if payload.get("version") != required_version:
        raise ConfigError(
            "FAIL_ARTIFACT_VERSION",
            f"manifest={payload.get('version')} expected={required_version}",
        )
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ConfigError("FAIL_ARTIFACT_MANIFEST", "files must be a non-empty list")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256"}:
            raise ConfigError("FAIL_ARTIFACT_MANIFEST", "file entry fields mismatch")
        relative = raw.get("path")
        expected = raw.get("sha256")
        if not isinstance(relative, str) or not relative or relative in seen:
            raise ConfigError("FAIL_ARTIFACT_MANIFEST", f"invalid or duplicate path: {relative!r}")
        path_value = Path(relative)
        if path_value.is_absolute() or ".." in path_value.parts or path_value.as_posix() != relative:
            raise ConfigError("FAIL_ARTIFACT_PATH", relative)
        if (
            not isinstance(expected, str) or len(expected) != 43
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in expected)
        ):
            raise ConfigError("FAIL_ARTIFACT_MANIFEST", f"invalid sha256: {relative}")
        path = source_root / path_value
        if path.is_symlink() or not path.is_file():
            raise ConfigError("FAIL_ARTIFACT_PATH", relative)
        actual = _sha256_b64(path.read_bytes())
        if actual != expected:
            raise ConfigError("FAIL_ARTIFACT_HASH", f"{relative} expected={expected} actual={actual}")
        seen.add(relative)
        entries.append({"path": relative, "sha256": expected})
    if [item["path"] for item in entries] != sorted(seen):
        raise ConfigError("FAIL_ARTIFACT_MANIFEST", "files must be sorted by path")
    expected_paths = {
        path.relative_to(source_root).as_posix() for path in _artifact_candidates(source_root)
    }
    if seen != expected_paths:
        missing = sorted(expected_paths - seen)
        extra = sorted(seen - expected_paths)
        raise ConfigError(
            "FAIL_ARTIFACT_MANIFEST",
            f"payload coverage differs missing={','.join(missing)} extra={','.join(extra)}",
        )
    aggregate = _sha256_b64(json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    if aggregate != payload.get("artifact_sha256"):
        raise ConfigError(
            "FAIL_ARTIFACT_HASH",
            f"aggregate expected={payload.get('artifact_sha256')} actual={aggregate}",
        )
    return Artifact(payload["version"], aggregate, tuple(entries), manifest_bytes)


def _installed_tree_hash(artifact: Artifact) -> str:
    entries = [{
        "path": item["path"],
        "sha256": base64.urlsafe_b64decode(item["sha256"] + "=").hex(),
    } for item in artifact.entries]
    entries.append({"path": "release-manifest.json", "sha256": _sha256(artifact.manifest_bytes)})
    entries.sort(key=lambda item: item["path"])
    return _sha256(json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def _path_hash(path: Path, kind: str) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise ConfigError("FAIL_PATH_ESCAPE", f"managed target is a symbolic link: {path}")
    if kind == "file":
        if not path.is_file():
            raise ConfigError("FAIL_PATH_TYPE", f"expected file: {path}")
        return _sha256(path.read_bytes())
    if not path.is_dir():
        raise ConfigError("FAIL_PATH_TYPE", f"expected directory: {path}")
    entries: list[dict[str, str]] = []
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        if item.is_symlink():
            raise ConfigError("FAIL_PATH_ESCAPE", f"symbolic link below managed directory: {item}")
        if item.is_file():
            entries.append({
                "path": item.relative_to(path).as_posix(), "sha256": _sha256(item.read_bytes()),
            })
    return _sha256(json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def _assert_within(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ConfigError("FAIL_PATH_ESCAPE", f"{path} escaped {root}") from exc
    if path.is_symlink():
        raise ConfigError("FAIL_PATH_ESCAPE", f"managed target is a symbolic link: {path}")


def _atomic_write(path: Path, content: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if executable:
            temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _user_data_root() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if not value:
            raise ConfigError("FAIL_USER_DATA", "LOCALAPPDATA is unavailable")
        return Path(value).resolve() / "agent-core"
    value = os.environ.get("XDG_DATA_HOME")
    return (Path(value).expanduser().resolve() if value else Path.home() / ".local" / "share") / "agent-core"


def _binding(
    config_path: Path,
    explicit_state: Path | None,
    *,
    receipt_override: bytes | None = None,
) -> tuple[dict[str, Any], Path, BindingEvidence]:
    config_path = config_path.resolve()
    config = load_config(config_path)
    configured = config["state_root"]
    if configured.startswith("<") and configured.endswith(">"):
        raise ConfigError("FAIL_STATE_UNBOUND", "install requires state attach first")
    configured_root = Path(configured).expanduser().resolve()
    state_root = (explicit_state or configured_root).expanduser().resolve()
    if state_root != configured_root:
        raise ConfigError("FAIL_STATE_BINDING", "explicit state differs from the attached state_root")
    evidence = validate_state_binding(
        state_root, config_path, receipt_bytes=receipt_override, require_clean_snapshot=True,
    )
    return config, evidence.state_root, evidence


def _wrapper_content() -> tuple[bytes, bytes]:
    posix = (
        "#!/bin/sh\n"
        "launcher=\"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)/agent_core_launcher.py\" || exit 2\n"
        "if [ -n \"$AGENT_CORE_PYTHON\" ]; then exec \"$AGENT_CORE_PYTHON\" \"$launcher\" \"$@\"; fi\n"
        "if command -v python3 >/dev/null 2>&1; then exec python3 \"$launcher\" \"$@\"; fi\n"
        "if command -v python >/dev/null 2>&1; then exec python \"$launcher\" \"$@\"; fi\n"
        "echo 'agent-core: Python 3 is unavailable' >&2\nexit 2\n"
    ).encode("utf-8")
    windows = (
        "@echo off\r\n"
        "setlocal\r\n"
        "if defined AGENT_CORE_PYTHON goto custom_python\r\n"
        "where py >nul 2>nul\r\n"
        "if %ERRORLEVEL% EQU 0 goto py_launcher\r\n"
        "python \"%~dp0agent_core_launcher.py\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
        ":custom_python\r\n"
        "\"%AGENT_CORE_PYTHON%\" \"%~dp0agent_core_launcher.py\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
        ":py_launcher\r\n"
        "py -3 \"%~dp0agent_core_launcher.py\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
    ).encode("utf-8")
    return posix, windows


def _build_plan(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    expected_version: str | None = None,
    binding_receipt_override: bytes | None = None,
) -> InstallPlan:
    source_root = source_root.resolve()
    manifest_path = (manifest_path or source_root / "release-manifest.json").resolve()
    artifact = verify_release_manifest(
        source_root, manifest_path, expected_version=expected_version,
    )
    config, state_root, binding = _binding(
        config_path, explicit_state, receipt_override=binding_receipt_override,
    )
    lock = _load_json(state_root / "agent-core.lock.json", "FAIL_STATE_LOCK")
    if lock.get("engine_version") != artifact.version:
        raise ConfigError(
            "FAIL_ENGINE_VERSION",
            f"state={lock.get('engine_version')} artifact={artifact.version}",
        )
    install_root = _user_data_root()
    engine_destination = install_root / "engine" / artifact.version
    receipt_path = config_path.resolve().parent / "install-receipt.json"
    _assert_within(receipt_path, config_path.resolve().parent)
    objects: list[ManagedObject] = [ManagedObject(
        "engine", engine_destination, install_root, "dir", _installed_tree_hash(artifact), None,
    )]
    posix, windows = _wrapper_content()
    launcher = (source_root / "agent_core" / "launcher.py").read_bytes()
    file_values = (
        ("launcher", install_root / "bin" / "agent_core_launcher.py", launcher),
        ("wrapper-posix", install_root / "bin" / "agent-core", posix),
        ("wrapper-windows", install_root / "bin" / "agent-core.cmd", windows),
    )
    for label, path, content in file_values:
        objects.append(ManagedObject(label, path, install_root, "file", _sha256(content), content))
    for operation in collect_operations(source_root, config, state_root):
        target = next(item for item in config["targets"] if item["id"] == operation.target_id)
        target_root = Path(target["root"]).expanduser().resolve()
        objects.append(ManagedObject(
            f"runtime:{operation.target_id}:{operation.source_label}",
            operation.destination,
            target_root,
            "file",
            _sha256(operation.content),
            operation.content,
        ))
    hook_bindings: list[RuntimeBinding] = []
    for target in config["targets"]:
        runtime = target["runtime"]
        if runtime not in {"claude-code", "codex"}:
            continue
        target_root = Path(target["root"]).expanduser().resolve()
        hook_target = target_root / target["hook_target"]
        fragment = source_root / "runtimes" / runtime / "hook.fragment.json"
        settings = runtime_config_path(runtime, target_root)
        _assert_within(settings, target_root)
        hook_bindings.append(RuntimeBinding(
            target["id"], runtime, settings, target_root,
            render_fragment(fragment, hook_target),
        ))
    pin = _json_bytes({
        "schema": PIN_SCHEMA,
        "version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "config_path": str(config_path.resolve()),
    })
    objects.append(ManagedObject(
        "pin", install_root / "engine-pin.json", install_root, "file", _sha256(pin), pin,
    ))
    seen: set[str] = set()
    for item in objects:
        _assert_within(item.path, item.root)
        key = str(item.path).casefold()
        if key in seen:
            raise ConfigError("FAIL_INSTALL_PLAN", f"duplicate managed path: {item.path}")
        seen.add(key)
    return InstallPlan(
        config_path.resolve(), config, state_root, source_root, artifact, install_root,
        engine_destination, receipt_path, tuple(objects), tuple(hook_bindings), binding,
    )


def _load_receipt(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"missing {path}")
        return None
    payload = _load_json(path, "FAIL_INSTALL_RECEIPT")
    expected = {
        "schema", "engine_version", "artifact_sha256", "config_sha256",
        "state_lock_sha256", "snapshot_path", "objects", "hook_bindings",
    }
    if set(payload) != expected or payload.get("schema") != RECEIPT_SCHEMA:
        raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt fields mismatch")
    if not isinstance(payload.get("objects"), list) or not isinstance(payload.get("hook_bindings"), list):
        raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt collections must be lists")
    return payload


def _receipt_objects(receipt: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if receipt is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    fields = {
        "label", "path", "root", "kind", "before_exists", "before_sha256",
        "installed_sha256", "snapshot_rel",
    }
    for item in receipt["objects"]:
        if not isinstance(item, dict) or set(item) != fields:
            raise ConfigError("FAIL_INSTALL_RECEIPT", "managed object fields mismatch")
        path = item.get("path")
        root = item.get("root")
        snapshot_rel = item.get("snapshot_rel")
        before_hash = item.get("before_sha256")
        if (
            not isinstance(path, str) or not Path(path).is_absolute()
            or not isinstance(root, str) or not Path(root).is_absolute()
            or path.casefold() in result
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", "invalid or duplicate managed path")
        if item.get("kind") not in {"file", "dir"} or not isinstance(item.get("before_exists"), bool):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid managed type: {path}")
        if (
            not isinstance(item.get("installed_sha256"), str)
            or len(item["installed_sha256"]) != 64
            or (before_hash is not None and (not isinstance(before_hash, str) or len(before_hash) != 64))
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid managed hash: {path}")
        if item["before_exists"] != (before_hash is not None):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"before hash/exists differ: {path}")
        if (
            not isinstance(snapshot_rel, str) or not snapshot_rel
            or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid snapshot path: {path}")
        result[path.casefold()] = item
    return result


def _receipt_hook_bindings(receipt: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if receipt is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    fields = {
        "target_id", "runtime", "path", "root", "before_exists", "before_sha256",
        "installed_sha256", "snapshot_rel", "ownership",
    }
    for item in receipt["hook_bindings"]:
        if not isinstance(item, dict) or set(item) != fields:
            raise ConfigError("FAIL_INSTALL_RECEIPT", "hook binding fields mismatch")
        target_id = item.get("target_id")
        path = item.get("path")
        root = item.get("root")
        before_hash = item.get("before_sha256")
        snapshot_rel = item.get("snapshot_rel")
        ownership = item.get("ownership")
        groups = ownership.get("groups") if isinstance(ownership, dict) else None
        valid_groups = (
            isinstance(ownership, dict)
            and set(ownership) == {"hooks_created", "groups"}
            and isinstance(ownership.get("hooks_created"), bool)
            and isinstance(groups, list)
            and [group.get("event") for group in groups if isinstance(group, dict)]
            == ["UserPromptSubmit", "PreToolUse", "Stop"]
            and all(
                set(group) == {
                    "event", "index", "event_created", "original_exists",
                    "original_value", "installed_value",
                }
                and isinstance(group["index"], int) and group["index"] >= 0
                and isinstance(group["event_created"], bool)
                and isinstance(group["original_exists"], bool)
                and isinstance(group["installed_value"], dict)
                and (
                    (group["original_exists"] and isinstance(group["original_value"], dict))
                    or (not group["original_exists"] and group["original_value"] is None)
                )
                for group in (groups or []) if isinstance(group, dict)
            )
            and all(isinstance(group, dict) for group in (groups or []))
        )
        if (
            not isinstance(target_id, str) or not target_id or target_id in result
            or item.get("runtime") not in {"claude-code", "codex"}
            or not isinstance(path, str) or not Path(path).is_absolute()
            or not isinstance(root, str) or not Path(root).is_absolute()
            or not isinstance(item.get("before_exists"), bool)
            or item["before_exists"] != (before_hash is not None)
            or (before_hash is not None and (not isinstance(before_hash, str) or len(before_hash) != 64))
            or not isinstance(item.get("installed_sha256"), str)
            or len(item["installed_sha256"]) != 64
            or not isinstance(snapshot_rel, str) or not snapshot_rel
            or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
            or not valid_groups
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid hook binding: {target_id!r}")
        result[target_id] = item
    return result


def _prepare_hook_bindings(
    plan: InstallPlan, previous: dict[str, Any] | None, *, force: bool,
) -> tuple[
    list[dict[str, Any]], bool, list[tuple[str, Path, str]], list[str],
]:
    previous_bindings = _receipt_hook_bindings(previous)
    expected = {item.target_id for item in plan.hook_bindings}
    if set(previous_bindings) - expected:
        raise ConfigError("INSTALL_CONFLICT", "runtime hook binding set changed; uninstall first")
    records: list[dict[str, Any]] = []
    changed = previous is None
    statuses: list[tuple[str, Path, str]] = []
    conflicts: list[str] = []
    for index, binding in enumerate(plan.hook_bindings):
        junction = getattr(binding.path, "is_junction", None)
        if os.path.lexists(binding.path) and (
            binding.path.is_symlink()
            or (callable(junction) and junction())
            or not binding.path.is_file()
        ):
            changed = True
            label = f"runtime-config:{binding.target_id}"
            statuses.append((label, binding.path, "conflict"))
            conflicts.append(label)
            continue
        _assert_within(binding.path, binding.root)
        current = binding.path.read_bytes() if binding.path.is_file() else None
        old = previous_bindings.get(binding.target_id)
        if old is not None and (
            old["runtime"] != binding.runtime
            or Path(old["path"]).resolve() != binding.path
            or Path(old["root"]).resolve() != binding.root
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"runtime hook identity differs: {binding.target_id}")
        try:
            merged, ownership, item_changed = merge_owned_hooks(
                current, binding.desired, old.get("ownership") if old else None,
                force=force,
            )
        except ConfigError as exc:
            if exc.code != "INSTALL_CONFLICT":
                raise
            changed = True
            label = f"runtime-config:{binding.target_id}"
            statuses.append((label, binding.path, "conflict"))
            conflicts.append(label)
            continue
        changed = changed or item_changed
        status = "identical" if current is not None and _sha256(current) == _sha256(merged) else "missing"
        statuses.append((f"runtime-config:{binding.target_id}", binding.path, status))
        records.append({
            "target_id": binding.target_id,
            "runtime": binding.runtime,
            "path": str(binding.path),
            "root": str(binding.root),
            "before_exists": current is not None,
            "before_sha256": _sha256(current) if current is not None else None,
            "installed_sha256": _sha256(merged),
            "snapshot_rel": f"hook-bindings/{index}",
            "ownership": ownership,
            "content": merged,
            "before_content": current,
        })
    return records, changed, statuses, conflicts


def _classify_install(
    plan: InstallPlan, *, force: bool,
) -> tuple[
    dict[str, Any] | None, bool, list[dict[str, Any]],
    list[tuple[str, Path, str]], list[str],
]:
    previous = _load_receipt(plan.receipt_path)
    previous_objects = _receipt_objects(previous)
    expected_keys = {str(item.path).casefold() for item in plan.objects}
    if previous is not None:
        retired = set(previous_objects) - expected_keys
        allowed_retired = {
            key for key in retired
            if Path(previous_objects[key]["path"]).parent == plan.install_root / "engine"
        }
        if retired != allowed_retired:
            raise ConfigError("INSTALL_CONFLICT", "managed path set changed; uninstall first")
    no_changes = previous is not None
    statuses: list[tuple[str, Path, str]] = []
    conflicts: list[str] = []
    for item in plan.objects:
        current = _path_hash(item.path, item.kind)
        old = previous_objects.get(str(item.path).casefold())
        if current == item.installed_sha256:
            status = "identical"
        elif current is None:
            status = "missing"
        elif force:
            if item.kind == "dir":
                raise ConfigError("FAIL_IMMUTABLE_ARTIFACT", str(item.path))
            status = "missing"
        else:
            status = "conflict"
            conflicts.append(item.label)
        statuses.append((item.label, item.path, status))
        no_changes = no_changes and status == "identical"
    if previous is not None and (
        previous.get("engine_version") != plan.artifact.version
        or previous.get("artifact_sha256") != plan.artifact.artifact_sha256
    ):
        no_changes = False
    hook_bindings, hooks_changed, hook_statuses, hook_conflicts = _prepare_hook_bindings(
        plan, previous, force=force,
    )
    statuses.extend(hook_statuses)
    conflicts.extend(hook_conflicts)
    no_changes = no_changes and not hooks_changed
    return previous, no_changes, hook_bindings, statuses, conflicts


def _preflight(
    plan: InstallPlan, *, force: bool,
) -> tuple[dict[str, Any] | None, bool, list[dict[str, Any]]]:
    previous, no_changes, hook_bindings, _statuses, conflicts = _classify_install(
        plan, force=force,
    )
    if conflicts:
        raise ConfigError("INSTALL_CONFLICT", "install plan ready=false")
    return previous, no_changes, hook_bindings


def plan_install(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    expected_version: str | None = None,
    binding_receipt_override: bytes | None = None,
) -> list[str]:
    plan = _build_plan(
        engine_root, config_path, explicit_state, source_root, manifest_path,
        expected_version=expected_version,
        binding_receipt_override=binding_receipt_override,
    )
    _previous, no_changes, _hook_bindings, statuses, conflicts = _classify_install(
        plan, force=False,
    )
    lines = [
        f"PLAN operation=install version={plan.artifact.version}",
        f"PLAN artifact_sha256={plan.artifact.artifact_sha256}",
    ]
    for label, path, status in statuses:
        lines.append(f"TARGET {label} status={status} path={path}")
    lines.append(
        f"DRY_RUN writes=0 ready={'false' if conflicts else 'true'} "
        f"no_changes={'true' if no_changes else 'false'}"
    )
    return lines


def _copy_path(source: Path, destination: Path, kind: str) -> None:
    if kind == "file":
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return
    shutil.copytree(source, destination)


def _remove_path(path: Path, kind: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or kind == "file":
        path.unlink()
    else:
        shutil.rmtree(path)


def _snapshot(
    plan: InstallPlan,
    previous: dict[str, Any] | None,
    hook_bindings: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot = plan.config_path.parent / "rollback" / f"install-{uuid.uuid4().hex}"
    _assert_within(snapshot, plan.config_path.parent)
    snapshot.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(plan.objects):
            current = _path_hash(item.path, item.kind)
            relative = f"objects/{index}"
            if current is not None:
                _copy_path(item.path, snapshot / relative, item.kind)
            records.append({
                "label": item.label,
                "path": str(item.path),
                "root": str(item.root),
                "kind": item.kind,
                "before_exists": current is not None,
                "before_sha256": current,
                "installed_sha256": item.installed_sha256,
                "snapshot_rel": relative,
            })
        binding_records: list[dict[str, Any]] = []
        for item in hook_bindings:
            record = {key: value for key, value in item.items() if key not in {"content", "before_content"}}
            before = item["before_content"]
            if before is not None:
                destination = snapshot / item["snapshot_rel"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(before)
            binding_records.append(record)
        if previous is not None:
            (snapshot / "previous-receipt.json").write_bytes(_json_bytes(previous))
        _atomic_write(snapshot / "snapshot.json", _json_bytes({
            "schema": SNAPSHOT_SCHEMA, "objects": records, "hook_bindings": binding_records,
        }))
        return snapshot, records, binding_records
    except Exception:
        if snapshot.exists():
            shutil.rmtree(snapshot)
        raise


def _restore(snapshot: Path, records: list[dict[str, Any]]) -> None:
    for record in reversed(records):
        if record["before_sha256"] == record["installed_sha256"]:
            continue
        path = Path(record["path"])
        root = Path(record["root"])
        kind = record["kind"]
        _assert_within(path, root)
        _remove_path(path, kind)
        if record["before_exists"]:
            _copy_path(snapshot / record["snapshot_rel"], path, kind)
        actual = _path_hash(path, kind)
        if actual != record["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", str(path))


def _restore_hook_bindings(snapshot: Path, records: list[dict[str, Any]]) -> None:
    for record in reversed(records):
        if record["before_sha256"] == record["installed_sha256"]:
            continue
        path = Path(record["path"])
        root = Path(record["root"])
        _assert_within(path, root)
        if record["before_exists"]:
            _atomic_write(path, (snapshot / record["snapshot_rel"]).read_bytes())
        else:
            path.unlink(missing_ok=True)
        actual = _sha256(path.read_bytes()) if path.is_file() else None
        if actual != record["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", str(path))


def _install_engine(plan: InstallPlan) -> None:
    if _path_hash(plan.engine_root, "dir") == _installed_tree_hash(plan.artifact):
        return
    plan.engine_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".engine-install-", dir=plan.engine_root.parent))
    try:
        for entry in plan.artifact.entries:
            source = plan.source_root / entry["path"]
            destination = staging / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        (staging / "release-manifest.json").write_bytes(plan.artifact.manifest_bytes)
        if _path_hash(staging, "dir") != _installed_tree_hash(plan.artifact):
            raise ConfigError("FAIL_ARTIFACT_HASH", "installed staging tree differs")
        os.replace(staging, plan.engine_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_installed(
    plan: InstallPlan,
    hook_bindings: list[dict[str, Any]],
    *,
    include_launcher: bool,
) -> None:
    for item in plan.objects:
        if item.label == "pin" and not include_launcher:
            continue
        if _path_hash(item.path, item.kind) != item.installed_sha256:
            raise ConfigError("FAIL_INSTALL_VERIFY", str(item.path))
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(plan.engine_root)
    commands = [
        [
            sys.executable, "-P", "-m", "agent_core.ledger",
            str(plan.state_root / "experience" / "LESSONS.md"), "--all-profiles",
        ],
        [
            sys.executable, "-P", "-m", "agent_core.cli", "lessons", "match",
            "--ledger", str(plan.state_root / "experience" / "LESSONS.md"),
            "--stage", "prompt", "--text", "synthetic verification",
        ],
    ]
    for command in commands:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, encoding="utf-8",
            env=environment, timeout=30,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ConfigError("FAIL_INSTALL_VERIFY", detail)
    manifest = _load_json(plan.engine_root / "manifest.yaml", "FAIL_INSTALL_VERIFY")
    for capability in manifest.get("capabilities", []):
        if capability.get("kind") == "skill" and not (
            plan.engine_root / capability.get("source", "")
        ).is_dir():
            raise ConfigError("FAIL_INSTALL_VERIFY", f"missing {capability.get('source')}")
    for target in plan.config["targets"]:
        hook = runtime_hook_path(
            Path(target["root"]).expanduser().resolve() / target["hook_target"]
        )
        try:
            hook_retrieval_status(hook)
        except ConfigError as exc:
            raise ConfigError("FAIL_INSTALL_VERIFY", str(exc)) from exc
    bindings_by_id = {item["target_id"]: item for item in hook_bindings}
    for binding in plan.hook_bindings:
        record = bindings_by_id[binding.target_id]
        if not binding.path.is_file() or _sha256(binding.path.read_bytes()) != record["installed_sha256"]:
            raise ConfigError("FAIL_INSTALL_VERIFY", str(binding.path))
        merged, _ownership, changed = merge_owned_hooks(
            binding.path.read_bytes(), binding.desired, record["ownership"], force=False,
        )
        if changed or merged != binding.path.read_bytes():
            raise ConfigError("FAIL_INSTALL_VERIFY", f"runtime caller differs: {binding.target_id}")
    if include_launcher:
        launcher = plan.install_root / "bin" / "agent_core_launcher.py"
        completed = subprocess.run(
            [sys.executable, str(launcher), "--state", str(plan.state_root), "--version"],
            check=False, capture_output=True, text=True, encoding="utf-8",
            env=environment, timeout=30,
        )
        if completed.returncode != 0 or completed.stdout.strip() != plan.artifact.version:
            raise ConfigError(
                "FAIL_INSTALL_VERIFY", completed.stderr.strip() or "stable launcher version mismatch",
            )


def apply_install(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    force: bool,
    expected_version: str | None = None,
) -> list[str]:
    plan = _build_plan(
        engine_root, config_path, explicit_state, source_root, manifest_path,
        expected_version=expected_version,
    )
    return _apply_install_plan(plan, force=force)


def _apply_install_plan(plan: InstallPlan, *, force: bool) -> list[str]:
    binding = validate_state_binding(
        plan.state_root,
        plan.config_path,
        require_clean_snapshot=False,
        require_remote_observation=False,
        expected_remote_revision=plan.binding.remote_revision,
    )
    if binding != plan.binding:
        raise ConfigError("FAIL_STATE_BINDING", "binding changed after install plan")
    previous, no_changes, hook_bindings = _preflight(plan, force=force)
    if no_changes:
        return [f"PASS install version={plan.artifact.version} no_changes=true"]
    require_fresh(plan.state_root, "sync", plan.config_path.parent / "txn")
    snapshot, records, binding_records = _snapshot(plan, previous, hook_bindings)
    try:
        _install_engine(plan)
        for item in plan.objects:
            if item.kind != "file" or item.label == "pin":
                continue
            if _path_hash(item.path, item.kind) == item.installed_sha256:
                continue
            _assert_within(item.path, item.root)
            _atomic_write(
                item.path, item.content or b"", executable=item.label == "wrapper-posix",
            )
        for item in hook_bindings:
            if item["before_sha256"] == item["installed_sha256"]:
                continue
            _atomic_write(Path(item["path"]), item["content"])
        _verify_installed(plan, hook_bindings, include_launcher=False)
        pin = next(item for item in plan.objects if item.label == "pin")
        if _path_hash(pin.path, pin.kind) != pin.installed_sha256:
            _atomic_write(pin.path, pin.content or b"")
        _verify_installed(plan, hook_bindings, include_launcher=True)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "engine_version": plan.artifact.version,
            "artifact_sha256": plan.artifact.artifact_sha256,
            "config_sha256": _sha256(plan.config_path.read_bytes()),
            "state_lock_sha256": _sha256((plan.state_root / "agent-core.lock.json").read_bytes()),
            "snapshot_path": str(snapshot),
            "objects": records,
            "hook_bindings": binding_records,
        }
        _atomic_write(plan.receipt_path, _json_bytes(receipt))
    except Exception as exc:
        try:
            _restore_hook_bindings(snapshot, binding_records)
            _restore(snapshot, records)
            if previous is None:
                plan.receipt_path.unlink(missing_ok=True)
            else:
                _atomic_write(plan.receipt_path, _json_bytes(previous))
            shutil.rmtree(snapshot)
            for directory in (
                plan.install_root / "bin", plan.install_root / "engine", plan.install_root,
            ):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        except Exception as rollback_exc:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", str(rollback_exc)) from rollback_exc
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("FAIL_INSTALL", str(exc)) from exc
    return [
        f"APPLIED install version={plan.artifact.version} objects={len(records)} hook_bindings={len(binding_records)}",
        f"PASS artifact_sha256={plan.artifact.artifact_sha256} receipt={plan.receipt_path}",
    ]


def _uninstall_plan(
    config_path: Path,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    config = load_config(config_path.resolve())
    install_root = _user_data_root()
    allowed_roots = {str(install_root).casefold()}
    allowed_roots.update(
        str(Path(target["root"]).expanduser().resolve()).casefold()
        for target in config["targets"]
    )
    receipt_path = config_path.resolve().parent / "install-receipt.json"
    receipt = _load_receipt(receipt_path, required=True)
    assert receipt is not None
    objects = list(_receipt_objects(receipt).values())
    hook_bindings = list(_receipt_hook_bindings(receipt).values())
    for item in objects:
        path = Path(item["path"])
        root = Path(item["root"])
        if str(root.resolve()).casefold() not in allowed_roots:
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"unapproved managed root: {root}")
        _assert_within(path, root)
        actual = _path_hash(path, item["kind"])
        if actual != item["installed_sha256"]:
            raise ConfigError("UNINSTALL_CONFLICT", f"modified managed object: {path}")
    expected_targets = {
        target["id"]: (
            target["runtime"], Path(target["root"]).expanduser().resolve(),
        )
        for target in config["targets"] if target["runtime"] in {"claude-code", "codex"}
    }
    if set(expected_targets) != {item["target_id"] for item in hook_bindings}:
        raise ConfigError("FAIL_INSTALL_RECEIPT", "runtime hook target set differs")
    for item in hook_bindings:
        path = Path(item["path"])
        root = Path(item["root"])
        expected_runtime, expected_root = expected_targets[item["target_id"]]
        expected_path = runtime_config_path(expected_runtime, expected_root)
        if (
            item["runtime"] != expected_runtime or root.resolve() != expected_root
            or path.resolve() != expected_path.resolve()
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"runtime hook identity differs: {item['target_id']}")
        if str(root.resolve()).casefold() not in allowed_roots:
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"unapproved runtime root: {root}")
        _assert_within(path, root)
        if not path.is_file():
            raise ConfigError("UNINSTALL_CONFLICT", f"runtime config missing: {path}")
        remove_owned_hooks(path.read_bytes(), item["ownership"])
    snapshot = Path(receipt["snapshot_path"])
    rollback_root = config_path.resolve().parent / "rollback"
    _assert_within(snapshot, rollback_root)
    if not (snapshot / "snapshot.json").is_file():
        raise ConfigError("FAIL_INSTALL_RECEIPT", f"missing snapshot: {snapshot}")
    snapshot_manifest = _load_json(snapshot / "snapshot.json", "FAIL_INSTALL_RECEIPT")
    if (
        set(snapshot_manifest) != {"schema", "objects", "hook_bindings"}
        or snapshot_manifest.get("schema") != SNAPSHOT_SCHEMA
        or snapshot_manifest.get("objects") != receipt["objects"]
        or snapshot_manifest.get("hook_bindings") != receipt["hook_bindings"]
    ):
        raise ConfigError("FAIL_INSTALL_RECEIPT", "snapshot manifest differs from receipt")
    for item in objects:
        if not item["before_exists"]:
            continue
        snapshotted = snapshot / item["snapshot_rel"]
        if _path_hash(snapshotted, item["kind"]) != item["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"snapshot hash differs: {item['path']}")
    for item in hook_bindings:
        if not item["before_exists"]:
            continue
        snapshotted = snapshot / item["snapshot_rel"]
        if not snapshotted.is_file() or _sha256(snapshotted.read_bytes()) != item["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"runtime snapshot hash differs: {item['path']}")
    return receipt_path, receipt, objects, hook_bindings


def plan_uninstall(config_path: Path) -> list[str]:
    _receipt_path, receipt, objects, hook_bindings = _uninstall_plan(config_path)
    lines = [f"PLAN operation=uninstall version={receipt['engine_version']}"]
    lines.extend(f"TARGET remove-or-restore path={item['path']}" for item in objects)
    lines.extend(f"TARGET remove-hook-binding path={item['path']}" for item in hook_bindings)
    lines.append("DRY_RUN writes=0")
    return lines


def apply_uninstall(config_path: Path) -> list[str]:
    receipt_path, receipt, objects, hook_bindings = _uninstall_plan(config_path)
    snapshot = Path(receipt["snapshot_path"])
    for item in hook_bindings:
        path = Path(item["path"])
        cleaned = remove_owned_hooks(path.read_bytes(), item["ownership"])
        if not item["before_exists"] and json.loads(cleaned) == {}:
            path.unlink()
        else:
            _atomic_write(path, cleaned)
    _restore(snapshot, objects)
    previous_receipt = snapshot / "previous-receipt.json"
    if previous_receipt.is_file():
        _atomic_write(receipt_path, previous_receipt.read_bytes())
    else:
        receipt_path.unlink()
    shutil.rmtree(snapshot)
    return [
        f"APPLIED uninstall objects={len(objects)} hook_bindings={len(hook_bindings)}",
        "PASS uninstall",
    ]


def _parser(engine_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--state", type=Path)
    install.add_argument("--source", type=Path, default=engine_root)
    install.add_argument("--artifact-manifest", type=Path)
    install.add_argument("--apply", action="store_true")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--config", type=Path, required=True)
    uninstall.add_argument("--apply", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    engine_root: Path | None = None,
) -> int:
    root = (engine_root or Path(__file__).resolve().parents[1]).resolve()
    args = _parser(root).parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "install":
            lines = apply_install(
                root, args.config, args.state, args.source, args.artifact_manifest,
                force=False,
            ) if args.apply else plan_install(
                root, args.config, args.state, args.source, args.artifact_manifest,
            )
        else:
            lines = apply_uninstall(args.config) if args.apply else plan_uninstall(args.config)
        print(*lines, sep="\n")
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
