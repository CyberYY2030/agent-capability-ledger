"""Fail-closed Git freshness checks for state mutations and materialization."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from unicodedata import normalize

from .config import ConfigError, HOST_LABEL_RE
from .match import MatchError, parse_when
from .repository import RepositoryContext, _is_reparse_alias, resolve_repository_context


CANDIDATE_SCHEMA = "candidate/1"
CANDIDATE_V2_SCHEMA = "candidate/2"
CANDIDATE_FIELDS = {
    "schema", "id", "created_utc", "host", "agent", "base_revision",
    "rule", "trigger", "cost", "sink", "scope_hint", "evidence",
}
CANDIDATE_V2_FIELDS = {*CANDIDATE_FIELDS, "when"}
CANDIDATE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}T\d{6}Z-[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Freshness:
    head: str
    remote: str | None
    behind: int
    ahead: int
    dirty: tuple[str, ...]
    unmerged: tuple[str, ...]
    offline: bool
    context: RepositoryContext | None = None
    remote_issue: str | None = None
    remote_detail: str | None = None


@dataclass(frozen=True)
class RemoteBaselineFileReview:
    role: str
    path: Path
    exists: bool
    sha256: str | None
    last_known_good: str | None
    status: str
    action: str


@dataclass(frozen=True)
class RemoteBaselineReview:
    legacy: RemoteBaselineFileReview
    current: RemoteBaselineFileReview
    selected_last_known_good: str | None
    desired_sha256: str
    ready: bool


def _git(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    resolved = repo.resolve()
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GCM_INTERACTIVE", "Never")
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), *args],
            check=False, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("FAIL_GIT", f"{args[0]}: {exc}") from exc


def _required_git(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise ConfigError("FAIL_GIT", _git_failure(args, result))
    return result.stdout.strip()


def _git_failure(args: tuple[str, ...], result: subprocess.CompletedProcess[str]) -> str:
    command = " ".join(args[:2])
    output = result.stderr.strip() or result.stdout.strip() or "git command failed"
    first_line = output.splitlines()[0][:240]
    safe = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@", r"\1***@", first_line)
    return f"git {command}: {safe}"


def _confirmed_offline(result: subprocess.CompletedProcess[str]) -> bool:
    detail = (result.stderr or result.stdout).casefold()
    return any(marker in detail for marker in (
        "could not resolve host", "failed to connect", "network is unreachable",
        "connection timed out", "connection refused",
    ))


def is_repository(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def parse_candidate_bytes(raw: bytes, candidate_id: str, *, allow_project: bool = False) -> dict[str, str]:
    """Validate candidate bytes against their already trusted logical identity."""
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_CANDIDATE", "cannot read candidate") from exc
    if not isinstance(payload, dict):
        raise ConfigError("FAIL_CANDIDATE", "candidate fields mismatch")
    schema = payload.get("schema")
    expected_fields = {
        CANDIDATE_SCHEMA: CANDIDATE_FIELDS,
        CANDIDATE_V2_SCHEMA: CANDIDATE_V2_FIELDS,
    }.get(schema)
    if expected_fields is None:
        raise ConfigError("FAIL_CANDIDATE", f"schema must be {CANDIDATE_SCHEMA} or {CANDIDATE_V2_SCHEMA}")
    if set(payload) != expected_fields:
        raise ConfigError("FAIL_CANDIDATE", f"{schema} fields mismatch")
    if not all(isinstance(value, str) and value for value in payload.values()):
        raise ConfigError("FAIL_CANDIDATE", f"all {schema} fields must be non-empty strings")
    if schema == CANDIDATE_V2_SCHEMA:
        try:
            parse_when(payload["when"], source="candidate when")
        except MatchError as exc:
            raise ConfigError("FAIL_CANDIDATE", str(exc)) from exc
    if not CANDIDATE_ID_RE.fullmatch(payload["id"]):
        raise ConfigError("FAIL_CANDIDATE", f"invalid candidate id: {payload['id']}")
    if candidate_id != payload["id"]:
        raise ConfigError("FAIL_CANDIDATE", "candidate filename and id differ")
    if not HOST_LABEL_RE.fullmatch(payload["host"]) or not payload["id"].startswith(payload["host"] + "-"):
        raise ConfigError("FAIL_CANDIDATE", "candidate host and id differ")
    base = payload["base_revision"].removesuffix(" unverified")
    if not SHA_RE.fullmatch(base):
        raise ConfigError("FAIL_CANDIDATE", "base_revision must contain a Git sha")
    project = payload["scope_hint"].removeprefix("project:")
    project_allowed = (
        allow_project
        and payload["scope_hint"].startswith("project:")
        and HOST_LABEL_RE.fullmatch(project) is not None
    )
    if (
        payload["scope_hint"] != "global"
        and not payload["scope_hint"].startswith("profile:")
        and not project_allowed
    ):
        raise ConfigError("FAIL_CANDIDATE", "scope_hint must be global or profile:<id>")
    return payload


def load_candidate(path: Path, *, allow_project: bool = False) -> dict[str, str]:
    """Safely load a candidate file, then apply the pure bytes validator."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError("FAIL_CANDIDATE", f"cannot read {path}: {exc}") from exc
    return parse_candidate_bytes(raw, path.stem, allow_project=allow_project)


def _candidate_allowed(context: RepositoryContext, state_relative: str | None) -> bool:
    if (
        state_relative is None
        or not state_relative.startswith("inbox/")
        or state_relative.startswith("inbox/consumed/")
        or "/" in state_relative[len("inbox/"):]
    ):
        return False
    inbox = context.state_root / "inbox"
    path = context.state_root / Path(state_relative)
    if _is_reparse_alias(inbox) or _is_reparse_alias(path) or not path.is_file():
        return False
    try:
        if path.resolve().parent != inbox.resolve():
            return False
        load_candidate(path)
    except (ConfigError, OSError):
        return False
    return True


def _parse_status(context: RepositoryContext, raw: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    dirty: list[str] = []
    unmerged: list[str] = []
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2]
        path_text = normalize("NFC", record[3:])
        if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
            if index < len(records):
                index += 1
        if "U" in code or code in {"AA", "DD"}:
            unmerged.append(path_text)
            continue
        if code == "??":
            state_relative = context.repo_to_state_path(path_text)
            if _candidate_allowed(context, state_relative):
                continue
        dirty.append(path_text)
    return tuple(sorted(dirty)), tuple(sorted(unmerged))


def _status(context: RepositoryContext) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = _required_git(context.repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return _parse_status(context, raw)


def _state_path(control_root: Path) -> Path:
    return control_root.resolve() / "remote-state.json"


def remote_state_path(config_path: Path) -> Path:
    return config_path.resolve().parent / "txn" / "remote-state.json"


def _is_alias(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(callable(junction) and junction())


def _remote_state_bytes(path: Path) -> tuple[bytes, str]:
    if _is_alias(path) or not path.is_file():
        raise ConfigError("FAIL_REMOTE_STATE", f"not an ordinary file: {path}")
    try:
        if path.stat().st_nlink != 1:
            raise ConfigError("FAIL_REMOTE_STATE", f"remote baseline has aliases: {path}")
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_REMOTE_STATE", f"invalid remote baseline: {path}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"last_known_good"}
        or not isinstance(payload.get("last_known_good"), str)
        or SHA_RE.fullmatch(payload["last_known_good"]) is None
    ):
        raise ConfigError("FAIL_REMOTE_STATE", f"invalid remote baseline: {path}")
    return raw, payload["last_known_good"]


def render_remote_state(sha: str) -> bytes:
    if SHA_RE.fullmatch(sha) is None:
        raise ConfigError("FAIL_REMOTE_SHA", sha)
    return (json.dumps({"last_known_good": sha}, sort_keys=True) + "\n").encode("utf-8")


def _baseline_file(role: str, path: Path) -> RemoteBaselineFileReview:
    if not os.path.lexists(path):
        return RemoteBaselineFileReview(role, path.resolve(), False, None, None, "absent", "none")
    raw, known = _remote_state_bytes(path)
    return RemoteBaselineFileReview(
        role, path.resolve(), True, sha256(raw).hexdigest(), known, "valid", "none",
    )


def review_remote_state(
    config_path: Path,
    repo: Path,
    remote_revision: str,
) -> RemoteBaselineReview:
    """Review install baseline inputs without fetching or writing host state."""
    resolved_config = config_path.resolve()
    current = _baseline_file("current", remote_state_path(resolved_config))
    legacy = _baseline_file("legacy", resolved_config.parent / "remote-state.json")
    default_config = (Path.home() / ".agent-core" / "host.json").resolve()
    selected = current if current.exists else (
        legacy if resolved_config == default_config and legacy.exists else None
    )
    selected_role = selected.role if selected is not None else None
    if current.exists:
        current = RemoteBaselineFileReview(
            current.role, current.path, current.exists, current.sha256, current.last_known_good,
            current.status, "use-current",
        )
        if legacy.exists:
            legacy = RemoteBaselineFileReview(
                legacy.role, legacy.path, legacy.exists, legacy.sha256, legacy.last_known_good,
                legacy.status, "preserve-ignored",
            )
    elif selected is legacy:
        legacy = RemoteBaselineFileReview(
            legacy.role, legacy.path, legacy.exists, legacy.sha256, legacy.last_known_good,
            legacy.status, "validate-legacy-and-publish",
        )
        current = RemoteBaselineFileReview(
            current.role, current.path, current.exists, current.sha256, current.last_known_good,
            current.status, "publish-reviewed-remote",
        )
    else:
        current = RemoteBaselineFileReview(
            current.role, current.path, current.exists, current.sha256, current.last_known_good,
            current.status, "publish-reviewed-remote",
        )
        if legacy.exists:
            legacy = RemoteBaselineFileReview(
                legacy.role, legacy.path, legacy.exists, legacy.sha256, legacy.last_known_good,
                "not-applicable", "preserve-ignored",
            )

    ready = True
    selected_known = selected.last_known_good if selected is not None else None
    if selected_known is not None:
        context = resolve_repository_context(repo)
        ancestry = _git(
            context.repo_root, "merge-base", "--is-ancestor", selected_known, remote_revision,
        )
        if ancestry.returncode != 0:
            ready = False
            if selected_role == "current":
                current = RemoteBaselineFileReview(
                    current.role, current.path, current.exists, current.sha256,
                    current.last_known_good, "remote-rewind", "block",
                )
            else:
                legacy = RemoteBaselineFileReview(
                    legacy.role, legacy.path, legacy.exists, legacy.sha256,
                    legacy.last_known_good, "remote-rewind", "block",
                )
    desired = render_remote_state(remote_revision)
    return RemoteBaselineReview(
        legacy, current, selected_known, sha256(desired).hexdigest(), ready,
    )


def last_known_remote(control_root: Path) -> str | None:
    path = _state_path(control_root)
    if not os.path.lexists(path):
        return None
    _raw, value = _remote_state_bytes(path)
    return value


def migrate_legacy_remote_state(
    config_path: Path,
    repo: Path,
    *,
    lock_token: object,
) -> bool:
    """Copy the one legacy default-host baseline without deleting its source."""
    from .materializer import require_lock_token

    resolved_config = config_path.resolve()
    control_root = resolved_config.parent / "txn"
    require_lock_token(lock_token, control_root)
    current = _state_path(control_root)
    if os.path.lexists(current):
        _remote_state_bytes(current)
        return False
    default_config = (Path.home() / ".agent-core" / "host.json").resolve()
    if resolved_config != default_config:
        return False
    legacy = resolved_config.parent / "remote-state.json"
    if not os.path.lexists(legacy):
        return False
    raw, _known = _remote_state_bytes(legacy)
    control_root.mkdir(parents=True, exist_ok=True)
    if _is_alias(control_root):
        raise ConfigError("FAIL_REMOTE_STATE", f"control root is an alias: {control_root}")
    handle, temporary_name = tempfile.mkstemp(prefix="remote-state-migrate-", suffix=".tmp", dir=control_root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, current)
        except FileExistsError as exc:
            raise ConfigError("FAIL_REMOTE_STATE_RACE", str(current)) from exc
        except OSError as exc:
            raise ConfigError("FAIL_REMOTE_STATE_RACE", str(current)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    copied, _value = _remote_state_bytes(current)
    if copied != raw:
        raise ConfigError("FAIL_REMOTE_STATE_RACE", str(current))
    return True


def record_remote_head(control_root: Path, sha: str) -> None:
    desired = render_remote_state(sha)
    path = _state_path(control_root)
    if os.path.lexists(path):
        _remote_state_bytes(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_alias(path.parent):
        raise ConfigError("FAIL_REMOTE_STATE", f"control root is an alias: {path.parent}")
    handle, temporary_name = tempfile.mkstemp(prefix="remote-state-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(desired.decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect(
    repo: Path,
    control_root: Path,
    *,
    fetch: bool = True,
    reviewed_known_remote: str | None = None,
    baseline_reviewed: bool = False,
) -> Freshness:
    context = resolve_repository_context(repo)
    repository = context.repo_root
    head = _required_git(repository, "rev-parse", "HEAD")
    offline = False
    remote_issue: str | None = None
    remote_detail: str | None = None
    origin = _git(repository, "remote", "get-url", "origin")
    if origin.returncode != 0:
        remote_issue = "origin-missing"
        remote_detail = _git_failure(("remote", "get-url", "origin"), origin)
    elif fetch:
        fetched = _git(repository, "fetch", "origin", "--quiet")
        if fetched.returncode != 0:
            offline = _confirmed_offline(fetched)
            remote_issue = "fetch-failed"
            remote_detail = _git_failure(("fetch", "origin", "--quiet"), fetched)
    remote = None
    if remote_issue is None:
        remote_result = _git(repository, "rev-parse", "--verify", "origin/main")
        if remote_result.returncode == 0:
            remote = remote_result.stdout.strip()
        else:
            remote_issue = "main-missing"
            remote_detail = _git_failure(
                ("rev-parse", "--verify", "origin/main"), remote_result,
            )
    dirty, unmerged = _status(context)
    behind = ahead = 0
    if remote is not None:
        counts = _required_git(repository, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
        ahead, behind = (int(counts[0]), int(counts[1]))
        known = reviewed_known_remote if baseline_reviewed else last_known_remote(control_root)
        if known and _git(repository, "merge-base", "--is-ancestor", known, remote).returncode != 0:
            raise ConfigError("FAIL_REMOTE_REWIND", f"last={known} remote={remote}")
    return Freshness(
        head, remote, behind, ahead, dirty, unmerged, offline, context,
        remote_issue, remote_detail,
    )


def require_fresh(
    repo: Path,
    operation: str,
    control_root: Path,
    *,
    fetch: bool = True,
    reviewed_known_remote: str | None = None,
    baseline_reviewed: bool = False,
) -> Freshness:
    state = inspect(
        repo, control_root, fetch=fetch,
        reviewed_known_remote=reviewed_known_remote, baseline_reviewed=baseline_reviewed,
    )
    if state.unmerged:
        raise ConfigError("FAIL_CONFLICT", ",".join(state.unmerged))
    if state.dirty:
        raise ConfigError("FAIL_DIRTY", ",".join(state.dirty))
    if operation == "capture":
        return state
    if state.remote_issue is not None:
        if operation == "doctor":
            code = {
                "origin-missing": "FAIL_REMOTE_ORIGIN",
                "fetch-failed": "FAIL_REMOTE_FETCH",
                "main-missing": "FAIL_REMOTE_REF",
            }[state.remote_issue]
        else:
            code = "REMOTE_REQUIRED"
        raise ConfigError(code, state.remote_detail or state.remote_issue)
    if state.behind:
        code = (
            "FAIL_STALE" if operation == "promote"
            else "FAIL_REMOTE_DIVERGED" if operation == "doctor"
            else "FAIL_DIVERGED"
        )
        raise ConfigError(code, f"ahead={state.ahead} behind={state.behind}")
    if state.ahead:
        code = "FAIL_REMOTE_DIVERGED" if operation == "doctor" else "FAIL_DIVERGED"
        raise ConfigError(code, f"ahead={state.ahead} behind={state.behind}")
    return state
