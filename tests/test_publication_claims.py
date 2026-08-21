from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# PLAN.md is intentionally excluded: its macOS matches are historical statements denying support, not support claims.
PUBLICATION_NARRATIVE_FILES = (
    "README.md",
    "docs/DESIGN.md",
    "docs/LIFECYCLE.md",
    "docs/REPOSITORY_SEPARATION.md",
)
FORBIDDEN_CAPABILITY_CLAIMS = (
    "macos",
    "mac os",
    "cross-platform",
    "跨平台",
    "darwin",
    "two machines",
    "双机",
    "download and run",
    "works out of the box",
)
UNVERIFIABLE_METRIC_TERMS = ("faster", "improve")


def _narrative_texts() -> dict[str, str]:
    return {
        relative_path: (ROOT / relative_path).read_text(encoding="utf-8").casefold()
        for relative_path in PUBLICATION_NARRATIVE_FILES
    }


def test_publication_narrative_excludes_unverified_capability_claims() -> None:
    violations = [
        f"{relative_path}: {term}"
        for relative_path, text in _narrative_texts().items()
        for term in FORBIDDEN_CAPABILITY_CLAIMS
        if term in text
    ]
    assert not violations, "unverified capability claims: " + ", ".join(violations)


def test_publication_narrative_excludes_unverifiable_percentage_metrics() -> None:
    violations = [
        relative_path
        for relative_path, text in _narrative_texts().items()
        if "%" in text and any(term in text for term in UNVERIFIABLE_METRIC_TERMS)
    ]
    assert not violations, "unverifiable percentage metric: " + ", ".join(violations)


def test_first_linux_privacy_gate_requires_manual_private_validation() -> None:
    workflow = (ROOT / ".github" / "workflows" / "privacy.yml").read_text(encoding="utf-8")
    trigger_head = workflow.split("\njobs:", 1)[0]
    assert "workflow_dispatch:" in trigger_head
    assert "pull_request:" in trigger_head
    assert "push:" not in trigger_head

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert (
        "The first remote must remain private. Run the `privacy-gate` workflow manually; "
        "restore the `push` trigger only after it succeeds. Linux remains unverified, "
        "and D6/public publication still require explicit human approval."
    ) in readme
