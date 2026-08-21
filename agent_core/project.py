"""Shared project routing identity resolution."""

from __future__ import annotations

import json
from pathlib import Path

from . import ledger
from .config import ConfigError


def resolve_project_context(workspace: Path) -> tuple[Path, str]:
    root_text = ledger.find_git_root(str(workspace))
    if not root_text:
        raise ConfigError("REJECTED", "scope project_identity_unavailable no_git_root")
    root = Path(root_text).resolve()
    config_path = root / ".agents" / "lessons.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            "REJECTED", f"scope project_identity_unavailable lessons_config:{exc}"
        ) from exc
    if payload.get("schema") != ledger.ROUTING_SCHEMA:
        raise ConfigError("REJECTED", "scope project_identity_unavailable invalid_schema")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ledger.PROFILE_RE.fullmatch(project_id):
        raise ConfigError("REJECTED", "scope project_identity_unavailable invalid_project_id")
    return root, project_id
