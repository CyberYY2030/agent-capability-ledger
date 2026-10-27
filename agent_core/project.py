"""Project identity adapters for candidate capture and Git-only mutations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ledger
from .config import ConfigError


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    project_id: str
    directory: bool


def check_directory_paths(root: Path) -> None:
    try:
        ledger.check_directory_paths(root)
    except ledger.ProjectRoutingError as exc:
        raise ConfigError("REJECTED", str(exc)) from exc


def resolve_capture_project(workspace: Path, *, allow_directory: bool = True) -> ProjectContext:
    try:
        root, directory = ledger.project_location(workspace)
        if directory and not (root / ".agents" / "lessons.json").exists():
            raise ledger.ProjectRoutingError("scope project_identity_unavailable no_git_root")
        payload = ledger.read_routing(root, strict=directory)
        if directory:
            ledger.validate_directory_routing(payload)
            if not allow_directory:
                raise ledger.ProjectRoutingError("scope directory_capture_requires_explicit_workspace")
        elif payload.get("schema") != ledger.ROUTING_SCHEMA:
            raise ledger.ProjectRoutingError("scope project_identity_unavailable invalid_schema")
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not ledger.PROFILE_RE.fullmatch(project_id):
            raise ledger.ProjectRoutingError("scope project_identity_unavailable invalid_project_id")
    except ledger.ProjectRoutingError as exc:
        raise ConfigError("REJECTED", str(exc)) from exc
    return ProjectContext(root, project_id, directory)


def resolve_project_context(workspace: Path) -> tuple[Path, str]:
    """The existing mutation consumers remain Git-only."""
    context = resolve_capture_project(workspace)
    if context.directory:
        raise ConfigError("REJECTED", "scope directory_candidates_capture_only; promotion unsupported")
    return context.root, context.project_id
