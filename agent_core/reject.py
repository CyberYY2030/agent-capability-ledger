"""Move one reviewed lesson candidate into its repository-local rejected queue."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, default_config_path, load_config
from .promote import apply_local_reject, plan_local_reject


ENGINE_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core lessons reject")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--control-root", type=Path)
    parser.add_argument("--config", type=Path, default=default_config_path(ENGINE_ROOT))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-hash")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state_root = args.state
        if state_root is None:
            configured = load_config(args.config)["state_root"]
            if not (configured.startswith("<") and configured.endswith(">")):
                state_root = Path(configured).expanduser()
        plan = plan_local_reject(
            args.workspace, args.control_root, args.id,
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
        result = apply_local_reject(
            args.workspace, args.control_root, plan, args.plan_hash,
            config_path=args.config,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for path in result.changed_paths:
        print(f"STAGED {path}")
    print(f"PASS lesson_rejected={result.candidate_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
