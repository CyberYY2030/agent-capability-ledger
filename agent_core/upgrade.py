"""Plan-bound local engine upgrade using verified release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import ConfigError, load_config
from .freshness import inspect
from .installer import apply_install, plan_install, verify_release_manifest
from .promote import operation_lock
from .state import _atomic_write, binding_receipt_path, validate_state_binding


VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


@dataclass(frozen=True)
class UpgradePlan:
    state_root: Path
    config_path: Path
    source_root: Path
    manifest_path: Path
    target_version: str
    artifact_sha256: str
    remote_sha: str
    binding: dict[str, Any]
    binding_bytes: bytes
    refreshed_binding: bytes
    plan_hash: str
    install_lines: tuple[str, ...]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _load_object(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(code, f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(code, f"root must be an object: {path}")
    return payload, content


def _source_version(source_root: Path) -> str:
    try:
        text = (source_root / "agent_core" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("FAIL_ARTIFACT_VERSION", str(exc)) from exc
    match = VERSION_RE.search(text)
    if match is None:
        raise ConfigError("FAIL_ARTIFACT_VERSION", "source __version__ is unavailable")
    return match.group(1)


def _binding_plan(
    state_root: Path, config_path: Path, *, expected_state_lock_sha256: str,
) -> tuple[dict[str, Any], bytes, bytes, str]:
    config_path = config_path.resolve()
    state_root = state_root.resolve()
    evidence = validate_state_binding(
        state_root, config_path, require_clean_snapshot=True,
        expected_state_lock_sha256=expected_state_lock_sha256,
    )
    if evidence.state_root != state_root:
        raise ConfigError("FAIL_STATE_BINDING", "config state_root differs")
    receipt_path = binding_receipt_path(config_path)
    receipt, receipt_bytes = _load_object(receipt_path, "FAIL_STATE_BINDING")
    lock_bytes = (state_root / "agent-core.lock.json").read_bytes()
    refreshed = dict(receipt)
    refreshed["remote_revision"] = evidence.remote_revision
    refreshed["state_lock_sha256"] = evidence.state_lock_sha256
    return receipt, receipt_bytes, _json_bytes(refreshed), evidence.remote_revision


def plan_upgrade(
    engine_root: Path, state_root: Path, config_path: Path, source_root: Path,
    manifest_path: Path | None, target_version: str,
) -> UpgradePlan:
    state_root = state_root.resolve()
    config_path = config_path.resolve()
    source_root = source_root.resolve()
    manifest_path = (manifest_path or source_root / "release-manifest.json").resolve()
    artifact = verify_release_manifest(
        source_root, manifest_path, expected_version=target_version,
    )
    if _source_version(source_root) != target_version:
        raise ConfigError("FAIL_ARTIFACT_VERSION", "source and manifest versions differ")
    lock, lock_bytes = _load_object(state_root / "agent-core.lock.json", "FAIL_STATE_LOCK")
    if lock.get("engine_version") != target_version or lock.get("schema_version") != "lessons-ledger/2":
        raise ConfigError(
            "FAIL_ENGINE_VERSION",
            f"state={lock.get('engine_version')} target={target_version}",
        )
    binding, binding_bytes, refreshed, remote_sha = _binding_plan(
        state_root, config_path, expected_state_lock_sha256=_sha256(lock_bytes),
    )

    # Installer planning consumes the refreshed receipt in memory. The public plan remains zero-write.
    current_lock_hash = _sha256(lock_bytes)
    install_lines = tuple(plan_install(
        engine_root, config_path, state_root, source_root, manifest_path,
        expected_version=target_version,
        binding_receipt_override=refreshed,
    ))
    payload = {
        "state": str(state_root),
        "config_sha256": _sha256(config_path.read_bytes()),
        "binding_before_sha256": _sha256(binding_bytes),
        "binding_after_sha256": _sha256(refreshed),
        "lock_sha256": current_lock_hash,
        "remote_sha": remote_sha,
        "source": str(source_root),
        "manifest_sha256": _sha256(manifest_path.read_bytes()),
        "target_version": target_version,
        "artifact_sha256": artifact.artifact_sha256,
    }
    plan_hash = _sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    return UpgradePlan(
        state_root, config_path, source_root, manifest_path, target_version,
        artifact.artifact_sha256, remote_sha, binding, binding_bytes, refreshed,
        plan_hash, install_lines,
    )


def render_plan(plan: UpgradePlan) -> list[str]:
    return [
        f"PLAN operation=engine-upgrade to={plan.target_version}",
        f"PLAN artifact_sha256={plan.artifact_sha256}",
        f"EXPECTED_REMOTE_SHA {plan.remote_sha}",
        f"PLAN_HASH {plan.plan_hash}",
        *plan.install_lines,
        "DRY_RUN writes=0",
    ]


def apply_upgrade(
    engine_root: Path, state_root: Path, config_path: Path, source_root: Path,
    manifest_path: Path | None, target_version: str, control_root: Path, plan_hash: str,
) -> list[str]:
    with operation_lock(control_root):
        plan = plan_upgrade(
            engine_root, state_root, config_path, source_root, manifest_path, target_version,
        )
        if plan.plan_hash != plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", f"planned={plan_hash} actual={plan.plan_hash}")
        receipt_path = binding_receipt_path(plan.config_path)
        try:
            _atomic_write(receipt_path, plan.refreshed_binding)
            result = apply_install(
                engine_root, plan.config_path, plan.state_root, plan.source_root,
                plan.manifest_path, force=False, expected_version=plan.target_version,
            )
        except Exception:
            _atomic_write(receipt_path, plan.binding_bytes)
            raise
        return [
            f"APPLIED engine-upgrade to={plan.target_version}",
            *result,
        ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core engine upgrade")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--to", required=True)
    parser.add_argument("--control-root", type=Path, default=Path.home() / ".agent-core")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    return parser


def main(argv: Sequence[str] | None, engine_root: Path) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.apply:
            if not args.plan_hash:
                raise ConfigError("FAIL_PLAN_HASH", "--apply requires --plan-hash")
            lines = apply_upgrade(
                engine_root, args.state, args.config, args.source, args.manifest,
                args.to, args.control_root, args.plan_hash,
            )
        else:
            if args.plan_hash:
                raise ConfigError("FAIL_ARGUMENT", "--plan-hash requires --apply")
            lines = render_plan(plan_upgrade(
                engine_root, args.state, args.config, args.source, args.manifest, args.to,
            ))
        for line in lines:
            print(line)
        return 0
    except ConfigError as exc:
        print(f"{exc.code} {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(None, Path(__file__).resolve().parents[1]))
