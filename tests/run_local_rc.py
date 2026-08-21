from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_core import __version__
from agent_core.docs_contract import verify_commands


SCHEMA = "local-rc/1"
ROLLBACK_TESTS = (
    "tests/test_install.py::test_install_plan_is_zero_write_and_apply_is_idempotent",
    "tests/test_install.py::test_install_verify_failure_rolls_back_every_target",
    "tests/test_install.py::test_receipt_write_failure_rolls_back_pin_and_all_installed_objects",
    "tests/test_upgrade.py::test_upgrade_restores_binding_when_installer_fails",
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run(engine_root: Path, output: Path) -> dict:
    engine_root = engine_root.resolve()
    results = verify_commands(
        engine_root,
        engine_root / "docs" / "commands.json",
        lifecycle_via_launcher=True,
    )
    with tempfile.TemporaryDirectory(prefix="agent-core-local-rc-tests-") as temporary:
        isolation = Path(temporary)
        global_config = isolation / "git-global.config"
        system_config = isolation / "git-system.config"
        global_config.write_text("", encoding="utf-8")
        system_config.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(isolation / "home"),
            "USERPROFILE": str(isolation / "home"),
            "LOCALAPPDATA": str(isolation / "local-data"),
            "XDG_DATA_HOME": str(isolation / "xdg-data"),
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_SYSTEM": str(system_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(engine_root),
        })
        checked = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *ROLLBACK_TESTS],
            cwd=engine_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    if checked.returncode != 0:
        raise RuntimeError(checked.stdout + checked.stderr)
    summary = checked.stdout.strip().splitlines()[-1]
    payload = {
        "schema": SCHEMA,
        "engine_version": __version__,
        "segments": [
            {
                "id": "source-bootstrap-and-docs",
                "commands": [item.command_id for item in results if item.command_id in {"state_init", "state_attach", "install"}],
            },
            {
                "id": "installed-stable-launcher-lifecycle",
                "commands": [item.command_id for item in results if item.command_id not in {"state_init", "state_attach", "install"}],
            },
            {
                "id": "idempotency-and-rollback",
                "tests": list(ROLLBACK_TESTS),
                "summary": summary,
            },
        ],
        "command_output_sha256": {
            item.command_id: _sha256(item.normalized_stdout) for item in results
        },
        "command_entrypoints": {
            item.command_id: item.entrypoint for item in results
        },
        "isolation": {
            "temporary_home": True,
            "temporary_runtime_roots": 2,
            "local_bare_remote_only": True,
            "git_global_config": "isolated-empty",
            "git_system_config": "disabled-and-isolated-empty",
            "real_state_or_runtime_modified": False,
        },
        "limitations": [
            "does not prove real runtime event delivery",
            "does not prove cross-machine or macOS parity",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_output, output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = run(args.engine, args.output)
    print(
        f"PASS local_rc commands={len(payload['command_output_sha256'])} "
        f"rollback_tests={len(ROLLBACK_TESTS)} manifest={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
