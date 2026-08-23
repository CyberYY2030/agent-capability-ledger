"""Project-scoped lesson promotion that stops at the worktree/index boundary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, default_config_path, load_config
from .promote import apply_local_promote, plan_local_promote


ENGINE_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core lessons promote")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--control-root", type=Path)
    parser.add_argument("--config", type=Path, default=default_config_path(ENGINE_ROOT))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--scope", help="global, profile:<declared>, or project:<current-project-id>")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--update")
    choice.add_argument("--supersedes")
    choice.add_argument("--force-new", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.update is not None:
            parts = args.update.split(":", 2)
            if len(parts) != 3 or not all(parts):
                raise ConfigError("FAIL_UPDATE_TARGET", "RETRY --update <scope:store:lesson-id>")
        state_root = args.state
        if state_root is None:
            configured = load_config(args.config)["state_root"]
            if not (configured.startswith("<") and configured.endswith(">")):
                state_root = Path(configured).expanduser()
        plan = plan_local_promote(
            args.workspace, args.control_root, args.id,
            scope_override=args.scope, supersedes=args.supersedes, force_new=args.force_new, update=args.update,
            state_root=state_root, config_path=args.config,
        )
        if not args.apply:
            for line in plan.lines:
                print(line)
            return 0
        if not args.plan_hash:
            raise ConfigError("FAIL_PLAN_HASH", "--apply requires --plan-hash")
        if args.plan_hash != plan.plan_hash:
            raise ConfigError("FAIL_INPUT_CHANGED", args.id)
        result = apply_local_promote(
            args.workspace, args.control_root, plan, args.plan_hash,
            config_path=args.config,
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
