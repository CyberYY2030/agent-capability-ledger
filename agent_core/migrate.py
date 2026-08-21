"""Plan-bound, forward-only state ledger schema migrations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __version__, ledger
from .config import ConfigError
from .promote import operation_lock


TARGET_SCHEMA = "lessons-ledger/2"
SUPPORTED_SCHEMAS = {"lessons-ledger/1", TARGET_SCHEMA}
HEADER_RE = re.compile(r"^<!-- lessons-schema: ([^\s]+) -->(?=\r?$)", re.MULTILINE)
LOCK_FIELDS = {"engine_version", "engine_source", "schema_version", "pinned_at"}


@dataclass(frozen=True)
class MigrationFile:
    path: Path
    before_sha256: str
    before: bytes
    after: bytes
    before_counts: dict[str, int]
    after_counts: dict[str, int]


@dataclass(frozen=True)
class MigrationPlan:
    root: Path
    target_schema: str
    files: tuple[MigrationFile, ...]
    plan_hash: str

    @property
    def changed(self) -> tuple[MigrationFile, ...]:
        return tuple(item for item in self.files if item.before != item.after)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _counts(text: str) -> dict[str, int]:
    counts, active, archived, _missing = ledger.ledger_summary(text)
    return {**counts, "active": active, "archived": archived}


def _read_object(path: Path, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(code, f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(code, f"root must be an object: {path}")
    return payload, content


def _transform_ledger(text: str, target: str) -> str:
    return HEADER_RE.sub(f"<!-- lessons-schema: {target} -->", text, count=1)


def _ledger_after(path: Path, content: bytes, target: str) -> tuple[bytes, dict[str, int], dict[str, int]]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("FAIL_MIGRATION_INPUT", f"{path}: {exc}") from exc
    matches = HEADER_RE.findall(text)
    if len(matches) != 1 or matches[0] not in SUPPORTED_SCHEMAS:
        raise ConfigError("FAIL_MIGRATION_INPUT", f"unsupported or ambiguous schema header: {path}")
    before_counts = _counts(text)
    rendered = _transform_ledger(text, target)
    after_counts = _counts(rendered)
    if before_counts != after_counts:
        raise ConfigError("FAIL_MIGRATION_COUNTS", str(path))
    expected_scope = "global" if path.parent.name == "experience" else "profile"
    _ids, errors, _warnings = ledger.parse_ledger(rendered, expected_scope, str(path))
    if errors:
        raise ConfigError("FAIL_MIGRATION_LEDGER", "; ".join(errors))
    return rendered.encode("utf-8"), before_counts, after_counts


def plan_migration(root: Path, target_schema: str = TARGET_SCHEMA) -> MigrationPlan:
    root = root.resolve()
    if target_schema != TARGET_SCHEMA:
        raise ConfigError("FAIL_MIGRATION_DIRECTION", f"unsupported target: {target_schema}")
    experience = root / "experience"
    ledger_paths = sorted(experience.rglob("LESSONS.md"), key=lambda item: item.as_posix())
    if not ledger_paths or not (experience / "LESSONS.md").is_file():
        raise ConfigError("FAIL_MIGRATION_INPUT", "global experience/LESSONS.md is missing")
    files: list[MigrationFile] = []
    observed_schemas: set[str] = set()
    for path in ledger_paths:
        before = path.read_bytes()
        try:
            text = before.decode("utf-8")
        except UnicodeError as exc:
            raise ConfigError("FAIL_MIGRATION_INPUT", f"{path}: {exc}") from exc
        schemas = HEADER_RE.findall(text)
        if len(schemas) != 1 or schemas[0] not in SUPPORTED_SCHEMAS:
            raise ConfigError("FAIL_MIGRATION_INPUT", f"unsupported or ambiguous schema header: {path}")
        observed_schemas.add(schemas[0])
        after, before_counts, after_counts = _ledger_after(path, before, target_schema)
        files.append(MigrationFile(path, _sha256(before), before, after, before_counts, after_counts))

    lock_path = root / "agent-core.lock.json"
    lock, lock_before = _read_object(lock_path, "FAIL_STATE_LOCK")
    if set(lock) != LOCK_FIELDS or lock.get("schema_version") not in SUPPORTED_SCHEMAS:
        raise ConfigError("FAIL_STATE_LOCK", "lock fields or schema mismatch")
    observed_schemas.add(str(lock["schema_version"]))
    if any(schema not in SUPPORTED_SCHEMAS for schema in observed_schemas):
        raise ConfigError("FAIL_MIGRATION_DIRECTION", "only /1 to /2 is supported")
    lock_after_payload = dict(lock)
    lock_after_payload["schema_version"] = target_schema
    lock_after = _json_bytes(lock_after_payload)
    files.append(MigrationFile(lock_path, _sha256(lock_before), lock_before, lock_after, {}, {}))

    plan_payload = {
        "migration_version": __version__,
        "root": str(root),
        "target_schema": target_schema,
        "files": [
            {
                "path": item.path.relative_to(root).as_posix(),
                "before_sha256": item.before_sha256,
                "after_sha256": _sha256(item.after),
            }
            for item in files
        ],
    }
    digest = _sha256(json.dumps(
        plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    return MigrationPlan(root, target_schema, tuple(files), digest)


def render_plan(plan: MigrationPlan) -> list[str]:
    lines = [
        f"PLAN operation=migrate from={plan.root} to={plan.target_schema}",
        f"PLAN_HASH {plan.plan_hash}",
    ]
    for item in plan.files:
        if item.before_counts:
            counts = ",".join(f"{key}={value}" for key, value in sorted(item.before_counts.items()))
            lines.append(
                f"SOURCE {item.path.relative_to(plan.root).as_posix()} "
                f"status={'change' if item.before != item.after else 'same'} counts={counts}"
            )
    lines.append(f"DRY_RUN writes=0 planned_writes={len(plan.changed)}")
    return lines


def _atomic_write(path: Path, content: bytes) -> None:
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_migration(
    root: Path, control_root: Path, plan_hash: str, target_schema: str = TARGET_SCHEMA,
) -> list[str]:
    with operation_lock(control_root):
        plan = plan_migration(root, target_schema)
        if plan.plan_hash != plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", f"planned={plan_hash} actual={plan.plan_hash}")
        changed = plan.changed
        if not changed:
            return [f"PASS migration to={target_schema} no_changes=true"]
        snapshot = control_root.resolve() / "rollback" / f"migrate-{uuid.uuid4().hex}"
        snapshot.mkdir(parents=True)
        try:
            for item in changed:
                relative = item.path.relative_to(plan.root)
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.before)
            (snapshot / "snapshot.json").write_bytes(_json_bytes({
                "schema": "migration-snapshot/1",
                "root": str(plan.root),
                "plan_hash": plan.plan_hash,
                "files": [item.path.relative_to(plan.root).as_posix() for item in changed],
            }))
            for item in changed:
                if _sha256(item.path.read_bytes()) != item.before_sha256:
                    raise ConfigError("FAIL_PLAN_HASH", f"input changed: {item.path}")
                _atomic_write(item.path, item.after)
            verified = plan_migration(root, target_schema)
            if verified.changed:
                raise ConfigError("FAIL_MIGRATION_VERIFY", "migration is not idempotent")
        except Exception as exc:
            try:
                for item in reversed(changed):
                    _atomic_write(item.path, (snapshot / item.path.relative_to(plan.root)).read_bytes())
            except Exception as rollback_exc:
                raise ConfigError("FAIL_MIGRATION_ROLLBACK", str(rollback_exc)) from rollback_exc
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError("FAIL_MIGRATION", str(exc)) from exc
        return [
            f"APPLIED migration to={target_schema} writes={len(changed)}",
            f"PASS snapshot={snapshot}",
        ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core migrate")
    parser.add_argument("--from", dest="root", type=Path, required=True)
    parser.add_argument("--to", default=TARGET_SCHEMA)
    parser.add_argument("--control-root", type=Path, default=Path.home() / ".agent-core")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.apply:
            if not args.plan_hash:
                raise ConfigError("FAIL_PLAN_HASH", "--apply requires --plan-hash")
            lines = apply_migration(args.root, args.control_root, args.plan_hash, args.to)
        else:
            if args.plan_hash:
                raise ConfigError("FAIL_ARGUMENT", "--plan-hash requires --apply")
            lines = render_plan(plan_migration(args.root, args.to))
        for line in lines:
            print(line)
        return 0
    except ConfigError as exc:
        print(f"{exc.code} {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
