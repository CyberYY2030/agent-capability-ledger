"""Capability fingerprints and deterministic parity decisions."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .config import Composition, compose_manifests, load_config


FINGERPRINT_SCHEMA = "capability-fingerprint/1"


def _normalized_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    files = [path] if path.is_file() else sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.as_posix())
    for item in files:
        relative = item.name if path.is_file() else item.relative_to(path).as_posix()
        data = item.read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def _state_revision(state_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={state_root}", "-C", str(state_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    digest = hashlib.sha256()
    for relative in ("manifest.yaml", "experience/LESSONS.md", "experience/CASE_LAW.md", "rules/global.md"):
        path = state_root / relative
        if path.is_file():
            digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes().replace(b"\r\n", b"\n"))
    return "content:" + digest.hexdigest()


def _effective_path(target: dict[str, Any], capability: dict[str, Any], engine_root: Path) -> Path:
    root = Path(target["root"]).expanduser().resolve()
    kind = capability["kind"]
    if kind == "skill":
        return root / target["skills_root"] / Path(capability["source"]).name
    if kind == "rule":
        return root / target["rules_target"]
    if kind == "ledger":
        return root / target["lessons_target"]
    if kind == "hook":
        return root / target["hook_target"]
    return engine_root / capability["source"]


def generate(engine_root: Path, config_path: Path, state_root: Path, state_manifest: Path | None) -> dict[str, Any]:
    config = load_config(config_path)
    composition = compose_manifests(engine_root / "manifest.yaml", state_manifest, config)
    entries: list[dict[str, Any]] = []
    for target in config["targets"]:
        if target["root"].startswith("<"):
            raise ValueError(f"FAIL_TARGET_UNBOUND {target['id']}")
        for capability in composition.capabilities:
            source_root = engine_root if capability["origin"] == "engine" else state_root
            source_hash = _normalized_hash(source_root / capability["source"])
            if target["runtime"] not in capability["runtimes"]:
                state = "unsupported"
                effective_hash = None
            elif capability["state"] == "disabled":
                state = "disabled"
                effective_hash = None
            else:
                effective_hash = _normalized_hash(_effective_path(target, capability, engine_root))
                state = "active" if source_hash is not None and effective_hash is not None else "absent"
            entries.append({
                "id": capability["id"],
                "kind": capability["kind"],
                "requirement": capability["requirement"],
                "runtime": target["runtime"],
                "target": target["id"],
                "state": state,
                "source_hash": source_hash,
                "effective_hash": effective_hash,
            })
    return {
        "schema": FINGERPRINT_SCHEMA,
        "host": config["host_label"],
        "engine_version": __version__,
        "schema_version": "lessons-ledger/2",
        "state_revision": _state_revision(state_root),
        "capabilities": sorted(entries, key=lambda item: (item["target"], item["id"])),
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def parity(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, list[str]]:
    if left.get("schema") != FINGERPRINT_SCHEMA or right.get("schema") != FINGERPRINT_SCHEMA:
        return "FAIL", ["schema mismatch"]
    reasons: list[str] = []
    if left.get("engine_version") != right.get("engine_version") or left.get("schema_version") != right.get("schema_version"):
        reasons.append("version mismatch")
    left_items = {(item["target"], item["id"]): item for item in left.get("capabilities", [])}
    right_items = {(item["target"], item["id"]): item for item in right.get("capabilities", [])}
    optional_differences: list[str] = []
    degraded: list[str] = []
    for key in sorted(set(left_items) | set(right_items)):
        a = left_items.get(key)
        b = right_items.get(key)
        if a is None or b is None:
            reasons.append(f"missing capability {key}")
            continue
        required = a.get("requirement") == "required" or b.get("requirement") == "required"
        if required and (a["state"] in {"disabled", "unsupported"} or b["state"] in {"disabled", "unsupported"}):
            degraded.append(f"required degraded {key}")
        elif required and (a["state"] != "active" or b["state"] != "active"):
            reasons.append(f"required absent {key}")
        elif required and (a["source_hash"], a["effective_hash"]) != (b["source_hash"], b["effective_hash"]):
            reasons.append(f"required hash mismatch {key}")
        elif not required and (a["state"], a["source_hash"], a["effective_hash"]) != (b["state"], b["source_hash"], b["effective_hash"]):
            optional_differences.append(f"optional difference {key}")
    if reasons:
        return "FAIL", reasons + degraded + optional_differences
    if degraded:
        return "DEGRADED", degraded + optional_differences
    if optional_differences:
        return "PASS_WITH_EXCEPTIONS", optional_differences
    return "PASS", []
