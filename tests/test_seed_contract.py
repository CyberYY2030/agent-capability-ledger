from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from agent_core import ledger, match, retire
from agent_core.cli import main as cli_main


ENGINE = Path(__file__).resolve().parents[1]
SEED = ENGINE / "seed" / "LESSONS.md"
CASE_LAW = ENGINE / "seed" / "CASE_LAW.md"
EXPECTED_SKILL_REFS = {
    "adversarial-audit": {"CASE-4", "CASE-6", "CASE-7"},
    "dispatching-task-cards": {"CASE-3", "CASE-4", "CASE-5", "CASE-6"},
    "first-divergence-debugging": {"CASE-1", "CASE-2", "CASE-3"},
}


def _seed_lessons() -> list[match.Lesson]:
    return match.parse_markdown(SEED.read_text(encoding="utf-8"), "global", str(SEED))


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    expanded = [sys.executable if item == "<PYTHON>" else item for item in argv]
    return subprocess.run(
        expanded, cwd=ENGINE, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def _capture_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    (workspace / ".agents").mkdir(parents=True)
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "config", "user.email", f"test{chr(64)}invalid")
    (workspace / ".agents" / "lessons.json").write_text(json.dumps({
        "schema": "lessons-routing/1", "project_id": "sample-project", "profiles": [],
    }) + "\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "seed")
    config = tmp_path / "host.json"
    config.write_text(json.dumps({
        "schema": "agent-core.config/1",
        "host_label": "test-host",
        "state_root": str(tmp_path / "unused-state"),
        "backup_root": str(tmp_path / "backup"),
        "prompt_injection": {"lines": ["Read matched lessons."]},
        "targets": [{
            "id": "generic", "runtime": "generic", "root": str(tmp_path / "runtime"),
            "rules_target": "AGENTS.md", "lessons_target": "LESSONS.md",
            "case_law_target": "CASE_LAW.md", "skills_root": "skills",
            "hook_target": "hooks/prompt.sh",
        }],
        "capability_overrides": [],
    }) + "\n", encoding="utf-8")
    return workspace, config


def _seed_costs() -> dict[str, str]:
    costs: dict[str, str] = {}
    for line in SEED.read_text(encoding="utf-8").splitlines():
        entry = match.ENTRY_RE.match(line)
        if not entry:
            continue
        lesson_id = entry.group("id")
        found = re.search(r"代价:\s*(.*?)(?=\s+(?:verifier:|sink\s))", entry.group("body"))
        assert found, lesson_id
        costs[lesson_id] = found.group(1).strip().rstrip(".")
    return costs


def test_seed_has_eight_complete_entries_and_required_statuses() -> None:
    lessons = _seed_lessons()
    assert [item.lesson_id for item in lessons] == [f"L-{number}" for number in range(1, 9)]
    assert all(item.when for item in lessons)
    counts = Counter(item.status for item in lessons)
    assert counts["pending"] >= 2
    assert counts["checklist"] >= 2
    assert counts["enforced"] >= 2
    combined = "\n".join(f"{item.rule} {item.trigger} {item.sink}" for item in lessons)
    assert "创建/更新/晋升" in combined
    assert "docs/privacy-review.md" in combined


def test_seed_ledger_and_match_use_the_public_seed(capsys: pytest.CaptureFixture[str]) -> None:
    assert ledger.main(["agent-core", str(SEED)]) == 0
    assert "PASS" in capsys.readouterr().out
    assert cli_main([
        "lessons", "match", "--ledger", str(SEED), "--stage", "pretool",
        "--paths", "src/constants.py", "--explain",
    ]) == 0
    output = capsys.readouterr().out
    assert "LESSON L-1:" in output
    assert 'MATCH predicate=paths value="src/constants.py"' in output


def test_seed_sinks_and_trusted_verifiers_are_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["lessons", "retire", "--workspace", str(ENGINE), "--strict", "--report"]) == 0
    assert "BROKEN_SINK" not in capsys.readouterr().out
    records = retire.load_records(ENGINE)
    verifiers = retire.load_verifiers(ENGINE)
    guarded = [record for record in records if record.status in {"checklist", "enforced"}]
    assert guarded
    assert all(record.verifier_id in verifiers for record in guarded)
    for verifier_id in sorted({record.verifier_id for record in guarded}):
        verifier = verifiers[verifier_id]
        negative = _run(verifier["negative"]["argv"])
        positive = _run(verifier["positive"]["argv"])
        assert negative.returncode == 1, verifier_id
        assert positive.returncode == 0, (verifier_id, positive.stderr)
        for consumer in verifier["consumers"]:
            result = _run(consumer["consumer_probe"]["argv"])
            assert result.returncode == 0, (verifier_id, consumer["path"], result.stderr)


def test_case_ids_and_every_skill_reference_are_exact() -> None:
    cases = re.findall(r"^\| (CASE-[1-7]) \|", CASE_LAW.read_text(encoding="utf-8"), re.MULTILINE)
    assert cases == [f"CASE-{number}" for number in range(1, 8)]
    referenced: set[str] = set()
    for skill, expected in EXPECTED_SKILL_REFS.items():
        text = (ENGINE / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        actual = set(re.findall(r"\[(CASE-\d+)\]", text))
        assert actual == expected
        referenced.update(actual)
    assert referenced == {f"CASE-{number}" for number in range(1, 8)}
    manifest = json.loads((ENGINE / "manifest.yaml").read_text(encoding="utf-8"))
    installed_skills = {
        item["source"] for item in manifest["capabilities"] if item["kind"] == "skill"
    }
    assert installed_skills == {f"skills/{name}" for name in EXPECTED_SKILL_REFS}


def test_all_eight_seed_texts_pass_real_project_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, config = _capture_workspace(tmp_path)
    costs = _seed_costs()
    lessons = _seed_lessons()
    for item in lessons:
        assert cli_main([
            "lessons", "capture", "--config", str(config), "--workspace", str(workspace),
            "--scope", "auto", "--agent", "codex", "--rule", item.rule,
            "--trigger", item.trigger, "--cost", costs[item.lesson_id], "--sink", item.sink,
            "--evidence", f"synthetic:seed-{item.lesson_id}",
        ]) == 0
        output = capsys.readouterr().out
        assert "CAPTURED " in output
    assert len(list((workspace / ".agents" / "inbox").glob("*.md"))) == 8
