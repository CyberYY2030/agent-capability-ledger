"""Append-only candidate publication and transactional lesson promotion."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unicodedata import normalize

from . import ledger
from .config import ConfigError, HOST_LABEL_RE
from .freshness import (CANDIDATE_ID_RE, SHA_RE, load_candidate, parse_candidate_bytes,
                        record_remote_head, require_fresh)
from .match import parse_markdown, tokenize
from .project import resolve_project_context
from .repository import RepositoryContext, _is_reparse_alias, resolve_repository_context
from .state import _validate_state_binding_context, binding_receipt_path, validate_state_binding


GLOBAL_LEDGER = Path("experience/LESSONS.md")
INBOX = Path("inbox")
CONSUMED = INBOX / "consumed"
LAST_COMMITTED = Path("txn/last_committed.json")
PROJECT_LEDGER = Path(".agents/LESSONS.md")
PROJECT_INBOX = Path(".agents/inbox")
PROJECT_CONSUMED = PROJECT_INBOX / "consumed"
CANONICAL_REMOTE = "origin"
CANONICAL_TARGET_REF = "refs/heads/main"
SNAPSHOT_SCHEMA = "agent-core-snapshot/2"
JOURNAL_SCHEMA = "agent-core-journal/1"
RECOVERY_JOURNAL_SCHEMA = "agent-core-recovery-journal/1"
ARTIFACT_ID_RE = re.compile(r"^\d{8}T\d{6}\d{6}Z-[0-9a-f]{16}$")
ADVANCE_LESSON_ID_RE = re.compile(r"^(?:L-[A-Za-z]?\d+|[A-Z][A-Z0-9-]*-\d+)$")
ROLLBACK_ID_RE = re.compile(r"^\d{8}T\d{12}Z$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOURNAL_EVENTS = {"preflight_ok", "snapshot_durable", "push_attempt", "ancestry_observed", "source_removal_intent", "source_removed", "fast_forward_done", "remote_pointer_updated", "completed", "failed", "cleanup_pending"}
RECOVERY_ACTIONS = {"artifact-cleanup", "input-disposition", "cleanup-only", "local-finalization"}
RECOVERY_EVENTS = {
    "pointer-updated", "source-quarantine-intent", "source-quarantined",
    "source-restore-intent", "source-restored", "quarantine-delete-intent",
    "quarantine-deleted", "fast-forward-intent", "fast-forward-done",
    "worktree-cleanup-intent", "worktree-cleaned", "source-preserved", "converged", "completed",
    "failed", "cleanup-pending",
}
RECOVERY_EVENT_ROLES = {
    "source-quarantine-intent": "source-quarantine", "source-quarantined": "source-quarantine",
    "source-restore-intent": "source-restore", "source-restored": "source-restore",
    "source-preserved": "source-preserved",
    "quarantine-delete-intent": "quarantine-delete", "quarantine-deleted": "quarantine-delete",
    "fast-forward-intent": "fast-forward", "fast-forward-done": "fast-forward",
    "pointer-updated": "pointer", "worktree-cleanup-intent": "worktree-cleanup",
    "worktree-cleaned": "worktree-cleanup",
}


@dataclass(frozen=True)
class Plan:
    operation: str
    candidate_id: str
    expected_remote_sha: str
    plan_hash: str
    lines: tuple[str, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class RollbackPreparedEvidence:
    """Private, process-local facts used only to compare a rollback Prepared."""

    snapshot_id: str
    snapshot_manifest_sha256: str
    original_operation_id: str
    original_operation: str
    original_journal_final_record_sha256: str
    settlement_kind: str
    settlement_record_sha256: str
    expected_base_sha: str
    prepared_target_sha: str
    prepared_tree_oid: str
    restore_count: int
    restore_facts_sha256: str
    reviewed_plan_hash: str
    pinned_observed_sha: str
    inverse_facts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Prepared:
    repo: Path
    control_root: Path
    txn: Path
    sha: str
    expected_remote_sha: str
    operation: str
    candidate_id: str
    changed_paths: tuple[str, ...]
    source_path: Path | None = None
    source_content: bytes | None = None
    context: RepositoryContext | None = None
    config_path: Path | None = None
    binding: tuple[tuple[str, str], ...] | None = None
    plan_hash: str = ""
    input_digest_sha256: str = ""
    binding_digest_sha256: str = ""
    tree_oid: str = ""
    target_ref: str = CANONICAL_TARGET_REF
    # Advance evidence is frozen from the reviewed plan.  Canonical apply must
    # never reacquire its semantics from a caller-controlled receipt path.
    advance_evidence: tuple[tuple[str, str], ...] | None = None
    # Rollback evidence is deliberately ephemeral.  R1b must independently
    # reprove every fact instead of treating this in-process capsule as durable.
    rollback_evidence: RollbackPreparedEvidence | None = None


@dataclass(frozen=True)
class ChangeExpectation:
    """One operation-specific Git change, before its transaction commit."""

    status: str
    path: str
    mode: str | None = "100644"


@dataclass(frozen=True)
class ChangeFact:
    status: str
    path: str
    mode: str
    oid: str


@dataclass(frozen=True)
class BlobMove:
    """A deletion/addition pair that must preserve one regular blob exactly."""

    source_path: str
    destination_path: str


@dataclass(frozen=True)
class Result:
    sha: str
    rollback_id: str
    cleanup_pending: bool = False
    cleanup_kind: str | None = None


@dataclass(frozen=True)
class CanonicalRemoteOutcome:
    """One pinned, privacy-safe canonical remote observation."""

    status: str
    observed_sha: str


@dataclass(frozen=True)
class CanonicalPushResult:
    """The intentionally minimal, privacy-safe result of a canonical CAS push."""

    returncode: int


@dataclass(frozen=True)
class RecoveryJournalRef:
    """One durable, host-local R1 checkpoint chain; R0 only reads it."""

    operation_id: str
    path: Path
    control_root: Path
    sequence: int
    record_sha256: str
    control_identity_sha256: str


@dataclass(frozen=True)
class RecoveryResult:
    """Closed result of one local canonical recovery checkpoint execution."""

    action: str
    operation_id: str
    converged: bool
    cleanup_pending: bool
    cleanup_kind: str | None = None


@dataclass(frozen=True)
class RecoveryQuarantine:
    root: Path
    root_identity: tuple[int, int]
    operation: Path
    operation_identity: tuple[int, int]
    target: Path
    token: OwnedFileToken
    raw: bytes
    handle_identity_sha256: str


@dataclass(frozen=True)
class CanonicalPlanDispatch:
    plan: Plan


@dataclass(frozen=True)
class CanonicalApplyDispatch:
    result: RecoveryResult


@dataclass(frozen=True)
class StandaloneDispatch:
    recovered_sha: str
    legacy_source: str


@dataclass(frozen=True)
class ProjectResult:
    lesson_id: str
    changed_paths: tuple[str, ...]


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    resolved = repo.resolve()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "-c", "core.longpaths=true",
         "-c", "user.name=agent-core", "-c", f"user.email=agent-core{chr(64)}invalid",
         "-C", str(resolved), *args],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ConfigError("FAIL_GIT", detail)
    return result


def _git_bytes(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    resolved = repo.resolve()
    return subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "-c", "core.longpaths=true",
         "-C", str(resolved), *args],
        check=False, capture_output=True, timeout=10,
    )


def _canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _freeze_binding(binding: dict[str, str] | None) -> tuple[tuple[str, str], ...] | None:
    if binding is None:
        return None
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in binding.items()):
        raise ConfigError("FAIL_PLAN_HASH", "binding is invalid")
    return tuple(sorted(binding.items()))


def _binding_digest(binding: dict[str, str] | None) -> str:
    return _canonical_hash(binding or {})


def _prepared_tree(repo: Path, sha: str) -> str:
    tree = _git(repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    if not SHA_RE.fullmatch(tree):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "prepared tree")
    return tree


def _prepared(
    repo: Path, control_root: Path, txn: Path, sha: str, plan: Plan,
    changed_paths: tuple[str, ...], *, source_path: Path | None = None,
    source_content: bytes | None = None, context: RepositoryContext | None = None,
    config_path: Path | None = None, binding: dict[str, str] | None = None,
    rollback_evidence: RollbackPreparedEvidence | None = None,
) -> Prepared:
    input_key = {"publish": "source_sha256", "promote": "candidate_sha256",
                 "advance": "receipt_sha256", "rollback": "snapshot_sha"}.get(plan.operation)
    if input_key is None or not isinstance(plan.payload.get(input_key), str):
        raise ConfigError("FAIL_PLAN_HASH", "transaction input digest is missing")
    advance_evidence: tuple[tuple[str, str], ...] | None = None
    if plan.operation == "advance":
        fields = ("ledger_path", "from_status", "to_status", "verifier_id", "verified_utc")
        if any(not isinstance(plan.payload.get(field), str) or not plan.payload[field] for field in fields):
            raise ConfigError("FAIL_PLAN_HASH", "advance evidence is missing")
        advance_evidence = tuple(sorted((field, plan.payload[field]) for field in fields))
    return Prepared(
        repo, control_root, txn, sha, plan.expected_remote_sha, plan.operation,
        plan.candidate_id, changed_paths, source_path, source_content, context, config_path,
        _freeze_binding(binding), plan.plan_hash, plan.payload[input_key], _binding_digest(binding),
        _prepared_tree(txn, sha), plan.payload["target_ref"], advance_evidence, rollback_evidence,
    )


def _candidate_path(repo: Path, candidate_id: str, *, include_consumed: bool = False) -> Path:
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ConfigError("FAIL_CANDIDATE", f"invalid candidate id: {candidate_id}")
    current = repo / INBOX / f"{candidate_id}.md"
    if current.is_file():
        return current
    consumed = repo / CONSUMED / f"{candidate_id}.md"
    if include_consumed and consumed.is_file():
        return consumed
    raise ConfigError("FAIL_CANDIDATE_MISSING", candidate_id)


def _safe_state_path(context: RepositoryContext, relative: Path, *, required: str | None = None) -> Path:
    text = normalize("NFC", relative.as_posix())
    candidate = Path(text)
    root = context.state_root
    try:
        if (
            not text or candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != text
            or _is_reparse_alias(root) or not root.is_dir()
        ):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", text or "state root")
        path = root / candidate
        current = path.parent
        while True:
            if os.path.lexists(current) and (_is_reparse_alias(current) or not current.is_dir()):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", text)
            if current == root:
                break
            current = current.parent
        if os.path.lexists(path) and _is_reparse_alias(path):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", text)
        if required == "file" and (not path.is_file() or _is_reparse_alias(path)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", text)
        if required == "dir" and (not path.is_dir() or _is_reparse_alias(path)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", text)
        return path
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", text or "state root") from exc


def _state_candidate_path(context: RepositoryContext, candidate_id: str,
                          *, include_consumed: bool = False) -> Path:
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ConfigError("FAIL_CANDIDATE", f"invalid candidate id: {candidate_id}")
    current = _safe_state_path(context, INBOX / f"{candidate_id}.md")
    if current.exists():
        return _safe_state_path(context, INBOX / f"{candidate_id}.md", required="file")
    consumed = _safe_state_path(context, CONSUMED / f"{candidate_id}.md")
    if include_consumed and consumed.exists():
        return _safe_state_path(context, CONSUMED / f"{candidate_id}.md", required="file")
    raise ConfigError("FAIL_CANDIDATE_MISSING", candidate_id)


def _plan_context(context: RepositoryContext, binding: dict[str, str] | None = None) -> dict[str, str]:
    values = {"layout": context.layout, "state_prefix": context.state_prefix}
    if binding is not None:
        values["binding"] = binding
    return values


def _prepare_context(repo: Path, plan: Plan) -> RepositoryContext:
    context = resolve_repository_context(repo)
    if (
        plan.payload.get("layout") != context.layout
        or plan.payload.get("state_prefix") != context.state_prefix
    ):
        raise ConfigError("FAIL_STATE_REPOSITORY", "state repository layout drifted")
    return context


def _resolved_local_path(path: Path, label: str) -> Path:
    """Resolve one possibly-not-yet-created local path without accepting aliases."""
    try:
        requested = Path(path).absolute()
        missing: list[str] = []
        current = requested
        while not os.path.lexists(current):
            if current == current.parent:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", label)
            missing.append(current.name)
            current = current.parent
        anchor = current
        while True:
            if _is_reparse_alias(current) or not current.is_dir():
                raise ConfigError("FAIL_TRANSACTION_SCOPE", label)
            if current == current.parent:
                break
            current = current.parent
        return anchor.resolve().joinpath(*reversed(missing))
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", label) from exc


def _posix_control_base(platform: str, home: Path) -> Path:
    if platform == "darwin":
        return home / "Library" / "Application Support"
    return Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))


def _local_control_base() -> Path:
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if not value:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "local control root")
        base = Path(value)
        text = str(base)
        if text.startswith("\\\\") or not re.fullmatch(r"[A-Za-z]:", base.drive):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "local control root")
        try:
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(base.drive + "\\") != 3:  # DRIVE_FIXED
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "local control root")
        except ConfigError:
            raise
        except (AttributeError, OSError):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "local control root")
        return base
    return _posix_control_base(sys.platform, Path.home())


def _canonical_control_root(context: RepositoryContext, supplied: Path | None) -> Path:
    if supplied is not None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical control root override")
    digest = hashlib.sha256(str(context.repo_root).encode("utf-8")).hexdigest()
    root = _resolved_local_path(_local_control_base() / "agent-core" / "transactions" / digest,
                                "local control root")
    try:
        root.relative_to(context.repo_root)
    except ValueError:
        return root
    raise ConfigError("FAIL_TRANSACTION_SCOPE", "local control root")


def _existing_ancestor(path: Path) -> Path:
    current = Path(path)
    try:
        while not os.path.lexists(current):
            if current == current.parent:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
            current = current.parent
        if _is_reparse_alias(current) or not current.is_dir():
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        return current.resolve()
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem") from exc


def _locality_digest(platform: str, *facts: str | int) -> str:
    rendered = "\0".join((platform, *(str(item) for item in facts))).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _unescape_mountinfo(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _rejected_control_filesystem(filesystem: str) -> bool:
    exact = {
        "nfs", "nfs4", "cifs", "9p", "ceph", "glusterfs", "lustre", "afs", "virtiofs",
        "davfs2", "tmpfs", "ramfs",
    }
    return filesystem in exact or filesystem.startswith((
        "smb", "fuse", "sshfs", "webdav", "osxfuse", "macfuse",
    ))


def _linux_mountinfo_locality(target: str, mountinfo: str) -> str:
    try:
        matches: list[tuple[str, str, str, str]] = []
        for line in mountinfo.splitlines():
            left, marker, right = line.partition(" - ")
            fields = left.split()
            tail = right.split()
            if not marker or len(fields) < 5 or len(tail) < 3:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
            mount_id, device, mountpoint = fields[0], fields[2], _unescape_mountinfo(fields[4])
            if not mountpoint.startswith("/"):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
            if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
                matches.append((mountpoint, mount_id, device, tail[0].lower()))
        if not matches:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        mountpoint, mount_id, device, filesystem = max(matches, key=lambda item: len(item[0]))
        if not filesystem or _rejected_control_filesystem(filesystem):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        return _locality_digest("linux", mount_id, device, filesystem, mountpoint)
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem") from exc


def _linux_control_filesystem(path: Path, mountinfo: str | None = None) -> str:
    try:
        content = mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        return _linux_mountinfo_locality(str(path.resolve()), content)
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem") from exc


def _darwin_statfs(path: Path) -> tuple[int, str]:
    if sys.platform != "darwin":
        raise OSError("statfs is only available on Darwin")
    import ctypes

    class _FSID(ctypes.Structure):
        _fields_ = [("val", ctypes.c_int32 * 2)]

    class _StatFS(ctypes.Structure):
        _fields_ = [
            ("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64), ("f_fsid", _FSID), ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32), ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024), ("f_mntfromname", ctypes.c_char * 1024),
            ("f_reserved", ctypes.c_uint32 * 8),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    statfs = libc.statfs
    statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_StatFS))
    statfs.restype = ctypes.c_int
    facts = _StatFS()
    if statfs(os.fsencode(path), ctypes.byref(facts)) != 0:
        raise OSError(ctypes.get_errno(), "statfs")
    filesystem = bytes(facts.f_fstypename).split(b"\0", 1)[0].decode("ascii", "strict").lower()
    return int(facts.f_flags), filesystem


def _darwin_control_filesystem(path: Path, *, flags: int | None = None,
                               filesystem: str | None = None, device: int | None = None) -> str:
    try:
        if flags is None and filesystem is None:
            flags, filesystem = _darwin_statfs(path)
            device = os.stat(path).st_dev
        if (not isinstance(flags, int) or not isinstance(filesystem, str)
                or not (flags & 0x1000) or not isinstance(device, int)):  # MNT_LOCAL
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        kind = filesystem.lower()
        if not kind or _rejected_control_filesystem(kind):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        return _locality_digest("darwin", kind, device if device is not None else "unknown")
    except ConfigError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem") from exc


def _windows_control_filesystem(path: Path) -> str:
    try:
        drive = path.drive
        if not re.fullmatch(r"[A-Za-z]:", drive):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem")
        stat = os.stat(path)
        return _locality_digest("windows", drive.upper(), stat.st_dev)
    except ConfigError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "control filesystem") from exc


def _control_filesystem_sha256(control_root: Path) -> str:
    ancestor = _existing_ancestor(control_root)
    if os.name == "nt":
        return _windows_control_filesystem(ancestor)
    if sys.platform == "darwin":
        return _darwin_control_filesystem(ancestor)
    return _linux_control_filesystem(ancestor)


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    path: Path
    manifest_sha256: str


@dataclass(frozen=True)
class JournalRef:
    operation_id: str
    path: Path
    sequence: int
    record_sha256: str
    control_identity_sha256: str = ""


@dataclass(frozen=True)
class QuarantineRef:
    root: Path
    root_dev: int
    root_ino: int
    operation_dir: Path
    operation_dev: int
    operation_ino: int
    target: Path
    target_dev: int
    target_ino: int
    target_size: int
    target_sha256: str


@dataclass(frozen=True)
class OwnedFileToken:
    """Identity returned by the exclusive writer's still-open file handle."""

    device: int
    inode: int
    size: int


def _artifact_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:16]


def _artifact_root(control_root: Path, namespace: str) -> Path:
    root = _resolved_local_path(control_root / namespace, "transaction artifact")
    try:
        root.relative_to(control_root.resolve())
    except ValueError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "transaction artifact") from exc
    return root


def _ensure_artifact_root(control_root: Path, namespace: str) -> Path:
    root = _artifact_root(control_root, namespace)
    try:
        missing: list[Path] = []
        current = root
        while not os.path.lexists(current):
            if current == current.parent:
                raise ValueError
            missing.append(current)
            current = current.parent
        if _is_reparse_alias(current) or not current.is_dir():
            raise ValueError
        for created in reversed(missing):
            os.mkdir(created)
            _fsync_directory(created.parent)
        if _is_reparse_alias(root) or not root.is_dir():
            raise ValueError
        return root
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "transaction artifact") from exc


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record_hash(payload: dict[str, Any], omitted: str) -> str:
    copied = dict(payload)
    copied.pop(omitted, None)
    return hashlib.sha256(_canonical_bytes(copied)).hexdigest()


def _write_owned(path: Path, data: bytes) -> OwnedFileToken:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise ValueError
            return OwnedFileToken(observed.st_dev, observed.st_ino, observed.st_size)
    except (FileExistsError, OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "transaction artifact") from exc


def _owned_file_matches(path: Path, token: OwnedFileToken, *, links: int) -> bool:
    try:
        observed = os.lstat(path)
        return (not _is_reparse_alias(path) and stat.S_ISREG(observed.st_mode) and observed.st_nlink == links
                and observed.st_dev == token.device and observed.st_ino == token.inode
                and observed.st_size == token.size)
    except OSError:
        return False


def _unlink_owned_file(path: Path, token: OwnedFileToken, *, links: int) -> bool:
    if not _owned_file_matches(path, token, links=links):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _finalize_owned_directory(temporary: Path, final: Path) -> None:
    try:
        if final.exists() or os.stat(temporary).st_dev != os.stat(final.parent).st_dev:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "transaction artifact")
        _fsync_directory(temporary / "files")
        _fsync_directory(temporary)
        os.replace(temporary, final)
        _fsync_directory(final.parent)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "transaction artifact") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finalize_owned_file(temporary: Path, final: Path, token: OwnedFileToken) -> None:
    try:
        if (not _owned_file_matches(temporary, token, links=1)
                or token.device != os.stat(final.parent).st_dev or os.path.lexists(final)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal collision")
        # link creates the final name only if absent, so a collision cannot be replaced.
        os.link(temporary, final)
        published = os.lstat(final)
        if (_is_reparse_alias(final) or not stat.S_ISREG(published.st_mode)
                or published.st_dev != token.device or published.st_ino != token.inode
                or published.st_size != token.size or published.st_nlink != 2
                or not _owned_file_matches(temporary, token, links=2)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
        if not _unlink_owned_file(temporary, token, links=2):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
        if not _owned_file_matches(final, token, links=1):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
        _fsync_directory(final.parent)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal collision") from exc


def _tree_blob(repo: Path, sha: str, path: str) -> tuple[str, str, bytes] | None:
    listed = _git_bytes(repo, "ls-tree", "-z", sha, "--", path)
    if listed.returncode != 0:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot tree")
    if not listed.stdout:
        return None
    try:
        header, listed_path = listed.stdout.rstrip(b"\0").split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        if kind != "blob" or mode != "100644" or not SHA_RE.fullmatch(oid) or listed_path.decode("utf-8") != path:
            raise ValueError
        content = _git_bytes(repo, "show", f"{sha}:{path}")
        if content.returncode != 0:
            raise ValueError
        return mode, oid, content.stdout
    except (UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot tree") from exc


def create_canonical_snapshot(prepared: Prepared) -> SnapshotRef:
    context = prepared.context
    if context is None or context.layout != "canonical" or prepared.target_ref != CANONICAL_TARGET_REF:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot context")
    snapshot_id = _artifact_id()
    if not ARTIFACT_ID_RE.fullmatch(snapshot_id):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot id")
    root = _ensure_artifact_root(prepared.control_root, "snapshots")
    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=root))
        final = root / snapshot_id
        if final.exists():
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot collision")
        files: list[dict[str, Any]] = []
        for index, path in enumerate(prepared.changed_paths):
            normalized = _normal_git_path(path)
            if not normalized.startswith(context.state_prefix.rstrip("/") + "/"):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot path")
            before, after = _tree_blob(prepared.repo, prepared.expected_remote_sha, normalized), _tree_blob(prepared.repo, prepared.sha, normalized)
            entry: dict[str, Any] = {"path": normalized, "before": {"exists": False}, "after": {"exists": False}}
            for label, fact in (("before", before), ("after", after)):
                if fact is not None:
                    mode, oid, data = fact
                    name = f"files/{index:04d}-{label}.bin"
                    _write_owned(temporary / name, data)
                    entry[label] = {"exists": True, "mode": mode, "oid": oid, "sha256": hashlib.sha256(data).hexdigest(), "data": name}
            files.append(entry)
        local_input = None
        if prepared.operation == "publish":
            if prepared.source_content is None or hashlib.sha256(prepared.source_content).hexdigest() != prepared.input_digest_sha256:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot input")
            _write_owned(temporary / "local-input.bin", prepared.source_content)
            local_input = {"path_kind": "candidate", "sha256": prepared.input_digest_sha256, "data": "local-input.bin"}
        manifest = {"schema": SNAPSHOT_SCHEMA, "snapshot_id": snapshot_id, "operation": prepared.operation,
                    "candidate_id": prepared.candidate_id, "expected_remote_sha": prepared.expected_remote_sha,
                    "prepared_commit_sha": prepared.sha, "prepared_tree_oid": prepared.tree_oid,
                    "plan_hash": prepared.plan_hash, "input_digest_sha256": prepared.input_digest_sha256,
                    "binding_digest_sha256": prepared.binding_digest_sha256, "target_ref": prepared.target_ref,
                    "files": files, "local_input": local_input, "manifest_sha256": ""}
        manifest["manifest_sha256"] = _record_hash(manifest, "manifest_sha256")
        _write_owned(temporary / "manifest.json", _canonical_bytes(manifest))
        _finalize_owned_directory(temporary, final)
        return SnapshotRef(snapshot_id, final, manifest["manifest_sha256"])
    except ConfigError:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
        raise
    except (OSError, TypeError, ValueError) as exc:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot") from exc


def _read_snapshot(snapshot: SnapshotRef) -> dict[str, Any]:
    try:
        manifest_path = snapshot.path / "manifest.json"
        if (_is_reparse_alias(snapshot.path) or not snapshot.path.is_dir()
                or _is_reparse_alias(manifest_path) or not manifest_path.is_file()
                or manifest_path.stat().st_nlink != 1):
            raise ValueError
        payload = json.loads(manifest_path.read_bytes())
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot manifest") from exc
    if (not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA
            or payload.get("snapshot_id") != snapshot.snapshot_id
            or payload.get("manifest_sha256") != snapshot.manifest_sha256
            or _record_hash(payload, "manifest_sha256") != snapshot.manifest_sha256):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot manifest")
    return payload


def _safe_snapshot_data(snapshot: SnapshotRef, relative: Any) -> tuple[str, Path]:
    try:
        if not isinstance(relative, str) or not relative or "\\" in relative:
            raise ValueError
        logical = Path(relative)
        if logical.is_absolute() or ".." in logical.parts or logical.as_posix() != relative:
            raise ValueError
        target = snapshot.path / logical
        if _is_reparse_alias(target) or not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError
        return relative, target
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot material") from exc


def _validate_snapshot_material(snapshot: SnapshotRef) -> dict[str, Any]:
    """Validate recovery bytes without trusting a caller-supplied Prepared."""
    try:
        if not ARTIFACT_ID_RE.fullmatch(snapshot.snapshot_id) or _is_reparse_alias(snapshot.path) or not snapshot.path.is_dir():
            raise ValueError
        payload = _read_snapshot(snapshot)
        required = {"schema", "snapshot_id", "operation", "candidate_id", "expected_remote_sha", "prepared_commit_sha",
                    "prepared_tree_oid", "plan_hash", "input_digest_sha256", "binding_digest_sha256", "target_ref",
                    "files", "local_input", "manifest_sha256"}
        git_fields = ("expected_remote_sha", "prepared_commit_sha", "prepared_tree_oid")
        digest_fields = ("plan_hash", "input_digest_sha256", "binding_digest_sha256", "manifest_sha256")
        if any(not isinstance(payload.get(key), str) or not SHA_RE.fullmatch(payload[key]) for key in git_fields):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git SHA-1 object id required")
        if (set(payload) != required or payload.get("snapshot_id") != snapshot.snapshot_id
                or payload.get("schema") != SNAPSHOT_SCHEMA
                or payload.get("operation") not in {"publish", "promote", "advance", "rollback"}
                or payload.get("target_ref") != CANONICAL_TARGET_REF
                or any(not isinstance(payload.get(key), str) or not SHA256_RE.fullmatch(payload[key]) for key in digest_fields)
                or not isinstance(payload.get("files"), list)):
            raise ValueError
        expected_files = {"manifest.json"}
        seen_data: set[str] = set()
        logical_paths: set[str] = set()
        normalized_paths: set[str] = set()
        folded_paths: set[str] = set()
        for item in payload["files"]:
            if (not isinstance(item, dict) or set(item) != {"path", "before", "after"}
                    or not isinstance(item["path"], str) or _normal_git_path(item["path"]) != item["path"]
                    or not item["path"].startswith("state/")):
                raise ValueError
            normalized = normalize("NFC", item["path"])
            if (item["path"] in logical_paths or normalized in normalized_paths
                    or normalized.casefold() in folded_paths
                    or item["before"] == {"exists": False} and item["after"] == {"exists": False}):
                raise ValueError
            logical_paths.add(item["path"])
            normalized_paths.add(normalized)
            folded_paths.add(normalized.casefold())
            for fact in (item["before"], item["after"]):
                if fact == {"exists": False}:
                    continue
                if (not isinstance(fact, dict) or set(fact) != {"exists", "mode", "oid", "sha256", "data"}
                        or fact.get("exists") is not True or fact.get("mode") != "100644"
                        or not isinstance(fact.get("sha256"), str) or not SHA256_RE.fullmatch(fact["sha256"])):
                    raise ValueError
                if not isinstance(fact.get("oid"), str) or not SHA_RE.fullmatch(fact["oid"]):
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git SHA-1 object id required")
                name, data_path = _safe_snapshot_data(snapshot, fact.get("data"))
                if not name.startswith("files/") or name in seen_data:
                    raise ValueError
                data = data_path.read_bytes()
                blob_oid = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
                if hashlib.sha256(data).hexdigest() != fact["sha256"] or blob_oid != fact["oid"]:
                    raise ValueError
                seen_data.add(name)
                expected_files.add(name)
        local = payload["local_input"]
        if payload["operation"] == "publish":
            if not isinstance(local, dict) or set(local) != {"path_kind", "sha256", "data"} or local.get("path_kind") != "candidate" or local.get("data") != "local-input.bin" or not isinstance(local.get("sha256"), str) or not SHA256_RE.fullmatch(local["sha256"]):
                raise ValueError
            name, local_path = _safe_snapshot_data(snapshot, local["data"])
            if name in seen_data or hashlib.sha256(local_path.read_bytes()).hexdigest() != local["sha256"]:
                raise ValueError
            expected_files.add(name)
        elif local is not None:
            raise ValueError
        nodes = tuple(snapshot.path.rglob("*"))
        if any(_is_reparse_alias(path) for path in nodes):
            raise ValueError
        actual_files = {path.relative_to(snapshot.path).as_posix() for path in nodes if path.is_file()}
        actual_directories = {path.relative_to(snapshot.path).as_posix() for path in nodes if path.is_dir()}
        if actual_files != expected_files or actual_directories != {"files"}:
            raise ValueError
        return payload
    except ConfigError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot material") from exc


def _validate_snapshot(prepared: Prepared, snapshot: SnapshotRef) -> dict[str, Any]:
    context = prepared.context
    if context is None or context.layout != "canonical" or not ARTIFACT_ID_RE.fullmatch(snapshot.snapshot_id):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    root = _artifact_root(prepared.control_root, "snapshots")
    if snapshot.path != root / snapshot.snapshot_id or _is_reparse_alias(snapshot.path) or not snapshot.path.is_dir():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    payload = _validate_snapshot_material(snapshot)
    required = {"schema", "snapshot_id", "operation", "candidate_id", "expected_remote_sha", "prepared_commit_sha",
                "prepared_tree_oid", "plan_hash", "input_digest_sha256", "binding_digest_sha256", "target_ref",
                "files", "local_input", "manifest_sha256"}
    expected = {"operation": prepared.operation, "candidate_id": prepared.candidate_id,
                "expected_remote_sha": prepared.expected_remote_sha, "prepared_commit_sha": prepared.sha,
                "prepared_tree_oid": prepared.tree_oid, "plan_hash": prepared.plan_hash,
                "input_digest_sha256": prepared.input_digest_sha256, "binding_digest_sha256": prepared.binding_digest_sha256,
                "target_ref": prepared.target_ref}
    if set(payload) != required or any(payload.get(k) != v for k, v in expected.items()) or not isinstance(payload.get("files"), list):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    if len(payload["files"]) != len(prepared.changed_paths):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    try:
        for item, path in zip(payload["files"], prepared.changed_paths, strict=True):
            if not isinstance(item, dict) or item.get("path") != _normal_git_path(path):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
            for label, sha in (("before", prepared.expected_remote_sha), ("after", prepared.sha)):
                fact = item.get(label)
                tree = _tree_blob(prepared.repo, sha, item["path"])
                if tree is None:
                    if fact != {"exists": False}:
                        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
                    continue
                mode, oid, data = tree
                if not isinstance(fact, dict) or set(fact) != {"exists", "mode", "oid", "sha256", "data"} or fact["exists"] is not True or fact["mode"] != mode or fact["oid"] != oid or fact["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
                if not isinstance(fact.get("data"), str):
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
                data_path = snapshot.path / fact["data"]
                if Path(fact["data"]).is_absolute() or ".." in Path(fact["data"]).parts or _is_reparse_alias(data_path) or not data_path.is_file() or data_path.stat().st_nlink != 1 or data_path.read_bytes() != data:
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot") from exc
    local = payload["local_input"]
    if prepared.operation == "publish":
        input_path = snapshot.path / "local-input.bin"
        if not isinstance(local, dict) or local.get("path_kind") != "candidate" or local.get("sha256") != prepared.input_digest_sha256 or local.get("data") != "local-input.bin" or _is_reparse_alias(input_path) or not input_path.is_file() or input_path.stat().st_nlink != 1 or input_path.read_bytes() != prepared.source_content:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    elif local is not None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot")
    return payload


def _journal_record(payload: dict[str, Any]) -> dict[str, Any]:
    record = dict(payload)
    record["record_sha256"] = _record_hash(record, "record_sha256")
    return record


def _read_journal(path: Path, journal_root: Path) -> tuple[dict[str, Any], ...]:
    try:
        if (_is_reparse_alias(journal_root) or not journal_root.is_dir()
                or path != journal_root / f"{path.stem}.jsonl"):
            raise ValueError
        with _locked_journal(path) as stream:
            stream.seek(0)
            return _parse_journal_bytes(stream.read(), path)
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal") from exc


def _read_regular_bytes_readonly(path: Path, label: str) -> bytes:
    try:
        before = os.lstat(path)
        if _is_reparse_alias(path) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError
        raw = path.read_bytes()
        after = os.lstat(path)
        if (after.st_dev != before.st_dev or after.st_ino != before.st_ino
                or after.st_size != before.st_size or after.st_nlink != 1):
            raise ValueError
        return raw
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", label) from exc


def _read_journal_readonly(path: Path, journal_root: Path) -> tuple[dict[str, Any], ...]:
    """Parse one immutable journal without taking a writable transaction lock."""
    try:
        if (_is_reparse_alias(journal_root) or not journal_root.is_dir()
                or path != journal_root / f"{path.stem}.jsonl"):
            raise ValueError
        raw = _read_regular_bytes_readonly(path, "journal")
        return _parse_journal_bytes(raw, path)
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal") from exc


def _parse_journal_bytes(raw: bytes, path: Path) -> tuple[dict[str, Any], ...]:
    try:
        if not raw or not raw.endswith(b"\n"):
            raise ValueError
        records = tuple(json.loads(line) for line in raw.decode("utf-8").splitlines())
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal") from exc
    if not ARTIFACT_ID_RE.fullmatch(path.stem) or path.suffix != ".jsonl":
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
    previous = None
    for sequence, record in enumerate(records):
        if (not isinstance(record, dict) or record.get("schema") != JOURNAL_SCHEMA
                or type(record.get("sequence")) is not int or record.get("sequence") != sequence or record.get("previous_record_sha256") != previous
                or not isinstance(record.get("utc"), str) or not UTC_RE.fullmatch(record["utc"])
                or not isinstance(record.get("record_sha256"), str) or not SHA256_RE.fullmatch(record["record_sha256"])
                or record.get("record_sha256") != _record_hash(record, "record_sha256")):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
        if sequence == 0:
            baseline_keys = {"schema", "record_type", "sequence", "operation_id", "operation", "candidate_id", "input_digest_sha256", "plan_hash", "binding_digest_sha256", "lineage_root_sha", "control_identity_sha256", "control_filesystem_sha256", "target_ref", "expected_remote_sha", "prepared_commit_sha", "prepared_tree_oid", "snapshot_id", "snapshot_manifest_sha256", "utc", "previous_record_sha256", "record_sha256"}
            git_fields = ("lineage_root_sha", "expected_remote_sha", "prepared_commit_sha", "prepared_tree_oid")
            digest_fields = ("input_digest_sha256", "plan_hash", "binding_digest_sha256", "control_identity_sha256", "control_filesystem_sha256", "snapshot_manifest_sha256")
            if any(not isinstance(record.get(key), str) or not SHA_RE.fullmatch(record[key]) for key in git_fields):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git SHA-1 object id required")
            if (set(record) != baseline_keys or record.get("record_type") != "baseline" or record.get("operation_id") != path.stem or record.get("operation") not in {"publish", "promote", "advance", "rollback"} or not ARTIFACT_ID_RE.fullmatch(str(record.get("operation_id"))) or not ARTIFACT_ID_RE.fullmatch(str(record.get("snapshot_id"))) or record.get("target_ref") != CANONICAL_TARGET_REF or any(not isinstance(record.get(key), str) or not SHA_RE.fullmatch(record[key]) for key in git_fields) or any(not isinstance(record.get(key), str) or not SHA256_RE.fullmatch(record[key]) for key in digest_fields)):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
            candidate = record.get("candidate_id")
            operation = record["operation"]
            valid_candidate = (operation in {"publish", "promote"} and isinstance(candidate, str) and CANDIDATE_ID_RE.fullmatch(candidate)) or (operation == "advance" and isinstance(candidate, str) and ADVANCE_LESSON_ID_RE.fullmatch(candidate)) or (operation == "rollback" and isinstance(candidate, str) and (ROLLBACK_ID_RE.fullmatch(candidate) or ARTIFACT_ID_RE.fullmatch(candidate)))
            if not valid_candidate:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
        else:
            if set(record) != {"schema", "record_type", "sequence", "event", "utc", "details", "previous_record_sha256", "record_sha256"} or record.get("record_type") != "event" or record.get("event") not in JOURNAL_EVENTS or not _journal_details(record.get("details"), record["event"]):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
        previous = record["record_sha256"]
    if not records or records[0].get("record_type") != "baseline":
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
    _validate_journal_terminal_events(records)
    return records


def _validate_journal_terminal_events(records: tuple[dict[str, Any], ...]) -> None:
    """Reject terminal journal transitions that could rewrite completed truth."""
    completed = False
    cleanup_pending = False
    for record in records[1:]:
        event = record.get("event")
        if event == "completed":
            if completed:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal terminal")
            completed = True
        elif event == "cleanup_pending":
            if not completed or cleanup_pending:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal terminal")
            cleanup_pending = True
        elif completed:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal terminal")


def _journal_details(value: Any, event: str) -> bool:
    if not isinstance(value, dict):
        return False
    if event == "cleanup_pending":
        return (set(value) == {"phase", "kind"}
                and value.get("kind") in {"quarantine", "worktree"}
                and value.get("phase") == value.get("kind"))
    if "kind" in value:
        return False
    has_site = "site" in value
    has_reason = "reason" in value
    if has_site != has_reason or (has_site and event != "failed"):
        return False
    for key, item in value.items():
        if key not in {"phase", "status", "sha", "code", "ok", "site", "reason"}:
            return False
        if key == "site":
            if item not in {"preflight", "under_lock"}:
                return False
            continue
        if key == "reason":
            if item not in {"git", "os", "timeout", "unicode", "validation", "unexpected"}:
                return False
            continue
        if isinstance(item, bool):
            continue
        if not isinstance(item, str) or "/" in item or "\\" in item or ".." in item or ":" in item:
            return False
        if key == "sha" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item):
            return False
        if key != "sha" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item):
            return False
    return True


def create_canonical_journal(prepared: Prepared, snapshot: SnapshotRef) -> JournalRef:
    _validate_snapshot(prepared, snapshot)
    operation_id = _artifact_id()
    if not ARTIFACT_ID_RE.fullmatch(operation_id):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal id")
    root = _ensure_artifact_root(prepared.control_root, "journal")
    final = root / f"{operation_id}.jsonl"
    temporary = root / f".{operation_id}.tmp"
    if final.exists() or temporary.exists() or os.stat(root).st_dev != os.stat(final.parent).st_dev:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal collision")
    binding = dict(prepared.binding or ())
    baseline = _journal_record({
        "schema": JOURNAL_SCHEMA, "record_type": "baseline", "sequence": 0,
        "operation_id": operation_id, "operation": prepared.operation, "candidate_id": prepared.candidate_id,
        "input_digest_sha256": prepared.input_digest_sha256, "plan_hash": prepared.plan_hash,
        "binding_digest_sha256": prepared.binding_digest_sha256,
        "lineage_root_sha": binding.get("repository_root_sha", ""),
        "control_identity_sha256": hashlib.sha256(binding.get("control_identity", "").encode()).hexdigest(),
        "control_filesystem_sha256": binding.get("control_filesystem_sha256", ""),
        "target_ref": prepared.target_ref, "expected_remote_sha": prepared.expected_remote_sha,
        "prepared_commit_sha": prepared.sha, "prepared_tree_oid": prepared.tree_oid,
        "snapshot_id": snapshot.snapshot_id, "snapshot_manifest_sha256": snapshot.manifest_sha256,
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(), "previous_record_sha256": None,
    })
    temporary_token = _write_owned(temporary, _canonical_bytes(baseline) + b"\n")
    _finalize_owned_file(temporary, final, temporary_token)
    return JournalRef(operation_id, final, 0, baseline["record_sha256"], baseline["control_identity_sha256"])


@contextmanager
def _locked_journal(path: Path):
    try:
        observed = os.lstat(path)
        if (_is_reparse_alias(path) or not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
        with path.open("r+b") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_nlink != 1 or before.st_dev != observed.st_dev
                    or before.st_ino != observed.st_ino):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                after = os.fstat(stream.fileno())
                latest = os.lstat(path)
                if (after.st_nlink != 1 or after.st_dev != observed.st_dev or after.st_ino != observed.st_ino
                        or latest.st_dev != observed.st_dev or latest.st_ino != observed.st_ino):
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
                yield stream
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except ConfigError:
        raise
    except (OSError, ImportError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal lock") from exc


def append_journal_event(journal: JournalRef, event: str, details: dict[str, str] | None = None,
                         *, prepared: Prepared | None = None) -> JournalRef:
    if event not in JOURNAL_EVENTS or not _journal_details(details or {}, event):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal event")
    if prepared is None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
    binding = dict(prepared.binding or ())
    root = _artifact_root(prepared.control_root, "journal")
    if journal.path != root / f"{journal.operation_id}.jsonl" or journal.control_identity_sha256 != hashlib.sha256(binding.get("control_identity", "").encode()).hexdigest():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
    if _is_reparse_alias(journal.path) or not journal.path.is_file() or journal.path.stat().st_nlink != 1:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal identity")
    with _locked_journal(journal.path) as stream:
        stream.seek(0)
        raw = stream.read()
        if not raw.endswith(b"\n"):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal")
        records = _parse_journal_bytes(raw, journal.path)
        if records[-1]["record_sha256"] != journal.record_sha256 or records[-1]["sequence"] != journal.sequence:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal baseline")
        _validate_journal_terminal_events(records + ({"event": event},))
        record = _journal_record({"schema": JOURNAL_SCHEMA, "record_type": "event", "sequence": journal.sequence + 1,
                                  "event": event, "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                  "details": details or {}, "previous_record_sha256": journal.record_sha256})
        try:
            stream.seek(0, os.SEEK_END)
            stream.write(_canonical_bytes(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        except OSError as exc:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "journal") from exc
    return JournalRef(journal.operation_id, journal.path, record["sequence"], record["record_sha256"], journal.control_identity_sha256)


def _orphan_snapshot_ids(control_root: Path) -> tuple[str, ...]:
    snapshots = _artifact_root(control_root, "snapshots")
    journals = _artifact_root(control_root, "journal")
    if snapshots.exists() and (_is_reparse_alias(snapshots) or not snapshots.is_dir()):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
    snapshot_map: dict[str, dict[str, Any]] = {}
    if snapshots.exists():
        for path in snapshots.iterdir():
            try:
                if _is_reparse_alias(path) or not path.is_dir() or not ARTIFACT_ID_RE.fullmatch(path.name):
                    raise ValueError
                raw = json.loads((path / "manifest.json").read_bytes())
                ref = SnapshotRef(path.name, path, str(raw.get("manifest_sha256", "")))
                snapshot_map[path.name] = _validate_snapshot_material(ref)
            except (ConfigError, OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status") from exc
    referenced: set[str] = set()
    if journals.exists():
        if _is_reparse_alias(journals) or not journals.is_dir():
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
        for path in journals.iterdir():
            if _is_reparse_alias(path) or not path.is_file() or not ARTIFACT_ID_RE.fullmatch(path.stem) or path.suffix != ".jsonl":
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
            baseline = _read_journal(path, journals)[0]
            pair = str(baseline.get("snapshot_id", "")) + ":" + str(baseline.get("snapshot_manifest_sha256", ""))
            manifest = snapshot_map.get(str(baseline.get("snapshot_id", "")))
            shared = {"operation": "operation", "candidate_id": "candidate_id", "input_digest_sha256": "input_digest_sha256", "plan_hash": "plan_hash", "binding_digest_sha256": "binding_digest_sha256", "expected_remote_sha": "expected_remote_sha", "prepared_commit_sha": "prepared_commit_sha", "prepared_tree_oid": "prepared_tree_oid", "target_ref": "target_ref"}
            if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != baseline.get("snapshot_manifest_sha256") or any(baseline.get(left) != manifest.get(right) for left, right in shared.items()) or pair in referenced:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
            referenced.add(pair)
    if not snapshots.exists():
        if referenced:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
        return ()
    found: list[str] = []
    for path in snapshots.iterdir():
        if _is_reparse_alias(path) or not path.is_dir() or not ARTIFACT_ID_RE.fullmatch(path.name):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
        manifest = snapshot_map.get(path.name)
        if not isinstance(manifest, dict):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "snapshot status")
        if path.name + ":" + str(manifest.get("manifest_sha256", "")) not in referenced:
            found.append(path.name)
    return tuple(sorted(found))


def _quarantine_root(repo: Path) -> Path:
    """Return the dedicated, non-alias Git metadata quarantine namespace."""
    try:
        git_dir = repo.resolve() / ".git"
        if _is_reparse_alias(git_dir) or not git_dir.is_dir():
            raise ValueError
        root = git_dir / "agent-core-quarantine"
        if os.path.lexists(root) and (_is_reparse_alias(root) or not root.is_dir()):
            raise ValueError
        return root
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "quarantine") from None


def _quarantine_pending_ids(repo: Path) -> tuple[str, ...]:
    root = _quarantine_root(repo)
    if not os.path.lexists(root):
        return ()
    try:
        pending: list[str] = []
        for entry in root.iterdir():
            if (_is_reparse_alias(entry) or not entry.is_dir()
                    or not ARTIFACT_ID_RE.fullmatch(entry.name)):
                raise ValueError
            pending.append(entry.name)
        return tuple(sorted(pending))
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "quarantine status") from None


def _canonical_binding(context: RepositoryContext, supplied_control: Path | None,
                       config_path: Path | None, operation: str, *, fetch: bool = True) -> tuple[Path, dict[str, str], str]:
    if config_path is None:
        raise ConfigError("FAIL_STATE_BINDING", "canonical transaction requires --config")
    control_root = _canonical_control_root(context, supplied_control)
    control_filesystem_sha256 = _control_filesystem_sha256(control_root)
    freshness = require_fresh(context.state_root, operation, control_root, fetch=fetch)
    evidence = validate_state_binding(context.state_root, config_path, require_clean_snapshot=False)
    remote = freshness.remote or ""
    if not remote or remote != evidence.remote_revision:
        raise ConfigError("FAIL_STATE_BINDING", "canonical remote identity changed")
    binding = {
        "config_path": str(Path(config_path).resolve()),
        "receipt_path": str(evidence.receipt_path),
        "schema": evidence.schema,
        "config_sha256": evidence.config_sha256,
        "receipt_sha256": evidence.receipt_sha256,
        "remote_url_sha256": evidence.remote_url_sha256,
        "remote_revision": evidence.remote_revision,
        "repository_root_sha": evidence.repository_root_sha or "",
        "repository_root": str(context.repo_root),
        "engine_provenance_sha256": evidence.engine_provenance_sha256 or "",
        "state_lock_sha256": evidence.state_lock_sha256,
        "control_identity": str(control_root),
        "control_filesystem_sha256": control_filesystem_sha256,
    }
    return control_root, binding, remote


def _transaction_plan_context(context: RepositoryContext, supplied_control: Path | None,
                              config_path: Path | None, operation: str) -> tuple[Path, dict[str, str] | None, str]:
    if context.layout != "canonical":
        if supplied_control is None:
            return (Path.home() / ".agent-core").resolve(), None, ""
        return supplied_control.resolve(), None, ""
    return _canonical_binding(context, supplied_control, config_path, operation)


def _revalidate_canonical_plan(context: RepositoryContext, supplied_control: Path | None,
                               config_path: Path | None, plan: Plan, operation: str) -> tuple[Path, dict[str, str] | None]:
    if context.layout != "canonical":
        return _transaction_plan_context(context, supplied_control, config_path, operation)[:2]
    control_root, binding, remote = _canonical_binding(context, supplied_control, config_path, operation)
    if remote != plan.expected_remote_sha or binding != plan.payload.get("binding"):
        raise ConfigError("FAIL_PLAN_HASH", "canonical transaction identity changed")
    return control_root, binding


def candidate_id(host: str, created: dt.datetime, value: uuid.UUID) -> str:
    """Render the stable full-UUID candidate id contract."""
    return f"{host}-{created.strftime('%Y%m%dT%H%M%SZ')}-{value.hex}"


def create_candidate(
    repo: Path,
    control_root: Path,
    *,
    host: str,
    agent: str,
    rule: str,
    trigger: str,
    cost: str,
    sink: str,
    scope_hint: str,
    evidence: str,
    when: str | None = None,
    base_revision: str | None = None,
    inbox_path: Path | None = None,
    require_state_freshness: bool = True,
    allow_project: bool = False,
) -> Path:
    """Create one schema-valid candidate with exclusive, durable file creation."""
    if not HOST_LABEL_RE.fullmatch(host):
        raise ConfigError("FAIL_CANDIDATE", "host must be a privacy-safe kebab-case label")
    values = (agent, rule, trigger, cost, sink, scope_hint, evidence)
    if any(not isinstance(value, str) or not value for value in values):
        raise ConfigError("FAIL_CANDIDATE", "candidate fields must be non-empty strings")
    if require_state_freshness:
        state = require_fresh(repo, "capture", control_root)
        if base_revision is None:
            base_revision = state.remote or f"{state.head} unverified"
    elif base_revision is None:
        raise ConfigError("FAIL_CANDIDATE", "base_revision is required without state freshness")
    created = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    created_text = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    inbox = inbox_path if inbox_path is not None else repo / INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        item_id = candidate_id(host, created, uuid.uuid4())
        path = inbox / f"{item_id}.md"
        payload = {
            "schema": "candidate/2" if when is not None else "candidate/1",
            "id": item_id, "created_utc": created_text,
            "host": host, "agent": agent, "base_revision": base_revision,
            "rule": rule, "trigger": trigger, "cost": cost, "sink": sink,
            "scope_hint": scope_hint, "evidence": evidence,
        }
        if when is not None:
            payload["when"] = when
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            load_candidate(path, allow_project=allow_project)
            return path
        except FileExistsError:
            continue
        except Exception:
            path.unlink(missing_ok=True)
            raise
    raise ConfigError("FAIL_CANDIDATE_COLLISION", host)


def _plan(operation: str, candidate_id: str, expected: str, **values: Any) -> Plan:
    payload = {"operation": operation, "candidate_id": candidate_id,
               "expected_remote_sha": expected, **values, "target_ref": CANONICAL_TARGET_REF}
    digest = _canonical_hash(payload)
    lines = (f"PLAN operation={operation} candidate={candidate_id}",
             f"EXPECTED_REMOTE_SHA {expected}", f"PLAN_HASH {digest}")
    binding = values.get("binding")
    if isinstance(binding, dict):
        warnings = (
            tuple(f"ORPHAN_SNAPSHOT {item}" for item in _orphan_snapshot_ids(Path(binding["control_identity"])))
            + tuple(f"QUARANTINE_PENDING {item}" for item in _quarantine_pending_ids(
                Path(binding.get("repository_root", "."))))
        )
        lines = warnings + lines
    return Plan(operation, candidate_id, expected, digest, lines, payload)


def plan_publish(repo: Path, control_root: Path | None, candidate_id: str,
                 *, config_path: Path | None = None) -> Plan:
    context = resolve_repository_context(repo)
    resolved_control, binding, remote = _transaction_plan_context(
        context, control_root, config_path, "publish")
    source = _state_candidate_path(context, candidate_id)
    content = source.read_bytes()
    load_candidate(source)
    state = require_fresh(repo, "publish", resolved_control) if binding is None else None
    return _plan(
        "publish", candidate_id, remote or (state.remote or ""), source_sha256=hashlib.sha256(content).hexdigest(),
        **_plan_context(context, binding),
    )


def _already_promoted(ledger_text: str, candidate_id: str) -> str | None:
    for line in ledger_text.splitlines():
        if f"from: {candidate_id}" not in line:
            continue
        match = ledger.ENTRY_RE.match(line)
        if match:
            return match.group(1) or match.group(2)
    return None


def _find_promoted(context: RepositoryContext, candidate_id: str) -> str | None:
    root = _safe_state_path(context, Path("experience"), required="dir")
    def reject_walk(error: OSError) -> None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "experience") from error

    for directory, names, files in os.walk(root, followlinks=False, onerror=reject_walk):
        checked = Path(directory)
        if _is_reparse_alias(checked):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "experience")
        for name in names:
            if _is_reparse_alias(checked / name):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "experience")
        if "LESSONS.md" in files:
            relative = (checked / "LESSONS.md").relative_to(context.state_root)
            path = _safe_state_path(context, relative, required="file")
            found = _already_promoted(path.read_text(encoding="utf-8"), candidate_id)
            if found:
                return found
    return None


def _ledger_target(
    item: dict[str, str], *, project_root: Path | None = None,
) -> tuple[Path, str, str, str]:
    hint = item["scope_hint"]
    if hint == "global":
        return GLOBAL_LEDGER, "global", ledger.SCOPE_LABEL["global"], "L"
    if hint.startswith("project:"):
        project_id = hint.removeprefix("project:")
        if project_root is None:
            raise ConfigError("FAIL_PROJECT_CONTEXT", project_id)
        if not ledger.PROFILE_RE.fullmatch(project_id):
            raise ConfigError("FAIL_CANDIDATE", f"invalid project scope_hint: {hint}")
        prefix = project_id.split("-", 1)[0].upper()
        return PROJECT_LEDGER, "project", ledger.SCOPE_LABEL["project"], prefix
    profile = hint.removeprefix("profile:")
    if not ledger.PROFILE_RE.fullmatch(profile):
        raise ConfigError("FAIL_CANDIDATE", f"invalid profile scope_hint: {hint}")
    prefix = profile.split("-", 1)[0].upper()
    return (Path("experience/profiles") / profile / "LESSONS.md", "profile",
            ledger.SCOPE_LABEL["profile"], prefix)


def _next_id(ledger_text: str, scope: str, prefix: str) -> str:
    ids, errors, _warnings = ledger.parse_ledger(ledger_text, scope, "LESSONS.md")
    if errors:
        raise ConfigError("FAIL_LEDGER", "; ".join(errors))
    pattern = re.compile(rf"{re.escape(prefix)}-(\d+)")
    candidates = set(ids)
    if scope == "global":
        candidates |= set(ledger.MOVED_ID_RE.findall(ledger_text))
    numbers = [int(match.group(1)) for item in candidates if (match := pattern.fullmatch(item))]
    return f"{prefix}-{max(numbers, default=0) + 1}"


def _similarities(ledger_text: str, rule: str) -> list[tuple[str, float]]:
    candidate_tokens = set(tokenize(rule))
    if not candidate_tokens:
        return []
    hits = []
    for lesson_item in parse_markdown(ledger_text, "global", "LESSONS.md"):
        lesson_tokens = set(tokenize(lesson_item.rule))
        union = candidate_tokens | lesson_tokens
        score = len(candidate_tokens & lesson_tokens) / len(union) if union else 0.0
        if score >= 0.6:
            hits.append((lesson_item.lesson_id, score))
    return sorted(hits, key=lambda item: (-item[1], item[0]))


def _review_similarities(
    similarities: list[tuple[str, float]], *, supersedes: str | None, force_new: bool,
) -> None:
    if similarities and not (supersedes or force_new):
        detail = ",".join(f"{item_id}:{score:.3f}" for item_id, score in similarities)
        raise ConfigError("FAIL_SIMILAR_REVIEW", detail)
    if supersedes and supersedes not in {item_id for item_id, _score in similarities}:
        raise ConfigError("FAIL_SUPERSEDES", supersedes)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def plan_promote(
    repo: Path,
    control_root: Path | None,
    candidate_id: str,
    *,
    reviewed_against: str | None = None,
    supersedes: str | None = None,
    force_new: bool = False,
    config_path: Path | None = None,
) -> Plan:
    context = resolve_repository_context(repo)
    resolved_control, binding, bound_remote = _transaction_plan_context(
        context, control_root, config_path, "promote")
    source = _state_candidate_path(context, candidate_id, include_consumed=True)
    item = load_candidate(source)
    ledger_relative, scope, label, prefix = _ledger_target(item)
    ledger_path = _safe_state_path(context, ledger_relative, required="file")
    previous = _find_promoted(context, candidate_id)
    if previous:
        raise ConfigError("FAIL_ALREADY_PROMOTED", previous)
    ledger_text = ledger_path.read_text(encoding="utf-8")
    state = require_fresh(repo, "promote", resolved_control) if binding is None else None
    base = item["base_revision"].removesuffix(" unverified")
    remote = bound_remote or (state.remote or "")
    if not _is_ancestor(context.repo_root, base, remote):
        raise ConfigError("FAIL_STALE_BASE", f"base={base} current={remote}")
    if base != remote and reviewed_against != remote:
        raise ConfigError("REVIEW_REQUIRED", f"base={base} current={remote}")
    lesson_id = _next_id(ledger_text, scope, prefix)
    similarities = _similarities(ledger_text, item["rule"])
    _review_similarities(similarities, supersedes=supersedes, force_new=force_new)
    candidate_git_path = context.state_to_repo_path(INBOX / source.name)
    remote_blob = _git_bytes(context.repo_root, "show", f"origin/main:{candidate_git_path}")
    if remote_blob.returncode != 0:
        raise ConfigError("FAIL_CANDIDATE_UNPUBLISHED", candidate_id)
    plan = _plan(
        "promote", candidate_id, remote, candidate_sha256=hashlib.sha256(remote_blob.stdout).hexdigest(),
        lesson_id=lesson_id, reviewed_against=reviewed_against, supersedes=supersedes,
        force_new=force_new, base_revision=base, ledger_path=ledger_relative.as_posix(),
        scope=scope, label=label, **_plan_context(context, binding),
    )
    extra = tuple(f"SIMILAR {item_id} {score:.3f}" for item_id, score in similarities)
    if base != remote:
        diff = _git(context.repo_root, "diff", "--stat", base, remote).stdout.strip().replace("\n", " | ")
        extra += (f"BASE_DIFF {diff or 'none'}",)
    return replace(plan, lines=extra + plan.lines)


def _checked_receipt(repo: Path, lesson_id: str, receipt_path: Path, expected_hash: str | None = None):
    from .retire import find_lesson, verify_receipt

    record = find_lesson(repo, lesson_id)
    check = verify_receipt(
        repo, receipt_path, expected_lesson_id=lesson_id,
        expected_hash=expected_hash or receipt_path.stem,
    )
    if not check.fresh:
        code = "FAIL_EVIDENCE_HASH" if check.reason.startswith("receipt_hash") else "FAIL_STALE_EVIDENCE"
        raise ConfigError(code, check.reason)
    if record.status != check.payload["from_status"]:
        raise ConfigError(
            "FAIL_EVIDENCE_STATUS",
            f"lesson={record.status} receipt={check.payload['from_status']}",
        )
    if record.verifier_id != check.payload["verifier_id"]:
        raise ConfigError("FAIL_EVIDENCE_VERIFIER", lesson_id)
    return record, check


def plan_advance(repo: Path, control_root: Path | None, lesson_id: str, receipt_path: Path,
                 *, config_path: Path | None = None) -> Plan:
    context = resolve_repository_context(repo)
    resolved_control, binding, bound_remote = _transaction_plan_context(
        context, control_root, config_path, "advance")
    state = require_fresh(repo, "advance", resolved_control) if binding is None else None
    record, check = _checked_receipt(repo, lesson_id, receipt_path)
    return _plan(
        "advance", lesson_id, bound_remote or (state.remote or ""), lesson_id=lesson_id,
        ledger_path=record.ledger_path.relative_to(context.state_root).as_posix(),
        from_status=check.payload["from_status"], to_status=check.payload["to_status"],
        verifier_id=check.payload["verifier_id"], verified_utc=check.payload["verified_utc"],
        receipt_sha256=check.sha256, **_plan_context(context, binding),
    )


def assert_txn_path(control_root: Path, txn: Path) -> Path:
    root = (control_root.resolve() / "txn").resolve()
    resolved = txn.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ConfigError("FAIL_TXN_PATH", str(resolved)) from exc
    return resolved


def _new_worktree(repo: Path, control_root: Path, expected: str) -> Path:
    root = control_root.resolve() / "txn"
    root.mkdir(parents=True, exist_ok=True)
    txn = Path(tempfile.mkdtemp(prefix="txn-", dir=root))
    assert_txn_path(control_root, txn)
    try:
        _git(repo, "worktree", "add", "--quiet", "--detach", str(txn), expected)
        return txn
    except Exception:
        shutil.rmtree(txn, ignore_errors=True)
        _git(repo, "worktree", "prune", check=False)
        raise


def _normal_git_path(path: str) -> str:
    value = normalize("NFC", path)
    candidate = Path(value)
    if (
        not value or "\\" in value or candidate.is_absolute() or ".." in candidate.parts
        or candidate.as_posix() != value
    ):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", path)
    return value


def _validate_changed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_normal_git_path(path) for path in paths)
    if len(set(normalized)) != len(normalized) or len({path.casefold() for path in normalized}) != len(normalized):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "path normalization collision")
    return normalized


def _parse_name_status(result: subprocess.CompletedProcess[bytes]) -> tuple[tuple[str, str], ...]:
    if result.returncode != 0:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git name-status failed")
    raw = result.stdout
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "unterminated Git name-status")
    fields = raw[:-1].split(b"\0")
    if len(fields) % 2:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "malformed Git name-status")
    parsed: list[tuple[str, str]] = []
    for status, path in zip(fields[::2], fields[1::2]):
        try:
            status_text = status.decode("ascii")
            path_text = path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "non-UTF-8 Git path") from exc
        if status_text not in {"A", "M", "D", "T"}:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "unexpected Git status")
        parsed.append((status_text, _normal_git_path(path_text)))
    return tuple(parsed)


def _cached_changed_paths(txn: Path) -> tuple[tuple[str, str], ...]:
    return _parse_name_status(_git_bytes(txn, "diff", "--cached", "--no-renames", "--name-status", "-z"))


def _tree_entry(txn: Path, sha: str, path: str) -> tuple[str, str, str] | None:
    result = _git_bytes(txn, "ls-tree", "-z", sha, "--", path)
    if result.returncode != 0:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git tree entry failed")
    if not result.stdout:
        return None
    if not result.stdout.endswith(b"\0"):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "malformed Git tree entry")
    fields = result.stdout[:-1].split(b"\t")
    if len(fields) != 2:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "malformed Git tree entry")
    try:
        mode, kind, oid = fields[0].decode("ascii").split()
        entry_path = fields[1].decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "malformed Git tree entry") from exc
    if (_normal_git_path(entry_path) != path or kind != "blob" or mode not in {"100644", "100755"}
            or not re.fullmatch(r"[0-9a-f]{40,64}", oid)):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", f"invalid committed tree entry: {path}")
    return mode, kind, oid


def _index_entry(txn: Path, path: str) -> tuple[str, str]:
    result = _git_bytes(txn, "ls-files", "-s", "-z", "--", path)
    if result.returncode != 0 or not result.stdout or not result.stdout.endswith(b"\0"):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", f"missing index entry: {path}")
    fields = result.stdout[:-1].split(b"\t")
    if len(fields) != 2:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", f"malformed index entry: {path}")
    try:
        mode, oid, stage = fields[0].decode("ascii").split()
        index_path = fields[1].decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", f"malformed index entry: {path}") from exc
    if (stage != "0" or _normal_git_path(index_path) != path or mode not in {"100644", "100755"}
            or not re.fullmatch(r"[0-9a-f]{40,64}", oid)):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", f"invalid index entry: {path}")
    return mode, oid


def _assert_cached_scope(txn: Path, expected_paths: tuple[str, ...]) -> None:
    expected = _validate_changed_paths(expected_paths)
    actual = _cached_changed_paths(txn)
    actual_paths = tuple(path for _status, path in actual)
    if set(actual_paths) != set(expected) or len(actual_paths) != len(expected):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "cached paths differ from operation allowlist")
    for status, path in actual:
        if status == "D":
            continue
        _index_entry(txn, path)


def _validate_expectations(expected: tuple[ChangeExpectation, ...]) -> tuple[ChangeExpectation, ...]:
    if not expected:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "missing transaction allowlist")
    normalized = tuple(ChangeExpectation(item.status, _normal_git_path(item.path), item.mode) for item in expected)
    if any(item.status not in {"A", "M", "D", "T"} for item in normalized):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "unexpected transaction status")
    if len({item.path for item in normalized}) != len(normalized):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "duplicate transaction path")
    if any(item.mode not in {None, "100644", "100755"} for item in normalized):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "invalid expected mode")
    _validate_changed_paths(tuple(item.path for item in normalized))
    return normalized


def _capture_precommit_facts(txn: Path, expected: str,
                             expected_changes: tuple[ChangeExpectation, ...]) -> tuple[ChangeFact, ...]:
    expectations = _validate_expectations(expected_changes)
    actual = _cached_changed_paths(txn)
    pairs = tuple((item.status, item.path) for item in expectations)
    if set(actual) != set(pairs) or len(actual) != len(pairs):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "cached facts differ from transaction allowlist")
    facts: list[ChangeFact] = []
    for item in expectations:
        if item.status == "D":
            parent = _tree_entry(txn, expected, item.path)
            if parent is None:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", f"missing deletion parent: {item.path}")
            mode, _kind, oid = parent
            if item.mode is not None and mode != item.mode:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", f"unexpected deletion mode: {item.path}")
            facts.append(ChangeFact(item.status, item.path, mode, oid))
            continue
        mode, oid = _index_entry(txn, item.path)
        if item.mode is not None and mode != item.mode:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", f"unexpected index mode: {item.path}")
        facts.append(ChangeFact(item.status, item.path, mode, oid))
    return tuple(facts)


def _assert_blob_moves(facts: tuple[ChangeFact, ...], moves: tuple[BlobMove, ...]) -> None:
    if not all(isinstance(move, BlobMove) for move in moves) or len(set(moves)) != len(moves):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "duplicate blob move")
    by_path = {fact.path: fact for fact in facts}
    endpoints: set[str] = set()
    for move in moves:
        source = _normal_git_path(move.source_path)
        destination = _normal_git_path(move.destination_path)
        if source == destination or source in endpoints or destination in endpoints:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "self-referential blob move")
        endpoints.update((source, destination))
        source_fact = by_path.get(source)
        destination_fact = by_path.get(destination)
        if (
            source_fact is None or destination_fact is None
            or source_fact.status != "D" or destination_fact.status != "A"
            or source_fact.mode != "100644" or destination_fact.mode != "100644"
            or source_fact.oid != destination_fact.oid
        ):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "invalid blob move")


def _assert_worktree_paths(txn: Path, expected_paths: tuple[str, ...], context: RepositoryContext) -> None:
    txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
    for path_text in _validate_changed_paths(expected_paths):
        relative = txn_context.repo_to_state_path(path_text)
        if relative is None:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", f"outside state subtree: {path_text}")
        _safe_state_path(txn_context, Path(relative))


def _assert_engine_tree(txn: Path, expected: str, sha: str, context: RepositoryContext) -> None:
    if context.layout != "canonical":
        return
    before = _git(txn, "rev-parse", f"{expected}:engine", check=False)
    after = _git(txn, "rev-parse", f"{sha}:engine", check=False)
    if before.returncode != 0 or after.returncode != 0 or before.stdout.strip() != after.stdout.strip():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "engine tree changed or missing")


def _assert_engine_tree_unchanged(repo: Path, expected: str, sha: str) -> None:
    before = _git(repo, "rev-parse", f"{expected}:engine", check=False)
    after = _git(repo, "rev-parse", f"{sha}:engine", check=False)
    if before.returncode != 0 or after.returncode != 0 or before.stdout.strip() != after.stdout.strip():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "engine tree changed or missing")


def _assert_committed_scope(txn: Path, expected: str, sha: str, facts: tuple[ChangeFact, ...],
                            context: RepositoryContext, moves: tuple[BlobMove, ...] = ()) -> None:
    parents = _git(txn, "rev-list", "--parents", "-n", "1", sha).stdout.strip().split()
    if parents != [sha, expected]:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "committed parent differs from expected")
    changed = _parse_name_status(
        _git_bytes(txn, "diff", "--no-renames", "--name-status", "-z", expected, sha),
    )
    if set(changed) != {(item.status, item.path) for item in facts} or len(changed) != len(facts):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "committed facts differ from transaction allowlist")
    for fact in facts:
        final = _tree_entry(txn, sha, fact.path)
        if fact.status == "D":
            if final is not None:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", f"deletion survived commit: {fact.path}")
        elif final != (fact.mode, "blob", fact.oid):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", f"committed blob differs from index: {fact.path}")
    _assert_blob_moves(facts, moves)
    _assert_engine_tree(txn, expected, sha, context)


def _commit(txn: Path, message: str, expected: str, context: RepositoryContext,
            expected_changes: tuple[ChangeExpectation, ...], moves: tuple[BlobMove, ...] = ()) -> str:
    expectations = _validate_expectations(expected_changes)
    expected_paths = tuple(item.path for item in expectations)
    _assert_worktree_paths(txn, expected_paths, context)
    _git(txn, "add", "--", *expected_paths)
    _assert_cached_scope(txn, expected_paths)
    facts = _capture_precommit_facts(txn, expected, expectations)
    _assert_blob_moves(facts, moves)
    if not _git(txn, "diff", "--cached", "--quiet", check=False).returncode:
        raise ConfigError("FAIL_EMPTY_TRANSACTION", message)
    try:
        if _is_reparse_alias(txn.parent) or not txn.parent.is_dir():
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "unsafe hooks control directory")
        hooks = Path(tempfile.mkdtemp(prefix="hooks-", dir=txn.parent))
        if (_is_reparse_alias(hooks) or not hooks.is_dir() or any(hooks.iterdir())
                or hooks.parent.resolve() != txn.parent.resolve()):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "unsafe hooks directory")
        _git(txn, "-c", f"core.hooksPath={hooks.resolve()}", "commit", "--quiet",
             "-m", f"{message}\n\nTransaction: {uuid.uuid4().hex}")
        sha = _git(txn, "rev-parse", "HEAD").stdout.strip()
        _assert_committed_scope(txn, expected, sha, facts, context, moves)
        return sha
    finally:
        if "hooks" in locals() and hooks.exists():
            shutil.rmtree(hooks)


def _verify_apply_inputs(plan: Plan, plan_hash: str, expected_remote_sha: str) -> Plan:
    try:
        payload = plan.payload
        if not isinstance(payload, dict) or _canonical_hash(payload) != plan.plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", "plan payload integrity")
        for field, value in (("operation", plan.operation), ("candidate_id", plan.candidate_id),
                             ("expected_remote_sha", plan.expected_remote_sha),
                             ("target_ref", CANONICAL_TARGET_REF)):
            if payload.get(field) != value:
                raise ConfigError("FAIL_PLAN_HASH", f"plan {field}")
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError("FAIL_PLAN_HASH", "plan payload integrity") from exc
    if plan_hash != plan.plan_hash:
        raise ConfigError("FAIL_PLAN_HASH", f"expected={plan.plan_hash} actual={plan_hash}")
    if expected_remote_sha != plan.expected_remote_sha:
        raise ConfigError("FAIL_REMOTE_SHA", f"expected={plan.expected_remote_sha} actual={expected_remote_sha}")
    try:
        snapshot = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ConfigError("FAIL_PLAN_HASH", "plan payload snapshot") from exc
    if not isinstance(snapshot, dict) or _canonical_hash(snapshot) != plan.plan_hash:
        raise ConfigError("FAIL_PLAN_HASH", "plan payload snapshot")
    return Plan(plan.operation, plan.candidate_id, plan.expected_remote_sha, plan.plan_hash,
                plan.lines, snapshot)


def prepare_publish(repo: Path, control_root: Path | None, plan: Plan, plan_hash: str,
                    expected_remote_sha: str, *, config_path: Path | None = None) -> Prepared:
    plan = _verify_apply_inputs(plan, plan_hash, expected_remote_sha)
    context = _prepare_context(repo, plan)
    resolved_control, binding = _revalidate_canonical_plan(
        context, control_root, config_path, plan, "publish")
    source = _state_candidate_path(context, plan.candidate_id)
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != plan.payload["source_sha256"]:
        raise ConfigError("FAIL_CANDIDATE_CHANGED", plan.candidate_id)
    txn = _new_worktree(context.repo_root, resolved_control, expected_remote_sha)
    try:
        txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
        destination = _safe_state_path(txn_context, INBOX / source.name)
        if destination.exists():
            raise ConfigError("FAIL_CANDIDATE_EXISTS", plan.candidate_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        changed = (context.state_to_repo_path(INBOX / source.name),)
        sha = _commit(
            txn, f"candidate: publish {plan.candidate_id}", expected_remote_sha, context,
            (ChangeExpectation("A", changed[0], "100644"),),
        )
        return _prepared(
            context.repo_root, resolved_control, txn, sha, plan, changed,
            source_path=source, source_content=content, context=context,
            config_path=Path(binding["config_path"]) if binding else config_path, binding=binding,
        )
    except Exception:
        _cleanup_worktree(context.repo_root, resolved_control, txn)
        raise


def _archive_superseded(text: str, supersedes: str, replacement: str) -> str:
    lines = text.splitlines()
    active = False
    archived_line = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            active = ledger.is_active_heading(line.strip()[3:])
            continue
        match = ledger.ENTRY_RE.match(line)
        if active and match and (match.group(1) or match.group(2)) == supersedes:
            archived_line = lines.pop(index).rstrip() + f" superseded_by: {replacement}."
            break
    if archived_line is None:
        raise ConfigError("FAIL_SUPERSEDES", supersedes)
    archive_index = next((index for index, line in enumerate(lines) if line.startswith("## ") and
                          ("归档" in line or line.strip() == "## Archived")), None)
    if archive_index is None:
        raise ConfigError("FAIL_LEDGER", "missing archive heading")
    lines.insert(archive_index + 1, "")
    lines.insert(archive_index + 2, archived_line)
    return "\n".join(lines) + "\n"


def _append_lesson(text: str, item: dict[str, str], lesson_id: str, label: str,
                   supersedes: str | None) -> str:
    if supersedes:
        text = _archive_superseded(text, supersedes, lesson_id)
    suffix = f" supersedes: {supersedes}." if supersedes else ""
    rendered_id = lesson_id if label == "通用" else f"[[lesson:{lesson_id}]]"
    when = f" when: {item['when']}" if "when" in item else ""
    entry = (f"- **{rendered_id} [pending·{label}] {item['rule']}.** 触发: {item['trigger']}. "
             f"代价: {item['cost']}. from: {item['id']}.{suffix} sink → {item['sink']}.{when}")
    marker = "\n## 归档"
    if marker not in text:
        raise ConfigError("FAIL_LEDGER", "missing archive heading")
    return text.replace(marker, f"\n{entry}\n{marker}", 1)


def prepare_promote(repo: Path, control_root: Path | None, plan: Plan, plan_hash: str,
                    expected_remote_sha: str, *, config_path: Path | None = None) -> Prepared:
    plan = _verify_apply_inputs(plan, plan_hash, expected_remote_sha)
    context = _prepare_context(repo, plan)
    resolved_control, binding = _revalidate_canonical_plan(
        context, control_root, config_path, plan, "promote")
    source = _state_candidate_path(context, plan.candidate_id)
    item = load_candidate(source)
    candidate_git_path = context.state_to_repo_path(INBOX / source.name)
    remote_blob = _git_bytes(context.repo_root, "show", f"origin/main:{candidate_git_path}")
    if remote_blob.returncode != 0:
        raise ConfigError("FAIL_CANDIDATE_UNPUBLISHED", plan.candidate_id)
    if hashlib.sha256(remote_blob.stdout).hexdigest() != plan.payload["candidate_sha256"]:
        raise ConfigError("FAIL_CANDIDATE_CHANGED", plan.candidate_id)
    txn = _new_worktree(context.repo_root, resolved_control, expected_remote_sha)
    try:
        txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
        source_txn = _safe_state_path(txn_context, INBOX / source.name, required="file")
        consumed = _safe_state_path(txn_context, CONSUMED / source.name)
        if consumed.exists():
            raise ConfigError("FAIL_CANDIDATE_STATE", plan.candidate_id)
        ledger_relative = Path(plan.payload["ledger_path"])
        ledger_path = _safe_state_path(txn_context, ledger_relative, required="file")
        _safe_state_path(txn_context, CONSUMED)
        _find_promoted(txn_context, plan.candidate_id)
        consumed.parent.mkdir(parents=True, exist_ok=True)
        source_txn.replace(consumed)
        text = ledger_path.read_text(encoding="utf-8")
        previous = _find_promoted(txn_context, plan.candidate_id)
        if previous:
            raise ConfigError("FAIL_ALREADY_PROMOTED", previous)
        rendered = _append_lesson(
            text, item, plan.payload["lesson_id"], plan.payload["label"], plan.payload["supersedes"])
        ledger_path.write_text(rendered, encoding="utf-8", newline="\n")
        changed = (
            context.state_to_repo_path(ledger_relative),
            context.state_to_repo_path(INBOX / source.name),
            context.state_to_repo_path(CONSUMED / source.name),
        )
        sha = _commit(
            txn, f"lessons: promote {plan.candidate_id}", expected_remote_sha, context,
            (
                ChangeExpectation("M", changed[0], "100644"),
                ChangeExpectation("D", changed[1], "100644"),
                ChangeExpectation("A", changed[2], "100644"),
            ),
            (BlobMove(changed[1], changed[2]),),
        )
        return _prepared(
            context.repo_root, resolved_control, txn, sha, plan, changed, context=context,
            config_path=Path(binding["config_path"]) if binding else config_path, binding=binding,
        )
    except Exception:
        _cleanup_worktree(context.repo_root, resolved_control, txn)
        raise


def _render_advance(text: str, line_index: int, lesson_id: str, payload: dict[str, Any],
                    evidence_sha: str) -> str:
    lines = text.splitlines()
    if not 0 <= line_index < len(lines):
        raise ConfigError("FAIL_LESSON", lesson_id)
    line = lines[line_index]
    match = ledger.ENTRY_RE.match(line)
    if not match or (match.group(1) or match.group(2)) != lesson_id:
        raise ConfigError("FAIL_LESSON", lesson_id)
    line = re.sub(r"\s+last_verified:\s*\d{4}-\d{2}-\d{2}\.? *", "", line)
    line = re.sub(r"\s+evidence:\s*[0-9a-f]{64}\.? *", "", line).rstrip()
    fields = (f" last_verified: {payload['verified_utc'][:10]}."
              f" evidence: {evidence_sha}.")
    when_index = line.find(" when:")
    line = line + fields if when_index < 0 else line[:when_index] + fields + line[when_index:]
    if payload["to_status"] == "enforced":
        line = line.replace("[checklist·", "[enforced·", 1)
        lines[line_index] = line
    elif payload["to_status"] == "archived":
        lines.pop(line_index)
        archive_index = next((index for index, value in enumerate(lines)
                              if value.startswith("## ") and
                              ("归档" in value or value.strip() == "## Archived")), None)
        if archive_index is None:
            raise ConfigError("FAIL_LEDGER", "missing archive heading")
        lines.insert(archive_index + 1, "")
        lines.insert(archive_index + 2, line)
    else:
        raise ConfigError("FAIL_EVIDENCE_STATUS", str(payload["to_status"]))
    return "\n".join(lines) + "\n"


def _canonical_advance_evidence(context: RepositoryContext, prepared: Prepared) -> tuple[str, str, dict[str, Any]]:
    """Reprove committed advance evidence against the pinned expected-base state."""
    try:
        from .retire import find_lesson, verify_receipt

        record = find_lesson(context.state_root, prepared.candidate_id)
        ledger_relative = record.ledger_path.relative_to(context.state_root)
        ledger_path = context.state_to_repo_path(ledger_relative)
        local_ledger = _safe_state_path(context, ledger_relative, required="file").read_bytes()
        base_ledger = _git_bytes(prepared.txn, "show", f"{prepared.expected_remote_sha}:{ledger_path}")
        if base_ledger.returncode != 0 or base_ledger.stdout != local_ledger:
            raise ValueError
        changed = _parse_name_status(_git_bytes(
            prepared.txn, "diff", "--no-renames", "--name-status", "-z",
            prepared.expected_remote_sha, prepared.sha,
        ))
        prefix = context.state_to_repo_path(Path("evidence") / prepared.candidate_id) + "/"
        evidence_paths = [path for status, path in changed if status == "A" and path.startswith(prefix)]
        if len(evidence_paths) != 1:
            raise ValueError
        evidence_path = evidence_paths[0]
        evidence_name = Path(evidence_path)
        if evidence_name.parent.as_posix() != prefix.rstrip("/") or evidence_name.suffix != ".json":
            raise ValueError
        evidence_digest = evidence_name.stem
        if not SHA256_RE.fullmatch(evidence_digest):
            raise ValueError
        committed = _git_bytes(prepared.txn, "show", f"{prepared.sha}:{evidence_path}")
        if committed.returncode != 0:
            raise ValueError
        txn_context = replace(context, repo_root=prepared.txn.resolve(),
                              state_root=context.temporary_state_root(prepared.txn))
        evidence_relative = txn_context.repo_to_state_path(evidence_path)
        if evidence_relative is None:
            raise ValueError
        receipt = _safe_state_path(txn_context, Path(evidence_relative), required="file")
        if receipt.read_bytes() != committed.stdout:
            raise ValueError
        preliminary = json.loads(committed.stdout.decode("utf-8"))
        verified = dt.datetime.strptime(preliminary["verified_utc"], "%Y-%m-%dT%H:%M:%SZ")
        check = verify_receipt(
            txn_context.state_root, receipt, expected_lesson_id=prepared.candidate_id,
            expected_hash=evidence_digest, now=verified.replace(tzinfo=dt.timezone.utc),
        )
        if not check.fresh:
            raise ValueError
        payload = check.payload
        if record.status != payload["from_status"] or record.verifier_id != payload["verifier_id"]:
            raise ValueError
        expected_frozen = {
            "ledger_path": ledger_relative.as_posix(), "from_status": record.status,
            "to_status": payload["to_status"], "verifier_id": record.verifier_id,
            "verified_utc": payload["verified_utc"],
        }
        if (dict(prepared.advance_evidence or ()) != expected_frozen
                or prepared.input_digest_sha256 != evidence_digest):
            raise ValueError
        old_text = base_ledger.stdout.decode("utf-8")
        target_ledger = _git_bytes(prepared.txn, "show", f"{prepared.sha}:{ledger_path}")
        if target_ledger.returncode != 0:
            raise ValueError
        line_index = _advance_active_line(
            old_text, prepared.candidate_id, from_status=record.status,
            verifier_id=record.verifier_id,
        )
        if line_index != record.line_index:
            raise ValueError
        rendered = _render_advance(old_text, line_index, prepared.candidate_id, payload, evidence_digest)
        if target_ledger.stdout != rendered.encode("utf-8"):
            raise ValueError
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise
    except ConfigError as exc:
        if exc.code == "FAIL_GIT":
            raise
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical advance evidence") from None
    except Exception:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical advance evidence") from None
    return evidence_path, ledger_path, payload


def _advance_active_line(text: str, lesson_id: str, *, from_status: str,
                         verifier_id: str) -> int:
    """Find exactly one active ledger line matching frozen advance semantics."""
    active = False
    matches: list[int] = []
    verifier_pattern = re.compile(
        r"(?:^|\s)verifier:\s*([a-z][a-z0-9]*(?:[.-][a-z0-9]+)*)"
    )
    for index, line in enumerate(text.splitlines()):
        if line.strip().startswith("## "):
            active = ledger.is_active_heading(line.strip()[3:])
            continue
        match = ledger.ENTRY_RE.match(line)
        if not active or not match or (match.group(1) or match.group(2)) != lesson_id:
            continue
        verifier = verifier_pattern.search(line)
        if match.group(3) == from_status and verifier and verifier.group(1) == verifier_id:
            matches.append(index)
    if len(matches) != 1:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical advance ledger")
    return matches[0]


def prepare_advance(repo: Path, control_root: Path | None, plan: Plan, plan_hash: str,
                    expected_remote_sha: str, receipt_path: Path,
                    *, config_path: Path | None = None) -> Prepared:
    plan = _verify_apply_inputs(plan, plan_hash, expected_remote_sha)
    if plan.operation != "advance":
        raise ConfigError("FAIL_PLAN_HASH", f"expected advance, got {plan.operation}")
    context = _prepare_context(repo, plan)
    resolved_control, binding = _revalidate_canonical_plan(
        context, control_root, config_path, plan, "advance")
    try:
        raw = receipt_path.read_bytes()
    except OSError as exc:
        raise ConfigError("FAIL_EVIDENCE", f"cannot read {receipt_path}: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != plan.payload["receipt_sha256"]:
        raise ConfigError("FAIL_EVIDENCE_HASH", receipt_path.name)
    _checked_receipt(context.state_root, plan.candidate_id, receipt_path, plan.payload["receipt_sha256"])
    txn = _new_worktree(context.repo_root, resolved_control, expected_remote_sha)
    try:
        txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
        record, check = _checked_receipt(
            txn_context.state_root, plan.candidate_id, receipt_path, plan.payload["receipt_sha256"])
        ledger_relative = record.ledger_path.relative_to(txn_context.state_root)
        if (ledger_relative.as_posix() != plan.payload["ledger_path"]
                or any(check.payload[field] != plan.payload[field]
                       for field in ("from_status", "to_status", "verifier_id", "verified_utc"))):
            raise ConfigError("FAIL_LESSON", plan.candidate_id)
        destination_relative = Path("evidence") / plan.candidate_id / f"{check.sha256}.json"
        destination = _safe_state_path(txn_context, destination_relative)
        if destination.exists():
            raise ConfigError("FAIL_EVIDENCE_EXISTS", destination_relative.as_posix())
        _safe_state_path(txn_context, destination_relative.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        ledger_path = _safe_state_path(txn_context, ledger_relative, required="file")
        rendered = _render_advance(
            ledger_path.read_text(encoding="utf-8"), record.line_index,
            plan.candidate_id, check.payload, check.sha256,
        )
        ledger_path.write_text(rendered, encoding="utf-8", newline="\n")
        changed = tuple(sorted((
            context.state_to_repo_path(destination_relative), context.state_to_repo_path(ledger_relative),
        )))
        expectations = (
            ChangeExpectation("A", context.state_to_repo_path(destination_relative), "100644"),
            ChangeExpectation("M", context.state_to_repo_path(ledger_relative), "100644"),
        )
        sha = _commit(txn, f"lessons: advance {plan.candidate_id} to {check.payload['to_status']}",
                      expected_remote_sha, context, expectations)
        return _prepared(
            context.repo_root, resolved_control, txn, sha, plan, changed, context=context,
            config_path=Path(binding["config_path"]) if binding else config_path, binding=binding,
        )
    except Exception:
        _cleanup_worktree(context.repo_root, resolved_control, txn)
        raise


def _cleanup_worktree(repo: Path, control_root: Path, txn: Path) -> None:
    assert_txn_path(control_root, txn)
    _git(repo, "worktree", "remove", "--force", str(txn), check=False)
    if txn.exists():
        shutil.rmtree(txn)


def _snapshot(prepared: Prepared) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = prepared.control_root / "rollback"
    files_root = root / stamp / "files"
    manifest_entries = []
    for path_text in prepared.changed_paths:
        result = _git_bytes(prepared.repo, "show", f"{prepared.expected_remote_sha}:{path_text}")
        if result.returncode == 0:
            data = result.stdout
            backup = files_root / Path(path_text)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(data)
            manifest_entries.append({"path": path_text, "exists": True,
                                     "sha256": hashlib.sha256(data).hexdigest()})
        else:
            manifest_entries.append({"path": path_text, "exists": False, "sha256": None})
    root.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "agent-core-rollback/1", "id": stamp,
               "pre_apply_sha": prepared.expected_remote_sha, "operation": prepared.operation,
               "candidate_id": prepared.candidate_id, "files": manifest_entries}
    manifest = root / f"{stamp}.json"
    handle, temporary_name = tempfile.mkstemp(prefix=f"{stamp}-", suffix=".tmp", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return stamp


def _push(prepared: Prepared) -> subprocess.CompletedProcess[str]:
    return _git(prepared.repo, "push", "origin", f"{prepared.sha}:refs/heads/main", check=False)


def _remote_head(repo: Path) -> str | None:
    result = _git(repo, "ls-remote", "origin", "refs/heads/main", check=False)
    fields = result.stdout.split()
    return fields[0] if result.returncode == 0 and fields else None


def canonical_push_exact_lease(prepared: Prepared) -> CanonicalPushResult:
    """Issue the one canonical CAS push; callers determine outcome separately."""
    if (prepared.context is None or prepared.context.layout != "canonical"
            or prepared.target_ref != CANONICAL_TARGET_REF
            or not SHA_RE.fullmatch(prepared.expected_remote_sha)
            or not SHA_RE.fullmatch(prepared.sha)):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical push")
    try:
        result = _git(
            prepared.repo, "push", "--porcelain",
            f"--force-with-lease={CANONICAL_TARGET_REF}:{prepared.expected_remote_sha}",
            CANONICAL_REMOTE, f"{prepared.sha}:{CANONICAL_TARGET_REF}", check=False,
        )
    except (ConfigError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical push") from None
    if type(result.returncode) is not int:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical push")
    return CanonicalPushResult(result.returncode)


_FETCH_PORCELAIN_RE = re.compile(
    r"^(?P<flag>[ =*+!t-]) (?P<old>[0-9a-f]{40}) (?P<new>[0-9a-f]{40}) "
    r"refs/remotes/origin/main$"
)
CANONICAL_DESCENDANT_MAX_PATHS = 10_000
CANONICAL_DESCENDANT_MAX_RAW_BYTES = 4 * 1024 * 1024


def _parse_canonical_fetch_porcelain(stdout: str) -> str:
    """Accept exactly one Git >=2.41 fetch porcelain record for canonical main."""
    if not isinstance(stdout, str) or not stdout.endswith("\n"):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation")
    lines = stdout.splitlines()
    if len(lines) != 1:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation")
    match = _FETCH_PORCELAIN_RE.fullmatch(lines[0])
    if match is None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation")
    observed = match.group("new")
    if observed == "0" * 40:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation")
    return observed


def observe_canonical_remote_head(repo: Path) -> str:
    """Fetch exactly once and parse that porcelain record; never inspect FETCH_HEAD."""
    try:
        fetched = _git(repo, "fetch", "--porcelain", "--verbose", "--no-write-fetch-head",
                       "--no-tags", "--no-recurse-submodules", CANONICAL_REMOTE,
                       CANONICAL_TARGET_REF, check=False)
    except (ConfigError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation") from None
    if fetched.returncode != 0:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical remote observation")
    return _parse_canonical_fetch_porcelain(fetched.stdout)


def classify_canonical_remote_outcome(repo: Path, expected_sha: str, prepared_sha: str,
                                      observed_sha: str) -> CanonicalRemoteOutcome:
    """Classify only the supplied pinned observation; this helper never fetches."""
    if not all(SHA_RE.fullmatch(value) for value in (expected_sha, prepared_sha, observed_sha)):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "Git SHA-1 object id required")
    if observed_sha == expected_sha:
        return CanonicalRemoteOutcome("NOT_COMMITTED", observed_sha)
    if observed_sha == prepared_sha or _is_ancestor(repo, prepared_sha, observed_sha):
        return CanonicalRemoteOutcome("COMMITTED", observed_sha)
    if _is_ancestor(repo, expected_sha, observed_sha):
        return CanonicalRemoteOutcome("LOST_RACE", observed_sha)
    return CanonicalRemoteOutcome("UNSAFE", observed_sha)


def validate_canonical_descendant_scope(repo: Path, prepared_sha: str, observed_sha: str,
                                        *, max_depth: int = 256) -> None:
    """Require a bounded linear state-only descendant chain from prepared to observed."""
    if not all(SHA_RE.fullmatch(value) for value in (prepared_sha, observed_sha)) or max_depth < 1:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
    listing = _git_bytes(repo, "ls-tree", "-r", "-z", observed_sha, "--", "state")
    if (listing.returncode != 0 or len(listing.stdout) > CANONICAL_DESCENDANT_MAX_RAW_BYTES
            or (listing.stdout and not listing.stdout.endswith(b"\0"))):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
    try:
        all_paths = tuple(
            part.split(b"\t", 1)[1].decode("utf-8")
            for part in listing.stdout[:-1].split(b"\0") if part
        )
    except (IndexError, UnicodeDecodeError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant") from exc
    if len(all_paths) > CANONICAL_DESCENDANT_MAX_PATHS:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
    _validate_changed_paths(all_paths)
    current = observed_sha
    for _depth in range(max_depth + 1):
        if current == prepared_sha:
            return
        parents = _git(repo, "rev-list", "--parents", "-n", "1", current).stdout.strip().split()
        if len(parents) != 2 or parents[0] != current:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
        parent = parents[1]
        diff = _git_bytes(repo, "diff", "--no-renames", "--name-status", "-z", parent, current)
        if len(diff.stdout) > CANONICAL_DESCENDANT_MAX_RAW_BYTES:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
        changed = _parse_name_status(diff)
        if not changed or len(changed) > CANONICAL_DESCENDANT_MAX_PATHS:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
        _validate_changed_paths(tuple(path for _status, path in changed))
        for status, path in changed:
            if status not in {"A", "M", "D"} or not path.startswith("state/"):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
            entry = _tree_entry(repo, parent if status == "D" else current, path)
            if entry is None or entry[:2] != ("100644", "blob"):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")
        _assert_engine_tree_unchanged(repo, parent, current)
        current = parent
    raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical descendant")


def _revalidate_rollback_prepared_locked(prepared: Prepared) -> tuple[ChangeFact, ...]:
    """Pure-local R1a rollback capsule comparison; it intentionally never observes/fetches."""
    try:
        if (prepared.context is None or prepared.config_path is None or prepared.context.layout != "canonical"
                or prepared.target_ref != CANONICAL_TARGET_REF or prepared.rollback_evidence is None):
            raise ValueError
        evidence = prepared.rollback_evidence
        context = resolve_repository_context(prepared.context.state_root)
        if context != prepared.context or context.repo_root != prepared.repo.resolve():
            raise ValueError
        proof = _canonical_rollback_proof(context, evidence.snapshot_id, prepared.config_path)
        control, records, snapshot, binding, _kind, _record_sha, _restore, _digest, _manifest = proof
        plan = _rollback_plan_from_proof(proof, evidence.pinned_observed_sha)
        if (control != prepared.control_root or not _rollback_evidence_matches(evidence, plan, proof)
                or prepared.plan_hash != evidence.reviewed_plan_hash
                or prepared.expected_remote_sha != evidence.pinned_observed_sha
                or prepared.expected_remote_sha != evidence.prepared_target_sha
                or prepared.binding != _freeze_binding(binding)
                or prepared.binding_digest_sha256 != _binding_digest(binding)
                or prepared.candidate_id != evidence.snapshot_id
                or prepared.input_digest_sha256 != evidence.snapshot_manifest_sha256
                or prepared.tree_oid != _prepared_tree(prepared.txn, prepared.sha)):
            raise ValueError
        if (_git(prepared.repo, "rev-parse", "HEAD").stdout.strip() != evidence.prepared_target_sha
                or _git(prepared.repo, "status", "--porcelain=v1", "--untracked-files=all").stdout):
            raise ValueError
        if not _rollback_worktree_registered(prepared):
            raise ValueError
        expectations = tuple(
            ChangeExpectation(str(item["restore_status"]), str(item["path"]), "100644")
            for item in evidence.inverse_facts
        )
        facts: list[ChangeFact] = []
        for expectation, item in zip(expectations, evidence.inverse_facts, strict=True):
            if expectation.status == "D":
                if not _rollback_tree_matches(prepared.txn, evidence.prepared_target_sha, expectation.path,
                                              item["after"], snapshot):
                    raise ValueError
                entry = _tree_entry(prepared.txn, evidence.prepared_target_sha, expectation.path)
            else:
                if not _rollback_tree_matches(prepared.txn, prepared.sha, expectation.path,
                                              item["before"], snapshot):
                    raise ValueError
                entry = _tree_entry(prepared.txn, prepared.sha, expectation.path)
            if entry is None or entry[0] != "100644":
                raise ValueError
            facts.append(ChangeFact(expectation.status, expectation.path, entry[0], entry[2]))
        if tuple(item.path for item in expectations) != prepared.changed_paths:
            raise ValueError
        _assert_committed_scope(prepared.txn, evidence.prepared_target_sha, prepared.sha,
                                tuple(facts), context)
        return tuple(facts)
    except Exception:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback revalidation") from None


def _rollback_artifact_prepared(
    prepared: Prepared,
    proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                 dict[str, str], str, str, tuple[dict[str, Any], ...], str,
                 dict[str, Any]],
) -> Prepared:
    """Bind this new rollback transaction's durable artifacts to its CAS base.

    The reviewed rollback capsule remains bound to the original transaction's
    expected base.  Its own snapshot and journal instead describe the new
    forward transaction, whose expected remote is the reviewed target.
    """
    evidence = prepared.rollback_evidence
    binding = proof[3]
    if (
        evidence is None
        or binding.get("remote_revision") != evidence.expected_base_sha
        or prepared.binding != _freeze_binding(binding)
        or prepared.binding_digest_sha256 != _binding_digest(binding)
        or prepared.expected_remote_sha != evidence.prepared_target_sha
        or prepared.expected_remote_sha != evidence.pinned_observed_sha
    ):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback artifacts")
    artifact_binding = dict(binding)
    artifact_binding["remote_revision"] = prepared.expected_remote_sha
    return replace(
        prepared,
        binding=_freeze_binding(artifact_binding),
        binding_digest_sha256=_binding_digest(artifact_binding),
    )


def revalidate_canonical_prepared_locked(prepared: Prepared, *, fetch: bool = True) -> tuple[ChangeFact, ...]:
    """Reprove a canonical Prepared while the caller already holds operation_lock.

    This helper deliberately acquires no lock and performs no artifact, push, source,
    or fast-forward mutation.
    """
    if prepared.operation == "rollback":
        return _revalidate_rollback_prepared_locked(prepared)
    if (prepared.operation not in {"publish", "promote", "advance"} or prepared.context is None
            or prepared.config_path is None or prepared.context.layout != "canonical"
            or prepared.target_ref != CANONICAL_TARGET_REF):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    context = resolve_repository_context(prepared.context.state_root)
    if context != prepared.context or context.repo_root != prepared.repo.resolve():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    control, binding, remote = _canonical_binding(
        context, None, prepared.config_path, prepared.operation, fetch=fetch)
    frozen = dict(prepared.binding or ())
    if (control != prepared.control_root or binding is None or frozen != binding
            or _binding_digest(binding) != prepared.binding_digest_sha256
            or remote != prepared.expected_remote_sha):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    head = _git(prepared.repo, "rev-parse", "HEAD").stdout.strip()
    if head != prepared.expected_remote_sha:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    status = _git(prepared.repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    permitted = [f"?? state/inbox/{prepared.candidate_id}.md"] if prepared.operation == "publish" else []
    if status != permitted:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    assert_txn_path(prepared.control_root, prepared.txn)
    if (_git(prepared.txn, "rev-parse", "HEAD").stdout.strip() != prepared.sha
            or _git(prepared.txn, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
            or _prepared_tree(prepared.txn, prepared.sha) != prepared.tree_oid):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    inbox = context.state_to_repo_path(INBOX / f"{prepared.candidate_id}.md")
    if prepared.operation == "publish":
        expected = (ChangeExpectation("A", inbox, "100644"),)
        source = _safe_state_path(context, INBOX / f"{prepared.candidate_id}.md", required="file")
        if (prepared.source_path is None or source.resolve() != prepared.source_path.resolve()
                or source.stat().st_nlink != 1 or prepared.source_content is None
                or source.read_bytes() != prepared.source_content
                or hashlib.sha256(prepared.source_content).hexdigest() != prepared.input_digest_sha256):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    elif prepared.operation == "promote":
        consumed = context.state_to_repo_path(CONSUMED / f"{prepared.candidate_id}.md")
        ledger_paths = [path for path in prepared.changed_paths if path not in {inbox, consumed}]
        source_blob = _git_bytes(prepared.txn, "show", f"{prepared.expected_remote_sha}:{inbox}")
        if source_blob.returncode != 0 or hashlib.sha256(source_blob.stdout).hexdigest() != prepared.input_digest_sha256:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
        try:
            item = parse_candidate_bytes(source_blob.stdout, prepared.candidate_id)
        except ConfigError as exc:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared") from exc
        ledger_relative, _scope, _label, _prefix = _ledger_target(item)
        expected_ledger = context.state_to_repo_path(ledger_relative)
        if ledger_paths != [expected_ledger]:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
        source = _safe_state_path(context, INBOX / f"{prepared.candidate_id}.md", required="file")
        try:
            source_stat = source.stat()
            source_bytes = source.read_bytes()
        except OSError as exc:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared") from exc
        if source_stat.st_nlink != 1 or source_bytes != source_blob.stdout:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
        expected = (ChangeExpectation("D", inbox, "100644"), ChangeExpectation("A", consumed, "100644"),
                    ChangeExpectation("M", ledger_paths[0], "100644"))
    else:
        evidence, ledger_path, _payload = _canonical_advance_evidence(context, prepared)
        expected = (ChangeExpectation("A", evidence, "100644"),
                    ChangeExpectation("M", ledger_path, "100644"))
    facts = tuple(
        ChangeFact(item.status, item.path,
                   (_tree_entry(prepared.txn, prepared.expected_remote_sha, item.path) if item.status == "D"
                    else _tree_entry(prepared.txn, prepared.sha, item.path))[0],
                   (_tree_entry(prepared.txn, prepared.expected_remote_sha, item.path) if item.status == "D"
                    else _tree_entry(prepared.txn, prepared.sha, item.path))[2])
        for item in expected
    )
    if any(fact.mode != "100644" for fact in facts):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    moves = (BlobMove(inbox, consumed),) if prepared.operation == "promote" else ()
    _assert_committed_scope(prepared.txn, prepared.expected_remote_sha, prepared.sha, facts, context, moves)
    if tuple(sorted(item.path for item in facts)) != tuple(sorted(prepared.changed_paths)):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical prepared")
    return facts


def _revalidate_canonical_apply(prepared: Prepared, site: str) -> tuple[ChangeFact, ...]:
    """Sanitize every advance revalidation failure at the public apply boundary."""
    if prepared.operation != "advance":
        return revalidate_canonical_prepared_locked(prepared)
    try:
        return revalidate_canonical_prepared_locked(prepared, fetch=site == "preflight")
    except Exception as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "timeout"
        elif isinstance(exc, UnicodeError):
            reason = "unicode"
        elif isinstance(exc, OSError):
            reason = "os"
        elif isinstance(exc, ConfigError) and exc.code == "FAIL_GIT":
            reason = "git"
        elif isinstance(exc, ConfigError):
            reason = "validation"
        else:
            reason = "unexpected"
        failure = ConfigError("FAIL_TRANSACTION_SCOPE", "canonical advance revalidation")
        failure.revalidation_site = site if site in {"preflight", "under_lock"} else "preflight"
        failure.revalidation_reason = reason
        raise failure from None


def _only_other_inbox_adds(repo: Path, old: str, new: str, candidate_id: str) -> bool:
    result = _git(repo, "diff", "--name-status", old, new, check=False)
    if result.returncode != 0:
        return False
    lines = [line.split("\t") for line in result.stdout.splitlines() if line]
    own = f"inbox/{candidate_id}.md"
    return bool(lines) and all(len(parts) == 2 and parts[0] == "A" and
                               parts[1].startswith("inbox/") and parts[1] != own for parts in lines)


def _rebuild_publish(prepared: Prepared, expected: str) -> Prepared:
    if prepared.context is None:
        raise ConfigError("FAIL_TRANSACTION_CONTEXT", "publish")
    _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
    txn = _new_worktree(prepared.repo, prepared.control_root, expected)
    try:
        context = prepared.context
        txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
        destination = _safe_state_path(txn_context, INBOX / f"{prepared.candidate_id}.md")
        if destination.exists():
            raise ConfigError("FAIL_CANDIDATE_EXISTS", prepared.candidate_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(prepared.source_content or b"")
        path = context.state_to_repo_path(INBOX / f"{prepared.candidate_id}.md")
        sha = _commit(txn, f"candidate: publish {prepared.candidate_id}", expected, context,
                      (ChangeExpectation("A", path, "100644"),))
        return replace(prepared, txn=txn, sha=sha, expected_remote_sha=expected,
                       tree_oid=_prepared_tree(txn, sha))
    except Exception:
        _cleanup_worktree(prepared.repo, prepared.control_root, txn)
        raise


def fast_forward_local(repo: Path, sha: str) -> None:
    _git(repo, "merge", "--ff-only", sha)


def _last_committed_path(control_root: Path) -> Path:
    return control_root.resolve() / LAST_COMMITTED


def _write_last_committed(prepared: Prepared) -> None:
    path = _last_committed_path(prepared.control_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sha": prepared.sha,
        "operation": prepared.operation,
        "candidate_id": prepared.candidate_id,
        "utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    handle, temporary_name = tempfile.mkstemp(prefix="last-committed-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_last_committed(control_root: Path) -> dict[str, str]:
    path = _last_committed_path(control_root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_RECOVERY_ANCHOR", f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"sha", "operation", "candidate_id", "utc"}:
        raise ConfigError("FAIL_RECOVERY_ANCHOR", f"invalid fields: {path}")
    if not all(isinstance(value, str) and value for value in payload.values()):
        raise ConfigError("FAIL_RECOVERY_ANCHOR", f"fields must be non-empty strings: {path}")
    if not SHA_RE.fullmatch(payload["sha"]):
        raise ConfigError("FAIL_RECOVERY_ANCHOR", f"invalid sha: {path}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["utc"]):
        raise ConfigError("FAIL_RECOVERY_ANCHOR", f"invalid utc: {path}")
    return payload


def _clear_last_committed(control_root: Path, sha: str) -> None:
    path = _last_committed_path(control_root)
    if not path.is_file():
        return
    try:
        payload = _load_last_committed(control_root)
    except ConfigError:
        return
    if payload["sha"] == sha:
        path.unlink()


@contextmanager
def operation_lock(control_root: Path):
    path = control_root.resolve() / "locks" / "promote.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise ConfigError("FAIL_LOCKED", str(path)) from exc
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def _project_context(workspace: Path) -> tuple[Path, str]:
    return resolve_project_context(workspace)


def _project_candidate_path(repo: Path, candidate_id: str) -> Path:
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ConfigError("FAIL_CANDIDATE", f"invalid candidate id: {candidate_id}")
    path = repo / PROJECT_INBOX / f"{candidate_id}.md"
    if not path.is_file():
        raise ConfigError("FAIL_CANDIDATE_MISSING", candidate_id)
    return path


def _validate_project_identity(
    item: dict[str, str], project_id: str, *, phase: str,
) -> None:
    expected = f"project:{project_id}"
    if item["scope_hint"] != expected:
        raise ConfigError(
            "FAIL_PROJECT_MISMATCH",
            f"phase={phase} expected={expected} actual={item['scope_hint']}",
        )


def _validate_project_ledger(path: Path, project_id: str) -> str:
    if not path.is_file():
        raise ConfigError("FAIL_LEDGER", f"missing target {PROJECT_LEDGER.as_posix()}")
    _defined, errors, _warnings = ledger.validate_sources(
        [("project", project_id, str(path))]
    )
    if errors:
        raise ConfigError("FAIL_LEDGER", "; ".join(errors))
    return path.read_text(encoding="utf-8")


def _project_plan(
    candidate_id: str, *, project_id: str, lesson_id: str,
    candidate_sha256: str, canonical_sha256: str,
    supersedes: str | None, force_new: bool,
) -> Plan:
    payload = {
        "operation": "project-promote",
        "candidate_id": candidate_id,
        "expected_remote_sha": "",
        "project_id": project_id,
        "lesson_id": lesson_id,
        "ledger_path": PROJECT_LEDGER.as_posix(),
        "candidate_sha256": candidate_sha256,
        "canonical_sha256": canonical_sha256,
        "supersedes": supersedes,
        "force_new": force_new,
    }
    digest = _canonical_hash(payload)
    lines = (
        f"PLAN operation=project-promote candidate={candidate_id}",
        f"PROJECT_ID {project_id}",
        f"LESSON_ID {lesson_id}",
        f"CANDIDATE_SHA256 {candidate_sha256}",
        f"CANONICAL_SHA256 {canonical_sha256}",
        f"PLAN_HASH {digest}",
    )
    return Plan("project-promote", candidate_id, "", digest, lines, payload)


def plan_project_promote(
    workspace: Path, control_root: Path, candidate_id: str, *,
    supersedes: str | None = None, force_new: bool = False,
) -> Plan:
    del control_root  # Planning is read-only; apply owns the operation lock.
    repo, project_id = _project_context(workspace)
    source = _project_candidate_path(repo, candidate_id)
    item = load_candidate(source, allow_project=True)
    _validate_project_identity(item, project_id, phase="plan")
    ledger_relative, scope, _label, prefix = _ledger_target(item, project_root=repo)
    if ledger_relative != PROJECT_LEDGER or scope != "project":
        raise ConfigError("FAIL_PROJECT_CONTEXT", item["scope_hint"])
    ledger_path = repo / ledger_relative
    ledger_text = _validate_project_ledger(ledger_path, project_id)
    previous = _already_promoted(ledger_text, candidate_id)
    if previous:
        raise ConfigError("FAIL_ALREADY_PROMOTED", previous)
    similarities = _similarities(ledger_text, item["rule"])
    _review_similarities(similarities, supersedes=supersedes, force_new=force_new)
    plan = _project_plan(
        candidate_id,
        project_id=project_id,
        lesson_id=_next_id(ledger_text, scope, prefix),
        candidate_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        canonical_sha256=hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        supersedes=supersedes,
        force_new=force_new,
    )
    extra = tuple(f"SIMILAR {item_id} {score:.3f}" for item_id, score in similarities)
    return replace(plan, lines=extra + plan.lines)


def _atomic_write_text(path: Path, text: str) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_project_plan(plan: Plan, plan_hash: str) -> None:
    if plan.operation != "project-promote":
        raise ConfigError("FAIL_PLAN_HASH", f"expected project-promote, got {plan.operation}")
    if plan.plan_hash != _canonical_hash(plan.payload) or plan_hash != plan.plan_hash:
        raise ConfigError("FAIL_INPUT_CHANGED", plan.candidate_id)


def apply_project_promote(
    workspace: Path, control_root: Path, plan: Plan, plan_hash: str,
) -> ProjectResult:
    with operation_lock(control_root):
        _verify_project_plan(plan, plan_hash)
        repo, project_id = _project_context(workspace)
        source = _project_candidate_path(repo, plan.candidate_id)
        item = load_candidate(source, allow_project=True)
        _validate_project_identity(item, project_id, phase="apply")
        ledger_relative, scope, label, prefix = _ledger_target(item, project_root=repo)
        ledger_path = repo / ledger_relative
        ledger_text = _validate_project_ledger(ledger_path, project_id)
        live_candidate_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        live_canonical_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        expected_values = {
            "project_id": project_id,
            "ledger_path": ledger_relative.as_posix(),
            "candidate_sha256": live_candidate_hash,
            "canonical_sha256": live_canonical_hash,
            "lesson_id": _next_id(ledger_text, scope, prefix),
        }
        if any(plan.payload.get(key) != value for key, value in expected_values.items()):
            raise ConfigError("FAIL_INPUT_CHANGED", plan.candidate_id)
        previous = _already_promoted(ledger_text, plan.candidate_id)
        if previous:
            raise ConfigError("FAIL_ALREADY_PROMOTED", previous)
        rendered = _append_lesson(
            ledger_text, item, plan.payload["lesson_id"], label, plan.payload["supersedes"],
        )
        _ids, errors, _warnings = ledger.parse_ledger(
            rendered, "project", ledger_relative.as_posix(),
        )
        if errors:
            raise ConfigError("FAIL_LEDGER", "; ".join(errors))
        consumed = repo / PROJECT_CONSUMED / source.name
        if consumed.exists():
            raise ConfigError("FAIL_CANDIDATE_STATE", plan.candidate_id)
        consumed.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(ledger_path, rendered)
        source.replace(consumed)
        source_relative = source.relative_to(repo).as_posix()
        consumed_relative = consumed.relative_to(repo).as_posix()
        stage_paths = [ledger_relative.as_posix(), consumed_relative]
        tracked = _git(
            repo, "ls-files", "--error-unmatch", "--", source_relative, check=False,
        ).returncode == 0
        if tracked:
            stage_paths.append(source_relative)
        _git(repo, "add", "-A", "--", *stage_paths)
        return ProjectResult(plan.payload["lesson_id"], tuple(stage_paths))


def apply_prepared(prepared: Prepared, *, retry_inbox_race: bool = False) -> Result:
    # A canonical R1b rollback retains its linked worktree as an explicit,
    # bounded cleanup residue.  Its capsule is never routed through generic
    # context-failure cleanup.
    if prepared.operation == "rollback" and prepared.rollback_evidence is not None:
        try:
            if prepared.context is None or prepared.config_path is None:
                raise ValueError
            context = resolve_repository_context(prepared.context.state_root)
            if context != prepared.context or context.repo_root != prepared.repo.resolve() or context.layout != "canonical":
                raise ValueError
            actual_control = _canonical_control_root(context, None)
            frozen = dict(prepared.binding or ())
            if (actual_control != prepared.control_root
                    or frozen.get("control_identity") != str(actual_control)
                    or frozen.get("control_filesystem_sha256") != _control_filesystem_sha256(actual_control)):
                raise ValueError
            with operation_lock(actual_control):
                reviewed, proof = _review_canonical_rollback(
                    context, prepared.candidate_id, prepared.config_path,
                )
                if (reviewed.plan_hash != prepared.plan_hash
                        or reviewed.expected_remote_sha != prepared.expected_remote_sha
                        or not _rollback_evidence_matches(prepared.rollback_evidence, reviewed, proof)):
                    raise ConfigError("FAIL_INPUT_CHANGED", "canonical rollback plan")
                return _apply_canonical_rollback_locked(prepared, reviewed, proof)
        except ConfigError as exc:
            _raise_canonical_rollback_error(exc)
        except Exception:
            _raise_canonical_rollback_error(None)
    if prepared.context is None:
        try:
            assert_txn_path(prepared.control_root, prepared.txn)
        except ConfigError:
            pass
        else:
            if prepared.txn.exists():
                _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
        raise ConfigError("FAIL_TRANSACTION_CONTEXT", prepared.operation)
    try:
        actual = resolve_repository_context(prepared.context.state_root)
        if (
            actual != prepared.context
            or actual.repo_root != prepared.repo.resolve()
        ):
            raise ConfigError("FAIL_TRANSACTION_CONTEXT", prepared.operation)
    except ConfigError as exc:
        if prepared.txn.exists():
            try:
                assert_txn_path(prepared.control_root, prepared.txn)
            except ConfigError:
                pass
            else:
                _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
        if exc.code == "FAIL_TRANSACTION_CONTEXT":
            raise
        raise ConfigError("FAIL_TRANSACTION_CONTEXT", prepared.operation) from exc
    if actual.layout == "canonical":
        if prepared.operation not in {"publish", "promote", "advance"}:
            raise ConfigError("FAIL_CANONICAL_TRANSACTION_PENDING", prepared.operation)
        with operation_lock(prepared.control_root):
            return _apply_canonical_prepared_locked(prepared)
    with operation_lock(prepared.control_root):
        return _apply_prepared_locked(prepared, retry_inbox_race=retry_inbox_race)


def _quarantine_directory_identity(path: Path) -> tuple[int, int]:
    entry = os.lstat(path)
    if _is_reparse_alias(path) or not stat.S_ISDIR(entry.st_mode):
        raise ValueError
    return entry.st_dev, entry.st_ino


def _quarantine_file_identity(path: Path) -> tuple[int, int, int, str]:
    entry = os.lstat(path)
    if _is_reparse_alias(path) or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError
    content = path.read_bytes()
    latest = os.lstat(path)
    if (latest.st_dev != entry.st_dev or latest.st_ino != entry.st_ino
            or latest.st_size != entry.st_size or latest.st_nlink != 1):
        raise ValueError
    return entry.st_dev, entry.st_ino, entry.st_size, hashlib.sha256(content).hexdigest()


def _quarantine_publish_source(prepared: Prepared, journal: JournalRef) -> tuple[JournalRef, QuarantineRef]:
    """Atomically move one still-identical publish input into Git metadata."""
    if prepared.context is None or prepared.source_path is None or prepared.source_content is None:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "publish quarantine")
    source = _safe_state_path(prepared.context, INBOX / f"{prepared.candidate_id}.md", required="file")
    if source.resolve() != prepared.source_path.resolve():
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "publish quarantine")
    target_parent: Path | None = None
    moved = False
    try:
        with source.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError
            raw = stream.read()
            after = os.fstat(stream.fileno())
            if (after.st_dev != before.st_dev or after.st_ino != before.st_ino
                    or after.st_size != before.st_size or after.st_nlink != 1
                    or raw != prepared.source_content
                    or hashlib.sha256(raw).hexdigest() != prepared.input_digest_sha256):
                raise ValueError
            journal = append_journal_event(journal, "source_removal_intent", {"phase": "quarantine"}, prepared=prepared)
        latest = os.lstat(source)
        if (_is_reparse_alias(source) or not stat.S_ISREG(latest.st_mode) or latest.st_nlink != 1
                or latest.st_dev != before.st_dev or latest.st_ino != before.st_ino
                or latest.st_size != before.st_size):
            raise ValueError
        root = _quarantine_root(prepared.repo)
        root.mkdir(mode=0o700, exist_ok=True)
        root_dev, root_ino = _quarantine_directory_identity(root)
        target_parent = root / journal.operation_id
        os.mkdir(target_parent, mode=0o700)
        target = target_parent / f"{prepared.candidate_id}.md"
        operation_dev, operation_ino = _quarantine_directory_identity(target_parent)
        if (target.name != f"{prepared.candidate_id}.md" or os.path.lexists(target)
                or operation_dev != latest.st_dev):
            raise ValueError
        final = os.lstat(source)
        if (not stat.S_ISREG(final.st_mode) or final.st_nlink != 1
                or final.st_dev != before.st_dev or final.st_ino != before.st_ino
                or final.st_size != before.st_size):
            raise ValueError
        if (_quarantine_directory_identity(root) != (root_dev, root_ino)
                or _quarantine_directory_identity(target_parent) != (operation_dev, operation_ino)):
            raise ValueError
        os.replace(source, target)
        moved = True
        target_dev, target_ino, target_size, target_sha256 = _quarantine_file_identity(target)
        if (target_dev != before.st_dev or target_size != before.st_size
                or target_sha256 != prepared.input_digest_sha256):
            raise ValueError
        journal = append_journal_event(journal, "source_removed", {"phase": "quarantine"}, prepared=prepared)
        return journal, QuarantineRef(
            root, root_dev, root_ino, target_parent, operation_dev, operation_ino,
            target, target_dev, target_ino, target_size, target_sha256,
        )
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "publish quarantine") from exc
    finally:
        if target_parent is not None and not moved:
            try:
                target_parent.rmdir()
            except OSError:
                pass


def _cleanup_quarantine(reference: QuarantineRef, prepared: Prepared, journal: JournalRef) -> JournalRef:
    try:
        root = _quarantine_root(prepared.repo)
        if (root != reference.root or reference.operation_dir.parent != root
                or reference.target.parent != reference.operation_dir
                or reference.target.name != f"{prepared.candidate_id}.md"
                or not ARTIFACT_ID_RE.fullmatch(reference.operation_dir.name)
                or _quarantine_directory_identity(root) != (reference.root_dev, reference.root_ino)
                or _quarantine_directory_identity(reference.operation_dir)
                != (reference.operation_dev, reference.operation_ino)
                or _quarantine_file_identity(reference.target)
                != (reference.target_dev, reference.target_ino, reference.target_size, reference.target_sha256)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "quarantine")
        reference.target.unlink()
        reference.operation_dir.rmdir()
    except (ConfigError, OSError, ValueError):
        try:
            append_journal_event(journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
        except (ConfigError, OSError, ValueError):
            pass
    return journal


def _canonical_failed(primary: ConfigError, prepared: Prepared, journal: JournalRef | None,
                      observed: str | None) -> None:
    if journal is not None:
        details: dict[str, str] = {"phase": "apply", "code": primary.code}
        site = getattr(primary, "revalidation_site", None)
        reason = getattr(primary, "revalidation_reason", None)
        if site in {"preflight", "under_lock"} and reason in {
                "git", "os", "timeout", "unicode", "validation", "unexpected"}:
            details.update(site=site, reason=reason)
        if observed is not None and SHA_RE.fullmatch(observed):
            details["sha"] = observed
        try:
            append_journal_event(journal, "failed", details, prepared=prepared)
        except ConfigError:
            pass
    if prepared.operation != "rollback" and prepared.txn.exists():
        try:
            _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
        except Exception:
            pass


def _apply_canonical_prepared_locked(prepared: Prepared) -> Result:
    """Apply canonical publish/promote/advance using one pinned observation.

    The caller owns ``operation_lock``. This path never retries, rebuilds, or
    chases a remote revision after its single pinned observation.
    """
    if prepared.operation not in {"publish", "promote", "advance"}:
        raise ConfigError("FAIL_CANONICAL_TRANSACTION_PENDING", prepared.operation)
    journal: JournalRef | None = None
    snapshot: SnapshotRef | None = None
    observed: str | None = None
    quarantined: QuarantineRef | None = None
    try:
        _revalidate_canonical_apply(prepared, "preflight")
        snapshot = create_canonical_snapshot(prepared)
        journal = create_canonical_journal(prepared, snapshot)
        journal = append_journal_event(journal, "preflight_ok", {"phase": "preflight"}, prepared=prepared)
        journal = append_journal_event(journal, "snapshot_durable", {"phase": "snapshot"}, prepared=prepared)
        _revalidate_canonical_apply(prepared, "under_lock")
        journal = append_journal_event(journal, "push_attempt", {"phase": "push"}, prepared=prepared)
        try:
            canonical_push_exact_lease(prepared)
        except ConfigError:
            pass
        try:
            observed = observe_canonical_remote_head(prepared.repo)
            outcome = classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, observed)
        except (ConfigError, OSError, subprocess.SubprocessError, UnicodeError):
            raise ConfigError("REMOTE_OUTCOME_UNKNOWN", prepared.operation) from None
        if outcome.status in {"NOT_COMMITTED", "LOST_RACE"}:
            raise ConfigError("FAIL_REMOTE_RACE", prepared.operation)
        if outcome.status != "COMMITTED":
            raise ConfigError("FAIL_REMOTE_REWIND", prepared.operation)
        try:
            validate_canonical_descendant_scope(prepared.repo, prepared.sha, observed)
        except (ConfigError, OSError, subprocess.SubprocessError, UnicodeError):
            raise ConfigError("REMOTE_COMMITTED_SCOPE_UNVERIFIED", prepared.operation) from None
        try:
            journal = append_journal_event(journal, "ancestry_observed", {"phase": "ancestry", "sha": observed}, prepared=prepared)
            record_remote_head(prepared.control_root, observed)
            journal = append_journal_event(journal, "remote_pointer_updated", {"phase": "pointer", "sha": observed}, prepared=prepared)
            if prepared.operation == "publish":
                journal, quarantined = _quarantine_publish_source(prepared, journal)
            fast_forward_local(prepared.repo, observed)
            if _git(prepared.repo, "rev-parse", "HEAD").stdout.strip() != observed:
                raise ConfigError("REMOTE_COMMITTED_LOCAL_STALE", prepared.operation)
        except Exception:
            raise ConfigError("REMOTE_COMMITTED_LOCAL_STALE", prepared.operation) from None
        try:
            journal = append_journal_event(journal, "fast_forward_done", {"phase": "fast-forward", "sha": observed}, prepared=prepared)
            _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
            journal = append_journal_event(journal, "completed", {"phase": "completed", "sha": observed}, prepared=prepared)
        except Exception:
            raise ConfigError("REMOTE_COMMITTED_FINALIZATION_INCOMPLETE", prepared.operation) from None
        if quarantined is not None:
            try:
                _cleanup_quarantine(quarantined, prepared, journal)
            except Exception:
                pass
        return Result(observed, snapshot.snapshot_id)
    except ConfigError as primary:
        _canonical_failed(primary, prepared, journal, observed)
        raise


def _apply_canonical_rollback_locked(prepared: Prepared, reviewed: Plan,
                                     proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                                                  dict[str, str], str, str, tuple[dict[str, Any], ...], str,
                                                  dict[str, Any]]) -> Result:
    """Commit one already-reviewed forward rollback without deleting its worktree.

    The caller owns the canonical operation lock.  ``reviewed`` supplied the only
    pre-push observation; this function deliberately performs just its outcome
    observation after the exact lease attempt.
    """
    journal: JournalRef | None = None
    snapshot: SnapshotRef | None = None
    observed: str | None = None
    artifact_prepared = prepared
    try:
        evidence = prepared.rollback_evidence
        if (evidence is None or prepared.operation != "rollback" or reviewed.operation != "rollback"
                or reviewed.plan_hash != prepared.plan_hash
                or reviewed.expected_remote_sha != prepared.expected_remote_sha
                or not _rollback_evidence_matches(evidence, reviewed, proof)):
            raise ConfigError("FAIL_INPUT_CHANGED", "canonical rollback plan")
        # This is pure local: it rechecks capsule registration, full binding,
        # source snapshot, inverse facts, and the prepared commit before the
        # first durable apply artifact is created.
        _revalidate_rollback_prepared_locked(prepared)
        artifact_prepared = _rollback_artifact_prepared(prepared, proof)
        snapshot = create_canonical_snapshot(artifact_prepared)
        journal = create_canonical_journal(artifact_prepared, snapshot)
        journal = append_journal_event(journal, "preflight_ok", {"phase": "preflight"}, prepared=artifact_prepared)
        journal = append_journal_event(journal, "snapshot_durable", {"phase": "snapshot"}, prepared=artifact_prepared)
        _revalidate_rollback_prepared_locked(prepared)
        journal = append_journal_event(journal, "push_attempt", {"phase": "push"}, prepared=artifact_prepared)
        try:
            canonical_push_exact_lease(prepared)
        except ConfigError:
            # Return status is not authoritative.  The single post-push
            # observation below determines every remote outcome.
            pass
        try:
            observed = observe_canonical_remote_head(prepared.repo)
            outcome = classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, observed,
            )
        except (ConfigError, OSError, subprocess.SubprocessError, UnicodeError):
            raise ConfigError("REMOTE_OUTCOME_UNKNOWN", "rollback") from None
        if outcome.status in {"NOT_COMMITTED", "LOST_RACE"}:
            raise ConfigError("FAIL_REMOTE_RACE", "rollback")
        if outcome.status != "COMMITTED":
            raise ConfigError("FAIL_REMOTE_REWIND", "rollback")
        try:
            validate_canonical_descendant_scope(prepared.repo, prepared.sha, observed)
        except Exception:
            raise ConfigError("REMOTE_COMMITTED_SCOPE_UNVERIFIED", "rollback") from None
        try:
            journal = append_journal_event(
                journal, "ancestry_observed", {"phase": "ancestry", "sha": observed}, prepared=artifact_prepared,
            )
            record_remote_head(prepared.control_root, observed)
            journal = append_journal_event(
                journal, "remote_pointer_updated", {"phase": "pointer", "sha": observed}, prepared=artifact_prepared,
            )
            fast_forward_local(prepared.repo, observed)
            if _git(prepared.repo, "rev-parse", "HEAD").stdout.strip() != observed:
                raise ValueError
            journal = append_journal_event(
                journal, "fast_forward_done", {"phase": "fast-forward", "sha": observed}, prepared=artifact_prepared,
            )
        except Exception:
            raise ConfigError("REMOTE_COMMITTED_LOCAL_STALE", "rollback") from None
        try:
            journal = append_journal_event(
                journal, "completed", {"phase": "completed", "sha": observed}, prepared=artifact_prepared,
            )
            # R1b-0 deliberately made retained registered worktrees explicit
            # terminal residue.  No rollback path invokes generic cleanup.
            journal = append_journal_event(
                journal, "cleanup_pending", {"phase": "worktree", "kind": "worktree"}, prepared=artifact_prepared,
            )
        except Exception:
            raise ConfigError("REMOTE_COMMITTED_FINALIZATION_INCOMPLETE", "rollback") from None
        return Result(observed, snapshot.snapshot_id, True, "worktree")
    except ConfigError as primary:
        _canonical_failed(primary, artifact_prepared, journal, observed)
        raise


def _apply_prepared_locked(prepared: Prepared, *, retry_inbox_race: bool) -> Result:
    assert_txn_path(prepared.control_root, prepared.txn)
    active = prepared
    rollback_id = ""
    try:
        for attempt in range(3):
            rollback_id = _snapshot(active)
            observed = _remote_head(active.repo)
            pushed = _push(active) if observed == active.expected_remote_sha else None
            if pushed is not None and pushed.returncode == 0:
                break
            if not (retry_inbox_race and active.operation == "publish" and attempt < 2):
                raise ConfigError("FAIL_REMOTE_RACE", active.candidate_id)
            _git(active.repo, "fetch", "origin", "--quiet")
            current = _git(active.repo, "rev-parse", "origin/main").stdout.strip()
            if not _only_other_inbox_adds(active.repo, active.expected_remote_sha, current, active.candidate_id):
                raise ConfigError("FAIL_REMOTE_RACE", active.candidate_id)
            active = _rebuild_publish(active, current)
        else:
            raise ConfigError("FAIL_REMOTE_RACE", active.candidate_id)
        _write_last_committed(active)
        _git(active.repo, "fetch", "origin", "--quiet")
        remote = _git(active.repo, "rev-parse", "origin/main").stdout.strip()
        if remote != active.sha and not _is_ancestor(active.repo, active.sha, remote):
            raise ConfigError("FAIL_REMOTE_REWIND", f"committed={active.sha} remote={remote}")
        record_remote_head(active.control_root, remote)
        if active.operation == "publish" and active.source_path and active.source_path.is_file():
            if hashlib.sha256(active.source_path.read_bytes()).digest() != hashlib.sha256(active.source_content or b"").digest():
                raise ConfigError("FAIL_CANDIDATE_CHANGED", active.candidate_id)
            active.source_path.unlink()
        try:
            fast_forward_local(active.repo, remote)
        except Exception as exc:
            raise ConfigError("REMOTE_COMMITTED_LOCAL_STALE", active.sha) from exc
        _clear_last_committed(active.control_root, active.sha)
        return Result(active.sha, rollback_id)
    finally:
        if active.txn.exists():
            _cleanup_worktree(active.repo, active.control_root, active.txn)


def _canonical_recovery_binding(context: RepositoryContext, supplied_control: Path | None,
                                config_path: Path | None, expected_remote_sha: str) -> tuple[Path, dict[str, str]]:
    """Rebuild canonical identity from local attachment evidence without fetching."""
    if config_path is None:
        raise ConfigError("FAIL_STATE_BINDING", "canonical transaction requires --config")
    control_root = _canonical_control_root(context, supplied_control)
    evidence = _validate_state_binding_context(
        context, config_path, require_clean_snapshot=False,
        require_remote_observation=False, expected_remote_revision=expected_remote_sha,
    )
    binding = {
        "config_path": str(Path(config_path).resolve()),
        "receipt_path": str(evidence.receipt_path),
        "schema": evidence.schema,
        "config_sha256": evidence.config_sha256,
        "receipt_sha256": evidence.receipt_sha256,
        "remote_url_sha256": evidence.remote_url_sha256,
        "remote_revision": evidence.remote_revision,
        "repository_root_sha": evidence.repository_root_sha or "",
        "repository_root": str(context.repo_root),
        "engine_provenance_sha256": evidence.engine_provenance_sha256 or "",
        "state_lock_sha256": evidence.state_lock_sha256,
        "control_identity": str(control_root),
        "control_filesystem_sha256": _control_filesystem_sha256(control_root),
    }
    return control_root, binding


def _recovery_baseline_valid(record: dict[str, Any], path: Path) -> bool:
    required = {
        "schema", "record_type", "sequence", "operation_id", "original_operation_id",
        "original_operation", "target_sha", "expected_base_sha",
        "original_journal_final_record_sha256", "snapshot_id", "snapshot_manifest_sha256",
        "snapshot_input_sha256", "plan_hash", "binding_digest_sha256", "lineage_root_sha",
        "control_identity_sha256", "control_filesystem_sha256", "target_ref",
        "confirmed_observed_sha", "action", "utc", "previous_record_sha256", "record_sha256",
    }
    if (set(record) != required or record.get("record_type") != "recovery-baseline"
            or record.get("operation_id") != path.stem or record.get("target_ref") != CANONICAL_TARGET_REF
            or record.get("action") not in RECOVERY_ACTIONS
            or record.get("original_operation") not in {"publish", "promote", "advance", "rollback"}
            or not ARTIFACT_ID_RE.fullmatch(str(record.get("operation_id")))
            or not ARTIFACT_ID_RE.fullmatch(str(record.get("original_operation_id")))
            or not ARTIFACT_ID_RE.fullmatch(str(record.get("snapshot_id")))):
        return False
    if record.get("original_operation") == "publish":
        input_valid = isinstance(record.get("snapshot_input_sha256"), str) and SHA256_RE.fullmatch(
            record["snapshot_input_sha256"],
        ) is not None
    else:
        input_valid = record.get("snapshot_input_sha256") is None and record.get("action") not in {
            "input-disposition", "cleanup-only",
        }
    return (input_valid and all(isinstance(record.get(key), str) and SHA_RE.fullmatch(record[key])
                                for key in ("target_sha", "expected_base_sha", "lineage_root_sha",
                                            "confirmed_observed_sha"))
            and all(isinstance(record.get(key), str) and SHA256_RE.fullmatch(record[key])
                    for key in ("original_journal_final_record_sha256", "snapshot_manifest_sha256", "plan_hash",
                                "binding_digest_sha256", "control_identity_sha256", "control_filesystem_sha256")))


def _recovery_event_details(baseline: dict[str, Any], event: str, details: Any) -> bool:
    if event in {"converged", "completed"}:
        return details == {}
    if event == "failed":
        return details == {} or (isinstance(details, dict) and set(details) == {"site", "reason"}
                                 and details.get("site") == "action"
                                 and details.get("reason") in {"validation", "git", "os", "timeout", "unicode", "unexpected"})
    if event == "cleanup-pending":
        return isinstance(details, dict) and set(details) == {"kind"} and details.get("kind") in {
            "quarantine-delete", "worktree-cleanup",
        }
    role = RECOVERY_EVENT_ROLES.get(event)
    if role is None or not isinstance(details, dict) or details.get("role") != role:
        return False
    if event in {"source-quarantine-intent", "source-quarantined", "source-restore-intent",
                 "source-restored", "source-preserved", "quarantine-delete-intent", "quarantine-deleted"}:
        return (baseline["original_operation"] == "publish"
                and set(details) == {"role", "input_sha256", "handle_identity_sha256"}
                and details.get("input_sha256") == baseline["snapshot_input_sha256"]
                and isinstance(details.get("handle_identity_sha256"), str)
                and SHA256_RE.fullmatch(details["handle_identity_sha256"]) is not None)
    if event in {"fast-forward-intent", "fast-forward-done", "pointer-updated"}:
        return (set(details) == {"role", "sha"} and details.get("sha") == baseline["confirmed_observed_sha"])
    return (set(details) == {"role", "handle_identity_sha256"}
            and isinstance(details.get("handle_identity_sha256"), str)
            and SHA256_RE.fullmatch(details["handle_identity_sha256"]) is not None)


def _consume_recovery_pair(events: tuple[str, ...], index: int, intent: str, done: str) -> int:
    if index >= len(events) or events[index] != intent:
        return index
    index += 1
    if index == len(events):
        return index
    if events[index] != done:
        raise ValueError
    return index + 1


def _recovery_core_valid(action: str, events: tuple[str, ...]) -> bool:
    """Accept a valid action prefix so an interrupted durable intent remains diagnosable."""
    try:
        index = 0
        if action == "artifact-cleanup":
            index = _consume_recovery_pair(events, index, "worktree-cleanup-intent", "worktree-cleaned")
            if index == len(events):
                return True
            if events[index] != "converged":
                return False
            index += 1
            return (index == len(events)
                    or _consume_recovery_pair(events, index, "worktree-cleanup-intent", "worktree-cleaned") == len(events))
        if action == "cleanup-only":
            index = _consume_recovery_pair(events, index, "quarantine-delete-intent", "quarantine-deleted")
            if index == 0 or index == len(events):
                return index == len(events)
            return events[index] == "converged" and index + 1 == len(events)
        if action == "input-disposition":
            if not events:
                return True
            if events[index] == "source-preserved":
                index += 1
            elif events[index] == "source-restore-intent":
                index = _consume_recovery_pair(events, index, "source-restore-intent", "source-restored")
            else:
                return False
            if index == len(events):
                return True
            if events[index] != "converged":
                return False
            index += 1
            seen: set[str] = set()
            while index < len(events):
                event = events[index]
                if event == "quarantine-delete-intent" and event not in seen:
                    seen.add(event)
                    index = _consume_recovery_pair(events, index, event, "quarantine-deleted")
                elif event == "worktree-cleanup-intent" and event not in seen:
                    seen.add(event)
                    index = _consume_recovery_pair(events, index, event, "worktree-cleaned")
                else:
                    return False
            return True
        if action == "local-finalization":
            if not events:
                return True
            if events[index] != "pointer-updated":
                return False
            index += 1
            index = _consume_recovery_pair(events, index, "source-quarantine-intent", "source-quarantined")
            if index == len(events):
                return True
            before_ff = index
            index = _consume_recovery_pair(events, index, "fast-forward-intent", "fast-forward-done")
            if index == before_ff or index == len(events):
                return index == len(events)
            if events[index] != "converged":
                return False
            index += 1
            seen = set()
            while index < len(events):
                event = events[index]
                if event == "quarantine-delete-intent" and event not in seen:
                    seen.add(event)
                    index = _consume_recovery_pair(events, index, event, "quarantine-deleted")
                elif event == "worktree-cleanup-intent" and event not in seen:
                    seen.add(event)
                    index = _consume_recovery_pair(events, index, event, "worktree-cleaned")
                else:
                    return False
            return True
    except ValueError:
        return False
    return False


def _validate_recovery_transitions(baseline: dict[str, Any], records: tuple[dict[str, Any], ...]) -> None:
    events = tuple(record["event"] for record in records[1:])
    if any(event not in RECOVERY_EVENTS for event in events):
        raise ValueError
    if events.count("failed") > 1 or events.count("completed") > 1 or events.count("converged") > 1 or events.count("cleanup-pending") > 1:
        raise ValueError
    completed_pairs = {
        "source-quarantined": "source-quarantine-intent", "source-restored": "source-restore-intent",
        "quarantine-deleted": "quarantine-delete-intent", "fast-forward-done": "fast-forward-intent",
        "worktree-cleaned": "worktree-cleanup-intent",
    }
    for index, event in enumerate(events):
        intent = completed_pairs.get(event)
        if intent is None:
            continue
        if index == 0 or events[index - 1] != intent:
            raise ValueError
        previous_details = records[index]["details"]
        current_details = records[index + 1]["details"]
        if previous_details.get("handle_identity_sha256") != current_details.get("handle_identity_sha256"):
            raise ValueError
        if "input_sha256" in previous_details and previous_details.get("input_sha256") != current_details.get("input_sha256"):
            raise ValueError
    if "failed" in events:
        failed_at = events.index("failed")
        if failed_at != len(events) - 1 or "converged" in events[:failed_at]:
            raise ValueError
        if not _recovery_core_valid(baseline["action"], events[:failed_at]):
            raise ValueError
        return
    if "cleanup-pending" in events:
        pending_at = events.index("cleanup-pending")
        if (baseline["action"] not in {"input-disposition", "local-finalization"}
                and not (baseline["original_operation"] == "rollback"
                         and baseline["action"] == "artifact-cleanup")
                or pending_at != len(events) - 1 or pending_at == 0 or events[pending_at - 1] != "completed"):
            raise ValueError
        kind = records[pending_at + 1]["details"]["kind"]
        preceding = events[:pending_at - 1]
        if not preceding or preceding[-1] != f"{kind}-intent":
            raise ValueError
    if "completed" in events:
        completed_at = events.index("completed")
        if completed_at != len(events) - 1 and not (completed_at == len(events) - 2 and events[-1] == "cleanup-pending"):
            raise ValueError
        core = events[:completed_at]
        if "converged" not in core or not _recovery_core_valid(baseline["action"], core):
            raise ValueError
        return
    if "cleanup-pending" in events or not _recovery_core_valid(baseline["action"], events):
        raise ValueError


def _parse_recovery_journal_bytes(raw: bytes, path: Path, root: Path) -> tuple[dict[str, Any], ...]:
    if (_is_reparse_alias(root) or not root.is_dir() or path != root / f"{path.stem}.jsonl"
            or path.suffix != ".jsonl" or not ARTIFACT_ID_RE.fullmatch(path.stem)
            or not raw or not raw.endswith(b"\n")):
        raise ValueError
    records = tuple(json.loads(line) for line in raw.decode("utf-8").splitlines())
    previous = None
    for sequence, record in enumerate(records):
        if (not isinstance(record, dict) or record.get("schema") != RECOVERY_JOURNAL_SCHEMA
                or record.get("sequence") != sequence or type(record.get("sequence")) is not int
                or record.get("previous_record_sha256") != previous
                or not isinstance(record.get("utc"), str) or not UTC_RE.fullmatch(record["utc"])
                or not isinstance(record.get("record_sha256"), str)
                or not SHA256_RE.fullmatch(record["record_sha256"])
                or record["record_sha256"] != _record_hash(record, "record_sha256")):
            raise ValueError
        if sequence == 0:
            if not _recovery_baseline_valid(record, path):
                raise ValueError
        elif (set(record) != {"schema", "record_type", "sequence", "event", "utc", "details",
                              "previous_record_sha256", "record_sha256"}
              or record.get("record_type") != "event" or record.get("event") not in RECOVERY_EVENTS
              or not _recovery_event_details(records[0], record["event"], record.get("details"))):
            raise ValueError
        previous = record["record_sha256"]
    if not records or records[0].get("record_type") != "recovery-baseline":
        raise ValueError
    if (records[0]["control_identity_sha256"] != hashlib.sha256(str(root.parent).encode()).hexdigest()
            or records[0]["control_filesystem_sha256"] != _control_filesystem_sha256(root.parent)):
        raise ValueError
    _validate_recovery_transitions(records[0], records)
    return records


def _recovery_journal_records(path: Path, root: Path) -> tuple[dict[str, Any], ...]:
    """Read an existing recovery journal; R0 deliberately has no writer."""
    try:
        return _parse_recovery_journal_bytes(
            _read_regular_bytes_readonly(path, "canonical recovery artifacts"), path, root,
        )
    except ConfigError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def create_recovery_journal(control_root: Path, baseline: dict[str, Any]) -> RecoveryJournalRef:
    """Durably create an R1 checkpoint chain; production R1 does not call this yet."""
    temporary: Path | None = None
    temporary_token: OwnedFileToken | None = None
    try:
        required = {
            "original_operation_id", "original_operation", "target_sha", "expected_base_sha",
            "original_journal_final_record_sha256", "snapshot_id", "snapshot_manifest_sha256",
            "snapshot_input_sha256", "plan_hash", "binding_digest_sha256", "lineage_root_sha",
            "control_identity_sha256", "control_filesystem_sha256", "target_ref",
            "confirmed_observed_sha", "action",
        }
        if set(baseline) != required:
            raise ValueError
        if (baseline.get("control_identity_sha256") != hashlib.sha256(str(control_root).encode()).hexdigest()
                or baseline.get("control_filesystem_sha256") != _control_filesystem_sha256(control_root)):
            raise ValueError
        operation_id = _artifact_id()
        root = _ensure_artifact_root(control_root, "recovery-journal")
        final = root / f"{operation_id}.jsonl"
        temporary = root / f".{operation_id}.tmp"
        record = _journal_record({
            "schema": RECOVERY_JOURNAL_SCHEMA, "record_type": "recovery-baseline", "sequence": 0,
            "operation_id": operation_id, **baseline, "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "previous_record_sha256": None,
        })
        if not _recovery_baseline_valid(record, final) or final.exists() or temporary.exists():
            raise ValueError
        temporary_token = _write_owned(temporary, _canonical_bytes(record) + b"\n")
        _finalize_owned_file(temporary, final, temporary_token)
        return RecoveryJournalRef(operation_id, final, control_root, 0, record["record_sha256"],
                                  record["control_identity_sha256"])
    except (ConfigError, OSError, TypeError, ValueError):
        if temporary is not None and temporary_token is not None:
            _unlink_owned_file(temporary, temporary_token, links=1)
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def append_recovery_journal_event(journal: RecoveryJournalRef, event: str,
                                  details: dict[str, str] | None = None) -> RecoveryJournalRef:
    """Durably append one validated R1 checkpoint; production R1 does not call this yet."""
    try:
        root = _artifact_root(journal.control_root, "recovery-journal")
        if journal.path != root / f"{journal.operation_id}.jsonl":
            raise ValueError
        with _locked_journal(journal.path) as stream:
            stream.seek(0)
            records = _parse_recovery_journal_bytes(stream.read(), journal.path, root)
            if (records[-1]["sequence"] != journal.sequence or records[-1]["record_sha256"] != journal.record_sha256
                    or records[0]["control_identity_sha256"] != journal.control_identity_sha256):
                raise ValueError
            record = _journal_record({
                "schema": RECOVERY_JOURNAL_SCHEMA, "record_type": "event", "sequence": journal.sequence + 1,
                "event": event, "utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "details": details or {}, "previous_record_sha256": journal.record_sha256,
            })
            _parse_recovery_journal_bytes(
                _canonical_bytes(records[0]) + b"\n" + b"\n".join(_canonical_bytes(item) for item in records[1:] + (record,)) + b"\n",
                journal.path, root,
            )
            stream.seek(0, os.SEEK_END)
            stream.write(_canonical_bytes(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return RecoveryJournalRef(journal.operation_id, journal.path, journal.control_root, record["sequence"],
                                  record["record_sha256"], journal.control_identity_sha256)
    except (ConfigError, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def _recovery_status_detail(records: tuple[dict[str, Any], ...], *, completed: bool) -> str:
    action = records[0]["action"]
    step_record = records[-2] if not completed and records[-1].get("event") == "failed" else records[-1]
    step = step_record.get("event", "baseline")
    prefix = "canonical recovery completed" if completed else "canonical recovery interrupted"
    return f"{prefix} action={action} step={step}"


def _recovery_public_detail(code: str, detail: str) -> str | None:
    if (code == "FAIL_RECOVERY_CLEANUP_NOT_ACTIONABLE"
            and detail == "canonical recovery worktree residue"):
        return detail
    match = re.fullmatch(
        r"canonical recovery (interrupted|completed) action=([a-z-]+) step=([a-z-]+|baseline)", detail,
    )
    if match is None or match.group(2) not in RECOVERY_ACTIONS or match.group(3) not in RECOVERY_EVENTS | {"baseline"}:
        return None
    if code == "FAIL_RECOVERY_INTERRUPTED" and match.group(1) == "interrupted":
        return detail
    if code == "FAIL_RECOVERY_ALREADY_COMPLETED" and match.group(1) == "completed":
        return detail
    return None


def _recovery_matches_original(recovery: dict[str, Any], original_records: tuple[dict[str, Any], ...]) -> bool:
    original = original_records[0]
    if original["operation"] not in {"publish", "promote", "advance", "rollback"}:
        return False
    expected_input: str | None = original["input_digest_sha256"] if original["operation"] == "publish" else None
    return all(recovery.get(key) == value for key, value in {
        "original_operation_id": original["operation_id"], "original_operation": original["operation"],
        "target_sha": original["prepared_commit_sha"], "expected_base_sha": original["expected_remote_sha"],
        "original_journal_final_record_sha256": original_records[-1]["record_sha256"],
        "snapshot_id": original["snapshot_id"], "snapshot_manifest_sha256": original["snapshot_manifest_sha256"],
        "snapshot_input_sha256": expected_input, "binding_digest_sha256": original["binding_digest_sha256"],
        "lineage_root_sha": original["lineage_root_sha"],
        "control_identity_sha256": original["control_identity_sha256"],
        "control_filesystem_sha256": original["control_filesystem_sha256"], "target_ref": original["target_ref"],
    }.items())


def _recovery_existing_original(control_root: Path, original_records: tuple[dict[str, Any], ...]) -> None:
    original_operation_id = original_records[0]["operation_id"]
    root = _artifact_root(control_root, "recovery-journal")
    if not os.path.lexists(root):
        return
    try:
        if _is_reparse_alias(root) or not root.is_dir():
            raise ValueError
        matching: list[tuple[dict[str, Any], ...]] = []
        for path in sorted(root.glob("*.jsonl")):
            records = _recovery_journal_records(path, root)
            if records[0]["original_operation_id"] == original_operation_id:
                matching.append(records)
        if any(path.suffix != ".jsonl" for path in root.iterdir()):
            raise ValueError
        if len(matching) > 1:
            raise ValueError
        if matching and not _recovery_matches_original(matching[0][0], original_records):
            raise ValueError
        if any(records[-1].get("event") in {"completed", "cleanup-pending"} for records in matching):
            selected = next(records for records in matching if records[-1].get("event") in {"completed", "cleanup-pending"})
            raise ConfigError("FAIL_RECOVERY_ALREADY_COMPLETED", _recovery_status_detail(selected, completed=True))
        if matching:
            raise ConfigError("FAIL_RECOVERY_INTERRUPTED", _recovery_status_detail(matching[0], completed=False))
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def _recovery_original_journal(control_root: Path, target_sha: str) -> tuple[Path, tuple[dict[str, Any], ...]]:
    root = _artifact_root(control_root, "journal")
    if not os.path.lexists(root):
        raise ConfigError("FAIL_RECOVERY_NOT_FOUND", "canonical recovery source")
    try:
        if _is_reparse_alias(root) or not root.is_dir():
            raise ValueError
        matches: list[tuple[Path, tuple[dict[str, Any], ...]]] = []
        for path in sorted(root.glob("*.jsonl")):
            records = _read_journal_readonly(path, root)
            baseline = records[0]
            if (baseline["operation"] in {"publish", "promote", "advance", "rollback"}
                    and baseline["prepared_commit_sha"] == target_sha):
                matches.append((path, records))
        if any(path.suffix != ".jsonl" for path in root.iterdir()):
            raise ValueError
    except ConfigError as exc:
        if exc.code.startswith("FAIL_RECOVERY_"):
            raise
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None
    if not matches:
        raise ConfigError("FAIL_RECOVERY_NOT_FOUND", "canonical recovery source")
    if len(matches) != 1:
        raise ConfigError("FAIL_RECOVERY_AMBIGUOUS", "canonical recovery source")
    return matches[0]


def _recovery_snapshot(context: RepositoryContext, control_root: Path,
                       records: tuple[dict[str, Any], ...]) -> tuple[SnapshotRef, dict[str, Any]]:
    baseline = records[0]
    snapshot = SnapshotRef(
        baseline["snapshot_id"], _artifact_root(control_root, "snapshots") / baseline["snapshot_id"],
        baseline["snapshot_manifest_sha256"],
    )
    try:
        payload = _validate_snapshot_material(snapshot)
        required = ("operation", "candidate_id", "expected_remote_sha", "prepared_commit_sha",
                    "prepared_tree_oid", "plan_hash", "input_digest_sha256", "binding_digest_sha256",
                    "target_ref", "snapshot_id")
        if (any(payload.get(key) != baseline.get(key) for key in required)
                or payload.get("manifest_sha256") != baseline["snapshot_manifest_sha256"]):
            raise ValueError
        parents = _git(context.repo_root, "rev-list", "--parents", "-n", "1", baseline["prepared_commit_sha"]).stdout.strip().split()
        tree = _git(context.repo_root, "rev-parse", f"{baseline['prepared_commit_sha']}^{{tree}}").stdout.strip()
        if parents != [baseline["prepared_commit_sha"], baseline["expected_remote_sha"]] or tree != baseline["prepared_tree_oid"]:
            raise ValueError
        changes = _parse_name_status(_git_bytes(
            context.repo_root, "diff", "--no-renames", "--name-status", "-z",
            baseline["expected_remote_sha"], baseline["prepared_commit_sha"],
        ))
        expected: list[tuple[str, str]] = []
        for item in payload["files"]:
            before, after = item["before"], item["after"]
            status = "A" if before == {"exists": False} else "D" if after == {"exists": False} else "M"
            path = item["path"]
            if not path.startswith(context.state_prefix.rstrip("/") + "/"):
                raise ValueError
            expected.append((status, path))
            for sha, fact in ((baseline["expected_remote_sha"], before), (baseline["prepared_commit_sha"], after)):
                tree_fact = _tree_blob(context.repo_root, sha, path)
                if fact == {"exists": False}:
                    if tree_fact is not None:
                        raise ValueError
                elif tree_fact is None or tree_fact[0] != "100644" or tree_fact[1] != fact["oid"] or tree_fact[2] != (snapshot.path / fact["data"]).read_bytes():
                    raise ValueError
        if set(changes) != set(expected) or len(changes) != len(expected) or not expected:
            raise ValueError
        _assert_engine_tree_unchanged(context.repo_root, baseline["expected_remote_sha"], baseline["prepared_commit_sha"])
        return snapshot, payload
    except ConfigError:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None
    except (OSError, UnicodeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def _recovery_quarantine_valid(context: RepositoryContext, baseline: dict[str, Any],
                               snapshot: SnapshotRef, manifest: dict[str, Any]) -> bool:
    if baseline["operation"] != "publish" or not isinstance(manifest.get("local_input"), dict):
        return False
    try:
        root = _quarantine_root(context.repo_root)
        operation = root / baseline["operation_id"]
        target = operation / f"{baseline['candidate_id']}.md"
        if (not root.exists() or _quarantine_directory_identity(root) is None
                or _quarantine_directory_identity(operation) is None
                or target.parent != operation or target.name != f"{baseline['candidate_id']}.md"):
            return False
        _target_dev, _target_ino, _target_size, target_sha256 = _quarantine_file_identity(target)
        raw = target.read_bytes()
        local = manifest["local_input"]
        if (target_sha256 != local["sha256"] or hashlib.sha256(raw).hexdigest() != local["sha256"]
                or raw != (snapshot.path / local["data"]).read_bytes()):
            return False
        blob = _tree_blob(context.repo_root, baseline["prepared_commit_sha"],
                          context.state_to_repo_path(INBOX / f"{baseline['candidate_id']}.md"))
        return blob is not None and blob[2] == raw
    except (ConfigError, OSError, UnicodeError, TypeError, ValueError):
        return False


def _recovery_action(context: RepositoryContext, baseline: dict[str, Any],
                     records: tuple[dict[str, Any], ...], snapshot: SnapshotRef,
                     manifest: dict[str, Any], observed: str) -> str:
    events = tuple(record for record in records[1:] if record.get("record_type") == "event")
    details = tuple(record.get("details", {}) for record in events)
    codes = {item.get("code") for item in details if isinstance(item, dict)}
    completed = any(record.get("event") == "completed" for record in events)
    cleanup_pending = any(record.get("event") == "cleanup_pending" for record in events)
    if completed and not cleanup_pending:
        raise ConfigError("FAIL_RECOVERY_COMPLETE", "canonical recovery source")
    if "REMOTE_OUTCOME_UNKNOWN" in codes:
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery source")
    if any(record.get("event") == "ancestry_observed" for record in events):
        recorded = next(record["details"].get("sha") for record in reversed(events)
                        if record.get("event") == "ancestry_observed")
        if not isinstance(recorded, str) or not SHA_RE.fullmatch(recorded):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
        if recorded != observed and not _is_ancestor(context.repo_root, recorded, observed):
            raise ConfigError("FAIL_RECOVERY_UNSAFE", "canonical recovery source")
    base, target = baseline["expected_remote_sha"], baseline["prepared_commit_sha"]
    if observed == base:
        state = "base"
    elif observed == target:
        state = "target"
    elif _is_ancestor(context.repo_root, target, observed):
        try:
            validate_canonical_descendant_scope(context.repo_root, target, observed)
        except (ConfigError, OSError, UnicodeError, subprocess.SubprocessError):
            if "REMOTE_COMMITTED_SCOPE_UNVERIFIED" in codes and _recovery_quarantine_valid(context, baseline, snapshot, manifest):
                return "cleanup-only"
            raise ConfigError("FAIL_RECOVERY_SCOPE_UNVERIFIED", "canonical recovery source") from None
        state = "descendant"
    elif _is_ancestor(context.repo_root, base, observed):
        state = "lost-race"
    else:
        raise ConfigError("FAIL_RECOVERY_UNSAFE", "canonical recovery source")
    if "REMOTE_COMMITTED_SCOPE_UNVERIFIED" in codes:
        if _is_ancestor(context.repo_root, target, observed) and _recovery_quarantine_valid(context, baseline, snapshot, manifest):
            return "cleanup-only"
        raise ConfigError("FAIL_RECOVERY_SCOPE_UNVERIFIED", "canonical recovery source")
    pushed = any(record.get("event") == "push_attempt" for record in events)
    if not pushed and state != "base":
        raise ConfigError("FAIL_RECOVERY_UNSAFE", "canonical recovery source")
    if completed and cleanup_pending:
        if baseline["operation"] != "publish":
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
        if state not in {"target", "descendant"}:
            raise ConfigError("FAIL_RECOVERY_UNSAFE", "canonical recovery source")
        return "cleanup-only"
    if state in {"target", "descendant"}:
        return "local-finalization"
    return "input-disposition" if _recovery_quarantine_valid(context, baseline, snapshot, manifest) else "artifact-cleanup"


def _plan_canonical_recovery(context: RepositoryContext, target_sha: str, control_root: Path | None,
                             config_path: Path | None) -> Plan:
    """Create the C2a-R0 canonical recovery plan without local business mutation."""
    if config_path is None:
        raise ConfigError("FAIL_STATE_BINDING", "canonical transaction requires --config")
    control = _canonical_control_root(context, control_root)
    path, records = _recovery_original_journal(control, target_sha)
    baseline = records[0]
    control, binding = _canonical_recovery_binding(
        context, None, config_path, baseline["expected_remote_sha"],
    )
    if _binding_digest(binding) != baseline["binding_digest_sha256"]:
        raise ConfigError("FAIL_STATE_BINDING", "canonical recovery binding changed")
    _recovery_existing_original(control, records)
    snapshot, manifest = _recovery_snapshot(context, control, records)
    events = records[1:]
    cleanup_kind = _canonical_terminal_cleanup_kind(records)
    if cleanup_kind == "worktree":
        if baseline["operation"] != "rollback" or not any(record.get("event") == "completed" for record in events):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
        raise ConfigError("FAIL_RECOVERY_CLEANUP_NOT_ACTIONABLE", "canonical recovery worktree residue")
    if any(record.get("event") == "completed" for record in events) and not any(
            record.get("event") == "cleanup_pending" for record in events):
        raise ConfigError("FAIL_RECOVERY_COMPLETE", "canonical recovery source")
    try:
        observed = observe_canonical_remote_head(context.repo_root)
    except (ConfigError, OSError, UnicodeError, subprocess.SubprocessError):
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery observation") from None
    action = _recovery_action(context, baseline, records, snapshot, manifest, observed)
    # R0 takes no lock or writes.  Re-read the values bound into the plan so a
    # concurrent local artifact rewrite cannot be returned as a review target.
    try:
        stable_path, stable_records = _recovery_original_journal(control, target_sha)
        stable_baseline = stable_records[0]
        stable_control, stable_binding = _canonical_recovery_binding(
            context, None, config_path, stable_baseline["expected_remote_sha"],
        )
        stable_snapshot, _stable_manifest = _recovery_snapshot(context, control, stable_records)
    except Exception:
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state") from None
    _recovery_existing_original(control, stable_records)
    if (stable_control != control or stable_binding != binding or stable_path != path
            or stable_records[-1]["record_sha256"] != records[-1]["record_sha256"]
            or stable_snapshot.manifest_sha256 != snapshot.manifest_sha256):
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery artifacts")
    if action not in RECOVERY_ACTIONS:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery action")
    payload = {
        "operation": "recover", "candidate_id": target_sha, "expected_remote_sha": observed,
        "target_ref": CANONICAL_TARGET_REF, "prepared_commit_sha": target_sha,
        "expected_base_sha": baseline["expected_remote_sha"], "original_operation_id": baseline["operation_id"],
        "original_operation": baseline["operation"], "original_journal_final_record_sha256": records[-1]["record_sha256"],
        "snapshot_id": snapshot.snapshot_id, "snapshot_manifest_sha256": snapshot.manifest_sha256,
        "binding": binding, "binding_digest_sha256": _binding_digest(binding),
        "lineage_root_sha": binding["repository_root_sha"],
        "control_identity_sha256": hashlib.sha256(binding["control_identity"].encode()).hexdigest(),
        "control_filesystem_sha256": binding["control_filesystem_sha256"], "observed_sha": observed,
        "action": action, "review_only": True, "r1_observation_must_equal": True,
        **_plan_context(context, binding),
    }
    digest = _canonical_hash(payload)
    lines = (
        f"PLAN operation=recover candidate={target_sha}",
        f"RECOVERY_SOURCE {baseline['operation_id']}", f"RECOVERY_OBSERVED {observed}",
        f"RECOVERY_ACTION {action}", f"EXPECTED_REMOTE_SHA {observed}", f"PLAN_HASH {digest}",
    )
    return Plan("recover", target_sha, observed, digest, lines, payload)


def plan_canonical_recovery(repo: Path, target_sha: str, control_root: Path | None = None,
                            *, config_path: Path | None) -> Plan:
    """Public R0 boundary: expose only closed, marker-free recovery errors."""
    return _canonical_recovery_raw_boundary(repo, target_sha, control_root, config_path, apply=False)


def _canonical_recovery_planning_stage(context: RepositoryContext, target_sha: str,
                                       config_path: Path) -> Plan:
    """Normalize only R0 planning failures before a recovery apply mutates artifacts."""
    try:
        return _plan_canonical_recovery(context, target_sha, None, config_path)
    except ConfigError as exc:
        if exc.code == "FAIL_RECOVERY_INDETERMINATE":
            raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state") from None
        if exc.code in {"FAIL_STATE_BINDING", "FAIL_INPUT_CHANGED", "FAIL_TRANSACTION_SCOPE"}:
            raise
        if exc.code.startswith("FAIL_RECOVERY_"):
            raise
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state") from None
    except Exception:
        raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state") from None


def _recovery_handle(path: Path) -> tuple[OwnedFileToken, bytes, str]:
    """Read one regular, single-link recovery handle with an identity digest."""
    entry = os.lstat(path)
    if _is_reparse_alias(path) or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError
    with path.open("rb") as stream:
        raw = stream.read()
        observed = os.fstat(stream.fileno())
    token = OwnedFileToken(entry.st_dev, entry.st_ino, entry.st_size)
    if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino, observed.st_size)
            != (token.device, token.inode, token.size) or len(raw) != token.size
            or not _owned_file_matches(path, token, links=1)):
        raise ValueError
    identity = hashlib.sha256(_canonical_bytes({
        "device": token.device, "inode": token.inode, "size": token.size,
    })).hexdigest()
    return token, raw, identity


def _recovery_quarantine_handle(context: RepositoryContext, baseline: dict[str, Any],
                                snapshot: SnapshotRef, manifest: dict[str, Any]) -> RecoveryQuarantine:
    if not _recovery_quarantine_valid(context, baseline, snapshot, manifest):
        raise ValueError
    root = _quarantine_root(context.repo_root)
    operation = root / baseline["operation_id"]
    target = operation / f"{baseline['candidate_id']}.md"
    root_identity = _quarantine_directory_identity(root)
    operation_identity = _quarantine_directory_identity(operation)
    token, raw, identity = _recovery_handle(target)
    if hashlib.sha256(raw).hexdigest() != baseline["input_digest_sha256"]:
        raise ValueError
    return RecoveryQuarantine(root, root_identity, operation, operation_identity, target, token, raw, identity)


def _recovery_remove_quarantine(quarantine: RecoveryQuarantine) -> bool:
    if (not _owned_file_matches(quarantine.target, quarantine.token, links=1)
            or _quarantine_directory_identity(quarantine.root) != quarantine.root_identity
            or _quarantine_directory_identity(quarantine.operation) != quarantine.operation_identity
            or not _unlink_owned_file(quarantine.target, quarantine.token, links=1)):
        return False
    try:
        if (_quarantine_directory_identity(quarantine.root) != quarantine.root_identity
                or _quarantine_directory_identity(quarantine.operation) != quarantine.operation_identity):
            return False
        quarantine.operation.rmdir()
        return _quarantine_directory_identity(quarantine.root) == quarantine.root_identity
    except (OSError, ValueError):
        return False


def _recovery_postconvergence_worktrees(repo: Path, control: Path, target_sha: str,
                                        frozen: tuple[Path, ...], journal: RecoveryJournalRef) -> RecoveryJournalRef:
    if _recovery_worktrees(repo, control, target_sha) != frozen:
        raise ValueError
    for txn in frozen:
        identity = hashlib.sha256(str(txn).encode()).hexdigest()
        details = {"role": "worktree-cleanup", "handle_identity_sha256": identity}
        journal = _recovery_append(journal, "worktree-cleanup-intent", details)
        _git(repo, "worktree", "remove", str(txn))
        if os.path.lexists(txn):
            raise ValueError
        journal = _recovery_append(journal, "worktree-cleaned", details)
    return journal


def _recovery_pending_kind(quarantine: RecoveryQuarantine | None,
                           worktrees: tuple[Path, ...]) -> str | None:
    if quarantine is not None and (os.path.lexists(quarantine.target) or os.path.lexists(quarantine.operation)):
        return "quarantine-delete"
    if any(os.path.lexists(txn) for txn in worktrees):
        return "worktree-cleanup"
    return None


def _recovery_quarantine_local_source(context: RepositoryContext, baseline: dict[str, Any], source: Path,
                                      token: OwnedFileToken, raw: bytes, identity: str) -> RecoveryQuarantine:
    root = _quarantine_root(context.repo_root)
    root.mkdir(mode=0o700, exist_ok=True)
    root_identity = _quarantine_directory_identity(root)
    operation = root / baseline["original_operation_id"]
    target = operation / f"{baseline['target_sha'][:12]}.input"
    if os.path.lexists(operation) or os.path.lexists(target) or os.stat(source.parent).st_dev != root_identity[0]:
        raise ValueError
    os.mkdir(operation, mode=0o700)
    operation_identity = _quarantine_directory_identity(operation)
    if (not _owned_file_matches(source, token, links=1) or hashlib.sha256(raw).hexdigest() != baseline["snapshot_input_sha256"]
            or operation.parent != root):
        raise ValueError
    os.link(source, target)
    if not _owned_file_matches(source, token, links=2) or not _unlink_owned_file(source, token, links=2):
        raise ValueError
    if not _owned_file_matches(target, token, links=1):
        raise ValueError
    return RecoveryQuarantine(root, root_identity, operation, operation_identity, target, token, raw, identity)


def _recovery_worktrees(repo: Path, control_root: Path, target_sha: str) -> tuple[Path, ...]:
    root = control_root.resolve() / "txn"
    if not os.path.lexists(root):
        return ()
    if _is_reparse_alias(root) or not root.is_dir():
        raise ValueError
    result = _git(repo, "worktree", "list", "--porcelain")
    paths: list[Path] = []
    active: Path | None = None
    head: str | None = None
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            active = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("HEAD "):
            head = line.removeprefix("HEAD ")
        elif not line and active is not None:
            try:
                active.relative_to(root.resolve())
            except ValueError:
                pass
            else:
                if head != target_sha:
                    raise ValueError
                if _git(active, "status", "--porcelain=v1", "--untracked-files=all").stdout:
                    raise ValueError
                if _git(active, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0:
                    raise ValueError
                paths.append(active)
            active, head = None, None
    if len(paths) > 1:
        raise ValueError
    return tuple(paths)


def _recovery_local_finalization_ready(repo: Path, observed: str, source: Path | None) -> None:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    allowed = () if source is None else (f"?? {source.relative_to(repo).as_posix()}",)
    if tuple(status) != allowed:
        raise ValueError
    branch = _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if branch.returncode != 0 or branch.stdout.strip() != CANONICAL_TARGET_REF:
        raise ValueError
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if not SHA_RE.fullmatch(head) or not _is_ancestor(repo, head, observed):
        raise ValueError


def _recovery_apply_baseline(plan: Plan, original: dict[str, Any]) -> dict[str, Any]:
    payload = plan.payload
    required = {
        "original_operation_id", "original_operation", "prepared_commit_sha", "expected_base_sha",
        "original_journal_final_record_sha256", "snapshot_id", "snapshot_manifest_sha256",
        "binding_digest_sha256", "lineage_root_sha", "control_identity_sha256",
        "control_filesystem_sha256", "target_ref", "observed_sha", "action",
    }
    if any(key not in payload for key in required):
        raise ValueError
    return {
        "original_operation_id": payload["original_operation_id"], "original_operation": payload["original_operation"],
        "target_sha": payload["prepared_commit_sha"], "expected_base_sha": payload["expected_base_sha"],
        "original_journal_final_record_sha256": payload["original_journal_final_record_sha256"],
        "snapshot_id": payload["snapshot_id"], "snapshot_manifest_sha256": payload["snapshot_manifest_sha256"],
        "snapshot_input_sha256": original["input_digest_sha256"] if payload["original_operation"] == "publish" else None,
        "plan_hash": plan.plan_hash, "binding_digest_sha256": payload["binding_digest_sha256"],
        "lineage_root_sha": payload["lineage_root_sha"], "control_identity_sha256": payload["control_identity_sha256"],
        "control_filesystem_sha256": payload["control_filesystem_sha256"], "target_ref": payload["target_ref"],
        "confirmed_observed_sha": payload["observed_sha"], "action": payload["action"],
    }


def _recovery_append_failure(journal: RecoveryJournalRef, *, reason: str) -> None:
    try:
        append_recovery_journal_event(journal, "failed", {"site": "action", "reason": reason})
    except Exception:
        pass


def _recovery_append(journal: RecoveryJournalRef, event: str, details: dict[str, str]) -> RecoveryJournalRef:
    return append_recovery_journal_event(journal, event, details)


def _recovery_file_details(role: str, input_sha256: str, identity: str) -> dict[str, str]:
    return {"role": role, "input_sha256": input_sha256, "handle_identity_sha256": identity}


def _apply_canonical_recovery_locked(context: RepositoryContext, target_sha: str, plan_hash: str,
                                     expected_observed_sha: str, config_path: Path) -> RecoveryResult:
    plan = _canonical_recovery_planning_stage(context, target_sha, config_path)
    if plan.plan_hash != plan_hash or plan.expected_remote_sha != expected_observed_sha:
        raise ConfigError("FAIL_INPUT_CHANGED", "canonical recovery plan")
    control = _canonical_control_root(context, None)
    _path, original_records = _recovery_original_journal(control, target_sha)
    snapshot, manifest = _recovery_snapshot(context, control, original_records)
    original = original_records[0]
    baseline = _recovery_apply_baseline(plan, original)
    if not _recovery_matches_original({**baseline, "target_sha": baseline["target_sha"]}, original_records):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
    action = baseline["action"]
    retained_rollback_worktree = original["operation"] == "rollback"
    if action not in RECOVERY_ACTIONS or plan.payload.get("review_only") is not True:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
    worktrees = _recovery_worktrees(context.repo_root, control, target_sha)
    if retained_rollback_worktree and len(worktrees) != 1:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
    quarantine: RecoveryQuarantine | None = None
    source: Path | None = None
    local_source: tuple[OwnedFileToken, bytes, str] | None = None
    if action in {"cleanup-only", "input-disposition"}:
        quarantine = _recovery_quarantine_handle(context, original, snapshot, manifest)
    if action == "input-disposition":
        source = _safe_state_path(context, INBOX / f"{original['candidate_id']}.md")
        if os.path.lexists(source):
            source_token, source_raw, source_identity = _recovery_handle(source)
            if quarantine is None or source_raw != quarantine.raw or source_raw != (snapshot.path / manifest["local_input"]["data"]).read_bytes():
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
            del source_token, source_identity
        elif os.stat(source.parent).st_dev != quarantine.token.device:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
    if action == "local-finalization":
        if original["operation"] == "publish":
            source = _safe_state_path(context, INBOX / f"{original['candidate_id']}.md")
            if os.path.lexists(source):
                local_source = _recovery_handle(source)
                expected = (snapshot.path / manifest["local_input"]["data"]).read_bytes()
                if local_source[1] != expected or hashlib.sha256(local_source[1]).hexdigest() != original["input_digest_sha256"]:
                    raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")
        _recovery_local_finalization_ready(context.repo_root, expected_observed_sha, source if local_source else None)
    journal = create_recovery_journal(control, baseline)
    converged = False
    local_quarantine: RecoveryQuarantine | None = None
    try:
        if action == "artifact-cleanup":
            if not retained_rollback_worktree:
                for txn in worktrees:
                    token = hashlib.sha256(str(txn).encode()).hexdigest()
                    journal = _recovery_append(journal, "worktree-cleanup-intent", {"role": "worktree-cleanup", "handle_identity_sha256": token})
                    _git(context.repo_root, "worktree", "remove", str(txn))
                    if os.path.lexists(txn):
                        raise ValueError
                    journal = _recovery_append(journal, "worktree-cleaned", {"role": "worktree-cleanup", "handle_identity_sha256": token})
        elif action == "cleanup-only":
            assert quarantine is not None
            if not _recovery_quarantine_valid(context, original, snapshot, manifest):
                raise ValueError
            details = _recovery_file_details("quarantine-delete", baseline["snapshot_input_sha256"], quarantine.handle_identity_sha256)
            journal = _recovery_append(journal, "quarantine-delete-intent", details)
            if not _recovery_quarantine_valid(context, original, snapshot, manifest) or not _recovery_remove_quarantine(quarantine):
                raise ValueError
            journal = _recovery_append(journal, "quarantine-deleted", details)
        elif action == "input-disposition":
            assert quarantine is not None and source is not None
            target, token, raw, identity = quarantine.target, quarantine.token, quarantine.raw, quarantine.handle_identity_sha256
            if os.path.lexists(source):
                source_token, source_raw, source_identity = _recovery_handle(source)
                if source_raw != raw or not _owned_file_matches(target, token, links=1):
                    raise ValueError
                del source_token
                details = _recovery_file_details("source-preserved", baseline["snapshot_input_sha256"], source_identity)
                journal = _recovery_append(journal, "source-preserved", details)
                details = _recovery_file_details("quarantine-delete", baseline["snapshot_input_sha256"], identity)
                journal = _recovery_append(journal, "converged", {})
                converged = True
                pending_kind = "quarantine-delete"
                try:
                    journal = _recovery_append(journal, "quarantine-delete-intent", details)
                    if not _recovery_remove_quarantine(quarantine):
                        raise ValueError
                    journal = _recovery_append(journal, "quarantine-deleted", details)
                    pending_kind = "worktree-cleanup"
                    journal = _recovery_postconvergence_worktrees(
                        context.repo_root, control, target_sha, worktrees, journal,
                    )
                    journal = _recovery_append(journal, "completed", {})
                    return RecoveryResult(action, journal.operation_id, True, False)
                except Exception:
                    pending_kind = _recovery_pending_kind(quarantine, worktrees)
                    if pending_kind is not None:
                        try:
                            journal = _recovery_append(journal, "completed", {})
                            append_recovery_journal_event(journal, "cleanup-pending", {"kind": pending_kind})
                        except Exception:
                            pass
                    return RecoveryResult(
                        action, journal.operation_id, True, pending_kind is not None,
                        "worktree" if pending_kind == "worktree-cleanup" else None,
                    )
            details = _recovery_file_details("source-restore", baseline["snapshot_input_sha256"], identity)
            journal = _recovery_append(journal, "source-restore-intent", details)
            if os.path.lexists(source) or not _owned_file_matches(target, token, links=1):
                raise ValueError
            os.link(target, source)
            if (not _owned_file_matches(target, token, links=2) or not _unlink_owned_file(target, token, links=2)
                    or not _owned_file_matches(source, token, links=1)
                    or _quarantine_directory_identity(quarantine.root) != quarantine.root_identity
                    or _quarantine_directory_identity(quarantine.operation) != quarantine.operation_identity):
                raise ValueError
            quarantine.operation.rmdir()
            if (os.path.lexists(quarantine.operation)
                    or _quarantine_directory_identity(quarantine.root) != quarantine.root_identity):
                raise ValueError
            journal = _recovery_append(journal, "source-restored", details)
        elif action == "local-finalization":
            record_remote_head(control, expected_observed_sha)
            journal = _recovery_append(journal, "pointer-updated", {"role": "pointer", "sha": expected_observed_sha})
            if local_source is not None and source is not None:
                source_token, source_raw, source_identity = local_source
                details = _recovery_file_details("source-quarantine", baseline["snapshot_input_sha256"], source_identity)
                journal = _recovery_append(journal, "source-quarantine-intent", details)
                local_quarantine = _recovery_quarantine_local_source(
                    context, baseline, source, source_token, source_raw, source_identity,
                )
                journal = _recovery_append(journal, "source-quarantined", details)
            journal = _recovery_append(journal, "fast-forward-intent", {"role": "fast-forward", "sha": expected_observed_sha})
            fast_forward_local(context.repo_root, expected_observed_sha)
            if _git(context.repo_root, "rev-parse", "HEAD").stdout.strip() != expected_observed_sha:
                raise ValueError
            journal = _recovery_append(journal, "fast-forward-done", {"role": "fast-forward", "sha": expected_observed_sha})
        journal = _recovery_append(journal, "converged", {})
        converged = True
        if action == "local-finalization" and local_quarantine is not None:
            details = _recovery_file_details("quarantine-delete", baseline["snapshot_input_sha256"],
                                             local_quarantine.handle_identity_sha256)
            journal = _recovery_append(journal, "quarantine-delete-intent", details)
            if not _recovery_remove_quarantine(local_quarantine):
                raise ValueError
            journal = _recovery_append(journal, "quarantine-deleted", details)
        if action in {"input-disposition", "local-finalization"} and not retained_rollback_worktree:
            journal = _recovery_postconvergence_worktrees(
                context.repo_root, control, target_sha, worktrees, journal,
            )
        if retained_rollback_worktree and worktrees:
            # Rollback worktrees are explicit, inert residue.  Record their
            # retained cleanup state without calling any remover.
            txn = worktrees[0]
            identity = hashlib.sha256(str(txn).encode()).hexdigest()
            journal = _recovery_append(
                journal, "worktree-cleanup-intent",
                {"role": "worktree-cleanup", "handle_identity_sha256": identity},
            )
            journal = _recovery_append(journal, "completed", {})
            append_recovery_journal_event(journal, "cleanup-pending", {"kind": "worktree-cleanup"})
            return RecoveryResult(action, journal.operation_id, True, True, "worktree")
        journal = _recovery_append(journal, "completed", {})
        return RecoveryResult(action, journal.operation_id, True, False)
    except Exception:
        if converged:
            pending_kind = _recovery_pending_kind(local_quarantine or quarantine, worktrees)
            if pending_kind is not None:
                try:
                    journal = _recovery_append(journal, "completed", {})
                    append_recovery_journal_event(journal, "cleanup-pending", {"kind": pending_kind})
                except Exception:
                    pass
            return RecoveryResult(
                action, journal.operation_id, True, pending_kind is not None,
                "worktree" if pending_kind == "worktree-cleanup" else None,
            )
        _recovery_append_failure(journal, reason="validation")
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def apply_canonical_recovery(repo: Path, target_sha: str, plan_hash: str, expected_observed_sha: str,
                             control_root: Path | None = None, *, config_path: Path | None) -> RecoveryResult:
    """Apply exactly one reviewed R0 canonical recovery plan; this never pushes."""
    return _canonical_recovery_raw_boundary(
        repo, target_sha, control_root, config_path, apply=True, plan_hash=plan_hash,
        expected_observed_sha=expected_observed_sha,
    )


def _canonical_recovery_raw_boundary(repo: Path, target_sha: str, control_root: Path | None,
                                     config_path: Path | None, *, apply: bool,
                                     plan_hash: str | None = None,
                                     expected_observed_sha: str | None = None) -> Plan | RecoveryResult:
    if config_path is None or control_root is not None:
        raise ConfigError("FAIL_STATE_BINDING", "canonical recovery binding")
    if not _canonical_recovery_request_valid(target_sha):
        raise ConfigError("FAIL_REMOTE_SHA", "canonical recovery target")
    if apply and not _canonical_recovery_request_valid(target_sha, plan_hash, expected_observed_sha):
        raise ConfigError("FAIL_INPUT_CHANGED", "canonical recovery plan")
    try:
        context = resolve_repository_context(repo)
    except Exception:
        raise ConfigError("FAIL_STATE_REPOSITORY", "canonical recovery repository") from None
    if context.layout != "canonical":
        raise ConfigError("FAIL_STATE_REPOSITORY", "canonical recovery repository") from None
    try:
        if not apply:
            return _canonical_recovery_planning_stage(context, target_sha, config_path)
        with operation_lock(_canonical_control_root(context, None)):
            return _apply_canonical_recovery_locked(context, target_sha, plan_hash, expected_observed_sha, config_path)
    except ConfigError as exc:
        if exc.code == "FAIL_STATE_BINDING":
            raise ConfigError("FAIL_STATE_BINDING", "canonical recovery binding") from None
        if exc.code == "FAIL_INPUT_CHANGED":
            raise ConfigError("FAIL_INPUT_CHANGED", "canonical recovery plan") from None
        if exc.code == "FAIL_RECOVERY_INDETERMINATE":
            raise ConfigError("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state") from None
        if exc.code.startswith("FAIL_RECOVERY_"):
            detail = _recovery_public_detail(exc.code, exc.detail) or "canonical recovery source"
            raise ConfigError(exc.code, detail) from None
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None
    except Exception:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts") from None


def _canonical_recovery_request_valid(target_sha: Any, plan_hash: Any | None = None,
                                      expected_observed_sha: Any | None = None) -> bool:
    if not isinstance(target_sha, str) or SHA_RE.fullmatch(target_sha) is None:
        return False
    if plan_hash is None and expected_observed_sha is None:
        return True
    return (isinstance(plan_hash, str) and SHA256_RE.fullmatch(plan_hash) is not None
            and isinstance(expected_observed_sha, str) and SHA_RE.fullmatch(expected_observed_sha) is not None)


def _recover_local_context(context: RepositoryContext, sha: str | None,
                           control_root: Path | None, config_path: Path | None) -> tuple[str, str]:
    if context.layout == "canonical":
        if config_path is None:
            raise ConfigError("FAIL_STATE_BINDING", "canonical transaction requires --config")
        _canonical_control_root(context, control_root)
        raise ConfigError("FAIL_CANONICAL_TRANSACTION_PENDING", "recover")
    root = (control_root or Path.home() / ".agent-core").resolve()
    with operation_lock(root):
        source = "--sha"
        if sha is None:
            sha = _load_last_committed(root)["sha"]
            source = str(_last_committed_path(root))
        if not SHA_RE.fullmatch(sha):
            raise ConfigError("FAIL_REMOTE_SHA", sha)
        _git(context.repo_root, "fetch", "origin", "--quiet")
        remote = _git(context.repo_root, "rev-parse", "origin/main").stdout.strip()
        if remote != sha and not _is_ancestor(context.repo_root, sha, remote):
            raise ConfigError("FAIL_REMOTE_SHA", f"expected={sha} actual={remote}")
        fast_forward_local(context.repo_root, remote)
        _clear_last_committed(root, sha)
    return sha, source


def dispatch_recovery(repo: Path, sha: str | None, control_root: Path | None, *, apply: bool,
                      plan_hash: str | None, expected_observed_sha: str | None,
                      config_path: Path | None, requested_mode: str) -> CanonicalPlanDispatch | CanonicalApplyDispatch | StandaloneDispatch:
    """Resolve recovery layout once and dispatch the closed public recovery variants."""
    if requested_mode not in {"canonical", "standalone"}:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "recovery mode")
    if requested_mode == "standalone" and (config_path is not None or plan_hash is not None or expected_observed_sha is not None):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "recovery mode")
    if requested_mode == "canonical":
        result = _canonical_recovery_raw_boundary(
            repo, sha, control_root, config_path, apply=apply, plan_hash=plan_hash,
            expected_observed_sha=expected_observed_sha,
        )
        return CanonicalApplyDispatch(result) if apply else CanonicalPlanDispatch(result)
    try:
        context = resolve_repository_context(repo)
    except Exception:
        raise ConfigError("FAIL_STATE_REPOSITORY", "canonical recovery repository") from None
    if context.layout != "standalone":
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "recovery mode")
    try:
        recovered_sha, source = _recover_local_context(context, sha, control_root, None)
        return StandaloneDispatch(recovered_sha, source)
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "recovery mode") from None


def recover_local(repo: Path, sha: str | None = None,
                  control_root: Path | None = None, *, config_path: Path | None = None) -> tuple[str, str]:
    context = resolve_repository_context(repo)
    return _recover_local_context(context, sha, control_root, config_path)


def _canonical_rollback_original_journal(control_root: Path, snapshot_id: str) -> tuple[Path, tuple[dict[str, Any], ...]]:
    """Locate exactly one immutable original journal for a canonical snapshot."""
    root = _artifact_root(control_root, "journal")
    if not os.path.lexists(root):
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts")
    try:
        if _is_reparse_alias(root) or not root.is_dir():
            raise ValueError
        matches: list[tuple[Path, tuple[dict[str, Any], ...]]] = []
        for path in sorted(root.glob("*.jsonl")):
            records = _read_journal_readonly(path, root)
            if records[0]["snapshot_id"] == snapshot_id:
                matches.append((path, records))
        if any(path.suffix != ".jsonl" for path in root.iterdir()) or len(matches) != 1:
            raise ValueError
        return matches[0]
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts") from None


def _canonical_terminal_cleanup_kind(records: tuple[dict[str, Any], ...]) -> str | None:
    """Read the sole terminal canonical cleanup kind without accepting open metadata."""
    pending = tuple(record for record in records[1:] if record.get("event") == "cleanup_pending")
    if not pending:
        return None
    try:
        if len(pending) != 1 or records[-1] is not pending[0]:
            raise ValueError
        details = pending[0].get("details")
        if not _journal_details(details, "cleanup_pending"):
            raise ValueError
        return details["kind"]
    except (KeyError, TypeError, ValueError):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical cleanup pending") from None


def _canonical_rollback_settlement(control_root: Path, original: tuple[dict[str, Any], ...]) -> tuple[str, str]:
    """Return the only settlement evidence that can authorize a forward rollback."""
    events = tuple(record for record in original[1:] if record.get("record_type") == "event")
    cleanup_kind = _canonical_terminal_cleanup_kind(original)
    if cleanup_kind == "quarantine":
        raise ConfigError("FAIL_ROLLBACK_CLEANUP_PENDING", "canonical rollback cleanup required")
    if any(record.get("details", {}).get("code") == "REMOTE_COMMITTED_SCOPE_UNVERIFIED" for record in events):
        raise ConfigError("FAIL_ROLLBACK_SCOPE_UNVERIFIED", "canonical rollback scope unverified")
    if cleanup_kind == "worktree":
        if original[0]["operation"] != "rollback" or not any(record.get("event") == "completed" for record in events):
            raise ConfigError("FAIL_ROLLBACK_CLEANUP_PENDING", "canonical rollback cleanup required")
        return "original-completed", original[-1]["record_sha256"]
    if any(record.get("event") == "completed" for record in events):
        return "original-completed", original[-1]["record_sha256"]
    root = _artifact_root(control_root, "recovery-journal")
    if not os.path.lexists(root):
        raise ConfigError("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete")
    try:
        if _is_reparse_alias(root) or not root.is_dir():
            raise ValueError
        matches: list[tuple[dict[str, Any], ...]] = []
        for path in sorted(root.glob("*.jsonl")):
            records = _recovery_journal_records(path, root)
            if records[0]["original_operation_id"] == original[0]["operation_id"]:
                if not _recovery_matches_original(records[0], original):
                    raise ValueError
                matches.append(records)
        if any(path.suffix != ".jsonl" for path in root.iterdir()) or len(matches) != 1:
            raise ConfigError("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete")
        recovery = matches[0]
        terminal = recovery[-1]
        worktree_pending = (
            terminal.get("event") == "cleanup-pending"
            and terminal.get("details") == {"kind": "worktree-cleanup"}
            and len(recovery) >= 2 and recovery[-2].get("event") == "completed"
        )
        if (recovery[0]["action"] != "local-finalization"
                or recovery[0]["confirmed_observed_sha"] != original[0]["prepared_commit_sha"]):
            raise ConfigError("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete")
        if original[0]["operation"] == "rollback":
            if not worktree_pending:
                raise ConfigError("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete")
            return "recovery-local-finalization", terminal["record_sha256"]
        if terminal.get("event") != "completed" or worktree_pending:
            raise ConfigError("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete")
        return "recovery-local-finalization", terminal["record_sha256"]
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts") from None


def _canonical_rollback_binding(context: RepositoryContext, config_path: Path,
                                original: dict[str, Any], manifest: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    try:
        control, binding = _canonical_recovery_binding(
            context, None, config_path, original["expected_remote_sha"],
        )
        if (_binding_digest(binding) != original["binding_digest_sha256"]
                or manifest["binding_digest_sha256"] != original["binding_digest_sha256"]
                or binding["repository_root_sha"] != original["lineage_root_sha"]
                or hashlib.sha256(binding["control_identity"].encode()).hexdigest() != original["control_identity_sha256"]
                or binding["control_filesystem_sha256"] != original["control_filesystem_sha256"]):
            raise ValueError
        return control, binding
    except Exception:
        raise ConfigError("FAIL_ROLLBACK_BINDING_CHANGED", "canonical rollback binding changed") from None


def _canonical_rollback_restore_facts(manifest: dict[str, Any]) -> tuple[tuple[dict[str, Any], ...], str]:
    """Bind the exact snapshot restore set without exposing its paths in a public plan."""
    try:
        def projection(value: dict[str, Any]) -> dict[str, Any]:
            if value == {"exists": False}:
                return {"exists": False}
            return {key: value[key] for key in ("exists", "mode", "oid", "sha256")}

        facts: list[dict[str, Any]] = []
        for item in manifest["files"]:
            before, after = item["before"], item["after"]
            # A rollback starts at the original transaction target (`after`) and
            # restores its expected base (`before`), so its direction is the
            # inverse of the source transaction's diff.
            status = "D" if before == {"exists": False} else "A" if after == {"exists": False} else "M"
            facts.append({"path": item["path"], "restore_status": status,
                          "from": projection(after), "to": projection(before)})
        if not facts:
            raise ValueError
        return tuple(facts), _canonical_hash({"restore": facts})
    except (KeyError, TypeError, ValueError):
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts") from None


def _canonical_rollback_proof(context: RepositoryContext, snapshot_id: str,
                              config_path: Path) -> tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                                                           dict[str, str], str, str, tuple[dict[str, Any], ...], str,
                                                           dict[str, Any]]:
    """Rebuild every local fact a rollback plan may bind, without remote I/O."""
    control = _canonical_control_root(context, None)
    _path, original_records = _canonical_rollback_original_journal(control, snapshot_id)
    original = original_records[0]
    if original["operation"] == "rollback" and ARTIFACT_ID_RE.fullmatch(original["candidate_id"]) is None:
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts")
    snapshot, manifest = _recovery_snapshot(context, control, original_records)
    if (snapshot.snapshot_id != snapshot_id
            or (original["operation"] == "rollback" and manifest["candidate_id"] != original["candidate_id"])):
        raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts")
    control, binding = _canonical_rollback_binding(context, config_path, original, manifest)
    settlement_kind, settlement_record_sha = _canonical_rollback_settlement(control, original_records)
    facts, facts_digest = _canonical_rollback_restore_facts(manifest)
    return (control, original_records, snapshot, binding, settlement_kind, settlement_record_sha, facts,
            facts_digest, manifest)


def _canonical_rollback_proof_identity(proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                                                    dict[str, str], str, str, tuple[dict[str, Any], ...], str,
                                                    dict[str, Any]]) -> tuple[Any, ...]:
    control, records, snapshot, binding, settlement_kind, settlement_record_sha, facts, facts_digest, manifest = proof
    original = records[0]
    return (
        str(control), original["operation_id"], original["operation"], original["expected_remote_sha"],
        original["prepared_commit_sha"], original["prepared_tree_oid"], original["target_ref"],
        records[-1]["record_sha256"], snapshot.snapshot_id, snapshot.manifest_sha256,
        settlement_kind, settlement_record_sha, facts_digest, _canonical_hash({"facts": list(facts)}),
        manifest["manifest_sha256"], _binding_digest(binding),
        binding["repository_root_sha"], hashlib.sha256(binding["control_identity"].encode()).hexdigest(),
        binding["control_filesystem_sha256"],
    )


def _rollback_plan_from_proof(proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                                                  dict[str, str], str, str, tuple[dict[str, Any], ...], str,
                                                  dict[str, Any]], observed: str) -> Plan:
    """Render the public, path-free review plan from already-validated local proof."""
    _control, original_records, snapshot, binding, settlement_kind, settlement_record_sha, facts, facts_digest, _manifest = proof
    original = original_records[0]
    payload = {
        "operation": "rollback", "candidate_id": snapshot.snapshot_id, "expected_remote_sha": observed,
        "target_ref": original["target_ref"], "snapshot_id": snapshot.snapshot_id,
        "snapshot_manifest_sha256": snapshot.manifest_sha256, "original_operation_id": original["operation_id"],
        "original_operation": original["operation"], "original_journal_final_record_sha256": original_records[-1]["record_sha256"],
        "expected_base_sha": original["expected_remote_sha"], "prepared_commit_sha": original["prepared_commit_sha"],
        "prepared_tree_oid": original["prepared_tree_oid"], "settlement_kind": settlement_kind,
        "settlement_record_sha256": settlement_record_sha, "restore_count": len(facts),
        "restore_facts_sha256": facts_digest, "snapshot_sha": snapshot.manifest_sha256,
        "binding_digest_sha256": _binding_digest(binding),
        "lineage_root_sha": binding["repository_root_sha"],
        "control_identity_sha256": hashlib.sha256(binding["control_identity"].encode()).hexdigest(),
        "control_filesystem_sha256": binding["control_filesystem_sha256"], "review_only": True,
    }
    digest = _canonical_hash(payload)
    lines = (
        f"PLAN operation=rollback candidate={snapshot.snapshot_id}", f"ROLLBACK_RESTORE_COUNT {len(facts)}",
        f"ROLLBACK_RESTORE_FACTS {facts_digest}", f"ROLLBACK_SETTLEMENT {settlement_kind}",
        f"EXPECTED_REMOTE_SHA {observed}", f"PLAN_HASH {digest}",
    )
    return Plan("rollback", snapshot.snapshot_id, observed, digest, lines, payload)


def _review_canonical_rollback(context: RepositoryContext, snapshot_id: str,
                               config_path: Path) -> tuple[Plan, tuple[Path, tuple[dict[str, Any], ...], SnapshotRef,
                                                                       dict[str, str], str, str,
                                                                       tuple[dict[str, Any], ...], str,
                                                                       dict[str, Any]]]:
    """One R0 proof/observation/stability cycle, reused by R1a prepare."""
    proof = _canonical_rollback_proof(context, snapshot_id, config_path)
    original = proof[1][0]
    try:
        observed = observe_canonical_remote_head(context.repo_root)
    except Exception:
        raise ConfigError("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state") from None
    if observed != original["prepared_commit_sha"]:
        raise ConfigError("FAIL_ROLLBACK_REMOTE_CHANGED", "canonical rollback remote changed")
    try:
        stable = _canonical_rollback_proof(context, snapshot_id, config_path)
        if _canonical_rollback_proof_identity(stable) != _canonical_rollback_proof_identity(proof):
            raise ValueError
    except Exception:
        raise ConfigError("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state") from None
    return _rollback_plan_from_proof(stable, observed), stable


def _plan_canonical_rollback(context: RepositoryContext, snapshot_id: str, config_path: Path) -> Plan:
    """Create a forward-only rollback review plan without any local mutation."""
    return _review_canonical_rollback(context, snapshot_id, config_path)[0]


def _rollback_prepared_evidence(
    plan: Plan,
    proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef, dict[str, str], str, str,
                 tuple[dict[str, Any], ...], str, dict[str, Any]],
) -> RollbackPreparedEvidence:
    """Freeze only the private facts that R1a will compare under its future lock."""
    _control, records, snapshot, _binding, settlement_kind, settlement_record_sha, facts, facts_digest, manifest = proof
    original = records[0]
    if (plan.operation != "rollback" or plan.candidate_id != snapshot.snapshot_id
            or plan.expected_remote_sha != original["prepared_commit_sha"]
            or plan.payload.get("restore_count") != len(facts)
            or plan.payload.get("restore_facts_sha256") != facts_digest
            or plan.payload.get("snapshot_sha") != snapshot.manifest_sha256):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
    material: list[dict[str, Any]] = []
    for restore, item in zip(facts, manifest["files"], strict=True):
        if restore["path"] != item.get("path"):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        frozen = json.loads(_canonical_bytes(item))
        frozen["restore_status"] = restore["restore_status"]
        material.append(frozen)
    return RollbackPreparedEvidence(
        snapshot.snapshot_id, snapshot.manifest_sha256, original["operation_id"], original["operation"],
        records[-1]["record_sha256"], settlement_kind, settlement_record_sha,
        original["expected_remote_sha"], original["prepared_commit_sha"], original["prepared_tree_oid"],
        len(facts), facts_digest, plan.plan_hash, plan.expected_remote_sha, tuple(material),
    )


def _rollback_evidence_matches(
    evidence: RollbackPreparedEvidence | None, plan: Plan,
    proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef, dict[str, str], str, str,
                 tuple[dict[str, Any], ...], str, dict[str, Any]],
) -> bool:
    try:
        return evidence is not None and evidence == _rollback_prepared_evidence(plan, proof)
    except (ConfigError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _rollback_snapshot_bytes(snapshot: SnapshotRef, fact: dict[str, Any]) -> bytes | None:
    if fact == {"exists": False}:
        return None
    try:
        _name, path = _safe_snapshot_data(snapshot, fact["data"])
        raw = path.read_bytes()
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if (fact.get("exists") is not True or fact.get("mode") != "100644"
                or hashlib.sha256(raw).hexdigest() != fact.get("sha256") or oid != fact.get("oid")):
            raise ValueError
        return raw
    except (ConfigError, OSError, TypeError, ValueError):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback material") from None


def _rollback_tree_matches(repo: Path, sha: str, path: str, fact: dict[str, Any], snapshot: SnapshotRef) -> bool:
    try:
        entry = _tree_blob(repo, sha, path)
        raw = _rollback_snapshot_bytes(snapshot, fact)
        if raw is None:
            return entry is None
        return entry is not None and entry == ("100644", fact["oid"], raw)
    except ConfigError:
        return False


def _rollback_worktree_registered(prepared: Prepared) -> bool:
    """A rollback capsule is valid only while its linked worktree remains registered."""
    try:
        assert_txn_path(prepared.control_root, prepared.txn)
        if _is_reparse_alias(prepared.txn) or not prepared.txn.is_dir():
            return False
        expected_path = prepared.txn.resolve()
        listed = _git(prepared.repo, "worktree", "list", "--porcelain").stdout.splitlines()
        registered = 0
        active: Path | None = None
        listed_head: str | None = None
        for line in listed + [""]:
            if line.startswith("worktree "):
                active, listed_head = Path(line.removeprefix("worktree ")).resolve(), None
            elif line.startswith("HEAD "):
                listed_head = line.removeprefix("HEAD ")
            elif not line and active is not None:
                if active == expected_path:
                    registered += 1
                    if listed_head != prepared.sha:
                        return False
                active, listed_head = None, None
        return (registered == 1
                and _git(prepared.txn, "rev-parse", "HEAD").stdout.strip() == prepared.sha
                and _prepared_tree(prepared.txn, prepared.sha) == prepared.tree_oid
                and not _git(prepared.txn, "status", "--porcelain=v1", "--untracked-files=all").stdout
                and _git(prepared.txn, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0)
    except (ConfigError, OSError, ValueError):
        return False


def _rollback_write_inverse(txn: Path, context: RepositoryContext, evidence: RollbackPreparedEvidence,
                            snapshot: SnapshotRef) -> tuple[ChangeExpectation, ...]:
    txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
    expectations: list[ChangeExpectation] = []
    for item in evidence.inverse_facts:
        path = _normal_git_path(item["path"])
        status = item.get("restore_status")
        before, after = item.get("before"), item.get("after")
        if status not in {"A", "M", "D"} or not isinstance(before, dict) or not isinstance(after, dict):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        if not _rollback_tree_matches(txn, evidence.prepared_target_sha, path, after, snapshot):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        relative = txn_context.repo_to_state_path(path)
        if relative is None:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        target = _safe_state_path(txn_context, Path(relative))
        source_raw = _rollback_snapshot_bytes(snapshot, after)
        if source_raw is not None:
            if (_is_reparse_alias(target) or not target.is_file() or target.stat().st_nlink != 1
                    or target.read_bytes() != source_raw):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        if status == "D":
            if before != {"exists": False} or source_raw is None:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
            target.unlink()
        else:
            raw = _rollback_snapshot_bytes(snapshot, before)
            if raw is None or before.get("mode") != "100644":
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
            _safe_state_path(txn_context, Path(relative).parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _safe_state_path(txn_context, Path(relative))
            if os.path.lexists(target) and (_is_reparse_alias(target) or not target.is_file()):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
            target.write_bytes(raw)
            if (_is_reparse_alias(target) or not target.is_file() or target.stat().st_nlink != 1
                    or target.read_bytes() != raw):
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        expectations.append(ChangeExpectation(status, path, "100644"))
    return tuple(expectations)


def _prepare_rollback_from_reviewed(
        context: RepositoryContext, reviewed: Plan,
        proof: tuple[Path, tuple[dict[str, Any], ...], SnapshotRef, dict[str, str], str, str,
                     tuple[dict[str, Any], ...], str, dict[str, Any]], config_path: Path,
) -> Prepared:
    """Create one rollback capsule from the caller's single reviewed proof."""
    if context.layout != "canonical" or reviewed.operation != "rollback":
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
    control, records, snapshot, binding, _kind, _record_sha, _facts, _facts_digest, _manifest = proof
    evidence = _rollback_prepared_evidence(reviewed, proof)
    txn = _new_worktree(context.repo_root, control, evidence.prepared_target_sha)
    try:
        expectations = _rollback_write_inverse(txn, context, evidence, snapshot)
        changed = tuple(item.path for item in expectations)
        sha = _commit(txn, "rollback: restore state snapshot", evidence.prepared_target_sha, context, expectations)
        prepared = _prepared(
            context.repo_root, control, txn, sha, reviewed, changed, context=context,
            config_path=config_path, binding=binding, rollback_evidence=evidence,
        )
        if (prepared.operation != "rollback" or prepared.candidate_id != evidence.snapshot_id
                or prepared.input_digest_sha256 != evidence.snapshot_manifest_sha256
                or not _rollback_worktree_registered(prepared)):
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
        return prepared
    except Exception:
        _cleanup_worktree(context.repo_root, control, txn)
        raise


def prepare_rollback(repo: Path, control_root: Path | None, plan: Plan, plan_hash: str,
                     expected_remote_sha: str, *, config_path: Path | None = None) -> Prepared:
    """Build a detached, current-process-only canonical rollback Prepared; never apply it."""
    supplied = _verify_apply_inputs(plan, plan_hash, expected_remote_sha)
    context = resolve_repository_context(repo)
    if (context.layout != "canonical" or supplied.operation != "rollback" or config_path is None
            or control_root is not None):
        raise ConfigError("FAIL_TRANSACTION_SCOPE", "canonical rollback prepared")
    reviewed, proof = _review_canonical_rollback(context, supplied.candidate_id, config_path)
    if supplied != reviewed:
        raise ConfigError("FAIL_PLAN_HASH", "canonical rollback prepared")
    return _prepare_rollback_from_reviewed(context, reviewed, proof, config_path)


def _canonical_rollback_boundary(context: RepositoryContext, control_root: Path | None, snapshot_id: str,
                                 *, apply: bool, config_path: Path | None,
                                 plan_hash: str | None = None,
                                 expected_remote_sha: str | None = None) -> Plan | Result:
    if config_path is None or control_root is not None:
        raise ConfigError("FAIL_STATE_BINDING", "canonical rollback binding")
    if not isinstance(snapshot_id, str) or ARTIFACT_ID_RE.fullmatch(snapshot_id) is None:
        raise ConfigError("FAIL_ROLLBACK_ID", "canonical rollback id")
    if apply and (not isinstance(plan_hash, str) or SHA256_RE.fullmatch(plan_hash) is None
                  or not isinstance(expected_remote_sha, str) or SHA_RE.fullmatch(expected_remote_sha) is None):
        raise ConfigError("FAIL_PLAN_HASH", "canonical rollback plan")
    try:
        if not apply:
            return _plan_canonical_rollback(context, snapshot_id, config_path)
        with operation_lock(_canonical_control_root(context, None)):
            reviewed, proof = _review_canonical_rollback(context, snapshot_id, config_path)
            _verify_apply_inputs(reviewed, plan_hash, expected_remote_sha)
            prepared = _prepare_rollback_from_reviewed(context, reviewed, proof, config_path)
            return _apply_canonical_rollback_locked(prepared, reviewed, proof)
    except ConfigError as exc:
        _raise_canonical_rollback_error(exc)
    except Exception:
        _raise_canonical_rollback_error(None)


def _raise_canonical_rollback_error(exc: ConfigError | None) -> None:
    """Raise the closed public rollback boundary vocabulary without a cause."""
    details = {
        "FAIL_ROLLBACK_BINDING_CHANGED": "canonical rollback binding changed",
        "FAIL_ROLLBACK_CLEANUP_PENDING": "canonical rollback cleanup required",
        "FAIL_ROLLBACK_NOT_SETTLED": "canonical rollback incomplete",
        "FAIL_ROLLBACK_REMOTE_CHANGED": "canonical rollback remote changed",
        "FAIL_ROLLBACK_SCOPE_UNVERIFIED": "canonical rollback scope unverified",
        "FAIL_ROLLBACK_INDETERMINATE": "canonical rollback state",
        "FAIL_ROLLBACK_ARTIFACT_SCOPE": "canonical rollback artifacts",
        "FAIL_ROLLBACK_ID": "canonical rollback id",
        "FAIL_STATE_REPOSITORY": "canonical rollback repository",
        "FAIL_PLAN_HASH": "canonical rollback plan",
        "FAIL_INPUT_CHANGED": "canonical rollback plan",
        "FAIL_REMOTE_RACE": "rollback",
        "FAIL_REMOTE_REWIND": "rollback",
        "REMOTE_OUTCOME_UNKNOWN": "rollback",
        "REMOTE_COMMITTED_SCOPE_UNVERIFIED": "rollback",
        "REMOTE_COMMITTED_LOCAL_STALE": "rollback",
        "REMOTE_COMMITTED_FINALIZATION_INCOMPLETE": "rollback",
    }
    if exc is not None and exc.code in details:
        raise ConfigError(exc.code, details[exc.code]) from None
    if exc is not None and exc.code == "FAIL_STATE_BINDING":
        raise ConfigError("FAIL_ROLLBACK_BINDING_CHANGED", "canonical rollback binding changed") from None
    if exc is None:
        raise ConfigError("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state") from None
    raise ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts") from None


def _rollback_manifest(control_root: Path, rollback_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{8}T\d{12}Z", rollback_id):
        raise ConfigError("FAIL_ROLLBACK", f"invalid rollback id: {rollback_id}")
    path = control_root.resolve() / "rollback" / f"{rollback_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_ROLLBACK", f"cannot read {path}: {exc}") from exc
    if payload.get("schema") != "agent-core-rollback/1" or payload.get("id") != rollback_id:
        raise ConfigError("FAIL_ROLLBACK", rollback_id)
    return payload


def rollback(repo: Path, control_root: Path | None, rollback_id: str, *, apply: bool,
             plan_hash: str | None = None, expected_remote_sha: str | None = None,
             config_path: Path | None = None) -> Plan | Result:
    try:
        context = resolve_repository_context(repo)
    except Exception:
        if config_path is not None:
            raise ConfigError("FAIL_STATE_REPOSITORY", "canonical rollback repository") from None
        raise
    if context.layout == "canonical":
        return _canonical_rollback_boundary(
            context, control_root, rollback_id, apply=apply, config_path=config_path,
            plan_hash=plan_hash, expected_remote_sha=expected_remote_sha,
        )
    resolved_control, binding, bound_remote = _transaction_plan_context(
        context, control_root, config_path, "promote")
    manifest = _rollback_manifest(resolved_control, rollback_id)
    state = require_fresh(repo, "promote", resolved_control) if binding is None else None
    plan = _plan("rollback", rollback_id, bound_remote or (state.remote or ""),
                 snapshot_sha=manifest["pre_apply_sha"], **_plan_context(context, binding))
    if not apply:
        return plan
    if plan_hash is None or expected_remote_sha is None:
        raise ConfigError("FAIL_PLAN_HASH", "rollback --apply requires plan hash and expected remote sha")
    plan = _verify_apply_inputs(plan, plan_hash, expected_remote_sha)
    txn = _new_worktree(context.repo_root, resolved_control, plan.expected_remote_sha)
    changed = []
    expectations: list[ChangeExpectation] = []
    try:
        txn_context = replace(context, repo_root=txn.resolve(), state_root=context.temporary_state_root(txn))
        files_root = resolved_control / "rollback" / rollback_id / "files"
        for item in manifest["files"]:
            path_text = item["path"]
            logical = txn_context.repo_to_state_path(_normal_git_path(path_text))
            if logical is None:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", path_text)
            relative = Path(logical)
            target = _safe_state_path(txn_context, relative)
            git_path = context.state_to_repo_path(relative)
            changed.append(git_path)
            if item["exists"]:
                backup = (files_root / relative).resolve()
                try:
                    backup.relative_to(files_root.resolve())
                except ValueError as exc:
                    raise ConfigError("FAIL_ROLLBACK_PATH", path_text) from exc
                data = backup.read_bytes()
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise ConfigError("FAIL_ROLLBACK_HASH", path_text)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                parent = _tree_entry(txn, plan.expected_remote_sha, git_path)
                expectations.append(ChangeExpectation("M" if parent else "A", git_path, "100644"))
            elif target.exists():
                target.unlink()
                expectations.append(ChangeExpectation("D", git_path, "100644"))
            else:
                raise ConfigError("FAIL_TRANSACTION_SCOPE", f"rollback has no change: {path_text}")
        sha = _commit(txn, f"rollback: restore {rollback_id}", plan.expected_remote_sha, context,
                      tuple(expectations))
        prepared = _prepared(
            context.repo_root, resolved_control, txn, sha, plan, tuple(changed), context=context,
        )
        return apply_prepared(prepared)
    except Exception:
        if txn.exists():
            _cleanup_worktree(context.repo_root, resolved_control, txn)
        raise
