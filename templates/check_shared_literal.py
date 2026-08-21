"""Template guard: reject an old shared literal anywhere under a bounded root."""

from __future__ import annotations

import argparse
from pathlib import Path


# Seed lessons enforced by this sink. The verifier probes both references.
LESSON_IDS = ("L-1", "L-4")
TEXT_SUFFIXES = {".json", ".md", ".py", ".txt", ".yaml", ".yml"}


def _files(root: Path):
    if root.is_file():
        yield root
        return
    if root.is_dir():
        yield from (
            path for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.casefold() in TEXT_SUFFIXES
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--literal", required=True)
    parser.add_argument("--expect", choices=("absent", "present"), default="absent")
    args = parser.parse_args(argv)
    if not args.root.exists():
        return 2
    try:
        found = any(
            args.literal in path.read_text(encoding="utf-8", errors="replace")
            for path in _files(args.root)
        )
    except OSError:
        return 2
    expected = args.expect == "present"
    return 0 if found == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
