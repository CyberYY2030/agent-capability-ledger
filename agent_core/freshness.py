"""Fail-closed Git freshness checks for state mutations and materialization."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
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
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ConfigError("FAIL_GIT", detail)
    return result.stdout.strip()


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


def last_known_remote(control_root: Path) -> str | None:
    path = _state_path(control_root)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_known_good")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and SHA_RE.fullmatch(value) else None


def record_remote_head(control_root: Path, sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ConfigError("FAIL_REMOTE_SHA", sha)
    path = _state_path(control_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix="remote-state-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"last_known_good": sha}, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect(repo: Path, control_root: Path, *, fetch: bool = True) -> Freshness:
    context = resolve_repository_context(repo)
    repository = context.repo_root
    head = _required_git(repository, "rev-parse", "HEAD")
    offline = False
    if fetch:
        fetched = _git(repository, "fetch", "origin", "--quiet")
        offline = fetched.returncode != 0
    remote_result = _git(repository, "rev-parse", "--verify", "origin/main")
    remote = remote_result.stdout.strip() if remote_result.returncode == 0 else None
    if fetch and offline:
        remote = None
    dirty, unmerged = _status(context)
    behind = ahead = 0
    if remote is not None:
        counts = _required_git(repository, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
        ahead, behind = (int(counts[0]), int(counts[1]))
        known = last_known_remote(control_root)
        if known and _git(repository, "merge-base", "--is-ancestor", known, remote).returncode != 0:
            raise ConfigError("FAIL_REMOTE_REWIND", f"last={known} remote={remote}")
    return Freshness(head, remote, behind, ahead, dirty, unmerged, offline or remote is None, context)


def require_fresh(repo: Path, operation: str, control_root: Path, *, fetch: bool = True) -> Freshness:
    state = inspect(repo, control_root, fetch=fetch)
    if state.unmerged:
        raise ConfigError("FAIL_CONFLICT", ",".join(state.unmerged))
    if state.dirty:
        raise ConfigError("FAIL_DIRTY", ",".join(state.dirty))
    if operation == "capture":
        return state
    if state.offline:
        code = "FAIL_REMOTE_PARITY" if operation == "doctor" else "REMOTE_REQUIRED"
        raise ConfigError(code, f"{operation} requires origin/main")
    if state.behind:
        code = "FAIL_STALE" if operation == "promote" else "FAIL_DIVERGED"
        raise ConfigError(code, f"behind={state.behind}")
    if state.ahead:
        raise ConfigError("FAIL_DIVERGED", f"ahead={state.ahead}")
    return state
