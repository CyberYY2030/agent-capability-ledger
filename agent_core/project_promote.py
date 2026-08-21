"""Project-scoped lesson promotion that stops at the worktree/index boundary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError
from .promote import apply_project_promote, plan_project_promote


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core lessons promote")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--control-root", type=Path, default=Path.home() / ".agent-core")
    parser.add_argument("--id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--supersedes")
    choice.add_argument("--force-new", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = plan_project_promote(
            args.workspace, args.control_root, args.id,
            supersedes=args.supersedes, force_new=args.force_new,
        )
        if not args.apply:
            for line in plan.lines:
                print(line)
            return 0
        if not args.plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", "--apply requires --plan-hash")
        if args.plan_hash != plan.plan_hash:
            raise ConfigError("FAIL_INPUT_CHANGED", args.id)
        result = apply_project_promote(
            args.workspace, args.control_root, plan, args.plan_hash,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for path in result.changed_paths:
        print(f"STAGED {path}")
    print(f"PASS project_promoted={result.lesson_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
