"""Stable command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, capture, installer, match as lesson_match, project_promote, reject, retire
from .config import ConfigError
from .doctor import run as run_doctor
from .sync import execute as execute_sync


ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core")
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("install")

    sync = subparsers.add_parser(
        "sync",
        description=(
            "Preview what would be written. Repository write eligibility is checked only "
            "when applying."
        ),
    )
    sync.add_argument("--config", type=Path)
    sync.add_argument("--state", type=Path)
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sync.add_argument("--plan-hash", help="required with --apply; use the exact dry-run PLAN_HASH")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--state", type=Path)
    doctor.add_argument("--state-manifest", type=Path)
    doctor.add_argument("--workspace", type=Path, default=Path.cwd())

    subparsers.add_parser("lessons")
    return parser


def _lessons_help_parser() -> argparse.ArgumentParser:
    """Describe the complete public surface, including commands routed above match."""
    parser = argparse.ArgumentParser(prog="agent-core lessons")
    commands = parser.add_subparsers(dest="lessons_command")
    for name, description in (
        ("capture", "保存待审核的经验候选"),
        ("promote", "预览或应用已审核候选的晋升"),
        ("reject", "拒绝候选"),
        ("retire", "退役正式经验"),
        ("match", "检索匹配的正式经验"),
        ("eval", "评估检索夹具"),
        ("hook", "运行宿主检索钩子"),
    ):
        commands.add_parser(name, help=description)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["lessons", "--help"], ["lessons", "-h"]):
        _lessons_help_parser().parse_args(argv[1:])
    if argv[:2] == ["lessons", "retire"]:
        return retire.main(argv[2:])
    if argv[:2] == ["lessons", "capture"]:
        return capture.main(argv[2:])
    if argv[:2] == ["lessons", "promote"]:
        return project_promote.main(argv[2:])
    if argv[:2] == ["lessons", "reject"]:
        return reject.main(argv[2:])
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
                    require_versioned=args.apply, plan_hash=args.plan_hash):
                print(line)
            return 0
        if args.command == "doctor":
            for line in run_doctor(
                    ENGINE_ROOT, args.config, args.state, args.state_manifest,
                    require_versioned=args.state is not None, workspace=args.workspace):
                print(line)
            return 0
        parser.print_help()
        return 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
