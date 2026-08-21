"""Strict configuration and capability-manifest composition."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_SCHEMA = "agent-core.config/1"
MANIFEST_SCHEMA = "capability-manifest/1"
HOST_LABEL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUNTIMES = {"claude-code", "codex", "generic"}
CAPABILITY_STATES = {"active", "disabled"}
REQUIREMENTS = {"required", "optional"}


class ConfigError(ValueError):
    """Fail-closed configuration error with a stable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code} {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Composition:
    capabilities: tuple[dict[str, Any], ...]
    composition_hash: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_CONFIG", f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("FAIL_CONFIG", f"root must be an object: {path}")
    return payload


def _exact_keys(payload: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ConfigError("FAIL_CONFIG", f"unknown {context} fields: {','.join(unknown)}")


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("FAIL_CONFIG", f"{field} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError("FAIL_PATH", f"{field} must stay below its declared root")
    return path.as_posix()


def load_config(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    _exact_keys(
        payload,
        {"schema", "host_label", "state_root", "backup_root", "prompt_injection", "targets", "capability_overrides"},
        "config",
    )
    if payload.get("schema") != CONFIG_SCHEMA:
        raise ConfigError("FAIL_CONFIG", f"schema must be {CONFIG_SCHEMA}")
    label = payload.get("host_label")
    if not isinstance(label, str) or not HOST_LABEL_RE.fullmatch(label):
        raise ConfigError("FAIL_CONFIG", "host_label must be a privacy-safe kebab-case label")
    for field in ("state_root", "backup_root"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ConfigError("FAIL_CONFIG", f"{field} must be a non-empty string")
    injection = payload.get("prompt_injection")
    if not isinstance(injection, dict):
        raise ConfigError("FAIL_CONFIG", "prompt_injection must be an object")
    _exact_keys(injection, {"lines"}, "prompt_injection")
    lines = injection.get("lines")
    if not isinstance(lines, list) or not lines or any(not isinstance(line, str) or not line for line in lines):
        raise ConfigError("FAIL_CONFIG", "prompt_injection.lines must be a non-empty string list")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ConfigError("FAIL_CONFIG", "targets must be a non-empty list")
    target_ids: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ConfigError("FAIL_CONFIG", "each target must be an object")
        _exact_keys(
            target,
            {"id", "runtime", "root", "rules_target", "lessons_target", "case_law_target", "skills_root", "hook_target"},
            "target",
        )
        target_id = target.get("id")
        if not isinstance(target_id, str) or not HOST_LABEL_RE.fullmatch(target_id) or target_id in target_ids:
            raise ConfigError("FAIL_CONFIG", f"invalid or duplicate target id: {target_id!r}")
        target_ids.add(target_id)
        if target.get("runtime") not in RUNTIMES:
            raise ConfigError("FAIL_CONFIG", f"unsupported runtime for {target_id}")
        if not isinstance(target.get("root"), str) or not target["root"]:
            raise ConfigError("FAIL_CONFIG", f"target root missing for {target_id}")
        for field in ("rules_target", "lessons_target", "case_law_target", "skills_root", "hook_target"):
            target[field] = _relative_path(target.get(field), f"{target_id}.{field}")
    overrides = payload.get("capability_overrides")
    if not isinstance(overrides, list):
        raise ConfigError("FAIL_CONFIG", "capability_overrides must be a list")
    seen_overrides: set[str] = set()
    for override in overrides:
        if not isinstance(override, dict):
            raise ConfigError("FAIL_CONFIG", "each capability override must be an object")
        _exact_keys(override, {"id", "state"}, "capability override")
        capability_id = override.get("id")
        if not isinstance(capability_id, str) or not capability_id or capability_id in seen_overrides:
            raise ConfigError("FAIL_CONFIG", f"invalid or duplicate capability override: {capability_id!r}")
        seen_overrides.add(capability_id)
        if override.get("state") not in CAPABILITY_STATES:
            raise ConfigError("FAIL_CONFIG", f"invalid capability state for {capability_id}")
    return payload


def default_config_path(engine_root: Path) -> Path:
    configured = os.environ.get("AGENT_CORE_HOST_CONFIG")
    if configured:
        return Path(configured)
    host_config = Path.home() / ".agent-core" / "host.json"
    if host_config.is_file():
        return host_config
    return engine_root / "examples" / "host.example.json"


def _validate_capability(raw: Any, origin: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("FAIL_MANIFEST", f"{origin} capability must be an object")
    _exact_keys(raw, {"id", "kind", "source", "requirement", "runtimes", "trusted"}, "capability")
    capability_id = raw.get("id")
    kind = raw.get("kind")
    requirement = raw.get("requirement")
    runtimes = raw.get("runtimes")
    if not isinstance(capability_id, str) or not capability_id:
        raise ConfigError("FAIL_MANIFEST", f"{origin} capability id is invalid")
    if kind not in {"rule", "skill", "hook", "ledger", "checker"}:
        raise ConfigError("FAIL_MANIFEST", f"invalid kind for {capability_id}")
    source = _relative_path(raw.get("source"), f"{capability_id}.source")
    if requirement not in REQUIREMENTS:
        raise ConfigError("FAIL_MANIFEST", f"invalid requirement for {capability_id}")
    if not isinstance(runtimes, list) or not runtimes or any(runtime not in RUNTIMES for runtime in runtimes):
        raise ConfigError("FAIL_MANIFEST", f"invalid runtimes for {capability_id}")
    if not isinstance(raw.get("trusted"), bool):
        raise ConfigError("FAIL_MANIFEST", f"trusted must be boolean for {capability_id}")
    return {
        "id": capability_id,
        "kind": kind,
        "source": source,
        "requirement": requirement,
        "runtimes": sorted(set(runtimes)),
        "trusted": raw["trusted"],
        "origin": origin,
        "state": "active",
    }


def load_manifest(path: Path, origin: str) -> list[dict[str, Any]]:
    payload = _load_json(path)
    _exact_keys(payload, {"schema", "capabilities"}, "manifest")
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise ConfigError("FAIL_MANIFEST", f"schema must be {MANIFEST_SCHEMA}: {path}")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list):
        raise ConfigError("FAIL_MANIFEST", f"capabilities must be a list: {path}")
    return [_validate_capability(raw, origin) for raw in capabilities]


def compose_manifests(
    engine_manifest: Path,
    state_manifest: Path | None = None,
    config: dict[str, Any] | None = None,
) -> Composition:
    capabilities = load_manifest(engine_manifest, "engine")
    if state_manifest is not None:
        capabilities.extend(load_manifest(state_manifest, "state"))
    by_id: dict[str, dict[str, Any]] = {}
    by_path: dict[str, str] = {}
    for capability in capabilities:
        capability_id = capability["id"]
        path_key = capability["source"].casefold()
        if capability_id in by_id:
            raise ConfigError("FAIL_MANIFEST_CONFLICT", f"duplicate capability id: {capability_id}")
        if path_key in by_path:
            raise ConfigError(
                "FAIL_MANIFEST_CONFLICT",
                f"source path {capability['source']} belongs to both {by_path[path_key]} and {capability_id}",
            )
        by_id[capability_id] = capability
        by_path[path_key] = capability_id
    for override in (config or {}).get("capability_overrides", []):
        capability = by_id.get(override["id"])
        if capability is None:
            raise ConfigError("FAIL_MANIFEST_CONFLICT", f"unknown capability override: {override['id']}")
        if override["state"] == "disabled" and capability["requirement"] == "required":
            raise ConfigError("FAIL_REQUIRED_DISABLED", capability["id"])
        capability["state"] = override["state"]
    ordered = tuple(sorted(by_id.values(), key=lambda item: item["id"]))
    canonical = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Composition(ordered, hashlib.sha256(canonical).hexdigest())


def assert_capability_sources(composition: Composition, engine_root: Path, state_root: Path | None) -> None:
    for capability in composition.capabilities:
        if capability["state"] != "active":
            continue
        root = engine_root if capability["origin"] == "engine" else state_root
        if root is None or not (root / capability["source"]).exists():
            code = "FAIL_REQUIRED_CAPABILITY" if capability["requirement"] == "required" else "FAIL_OPTIONAL_CAPABILITY"
            raise ConfigError(code, capability["id"])
