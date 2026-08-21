"""Validate the required structure of the public Markdown sink templates."""

from __future__ import annotations

import argparse
from pathlib import Path


REQUIRED = {
    "task-card": ("## Contract", "## Acceptance", "## Reverse validation"),
    "audit-checklist": ("## Scope", "## Evidence", "## Verdict"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=(*REQUIRED, "contains"))
    parser.add_argument("path", type=Path)
    parser.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    try:
        text = args.path.read_text(encoding="utf-8")
    except OSError:
        return 2
    if args.mode == "contains":
        return 0 if args.value and args.value in text else 1
    return 0 if all(marker in text for marker in REQUIRED[args.mode]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
