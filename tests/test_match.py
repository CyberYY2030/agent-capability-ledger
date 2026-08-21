from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core import match


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "retrieval"
EVENTS = ROOT / "tests" / "acceptance" / "runtime-events"


def lesson(lesson_id: str, scope: str, status: str, when: dict[str, tuple[str, ...]] | None,
           trigger: str = "legacy alpha beta") -> match.Lesson:
    return match.Lesson(lesson_id, scope, status, f"Rule {lesson_id}.", f"checks/{lesson_id}.md",
                        trigger, when)


def test_tokenization_contract_nfkc_ascii_cjk_stopwords_and_short_terms() -> None:
    tokens = match.tokenize("ＡＢＣ－x 共享常量，这个")
    assert "abc" in tokens
    assert "x" not in tokens
    assert {"共享", "享常", "常量"} <= set(tokens)
    assert "这个" not in tokens


def test_when_requires_canonical_json_and_known_string_arrays() -> None:
    assert match.parse_when('{"cmds":["git commit"],"paths":["src/**"]}') == {
        "cmds": ("git commit",), "paths": ("src/**",)
    }
    with pytest.raises(match.MatchError, match="canonical"):
        match.parse_when('{"paths": ["src/**"]}')
    with pytest.raises(match.MatchError, match="unknown"):
        match.parse_when('{"event":["save"]}')
    with pytest.raises(match.MatchError, match="string array"):
        match.parse_when('{"text":[1]}')


def test_markdown_active_heading_variants_are_shared_and_exact() -> None:
    text = "\n".join([
        "## 活跃区",
        "- **L-1 [pending] Rule one.** Trigger: durable trigger。Cost: repeated work. sink -> checks/one.md.",
        "## 活跃度指标",
        "- **L-2 [pending] Rule two.** Trigger: excluded metric. Cost: repeated work. sink -> checks/two.md.",
        "## active experiments",
        "- **L-3 [pending] Rule three.** Trigger: excluded experiment. Cost: repeated work. sink -> checks/three.md.",
    ])

    lessons = match.parse_markdown(text, "global", "inline")
    assert [lesson.lesson_id for lesson in lessons] == ["L-1"]
    assert lessons[0].trigger == "durable trigger"


def test_each_predicate_is_or_matched_and_explained() -> None:
    entries = [
        lesson("L-1", "global", "pending", {"tasks": ("build",)}),
        lesson("L-2", "global", "pending", {"paths": ("src/**",)}),
        lesson("L-3", "global", "pending", {"cmds": ("git commit",)}),
        lesson("L-4", "global", "pending", {"text": ("shared constant",)}),
    ]
    cases = [
        (match.Query("dispatch", task="build"), "tasks", "L-1"),
        (match.Query("pretool", paths=("src/nested/a.py",)), "paths", "L-2"),
        (match.Query("pretool", cmds=("prefix GIT COMMIT suffix",)), "cmds", "L-3"),
        (match.Query("prompt", text="a SHARED constant changed"), "text", "L-4"),
    ]
    for query, predicate, expected_id in cases:
        hits, _ = match.match_lessons(entries, query)
        assert [hit.lesson.lesson_id for hit in hits] == [expected_id]
        assert hits[0].predicate == predicate
        assert f"predicate={predicate}" in match.render(hits, (), 1200, explain=True, stage=query.stage)


def test_prompt_explicitly_ignores_paths_and_commands() -> None:
    entries = [
        lesson("L-1", "global", "pending", {"paths": ("agent_core/**",)}),
        lesson("L-2", "global", "pending", {"cmds": ("git commit",)}),
    ]
    hits, ignored = match.match_lessons(
        entries, match.Query("prompt", paths=("agent_core/match.py",), cmds=("git commit",), text="neutral")
    )
    assert hits == []
    assert ignored == ("paths", "cmds")


def test_pretool_ignores_text_and_tasks() -> None:
    entries = [lesson("L-1", "global", "pending", {"text": ("shared constant",)})]
    hits, ignored = match.match_lessons(entries, match.Query("pretool", task="build", text="shared constant"))
    assert hits == []
    assert ignored == ("tasks", "text")


def test_legacy_requires_two_non_stopword_tokens() -> None:
    entry = lesson("L-1", "global", "pending", None, "frozen corpus checksum proof")
    assert not match.match_lessons([entry], match.Query("prompt", text="checksum"))[0]
    assert match.match_lessons([entry], match.Query("prompt", text="corpus checksum"))[0]


def test_order_is_project_profile_global_then_status_then_id() -> None:
    entries = [
        lesson("L-9", "global", "pending", {"tasks": ("audit",)}),
        lesson("L-3", "project", "enforced", {"tasks": ("audit",)}),
        lesson("L-2", "project", "pending", {"tasks": ("audit",)}),
        lesson("L-1", "profile", "checklist", {"tasks": ("audit",)}),
    ]
    hits, _ = match.match_lessons(entries, match.Query("dispatch", task="audit"))
    assert [hit.lesson.lesson_id for hit in hits] == ["L-2", "L-3", "L-1", "L-9"]


def test_character_budget_is_hard_and_truncation_is_visible() -> None:
    entries = [lesson(f"L-{index}", "global", "pending", {"tasks": ("build",)}) for index in range(10)]
    hits, ignored = match.match_lessons(entries, match.Query("dispatch", task="build"))
    output = match.render(hits, ignored, 100, explain=True, stage="dispatch")
    assert len(output) <= 100
    assert "TRUNCATED" in output


def test_fixture_hash_and_eval_gates() -> None:
    actual_hash = match.fixture_aggregate(FIXTURES)
    with pytest.raises(match.MatchError, match="fixture hash mismatch"):
        match.evaluate(FIXTURES, "0" * 64)
    code, lines = match.evaluate(FIXTURES, actual_hash)
    assert code == 0
    assert "METRIC recall=30/30 threshold=30/30" in lines
    assert "METRIC false_inject=0/30 threshold<=2/30" in lines
    assert "METRIC deterministic=yes threshold=yes" in lines
    assert "METRIC legacy_recall=30/30 threshold>=24/30" in lines


def test_runtime_payload_fixtures_preserve_stage_field_availability() -> None:
    prompt = json.loads((EVENTS / "codex-user-prompt.json").read_text(encoding="utf-8"))
    prompt_query = match.query_from_payload("codex", "prompt", prompt)
    assert prompt_query.paths == () and prompt_query.cmds == ()
    pretool = json.loads((EVENTS / "codex-pre-tool.json").read_text(encoding="utf-8"))
    assert match.query_from_payload("codex", "pretool", pretool).cmds == ("git commit -m ac2",)
    claude = json.loads((EVENTS / "claude-code-pre-tool.json").read_text(encoding="utf-8"))
    assert match.query_from_payload("claude-code", "pretool", claude).paths == ("agent_core/match.py",)


def test_completion_emits_capture_prompt_without_writing() -> None:
    output = match.render([], (), 1200, stage="completion")
    assert output.startswith("CAPTURE ")
    assert "do not write canonical automatically" in output


def test_hook_invalid_payload_is_fail_open(capsys: pytest.CaptureFixture[str]) -> None:
    code = match.main([
        "hook", "--runtime", "codex", "--stage", "pretool",
        "--event-json", str(EVENTS / "codex-user-prompt.json"),
    ])
    assert code == 0
    assert "WARNING lessons hook unavailable" in capsys.readouterr().err


def test_cli_output_is_byte_deterministic() -> None:
    actual_hash = match.fixture_aggregate(FIXTURES)
    command = [
        sys.executable, "-m", "agent_core.cli", "lessons", "eval",
        "--fixtures", str(FIXTURES), "--report", "--expect-hash", actual_hash,
    ]
    first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True).stdout
    second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True).stdout
    assert first == second
