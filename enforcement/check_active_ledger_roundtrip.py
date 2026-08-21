#!/usr/bin/env python3
"""External ledger acceptance; no clean-room fixtures or ledger content are embedded.

The expected count is supplied by the caller and never derived from the code under
test. An earlier version computed the active section with `ledger.is_active_heading`,
the very predicate this check exists to protect: reverting that predicate made the
parser return zero entries and the check still reported PASS (0 == 0). A reference
set that calls into the thing it is checking has no discriminating power, so the
only heading logic here is a local literal, and the binding assertion is the frozen
count the caller passes in.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from agent_core import match


ARCHIVE_HEADING_MARKERS = ("归档", "archive")
HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")


def total_entry_count(text: str) -> int:
    """Count every entry line in the file, using no heading logic at all."""
    return sum(1 for line in text.splitlines() if match.ENTRY_RE.match(line))


def archived_entry_count(text: str) -> int:
    """Count entries under archive headings, by a local literal independent of the engine."""
    archived = False
    count = 0
    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            folded = heading.group(1).casefold()
            archived = any(marker in folded for marker in ARCHIVE_HEADING_MARKERS)
        elif archived and match.ENTRY_RE.match(line):
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert a ledger's active entries round-trip through the lessons parser."
    )
    parser.add_argument("ledger", type=Path)
    parser.add_argument(
        "--expect", type=int, required=True,
        help="frozen active-entry count for this ledger, supplied by the caller",
    )
    args = parser.parse_args(argv)
    text = args.ledger.read_text(encoding="utf-8")

    parsed = len(match.parse_markdown(text, "external", args.ledger.name))
    total = total_entry_count(text)
    independent_active = total - archived_entry_count(text)
    name = args.ledger.name

    failures = []
    if parsed != args.expect:
        failures.append(f"parsed={parsed} expected={args.expect}")
    if parsed != independent_active:
        failures.append(f"parsed={parsed} independent_active={independent_active}")
    if parsed > total:
        failures.append(f"parsed={parsed} exceeds total_entry_re={total}")
    if failures:
        print(f"FAIL {'; '.join(failures)} total_entry_re={total} ledger={name}")
        return 1
    print(f"PASS parsed={parsed} expected={args.expect} total_entry_re={total} ledger={name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
