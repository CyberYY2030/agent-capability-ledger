"""Deterministic, explainable retrieval for LESSONS ledgers."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import ledger


ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ENGINE_ROOT / "seed" / "LESSONS.md"
DEFAULT_FIXTURES = ENGINE_ROOT / "tests" / "fixtures" / "retrieval"
DEFAULT_BUDGET = 1200
VALID_TASKS = {"fix", "build", "shape", "audit", "research", "unknown"}
VALID_STAGES = {"prompt", "dispatch", "pretool", "completion"}
WHEN_KEYS = {"tasks", "paths", "cmds", "text"}
SCOPE_ORDER = {"project": 0, "profile": 1, "global": 2}
STATUS_ORDER = {"pending": 0, "checklist": 1, "enforced": 2}
HOOK_HEARTBEAT_SCHEMA = "lessons-hook-heartbeat/1"
STAGE_FIELDS = {
    "prompt": {"tasks", "text"},
    "dispatch": {"tasks", "paths"},
    "pretool": {"paths", "cmds"},
    "completion": {"text"},
}
ENTRY_RE = re.compile(
    r"^\s*-\s+\*\*(?P<id>\[\[lesson:[A-Z][A-Z0-9-]*-\d+\]\]|L-[A-Za-z]?\d+)"
    r"\s+\[(?P<status>pending|checklist|enforced)[^\]]*\]\s*(?P<rule>.*?)\*\*(?P<body>.*)$"
)
WHEN_RE = re.compile(r"(?:^|\s)when:\s*(?P<value>\{.*\})\s*$")


class MatchError(ValueError):
    """A stable contract or fixture error."""


@dataclass(frozen=True)
class Query:
    stage: str
    task: str = "unknown"
    paths: tuple[str, ...] = ()
    cmds: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    scope: str
    status: str
    rule: str
    sink: str
    trigger: str
    when: Mapping[str, tuple[str, ...]] | None
    source: str = "fixture"
    line: int = 0


@dataclass(frozen=True)
class Hit:
    lesson: Lesson
    predicate: str
    value: str
    query_value: str


def _load_stopwords(path: Path | None = None) -> frozenset[str]:
    source = path or Path(__file__).with_name("stopwords.txt")
    words = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            words.append(_normalize(value))
    return frozenset(words)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # NFKC already performs this conversion; the explicit pass freezes the contract.
    chars = []
    for char in value:
        code = ord(char)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(char)
    return "".join(chars)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def tokenize(value: str, stopwords: frozenset[str] | None = None) -> tuple[str, ...]:
    """NFKC -> casefold -> halfwidth -> ASCII split/CJK bigram -> filter."""
    ignored = _load_stopwords() if stopwords is None else stopwords
    normalized = _normalize(value)
    tokens: list[str] = []
    ascii_run: list[str] = []
    cjk_run: list[str] = []

    def flush_ascii() -> None:
        if ascii_run:
            tokens.append("".join(ascii_run))
            ascii_run.clear()

    def flush_cjk() -> None:
        if cjk_run:
            run = "".join(cjk_run)
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
            cjk_run.clear()

    for char in normalized:
        if char.isascii() and char.isalnum():
            flush_cjk()
            ascii_run.append(char)
        elif _is_cjk(char):
            flush_ascii()
            cjk_run.append(char)
        else:
            flush_ascii()
            flush_cjk()
    flush_ascii()
    flush_cjk()
    return tuple(sorted({token for token in tokens if len(token) >= 2 and token not in ignored}))


def parse_when(raw: str, *, source: str = "when") -> dict[str, tuple[str, ...]]:
    """Parse the canonical single-line JSON predicate contract."""
    try:
        pairs = json.loads(raw, object_pairs_hook=list)
    except json.JSONDecodeError as exc:
        raise MatchError(f"{source}: invalid when JSON: {exc.msg}") from exc
    if not isinstance(pairs, list):
        raise MatchError(f"{source}: when must be a JSON object")
    keys = [pair[0] for pair in pairs if isinstance(pair, (list, tuple)) and len(pair) == 2]
    if len(keys) != len(set(keys)):
        raise MatchError(f"{source}: duplicate when key")
    value = dict(pairs)
    unknown = sorted(set(value) - WHEN_KEYS)
    if unknown:
        raise MatchError(f"{source}: unknown when key(s): {','.join(unknown)}")
    for key, items in value.items():
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item for item in items):
            raise MatchError(f"{source}: when.{key} must be a non-empty string array")
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if raw != canonical:
        raise MatchError(f"{source}: when must be canonical JSON: {canonical}")
    return {key: tuple(value[key]) for key in sorted(value)}


def _field(body: str, labels: Sequence[str], end_labels: Sequence[str]) -> str:
    starts = "|".join(re.escape(label) for label in labels)
    ends = "|".join(re.escape(label) for label in end_labels)
    terminator = rf"(?=\s*(?:[。．.!?！？]\s*)?(?:{ends})|$)" if end_labels else r"$"
    match = re.search(rf"(?:{starts})\s*(.*?){terminator}", body)
    return match.group(1).strip().rstrip(".") if match else ""


def parse_markdown(text: str, scope: str, source: str) -> list[Lesson]:
    lessons: list[Lesson] = []
    active = False
    saw_heading = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("## "):
            saw_heading = True
            active = ledger.is_active_heading(line.strip()[3:])
            continue
        match = ENTRY_RE.match(line)
        if not match or (saw_heading and not active):
            continue
        lesson_id = match.group("id")
        if lesson_id.startswith("[[lesson:"):
            lesson_id = lesson_id[len("[[lesson:"):-2]
        body = match.group("body").strip()
        when_match = WHEN_RE.search(body)
        if "when:" in body and not when_match:
            raise MatchError(f"{source}:{lineno}: when must be final canonical single-line JSON")
        when = parse_when(when_match.group("value"), source=f"{source}:{lineno}") if when_match else None
        body_without_when = body[:when_match.start()].rstrip() if when_match else body
        trigger = _field(body_without_when, ("触发:", "Trigger:"), ("代价:", "Cost:", "sink"))
        sink = _field(body_without_when, ("sink →", "sink ->", "sink:"), ())
        if not trigger:
            # Old ledgers remain searchable only when their prose trigger can be identified.
            trigger = body_without_when
        lessons.append(Lesson(
            lesson_id=lesson_id,
            scope=scope,
            status=match.group("status"),
            rule=match.group("rule").strip(),
            sink=sink or "unspecified",
            trigger=trigger,
            when=when,
            source=source,
            line=lineno,
        ))
    return lessons


def load_ledgers(global_ledger: Path, workspace: str | None = None, all_profiles: bool = False) -> list[Lesson]:
    sources, errors, _warnings = ledger.resolve_sources(str(global_ledger), workspace, all_profiles=all_profiles)
    if errors:
        raise MatchError("; ".join(errors))
    result: list[Lesson] = []
    for scope, _name, source in sources:
        path = Path(source)
        try:
            result.extend(parse_markdown(path.read_text(encoding="utf-8"), scope, str(path)))
        except (OSError, UnicodeDecodeError) as exc:
            raise MatchError(f"cannot read {path}: {exc}") from exc
    return result


def _hook_source_signature(
    global_ledger: Path, workspace: str | None, all_profiles: bool,
) -> tuple[str, list[tuple[str, str, str]]]:
    sources, errors, _warnings = ledger.resolve_sources(
        str(global_ledger), workspace, all_profiles=all_profiles,
    )
    if errors:
        raise MatchError("; ".join(errors))
    digest = hashlib.sha256()
    for scope, name, source in sources:
        path = Path(source)
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise MatchError(f"cannot stat {path}: {exc}") from exc
        digest.update(
            f"{scope}\0{name}\0{path.resolve()}\0{stat_result.st_mtime_ns}:{stat_result.st_size}\n".encode(
                "utf-8",
            )
        )
    return digest.hexdigest(), sources


def _previous_hook_signature(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    value = payload.get("source_mtime_sha256") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _validate_hook_sources_if_changed(
    global_ledger: Path,
    workspace: str | None,
    all_profiles: bool,
    heartbeat_path: Path | None,
) -> tuple[str, bool]:
    signature, sources = _hook_source_signature(global_ledger, workspace, all_profiles)
    changed = signature != _previous_hook_signature(heartbeat_path)
    if changed:
        _defined, errors, _warnings = ledger.validate_sources(sources)
        if errors:
            raise MatchError("; ".join(errors))
    return signature, changed


def _record_hook_heartbeat(
    path: Path | None,
    *,
    runtime: str,
    stage: str,
    source_signature: str | None,
    validation_ran: bool,
    result_nonempty: bool,
    status: str,
) -> None:
    if path is None:
        return
    script_value = os.environ.get("AGENT_CORE_HOOK_SCRIPT")
    script_hash = None
    if script_value:
        try:
            script_hash = hashlib.sha256(Path(script_value).read_bytes()).hexdigest()
        except OSError:
            script_hash = None
    payload = {
        "schema": HOOK_HEARTBEAT_SCHEMA,
        "runtime": runtime,
        "stage": stage,
        "status": status,
        "retrieval_invoked": True,
        "result_nonempty": result_nonempty,
        "validation_ran": validation_ran,
        "source_mtime_sha256": source_signature,
        "hook_sha256": script_hash,
        "observed_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        pass


def load_fixture_corpus(fixtures: Path) -> list[Lesson]:
    payload = json.loads((fixtures / "corpus.json").read_text(encoding="utf-8"))
    if payload.get("schema") != "lesson-retrieval-corpus/1" or len(payload.get("entries", [])) != 20:
        raise MatchError("fixture corpus must contain exactly 20 entries")
    lessons = []
    for index, item in enumerate(payload["entries"], 1):
        raw_when = item.get("when")
        when = parse_when(raw_when, source=f"corpus.json entry {index}") if raw_when is not None else None
        lessons.append(Lesson(
            lesson_id=item["id"], scope=item["scope"], status=item["status"],
            rule=item["rule"], sink=item["sink"], trigger=item["trigger"], when=when,
            source="corpus.json", line=index,
        ))
    return lessons


def fixture_aggregate(fixtures: Path) -> str:
    digest = hashlib.sha256()
    for name in ("corpus.json", "queries.json"):
        digest.update(name.encode("utf-8") + b"\0" + (fixtures / name).read_bytes())
    return digest.hexdigest()


def _glob_regex(pattern: str) -> re.Pattern[str]:
    normalized = _normalize(pattern).replace("\\", "/")
    output = ["^"]
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if char == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    output.append("(?:.*/)?")
                    index += 1
                else:
                    output.append(".*")
                continue
            output.append("[^/]*")
        elif char == "?":
            output.append("[^/]")
        else:
            output.append(re.escape(char))
        index += 1
    output.append("$")
    return re.compile("".join(output))


def _stage_query(query: Query) -> tuple[Query, tuple[str, ...]]:
    if query.stage not in VALID_STAGES:
        raise MatchError(f"unknown stage: {query.stage}")
    allowed = STAGE_FIELDS[query.stage]
    ignored = []
    values = {"task": query.task, "paths": query.paths, "cmds": query.cmds, "text": query.text}
    for field, present in (("tasks", query.task != "unknown"), ("paths", bool(query.paths)),
                           ("cmds", bool(query.cmds)), ("text", bool(query.text))):
        if present and field not in allowed:
            ignored.append(field)
    return Query(
        stage=query.stage,
        task=query.task if "tasks" in allowed else "unknown",
        paths=query.paths if "paths" in allowed else (),
        cmds=query.cmds if "cmds" in allowed else (),
        text=query.text if "text" in allowed else "",
    ), tuple(ignored)


def _match_lesson(lesson: Lesson, query: Query, stopwords: frozenset[str]) -> Hit | None:
    if lesson.when is None:
        overlap = sorted(set(tokenize(lesson.trigger, stopwords)) & set(tokenize(query.text, stopwords)))
        if len(overlap) >= 2:
            return Hit(lesson, "legacy_text", ",".join(overlap[:2]), query.text)
        return None
    for predicate in ("tasks", "paths", "cmds", "text"):
        values = lesson.when.get(predicate, ())
        if not values:
            continue
        if predicate == "tasks":
            normalized_task = _normalize(query.task)
            for value in values:
                if normalized_task == _normalize(value):
                    return Hit(lesson, predicate, value, query.task)
        elif predicate == "paths":
            for value in values:
                matcher = _glob_regex(value)
                for path in query.paths:
                    normalized_path = _normalize(path).replace("\\", "/")
                    if matcher.fullmatch(normalized_path):
                        return Hit(lesson, predicate, value, path)
        elif predicate == "cmds":
            for value in values:
                needle = _normalize(value)
                for command in query.cmds:
                    if needle in _normalize(command):
                        return Hit(lesson, predicate, value, command)
        else:
            query_tokens = set(tokenize(query.text, stopwords))
            for value in values:
                overlap = sorted(query_tokens & set(tokenize(value, stopwords)))
                if overlap:
                    return Hit(lesson, predicate, overlap[0], query.text)
    return None


def match_lessons(lessons: Iterable[Lesson], query: Query, stopwords: frozenset[str] | None = None) -> tuple[list[Hit], tuple[str, ...]]:
    effective, ignored = _stage_query(query)
    table = _load_stopwords() if stopwords is None else stopwords
    hits = [hit for lesson in lessons if (hit := _match_lesson(lesson, effective, table)) is not None]
    hits.sort(key=lambda hit: (
        SCOPE_ORDER.get(hit.lesson.scope, 99), STATUS_ORDER.get(hit.lesson.status, 99), hit.lesson.lesson_id
    ))
    return hits, ignored


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render(hits: Sequence[Hit], ignored: Sequence[str], budget_chars: int, explain: bool = False,
           stage: str = "prompt") -> str:
    if budget_chars < 32:
        raise MatchError("budget_chars must be at least 32")
    lines = []
    if explain:
        lines.extend(
            f"IGNORED predicate={field} reason=unavailable-at-{stage}" for field in sorted(ignored)
        )
    for hit in hits:
        line = f"LESSON {hit.lesson.lesson_id}: {hit.lesson.rule} sink={hit.lesson.sink}"
        if explain:
            line += (f" MATCH predicate={hit.predicate} value={_quote(hit.value)}"
                     f" query={_quote(hit.query_value)}")
        lines.append(line)
    if stage == "completion":
        lines.append("CAPTURE review corrections and verified methods; do not write canonical automatically.")
    total = len(lines)
    for keep in range(total, -1, -1):
        candidate = lines[:keep]
        if keep < total:
            candidate.append(f"TRUNCATED {total - keep} entries omitted")
        rendered = "\n".join(candidate) + ("\n" if candidate else "")
        if len(rendered) <= budget_chars:
            return rendered
    raise MatchError("budget_chars cannot hold truncation marker")


def query_from_payload(runtime: str, stage: str, payload: Mapping[str, object]) -> Query:
    if runtime not in {"claude-code", "codex"}:
        raise MatchError(f"unsupported runtime: {runtime}")
    event_expected = {"prompt": "UserPromptSubmit", "pretool": "PreToolUse", "completion": "Stop"}.get(stage)
    if event_expected is None:
        raise MatchError(f"runtime payload unsupported for stage: {stage}")
    if payload.get("hook_event_name") != event_expected:
        raise MatchError(f"expected {event_expected} payload")
    if stage == "prompt":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise MatchError("UserPromptSubmit.prompt must be a string")
        # UserPromptSubmit has no path or command fields. Never infer either from text.
        task = payload.get("task", "unknown")
        task = task if isinstance(task, str) and task in VALID_TASKS else "unknown"
        return Query(stage="prompt", task=task, text=prompt)
    if stage == "completion":
        message = payload.get("last_assistant_message", "")
        return Query(stage="completion", text=message if isinstance(message, str) else "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        raise MatchError("PreToolUse.tool_input must be an object")
    paths: list[str] = []
    cmds: list[str] = []
    for key in ("path", "file_path", "workdir"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    command = tool_input.get("command")
    if isinstance(command, str):
        cmds.append(command)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        cmds.extend(command)
    return Query(stage="pretool", paths=tuple(paths), cmds=tuple(cmds))


def _query_from_args(args: argparse.Namespace) -> Query:
    return Query(stage=args.stage, task=args.task, paths=tuple(args.paths or ()),
                 cmds=tuple(args.cmds or ()), text=args.text or "")


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--workspace")
    parser.add_argument("--all-profiles", action="store_true")


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stage", choices=sorted(VALID_STAGES), required=True)
    parser.add_argument("--task", choices=sorted(VALID_TASKS), default="unknown")
    parser.add_argument("--paths", nargs="*")
    parser.add_argument("--cmds", nargs="*")
    parser.add_argument("--text")
    parser.add_argument("--budget-chars", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--explain", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core lessons")
    commands = parser.add_subparsers(dest="lessons_command", required=True)
    match_parser = commands.add_parser("match")
    _add_source_args(match_parser)
    _add_query_args(match_parser)
    eval_parser = commands.add_parser("eval")
    eval_parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    eval_parser.add_argument("--report", action="store_true")
    eval_parser.add_argument("--expect-hash")
    hook_parser = commands.add_parser("hook")
    hook_parser.add_argument("--runtime", choices=("claude-code", "codex"), required=True)
    hook_parser.add_argument("--stage", choices=("prompt", "pretool", "completion"), required=True)
    hook_parser.add_argument("--event-json", type=Path)
    _add_source_args(hook_parser)
    hook_parser.add_argument("--budget-chars", type=int, default=DEFAULT_BUDGET)
    hook_parser.add_argument("--explain", action="store_true")
    return parser


def evaluate(fixtures: Path, expect_hash: str | None = None) -> tuple[int, list[str]]:
    aggregate = fixture_aggregate(fixtures)
    if expect_hash and expect_hash != aggregate:
        raise MatchError(f"fixture hash mismatch: expected={expect_hash} actual={aggregate}")
    lessons = load_fixture_corpus(fixtures)
    legacy_lessons = [replace(item, when=None) for item in lessons]
    payload = json.loads((fixtures / "queries.json").read_text(encoding="utf-8"))
    queries = payload.get("queries", [])
    positives = [item for item in queries if item.get("kind") == "positive"]
    negatives = [item for item in queries if item.get("kind") == "negative"]
    if len(positives) != 30 or len(negatives) != 30:
        raise MatchError("fixture queries must contain 30 positive and 30 negative cases")
    recalled = 0
    false_queries = 0
    unexpected_total = 0
    legacy_recalled = 0
    deterministic = True
    budget_ok = True
    for item in queries:
        query = Query(stage=item["stage"], task=item.get("task", "unknown"),
                      paths=tuple(item.get("paths", ())), cmds=tuple(item.get("cmds", ())),
                      text=item.get("text", ""))
        hits, ignored = match_lessons(lessons, query)
        actual = [hit.lesson.lesson_id for hit in hits]
        expected = item.get("expected_hit", [])
        if item["kind"] == "positive" and all(lesson_id in actual for lesson_id in expected):
            recalled += 1
        unexpected = [lesson_id for lesson_id in actual if lesson_id not in expected]
        if item["kind"] == "negative" and unexpected:
            false_queries += 1
        unexpected_total += len(unexpected)
        first = render(hits, ignored, DEFAULT_BUDGET, explain=True, stage=query.stage)
        second = render(*match_lessons(lessons, query), DEFAULT_BUDGET, explain=True, stage=query.stage)
        deterministic = deterministic and first.encode("utf-8") == second.encode("utf-8")
        budget_ok = budget_ok and len(first) <= DEFAULT_BUDGET
        probe = item.get("legacy_probe")
        if probe:
            probe_hits, _ = match_lessons(legacy_lessons, Query(stage="prompt", text=probe["text"]))
            if probe["expected_hit"] in [hit.lesson.lesson_id for hit in probe_hits]:
                legacy_recalled += 1
    passed = (recalled == 30 and false_queries <= 2 and unexpected_total <= 3
              and deterministic and legacy_recalled >= 24 and budget_ok)
    lines = [
        f"FIXTURE aggregate_sha256={aggregate}",
        f"METRIC recall={recalled}/30 threshold=30/30",
        f"METRIC false_inject={false_queries}/30 threshold<=2/30",
        f"METRIC unexpected_ids={unexpected_total} threshold<=3",
        f"METRIC deterministic={'yes' if deterministic else 'no'} threshold=yes",
        f"METRIC legacy_recall={legacy_recalled}/30 threshold>=24/30",
        f"METRIC budget={'pass' if budget_ok else 'fail'} chars={DEFAULT_BUDGET}",
        "PASS lessons eval" if passed else "FAIL lessons eval",
    ]
    return (0 if passed else 1), lines


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.lessons_command == "eval":
            code, lines = evaluate(args.fixtures, args.expect_hash)
            if args.report or code:
                print("\n".join(lines))
            return code
        if args.lessons_command == "match":
            lessons = load_ledgers(args.ledger, args.workspace, args.all_profiles)
            query = _query_from_args(args)
            hits, ignored = match_lessons(lessons, query)
            print(render(hits, ignored, args.budget_chars, args.explain, query.stage), end="")
            return 0
        heartbeat_value = os.environ.get("AGENT_CORE_HOOK_HEARTBEAT")
        heartbeat_path = Path(heartbeat_value).resolve() if heartbeat_value else None
        source_signature: str | None = None
        validation_ran = False
        try:
            if args.event_json:
                payload = json.loads(args.event_json.read_text(encoding="utf-8"))
            else:
                payload = json.load(sys.stdin)
            workspace = args.workspace
            payload_workspace = payload.get("cwd") if isinstance(payload, Mapping) else None
            if workspace is None and isinstance(payload_workspace, str) and payload_workspace:
                workspace = payload_workspace
            source_signature, validation_ran = _validate_hook_sources_if_changed(
                args.ledger, workspace, args.all_profiles, heartbeat_path,
            )
            lessons = load_ledgers(args.ledger, workspace, args.all_profiles)
            query = query_from_payload(args.runtime, args.stage, payload)
            hits, ignored = match_lessons(lessons, query)
            rendered = render(hits, ignored, args.budget_chars, args.explain, query.stage)
            print(rendered, end="")
            _record_hook_heartbeat(
                heartbeat_path,
                runtime=args.runtime,
                stage=args.stage,
                source_signature=source_signature,
                validation_ran=validation_ran,
                result_nonempty=bool(rendered.strip()),
                status="pass",
            )
        except Exception as exc:  # Hook contract is deliberately fail-open.
            print(f"WARNING lessons hook unavailable: {exc}", file=sys.stderr)
            _record_hook_heartbeat(
                heartbeat_path,
                runtime=args.runtime,
                stage=args.stage,
                source_signature=source_signature,
                validation_ran=validation_ran,
                result_nonempty=False,
                status="warning",
            )
        return 0
    except (MatchError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
