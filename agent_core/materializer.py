"""Lock-bound deterministic primitives for runtime materialization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import ConfigError


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MATERIALIZATION_RECEIPT_SCHEMA = "materialization-receipt/1"
MATERIALIZATION_PENDING_SCHEMA = "materialization-pending/1"


@dataclass(frozen=True)
class MaterializationTarget:
    target_id: str
    source_label: str
    root: Path
    path: Path
    content: bytes
    kind: str = "file"
    executable: bool = False


@dataclass(frozen=True)
class MaterializationReview:
    receipt_path: Path
    receipt_preimage_exists: bool
    receipt_preimage_sha256: str | None
    receipt_bytes: bytes
    receipt_changed: bool
    operations: tuple[dict[str, Any], ...]
    retained: tuple[dict[str, Any], ...]
    ready: bool
    receipt_error: str | None = None


def require_lock_token(token: object, control_root: Path) -> None:
    from .promote import _validate_lock_token

    _validate_lock_token(token, control_root)


def _is_alias(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(junction) and junction())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def materialization_receipt_path(config_path: Path) -> Path:
    return config_path.resolve().parent / "materialization-receipt.json"


def materialization_pending_path(config_path: Path) -> Path:
    return config_path.resolve().parent / "txn" / "materialization-pending.json"


def _same_path(left: Path, right: Path) -> bool:
    return str(left.resolve()).casefold() == str(right.resolve()).casefold()


def _target_roots(config: dict[str, Any]) -> dict[str, Path]:
    targets = config.get("targets")
    if not isinstance(targets, list):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "config target collection is invalid")
    result: dict[str, Path] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "config target identity is invalid")
        target_id = target.get("id")
        raw_root = target.get("root")
        if (
            not isinstance(target_id, str) or not target_id
            or not isinstance(raw_root, str) or not raw_root
            or (raw_root.startswith("<") and raw_root.endswith(">"))
            or target_id in result
        ):
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "config target identity is invalid")
        result[target_id] = Path(raw_root).expanduser().resolve()
    return result


def _parse_json_bytes(raw: bytes, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(code, "receipt is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError(code, "receipt root must be an object")
    return payload


def _validate_materialization_receipt(
    payload: dict[str, Any],
    *,
    host_config: dict[str, Any],
    config_path: Path,
    expected_config_sha256: str | None,
    state_root: Path,
    repository_root_sha: str | None,
    target_roots: dict[str, Path],
) -> dict[str, dict[str, Any]]:
    if set(payload) != {"schema", "config", "state", "generation", "transaction_id", "rows"}:
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt fields mismatch")
    if payload.get("schema") != MATERIALIZATION_RECEIPT_SCHEMA:
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt schema mismatch")
    config_record = payload.get("config")
    state = payload.get("state")
    rows = payload.get("rows")
    if (
        not isinstance(config_record, dict) or set(config_record) != {"path", "sha256"}
        or not isinstance(config_record.get("path"), str) or not Path(config_record["path"]).is_absolute()
        or not isinstance(config_record.get("sha256"), str) or _SHA256_RE.fullmatch(config_record["sha256"]) is None
        or not _same_path(Path(config_record["path"]), config_path)
        or (
            expected_config_sha256 is not None
            and config_record["sha256"] != expected_config_sha256
        )
    ):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt config identity mismatch")
    if (
        not isinstance(state, dict)
        or set(state) != {"root", "repository_root_sha", "head", "remote_revision"}
        or not isinstance(state.get("root"), str) or not Path(state["root"]).is_absolute()
        or not _same_path(Path(state["root"]), state_root)
        or state.get("repository_root_sha") != repository_root_sha
        or (
            state.get("repository_root_sha") is not None
            and (
                not isinstance(state["repository_root_sha"], str)
                or re.fullmatch(r"[0-9a-f]{40,64}", state["repository_root_sha"]) is None
            )
        )
    ):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt state identity mismatch")
    for name in ("head", "remote_revision"):
        value = state.get(name)
        if value is not None and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None
        ):
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", f"receipt {name} is invalid")
    if (
        not isinstance(payload.get("generation"), int) or payload["generation"] < 1
        or not isinstance(payload.get("transaction_id"), str)
        or _SHA256_RE.fullmatch(payload["transaction_id"]) is None
        or not isinstance(rows, list)
    ):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt generation or rows are invalid")
    result: dict[str, dict[str, Any]] = {}
    fields = {
        "target_id", "source_label", "root", "path", "kind", "installed_sha256", "status",
    }
    target_config = {
        item["id"]: item for item in host_config.get("targets", []) if isinstance(item, dict)
    }
    for row in rows:
        if not isinstance(row, dict) or frozenset(row) not in {
            frozenset(fields), frozenset({*fields, "executable"}),
        }:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt row fields mismatch")
        target_id = row.get("target_id")
        source_label = row.get("source_label")
        raw_root = row.get("root")
        raw_path = row.get("path")
        if (
            not isinstance(target_id, str) or target_id not in target_roots
            or not isinstance(source_label, str) or not source_label
            or not isinstance(raw_root, str) or not Path(raw_root).is_absolute()
            or not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
            or row.get("kind") != "file"
            or not isinstance(row.get("installed_sha256"), str)
            or _SHA256_RE.fullmatch(row["installed_sha256"]) is None
            or row.get("status") not in {"active", "retained"}
            or (
                row.get("executable") is not None
                and type(row.get("executable")) is not bool
            )
            or not _same_path(Path(raw_root), target_roots[target_id])
        ):
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt row identity mismatch")
        try:
            Path(raw_path).resolve().relative_to(Path(raw_root).resolve())
        except ValueError as exc:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt row escaped its root") from exc
        config_target = target_config.get(target_id)
        semantic_path = False
        if isinstance(config_target, dict):
            fixed_fields = {
                "rules": "rules_target", "lessons": "lessons_target",
                "case-law": "case_law_target", "hook": "hook_target",
            }
            field = fixed_fields.get(source_label)
            if field is not None and isinstance(config_target.get(field), str):
                semantic_path = _same_path(
                    Path(raw_path), target_roots[target_id] / config_target[field],
                )
            elif source_label == "hook-windows" and isinstance(config_target.get("hook_target"), str):
                semantic_path = _same_path(
                    Path(raw_path),
                    (target_roots[target_id] / config_target["hook_target"]).with_suffix(".ps1"),
                )
            elif source_label.startswith("skill:"):
                skill_name = source_label.removeprefix("skill:")
                skills_root = config_target.get("skills_root")
                if isinstance(skills_root, str) and skills_root and skill_name:
                    try:
                        Path(raw_path).resolve().relative_to(
                            (target_roots[target_id] / skills_root / skill_name).resolve()
                        )
                        semantic_path = True
                    except ValueError:
                        pass
        if not semantic_path:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt row path identity mismatch")
        key = str(Path(raw_path).resolve()).casefold()
        if key in result:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "duplicate receipt path")
        result[key] = {**row, "executable": row.get("executable")}
    return result


def _load_legacy_rows(
    path: Path,
    target_roots: dict[str, Path],
    config: dict[str, Any],
    config_sha256: str,
    state_root: Path,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    if not os.path.lexists(path):
        return {}, None
    try:
        if _is_alias(path) or not path.is_file():
            raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy receipt is not an ordinary file")
        payload = _parse_json_bytes(path.read_bytes(), "FAIL_INSTALL_RECEIPT")
        try:
            state_lock_sha256 = _sha256((state_root / "agent-core.lock.json").read_bytes())
        except OSError as exc:
            raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy state lock is unavailable") from exc
        if (
            set(payload) != {
                "schema", "engine_version", "artifact_sha256", "config_sha256",
                "state_lock_sha256", "snapshot_path", "objects", "hook_bindings",
            }
            or payload.get("schema") != "install-receipt/1"
            or not isinstance(payload.get("engine_version"), str)
            or not payload["engine_version"]
            or not isinstance(payload.get("artifact_sha256"), str)
            or not isinstance(payload.get("config_sha256"), str)
            or _SHA256_RE.fullmatch(payload["config_sha256"]) is None
            or payload["config_sha256"] != config_sha256
            or not isinstance(payload.get("state_lock_sha256"), str)
            or _SHA256_RE.fullmatch(payload["state_lock_sha256"]) is None
            or payload["state_lock_sha256"] != state_lock_sha256
            or not isinstance(payload.get("snapshot_path"), str)
            or not Path(payload["snapshot_path"]).is_absolute()
            or not isinstance(payload.get("objects"), list)
            or not isinstance(payload.get("hook_bindings"), list)
        ):
            raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy receipt fields mismatch")
        result: dict[str, dict[str, Any]] = {}
        target_config = {
            item["id"]: item for item in config.get("targets", []) if isinstance(item, dict)
        }
        object_fields = {
            "label", "path", "root", "kind", "before_exists", "before_sha256",
            "installed_sha256", "snapshot_rel",
        }
        for item in payload["objects"]:
            if not isinstance(item, dict) or set(item) != object_fields:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy object fields mismatch")
            label = item.get("label")
            raw_path = item.get("path")
            raw_root = item.get("root")
            before_sha256 = item.get("before_sha256")
            snapshot_rel = item.get("snapshot_rel")
            if (
                not isinstance(label, str) or not label
                or not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
                or not isinstance(raw_root, str) or not Path(raw_root).is_absolute()
                or item.get("kind") not in {"file", "dir"}
                or type(item.get("before_exists")) is not bool
                or item["before_exists"] != (before_sha256 is not None)
                or (
                    before_sha256 is not None
                    and (not isinstance(before_sha256, str) or _SHA256_RE.fullmatch(before_sha256) is None)
                )
                or not isinstance(item.get("installed_sha256"), str)
                or _SHA256_RE.fullmatch(item["installed_sha256"]) is None
                or not isinstance(snapshot_rel, str) or not snapshot_rel
                or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
            ):
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy object identity mismatch")
            try:
                Path(raw_path).resolve().relative_to(Path(raw_root).resolve())
            except ValueError as exc:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy object escaped its root") from exc
            if not isinstance(label, str) or not label.startswith("runtime:"):
                continue
            parts = label.split(":", 2)
            if len(parts) != 3 or parts[1] not in target_roots or not parts[2]:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy runtime identity mismatch")
            root = Path(item.get("root", ""))
            row_path = Path(item.get("path", ""))
            if (
                not root.is_absolute() or not row_path.is_absolute()
                or item.get("kind") != "file"
                or not isinstance(item.get("installed_sha256"), str)
                or _SHA256_RE.fullmatch(item["installed_sha256"]) is None
                or not _same_path(root, target_roots[parts[1]])
            ):
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy runtime identity mismatch")
            try:
                row_path.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy runtime escaped its root") from exc
            config_target = target_config.get(parts[1])
            semantic_path = False
            if isinstance(config_target, dict):
                fixed_fields = {
                    "rules": "rules_target", "lessons": "lessons_target",
                    "case-law": "case_law_target", "hook": "hook_target",
                }
                field = fixed_fields.get(parts[2])
                if field is not None and isinstance(config_target.get(field), str):
                    semantic_path = _same_path(row_path, root / config_target[field])
                elif parts[2] == "hook-windows" and isinstance(config_target.get("hook_target"), str):
                    semantic_path = _same_path(
                        row_path, (root / config_target["hook_target"]).with_suffix(".ps1"),
                    )
                elif parts[2].startswith("skill:"):
                    skill_name = parts[2].removeprefix("skill:")
                    skills_root = config_target.get("skills_root")
                    if isinstance(skills_root, str) and skills_root and skill_name:
                        try:
                            row_path.resolve().relative_to(
                                (root / skills_root / skill_name).resolve()
                            )
                            semantic_path = True
                        except ValueError:
                            pass
            if not semantic_path:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy runtime path identity mismatch")
            key = str(row_path.resolve()).casefold()
            if key in result:
                raise ConfigError("FAIL_INSTALL_RECEIPT", "duplicate legacy runtime path")
            result[key] = {
                "target_id": parts[1], "source_label": parts[2], "root": str(root.resolve()),
                "path": str(row_path.resolve()), "kind": "file",
                "installed_sha256": item["installed_sha256"], "status": "active",
                "executable": None,
            }
        hook_fields = {
            "target_id", "runtime", "path", "root", "before_exists", "before_sha256",
            "installed_sha256", "snapshot_rel", "ownership",
        }
        seen_hooks: set[str] = set()
        from .runtime_config import runtime_config_path

        for item in payload["hook_bindings"]:
            target_id = item.get("target_id") if isinstance(item, dict) else None
            expected = target_config.get(target_id)
            runtime = item.get("runtime") if isinstance(item, dict) else None
            raw_root = item.get("root") if isinstance(item, dict) else None
            raw_path = item.get("path") if isinstance(item, dict) else None
            before_sha256 = item.get("before_sha256") if isinstance(item, dict) else None
            snapshot_rel = item.get("snapshot_rel") if isinstance(item, dict) else None
            ownership = item.get("ownership") if isinstance(item, dict) else None
            groups = ownership.get("groups") if isinstance(ownership, dict) else None
            valid_ownership = (
                isinstance(ownership, dict)
                and set(ownership) == {"hooks_created", "groups"}
                and isinstance(ownership.get("hooks_created"), bool)
                and isinstance(groups, list)
                and [group.get("event") for group in groups if isinstance(group, dict)]
                == ["UserPromptSubmit", "PreToolUse", "Stop"]
                and all(isinstance(group, dict) for group in groups)
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
                    for group in groups
                )
            )
            if (
                not isinstance(item, dict) or set(item) != hook_fields
                or not isinstance(target_id, str) or target_id in seen_hooks
                or not isinstance(expected, dict)
                or runtime not in {"claude-code", "codex"}
                or expected.get("runtime") != runtime
                or not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
                or not isinstance(raw_root, str) or not Path(raw_root).is_absolute()
                or not _same_path(Path(raw_root), target_roots[target_id])
                or not _same_path(Path(raw_path), runtime_config_path(runtime, target_roots[target_id]))
                or type(item.get("before_exists")) is not bool
                or item["before_exists"] != (before_sha256 is not None)
                or (
                    before_sha256 is not None
                    and (not isinstance(before_sha256, str) or _SHA256_RE.fullmatch(before_sha256) is None)
                )
                or not isinstance(item.get("installed_sha256"), str)
                or _SHA256_RE.fullmatch(item["installed_sha256"]) is None
                or not isinstance(snapshot_rel, str) or not snapshot_rel
                or Path(snapshot_rel).is_absolute() or ".." in Path(snapshot_rel).parts
                or not valid_ownership
            ):
                raise ConfigError("FAIL_INSTALL_RECEIPT", "legacy hook binding is invalid")
            seen_hooks.add(target_id)
        return result, None
    except (ConfigError, OSError, ValueError) as exc:
        return {}, str(exc)


def review_materialization(
    config_path: Path,
    config: dict[str, Any],
    state_root: Path,
    targets: Iterable[MaterializationTarget],
    *,
    repository_root_sha: str | None,
    head: str | None,
    remote_revision: str | None,
    config_bytes: bytes | None = None,
) -> MaterializationReview:
    """Classify every runtime path and freeze the next ownership receipt."""
    config_path = config_path.resolve()
    state_root = state_root.resolve()
    target_list = list(targets)
    receipt_path = materialization_receipt_path(config_path)
    target_roots = _target_roots(config)
    if config_bytes is None:
        try:
            raw_config = config_path.read_bytes()
        except OSError as exc:
            raise ConfigError("FAIL_CONFIG", f"cannot read {config_path}") from exc
    else:
        raw_config = config_bytes
    receipt_exists = os.path.lexists(receipt_path)
    receipt_sha256: str | None = None
    previous: dict[str, Any] | None = None
    previous_rows: dict[str, dict[str, Any]] = {}
    receipt_error: str | None = None
    previous_raw: bytes | None = None
    if receipt_exists:
        try:
            if _is_alias(receipt_path) or not receipt_path.is_file():
                raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt is not an ordinary file")
            previous_raw = receipt_path.read_bytes()
            receipt_sha256 = _sha256(previous_raw)
            previous = _parse_json_bytes(previous_raw, "FAIL_MATERIALIZATION_RECEIPT")
            previous_rows = _validate_materialization_receipt(
                previous, host_config=config, config_path=config_path,
                expected_config_sha256=None, state_root=state_root,
                repository_root_sha=repository_root_sha, target_roots=target_roots,
            )
        except (ConfigError, OSError, ValueError) as exc:
            receipt_error = str(exc)
            previous = None
            previous_rows = {}
    legacy_rows: dict[str, dict[str, Any]] = {}
    legacy_error: str | None = None
    if not receipt_exists:
        legacy_rows, legacy_error = _load_legacy_rows(
            config_path.parent / "install-receipt.json", target_roots, config,
            _sha256(raw_config), state_root,
        )

    if previous is not None:
        active_identities: dict[tuple[str, str], set[str]] = {}
        for target in target_list:
            active_identities.setdefault((target.target_id, target.source_label), set()).add(
                str(target.path.resolve()).casefold()
            )
        target_config = {
            item["id"]: item for item in config.get("targets", []) if isinstance(item, dict)
        }
        for row in previous_rows.values():
            identity = (row["target_id"], row["source_label"])
            active_paths = active_identities.get(identity)
            row_path = Path(row["path"]).resolve()
            if active_paths is None or str(row_path).casefold() in active_paths:
                continue
            within_skill = False
            if row["source_label"].startswith("skill:"):
                skill_name = row["source_label"].removeprefix("skill:")
                skills_root = target_config.get(row["target_id"], {}).get("skills_root")
                if isinstance(skills_root, str) and skills_root and skill_name:
                    expected_root = target_roots[row["target_id"]] / skills_root / skill_name
                    try:
                        row_path.relative_to(expected_root.resolve())
                        within_skill = True
                    except ValueError:
                        pass
            if not within_skill:
                receipt_error = "FAIL_MATERIALIZATION_RECEIPT receipt row path identity mismatch"
                previous = None
                previous_rows = {}
                break

    planned: list[dict[str, Any]] = []
    active_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in target_list:
        root = target.root.resolve()
        path = target.path.resolve()
        if target.kind != "file" or target.target_id not in target_roots or not _same_path(
            root, target_roots[target.target_id],
        ):
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "planned runtime identity mismatch")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ConfigError("FAIL_PATH_ESCAPE", f"{path} escaped {root}") from exc
        key = str(path).casefold()
        if key in seen:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "duplicate planned runtime path")
        seen.add(key)
        desired_sha256 = _sha256(target.content)
        try:
            current_exists, current_sha256, current_executable = file_mode_preimage(path)
        except ConfigError:
            current_exists, current_sha256, current_executable = True, None, None
        row = previous_rows.get(key)
        legacy = legacy_rows.get(key)
        if receipt_error is not None:
            status = "indeterminate"
        elif row is not None and (
            row["target_id"] != target.target_id
            or row["source_label"] != target.source_label
            or not _same_path(Path(row["root"]), root)
        ):
            status = "indeterminate"
        elif row is not None and current_sha256 == row["installed_sha256"] == desired_sha256:
            status = (
                "mode-drift"
                if os.name != "nt" and current_executable != target.executable
                else "receipt-owned-identical"
            )
        elif row is not None and current_sha256 == row["installed_sha256"] != desired_sha256:
            status = "managed-update"
        elif row is not None:
            status = "foreign"
        elif not receipt_exists and legacy_error is not None:
            status = "indeterminate"
        elif not receipt_exists and legacy is not None and (
            legacy["target_id"] == target.target_id
            and legacy["source_label"] == target.source_label
            and current_sha256 == legacy["installed_sha256"]
        ):
            status = (
                "mode-drift"
                if current_sha256 == desired_sha256
                and os.name != "nt" and current_executable != target.executable
                else "bootstrap-identical" if current_sha256 == desired_sha256
                else "bootstrap-managed-update"
            )
        elif not current_exists:
            status = "absent"
        elif current_sha256 == desired_sha256:
            status = (
                "foreign"
                if os.name != "nt" and current_executable != target.executable
                else "adopt-identical"
            )
        else:
            status = "foreign"
        action = "WRITE" if status in {"absent", "managed-update", "bootstrap-managed-update"} else (
            "MODE" if status == "mode-drift" else
            "NOOP" if status in {"receipt-owned-identical", "bootstrap-identical", "adopt-identical"}
            else "BLOCK"
        )
        planned.append({
            "target_id": target.target_id, "source_label": target.source_label,
            "root": str(root), "path": str(path), "kind": "file",
            "before_exists": current_exists, "before_sha256": current_sha256,
            "before_executable": current_executable,
            "after_sha256": desired_sha256, "executable": target.executable,
            "status": status, "action": action,
        })
        active_rows.append({
            "target_id": target.target_id, "source_label": target.source_label,
            "root": str(root), "path": str(path), "kind": "file",
            "installed_sha256": desired_sha256, "executable": target.executable,
            "status": "active",
        })

    retained: list[dict[str, Any]] = []
    if previous is not None:
        for key, row in previous_rows.items():
            if key in seen:
                continue
            try:
                exists, current, current_executable = file_mode_preimage(Path(row["path"]))
            except ConfigError:
                exists, current, current_executable = True, None, None
            retained_status = (
                "retained"
                if exists and current == row["installed_sha256"] and (
                    os.name == "nt" or row.get("executable") is None
                    or current_executable == row["executable"]
                )
                else "retained-drift"
            )
            retained.append({**row, "status": retained_status})

    next_rows = sorted(
        active_rows + [{**row, "status": "retained"} for row in retained],
        key=lambda row: (str(row["path"]).casefold(), row["target_id"], row["source_label"]),
    )
    state = {
        "root": str(state_root), "repository_root_sha": repository_root_sha,
        "head": head, "remote_revision": remote_revision,
    }
    semantic = {
        "schema": MATERIALIZATION_RECEIPT_SCHEMA,
        "config": {"path": str(config_path), "sha256": _sha256(raw_config)},
        "state": state, "rows": next_rows,
    }
    previous_semantic = None if previous is None else {
        key: previous[key] for key in ("schema", "config", "state", "rows")
    }
    if previous_raw is not None and previous_semantic == semantic:
        receipt_bytes = previous_raw
        receipt_changed = False
    else:
        generation = (previous["generation"] + 1) if previous is not None else 1
        transaction_id = _sha256(_canonical_bytes({
            **semantic, "generation": generation,
            "previous_sha256": receipt_sha256,
        }))
        receipt_bytes = _json_bytes({
            **semantic, "generation": generation, "transaction_id": transaction_id,
        })
        receipt_changed = True
    ready = receipt_error is None and legacy_error is None and all(
        item["action"] != "BLOCK" for item in planned
    ) and all(
        item["status"] == "retained" for item in retained
    )
    return MaterializationReview(
        receipt_path, receipt_exists, receipt_sha256, receipt_bytes, receipt_changed,
        tuple(planned), tuple(retained), ready, receipt_error or legacy_error,
    )


def publish_materialization_receipt(
    review: MaterializationReview,
    transaction_root: Path,
    *,
    control_root: Path,
    lock_token: Any,
) -> bool:
    """Publish the reviewed authority record under the caller's held lock."""
    return materialize_bytes(
        review.receipt_path, review.receipt_path.parent,
        review.receipt_preimage_sha256 if review.receipt_preimage_exists else None,
        review.receipt_bytes, transaction_root, "materialization-receipt",
        control_root=control_root, lock_token=lock_token,
    )


def _current_repository_root_sha(state_root: Path) -> str | None:
    from .freshness import is_repository
    from .repository import resolve_repository_context

    resolved = state_root.resolve()
    if not is_repository(resolved):
        return None
    context = resolve_repository_context(resolved)
    if context.layout != "canonical":
        return None
    result = subprocess.run(
        [
            "git", "-c", f"safe.directory={context.repo_root.resolve().as_posix()}",
            "-C", str(context.repo_root), "rev-list", "--max-parents=0", "HEAD",
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    roots = [line for line in result.stdout.splitlines() if line]
    if result.returncode != 0 or len(roots) != 1 or re.fullmatch(r"[0-9a-f]{40,64}", roots[0]) is None:
        raise ConfigError("FAIL_STATE_BINDING", "repository lineage is unavailable")
    return roots[0]


def doctor_materialization_receipt(
    config_path: Path,
    config: dict[str, Any],
    state_root: Path,
    targets: Iterable[MaterializationTarget],
) -> list[str]:
    """Validate current active and retained runtime ownership without writes."""
    planned = list(targets)
    install_pending = config_path.resolve().parent / "install-pending.json"
    if os.path.lexists(install_pending):
        raise ConfigError(
            "FAIL_INSTALL_RECOVERY",
            f"pending install transaction requires inspection: {install_pending}",
        )
    pending = materialization_pending_path(config_path)
    if os.path.lexists(pending):
        raise ConfigError(
            "FAIL_MATERIALIZATION_RECOVERY",
            f"pending materialization transaction requires inspection: {pending}",
        )
    if not planned:
        return []
    receipt_path = materialization_receipt_path(config_path)
    if not os.path.lexists(receipt_path):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", f"missing {receipt_path}")
    if _is_alias(receipt_path) or not receipt_path.is_file():
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "receipt is not an ordinary file")
    raw = receipt_path.read_bytes()
    payload = _parse_json_bytes(raw, "FAIL_MATERIALIZATION_RECEIPT")
    try:
        config_sha256 = _sha256(config_path.resolve().read_bytes())
    except OSError as exc:
        raise ConfigError("FAIL_CONFIG", f"cannot read {config_path.resolve()}") from exc
    repository_root_sha = _current_repository_root_sha(state_root)
    rows = _validate_materialization_receipt(
        payload, host_config=config, config_path=config_path.resolve(),
        expected_config_sha256=config_sha256,
        state_root=state_root.resolve(),
        repository_root_sha=repository_root_sha, target_roots=_target_roots(config),
    )
    expected: dict[str, MaterializationTarget] = {
        str(item.path.resolve()).casefold(): item for item in planned
    }
    if len(expected) != len(planned):
        raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", "duplicate expected runtime path")
    lines: list[str] = []
    for key, target in expected.items():
        row = rows.get(key)
        desired = _sha256(target.content)
        if (
            row is None or row["status"] != "active"
            or row["target_id"] != target.target_id
            or row["source_label"] != target.source_label
            or not _same_path(Path(row["root"]), target.root)
            or row["installed_sha256"] != desired
            or row.get("executable") != target.executable
        ):
            raise ConfigError(
                "FAIL_MATERIALIZATION_RECEIPT", f"missing or stale active row: {target.path}",
            )
        exists, current, current_executable = file_mode_preimage(target.path)
        if not exists or current != desired or (
            os.name != "nt" and current_executable != target.executable
        ):
            raise ConfigError("FAIL_MATERIALIZATION_DRIFT", str(target.path))
    for key, row in rows.items():
        if row["status"] == "active" and key not in expected:
            raise ConfigError("FAIL_MATERIALIZATION_RECEIPT", f"unexpected active row: {row['path']}")
        if row["status"] != "retained":
            continue
        exists, current, current_executable = file_mode_preimage(Path(row["path"]))
        if not exists or current != row["installed_sha256"] or (
            os.name != "nt" and row.get("executable") is not None
            and current_executable != row["executable"]
        ):
            raise ConfigError("FAIL_MATERIALIZATION_DRIFT", f"retained path drift: {row['path']}")
        lines.append(f"RETAIN materialization_receipt path={row['path']}")
    return [
        f"PASS materialization_receipt schema={MATERIALIZATION_RECEIPT_SCHEMA} "
        f"generation={payload['generation']} rows={len(rows)} sha256={_sha256(raw)}",
        *lines,
    ]


def file_preimage(path: Path) -> tuple[bool, str | None]:
    """Read one ordinary-file fact without treating aliases as files."""
    if not os.path.lexists(path):
        return False, None
    if _is_alias(path) or not path.is_file():
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", f"not an ordinary file: {path}")
    try:
        return True, _sha256(path.read_bytes())
    except OSError as exc:
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", f"cannot read {path}") from exc


def file_mode_preimage(path: Path) -> tuple[bool, str | None, bool | None]:
    exists, digest = file_preimage(path)
    if not exists or os.name == "nt":
        return exists, digest, None
    try:
        executable = bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    except OSError as exc:
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", f"cannot stat {path}") from exc
    return exists, digest, executable


def _assert_within(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    try:
        path.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise ConfigError("FAIL_PATH_ESCAPE", f"{path} escaped {root}") from exc
    if _is_alias(path):
        raise ConfigError("FAIL_PATH_ESCAPE", f"materialized target is an alias: {path}")


def _flush_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_no_replace(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise ConfigError("FAIL_MATERIALIZER_RACE", "destination was recreated")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.rename(source, destination)
        else:
            os.link(source, destination)
            source.unlink()
    except (FileExistsError, OSError) as exc:
        raise ConfigError("FAIL_MATERIALIZER_RACE", "no-replace placement failed") from exc


def _stage_bytes(path: Path, content: bytes, *, executable: bool) -> None:
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


def materialize_bytes(
    path: Path,
    root: Path,
    expected_sha256: str | None,
    desired: bytes,
    transaction_root: Path,
    key: str,
    *,
    control_root: Path,
    lock_token: Any,
    expected_executable: bool | None = None,
    executable: bool | None = None,
) -> bool:
    """Place reviewed bytes with no replacement of an unreviewed pre-image."""
    require_lock_token(lock_token, control_root)
    if expected_sha256 is not None and _SHA256_RE.fullmatch(expected_sha256) is None:
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", "invalid expected sha256")
    relative_key = Path(key)
    if relative_key.is_absolute() or not relative_key.parts or ".." in relative_key.parts:
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", "invalid transaction key")
    _assert_within(path, root)
    exists, current, current_executable = file_mode_preimage(path)
    if current != expected_sha256 or exists != (expected_sha256 is not None):
        raise ConfigError("FAIL_MATERIALIZER_DRIFT", str(path))
    if (
        executable is not None and os.name != "nt"
        and current_executable != expected_executable
    ):
        raise ConfigError("FAIL_MATERIALIZER_DRIFT", f"mode changed: {path}")
    desired_sha256 = _sha256(desired)
    if current == desired_sha256:
        if executable is None or os.name == "nt" or current_executable == executable:
            return False
        try:
            current_mode = path.stat().st_mode
            execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            path.chmod(current_mode | execute_bits if executable else current_mode & ~execute_bits)
        except OSError as exc:
            raise ConfigError("FAIL_MATERIALIZER_RACE", f"cannot change mode: {path}") from exc
        placed_exists, placed_sha256, placed_executable = file_mode_preimage(path)
        if not placed_exists or placed_sha256 != desired_sha256 or placed_executable != executable:
            raise ConfigError("FAIL_MATERIALIZER_RACE", "placed mode changed")
        return True

    transaction_root.mkdir(parents=True, exist_ok=True)
    staged = transaction_root / "staged" / relative_key
    detached = transaction_root / "detached" / relative_key
    _stage_bytes(staged, desired, executable=bool(executable))
    if expected_sha256 is not None:
        _move_no_replace(path, detached)
        detached_exists, detached_sha256 = file_preimage(detached)
        if not detached_exists or detached_sha256 != expected_sha256:
            try:
                _move_no_replace(detached, path)
            except ConfigError:
                pass
            raise ConfigError("FAIL_MATERIALIZER_RACE", "detached pre-image changed")
    _move_no_replace(staged, path)
    placed_exists, placed_sha256, placed_executable = file_mode_preimage(path)
    if not placed_exists or placed_sha256 != desired_sha256 or (
        executable is not None and os.name != "nt" and placed_executable != executable
    ):
        raise ConfigError("FAIL_MATERIALIZER_RACE", "placed bytes changed")
    return True


def restore_file_preimage(
    path: Path,
    root: Path,
    expected_current_sha256: str | None,
    before: bytes | None,
    transaction_root: Path,
    key: str,
    *,
    control_root: Path,
    lock_token: Any,
    expected_executable: bool | None = None,
    before_executable: bool | None = None,
) -> None:
    """Restore one snapshotted ordinary-file fact without overwriting raced bytes."""
    require_lock_token(lock_token, control_root)
    exists, current, current_executable = file_mode_preimage(path)
    if current != expected_current_sha256 or exists != (expected_current_sha256 is not None) or (
        before_executable is not None and os.name != "nt"
        and current_executable != expected_executable
    ):
        raise ConfigError("FAIL_MATERIALIZER_RACE", f"rollback target changed: {path}")
    if before is not None:
        materialize_bytes(
            path, root, expected_current_sha256, before, transaction_root, key,
            control_root=control_root, lock_token=lock_token,
            expected_executable=expected_executable,
            executable=before_executable,
        )
        return
    if expected_current_sha256 is None:
        return
    relative_key = Path(key)
    if relative_key.is_absolute() or not relative_key.parts or ".." in relative_key.parts:
        raise ConfigError("FAIL_MATERIALIZER_PREIMAGE", "invalid transaction key")
    _assert_within(path, root)
    detached = transaction_root / "rollback-detached" / relative_key
    _move_no_replace(path, detached)
    if os.path.lexists(path):
        raise ConfigError("FAIL_MATERIALIZER_RACE", f"rollback removal failed: {path}")
