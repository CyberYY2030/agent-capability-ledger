from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# PLAN.md is intentionally excluded: it records historical acceptance limits.
PUBLICATION_NARRATIVE_FILES = (
    "README.md",
    "docs/DESIGN.md",
    "docs/LIFECYCLE.md",
    "docs/REPOSITORY_SEPARATION.md",
)
PLATFORM_WORDS = r"macos|mac os|darwin|cross-platform|two machines|双机"
UNVERIFIED_SUPPORT_PATTERNS = (
    re.compile(rf"\b(?:{PLATFORM_WORDS})\b\s+(?:is|are)\s+(?:fully\s+)?(?:supported|ready|accepted|verified)\b", re.IGNORECASE),
    re.compile(rf"\b(?:supports?|accepts?|verifies)\s+(?:{PLATFORM_WORDS})\b", re.IGNORECASE),
    re.compile(rf"\b(?:{PLATFORM_WORDS})\b\s+works?\b", re.IGNORECASE),
    re.compile(r"download and run|works out of the box", re.IGNORECASE),
)
UNVERIFIABLE_METRIC_TERMS = ("faster", "improve")


def _narrative_texts() -> dict[str, str]:
    return {
        relative_path: (ROOT / relative_path).read_text(encoding="utf-8").casefold()
        for relative_path in PUBLICATION_NARRATIVE_FILES
    }


def test_publication_narrative_excludes_positive_unverified_support_claims() -> None:
    violations = [
        f"{relative_path}: {pattern.pattern}"
        for relative_path, text in _narrative_texts().items()
        for pattern in UNVERIFIED_SUPPORT_PATTERNS
        if pattern.search(text)
    ]
    assert not violations, "unverified capability claims: " + ", ".join(violations)


def test_publication_narrative_excludes_unverifiable_percentage_metrics() -> None:
    violations = [
        relative_path
        for relative_path, text in _narrative_texts().items()
        if "%" in text and any(term in text for term in UNVERIFIABLE_METRIC_TERMS)
    ]
    assert not violations, "unverifiable percentage metric: " + ", ".join(violations)


def test_privacy_workflow_and_public_export_boundary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "privacy.yml").read_text(encoding="utf-8")
    trigger_head = workflow.split("\njobs:", 1)[0]
    assert "workflow_dispatch:" in trigger_head
    assert "pull_request:" in trigger_head
    assert "push:" not in trigger_head

    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "one-way whitelist export of `engine/`" in readme
    assert "publication artifact, never a runtime dependency" in readme
    assert "never receives private state, host bindings, credentials, sessions, caches, or machine-specific paths." in readme
