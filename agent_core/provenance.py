"""Read-only validation of canonical chained engine provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import ConfigError
from .repository import _is_reparse_alias


RECORD = "engine.provenance.json"
MANIFEST = "engine/release-manifest.json"
RECORD_KEYS = {
    "schema", "sequence", "previous_record_sha256", "engine_tree_oid", "release_artifact_sha256",
}
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class EngineProvenance:
    sequence: int
    record_sha256: str
    engine_tree_oid: str
    release_artifact_sha256: str


class EngineLayout(str, Enum):
    CANONICAL = "canonical"
    STANDALONE = "standalone"


def _fail(detail: str) -> None:
    raise ConfigError("FAIL_ENGINE_PROVENANCE", detail)


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *args],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "git unavailable") from exc
    if result.returncode != 0:
        _fail("git validation failed")
    return result.stdout


def _top_level(engine_root: Path) -> Path:
    raw = _git(engine_root, "rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()
    if not raw:
        _fail("repository missing")
    return Path(raw).resolve()


def _has_alias(path: Path, root: Path) -> bool:
    current = path
    while True:
        if _is_reparse_alias(current):
            return True
        try:
            if current.resolve() == root.resolve():
                return False
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _classify_engine_layout(engine_root: Path) -> tuple[EngineLayout, Path, Path]:
    requested = Path(engine_root).absolute()
    repo = _top_level(requested)
    engine = repo / "engine"
    state = repo / "state"
    if _has_alias(requested, repo):
        _fail("engine path alias")
    if requested.resolve() == engine.resolve():
        if (
            _has_alias(engine, repo) or _has_alias(state, repo)
            or _is_reparse_alias(engine) or _is_reparse_alias(state)
            or not engine.is_dir() or not state.is_dir()
        ):
            _fail("canonical layout invalid")
        return EngineLayout.CANONICAL, repo, engine
    if requested.resolve() == repo.resolve():
        if any(os.path.lexists(repo / name) or _is_reparse_alias(repo / name) for name in ("engine", "state")):
            _fail("standalone layout ambiguous")
        return EngineLayout.STANDALONE, repo, requested
    _fail("engine layout invalid")


def classify_engine_layout(engine_root: Path) -> EngineLayout:
    """Classify only proven canonical or standalone engine roots; otherwise fail closed."""
    try:
        layout, _repo, _engine = _classify_engine_layout(engine_root)
        return layout
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "engine layout unreadable") from exc


def _tracked_file(repo: Path, path: Path, relative: str) -> bytes:
    if _is_reparse_alias(path) or not path.is_file():
        _fail("required file invalid")
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), "ls-files",
         "--error-unmatch", "--", relative],
        check=False, capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        _fail("required file untracked")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "required file unreadable") from exc


def _record(raw: bytes) -> dict[str, Any]:
    try:
        value: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "record invalid") from exc
    if not isinstance(value, dict) or set(value) != RECORD_KEYS or value.get("schema") != "engine-provenance/1":
        _fail("record fields invalid")
    if type(value["sequence"]) is not int or value["sequence"] < 1:
        _fail("record sequence invalid")
    previous = value["previous_record_sha256"]
    if previous is not None and (not isinstance(previous, str) or SHA256_RE.fullmatch(previous) is None):
        _fail("record previous invalid")
    if not isinstance(value["engine_tree_oid"], str) or OID_RE.fullmatch(value["engine_tree_oid"]) is None:
        _fail("record tree invalid")
    if (not isinstance(value["release_artifact_sha256"], str)
            or BASE64URL_RE.fullmatch(value["release_artifact_sha256"]) is None):
        _fail("record artifact invalid")
    return value


def _manifest(raw: bytes) -> str:
    try:
        value: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "manifest invalid") from exc
    aggregate = value.get("artifact_sha256") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema") != "release-manifest/1"
            or not isinstance(aggregate, str) or BASE64URL_RE.fullmatch(aggregate) is None):
        _fail("manifest binding invalid")
    return aggregate


def _tree_oid(repo: Path, revision: str) -> str:
    value = _git(repo, "rev-parse", f"{revision}:engine").decode("ascii", "strict").strip()
    if OID_RE.fullmatch(value) is None:
        _fail("engine tree missing")
    return value


def _blob(repo: Path, revision: str, path: str) -> bytes:
    return _git(repo, "show", f"{revision}:{path}")


def _dirty(repo: Path) -> None:
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "engine", RECORD)
    if raw:
        _fail("working tree dirty")


def is_canonical_engine_layout(engine_root: Path) -> bool:
    """Return whether this is exactly the canonical sibling engine root."""
    return classify_engine_layout(engine_root) is EngineLayout.CANONICAL


def validate_engine_provenance(engine_root: Path) -> EngineProvenance:
    """Validate the canonical engine record and its complete first-parent chain."""
    try:
        layout, repo, engine = _classify_engine_layout(engine_root)
        if layout is not EngineLayout.CANONICAL:
            _fail("canonical provenance required")
        record_path = repo / RECORD
        manifest_path = engine / "release-manifest.json"
        current_raw = _tracked_file(repo, record_path, RECORD)
        manifest_raw = _tracked_file(repo, manifest_path, MANIFEST)
        current = _record(current_raw)
        if _manifest(manifest_raw) != current["release_artifact_sha256"]:
            _fail("current artifact differs")
        _dirty(repo)
        history = _git(repo, "log", "--first-parent", "--format=%H", "--", RECORD).decode("ascii", "strict").splitlines()
        if not history:
            _fail("record history missing")
        history.reverse()
        previous_raw: bytes | None = None
        latest: dict[str, Any] | None = None
        for expected_sequence, revision in enumerate(history, start=1):
            raw = _blob(repo, revision, RECORD)
            record = _record(raw)
            if record["sequence"] != expected_sequence:
                _fail("record sequence chain invalid")
            previous = None if previous_raw is None else hashlib.sha256(previous_raw).hexdigest()
            if record["previous_record_sha256"] != previous:
                _fail("record previous chain invalid")
            if record["engine_tree_oid"] != _tree_oid(repo, revision):
                _fail("record tree chain invalid")
            if record["release_artifact_sha256"] != _manifest(_blob(repo, revision, MANIFEST)):
                _fail("record artifact chain invalid")
            previous_raw, latest = raw, record
        if latest is None or previous_raw is None or current != latest:
            _fail("current record differs from history")
        head_tree = _tree_oid(repo, "HEAD")
        if current["engine_tree_oid"] != head_tree:
            _fail("head engine tree differs")
        return EngineProvenance(
            current["sequence"], hashlib.sha256(previous_raw).hexdigest(), head_tree,
            current["release_artifact_sha256"],
        )
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_ENGINE_PROVENANCE", "provenance unreadable") from exc
