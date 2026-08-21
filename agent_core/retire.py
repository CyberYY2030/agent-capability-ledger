"""Evidence-backed lesson lifecycle reporting and verifier execution."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, ledger
from .config import ConfigError


ENGINE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("enforcement/verifiers.json")
MAX_OUTPUT_CHARS = 8192
VERIFIER_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass(frozen=True)
class LessonRecord:
    lesson_id: str
    status: str
    sink: str
    verifier_id: str | None
    evidence_sha: str | None
    last_verified: str | None
    ledger_path: Path
    line_index: int
    line: str


@dataclass(frozen=True)
class ReceiptCheck:
    fresh: bool
    reason: str
    payload: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class LifecycleReport:
    lines: tuple[str, ...]
    exit_code: int
    receipts: dict[str, Path]


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _field(line: str, name: str, pattern: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}:\s*({pattern})", line)
    return match.group(1).rstrip(".") if match else None


def _sink(line: str) -> str:
    match = re.search(
        r"\bsink\s*(?:→|->|:)\s*(.*?)(?=\s+(?:verifier|last_verified|evidence|when):|$)",
        line,
    )
    return match.group(1).strip().rstrip(".") if match else ""


def _ledger_paths(workspace: Path) -> list[Path]:
    experience = workspace / "experience"
    if experience.is_dir():
        return sorted(experience.rglob("LESSONS.md"))
    seed = workspace / "seed" / "LESSONS.md"
    return [seed] if seed.is_file() else []


def load_records(workspace: Path) -> list[LessonRecord]:
    records: list[LessonRecord] = []
    for path in _ledger_paths(workspace):
        active = False
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if line.strip().startswith("## "):
                active = ledger.is_active_heading(line.strip()[3:])
                continue
            match = ledger.ENTRY_RE.match(line)
            if not active or not match:
                continue
            lesson_id = match.group(1) or match.group(2)
            records.append(LessonRecord(
                lesson_id=lesson_id,
                status=match.group(3),
                sink=_sink(line),
                verifier_id=_field(line, "verifier", r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*"),
                evidence_sha=_field(line, "evidence", r"[0-9a-f]{64}"),
                last_verified=_field(line, "last_verified", r"\d{4}-\d{2}-\d{2}"),
                ledger_path=path,
                line_index=index,
                line=line,
            ))
    return records


def find_lesson(workspace: Path, lesson_id: str) -> LessonRecord:
    matches = [record for record in load_records(workspace) if record.lesson_id == lesson_id]
    if len(matches) != 1:
        raise ConfigError("FAIL_LESSON", f"expected one active {lesson_id}, found {len(matches)}")
    return matches[0]


def _argv_block(value: Any, source: str) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != {"argv"}:
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source} must contain only argv")
    argv = value["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.argv must be a non-empty string array")
    return {"argv": argv}


def _validate_verifier(value: Any, source: str) -> dict[str, Any]:
    required = {"id", "positive", "negative", "cwd_ref", "timeout_sec", "consumers", "enforcement_scope"}
    if not isinstance(value, dict) or set(value) != required:
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source} fields")
    if not isinstance(value["id"], str) or not VERIFIER_ID_RE.fullmatch(value["id"]):
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.id")
    if value["cwd_ref"] not in {"state", "engine"}:
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.cwd_ref")
    if not isinstance(value["timeout_sec"], int) or not 1 <= value["timeout_sec"] <= 120:
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.timeout_sec")
    positive = _argv_block(value["positive"], f"{source}.positive")
    negative = _argv_block(value["negative"], f"{source}.negative")
    consumers = value["consumers"]
    if not isinstance(consumers, list) or not consumers:
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.consumers")
    normalized_consumers = []
    for index, consumer in enumerate(consumers):
        if not isinstance(consumer, dict) or set(consumer) != {"path", "consumer_probe"}:
            raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.consumers[{index}] fields")
        if not isinstance(consumer["path"], str) or not consumer["path"]:
            raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.consumers[{index}].path")
        normalized_consumers.append({
            "path": consumer["path"],
            "consumer_probe": _argv_block(
                consumer["consumer_probe"], f"{source}.consumers[{index}].consumer_probe"),
        })
    scope = value["enforcement_scope"]
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item for item in scope):
        raise ConfigError("FAIL_VERIFIER_SCHEMA", f"{source}.enforcement_scope")
    return {
        "id": value["id"], "positive": positive, "negative": negative,
        "cwd_ref": value["cwd_ref"], "timeout_sec": value["timeout_sec"],
        "consumers": normalized_consumers, "enforcement_scope": scope,
    }


def load_verifiers(workspace: Path) -> dict[str, dict[str, Any]]:
    path = workspace / MANIFEST
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_VERIFIER_MANIFEST", f"{path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "verifiers"} or payload.get("schema") != "verifier/1":
        raise ConfigError("FAIL_VERIFIER_SCHEMA", str(path))
    if not isinstance(payload["verifiers"], list):
        raise ConfigError("FAIL_VERIFIER_SCHEMA", "verifiers must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(payload["verifiers"]):
        item = _validate_verifier(raw, f"verifiers[{index}]")
        if item["id"] in result:
            raise ConfigError("FAIL_VERIFIER_SCHEMA", f"duplicate id {item['id']}")
        result[item["id"]] = item
    return result


def _safe_relative(workspace: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigError("FAIL_VERIFIER_PATH", value)
    root = workspace.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError("FAIL_VERIFIER_PATH", value) from exc
    return resolved


def _looks_like_path(value: str) -> bool:
    return value != "<PYTHON>" and ("/" in value or "\\" in value or value.startswith("."))


def _related_inputs(workspace: Path, verifier: dict[str, Any]) -> list[dict[str, str]]:
    related_root = (workspace if verifier["cwd_ref"] == "state" else ENGINE_ROOT).resolve()
    namespace = verifier["cwd_ref"]
    paths: dict[str, Path] = {}
    blocks = [verifier["negative"], verifier["positive"]]
    blocks.extend(item["consumer_probe"] for item in verifier["consumers"])
    for block in blocks:
        for argument in block["argv"]:
            if not _looks_like_path(argument):
                continue
            path = _safe_relative(related_root, argument)
            if not path.is_file():
                raise ConfigError("FAIL_VERIFIER_INPUT", argument)
            paths[f"{namespace}:{Path(argument).as_posix()}"] = path
    for consumer in verifier["consumers"]:
        path = _safe_relative(related_root, consumer["path"])
        if not path.is_file():
            raise ConfigError("FAIL_VERIFIER_INPUT", consumer["path"])
        paths[f"{namespace}:{Path(consumer['path']).as_posix()}"] = path
    for pattern in verifier["enforcement_scope"]:
        relative = Path(pattern)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigError("FAIL_VERIFIER_PATH", pattern)
        if pattern.endswith("/**"):
            scope_root = _safe_relative(related_root, pattern[:-3])
            matches = [path for path in scope_root.rglob("*") if path.is_file()] if scope_root.is_dir() else []
        else:
            matches = [path for path in related_root.glob(pattern) if path.is_file()]
        if not matches:
            raise ConfigError("FAIL_VERIFIER_INPUT", pattern)
        for path in matches:
            relative_path = path.relative_to(related_root).as_posix()
            resolved_path = _safe_relative(related_root, relative_path)
            paths[f"{namespace}:{relative_path}"] = resolved_path
    return [{"path": name, "sha256": _sha256(paths[name].read_bytes())} for name in sorted(paths)]


def _expanded(argv: list[str]) -> list[str]:
    return [sys.executable if item == "<PYTHON>" else item for item in argv]


def _run(kind: str, argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    expanded = _expanded(argv)
    try:
        result = subprocess.run(
            expanded, cwd=cwd, shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        output = (result.stdout + "\n<stderr>\n" + result.stderr).replace("\r\n", "\n").replace("\r", "\n")
        exit_code = result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"{type(exc).__name__}: {exc}"
        exit_code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127
    bounded = output[:MAX_OUTPUT_CHARS]
    return {
        "kind": kind, "argv": expanded, "exit_code": exit_code,
        "output_sha256": _sha256(bounded.encode("utf-8")),
        "output_chars": len(bounded), "truncated": len(output) > len(bounded),
    }


def _git_revision(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={workspace.resolve().as_posix()}", "-C", str(workspace),
         "rev-parse", "HEAD"],
        shell=False, check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else "unversioned"


def _now(value: dt.datetime | None) -> dt.datetime:
    current = value or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ConfigError("FAIL_TIME", "now must be timezone-aware")
    return current.astimezone(dt.timezone.utc).replace(microsecond=0)


def _write_receipt(control_root: Path, payload: dict[str, Any]) -> Path:
    content = _canonical_bytes(payload)
    digest = _sha256(content)
    root = control_root.resolve() / "evidence-pending"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{digest}.json"
    handle, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _generate_receipt(workspace: Path, control_root: Path, record: LessonRecord,
                      verifier: dict[str, Any], current: dt.datetime) -> Path:
    inputs = _related_inputs(workspace, verifier)
    cwd = workspace if verifier["cwd_ref"] == "state" else ENGINE_ROOT
    runs = [_run("negative", verifier["negative"]["argv"], cwd, verifier["timeout_sec"]),
            _run("positive", verifier["positive"]["argv"], cwd, verifier["timeout_sec"])]
    runs.extend(_run("consumer", item["consumer_probe"]["argv"], cwd, verifier["timeout_sec"])
                for item in verifier["consumers"])
    if runs[0]["exit_code"] == 0:
        raise ConfigError("FAIL_NEGATIVE_GREEN", record.lesson_id)
    if runs[1]["exit_code"] != 0:
        raise ConfigError("FAIL_POSITIVE_RED", record.lesson_id)
    if not any(run["exit_code"] == 0 for run in runs[2:]):
        raise ConfigError("FAIL_CONSUMER_RED", record.lesson_id)
    target = "enforced" if record.status == "checklist" else "archived"
    payload = {
        "schema": "evidence/1", "lesson_id": record.lesson_id,
        "from_status": record.status, "to_status": target,
        "verifier_id": verifier["id"],
        "verifier_sha256": _sha256(_canonical_bytes(verifier)),
        "enforcement_scope": verifier["enforcement_scope"],
        "inputs": inputs, "runs": runs,
        "verified_revision": _git_revision(workspace),
        "verified_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine_version": __version__,
    }
    return _write_receipt(control_root, payload)


def _invalid_receipt(reason: str, payload: dict[str, Any] | None = None,
                     digest: str = "") -> ReceiptCheck:
    return ReceiptCheck(False, reason, payload or {}, digest)


def _receipt_schema_error(payload: dict[str, Any]) -> str | None:
    required = {
        "schema", "lesson_id", "from_status", "to_status", "verifier_id", "verifier_sha256",
        "enforcement_scope", "inputs", "runs", "verified_revision", "verified_utc", "engine_version",
    }
    if set(payload) != required or payload.get("schema") != "evidence/1":
        return "receipt_schema"
    strings = ("lesson_id", "verifier_id", "verified_revision", "engine_version")
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in strings):
        return "receipt_schema"
    if not isinstance(payload.get("verifier_sha256"), str) or not SHA256_RE.fullmatch(payload["verifier_sha256"]):
        return "receipt_schema"
    if payload.get("from_status") not in {"checklist", "enforced"}:
        return "receipt_status"
    expected_target = "enforced" if payload["from_status"] == "checklist" else "archived"
    if payload.get("to_status") != expected_target:
        return "receipt_status"
    scope = payload.get("enforcement_scope")
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item for item in scope):
        return "receipt_schema"
    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return "receipt_schema"
    input_paths = []
    for item in inputs:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str) or not item["path"]
                or not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"])):
            return "receipt_schema"
        input_paths.append(item["path"])
    if input_paths != sorted(set(input_paths)):
        return "receipt_schema"
    runs = payload.get("runs")
    run_fields = {"kind", "argv", "exit_code", "output_sha256", "output_chars", "truncated"}
    if not isinstance(runs, list) or len(runs) < 3:
        return "receipt_runs"
    for run in runs:
        if not isinstance(run, dict) or set(run) != run_fields:
            return "receipt_runs"
        if run["kind"] not in {"negative", "positive", "consumer"}:
            return "receipt_runs"
        if not isinstance(run["argv"], list) or not run["argv"] or any(not isinstance(x, str) for x in run["argv"]):
            return "receipt_runs"
        if not isinstance(run["exit_code"], int):
            return "receipt_runs"
        if not isinstance(run["output_sha256"], str) or not SHA256_RE.fullmatch(run["output_sha256"]):
            return "receipt_runs"
        if not isinstance(run["output_chars"], int) or run["output_chars"] < 0:
            return "receipt_runs"
        if not isinstance(run["truncated"], bool):
            return "receipt_runs"
    verified_utc = payload.get("verified_utc")
    if not isinstance(verified_utc, str) or not UTC_RE.fullmatch(verified_utc):
        return "receipt_time"
    return None


def verify_receipt(workspace: Path, receipt_path: Path, *, expected_lesson_id: str | None = None,
                   expected_hash: str | None = None, max_age_days: int = 90,
                   now: dt.datetime | None = None) -> ReceiptCheck:
    try:
        raw = receipt_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _invalid_receipt("receipt_hash unreadable")
    if not isinstance(payload, dict):
        return _invalid_receipt("receipt_hash invalid", {}, _sha256(raw))
    canonical = _canonical_bytes(payload)
    digest = _sha256(canonical)
    wanted = expected_hash or receipt_path.stem
    if raw != canonical or not SHA256_RE.fullmatch(wanted) or digest != wanted:
        return _invalid_receipt(f"receipt_hash expected={wanted} actual={digest}", payload, digest)
    schema_error = _receipt_schema_error(payload)
    if schema_error:
        return _invalid_receipt(schema_error, payload, digest)
    if expected_lesson_id and payload.get("lesson_id") != expected_lesson_id:
        return _invalid_receipt("receipt_lesson", payload, digest)
    try:
        verifiers = load_verifiers(workspace)
        verifier = verifiers[payload["verifier_id"]]
        current_inputs = _related_inputs(workspace, verifier)
    except (ConfigError, KeyError):
        return _invalid_receipt("related_input manifest", payload, digest)
    if _sha256(_canonical_bytes(verifier)) != payload.get("verifier_sha256"):
        return _invalid_receipt("related_input verifier", payload, digest)
    if current_inputs != payload.get("inputs") or verifier["enforcement_scope"] != payload.get("enforcement_scope"):
        return _invalid_receipt("related_input files", payload, digest)
    runs = payload["runs"]
    if runs[0].get("kind") != "negative" or runs[0].get("exit_code") == 0:
        return _invalid_receipt("receipt_negative", payload, digest)
    if runs[1].get("kind") != "positive" or runs[1].get("exit_code") != 0:
        return _invalid_receipt("receipt_positive", payload, digest)
    if not any(run.get("kind") == "consumer" and run.get("exit_code") == 0 for run in runs[2:]):
        return _invalid_receipt("receipt_consumer", payload, digest)
    verified_utc = payload["verified_utc"]
    verified = dt.datetime.strptime(verified_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    age = (_now(now) - verified).days
    if age < 0:
        return _invalid_receipt("receipt_time future", payload, digest)
    if age > max_age_days:
        return _invalid_receipt(f"expired age_days={age}", payload, digest)
    return ReceiptCheck(True, "fresh", payload, digest)


def _receipt_for_record(workspace: Path, record: LessonRecord) -> Path | None:
    if not record.evidence_sha:
        return None
    return workspace / "evidence" / record.lesson_id / f"{record.evidence_sha}.json"


def inspect_lifecycle(workspace: Path, control_root: Path, *, report: bool = False,
                      verify: bool = False, allow_exec: bool = False, strict: bool = False,
                      max_age_days: int = 90, now: dt.datetime | None = None) -> LifecycleReport:
    if report and (verify or allow_exec):
        raise ConfigError("FAIL_REPORT_EXEC", "--report cannot use --verify or --allow-exec")
    if verify and not allow_exec:
        raise ConfigError("FAIL_EXEC_APPROVAL", "--verify requires --allow-exec")
    root = workspace.resolve()
    records = load_records(root)
    if not records:
        raise ConfigError("FAIL_LEDGER", f"no active lessons under {root}")
    current = _now(now)
    events: list[tuple[int, str]] = []
    receipts: dict[str, Path] = {}
    failed = False
    if len(records) >= int(ledger.ACTIVE_CAP * 0.8):
        events.append((1, f"WARN capacity {len(records)}/{ledger.ACTIVE_CAP}"))
    try:
        verifiers = load_verifiers(root)
    except ConfigError:
        verifiers = {}
    for record in records:
        try:
            sink = _safe_relative(root, record.sink) if record.sink else None
        except ConfigError:
            sink = None
        if sink is None or not sink.is_file():
            events.append((7, f"BROKEN_SINK {record.lesson_id} {record.sink or '<missing>'}"))
            failed = failed or strict
            continue
        if record.status == "pending":
            if record.lesson_id in sink.read_text(encoding="utf-8", errors="replace"):
                events.append((4, f"READY_TO_CHECKLIST {record.lesson_id}"))
            else:
                events.append((6, f"NOT_READY {record.lesson_id} missing_sink_reference"))
            continue
        if record.status == "checklist":
            verifier = verifiers.get(record.verifier_id or "")
            if verifier is None:
                events.append((6, f"NOT_READY {record.lesson_id} missing_verifier"))
                failed = failed or verify
                continue
            if not verify:
                events.append((6, f"NOT_READY {record.lesson_id} verification_required"))
                continue
            try:
                receipt = _generate_receipt(root, control_root, record, verifier, current)
            except ConfigError as exc:
                reason = "missing_input" if exc.code == "FAIL_VERIFIER_INPUT" else exc.code
                events.append((6, f"NOT_READY {record.lesson_id} {reason} {exc.detail}".rstrip()))
                failed = True
                continue
            receipts[record.lesson_id] = receipt
            events.append((3, f"READY_TO_ENFORCED {record.lesson_id} {receipt} {receipt.stem}"))
            continue
        receipt = _receipt_for_record(root, record)
        if receipt is None:
            events.append((2, f"STALE_EVIDENCE {record.lesson_id} missing_receipt"))
            continue
        check = verify_receipt(root, receipt, expected_lesson_id=record.lesson_id,
                               expected_hash=record.evidence_sha, max_age_days=max_age_days, now=current)
        if not check.fresh:
            events.append((2, f"STALE_EVIDENCE {record.lesson_id} {check.reason}"))
            continue
        if record.last_verified != check.payload["verified_utc"][:10]:
            events.append((2, f"STALE_EVIDENCE {record.lesson_id} last_verified"))
            continue
        if not verify:
            events.append((5, f"FRESH_EVIDENCE {record.lesson_id}"))
            continue
        verifier = verifiers.get(record.verifier_id or "")
        if verifier is None:
            events.append((2, f"STALE_EVIDENCE {record.lesson_id} related_input manifest"))
            continue
        try:
            new_receipt = _generate_receipt(root, control_root, record, verifier, current)
        except ConfigError as exc:
            events.append((6, f"NOT_READY {record.lesson_id} {exc.code} {exc.detail}".rstrip()))
            failed = True
            continue
        receipts[record.lesson_id] = new_receipt
        events.append((0, f"READY_TO_ARCHIVE {record.lesson_id} {new_receipt} {new_receipt.stem}"))
    lines = tuple(line for _priority, line in sorted(events, key=lambda item: (item[0], item[1])))
    return LifecycleReport(lines, 1 if failed else 0, receipts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-core lessons retire")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--control-root", type=Path, default=Path.home() / ".agent-core")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--allow-exec", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--max-age-days", type=int, default=90)
    args = parser.parse_args(argv)
    try:
        result = inspect_lifecycle(
            args.workspace, args.control_root, report=args.report, verify=args.verify,
            allow_exec=args.allow_exec, strict=args.strict, max_age_days=args.max_age_days,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
