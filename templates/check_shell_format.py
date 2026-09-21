"""Check explicit final shell files for carriage-return bytes without modifying them."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path


def _check(path: Path) -> tuple[bool, str]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False, "missing"
    except OSError as exc:
        return False, f"unreadable error={exc}"

    if stat.S_ISLNK(mode):
        return False, "symlink"
    if not stat.S_ISREG(mode):
        return False, "not-regular-file"
    if path.suffix.casefold() != ".sh":
        return True, "not-shell"

    try:
        content = path.read_bytes()
    except OSError as exc:
        return False, f"unreadable error={exc}"
    if b"\r" in content:
        return False, "carriage-return"
    return True, "shell"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check explicit final .sh files for CR or CRLF bytes.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="final artifact path")
    args = parser.parse_args(argv)

    failed = False
    for path in args.paths:
        passed, reason = _check(path)
        if not passed:
            print(f"FAIL {path} reason={reason}", file=sys.stderr)
            failed = True
        elif reason == "not-shell":
            print(f"SKIP {path} reason=not-shell")
        else:
            print(f"PASS {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
