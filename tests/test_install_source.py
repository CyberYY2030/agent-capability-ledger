from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core import installer
from tests.engine_fixture import create_engine_repo
from tests.test_install import git, installed_fixture


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AGENT_CORE_"):
            monkeypatch.delenv(name)
    for name, directory in (
        ("HOME", "home"), ("USERPROFILE", "home"),
        ("XDG_DATA_HOME", "xdg-data"), ("LOCALAPPDATA", "local-data"),
    ):
        monkeypatch.setenv(name, str(tmp_path / directory))
    (tmp_path / "home").mkdir()


def _run_plan(
    running_engine: Path, config: Path, *, state: Path | None = None, source: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable, "-P", "-m", "agent_core.cli", "install", "--config", str(config),
    ]
    if state is not None:
        command.extend(["--state", str(state)])
    if source is not None:
        command.extend(["--source", str(source)])
    return subprocess.run(
        command, input="", capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONPATH=str(running_engine), PYTHONDONTWRITEBYTECODE="1"),
        cwd=os.environ["HOME"], timeout=60, check=False,
    )


@pytest.mark.parametrize("selection", ["sibling", "fallback", "explicit"])
@pytest.mark.parametrize("explicit_state", [True, False])
def test_install_source_selection(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    selection: str, explicit_state: bool,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state, config, _manifest, install_root = installed_fixture(
        standalone_engine, workspace, monkeypatch,
    )
    Path(os.environ["HOME"]).mkdir(parents=True)
    expected = standalone_engine
    if selection != "fallback":
        sibling = create_engine_repo(workspace / "engine")
        (sibling / "NOTICE").write_text("Synthetic sibling engine\n", encoding="utf-8")
        manifest = installer.build_release_manifest(sibling)
        (sibling / "release-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        git(sibling, "add", "NOTICE", "release-manifest.json")
        git(
            sibling, "-c", "user.name=Synthetic Engine", "-c", "user.email=engine@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic artifact",
        )
        running = json.loads((standalone_engine / "release-manifest.json").read_text(encoding="utf-8"))
        assert manifest["artifact_sha256"] != running["artifact_sha256"]
        if selection == "sibling":
            expected = sibling

    result = _run_plan(
        standalone_engine, config, state=state if explicit_state else None,
        source=standalone_engine if selection == "explicit" else None,
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads((expected / "release-manifest.json").read_text(encoding="utf-8"))
    lines = result.stdout.splitlines()
    artifact_line = f"PLAN artifact_sha256={artifact['artifact_sha256']}"
    assert artifact_line in lines
    assert lines[lines.index(artifact_line) + 1] == f"PLAN source={expected.resolve()}"
    assert "PLAN_HASH " in result.stdout
    assert "DRY_RUN writes=0 ready=true" in result.stdout
    assert not install_root.exists()
    assert not (config.parent / "install-receipt.json").exists()


@pytest.mark.parametrize("config_kind", ["missing", "invalid-json", "missing-state", "placeholder"])
def test_source_resolution_leaves_config_errors_to_plan(tmp_path: Path, config_kind: str) -> None:
    config = tmp_path / "host.json"
    if config_kind != "missing":
        payload = {"invalid-json": "{", "missing-state": "{}", "placeholder": None}[config_kind]
        if payload is None:
            example = Path(__file__).resolve().parents[1] / "examples" / "host.example.json"
            value = json.loads(example.read_text(encoding="utf-8"))
            value["state_root"] = "<STATE_ROOT>"
            payload = json.dumps(value)
        config.write_text(payload, encoding="utf-8")
    running = tmp_path / "running-engine"
    assert installer._resolve_source(running, config, None, None) == running


def test_sibling_without_manifest_falls_back(tmp_path: Path) -> None:
    sibling = tmp_path / "engine"
    sibling.mkdir()
    running = tmp_path / "running-engine"
    state = tmp_path / "state"
    config = tmp_path / "missing-config.json"
    assert installer._resolve_source(running, config, state, None) == running
    (sibling / "release-manifest.json").mkdir()
    assert installer._resolve_source(running, config, state, None) == running


def test_explicit_state_source_lookup_does_not_require_readable_config(tmp_path: Path) -> None:
    sibling = tmp_path / "engine"
    sibling.mkdir()
    (sibling / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    assert installer._resolve_source(
        tmp_path / "running-engine", tmp_path / "missing-config.json", tmp_path / "state", None,
    ) == sibling


def test_source_fallback_preserves_normal_config_failure(
    standalone_engine: Path, tmp_path: Path,
) -> None:
    config = tmp_path / "host.json"
    config.write_text("{", encoding="utf-8")
    result = _run_plan(standalone_engine, config)
    assert result.returncode == 1
    assert "FAIL_CONFIG cannot read" in result.stderr
    assert not result.stdout
