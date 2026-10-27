from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path

from .config import ConfigError, HOST_LABEL_RE
from .freshness import DIRECTORY_CANDIDATE_SCHEMA, load_candidate, require_fresh
from .match import parse_markdown, tokenize


INBOX = Path("inbox")


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
    directory_project: bool = False,
) -> Path:
    """Create one schema-valid candidate with exclusive, durable file creation."""
    if not HOST_LABEL_RE.fullmatch(host):
        raise ConfigError("FAIL_CANDIDATE", "host must be a privacy-safe kebab-case label")
    values = (agent, rule, trigger, cost, sink, scope_hint, evidence)
    if any(not isinstance(value, str) or not value for value in values):
        raise ConfigError("FAIL_CANDIDATE", "candidate fields must be non-empty strings")
    if directory_project:
        from .project import check_directory_paths
        if (require_state_freshness or not allow_project or when is None or base_revision is not None
                or inbox_path != repo / ".agents" / "inbox"):
            raise ConfigError("FAIL_CANDIDATE", "invalid directory capture contract")
        check_directory_paths(repo)
    if require_state_freshness:
        state = require_fresh(repo, "capture", control_root)
        if base_revision is None:
            base_revision = state.remote or f"{state.head} unverified"
    elif base_revision is None and not directory_project:
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
        if directory_project:
            payload["schema"] = DIRECTORY_CANDIDATE_SCHEMA
            del payload["base_revision"]
            payload["source_kind"] = "directory"
            check_directory_paths(repo)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            load_candidate(path, allow_project=allow_project, allow_directory=directory_project)
            return path
        except FileExistsError:
            continue
        except Exception:
            path.unlink(missing_ok=True)
            raise
    raise ConfigError("FAIL_CANDIDATE_COLLISION", host)


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
