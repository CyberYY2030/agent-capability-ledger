from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_core.config import ConfigError
from agent_core.docs_contract import render_docs, verify_commands


ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / "docs" / "commands.json"


def _write_commands(tmp_path: Path, mutate) -> Path:
    payload = json.loads(COMMANDS.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "commands.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_documented_commands_run_in_hermetic_fixture() -> None:
    results = verify_commands(ROOT, COMMANDS)
    assert [item.command_id for item in results] == [
        "state_init", "state_attach", "install", "capture", "publish_plan",
        "publish_apply", "promote_plan", "promote_apply", "sync", "match",
        "retire_report",
    ]
    assert all(item.normalized_stdout for item in results)


def test_docs_fixture_ignores_host_git_signing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_config = tmp_path / "host-gitconfig"
    host_config.write_text("[commit]\n\tgpgsign = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(host_config))
    results = verify_commands(ROOT, COMMANDS)
    assert len(results) == 11


def test_readme_and_lifecycle_generated_blocks_are_current() -> None:
    assert render_docs(ROOT, COMMANDS, apply=False) == ["PASS docs_render changed=0"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = [
        "## What it solves", "## Layering principle", "## Three-minute start",
        "## What it is not", "## Design choices",
    ]
    assert [readme.index(heading) for heading in headings] == sorted(
        readme.index(heading) for heading in headings
    )
    assert readme.count("<!-- COMMANDS:quickstart:START -->") == 1
    lifecycle = (ROOT / "docs" / "LIFECYCLE.md").read_text(encoding="utf-8")
    assert lifecycle.count("<!-- COMMANDS:lifecycle:START -->") == 1


def test_render_check_rejects_drift_inside_bounded_block(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    (tmp_path / "README.md").write_text(
        readme.replace("PASS revision=<SHA>", "PASS revision=<INVENTED>", 1),
        encoding="utf-8",
    )
    (tmp_path / "docs" / "LIFECYCLE.md").write_bytes(
        (ROOT / "docs" / "LIFECYCLE.md").read_bytes()
    )
    with pytest.raises(ConfigError, match="FAIL_DOCS_DRIFT"):
        render_docs(tmp_path, COMMANDS, apply=False)


def test_token_must_be_one_complete_argv_element(tmp_path: Path) -> None:
    path = _write_commands(
        tmp_path,
        lambda payload: payload["steps"][0]["argv"].append("prefix-{{state}}"),
    )
    with pytest.raises(ConfigError, match="FAIL_DOCS_SCHEMA"):
        verify_commands(ROOT, path)


def test_plan_capture_must_have_a_real_apply_consumer(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        step = next(item for item in payload["steps"] if item["id"] == "publish_plan")
        step["capture"].pop("publish_plan_hash")
        step["example_output"][-1] = "PLAN_HASH <SHA256>"

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_TOKEN"):
        verify_commands(ROOT, path)


def test_displayed_output_must_exist_in_real_normalized_stdout(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        payload["steps"][0]["example_output"] = ["PASS invented output"]

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_EXAMPLE"):
        verify_commands(ROOT, path)


def test_every_real_output_line_must_be_displayed_or_exactly_omitted(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        payload["steps"][0]["example_output"].pop()

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_EXAMPLE"):
        verify_commands(ROOT, path)


def test_displayed_output_preserves_real_order(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        payload["steps"][0]["example_output"].reverse()

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_EXAMPLE"):
        verify_commands(ROOT, path)


def test_omit_requires_an_exact_line_and_reason(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        payload["steps"][0]["omit"] = [{"reason": "too broad"}]

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_SCHEMA"):
        verify_commands(ROOT, path)


def test_omit_preserves_duplicate_counts(tmp_path: Path) -> None:
    def mutate(payload) -> None:
        step = payload["steps"][2]
        step["omit"].append(dict(step["omit"][0]))

    path = _write_commands(tmp_path, mutate)
    with pytest.raises(ConfigError, match="FAIL_DOCS_EXAMPLE"):
        verify_commands(ROOT, path)


def test_exact_omit_is_verified_but_not_rendered() -> None:
    results = verify_commands(ROOT, COMMANDS)
    install = next(item for item in results if item.command_id == "install")
    assert "APPLIED install version=" in install.normalized_stdout
    rendered = render_docs(ROOT, COMMANDS, apply=False)
    assert rendered == ["PASS docs_render changed=0"]
    assert "APPLIED install version=" not in (ROOT / "README.md").read_text(encoding="utf-8")


def test_docs_executor_has_no_dynamic_language_or_shell_execution() -> None:
    inspected = [ROOT / "agent_core" / "docs_contract.py", ROOT / "tests" / "test_docs_contract.py"]
    for path in inspected:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            assert not (isinstance(node.func, ast.Name) and node.func.id == "eval")
            for keyword in node.keywords:
                assert not (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                )
