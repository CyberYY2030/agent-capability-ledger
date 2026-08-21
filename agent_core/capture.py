"""Low-friction, append-only lesson candidate capture."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from . import ledger, privacy
from .config import ConfigError, default_config_path, load_config
from .match import MatchError, parse_when
from .project import resolve_project_context
from .promote import _similarities, create_candidate


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


def capture(
    *, config_path: Path, explicit_state: Path | None, control_root: Path,
    workspace: Path, agent: str, rule: str, trigger: str, cost: str,
    sink: str, scope: str, evidence: str, when: str | None = None,
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
        path = create_candidate(
            project_root, control_root, host=host, agent=agent, rule=rule,
            trigger=trigger, cost=cost, sink=sink, scope_hint=expected_scope,
            evidence=evidence, base_revision=base_revision,
            inbox_path=project_root / ".agents" / "inbox",
            require_state_freshness=False, allow_project=True,
            when=when,
        )
        lines = list(_similarity_lines(project_root, expected_scope, rule))
        if rule_warning:
            lines.insert(0, rule_warning)
        return path, tuple(lines)
    state = _state_root(config, explicit_state)
    path = create_candidate(
        state, control_root, host=host, agent=agent, rule=rule, trigger=trigger,
        cost=cost, sink=sink, scope_hint=scope, evidence=evidence,
        when=when,
    )
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
