from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path

from agent_core import ledger


def ledger_text(scope: str, name: str | None = None, entry: str = "") -> str:
    extra = ""
    if scope == "profile":
        extra = f"<!-- lessons-profile: {name} -->\n"
    elif scope == "project":
        extra = f"<!-- lessons-project: {name} -->\n"
    return (
        "# LESSONS\n<!-- lessons-schema: lessons-ledger/1 -->\n"
        f"<!-- lessons-scope: {scope} -->\n{extra}## 活跃区\n{entry}\n## 归档区\n"
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


class TestLedgerGuard:
    good = """# LESSONS
## 活跃区
- **L-1 [checklist·通用] Rule one.** 触发:shared change. 代价:x. sink → a.
- **L-2 [pending·项目] Rule two.** 触发:project change. 代价:y. sink → b.
## 归档区
- **L-A2 [enforced·通用] Rule three.** 触发:archive. 代价:z. sink → c.
"""

    def errors(self, text: str) -> list[str]:
        _ids, errors, _warns = ledger.parse_ledger(text)
        return errors

    def test_good_passes(self) -> None:
        assert self.errors(self.good) == []

    def test_duplicate_id_flagged(self) -> None:
        duplicate = self.good.replace("## 归档区", "- **L-2 [pending·通用] Duplicate.** 触发:x. 代价:y. sink → d.\n## 归档区")
        assert any("重号" in error for error in self.errors(duplicate))

    def test_void_contradiction_flagged(self) -> None:
        assert any("矛盾" in error for error in self.errors(self.good + "\n<!-- L-1 void 作废 -->\n"))

    def test_bad_status_flagged(self) -> None:
        assert any("状态标签" in error for error in self.errors(self.good.replace("[checklist·通用]", "[todo·通用]")))

    def test_dangling_reference_flagged(self, tmp_path: Path) -> None:
        ids, _errors, _warns = ledger.parse_ledger(self.good)
        card = tmp_path / "card.md"
        card.write_text("References L-1 and missing L-99; AL-3 is unrelated.", encoding="utf-8")
        errors = ledger.check_refs(ids, [str(card)], [str(tmp_path / "none.md")])
        assert any("L-99" in error for error in errors)
        assert not any("L-1:" in error for error in errors)

    def test_summary_distribution_and_missing_sink(self) -> None:
        counts, active, archived, missing = ledger.ledger_summary(self.good.replace("sink → b.", "no sink."))
        assert (counts["pending"], counts["checklist"], active, archived) == (1, 1, 2, 1)
        assert missing == ["L-2"]

    def test_schema_v2_keeps_v1_entry_valid(self) -> None:
        text = ledger_text("global", entry="- **L-1 [pending·通用] Rule.** 触发:x. 代价:y. sink → z.")
        _ids, errors, _warns = ledger.parse_ledger(text.replace("lessons-ledger/1", "lessons-ledger/2"), "global")
        assert errors == []


class TestLessonRouting:
    def setup_method(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.global_path = self.root / "experience" / "LESSONS.md"
        self.profile_path = self.root / "experience" / "profiles" / "example-domain" / "LESSONS.md"
        write(self.global_path, ledger_text("global", entry="- **L-1 [checklist·通用] Global.** 触发:x. 代价:y. sink → a."))
        write(self.profile_path, ledger_text("profile", "example-domain", "- **[[lesson:EXAMPLE-1]] [pending·领域] Domain.** 触发:x. 代价:y. sink → b."))
        self.workspace = self.root / "workspace"
        (self.workspace / ".agents").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True, capture_output=True)

    def teardown_method(self) -> None:
        self.temp.cleanup()

    def config(self, profiles: list[str]) -> None:
        write(self.workspace / ".agents" / "lessons.json", json.dumps({
            "schema": "lessons-routing/1", "project_id": "sample-project", "profiles": profiles
        }))

    def project(self, entry: str | None = None) -> None:
        entry = entry or "- **[[lesson:SAMPLE-1]] [pending·项目] Project.** 触发:x. 代价:y. sink → c. legacy_id: L-6"
        write(self.workspace / ".agents" / "LESSONS.md", ledger_text("project", "sample-project", entry))

    def test_no_config_loads_only_global(self) -> None:
        sources, errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        assert errors == [] and [item[0] for item in sources] == ["global"]

    def test_declared_profile_and_project_load_in_order(self) -> None:
        self.config(["example-domain"])
        self.project()
        sources, errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        assert errors == [] and [item[0] for item in sources] == ["global", "profile", "project"]
        defined, errors, _warns = ledger.validate_sources(sources)
        assert errors == [] and {"L-1", "EXAMPLE-1", "SAMPLE-1", "L-6"}.issubset(defined)

    def test_undeclared_profile_is_not_loaded(self) -> None:
        self.config([])
        sources, errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        assert errors == [] and [item[0] for item in sources] == ["global"]

    def test_unknown_profile_fails(self) -> None:
        self.config(["missing-profile"])
        _sources, errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        assert any("profile 不存在" in error for error in errors)

    def test_all_profiles_loads_canonical_profiles(self) -> None:
        sources, errors, _warns = ledger.resolve_sources(str(self.global_path), all_profiles=True)
        assert errors == [] and [item[0] for item in sources] == ["global", "profile"]

    def test_global_project_scope_fails_strict(self) -> None:
        text = ledger_text("global", entry="- **L-1 [pending·项目] Wrong scope.** 触发:x. 代价:y. sink → z.")
        _ids, errors, _warns = ledger.parse_ledger(text, "global", "global")
        assert any("只允许 通用" in error for error in errors)

    def test_cross_store_legacy_conflict_fails(self) -> None:
        write(self.global_path, ledger_text("global", entry="- **L-6 [pending·通用] Old.** 触发:x. 代价:y. sink → z."))
        self.config([])
        self.project()
        sources, errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        assert errors == []
        _defined, errors, _warns = ledger.validate_sources(sources)
        assert any("legacy_id 冲突:L-6" in error for error in errors)

    def test_historical_id_resolves_only_in_owner_workspace(self) -> None:
        self.config([])
        self.project()
        reference = self.root / "card.md"
        reference.write_text("Historical reference L-6.", encoding="utf-8")
        sources, _errors, _warns = ledger.resolve_sources(str(self.global_path), str(self.workspace))
        defined, errors, _warns = ledger.validate_sources(sources)
        assert errors == [] and ledger.check_refs(defined, [str(reference)], [item[2] for item in sources]) == []
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        subprocess.run(["git", "init", "-q", str(unrelated)], check=True, capture_output=True)
        sources, _errors, _warns = ledger.resolve_sources(str(self.global_path), str(unrelated))
        defined, errors, _warns = ledger.validate_sources(sources)
        assert errors == [] and ledger.check_refs(defined, [str(reference)], [item[2] for item in sources])

    def test_main_preserves_source_summary_pass_lines(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ledger.main(["agent-core", str(self.global_path)])
        lines = output.getvalue().splitlines()
        assert code == 0
        assert lines[0].startswith("SOURCE global:global ")
        assert lines[1].startswith("SUMMARY global:global ")
        assert lines[-1].startswith("PASS  ")

