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


def test_domain_stopwords_remove_generic_terms_but_preserve_specific_tokens() -> None:
    domain_stopwords = {
        "用户", "文件", "配置", "任务", "测试", "步骤",
        "规则", "修改", "数据", "状态", "目录", "一个",
    }
    stopwords = match._load_stopwords()

    assert domain_stopwords <= stopwords
    assert domain_stopwords.isdisjoint(match.tokenize(" ".join(sorted(domain_stopwords))))
    assert "全局" in match.tokenize("全局配置")
    assert "预算" in match.tokenize("测试预算")
    assert {"运行", "执行"}.isdisjoint(stopwords)


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


def test_explain_shows_text_element_tokens_without_changing_default_output() -> None:
    entry = lesson("SYN-DIAG", "global", "pending", {
        "text": ("nebula_matrix_crane", "forge --ember", "altitude=0"),
    })
    query = match.Query("prompt", text="nebula forge altitude")
    hits, ignored = match.match_lessons([entry], query)

    plain = match.render(hits, ignored, 1200, stage="prompt")
    explained = match.render(hits, ignored, 1200, explain=True, stage="prompt")

    assert "RETRIEVAL " not in plain
    assert 'element="nebula_matrix_crane" tokens=crane,matrix,nebula overlap=nebula' in explained
    assert 'element="forge --ember" tokens=ember,forge overlap=forge' in explained
    assert 'element="altitude=0" tokens=altitude overlap=altitude' in explained


def test_fixture_precision_shapes_are_present() -> None:
    payload = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    entries = {item["id"]: match.parse_when(item["when"]) for item in payload["entries"] if item["when"]}
    assert {"SYN-BAG", "SYN-ID", "SYN-CJK"} <= set(entries)
    assert 6 <= len(match.tokenize(entries["SYN-BAG"]["text"][0])) <= 9
    assert set(match.tokenize(entries["SYN-ID"]["text"][0])) == {"crane", "matrix", "nebula"}
    assert set(match.tokenize("潮汐车站")) & set(match.tokenize("纸鸢车站")) == {"车站"}


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
    assert "METRIC recall=33/33 threshold=33/33" in lines
    assert "METRIC rendered_recall=33/33 threshold=33/33" in lines
    assert "METRIC rendered_omissions=0" in lines
    assert "METRIC false_inject=3/33 threshold<=3/33" in lines
    assert "METRIC deterministic=yes threshold=yes" in lines
    assert "METRIC production_deterministic=yes threshold=yes" in lines
    assert "METRIC explain_deterministic=yes threshold=yes" in lines
    assert "METRIC legacy_recall=33/33 threshold>=27/33" in lines
    assert "METRIC production_budget=pass chars=1200" in lines
    assert "METRIC explain_budget=pass chars=1200" in lines


def test_runtime_payload_fixtures_preserve_stage_field_availability() -> None:
    prompt = json.loads((EVENTS / "codex-user-prompt.json").read_text(encoding="utf-8"))
    prompt_query = match.query_from_payload("codex", "prompt", prompt)
    assert prompt_query.paths == () and prompt_query.cmds == ()
    pretool = json.loads((EVENTS / "codex-pre-tool.json").read_text(encoding="utf-8"))
    assert match.query_from_payload("codex", "pretool", pretool).cmds == ("git commit -m ac2",)
    claude = json.loads((EVENTS / "claude-code-pre-tool.json").read_text(encoding="utf-8"))
    assert match.query_from_payload("claude-code", "pretool", claude).paths == ("agent_core/match.py",)


def test_completion_match_renders_lessons_without_fixed_capture_advice() -> None:
    entry = lesson("L-1", "global", "pending", {"text": ("shared constant",)})
    hits, ignored = match.match_lessons(
        [entry], match.Query("completion", text="review the shared constant"),
    )

    output = match.render(hits, ignored, 1200, stage="completion")

    assert output.startswith("LESSON L-1:")
    assert "CAPTURE review corrections" not in output


def test_eval_fails_when_expected_matches_do_not_survive_production_budget(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "retrieval"
    fixtures.mkdir()
    for name in ("corpus.json", "queries.json"):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if name == "corpus.json":
            for entry in payload["entries"]:
                entry["rule"] = "x" * (match.DEFAULT_BUDGET + 1)
        (fixtures / name).write_text(json.dumps(payload), encoding="utf-8")

    code, lines = match.evaluate(fixtures)

    assert code == 1
    assert "METRIC recall=33/33 threshold=33/33" in lines
    assert "METRIC rendered_recall=0/33 threshold=33/33" in lines
    assert "METRIC rendered_omissions=38" in lines
    omission_lines = [line for line in lines if line.startswith("OMISSION ")]
    assert len(omission_lines) == 38
    assert all(" query=" in line and " scope=" in line for line in omission_lines)
    assert all(" source=corpus.json lesson=" in line for line in omission_lines)
    assert lines[-1] == "FAIL lessons eval"


def test_budget_tracking_counts_same_id_lessons_by_scope() -> None:
    entries = [
        lesson("L-7", "project", "pending", {"tasks": ("build",)}),
        lesson("L-7", "global", "pending", {"tasks": ("build",)}),
    ]
    hits, ignored = match.match_lessons(entries, match.Query("dispatch", task="build"))
    budget = len("LESSON L-7: Rule L-7. sink=checks/L-7.md\nTRUNCATED 1 entries omitted\n")

    output, rendered_hits = match._render_with_hits(
        hits, ignored, budget, stage="dispatch",
    )

    assert "TRUNCATED 1 entries omitted" in output
    assert [(hit.lesson.scope, hit.lesson.lesson_id) for hit in rendered_hits] == [
        ("project", "L-7"),
    ]
    omitted = [hit for hit in hits if id(hit) not in {id(item) for item in rendered_hits}]
    assert [(hit.lesson.scope, hit.lesson.lesson_id) for hit in omitted] == [
        ("global", "L-7"),
    ]


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
