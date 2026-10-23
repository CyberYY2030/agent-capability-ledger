from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLICATION_NARRATIVE_FILES = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/LESSONS.md",
    "docs/DESIGN.md",
)
PLATFORM_WORDS = r"macos|mac os|darwin|cross-platform|two machines|双机"
UNVERIFIED_SUPPORT_PATTERNS = (
    re.compile(rf"\b(?:{PLATFORM_WORDS})\b\s+(?:is|are)\s+(?:fully\s+)?(?:supported|ready|accepted|verified)\b", re.IGNORECASE),
    re.compile(rf"\b(?:supports?|accepts?|verifies)\s+(?:{PLATFORM_WORDS})\b", re.IGNORECASE),
    re.compile(rf"\b(?:{PLATFORM_WORDS})\b\s+works?\b", re.IGNORECASE),
    re.compile(r"download and run|works out of the box", re.IGNORECASE),
)
CHINESE_PLATFORM_WORDS = r"macos|mac os|darwin|linux|windows|跨平台|双机|多台机器"
CHINESE_SUPPORT_PATTERNS = (
    re.compile(rf"(?:{CHINESE_PLATFORM_WORDS})\s*(?:上|版本|流程)?\s*(?:已|已经)?\s*(?:全面|完全|正式)?\s*(?:支持|兼容|已验证|验证通过)", re.IGNORECASE),
    re.compile(rf"(?<![不未])(?:支持|兼容|已验证)\s*(?:了\s*)?(?:{CHINESE_PLATFORM_WORDS})", re.IGNORECASE),
    re.compile(r"开箱即用|下载即用"),
)
UNVERIFIED_SUPPORT_PATTERNS += CHINESE_SUPPORT_PATTERNS
UNVERIFIABLE_METRIC_TERMS = ("faster", "improve", "更快", "提升", "提高")


def _has_unverifiable_percentage_metric(text: str) -> bool:
    return any(mark in text for mark in ("%", "％")) and any(
        term in text for term in UNVERIFIABLE_METRIC_TERMS
    )


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
        if _has_unverifiable_percentage_metric(text)
    ]
    assert not violations, "unverifiable percentage metric: " + ", ".join(violations)


def test_privacy_workflow_and_public_export_boundary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "privacy.yml").read_text(encoding="utf-8")
    trigger_head = workflow.split("\njobs:", 1)[0]
    assert "workflow_dispatch:" in trigger_head
    assert "pull_request:" in trigger_head
    assert "push:" not in trigger_head

    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "这个公开仓库只有程序本身：它是作者私有仓库里 `engine/` 目录的导出，不含私有状态、本机配置、凭据、会话、缓存或本机路径，运行时也不依赖它。" in readme


@pytest.mark.parametrize(("pattern", "positive", "negative"), (
    (CHINESE_SUPPORT_PATTERNS[0], "Windows 已验证。", "Windows 上的完整流程还没有实测。"),
    (CHINESE_SUPPORT_PATTERNS[1], "兼容 macOS。", "下面的命令适用于 macOS 和 Linux。"),
    (CHINESE_SUPPORT_PATTERNS[2], "下载即用。", "首次使用前需要安装和配置。"),
))
def test_chinese_support_claim_patterns(
    pattern: re.Pattern[str], positive: str, negative: str,
) -> None:
    assert pattern.search(positive)
    assert not any(candidate.search(negative) for candidate in UNVERIFIED_SUPPORT_PATTERNS)


@pytest.mark.parametrize("term", ("更快", "提升", "提高"))
@pytest.mark.parametrize("percent", ("%", "％"))
def test_chinese_percentage_claim_patterns(term: str, percent: str) -> None:
    assert _has_unverifiable_percentage_metric(f"效率{term} 20{percent}")
    assert not _has_unverifiable_percentage_metric(f"效率{term}，目前没有量化证据。")
