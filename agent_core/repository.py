"""Fail-closed repository layout resolution for private state."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unicodedata import normalize

from .config import ConfigError


def _top_level(state_root: Path) -> Path:
    requested = Path(state_root).absolute()
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GCM_INTERACTIVE", "Never")
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={requested.as_posix()}", "-C", str(requested),
             "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("FAIL_STATE_REPOSITORY", f"cannot resolve {requested}: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ConfigError("FAIL_STATE_REPOSITORY", str(requested))
    return Path(result.stdout.strip()).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _is_reparse_alias(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or (callable(junction) and junction())
    except OSError:
        return True


def _has_reparse_alias(path: Path, root: Path) -> bool:
    current = path
    while True:
        if _is_reparse_alias(current):
            return True
        if str(current) == str(root):
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _reserved_shape(repo_root: Path) -> bool:
    for name in ("engine", "state"):
        path = repo_root / name
        if os.path.lexists(path) or _is_reparse_alias(path):
            return True
    return False


@dataclass(frozen=True)
class RepositoryContext:
    """Resolved Git repository and its explicit private-state root."""

    repo_root: Path
    state_root: Path
    state_prefix: str
    layout: str

    def state_to_repo_path(self, relative: str | Path) -> str:
        """Convert an application logical state path to a POSIX Git path."""
        value = normalize("NFC", Path(relative).as_posix()).replace("\\", "/")
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or value == ".":
            raise ConfigError("FAIL_STATE_REPOSITORY", "state-relative path escapes state root")
        return f"{self.state_prefix}{value}"

    def temporary_state_root(self, temporary_worktree: Path) -> Path:
        root = Path(temporary_worktree).resolve()
        return root if self.layout == "standalone" else root / "state"

    def repo_to_state_path(self, relative: str) -> str | None:
        """Map a raw Git-returned repository path without changing separators."""
        value = normalize("NFC", relative)
        if self.layout == "standalone":
            return value
        if not value.startswith(self.state_prefix):
            return None
        return value[len(self.state_prefix):]


def resolve_repository_context(state_root: Path) -> RepositoryContext:
    """Resolve only the legacy standalone or canonical sibling state layout."""

    requested = Path(state_root).absolute()
    repo_root = _top_level(requested)
    resolved_state = requested.resolve()
    if _has_reparse_alias(requested, repo_root):
        raise ConfigError("FAIL_STATE_REPOSITORY", "state root must not traverse a reparse alias")
    if str(requested) == str(repo_root):
        if _reserved_shape(repo_root):
            raise ConfigError("FAIL_STATE_REPOSITORY", "canonical children require an explicit state root")
        return RepositoryContext(repo_root, resolved_state, "", "standalone")

    canonical_state = repo_root / "state"
    engine_root = repo_root / "engine"
    if (
        str(requested) != str(canonical_state)
        or _is_reparse_alias(canonical_state)
        or _is_reparse_alias(engine_root)
        or not canonical_state.is_dir()
        or not engine_root.is_dir()
        or resolved_state != canonical_state.resolve()
        or _is_within(engine_root.resolve(), resolved_state)
        or _is_within(resolved_state, engine_root.resolve())
    ):
        raise ConfigError("FAIL_STATE_REPOSITORY", "unsupported state repository layout")
    return RepositoryContext(repo_root, resolved_state, "state/", "canonical")
