from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    mode, source = argv[0], Path(argv[1])
    try:
        value = source.read_text(encoding="utf-8").strip()
    except OSError:
        return 2
    if mode == "positive":
        return 0 if value == "valid" else 1
    if mode == "negative":
        return 1 if value == "invalid" else 0
    if mode == "consumer" and len(argv) == 3:
        return 0 if argv[2] in value else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
