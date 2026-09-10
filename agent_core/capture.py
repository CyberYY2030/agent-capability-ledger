"""Low-friction, append-only lesson candidate capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from . import ledger, privacy
from .config import ConfigError, default_config_path, load_config
from .freshness import load_candidate
from .match import MatchError, parse_when
from .project import resolve_project_context
from .promote import _similarities, create_candidate, operation_lock


ENGINE_ROOT = Path(__file__).resolve().parents[1]
STATE_CAPTURE_PATH_RULES = tuple(
    rule for rule in privacy.ABSOLUTE_PATH_RULES if rule.rule_id != "home_reference"
)
STATE_CAPTURE_RULES = (*STATE_CAPTURE_PATH_RULES, *privacy.SENSITIVE_IDENTITY_RULES)
PROJECT_CAPTURE_RULES = (
    *privacy.CAPTURE_ABSOLUTE_PATH_RULES,
    *privacy.SENSITIVE_IDENTITY_RULES,
)
PATH_RULE_IDS = {rule.rule_id for rule in privacy.CAPTURE_ABSOLUTE_PATH_RULES}
RULE_RETRY = "RETRY use --rule '当 <可观察触发>，先 <一个原子动作>'"
CAPTURE_SENTINEL = "agent-core-capture"
CAPTURE_FIELDS = ("rule", "trigger", "cost", "sink")
CAPTURE_MESSAGE_LIMIT = 64 * 1024
CAPTURE_SCAN_FILE_LIMIT = 4096
CAPTURE_SCAN_BYTE_LIMIT = 4 * 1024 * 1024
CAPTURE_WARNING = "WARNING automatic lesson capture unavailable"
AUTOMATIC_CAPTURE_GATES = frozenset({
    "payload-event",
    "payload-active-missing",
    "payload-active",
    "payload-output",
    "parse",
    "identity",
    "lock",
    "scan",
    "capture",
})


def _automatic_capture_warning(gate: str) -> str:
    if gate not in AUTOMATIC_CAPTURE_GATES:
        raise AssertionError("unknown automatic capture gate")
    return f"{CAPTURE_WARNING} gate={gate}"


def _state_root(config: dict, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    value = config["state_root"]
    if value.startswith("<") and value.endswith(">"):
        raise ConfigError("FAIL_STATE_UNBOUND", "use --state or configure state_root")
    return Path(value).expanduser().resolve()


def _project_identity(workspace: Path) -> tuple[Path, str]:
    return resolve_project_context(workspace)


def _local_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), "rev-parse", "HEAD"],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40:
        raise ConfigError("REJECTED", "scope base_revision_unavailable no_local_head")
    return value


def _missing(value: str, field: str) -> None:
    if value.strip():
        return
    raise ConfigError(
        "REJECTED", f"{field} missing; RETRY agent-core lessons capture --{field} <{field}>"
    )


def _validate_rule(rule: str, *, strict: bool) -> str | None:
    """Reject inline markdown always; require the executable form only for candidate/2.

    Inline `**` truncates the rendered rule mid-sentence, so it is rejected on every
    path. The `当 ...，先 ...` form is enforced only when the capture carries a `when`
    predicate: candidate/1 captures include the frozen seed corpus, which must keep
    passing unchanged, so they get a visible warning instead of a rejection.
    """
    if "**" in rule:
        raise ConfigError("REJECTED", f"rule inline_markdown; {RULE_RETRY}")
    match = re.fullmatch(r"当\s*(.+?)\s*[，,]\s*先\s*(.+?)[。.]?", rule.strip())
    conforming = match is not None and not any(
        token in match.group(2)
        for token in ("、", "；", ";", "并且", "然后", "再", "以及", "同时")
    )
    if conforming:
        return None
    if strict:
        raise ConfigError("REJECTED", f"rule format; {RULE_RETRY}")
    return f"RULE_FORMAT_WARNING {RULE_RETRY}"


def _reject_private_values(
    values: dict[str, str], rules: tuple[privacy.Rule, ...],
    forbidden: tuple[str, ...] = (),
) -> None:
    for field, value in values.items():
        matched = next((rule.rule_id for rule in rules if rule.regex.search(value)), None)
        root_match = any(token and token in value for token in forbidden)
        if matched is None and not root_match:
            continue
        category = "absolute_path" if matched in PATH_RULE_IDS or root_match else matched
        if category == "absolute_path":
            if matched == "absolute_posix_path":
                retry = (
                    f"replace --{field} absolute path-like value; it looks like an absolute "
                    "path, so use a repository-relative reference"
                )
            else:
                retry = (
                    f"replace --{field} absolute path with a repository-relative reference"
                )
        else:
            retry = f"replace --{field} sensitive value with a privacy-safe label"
        raise ConfigError("REJECTED", f"privacy {category} {field}; RETRY {retry}")


def _similarity_lines(root: Path, scope_hint: str, rule: str) -> tuple[str, ...]:
    if scope_hint == "global":
        ledger_path = root / "experience" / "LESSONS.md"
    elif scope_hint.startswith("profile:"):
        ledger_path = root / "experience" / "profiles" / scope_hint.removeprefix("profile:") / "LESSONS.md"
    else:
        ledger_path = root / ".agents" / "LESSONS.md"
    if not ledger_path.is_file():
        return ()
    return tuple(
        f"SIMILAR {lesson_id} {score:.3f}"
        for lesson_id, score in _similarities(ledger_path.read_text(encoding="utf-8"), rule)
    )


def _project_capture_sources(
    config: dict, explicit_state: Path | None, project_root: Path, project_id: str,
) -> tuple[tuple[str, str, str], ...]:
    """Resolve project capture stores without turning an offline project into a state operation."""
    project_only = (("project", project_id, str(project_root / ".agents" / "LESSONS.md")),)
    try:
        state = _state_root(config, explicit_state)
    except ConfigError as exc:
        if exc.code == "FAIL_STATE_UNBOUND":
            return project_only
        raise
    global_ledger = state / "experience" / "LESSONS.md"
    if not global_ledger.is_file():
        return project_only
    sources, errors, _warnings = ledger.resolve_sources(str(global_ledger), str(project_root))
    if errors:
        raise ConfigError("FAIL_LESSON_ROUTING", "configured lessons sources")
    return tuple(sources)


def _resolved_similarity_lines(sources: tuple[tuple[str, str, str], ...], rule: str) -> tuple[str, ...]:
    lines: list[str] = []
    for scope, store, source in sources:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError("FAIL_LESSON_ROUTING", "configured lessons source") from exc
        for lesson_id, score in _similarities(text, rule):
            lines.append(f"SIMILAR scope={scope} store={store} id={lesson_id} score={score:.3f}")
    return tuple(lines)


def capture(
    *, config_path: Path, explicit_state: Path | None, control_root: Path,
    workspace: Path, agent: str, rule: str, trigger: str, cost: str,
    sink: str, scope: str, evidence: str, when: str | None = None,
    require_state_freshness: bool = True, base_revision: str | None = None,
    include_advisories: bool = True,
) -> tuple[Path, tuple[str, ...]]:
    for field, value in (("trigger", trigger), ("cost", cost), ("sink", sink)):
        _missing(value, field)
    if not agent.strip() or not rule.strip() or not evidence.strip():
        raise ConfigError("REJECTED", "candidate agent, rule, and evidence must be non-empty")
    config = load_config(config_path)
    host = config["host_label"]
    project_scope = scope == "auto" or scope.startswith("project:")
    project_root: Path | None = None
    project_id: str | None = None
    if project_scope:
        project_root, project_id = _project_identity(workspace)
        expected_scope = f"project:{project_id}"
        if scope not in {"auto", expected_scope}:
            raise ConfigError("REJECTED", f"scope project_identity_mismatch expected={expected_scope}")
    elif scope != "global" and not scope.startswith("profile:"):
        raise ConfigError("REJECTED", f"scope invalid {scope}")

    private_values = {
        "agent": agent, "rule": rule, "trigger": trigger,
        "cost": cost, "sink": sink, "evidence": evidence,
    }
    if when is not None:
        private_values["when"] = when
    capture_rules = (
        PROJECT_CAPTURE_RULES if project_scope else STATE_CAPTURE_RULES
    )
    _reject_private_values(private_values, capture_rules)
    rule_warning = _validate_rule(rule, strict=when is not None)
    if when is not None:
        try:
            parse_when(when, source="when")
        except MatchError as exc:
            raise ConfigError("REJECTED", f"when invalid {exc}; RETRY --when <canonical-json>") from exc

    if project_scope:
        assert project_root is not None and project_id is not None
        forbidden = {str(project_root), str(project_root).replace("\\", "/")}
        _reject_private_values(private_values, (), tuple(forbidden))
        base_revision = f"{_local_head(project_root)} unverified"
        sources = _project_capture_sources(config, explicit_state, project_root, project_id)
        lines = list(_resolved_similarity_lines(sources, rule))
        path = create_candidate(
            project_root, control_root, host=host, agent=agent, rule=rule,
            trigger=trigger, cost=cost, sink=sink, scope_hint=expected_scope,
            evidence=evidence, base_revision=base_revision,
            inbox_path=project_root / ".agents" / "inbox",
            require_state_freshness=False, allow_project=True,
            when=when,
        )
        if rule_warning:
            lines.insert(0, rule_warning)
        return path, tuple(lines)
    state = _state_root(config, explicit_state)
    path = create_candidate(
        state, control_root, host=host, agent=agent, rule=rule, trigger=trigger,
        cost=cost, sink=sink, scope_hint=scope, evidence=evidence,
        when=when, require_state_freshness=require_state_freshness,
        base_revision=base_revision,
    )
    if not include_advisories:
        return path, ()
    lines = list(_similarity_lines(state, scope, rule))
    if scope == "global":
        root_text = ledger.find_git_root(str(workspace))
        if root_text:
            project_root = Path(root_text)
            names = {project_root.name.casefold()}
            config_file = project_root / ".agents" / "lessons.json"
            try:
                project_id = json.loads(config_file.read_text(encoding="utf-8")).get("project_id")
                if isinstance(project_id, str):
                    names.add(project_id.casefold())
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            folded = rule.casefold()
            if any(name and name in folded for name in names):
                lines.insert(0, "SCOPE_WARNING project scope may be narrower")
    if rule_warning:
        lines.insert(0, rule_warning)
    return path, tuple(lines)


def _strict_capture_object(raw: str) -> dict[str, str]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError("REJECTED", "automatic capture malformed JSON") from exc
    if not isinstance(value, dict) or set(value) != set(CAPTURE_FIELDS):
        raise ConfigError("REJECTED", "automatic capture fields mismatch")
    if any(not isinstance(value[field], str) or not value[field].strip() for field in CAPTURE_FIELDS):
        raise ConfigError("REJECTED", "automatic capture fields must be non-empty strings")
    try:
        for field in CAPTURE_FIELDS:
            value[field].encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("REJECTED", "automatic capture fields are not UTF-8") from exc
    return {field: value[field] for field in CAPTURE_FIELDS}


def parse_completion_capture(message: str) -> dict[str, str] | None:
    if not isinstance(message, str) or not message:
        return None
    try:
        encoded = message.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigError("REJECTED", "automatic capture output is not UTF-8") from exc
    if len(encoded) > CAPTURE_MESSAGE_LIMIT:
        raise ConfigError("REJECTED", "automatic capture output exceeds limit")
    lines = message.splitlines()
    sentinels: list[int] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        marker = "```" if stripped.startswith("```") else ("~~~" if stripped.startswith("~~~") else None)
        if marker is not None:
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None and line == CAPTURE_SENTINEL:
            sentinels.append(index)
    if not sentinels:
        return None
    if len(sentinels) != 1:
        raise ConfigError("REJECTED", "automatic capture block count")
    index = sentinels[0]
    if index + 1 >= len(lines):
        raise ConfigError("REJECTED", "automatic capture JSON missing")
    if any(line for line in lines[index + 2:]):
        raise ConfigError("REJECTED", "automatic capture trailing content")
    return _strict_capture_object(lines[index + 1])


def _request_sha256(fields: Mapping[str, str]) -> str:
    raw = json.dumps(
        {field: fields[field] for field in CAPTURE_FIELDS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _duplicate_request(state: Path, token: str) -> bool:
    files = 0
    total = 0
    for inbox in (state / "inbox", state / "inbox" / "consumed"):
        if not inbox.exists():
            continue
        if not inbox.is_dir() or inbox.is_symlink():
            raise ConfigError("REJECTED", "automatic capture inbox identity")
        for path in sorted(inbox.glob("*.md")):
            files += 1
            if files > CAPTURE_SCAN_FILE_LIMIT:
                raise ConfigError("REJECTED", "automatic capture scan budget")
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ConfigError("REJECTED", "automatic capture candidate identity")
            total += info.st_size
            if total > CAPTURE_SCAN_BYTE_LIMIT:
                raise ConfigError("REJECTED", "automatic capture scan budget")
            try:
                candidate = load_candidate(path)
            except (ConfigError, OSError, UnicodeError, ValueError):
                continue
            if token in candidate["evidence"].split():
                return True
    return False


def automatic_completion_capture(
    payload: Mapping[str, object], *, runtime: str, stage: str,
    ledger_path: Path, config_path: Path | None = None,
) -> str | None:
    """Fail-open Claude Stop adapter; return one bounded warning or None."""
    if runtime != "claude-code" or stage != "completion":
        return None

    try:
        if payload.get("hook_event_name") != "Stop":
            return _automatic_capture_warning("payload-event")
    except Exception:
        return _automatic_capture_warning("payload-event")

    try:
        if "stop_hook_active" not in payload:
            return _automatic_capture_warning("payload-active-missing")
        if payload.get("stop_hook_active") is not False:
            return _automatic_capture_warning("payload-active")
    except Exception:
        return _automatic_capture_warning("payload-active")

    try:
        message = payload.get("last_assistant_message")
        if not isinstance(message, str) or not message:
            return _automatic_capture_warning("payload-output")
    except Exception:
        return _automatic_capture_warning("payload-output")

    try:
        fields = parse_completion_capture(message)
        if fields is None:
            return None
        request_digest = _request_sha256(fields)
        output_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        token = f"request-sha256:{request_digest}"
        evidence = (
            f"claude-code-stop {token} "
            f"assistant-output-sha256:{output_digest}"
        )
    except Exception:
        return _automatic_capture_warning("parse")

    try:
        resolved_config = (config_path or default_config_path(ENGINE_ROOT)).resolve()
        config = load_config(resolved_config)
        state = _state_root(config, None)
        expected_ledger = (state / "experience" / "LESSONS.md").resolve()
        if ledger_path.resolve() != expected_ledger:
            raise ConfigError("REJECTED", "automatic capture ledger identity")
        control_root = resolved_config.parent / "txn"
    except Exception:
        return _automatic_capture_warning("identity")

    try:
        with operation_lock(control_root):
            try:
                base_revision = f"{_local_head(state)} unverified"
            except Exception:
                return _automatic_capture_warning("identity")
            try:
                duplicate = _duplicate_request(state, token)
            except Exception:
                return _automatic_capture_warning("scan")
            if duplicate:
                return None
            try:
                capture(
                    config_path=resolved_config, explicit_state=state,
                    control_root=control_root, workspace=state,
                    agent="claude-code", rule=fields["rule"], trigger=fields["trigger"],
                    cost=fields["cost"], sink=fields["sink"], scope="global",
                    evidence=evidence, require_state_freshness=False,
                    base_revision=base_revision, include_advisories=False,
                )
            except Exception:
                return _automatic_capture_warning("capture")
    except Exception:
        return _automatic_capture_warning("lock")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core lessons capture")
    parser.add_argument("--config", type=Path, default=default_config_path(ENGINE_ROOT))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--control-root", type=Path, default=Path.home() / ".agent-core")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--agent", required=True)
    parser.add_argument("--rule", required=True)
    parser.add_argument("--trigger", required=True)
    parser.add_argument("--cost", required=True)
    parser.add_argument("--sink", required=True)
    parser.add_argument("--scope", default="auto")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--when")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path, lines = capture(
            config_path=args.config, explicit_state=args.state,
            control_root=args.control_root, workspace=args.workspace,
            agent=args.agent, rule=args.rule, trigger=args.trigger, cost=args.cost,
            sink=args.sink, scope=args.scope, evidence=args.evidence, when=args.when,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"CAPTURED {path}")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
