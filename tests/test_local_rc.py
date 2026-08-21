from __future__ import annotations

import json
from pathlib import Path

from tests.run_local_rc import ROLLBACK_TESTS, run


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROLLBACK_TESTS = [
    "tests/test_install.py::test_install_plan_is_zero_write_and_apply_is_idempotent",
    "tests/test_install.py::test_install_verify_failure_rolls_back_every_target",
    "tests/test_install.py::test_receipt_write_failure_rolls_back_pin_and_all_installed_objects",
    "tests/test_upgrade.py::test_upgrade_restores_binding_when_installer_fails",
]


def test_local_rc_runs_installed_lifecycle_and_writes_bounded_manifest(tmp_path: Path) -> None:
    output = tmp_path / "local-rc.json"
    payload = run(ROOT, output)
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert payload["schema"] == "local-rc/1"
    assert payload["segments"][1] == {
        "id": "installed-stable-launcher-lifecycle",
        "commands": [
            "capture", "publish_plan", "publish_apply", "promote_plan",
            "promote_apply", "sync", "match", "retire_report",
        ],
    }
    assert list(ROLLBACK_TESTS) == EXPECTED_ROLLBACK_TESTS
    assert payload["segments"][2]["tests"] == EXPECTED_ROLLBACK_TESTS
    assert {
        payload["command_entrypoints"][command]
        for command in payload["segments"][1]["commands"]
    } == {"installed-stable-launcher"}
    assert payload["isolation"]["git_global_config"] == "isolated-empty"
    rendered = output.read_text(encoding="utf-8")
    assert "agent-core-docs-" not in rendered
    assert "AppData" not in rendered and "BaiduNetdiskDownload" not in rendered
