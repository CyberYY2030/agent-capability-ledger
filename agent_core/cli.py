"""Stable command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, capture, installer, match as lesson_match, project_promote, retire
from .config import ConfigError
from .doctor import run as run_doctor
from .sync import execute as execute_sync


ENGINE_ROOT = Path(__file__).resolve().parents[1]
FROZEN_TOP_LEVEL_COMMANDS = frozenset({
    "candidate",
    "check",
    "docs",
    "fingerprint",
    "manifest",
    "migrate",
    "parity",
    "privacy",
    "promote",
    "recover",
    "rollback",
    "state",
    "uninstall",
})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core")
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("install")

    sync = subparsers.add_parser("sync")
    sync.add_argument("--config", type=Path)
    sync.add_argument("--state", type=Path)
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--state", type=Path)
    doctor.add_argument("--state-manifest", type=Path)

    subparsers.add_parser("lessons")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    frozen_command = "engine upgrade" if argv[:2] == ["engine", "upgrade"] else (
        argv[0] if argv and argv[0] in FROZEN_TOP_LEVEL_COMMANDS else None
    )
    if frozen_command is not None:
        print(f"FAIL_COMMAND_FROZEN {frozen_command}", file=sys.stderr)
        return 2
    if argv[:2] == ["lessons", "retire"]:
        return retire.main(argv[2:])
    if argv[:2] == ["lessons", "capture"]:
        return capture.main(argv[2:])
    if argv[:2] == ["lessons", "promote"]:
        return project_promote.main(argv[2:])
    if argv and argv[0] == "lessons":
        return lesson_match.main(argv[1:])
    if argv and argv[0] == "install":
        return installer.main(argv, ENGINE_ROOT)
    parser = _parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    try:
        if args.command == "sync":
            for line in execute_sync(
                    ENGINE_ROOT, args.config, args.state, apply=args.apply,
                    require_versioned=args.apply):
                print(line)
            return 0
        if args.command == "doctor":
            for line in run_doctor(
                    ENGINE_ROOT, args.config, args.state, args.state_manifest,
                    require_versioned=args.state is not None):
                print(line)
            return 0
        parser.print_help()
        return 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
