from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unicodedata import normalize

from . import ledger
from .candidates import INBOX, _similarities
from .config import ConfigError, load_config
from .freshness import CANDIDATE_ID_RE, SHA_RE, load_candidate, parse_candidate_bytes
from .lock import operation_lock
from .match import MatchError, parse_when, tokenize
from .project import resolve_project_context
from .repository import RepositoryContext, _is_reparse_alias, resolve_repository_context


GLOBAL_LEDGER = Path("experience/LESSONS.md")
CONSUMED = INBOX / "consumed"
PROJECT_LEDGER = Path(".agents/LESSONS.md")
PROJECT_INBOX = Path(".agents/inbox")
PROJECT_CONSUMED = PROJECT_INBOX / "consumed"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Plan:
    operation: str
    candidate_id: str
    expected_remote_sha: str
    plan_hash: str
    lines: tuple[str, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ProjectResult:
    lesson_id: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class LocalRejectResult:
    candidate_id: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class LocalPromoteContext:
    """One lock-held, byte-and-identity-bound local promote snapshot."""

    repo: Path
    state: RepositoryContext | None
    candidate_path: Path
    source_path: Path
    target_path: Path
    consumed_path: Path
    candidate_raw: bytes
    target_raw: bytes
    candidate_token: OwnedFileToken
    target_token: OwnedFileToken
    sources: tuple[tuple[str, str, str], ...]
    advisories: tuple[str, ...]


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


def _canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
class OwnedFileToken:
    """Identity returned by the exclusive writer's still-open file handle."""

    device: int
    inode: int
    size: int


def _owned_file_matches(path: Path, token: OwnedFileToken, *, links: int) -> bool:
    try:
        observed = os.lstat(path)
        return (not _is_reparse_alias(path) and stat.S_ISREG(observed.st_mode) and observed.st_nlink == links
                and observed.st_dev == token.device and observed.st_ino == token.inode
                and observed.st_size == token.size)
    except OSError:
        return False


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


def _project_context(workspace: Path) -> tuple[Path, str]:
    return resolve_project_context(workspace)


def _project_active_target(ledger_text: str, lesson_id: str, project_id: str) -> None:
    del project_id
    is_active = False
    for line in ledger_text.splitlines():
        if line.strip().startswith("## "):
            is_active = ledger.is_active_heading(line.strip()[3:])
            continue
        match = ledger.ENTRY_RE.match(line)
        if is_active and match and (match.group(1) or match.group(2)) == lesson_id:
            return
    raise ConfigError("FAIL_UPDATE_TARGET", lesson_id)


def _assert_project_index_clean(repo: Path, paths: tuple[Path, ...]) -> None:
    _assert_local_index_clean(repo, paths, "project promotion")


def _assert_local_index_clean(repo: Path, paths: tuple[Path, ...], operation: str) -> None:
    relative = tuple(path.relative_to(repo).as_posix() for path in paths)
    result = _git(repo, "diff", "--cached", "--quiet", "--", *relative, check=False)
    if result.returncode == 1:
        raise ConfigError(
            "FAIL_INDEX_CONFLICT",
            f"{operation} paths; NEXT commit the paths staged by the previous apply, then preview again",
        )
    if result.returncode != 0:
        raise ConfigError("FAIL_INDEX_CONFLICT", f"{operation} index")


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


def _local_checked_source(path: Path, state: RepositoryContext | None, label: str) -> Path:
    """Return one reviewed ordinary ledger file without alias resolution."""
    if state is not None:
        try:
            return _local_state_file(state, path.relative_to(state.state_root), label)
        except ValueError:
            pass
    return _local_safe_file(path, label)


def _local_promote_sources(workspace_repo: Path, project_id: str | None,
                           state: RepositoryContext | None) -> tuple[tuple[str, str, Path], ...]:
    sources: list[tuple[str, str, Path]] = []
    project_ledger = workspace_repo / PROJECT_LEDGER
    if project_id is not None and project_ledger.is_file():
        sources.append(("project", project_id, _local_checked_source(project_ledger, state, "project ledger")))
    if state is None:
        return tuple(sources)
    global_path = _safe_state_path(state, GLOBAL_LEDGER)
    if not global_path.is_file():
        raise ConfigError("FAIL_LESSON_ROUTING", "global ledger; RETRY lessons capture after repairing the bound state")
    global_path = _local_state_file(state, GLOBAL_LEDGER, "global ledger")
    resolved, errors, _warnings = ledger.resolve_sources(str(global_path), str(workspace_repo))
    if errors:
        raise ConfigError("FAIL_LESSON_ROUTING", "declared lessons routing; RETRY lessons capture after repairing routing")
    result = [(scope, store, _local_checked_source(Path(path), state, "resolved lessons source"))
              for scope, store, path in resolved]
    if project_id is not None and project_ledger.is_file() and not any(
            scope == "project" and store == project_id for scope, store, _path in result):
        result.append(("project", project_id, _local_checked_source(project_ledger, state, "project ledger")))
    for source in result:
        _defined, validation_errors, _validation_warnings = ledger.validate_sources(
            [(source[0], source[1], str(source[2]))]
        )
        if validation_errors:
            raise ConfigError("FAIL_LESSON_ROUTING", "resolved lessons source; RETRY lessons capture after repairing ledger")
    return tuple(result)


def _local_state_context(state_root: Path | None) -> RepositoryContext | None:
    if state_root is None:
        return None
    try:
        return resolve_repository_context(state_root)
    except ConfigError as exc:
        raise ConfigError("FAIL_SCOPE_REPOSITORY", "state root; RETRY with the bound private state root") from exc


def _local_file_token(path: Path, label: str) -> OwnedFileToken:
    try:
        entry = os.lstat(path)
        if _is_reparse_alias(path) or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise ValueError
        return OwnedFileToken(entry.st_dev, entry.st_ino, entry.st_size)
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_TRANSACTION_SCOPE", label) from exc


def _local_safe_file(path: Path, label: str) -> Path:
    # `_resolved_local_path` validates a directory chain.  The final leaf is
    # deliberately checked separately as an ordinary single-link file.
    resolved = _resolved_local_path(path.parent, label) / path.name
    _local_file_token(resolved, label)
    return resolved


def _local_state_file(context: RepositoryContext, relative: Path, label: str,
                      *, required: bool = True) -> Path:
    path = _safe_state_path(context, relative, required="file" if required else None)
    if required:
        _local_file_token(path, label)
    return path


def _local_candidate_source(workspace_repo: Path, state: RepositoryContext | None,
                            candidate_id: str) -> tuple[Path, Path, str]:
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ConfigError("FAIL_CANDIDATE", "invalid candidate id; RETRY lessons capture")
    candidates = [(workspace_repo / PROJECT_INBOX / f"{candidate_id}.md", "project")]
    if state is not None:
        candidates.append((_safe_state_path(state, INBOX / f"{candidate_id}.md"), "state"))
    current = [(path, kind) for path, kind in candidates if path.is_file()]
    if len(current) == 1:
        path, kind = current[0]
        return (_local_safe_file(path, "candidate") if kind == "project" else _local_state_file(
            state, INBOX / f"{candidate_id}.md", "candidate"), path, kind)
    if len(current) > 1:
        raise ConfigError("FAIL_CANDIDATE", "candidate source; RETRY lessons capture in the selected target inbox")
    consumed: list[tuple[Path, Path, str]] = []
    for source, kind in candidates:
        target = source.parent / "consumed" / source.name
        if target.is_file():
            checked = (_local_safe_file(target, "consumed candidate") if kind == "project" else _local_state_file(
                state, CONSUMED / f"{candidate_id}.md", "consumed candidate"))
            consumed.append((checked, source, kind))
    if len(consumed) == 1:
        return consumed[0]
    if len(consumed) > 1:
        raise ConfigError("FAIL_CANDIDATE", "candidate source; RETRY lessons capture in the selected target inbox")
    if not current:
        code = "FAIL_CANDIDATE_MISSING"
        raise ConfigError(code, "candidate source; RETRY lessons capture in the selected target inbox")


def _parse_local_scope(value: str, project_id: str | None) -> tuple[str, str]:
    if value == "global":
        return "global", "global"
    if value.startswith("profile:"):
        name = value.removeprefix("profile:")
        if ledger.PROFILE_RE.fullmatch(name):
            return "profile", name
    if value.startswith("project:") and value.removeprefix("project:") == project_id:
        return "project", project_id
    retry = f"project:{project_id}" if project_id else "global"
    raise ConfigError("FAIL_SCOPE_REVIEW", f"target scope; RETRY lessons capture --scope {retry}")


def _local_target(sources: tuple[tuple[str, str, Path], ...], scope: str,
                  store: str) -> tuple[Path, str, str]:
    matches = [path for source_scope, source_store, path in sources
               if source_scope == scope and source_store == store]
    if len(matches) != 1 or not matches[0].is_file():
        raise ConfigError("FAIL_SCOPE_REVIEW", "target ledger; RETRY lessons capture in the declared target inbox")
    label = ledger.SCOPE_LABEL[scope]
    prefix = "L" if scope == "global" else store.split("-", 1)[0].upper()
    return matches[0], label, prefix


def _local_git_root(path: Path) -> Path:
    root = ledger.find_git_root(str(path if path.is_dir() else path.parent))
    if not root:
        raise ConfigError("FAIL_SCOPE_REPOSITORY", "candidate/target repository; RETRY lessons capture in the target inbox")
    return Path(root).resolve()


def _local_control_root(config_path: Path | None, control_root: Path | None,
                        state_root: Path | None) -> Path:
    """Use install's one lock identity whenever a host config is supplied."""
    del state_root
    if config_path is None:
        return (control_root or Path.home() / ".agent-core").resolve()
    config = config_path.resolve()
    expected = config.parent / "txn"
    if control_root is not None:
        supplied = control_root.resolve()
        if supplied not in {config.parent, expected}:
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "control root; RETRY omit --control-root")
    return expected


def _local_update_pointer(value: str | None, scope: str, store: str) -> str | None:
    if value is None:
        return None
    # Legacy direct callers may still supply a bare id.  The public CLI emits
    # and accepts only the scoped form.
    if ":" not in value:
        return value
    pieces = value.split(":", 2)
    if len(pieces) != 3 or pieces[0] != scope or pieces[1] != store or not pieces[2]:
        raise ConfigError("FAIL_UPDATE_TARGET", f"RETRY --update {scope}:{store}:<lesson-id>")
    return pieces[2]


def _local_effective_when(item: dict[str, str], override: str | None) -> tuple[str | None, str, tuple[str, ...]]:
    raw = override if override is not None else item.get("when")
    source = "cli" if override is not None else ("candidate" if raw is not None else "legacy")
    if raw is None:
        return None, source, ()
    try:
        parsed = parse_when(raw, source="effective when")
    except MatchError as exc:
        raise ConfigError("FAIL_WHEN", f"{exc}; RETRY --when <canonical-json>") from exc
    diagnostics: list[str] = []
    for predicate, values in parsed.items():
        for index, value in enumerate(values):
            tokens = tokenize(value)
            if not tokens:
                raise ConfigError(
                    "FAIL_WHEN",
                    f"when.{predicate}[{index}] tokenizes empty; RETRY --when <canonical-json>",
                )
            diagnostics.append(
                f"EFFECTIVE_WHEN_TOKENS predicate={predicate} index={index} tokens={','.join(tokens)}"
            )
    return raw, source, tuple(diagnostics)


def _local_item_with_when(item: dict[str, str], effective_when: str | None) -> dict[str, str]:
    effective = dict(item)
    if effective_when is None:
        effective.pop("when", None)
    else:
        effective["when"] = effective_when
    return effective


def _local_entry(item: dict[str, str], lesson_id: str, label: str, *,
                 project: bool, supersedes: str | None = None) -> str:
    rendered_id = lesson_id if label == ledger.SCOPE_LABEL["global"] else f"[[lesson:{lesson_id}]]"
    # Project rendering preserves the candidate's exact rule bytes.  Global
    # and profile retain their existing frozen punctuation renderer.
    rule = item["rule"] if project else f"{item['rule']}."
    suffix = f" supersedes: {supersedes}." if supersedes else ""
    when = f" when: {item['when']}" if "when" in item else ""
    return (f"- **{rendered_id} [pending·{label}] {rule}** 触发: {item['trigger']}. "
            f"代价: {item['cost']}. from: {item['id']}.{suffix} sink → {item['sink']}.{when}")


def _local_render(text: str, item: dict[str, str], lesson_id: str, label: str,
                  scope: str, update: str | None, supersedes: str | None) -> str:
    if update is None:
        if supersedes:
            text = _archive_superseded(text, supersedes, lesson_id)
        marker = "\n## 归档"
        if marker not in text:
            raise ConfigError("FAIL_LEDGER", "missing archive heading; RETRY after repairing target ledger")
        entry = _local_entry(item, lesson_id, label, project=scope == "project", supersedes=supersedes)
        return text.replace(marker, f"\n{entry}\n{marker}", 1)
    lines = text.splitlines()
    active = False
    replacement = _local_entry(item, lesson_id, label, project=scope == "project")
    for index, line in enumerate(lines):
        if line.strip().startswith("## "):
            active = ledger.is_active_heading(line.strip()[3:])
            continue
        match = ledger.ENTRY_RE.match(line)
        if active and match and (match.group(1) or match.group(2)) == update:
            lines[index] = replacement
            return "\n".join(lines) + "\n"
    raise ConfigError("FAIL_UPDATE_TARGET", f"{update}; RETRY with an active target pointer")


def _local_preview_lines(preimage: str, postimage: str) -> tuple[str, ...]:
    """Derive review lines from the exact postimage; re-rendering the entry could drift from bytes written."""
    before, after = preimage.splitlines(), postimage.splitlines()
    preview: list[str] = []
    for tag, before_start, before_end, after_start, after_end in difflib.SequenceMatcher(
            None, before, after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        preview.extend(f"POSTIMAGE_REMOVED {line}" for line in before[before_start:before_end] if line.strip())
        preview.extend(f"POSTIMAGE_ADDED {line}" for line in after[after_start:after_end] if line.strip())
    return tuple(preview)


def _local_exact_and_advisories(sources: tuple[tuple[str, str, Path], ...], rule: str,
                                target: tuple[str, str], update: str | None,
                                *, exclude_pointer: tuple[str, str, str] | None = None) -> tuple[str, ...]:
    normalized = ledger.normalize_rule(rule)
    try:
        active = ledger.active_lessons(tuple((scope, store, str(path)) for scope, store, path in sources))
    except ValueError as exc:
        raise ConfigError("FAIL_LESSON_ROUTING", "lessons source; RETRY lessons capture after repairing source") from exc
    lines: list[str] = []
    for entry in active:
        pointer = (entry.scope, entry.store, entry.lesson_id)
        if entry.normalized_rule == normalized:
            if pointer == exclude_pointer or pointer == (target[0], target[1], update):
                continue
            if pointer[:2] == target:
                raise ConfigError("FAIL_EXACT_DUPLICATE",
                                  f"{entry.scope}:{entry.store}:{entry.lesson_id}; RETRY --update {entry.scope}:{entry.store}:{entry.lesson_id}")
            lines.append(f"EXACT scope={entry.scope} store={entry.store} id={entry.lesson_id}")
    for scope, store, source in sources:
        for lesson_id, score in _similarities(source.read_text(encoding="utf-8"), rule):
            lines.append(f"SIMILAR scope={scope} store={store} id={lesson_id} score={score:.3f}")
    return tuple(lines)


def _local_exact_digest(lines: tuple[str, ...]) -> str:
    """Only exact-duplicate facts are plan-bound; fuzzy advice is review output."""
    return hashlib.sha256("\n".join(line for line in lines if line.startswith("EXACT ")).encode("utf-8")).hexdigest()


def _local_promoted_entry(text: str, candidate_id: str, expected_entry: str) -> str | None:
    for line in text.splitlines():
        if f"from: {candidate_id}." not in line:
            continue
        match = ledger.ENTRY_RE.match(line)
        if match and line == expected_entry:
            return match.group(1) or match.group(2)
    return None


def _local_existing_result_id(text: str, candidate_id: str) -> str | None:
    """Find an existing candidate-derived entry before allocating a fresh ID."""
    for line in text.splitlines():
        if f"from: {candidate_id}." not in line:
            continue
        match = ledger.ENTRY_RE.match(line)
        if match:
            return match.group(1) or match.group(2)
    return None


def _after_local_promote_canonical_write() -> None:
    """Narrow test seam for the only post-write convergence boundary."""


def _local_plan(candidate_id: str, *, repo: Path, candidate_path: Path, source: Path, source_kind: str,
                target: Path, scope: str, store: str, project_id: str | None,
                state_root: Path | None, config_path: Path | None, control_root: Path,
                item: dict[str, str], effective_when: str | None, effective_when_source: str,
                effective_when_diagnostics: tuple[str, ...], action: str, review_choice: str, update: str | None,
                supersedes: str | None, result_id: str,
                canonical_sha256: str, expected_postimage_sha256: str, source_root: Path, target_root: Path,
                stage_paths: tuple[str, ...], sources: tuple[tuple[str, str, Path], ...],
                lines: tuple[str, ...], state: RepositoryContext | None,
                preview: tuple[str, ...] = ()) -> Plan:
    candidate_token = _local_file_token(candidate_path, "candidate")
    target_token = _local_file_token(target, "target ledger")
    payload = {
        "operation": "lessons-promote", "candidate_id": candidate_id,
        "expected_remote_sha": "", "source_path": source.relative_to(repo).as_posix(),
        "candidate_path": candidate_path.relative_to(repo).as_posix(), "source_kind": source_kind,
        "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "target_scope": scope, "target_store": store,
        "target_path": target.relative_to(repo).as_posix(), "operation_root": str(repo),
        "target_git_root": str(target_root), "source_git_root": str(source_root),
        "canonical_sha256": canonical_sha256, "expected_postimage_sha256": expected_postimage_sha256,
        "effective_when": effective_when,
        "action": action, "choice": review_choice,
        "update": update, "supersedes": supersedes,
        "result_id": result_id, "project_id": project_id,
        "state_root": str(state_root.resolve()) if state_root else None,
        "config_path": str(config_path.resolve()) if config_path else None,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path else None,
        "control_root": str(control_root), "stage_paths": stage_paths,
        "candidate_identity": (candidate_token.device, candidate_token.inode, candidate_token.size),
        "target_identity": (target_token.device, target_token.inode, target_token.size),
        "resolved_sources": tuple((scope, store, str(path)) for scope, store, path in sources),
        "exact_facts_sha256": _local_exact_digest(lines),
        "layout": state.layout if state else None,
        "state_prefix": state.state_prefix if state else None,
    }
    plan_hash = _canonical_hash(payload)
    plan_lines = lines + (
        f"PLAN operation=lessons-promote candidate={candidate_id}",
        f"TARGET scope={scope} store={store} path={payload['target_path']}",
        f"ACTION {action}", f"RESULT_ID {result_id}",
        f"CANDIDATE_SHA256 {payload['candidate_sha256']}",
        f"CANONICAL_SHA256 {canonical_sha256}",
        f"CANDIDATE_WHEN {item.get('when') or 'null'}",
        f"EFFECTIVE_WHEN source={effective_when_source} value={effective_when or 'null'}",
    ) + effective_when_diagnostics + (
        f"EXPECTED_POSTIMAGE_SHA256 {expected_postimage_sha256}", f"PLAN_HASH {plan_hash}",
    ) + preview
    return Plan("lessons-promote", candidate_id, "", plan_hash, plan_lines, payload)


def _local_context_from_plan(plan: Plan) -> LocalPromoteContext:
    """Rebuild the reviewed local paths once, then freeze bytes for the write phase."""
    payload = plan.payload
    try:
        repo = Path(payload["operation_root"]).resolve()
        if _local_git_root(repo) != repo:
            raise ValueError
        state_value = payload.get("state_root")
        state = _local_state_context(Path(state_value)) if isinstance(state_value, str) else None
        if state is not None and (state.repo_root != repo or payload.get("layout") != state.layout
                                  or payload.get("state_prefix") != state.state_prefix):
            raise ValueError
        candidate = repo / payload["candidate_path"]
        source = repo / payload["source_path"]
        target = repo / payload["target_path"]
        consumed = source.parent / "consumed" / source.name
        for value in (candidate, source, target, consumed):
            value.relative_to(repo)
        def checked(value: Path, label: str) -> Path:
            if state is not None:
                try:
                    relative = value.relative_to(state.state_root)
                except ValueError:
                    relative = None
                if relative is not None:
                    return _local_state_file(state, relative, label)
            return _local_safe_file(value, label)
        candidate = checked(candidate, "candidate")
        target = checked(target, "target ledger")
        candidate_raw = candidate.read_bytes()
        target_raw = target.read_bytes()
        candidate_token = _local_file_token(candidate, "candidate")
        target_token = _local_file_token(target, "target ledger")
        if (hashlib.sha256(candidate_raw).hexdigest() != payload.get("candidate_sha256")
                or hashlib.sha256(target_raw).hexdigest() != payload.get("canonical_sha256")
                or not isinstance(payload.get("expected_postimage_sha256"), str)
                or SHA256_RE.fullmatch(payload["expected_postimage_sha256"]) is None
                or tuple(payload.get("candidate_identity", ())) != (candidate_token.device, candidate_token.inode, candidate_token.size)
                or tuple(payload.get("target_identity", ())) != (target_token.device, target_token.inode, target_token.size)):
            raise ValueError
        sources_raw = payload.get("resolved_sources")
        if not isinstance(sources_raw, (tuple, list)):
            raise ValueError
        if not sources_raw or any(
                not isinstance(source, (tuple, list)) or len(source) != 3
                or any(not isinstance(value, str) or not value for value in source)
                for source in sources_raw):
            raise ValueError
        sources = tuple(
            (scope, store, str(_local_checked_source(Path(path), state, "resolved lessons source")))
            for scope, store, path in sources_raw
        )
        item = parse_candidate_bytes(candidate_raw, plan.candidate_id, allow_project=True)
        advisories = _local_exact_and_advisories(
            tuple((scope, store, Path(path)) for scope, store, path in sources), item["rule"],
            (str(payload["target_scope"]), str(payload["target_store"])), payload.get("update"),
            exclude_pointer=((str(payload["target_scope"]), str(payload["target_store"]), str(payload["result_id"]))
                             if payload.get("action") == "converge" else None))
        if _local_exact_digest(advisories) != payload.get("exact_facts_sha256"):
            raise ValueError
        return LocalPromoteContext(repo, state, candidate, source, target, consumed, candidate_raw,
                                   target_raw, candidate_token, target_token, sources, advisories)
    except (ConfigError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("FAIL_INPUT_CHANGED", "reviewed local promote context; RETRY replan lessons promote") from exc


def plan_local_promote(workspace: Path, control_root: Path | None, candidate_id: str, *,
                       scope_override: str | None = None, supersedes: str | None = None,
                       force_new: bool = False, update: str | None = None, when: str | None = None,
                       state_root: Path | None = None, config_path: Path | None = None) -> Plan:
    if sum(value is not None and value is not False for value in (supersedes, force_new, update)) != 1:
        raise ConfigError("FAIL_PLAN_HASH", "RETRY choose exactly one --update or --force-new")
    if config_path is not None and state_root is None:
        configured = load_config(config_path)["state_root"]
        if not (configured.startswith("<") and configured.endswith(">")):
            state_root = Path(configured).expanduser()
    workspace = workspace.resolve()
    state = _local_state_context(state_root)
    candidate_path, source, source_kind = _local_candidate_source(workspace, state, candidate_id)
    item = load_candidate(candidate_path, allow_project=True)
    effective_when, effective_when_source, effective_when_diagnostics = _local_effective_when(item, when)
    effective_item = _local_item_with_when(item, effective_when)
    requested_scope = scope_override or item["scope_hint"]
    project_repo: Path | None = None
    project_id: str | None = None
    if source_kind == "project" or requested_scope.startswith("project:"):
        project_repo, project_id = _project_context(workspace)
    scope, store = _parse_local_scope(requested_scope, project_id)
    sources = _local_promote_sources(workspace, project_id, state)
    target, label, prefix = _local_target(sources, scope, store)
    try:
        state_relative = target.relative_to(state.state_root) if state is not None else None
    except ValueError:
        state_relative = None
    if state_relative is not None:
        target = _local_state_file(state, state_relative, "target ledger")
    else:
        target = _local_safe_file(target, "target ledger")
    source_root, target_root = _local_git_root(source), _local_git_root(target)
    if source_root != target_root:
        raise ConfigError("FAIL_SCOPE_REPOSITORY",
                          f"RETRY agent-core lessons capture --scope {scope if scope == 'global' else scope + ':' + store}")
    if project_repo is not None and project_repo.resolve() != source_root:
        raise ConfigError("FAIL_SCOPE_REPOSITORY", "RETRY lessons capture in the target repository inbox")
    repo = source_root
    control = _local_control_root(config_path, control_root, state_root)
    update_id = _local_update_pointer(update, scope, store)
    ledger_text = target.read_text(encoding="utf-8")
    if update_id is not None:
        _project_active_target(ledger_text, update_id, project_id or "")
    prior_result_id = _local_existing_result_id(ledger_text, candidate_id)
    result_id = update_id or prior_result_id or _next_id(ledger_text, scope, prefix)
    expected_entry = _local_entry(effective_item, result_id, label, project=scope == "project", supersedes=supersedes)
    existing = _local_promoted_entry(ledger_text, candidate_id, expected_entry)
    consumed = source.parent / "consumed" / source.name
    if state is not None:
        try:
            source_relative_state = source.relative_to(state.state_root)
        except ValueError:
            source_relative_state = None
        if source_relative_state is not None:
            consumed = _safe_state_path(state, source_relative_state.parent / "consumed" / source.name)
        else:
            consumed = _resolved_local_path(consumed, "consumed candidate")
    else:
        consumed = _resolved_local_path(consumed, "consumed candidate")
    source_relative = source.relative_to(repo).as_posix(); consumed_relative = consumed.relative_to(repo).as_posix()
    target_relative = target.relative_to(repo).as_posix()
    tracked_source = _git(repo, "ls-files", "--error-unmatch", "--", source_relative, check=False).returncode == 0
    stage_paths = (target_relative, consumed_relative) + ((source_relative,) if tracked_source else ())
    _assert_project_index_clean(repo, (target, source, consumed))
    if existing is not None:
        convergence_advisories = _local_exact_and_advisories(
            sources, item["rule"], (scope, store), update_id,
            exclude_pointer=(scope, store, existing))
        current_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        return _local_plan(candidate_id, repo=repo, candidate_path=candidate_path, source=source, source_kind=source_kind, target=target,
                           scope=scope, store=store, project_id=project_id, state_root=state_root,
                           config_path=config_path, control_root=control, item=item,
                           effective_when=effective_when, effective_when_source=effective_when_source,
                           effective_when_diagnostics=effective_when_diagnostics, action="converge",
                           review_choice="update" if update_id else "force-new", update=update_id,
                           supersedes=supersedes, result_id=existing, canonical_sha256=current_sha256,
                           expected_postimage_sha256=current_sha256,
                           source_root=source_root, target_root=target_root, stage_paths=stage_paths,
                           sources=sources, lines=convergence_advisories, state=state)
    advisories = _local_exact_and_advisories(sources, item["rule"], (scope, store), update_id)
    postimage_text = _local_render(ledger_text, effective_item, result_id, label, scope, update_id, supersedes)
    expected_postimage_sha256 = hashlib.sha256(postimage_text.encode("utf-8")).hexdigest()
    return _local_plan(candidate_id, repo=repo, candidate_path=candidate_path, source=source, source_kind=source_kind, target=target,
                       scope=scope, store=store, project_id=project_id, state_root=state_root,
                       config_path=config_path, control_root=control, item=item,
                       effective_when=effective_when, effective_when_source=effective_when_source,
                       effective_when_diagnostics=effective_when_diagnostics,
                       action="update" if update_id else "force-new", review_choice="update" if update_id else "force-new",
                       update=update_id, supersedes=supersedes, result_id=result_id,
                       canonical_sha256=hashlib.sha256(target.read_bytes()).hexdigest(), source_root=source_root,
                       expected_postimage_sha256=expected_postimage_sha256, target_root=target_root, stage_paths=stage_paths, sources=sources,
                       lines=advisories, state=state, preview=_local_preview_lines(ledger_text, postimage_text))


def apply_local_promote(workspace: Path, control_root: Path | None, plan: Plan, plan_hash: str, *,
                        config_path: Path | None = None) -> ProjectResult:
    if plan.operation != "lessons-promote" or plan_hash != plan.plan_hash or plan.plan_hash != _canonical_hash(plan.payload):
        raise ConfigError("FAIL_INPUT_CHANGED", "RETRY replan lessons promote")
    state_value = plan.payload.get("state_root")
    state_root = Path(state_value) if isinstance(state_value, str) else None
    bound_config = Path(plan.payload["config_path"]) if isinstance(plan.payload.get("config_path"), str) else None
    if bound_config is not None:
        if config_path is None or config_path.resolve() != bound_config.resolve() or hashlib.sha256(bound_config.read_bytes()).hexdigest() != plan.payload["config_sha256"]:
            raise ConfigError("FAIL_INPUT_CHANGED", "config; RETRY replan lessons promote")
    control = _local_control_root(bound_config, control_root, state_root)
    if str(control) != plan.payload.get("control_root"):
        raise ConfigError("FAIL_INPUT_CHANGED", "lock identity; RETRY replan lessons promote")
    with operation_lock(control):
        reviewed = plan_local_promote(workspace, control, plan.candidate_id,
                                      scope_override=(plan.payload["target_scope"] if plan.payload["target_scope"] == "global" else f"{plan.payload['target_scope']}:{plan.payload['target_store']}"),
                                      supersedes=plan.payload.get("supersedes"),
                                      # A supersedes plan records choice=force-new; replan it as the
                                      # preview did, with supersedes alone.
                                      force_new=plan.payload.get("choice") == "force-new" and not plan.payload.get("supersedes"),
                                      update=(f"{plan.payload['target_scope']}:{plan.payload['target_store']}:{plan.payload['update']}" if plan.payload.get("choice") == "update" else None),
                                      when=plan.payload.get("effective_when"),
                                      state_root=state_root, config_path=bound_config)
        if reviewed.plan_hash != plan.plan_hash:
            raise ConfigError("FAIL_INPUT_CHANGED", "reviewed facts; RETRY replan lessons promote")
        payload = reviewed.payload
        context = _local_context_from_plan(reviewed)
        repo, source, target, consumed = context.repo, context.source_path, context.target_path, context.consumed_path
        item = parse_candidate_bytes(context.candidate_raw, reviewed.candidate_id, allow_project=True)
        effective_item = _local_item_with_when(item, payload.get("effective_when"))
        # Planning uses TextIO's universal-newline view and the atomic writer
        # persists LF.  Render from the same logical ledger text while keeping
        # the raw preimage hash in the reviewed context.
        target_text = context.target_raw.decode("utf-8", "strict").replace("\r\n", "\n").replace("\r", "\n")
        if payload["action"] != "converge":
            if os.path.lexists(consumed):
                raise ConfigError("FAIL_CANDIDATE_STATE", "consumed candidate; RETRY replan lessons promote")
            rendered = _local_render(target_text, effective_item, payload["result_id"], ledger.SCOPE_LABEL[payload["target_scope"]],
                                     payload["target_scope"], payload.get("update"), payload.get("supersedes"))
            try:
                _ids, errors, _warnings = ledger.parse_ledger(
                    rendered, payload["target_scope"], payload["target_path"])
                if errors:
                    raise ValueError("rendered ledger validation failed")
            except ValueError as exc:
                raise ConfigError("FAIL_LEDGER", "rendered target ledger; RETRY after repairing target ledger") from exc
            if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != payload["expected_postimage_sha256"]:
                raise ConfigError("FAIL_INPUT_CHANGED", "expected postimage; RETRY replan lessons promote")
            _atomic_write_text(target, rendered)
            _after_local_promote_canonical_write()
        else:
            expected = _local_entry(effective_item, payload["result_id"], ledger.SCOPE_LABEL[payload["target_scope"]], project=payload["target_scope"] == "project", supersedes=payload.get("supersedes"))
            if (hashlib.sha256(context.target_raw).hexdigest() != payload["expected_postimage_sha256"]
                    or _local_promoted_entry(target_text, plan.candidate_id, expected) != payload["result_id"]):
                raise ConfigError("FAIL_INPUT_CHANGED", "convergence evidence; RETRY replan lessons promote")
        if context.candidate_path != consumed:
            if (not _owned_file_matches(context.candidate_path, context.candidate_token, links=1)
                    or context.candidate_path.read_bytes() != context.candidate_raw):
                raise ConfigError("FAIL_INPUT_CHANGED", "candidate identity; RETRY replan lessons promote")
            if os.path.lexists(consumed):
                raise ConfigError("FAIL_CANDIDATE_STATE", "consumed candidate; RETRY replan lessons promote")
            consumed.parent.mkdir(parents=True, exist_ok=True)
            context.candidate_path.replace(consumed)
        staged = [payload["target_path"], consumed.relative_to(repo).as_posix()]
        tracked = _git(repo, "ls-files", "--error-unmatch", "--", payload["source_path"], check=False).returncode == 0
        if tracked:
            staged.append(payload["source_path"])
        if tuple(staged) != tuple(payload["stage_paths"]):
            raise ConfigError("FAIL_INPUT_CHANGED", "staging paths; RETRY replan lessons promote")
        _git(repo, "add", "--", *staged)
        return ProjectResult(payload["result_id"], tuple(staged))


def _local_reject_locations(workspace: Path, state: RepositoryContext | None,
                            candidate_id: str) -> tuple[tuple[str, Path, Path, Path], ...]:
    workspace_repo = _local_git_root(workspace.resolve())
    project_parent = _resolved_local_path(workspace_repo / PROJECT_INBOX, "candidate")
    project_source = project_parent / f"{candidate_id}.md"
    locations = [("project", project_source, project_source.parent / "consumed" / project_source.name,
                  project_source.parent / "rejected" / project_source.name)]
    if state is not None:
        state_source = _safe_state_path(state, INBOX / f"{candidate_id}.md")
        locations.append((
            "state", state_source,
            _safe_state_path(state, CONSUMED / f"{candidate_id}.md"),
            _safe_state_path(state, INBOX / "rejected" / f"{candidate_id}.md"),
        ))
    return tuple(locations)


def _local_reject_source(workspace: Path, state: RepositoryContext | None,
                         candidate_id: str) -> tuple[Path, str, Path]:
    locations = _local_reject_locations(workspace, state, candidate_id)
    if any(os.path.lexists(consumed) or os.path.lexists(rejected)
           for _kind, _source, consumed, rejected in locations):
        raise ConfigError("FAIL_CANDIDATE_STATE", "consumed/rejected collision; inspect candidate locations")
    present = [(kind, source, rejected) for kind, source, _consumed, rejected in locations
               if os.path.lexists(source)]
    if len(present) != 1:
        code = "FAIL_CANDIDATE_MISSING" if not present else "FAIL_CANDIDATE_STATE"
        raise ConfigError(code, "candidate source; inspect project and state inboxes")
    kind, source, rejected = present[0]
    try:
        source = (_local_safe_file(source, "candidate") if kind == "project" else
                  _local_state_file(state, INBOX / f"{candidate_id}.md", "candidate"))
        rejected = _resolved_local_path(rejected, "rejected candidate")
    except ConfigError as exc:
        raise ConfigError("FAIL_CANDIDATE_STATE", "candidate path identity; inspect candidate locations") from exc
    return source, kind, rejected


def _local_reject_plan(candidate_id: str, *, repo: Path, source: Path, source_kind: str,
                       destination: Path, raw: bytes, token: OwnedFileToken, head: str,
                       tracked: bool, state: RepositoryContext | None, state_root: Path | None,
                       config_path: Path | None, control_root: Path) -> Plan:
    source_path = source.relative_to(repo).as_posix()
    destination_path = destination.relative_to(repo).as_posix()
    stage_paths = (destination_path,) + ((source_path,) if tracked else ())
    payload = {
        "operation": "lessons-reject", "candidate_id": candidate_id, "action": "reject",
        "operation_root": str(repo), "head": head, "source_kind": source_kind,
        "source_path": source_path, "destination_path": destination_path,
        "candidate_sha256": hashlib.sha256(raw).hexdigest(),
        "candidate_identity": (token.device, token.inode, token.size),
        "source_tracked": tracked, "stage_paths": stage_paths,
        "state_root": str(state_root.resolve()) if state_root else None,
        "layout": state.layout if state else None,
        "state_prefix": state.state_prefix if state else None,
        "config_path": str(config_path.resolve()) if config_path else None,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path else None,
        "control_root": str(control_root),
        "control_filesystem_sha256": _control_filesystem_sha256(control_root),
    }
    plan_hash = _canonical_hash(payload)
    lines = (
        f"PLAN operation=lessons-reject candidate={candidate_id}",
        f"SOURCE kind={source_kind} path={source_path} tracked={str(tracked).lower()}",
        f"SOURCE_IDENTITY device={token.device} inode={token.inode} size={token.size}",
        f"REPOSITORY root={repo} head={head}",
        f"STATE root={payload['state_root']} layout={payload['layout']} prefix={payload['state_prefix']}",
        f"CONFIG path={payload['config_path']} sha256={payload['config_sha256']}",
        f"CONTROL root={control_root} filesystem_sha256={payload['control_filesystem_sha256']}",
        f"DESTINATION {destination_path}",
        f"STAGE_PATHS {','.join(stage_paths)}",
        f"CANDIDATE_SHA256 {payload['candidate_sha256']}",
        f"ACTION reject", f"PLAN_HASH {plan_hash}",
    )
    return Plan("lessons-reject", candidate_id, "", plan_hash, lines, payload)


def plan_local_reject(workspace: Path, control_root: Path | None, candidate_id: str, *,
                      state_root: Path | None = None, config_path: Path | None = None) -> Plan:
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ConfigError("FAIL_CANDIDATE", "invalid candidate id; RETRY lessons capture")
    if config_path is not None and state_root is None:
        configured = load_config(config_path)["state_root"]
        if not (configured.startswith("<") and configured.endswith(">")):
            state_root = Path(configured).expanduser()
    state = _local_state_context(state_root)
    source, source_kind, destination = _local_reject_source(workspace, state, candidate_id)
    repo = _local_git_root(source)
    raw = source.read_bytes()
    parse_candidate_bytes(raw, candidate_id, allow_project=source_kind == "project")
    token = _local_file_token(source, "candidate")
    head_result = _git(repo, "rev-parse", "HEAD", check=False)
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or SHA_RE.fullmatch(head) is None:
        raise ConfigError("FAIL_GIT", "reject HEAD")
    tracked = _git(repo, "ls-files", "--error-unmatch", "--", source.relative_to(repo).as_posix(),
                   check=False).returncode == 0
    _assert_local_index_clean(repo, (source, destination), "lesson reject")
    control = _local_control_root(config_path, control_root, state_root)
    return _local_reject_plan(
        candidate_id, repo=repo, source=source, source_kind=source_kind, destination=destination,
        raw=raw, token=token, head=head, tracked=tracked, state=state, state_root=state_root,
        config_path=config_path, control_root=control,
    )


def _before_local_reject_move(_source: Path, _destination: Path) -> None:
    """Narrow race-injection seam immediately before no-replace placement."""


def _local_directory_identity(path: Path) -> tuple[int, int]:
    try:
        entry = os.lstat(path)
        if _is_reparse_alias(path) or not stat.S_ISDIR(entry.st_mode):
            raise ValueError
        return entry.st_dev, entry.st_ino
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_CANDIDATE_STATE", "rejected parent identity; inspect candidate locations") from exc


def _local_move_no_replace(source: Path, destination: Path, token: OwnedFileToken, raw: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _resolved_local_path(destination, "rejected candidate")
    parent_identity = _local_directory_identity(destination.parent)
    if os.path.lexists(destination):
        raise ConfigError("FAIL_CANDIDATE_STATE", "rejected collision; inspect candidate locations")
    _before_local_reject_move(source, destination)
    if _local_directory_identity(destination.parent) != parent_identity:
        raise ConfigError("FAIL_CANDIDATE_STATE", "rejected parent changed; candidate preserved")
    try:
        if os.name == "nt":
            os.rename(source, destination)
        else:
            os.link(source, destination)
            if not _owned_file_matches(source, token, links=2):
                raise OSError("hard-link identity changed")
            source.unlink()
    except (FileExistsError, OSError) as exc:
        raise ConfigError("FAIL_CANDIDATE_STATE", "no-replace reject failed; candidate preserved") from exc
    try:
        if (not _owned_file_matches(destination, token, links=1) or destination.read_bytes() != raw
                or os.path.lexists(source)):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise ConfigError("FAIL_CANDIDATE_STATE", "reject move identity changed; inspect both paths") from exc


def _local_reject_restore(destination: Path, source: Path, token: OwnedFileToken, raw: bytes) -> None:
    try:
        if (os.path.lexists(source) or not _owned_file_matches(destination, token, links=1)
                or destination.read_bytes() != raw):
            raise ValueError
        if os.name == "nt":
            os.rename(destination, source)
        else:
            os.link(destination, source)
            if not _owned_file_matches(destination, token, links=2):
                raise OSError("hard-link identity changed")
            destination.unlink()
    except (FileExistsError, OSError, ValueError) as exc:
        raise ConfigError("FAIL_REJECT_RECOVERY", "candidate preserved at rejected path; inspect both paths") from exc
    if not _owned_file_matches(source, token, links=1) or source.read_bytes() != raw:
        raise ConfigError("FAIL_REJECT_RECOVERY", "candidate recovery identity changed; inspect both paths")


def apply_local_reject(workspace: Path, control_root: Path | None, plan: Plan, plan_hash: str, *,
                       config_path: Path | None = None) -> LocalRejectResult:
    if (plan.operation != "lessons-reject" or plan_hash != plan.plan_hash
            or plan.plan_hash != _canonical_hash(plan.payload)):
        raise ConfigError("FAIL_INPUT_CHANGED", "RETRY replan lessons reject")
    payload = plan.payload
    state_value = payload.get("state_root")
    state_root = Path(state_value) if isinstance(state_value, str) else None
    bound_config = Path(payload["config_path"]) if isinstance(payload.get("config_path"), str) else None
    if bound_config is not None:
        if (config_path is None or config_path.resolve() != bound_config.resolve()
                or hashlib.sha256(bound_config.read_bytes()).hexdigest() != payload.get("config_sha256")):
            raise ConfigError("FAIL_INPUT_CHANGED", "config; RETRY replan lessons reject")
    control = _local_control_root(bound_config, control_root, state_root)
    if str(control) != payload.get("control_root"):
        raise ConfigError("FAIL_INPUT_CHANGED", "lock identity; RETRY replan lessons reject")
    with operation_lock(control):
        reviewed = plan_local_reject(
            workspace, control, plan.candidate_id, state_root=state_root, config_path=bound_config,
        )
        if reviewed.plan_hash != plan.plan_hash:
            raise ConfigError("FAIL_INPUT_CHANGED", "reviewed facts; RETRY replan lessons reject")
        payload = reviewed.payload
        repo = Path(payload["operation_root"])
        source = repo / payload["source_path"]
        destination = repo / payload["destination_path"]
        token = _local_file_token(source, "candidate")
        raw = source.read_bytes()
        if (tuple(payload["candidate_identity"]) != (token.device, token.inode, token.size)
                or hashlib.sha256(raw).hexdigest() != payload["candidate_sha256"]):
            raise ConfigError("FAIL_INPUT_CHANGED", "candidate identity; RETRY replan lessons reject")
        _assert_local_index_clean(repo, (source, destination), "lesson reject")
        _local_move_no_replace(source, destination, token, raw)
        stage_paths = tuple(payload["stage_paths"])
        try:
            _git(repo, "add", "--", *stage_paths)
            staged = tuple(filter(None, _git(
                repo, "diff", "--cached", "--name-only", "--no-renames", "--", *stage_paths,
            ).stdout.splitlines()))
            if set(staged) != set(stage_paths):
                raise ConfigError("FAIL_INPUT_CHANGED", "staging paths; inspect Git index")
        except (ConfigError, OSError) as exc:
            _git(repo, "restore", "--staged", "--", *stage_paths, check=False)
            remaining = _git(
                repo, "diff", "--cached", "--name-only", "--no-renames", "--", *stage_paths,
                check=False,
            )
            _local_reject_restore(destination, source, token, raw)
            if remaining.returncode != 0 or remaining.stdout.strip():
                raise ConfigError("FAIL_REJECT_RECOVERY", "Git index recovery incomplete; inspect staged paths") from exc
            raise exc
        return LocalRejectResult(plan.candidate_id, stage_paths)
