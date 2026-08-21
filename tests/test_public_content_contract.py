from __future__ import annotations

import json
from pathlib import Path

from agent_core import match, privacy


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
RUNTIMES = ROOT / "runtimes"
PROFILE = ROOT / "seed" / "profiles" / "example-domain" / "LESSONS.md"


def test_public_example_inventory_and_rule_placeholder_are_exact() -> None:
    assert {path.name for path in EXAMPLES.iterdir() if path.is_file()} == {
        "host.example.json", "manifest.custom.example.yaml", "rules.global.example.md",
    }
    text = (EXAMPLES / "rules.global.example.md").read_text(encoding="utf-8")
    assert text == (
        "<!-- RUNTIME_HEAD -->\n# Global Agent Rules\n\n## About the User\n\n"
        "<!-- Add only privacy-safe collaboration preferences. Keep identity and machine details "
        "in the host overlay. -->\n\n## Working Agreement\n\n"
        "- Read local rules before changing a project.\n"
        "- Use evidence-backed acceptance commands before claiming completion.\n"
        "- Keep durable private state outside the public engine.\n"
        "- Mark unobserved behavior as unproven instead of inferring success.\n"
    )


def test_runtime_heads_are_minimal_and_exact() -> None:
    expected = {
        "claude-code": "<!-- RUNTIME_HEAD -->\n# Claude Code Rules\n\nThe content below is rendered from the bound private state.\n",
        "codex": "<!-- RUNTIME_HEAD -->\n# Codex Rules\n\nThe content below is rendered from the bound private state.\n",
        "generic": "<!-- RUNTIME_HEAD -->\n# Agent Rules\n\nThe content below is rendered from the bound private state.\n",
    }
    for runtime, content in expected.items():
        assert (RUNTIMES / runtime / "head.md").read_text(encoding="utf-8") == content


def test_public_identifiers_are_closed_over_an_exact_synthetic_allowlist() -> None:
    host = json.loads((EXAMPLES / "host.example.json").read_text(encoding="utf-8"))
    assert host == {
        "schema": "agent-core.config/1",
        "host_label": "desk",
        "state_root": "<STATE>",
        "backup_root": "<HOST_DATA>/backups",
        "prompt_injection": {"lines": [
            "Read matched lessons before acting.",
            "At completion, report any evidence-backed lesson candidate.",
        ]},
        "targets": [
            {
                "id": "claude-code", "runtime": "claude-code", "root": "<RUNTIME_A>",
                "rules_target": "CLAUDE.md", "lessons_target": "LESSONS.md",
                "case_law_target": "CASE_LAW.md", "skills_root": "skills",
                "hook_target": "hooks/user_prompt.sh",
            },
            {
                "id": "codex", "runtime": "codex", "root": "<RUNTIME_B>",
                "rules_target": "AGENTS.md", "lessons_target": "LESSONS.md",
                "case_law_target": "CASE_LAW.md", "skills_root": "skills",
                "hook_target": "hooks/user_prompt.sh",
            },
        ],
        "capability_overrides": [],
    }

    custom = json.loads((EXAMPLES / "manifest.custom.example.yaml").read_text(encoding="utf-8"))
    assert custom == {
        "schema": "capability-manifest/1",
        "capabilities": [{
            "id": "skill:custom-check", "kind": "skill", "source": "skills/custom-check",
            "requirement": "optional", "runtimes": ["claude-code", "codex"], "trusted": True,
        }],
    }
    built_in = json.loads((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert {
        item["source"] for item in built_in["capabilities"] if item["kind"] == "skill"
    } == {
        "skills/adversarial-audit",
        "skills/dispatching-task-cards",
        "skills/first-divergence-debugging",
    }

    profile_text = PROFILE.read_text(encoding="utf-8")
    assert "<!-- lessons-profile: example-domain -->" in profile_text
    lessons = match.parse_markdown(profile_text, "profile", str(PROFILE))
    assert [item.lesson_id for item in lessons] == ["EXAMPLE-1"]
    assert lessons[0].rule == "Keep public fixtures synthetic."
    assert lessons[0].trigger == "preparing reusable examples"
    assert lessons[0].sink == "checks/synthetic-fixtures.md"
    assert lessons[0].when == {"text": ("public fixture", "synthetic example")}


def test_public_content_surfaces_pass_the_builtin_privacy_contract() -> None:
    roots = [EXAMPLES, RUNTIMES, PROFILE.parent]
    findings, exemptions = privacy.scan_trees(
        roots, privacy._default_rules(), {}, privacy.DEFAULT_MAX_BLOB_BYTES,
    )
    assert exemptions == 0
    assert findings == []
