"""Dependency-free stable launcher for an installed, version-pinned engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import re
from pathlib import Path
from typing import Optional, Sequence


def _fail(code: str, detail: str) -> int:
    print(f"{code} {detail}", file=sys.stderr)
    return 1


def _load_object(path: Path, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{code} cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{code} root must be an object")
    return value


def _python_floor(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"FAIL_PYTHON_FLOOR cannot read {path}: {exc}") from exc
    matched = re.search(
        r'^requires-python\s*=\s*">=([0-9]+)\.([0-9]+)"\s*$', text, re.MULTILINE,
    )
    if matched is None:
        raise ValueError("FAIL_PYTHON_FLOOR requires-python must be a >=major.minor floor")
    return int(matched.group(1)), int(matched.group(2))


def _state_from_args(args: list[str], config: dict) -> tuple[Path, list[str]]:
    if args[:1] == ["--state"]:
        if len(args) < 2 or not args[1]:
            raise ValueError("FAIL_STATE_UNBOUND --state requires a path")
        return Path(args[1]).expanduser().resolve(), args[2:]
    value = config.get("state_root")
    if not isinstance(value, str) or not value or (value.startswith("<") and value.endswith(">")):
        raise ValueError("FAIL_STATE_UNBOUND use --state or bind host.json")
    return Path(value).expanduser().resolve(), args


def _inject_state(args: list[str], state_root: Path) -> list[str]:
    if not args or args[0] in {"--version", "state", "uninstall"}:
        return args
    if "--state" in args:
        return args
    if args[:2] in (["candidate", "publish"], ["lessons", "capture"]):
        return [*args[:2], "--state", str(state_root), *args[2:]]
    if args[:2] in (["lessons", "match"], ["lessons", "hook"]):
        if "--ledger" in args:
            return args
        return [
            *args[:2], "--ledger", str(state_root / "experience" / "LESSONS.md"),
            *args[2:],
        ]
    if args[:2] == ["engine", "upgrade"]:
        return [*args[:2], "--state", str(state_root), *args[2:]]
    if args[0] in {
        "check", "sync", "doctor", "fingerprint", "promote", "rollback", "recover", "install",
    }:
        return [args[0], "--state", str(state_root), *args[1:]]
    return args


def run(argv: Optional[Sequence[str]] = None, *, launcher_path: Optional[Path] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    launcher = (launcher_path or Path(__file__)).resolve()
    install_root = launcher.parent.parent
    pin_path = install_root / "engine-pin.json"
    try:
        source_pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        floor = _python_floor(source_pyproject) if source_pyproject.is_file() else None
        if floor is not None and sys.version_info < floor:
            raise ValueError(
                f"FAIL_PYTHON_VERSION requires Python >={floor[0]}.{floor[1]}"
            )
        if args.count("--state") > 1:
            raise ValueError("FAIL_STATE_ARGUMENT duplicate --state")
        pin = _load_object(pin_path, "FAIL_ENGINE_PIN")
        if set(pin) != {"schema", "version", "artifact_sha256", "config_path"}:
            raise ValueError("FAIL_ENGINE_PIN fields mismatch")
        if pin.get("schema") != "engine-pin/1":
            raise ValueError("FAIL_ENGINE_PIN schema mismatch")
        if floor is None:
            floor = _python_floor(
                install_root / "engine" / str(pin.get("version")) / "pyproject.toml"
            )
            if sys.version_info < floor:
                raise ValueError(
                    f"FAIL_PYTHON_VERSION requires Python >={floor[0]}.{floor[1]}"
                )
        config_path = Path(pin.get("config_path", "")).expanduser().resolve()
        config = _load_object(config_path, "FAIL_CONFIG")
        state_root, forwarded = _state_from_args(args, config)
        forwarded = _inject_state(forwarded, state_root)
        lock = _load_object(state_root / "agent-core.lock.json", "FAIL_STATE_LOCK")
        version = lock.get("engine_version")
        upgrade_escape = forwarded[:2] == ["engine", "upgrade"]
        if upgrade_escape and "--state" in forwarded:
            state_index = forwarded.index("--state")
            if state_index + 1 >= len(forwarded) or (
                Path(forwarded[state_index + 1]).expanduser().resolve() != state_root
            ):
                raise ValueError("FAIL_STATE_BINDING upgrade state differs from launcher state")
        if not isinstance(version, str) or (
            version != pin.get("version") and not upgrade_escape
        ):
            raise ValueError(
                f"FAIL_ENGINE_PIN state={version!r} installed={pin.get('version')!r}"
            )
        runtime_version = pin.get("version") if upgrade_escape else version
        engine_root = install_root / "engine" / str(runtime_version)
        if not (engine_root / "agent_core" / "cli.py").is_file():
            raise ValueError(f"FAIL_ENGINE_MISSING {engine_root}")
    except ValueError as exc:
        message = str(exc)
        code, _, detail = message.partition(" ")
        return _fail(code, detail)

    environment = os.environ.copy()
    environment["AGENT_CORE_HOST_CONFIG"] = str(config_path)
    environment["AGENT_CORE_STATE"] = str(state_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(engine_root) + (
        os.pathsep + current_pythonpath if current_pythonpath else ""
    )
    completed = subprocess.run(
        [sys.executable, "-P", "-m", "agent_core.cli", *forwarded],
        check=False,
        env=environment,
    )
    return completed.returncode


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
