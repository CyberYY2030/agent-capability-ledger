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
import re
from dataclasses import dataclass, replace
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
from .promote import operation_lock
from . import state as state_module
from .state import BindingEvidence, binding_receipt_path, validate_state_binding
from .sync import collect_operations


RELEASE_SCHEMA = "release-manifest/1"
RECEIPT_SCHEMA = "install-receipt/1"
SNAPSHOT_SCHEMA = "install-snapshot/1"
PENDING_SCHEMA = "install-pending/1"
PIN_SCHEMA = "engine-pin/1"
INSTALL_PLAN_SCHEMA = "install-plan/1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
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
    binding_pending: bool = False
    confirm_private_remote: bool = False
    binding_config_preimage_sha256: str | None = None
    binding_receipt_preimage_sha256: str | None = None
    binding_receipt_preimage_exists: bool = False
    artifact_manifest_path: Path | None = None
    plan_hash: str = ""
    object_preimages: tuple[str | None, ...] = ()
    hook_preimages: tuple[str | None, ...] = ()
    receipt_preimage_sha256: str | None = None
    receipt_preimage_exists: bool = False


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_b64(content: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _raw_file_sha256(path: Path) -> tuple[bool, str | None]:
    """Return an ordinary file's immutable reviewed fact without following aliases."""
    junction = getattr(path, "is_junction", None)
    if not os.path.lexists(path):
        return False, None
    if path.is_symlink() or (callable(junction) and junction()) or not path.is_file():
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed file identity changed")
    try:
        return True, _sha256(path.read_bytes())
    except (OSError, UnicodeError) as exc:
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed file is unreadable") from exc


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
        _flush_parent(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _flush_parent(path: Path) -> None:
    """Durably publish metadata where the platform exposes directory fsync."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ConfigError("FAIL_INSTALL_SNAPSHOT", "snapshot parent is unavailable") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ConfigError("FAIL_INSTALL_SNAPSHOT", "snapshot parent cannot be flushed") from exc
    finally:
        os.close(descriptor)


def _flush_file(path: Path) -> None:
    try:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ConfigError("FAIL_INSTALL_SNAPSHOT", "snapshot material cannot be flushed") from exc


def _move_no_replace(source: Path, destination: Path) -> None:
    """Move a staged or detached object without ever replacing a recreated path."""
    if os.path.lexists(destination):
        raise ConfigError("FAIL_INSTALL_RACE", "destination was recreated during install")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            # Windows rename refuses an existing destination; no REPLACE_EXISTING flag is used.
            os.rename(source, destination)
        elif source.is_file():
            # A hard-link publication is exclusive on POSIX; directory replacement is not.
            os.link(source, destination)
            source.unlink()
        else:
            raise ConfigError("FAIL_INSTALL_RACE", "no-replace directory placement is unavailable")
    except FileExistsError as exc:
        raise ConfigError("FAIL_INSTALL_RACE", "destination was recreated during install") from exc
    except OSError as exc:
        raise ConfigError("FAIL_INSTALL_RACE", "no-replace placement failed") from exc


def _detach_owned(
    path: Path,
    root: Path,
    kind: str,
    expected: str | None,
    snapshot: Path,
    key: str,
) -> Path | None:
    """Detach only a reviewed managed pre-image into this invocation's durable area."""
    _assert_within(path, root)
    current = _path_hash(path, kind)
    if current != expected:
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed target changed before detach")
    if expected is None:
        return None
    detached = snapshot / "detached" / key
    _move_no_replace(path, detached)
    if _path_hash(detached, kind) != expected:
        try:
            _move_no_replace(detached, path)
        except ConfigError:
            pass
        raise ConfigError("FAIL_INSTALL_RACE", "detached pre-image differs from reviewed plan")
    _flush_snapshot_material(detached, kind)
    return detached


def _stage_bytes(snapshot: Path, key: str, content: bytes, *, executable: bool = False) -> Path:
    staged = snapshot / "staged" / key
    _atomic_write(staged, content, executable=executable)
    return staged


def _replace_owned_bytes(
    path: Path,
    root: Path,
    expected: str | None,
    desired: bytes,
    snapshot: Path,
    key: str,
    *,
    executable: bool = False,
) -> None:
    staged = _stage_bytes(snapshot, key, desired, executable=executable)
    _detach_owned(path, root, "file", expected, snapshot, key)
    _move_no_replace(staged, path)
    if _path_hash(path, "file") != _sha256(desired):
        raise ConfigError("FAIL_INSTALL_RACE", "placed target differs from reviewed desired bytes")


def _user_data_root() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if not value:
            raise ConfigError("FAIL_USER_DATA", "LOCALAPPDATA is unavailable")
        return Path(value).resolve() / "agent-core"
    value = os.environ.get("XDG_DATA_HOME")
    return (Path(value).expanduser().resolve() if value else Path.home() / ".local" / "share") / "agent-core"


def _prospective_binding(
    config_path: Path,
    explicit_state: Path,
    config: dict[str, Any],
    receipt_path: Path,
) -> tuple[dict[str, Any], Path, BindingEvidence, str, str | None, bool]:
    context, _old_config, config_bytes, lock_bytes, remote_hash, remote_sha, root_sha, provenance_sha = state_module._validate_attach(
        explicit_state, config_path, confirm_private_remote=True,
    )
    config["state_root"] = str(context.state_root)
    rendered = _json_bytes(config)
    payload: dict[str, Any] = {
        "schema": state_module.BINDING_SCHEMA_V2 if context.layout == "canonical" else state_module.BINDING_SCHEMA_V1,
        "state_root": str(context.state_root), "remote_name": "origin",
        "remote_url_sha256": remote_hash, "remote_revision": remote_sha,
        "state_lock_sha256": _sha256(lock_bytes), "config_sha256": _sha256(rendered),
        "confirmed_private_remote": True,
    }
    if context.layout == "canonical":
        payload["repository_root_sha"] = root_sha
        payload["engine_provenance_sha256"] = provenance_sha
    raw = _json_bytes(payload)
    receipt_exists, receipt_sha256 = _raw_file_sha256(receipt_path)
    return config, context.state_root, BindingEvidence(
        context.layout, payload["schema"], receipt_path, _sha256(raw), context.state_root,
        remote_hash, remote_sha, root_sha, provenance_sha, _sha256(rendered), _sha256(lock_bytes),
    ), _sha256(config_bytes), receipt_sha256, receipt_exists


def _binding(
    config_path: Path,
    explicit_state: Path | None,
    *,
    receipt_override: bytes | None = None,
    confirm_private_remote: bool = False,
) -> tuple[dict[str, Any], Path, BindingEvidence, bool, str | None, str | None, bool]:
    config_path = config_path.resolve()
    config = load_config(config_path)
    configured = config["state_root"]
    receipt_path = binding_receipt_path(config_path)
    if not receipt_path.is_file():
        if not confirm_private_remote:
            raise ConfigError("PRIVATE_REMOTE_CONFIRMATION_REQUIRED", "install first binding requires confirmation")
        if explicit_state is None:
            raise ConfigError("FAIL_STATE_UNBOUND", "install first binding requires --state")
        config, state_root, evidence, config_sha256, receipt_sha256, receipt_exists = _prospective_binding(
            config_path, explicit_state, config, receipt_path,
        )
        return config, state_root, evidence, True, config_sha256, receipt_sha256, receipt_exists
    if configured.startswith("<") and configured.endswith(">"):
        raise ConfigError("FAIL_STATE_UNBOUND", "attached binding requires concrete state_root")
    configured_root = Path(configured).expanduser().resolve()
    state_root = (explicit_state or configured_root).expanduser().resolve()
    if state_root != configured_root:
        raise ConfigError("FAIL_STATE_BINDING", "explicit state differs from the attached state_root")
    try:
        evidence = validate_state_binding(
            state_root, config_path, receipt_bytes=receipt_override, require_clean_snapshot=True,
        )
    except ConfigError as exc:
        if exc.code != "FAIL_STATE_BINDING" or not confirm_private_remote:
            raise
        if explicit_state is None:
            raise ConfigError("FAIL_STATE_UNBOUND", "binding reacceptance requires --state") from None
        if explicit_state.expanduser().resolve() != configured_root:
            raise ConfigError("FAIL_STATE_BINDING", "explicit state differs from the attached state_root") from None
        config, state_root, evidence, config_sha256, receipt_sha256, receipt_exists = _prospective_binding(
            config_path, state_root, config, receipt_path,
        )
        return config, state_root, evidence, True, config_sha256, receipt_sha256, receipt_exists
    return config, evidence.state_root, evidence, False, None, None, False


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
    confirm_private_remote: bool = False,
) -> InstallPlan:
    source_root = source_root.resolve()
    manifest_path = (manifest_path or source_root / "release-manifest.json").resolve()
    artifact = verify_release_manifest(
        source_root, manifest_path, expected_version=expected_version,
    )
    config, state_root, binding, binding_pending, binding_config_preimage_sha256, binding_receipt_preimage_sha256, binding_receipt_preimage_exists = _binding(
        config_path, explicit_state, receipt_override=binding_receipt_override,
        confirm_private_remote=confirm_private_remote,
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
        binding_pending, confirm_private_remote, binding_config_preimage_sha256,
        binding_receipt_preimage_sha256, binding_receipt_preimage_exists, manifest_path,
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
    if (
        not isinstance(payload.get("engine_version"), str)
        or not isinstance(payload.get("artifact_sha256"), str)
        or not _is_sha256(payload.get("config_sha256"))
        or not _is_sha256(payload.get("state_lock_sha256"))
        or not isinstance(payload.get("snapshot_path"), str)
        or not Path(payload["snapshot_path"]).is_absolute()
    ):
        raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt header is invalid")
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
            not _is_sha256(item.get("installed_sha256"))
            or (before_hash is not None and not _is_sha256(before_hash))
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid managed hash: {path}")
        if item["before_exists"] != (before_hash is not None):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"before hash/exists differ: {path}")
        if (
            not isinstance(snapshot_rel, str) or not snapshot_rel
            or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid snapshot path: {path}")
        try:
            _assert_within(Path(path), Path(root))
        except (ConfigError, OSError, ValueError) as exc:
            raise ConfigError("FAIL_INSTALL_RECEIPT", "managed path is outside its root") from exc
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
            or (before_hash is not None and not _is_sha256(before_hash))
            or not _is_sha256(item.get("installed_sha256"))
            or not isinstance(snapshot_rel, str) or not snapshot_rel
            or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
            or not valid_groups
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", f"invalid hook binding: {target_id!r}")
        try:
            _assert_within(Path(path), Path(root))
        except (ConfigError, OSError, ValueError) as exc:
            raise ConfigError("FAIL_INSTALL_RECEIPT", "hook path is outside its root") from exc
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
            # Public install deliberately has no force path: every pre-existing
            # unmanaged hook conflict remains a whole-plan zero-write conflict.
            _ = force
            merged, ownership, item_changed = merge_owned_hooks(
                current, binding.desired, old.get("ownership") if old else None,
                force=False, adopt_identical=old is None and current is not None,
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
        adopt_identical = (
            old is None
            and current is not None
            and not item_changed
            and current == merged
        )
        status = "adopt-identical" if adopt_identical else (
            "identical" if current is not None and _sha256(current) == _sha256(merged) else "missing"
        )
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
            "adopt_identical": adopt_identical,
            "content": merged,
            "before_content": current,
        })
    return records, changed, statuses, conflicts


def _validate_receipt_identity(plan: InstallPlan, previous: dict[str, Any] | None, objects: dict[str, dict[str, Any]]) -> None:
    """A malformed or redirected receipt is indeterminate, never overwrite authority."""
    if previous is None:
        return
    if not isinstance(previous["engine_version"], str):
        raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt engine version is invalid")
    expected = {str(item.path).casefold(): item for item in plan.objects}
    for key, recorded in objects.items():
        current = expected.get(key)
        if current is None:
            # Historic engine versions are the one deliberate managed retirement.
            if Path(recorded["path"]).parent == plan.install_root / "engine":
                continue
            raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt has an unexpected managed path")
        if (
            recorded["label"] != current.label
            or recorded["kind"] != current.kind
            or Path(recorded["root"]).resolve() != current.root.resolve()
            or Path(recorded["path"]).resolve() != current.path.resolve()
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", "receipt managed identity differs from current plan")


def _classify_install(
    plan: InstallPlan, *, force: bool,
) -> tuple[
    dict[str, Any] | None, bool, list[dict[str, Any]],
    list[tuple[str, Path, str]], list[str],
]:
    try:
        previous = _load_receipt(plan.receipt_path)
        previous_objects = _receipt_objects(previous)
        _validate_receipt_identity(plan, previous, previous_objects)
    except ConfigError:
        statuses = [(item.label, item.path, "indeterminate") for item in plan.objects]
        return None, False, [], statuses, [item.label for item in plan.objects]
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
        try:
            current = _path_hash(item.path, item.kind)
        except ConfigError:
            statuses.append((item.label, item.path, "indeterminate"))
            conflicts.append(item.label)
            continue
        old = previous_objects.get(str(item.path).casefold())
        if current == item.installed_sha256:
            status = "identical"
        elif current is None:
            status = "absent"
        elif (
            old is not None
            and old["kind"] == item.kind
            and Path(old["path"]).resolve() == item.path.resolve()
            and old["installed_sha256"] == current
        ):
            status = "managed-update"
        else:
            status = "foreign"
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
    for label, path, status in hook_statuses:
        if status == "conflict":
            statuses.append((label, path, "foreign"))
        elif status == "missing":
            statuses.append((label, path, "absent" if not path.exists() else "managed-update"))
        else:
            statuses.append((label, path, status))
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


def _reviewed_install_plan(
    plan: InstallPlan,
) -> tuple[InstallPlan, dict[str, Any] | None, bool, list[dict[str, Any]], list[tuple[str, Path, str]], list[str]]:
    """Freeze every fact that gives install authority; callers never print payload."""
    previous, no_changes, hook_bindings, statuses, conflicts = _classify_install(plan, force=False)
    object_preimages: list[str | None] = []
    for item in plan.objects:
        try:
            object_preimages.append(_path_hash(item.path, item.kind))
        except ConfigError:
            object_preimages.append(None)
    hook_preimages: list[str | None] = []
    for binding in plan.hook_bindings:
        try:
            exists, digest = _raw_file_sha256(binding.path)
            hook_preimages.append(digest if exists else None)
        except ConfigError:
            hook_preimages.append(None)
    try:
        receipt_exists, receipt_sha256 = _raw_file_sha256(plan.receipt_path)
    except ConfigError:
        receipt_exists, receipt_sha256 = True, None
        conflicts.append("install-receipt")
    try:
        config_exists, config_sha256 = _raw_file_sha256(plan.config_path)
        manifest_exists, manifest_sha256 = _raw_file_sha256(plan.artifact_manifest_path or (plan.source_root / "release-manifest.json"))
    except ConfigError:
        config_exists = manifest_exists = False
        config_sha256 = manifest_sha256 = None
        conflicts.append("reviewed-input")
    ownership = [
        _sha256(_canonical_bytes(item["ownership"]))
        for item in hook_bindings
    ]
    payload = {
        "schema": INSTALL_PLAN_SCHEMA,
        "config_sha256": config_sha256 if config_exists else None,
        "state_identity_sha256": _sha256(str(plan.state_root.resolve()).encode("utf-8")),
        "binding": {
            "layout": plan.binding.layout,
            "schema": plan.binding.schema,
            "receipt_sha256": plan.binding.receipt_sha256,
            "remote_url_sha256": plan.binding.remote_url_sha256,
            "remote_revision": plan.binding.remote_revision,
            "repository_root_sha": plan.binding.repository_root_sha,
            "engine_provenance_sha256": plan.binding.engine_provenance_sha256,
            "config_sha256": plan.binding.config_sha256,
            "state_lock_sha256": plan.binding.state_lock_sha256,
        },
        "binding_pending": plan.binding_pending,
        "binding_refresh": {
            "config_preimage_sha256": plan.binding_config_preimage_sha256,
            "receipt_preimage_exists": plan.binding_receipt_preimage_exists,
            "receipt_preimage_sha256": plan.binding_receipt_preimage_sha256,
        },
        "manifest_sha256": manifest_sha256 if manifest_exists else None,
        "artifact": {"version": plan.artifact.version, "sha256": plan.artifact.artifact_sha256},
        "receipt": {"exists": receipt_exists, "sha256": receipt_sha256},
        "objects": [
            {
                "label": item.label,
                "path_identity_sha256": _sha256(str(item.path.resolve()).encode("utf-8")),
                "kind": item.kind,
                "desired_sha256": item.installed_sha256,
                "current_sha256": current,
            }
            for item, current in zip(plan.objects, object_preimages)
        ],
        "hooks": [
            {
                "target_id": binding.target_id,
                "path_identity_sha256": _sha256(str(binding.path.resolve()).encode("utf-8")),
                "current_sha256": current,
                "desired_sha256": hook_bindings[index]["installed_sha256"] if index < len(hook_bindings) else None,
                "ownership_sha256": ownership[index] if index < len(ownership) else None,
                "adopt_identical": hook_bindings[index]["adopt_identical"] if index < len(hook_bindings) else None,
            }
            for index, (binding, current) in enumerate(zip(plan.hook_bindings, hook_preimages))
        ],
    }
    token = _sha256(_canonical_bytes(payload))
    reviewed = replace(
        plan, plan_hash=token, object_preimages=tuple(object_preimages),
        hook_preimages=tuple(hook_preimages), receipt_preimage_sha256=receipt_sha256,
        receipt_preimage_exists=receipt_exists,
    )
    return reviewed, previous, no_changes, hook_bindings, statuses, conflicts


def _assert_reviewed_plan_current(plan: InstallPlan) -> tuple[InstallPlan, dict[str, Any] | None, bool, list[dict[str, Any]]]:
    reviewed, previous, no_changes, hook_bindings, _statuses, conflicts = _reviewed_install_plan(plan)
    if plan.plan_hash and reviewed.plan_hash != plan.plan_hash:
        raise ConfigError("FAIL_PLAN_HASH", "install inputs changed after reviewed plan")
    if conflicts:
        raise ConfigError("INSTALL_CONFLICT", "install plan ready=false")
    return reviewed, previous, no_changes, hook_bindings


def _assert_pending_binding_current(plan: InstallPlan) -> None:
    """Rebuild the prospective attach evidence before any install-side write."""
    if not plan.binding_pending:
        return
    config, state_root, binding, pending, config_sha256, receipt_sha256, receipt_exists = _binding(
        plan.config_path, plan.state_root, confirm_private_remote=plan.confirm_private_remote,
    )
    if (
        not pending or state_root != plan.state_root or config != plan.config or binding != plan.binding
        or config_sha256 != plan.binding_config_preimage_sha256
        or receipt_exists != plan.binding_receipt_preimage_exists
        or receipt_sha256 != plan.binding_receipt_preimage_sha256
    ):
        raise ConfigError("FAIL_STATE_BINDING", "binding changed after install plan")


def _assert_object_preimage(plan: InstallPlan, index: int) -> None:
    item = plan.objects[index]
    if index >= len(plan.object_preimages) or _path_hash(item.path, item.kind) != plan.object_preimages[index]:
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed target changed before replacement")


def _assert_hook_preimage(plan: InstallPlan, index: int, path: Path) -> None:
    exists, digest = _raw_file_sha256(path)
    current = digest if exists else None
    if index >= len(plan.hook_preimages) or current != plan.hook_preimages[index]:
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed hook changed before replacement")


def _assert_receipt_preimage(plan: InstallPlan) -> None:
    exists, digest = _raw_file_sha256(plan.receipt_path)
    if exists != plan.receipt_preimage_exists or digest != plan.receipt_preimage_sha256:
        raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed install receipt changed before publication")


def plan_install(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    expected_version: str | None = None,
    binding_receipt_override: bytes | None = None,
    confirm_private_remote: bool = False,
) -> list[str]:
    pending = _read_pending(config_path)
    if pending is not None:
        raise ConfigError(
            "FAIL_INSTALL_RECOVERY",
            f"inspect and clear pending install marker before replanning: {pending}",
        )
    plan = _build_plan(
        engine_root, config_path, explicit_state, source_root, manifest_path,
        expected_version=expected_version,
        binding_receipt_override=binding_receipt_override,
        confirm_private_remote=confirm_private_remote,
    )
    plan, _previous, no_changes, _hook_bindings, statuses, conflicts = _reviewed_install_plan(plan)
    lines = [
        f"PLAN operation=install version={plan.artifact.version}",
        f"PLAN artifact_sha256={plan.artifact.artifact_sha256}",
    ]
    if plan.binding_pending:
        lines.append("BINDING_REFRESH pending=true")
    for label, path, status in statuses:
        lines.append(f"TARGET {label} status={status} path={path}")
    if not conflicts:
        lines.append(f"EXPECTED_REMOTE_SHA {plan.binding.remote_revision}")
        lines.append(f"PLAN_HASH {plan.plan_hash}")
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
    *,
    host_paths: tuple[Path, ...] = (),
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    snapshot = plan.config_path.parent / "rollback" / f"install-{uuid.uuid4().hex}"
    _assert_within(snapshot, plan.config_path.parent)
    snapshot.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(plan.objects):
            current = _path_hash(item.path, item.kind)
            if index >= len(plan.object_preimages) or current != plan.object_preimages[index]:
                raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed target changed during snapshot")
            relative = f"objects/{index}"
            if current is not None:
                _copy_path(item.path, snapshot / relative, item.kind)
                _flush_snapshot_material(snapshot / relative, item.kind)
                if _path_hash(item.path, item.kind) != plan.object_preimages[index]:
                    raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed target changed during snapshot copy")
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
        for index, item in enumerate(hook_bindings):
            record = {
                key: value for key, value in item.items()
                if key not in {"content", "before_content", "adopt_identical"}
            }
            before = item["before_content"]
            expected = plan.hook_preimages[index] if index < len(plan.hook_preimages) else None
            actual = _sha256(before) if before is not None else None
            if actual != expected:
                raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed hook changed during snapshot")
            if before is not None:
                destination = snapshot / item["snapshot_rel"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(before)
                _flush_file(destination)
                _flush_parent(destination.parent)
                exists, digest = _raw_file_sha256(Path(item["path"]))
                if not exists or digest != expected:
                    raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed hook changed during snapshot copy")
            binding_records.append(record)
        host_records: list[dict[str, Any]] = []
        for index, path in enumerate(host_paths):
            _assert_within(path, plan.config_path.parent)
            exists, digest = _raw_file_sha256(path)
            relative = f"host/{index}"
            if exists:
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
                _flush_file(destination)
                _flush_parent(destination.parent)
            host_records.append({
                "path": str(path), "before_exists": exists, "before_sha256": digest,
                "snapshot_rel": relative,
            })
        if previous is not None and not host_paths:
            previous_receipt = snapshot / "previous-receipt.json"
            previous_receipt.write_bytes(_json_bytes(previous))
            _flush_file(previous_receipt)
            _flush_parent(previous_receipt.parent)
        _atomic_write(snapshot / "snapshot.json", _json_bytes({
            "schema": SNAPSHOT_SCHEMA, "objects": records, "hook_bindings": binding_records,
            "host_records": host_records,
        }))
        _flush_parent(snapshot)
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
        current = _path_hash(path, kind)
        if current == record["before_sha256"]:
            continue
        if current not in {None, record["installed_sha256"]}:
            # A non-cooperating writer won the race. Preserve both its bytes and our snapshot.
            continue
        if current is not None:
            _remove_path(path, kind)
        if record["before_exists"]:
            _restore_material_no_replace(snapshot / record["snapshot_rel"], path, kind)
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
        current = _path_hash(path, "file")
        if current == record["before_sha256"]:
            continue
        if current not in {None, record["installed_sha256"]}:
            continue
        if record["before_exists"]:
            if current is not None:
                path.unlink()
            _restore_material_no_replace(snapshot / record["snapshot_rel"], path, "file")
        else:
            if current is not None:
                path.unlink()
        actual = _sha256(path.read_bytes()) if path.is_file() else None
        if actual != record["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", str(path))


def _restore_material_no_replace(source: Path, destination: Path, kind: str) -> None:
    if os.path.lexists(destination):
        raise ConfigError("FAIL_INSTALL_RACE", "destination was recreated during rollback")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".agent-core-restore-{uuid.uuid4().hex}"
    try:
        _copy_path(source, staging, kind)
        _move_no_replace(staging, destination)
    finally:
        if os.path.lexists(staging):
            _remove_path(staging, kind)


def _snapshot_host_records(snapshot: Path) -> list[dict[str, Any]]:
    manifest = _load_json(snapshot / "snapshot.json", "FAIL_INSTALL_SNAPSHOT")
    records = manifest.get("host_records", [])
    if not isinstance(records, list):
        raise ConfigError("FAIL_INSTALL_SNAPSHOT", "host pre-image records are invalid")
    for item in records:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "before_exists", "before_sha256", "snapshot_rel"}
            or not isinstance(item["path"], str) or not Path(item["path"]).is_absolute()
            or not isinstance(item["before_exists"], bool)
            or item["before_exists"] != (item["before_sha256"] is not None)
            or (item["before_sha256"] is not None and not _is_sha256(item["before_sha256"]))
            or not isinstance(item["snapshot_rel"], str) or Path(item["snapshot_rel"]).is_absolute()
            or ".." in Path(item["snapshot_rel"]).parts
        ):
            raise ConfigError("FAIL_INSTALL_SNAPSHOT", "host pre-image record is invalid")
    return records


def _rollback_host_records(snapshot: Path, host_root: Path) -> None:
    for record in reversed(_snapshot_host_records(snapshot)):
        path = Path(record["path"])
        try:
            _assert_within(path, host_root)
        except (ConfigError, OSError, ValueError) as exc:
            raise ConfigError("FAIL_INSTALL_RECOVERY", "host pre-image path is invalid") from exc
        if record["before_exists"]:
            content = (snapshot / record["snapshot_rel"]).read_bytes()
            if _sha256(content) != record["before_sha256"]:
                raise ConfigError("FAIL_INSTALL_ROLLBACK", "host pre-image hash differs")
            _atomic_write(path, content)
        else:
            path.unlink(missing_ok=True)
        exists, digest = _raw_file_sha256(path)
        if exists != record["before_exists"] or digest != record["before_sha256"]:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", "host pre-image restore differs")


def _pending_path(config_path: Path) -> Path:
    return config_path.resolve().parent / "install-pending.json"


def _read_pending(config_path: Path) -> Path | None:
    """Read only the host-local diagnostic marker; never trust its snapshot to write."""
    path = _pending_path(config_path)
    exists, _digest = _raw_file_sha256(path)
    if not exists:
        return None
    payload = _load_json(path, "FAIL_INSTALL_RECOVERY")
    if set(payload) != {"schema", "snapshot_id", "snapshot_sha256"} or payload.get("schema") != PENDING_SCHEMA:
        raise ConfigError("FAIL_INSTALL_RECOVERY", "pending install marker is invalid")
    snapshot_id = payload.get("snapshot_id")
    snapshot_sha256 = payload.get("snapshot_sha256")
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"install-[0-9a-f]{32}", snapshot_id) or not _is_sha256(snapshot_sha256):
        raise ConfigError("FAIL_INSTALL_RECOVERY", "pending install marker identity is invalid")
    return path


def _write_pending(config_path: Path, snapshot: Path) -> None:
    pending = _pending_path(config_path)
    if os.path.lexists(pending):
        raise ConfigError("FAIL_INSTALL_RECOVERY", "install recovery marker already exists")
    exists, digest = _raw_file_sha256(snapshot / "snapshot.json")
    if not exists or digest is None:
        raise ConfigError("FAIL_INSTALL_SNAPSHOT", "snapshot manifest is unavailable")
    _atomic_write(pending, _json_bytes({
        "schema": PENDING_SCHEMA, "snapshot_id": snapshot.name, "snapshot_sha256": digest,
    }))


def _flush_snapshot_material(path: Path, kind: str) -> None:
    if kind == "file":
        _flush_file(path)
        _flush_parent(path.parent)
        return
    for item in path.rglob("*"):
        if item.is_file():
            _flush_file(item)
    _flush_parent(path)


def _install_engine(plan: InstallPlan, snapshot: Path) -> None:
    _assert_object_preimage(plan, 0)
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
        _detach_owned(
            plan.engine_root, plan.install_root, "dir", plan.object_preimages[0], snapshot,
            "object-0",
        )
        _move_no_replace(staging, plan.engine_root)
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


def _install_control_root(config_path: Path) -> Path:
    return config_path.resolve().parent / "txn"


def _assert_adopted_hooks_current(plan: InstallPlan, hook_bindings: list[dict[str, Any]]) -> None:
    """Reprove exact unowned hook adoption immediately before receipt publication."""
    for index, item in enumerate(hook_bindings):
        if not item["adopt_identical"]:
            continue
        path = Path(item["path"])
        exists, digest = _raw_file_sha256(path)
        if (
            not exists
            or digest != plan.hook_preimages[index]
            or digest != item["installed_sha256"]
        ):
            raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed adopted hook changed before receipt")
        current = path.read_bytes()
        merged, ownership, changed = merge_owned_hooks(
            current, plan.hook_bindings[index].desired, None,
            force=False, adopt_identical=True,
        )
        if changed or merged != current or ownership != item["ownership"]:
            raise ConfigError("FAIL_INSTALL_DRIFT", "reviewed adopted hook no longer matches desired result")


def apply_install(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    force: bool,
    expected_version: str | None = None,
    confirm_private_remote: bool = False,
    plan_hash: str | None = None,
    already_locked: bool = False,
) -> list[str]:
    """Apply under the shared host lock unless an explicit internal owner holds it."""
    if already_locked:
        return _apply_install_locked(
            engine_root, config_path, explicit_state, source_root, manifest_path,
            force=force, expected_version=expected_version,
            confirm_private_remote=confirm_private_remote, plan_hash=plan_hash,
        )
    with operation_lock(_install_control_root(config_path)):
        return _apply_install_locked(
            engine_root, config_path, explicit_state, source_root, manifest_path,
            force=force, expected_version=expected_version,
            confirm_private_remote=confirm_private_remote, plan_hash=plan_hash,
        )


def _apply_install_locked(
    engine_root: Path,
    config_path: Path,
    explicit_state: Path | None,
    source_root: Path,
    manifest_path: Path | None,
    *,
    force: bool,
    expected_version: str | None,
    confirm_private_remote: bool,
    plan_hash: str | None,
) -> list[str]:
    pending = _read_pending(config_path)
    if pending is not None:
        raise ConfigError(
            "FAIL_INSTALL_RECOVERY",
            f"inspect and clear pending install marker before replanning: {pending}",
        )
    plan = _build_plan(
        engine_root, config_path, explicit_state, source_root, manifest_path,
        expected_version=expected_version,
        confirm_private_remote=confirm_private_remote,
    )
    plan, _previous, _no_changes, _hooks, _statuses, _conflicts = _reviewed_install_plan(plan)
    if plan_hash is not None and plan_hash != plan.plan_hash:
        raise ConfigError("FAIL_PLAN_HASH", "install plan hash differs from reviewed plan")
    return _apply_install_plan(plan, force=force)


def _apply_install_plan(plan: InstallPlan, *, force: bool) -> list[str]:
    if plan.artifact_manifest_path is not None:
        current_artifact = verify_release_manifest(
            plan.source_root,
            plan.artifact_manifest_path,
            expected_version=plan.artifact.version,
        )
        if current_artifact != plan.artifact:
            raise ConfigError("FAIL_ARTIFACT_HASH", "install artifact changed after install plan")
    if not plan.binding_pending:
        binding = validate_state_binding(
            plan.state_root,
            plan.config_path,
            require_clean_snapshot=False,
            require_remote_observation=False,
            expected_remote_revision=plan.binding.remote_revision,
        )
        if binding != plan.binding:
            raise ConfigError("FAIL_STATE_BINDING", "binding changed after install plan")
    _assert_pending_binding_current(plan)
    plan, previous, no_changes, hook_bindings = _assert_reviewed_plan_current(plan)
    if no_changes:
        return [f"PASS install version={plan.artifact.version} no_changes=true"]
    require_fresh(plan.state_root, "sync", plan.config_path.parent / "txn")
    _assert_pending_binding_current(plan)
    plan, previous, no_changes, hook_bindings = _assert_reviewed_plan_current(plan)
    if no_changes:
        return [f"PASS install version={plan.artifact.version} no_changes=true"]
    host_paths = ()
    if plan.binding_pending:
        host_paths = (
            plan.config_path,
            binding_receipt_path(plan.config_path),
            plan.config_path.parent / "remote-state.json",
            plan.receipt_path,
        )
    snapshot, records, binding_records = _snapshot(
        plan, previous, hook_bindings, host_paths=host_paths,
    )
    _write_pending(plan.config_path, snapshot)
    try:
        if plan.binding_pending:
            state_module.apply_attach(
                plan.state_root, plan.config_path,
                confirm_private_remote=plan.confirm_private_remote,
            )
            binding = validate_state_binding(
                plan.state_root, plan.config_path,
                require_clean_snapshot=False,
                require_remote_observation=False,
                expected_remote_revision=plan.binding.remote_revision,
            )
            if binding != plan.binding:
                raise ConfigError("FAIL_STATE_BINDING", "binding changed after install plan")
        _install_engine(plan, snapshot)
        for index, item in enumerate(plan.objects):
            if item.kind != "file" or item.label == "pin":
                continue
            _assert_object_preimage(plan, index)
            if _path_hash(item.path, item.kind) == item.installed_sha256:
                continue
            _replace_owned_bytes(
                item.path, item.root, plan.object_preimages[index], item.content or b"", snapshot,
                f"object-{index}", executable=item.label == "wrapper-posix",
            )
        for index, item in enumerate(hook_bindings):
            _assert_hook_preimage(plan, index, Path(item["path"]))
            if item["before_sha256"] == item["installed_sha256"]:
                continue
            _replace_owned_bytes(
                Path(item["path"]), Path(item["root"]), plan.hook_preimages[index], item["content"],
                snapshot, f"hook-{index}",
            )
        _verify_installed(plan, hook_bindings, include_launcher=False)
        pin = next(item for item in plan.objects if item.label == "pin")
        _assert_object_preimage(plan, plan.objects.index(pin))
        if _path_hash(pin.path, pin.kind) != pin.installed_sha256:
            pin_index = plan.objects.index(pin)
            _replace_owned_bytes(
                pin.path, pin.root, plan.object_preimages[pin_index], pin.content or b"", snapshot,
                f"object-{pin_index}",
            )
        _verify_installed(plan, hook_bindings, include_launcher=True)
        _assert_adopted_hooks_current(plan, hook_bindings)
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
        _assert_receipt_preimage(plan)
        _replace_owned_bytes(
            plan.receipt_path, plan.config_path.parent, plan.receipt_preimage_sha256,
            _json_bytes(receipt), snapshot, "install-receipt",
        )
        _pending_path(plan.config_path).unlink()
    except Exception as exc:
        try:
            _restore_hook_bindings(snapshot, binding_records)
            _restore(snapshot, records)
            if previous is None:
                plan.receipt_path.unlink(missing_ok=True)
            else:
                _atomic_write(plan.receipt_path, _json_bytes(previous))
            if plan.binding_pending:
                _rollback_host_records(snapshot, plan.config_path.parent)
            if not (isinstance(exc, ConfigError) and exc.code == "FAIL_INSTALL_RACE"):
                _pending_path(plan.config_path).unlink(missing_ok=True)
                shutil.rmtree(snapshot)
            for directory in (
                plan.install_root / "bin", plan.install_root / "engine", plan.install_root,
            ):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        except Exception as rollback_exc:
            raise ConfigError("FAIL_INSTALL_ROLLBACK", str(rollback_exc)) from rollback_exc
        if isinstance(exc, ConfigError) and exc.code == "FAIL_INSTALL_RACE":
            raise ConfigError(
                "FAIL_INSTALL_RACE", f"raced bytes preserved; detached pre-image retained at {snapshot}",
            ) from None
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
        set(snapshot_manifest) not in ({"schema", "objects", "hook_bindings"}, {"schema", "objects", "hook_bindings", "host_records"})
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
    install.add_argument("--confirm-private-remote", action="store_true")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--plan-hash")
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
            if args.plan_hash and not args.apply:
                raise ConfigError("FAIL_PLAN_HASH", "install plan hash requires --apply")
            if args.apply and not args.plan_hash:
                raise ConfigError("FAIL_PLAN_HASH", "install --apply requires a reviewed plan hash")
            lines = apply_install(
                root, args.config, args.state, args.source, args.artifact_manifest,
                force=False, confirm_private_remote=args.confirm_private_remote,
                plan_hash=args.plan_hash,
            ) if args.apply else plan_install(
                root, args.config, args.state, args.source, args.artifact_manifest,
                confirm_private_remote=args.confirm_private_remote,
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
