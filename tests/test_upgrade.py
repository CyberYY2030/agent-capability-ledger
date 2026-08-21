from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_core.installer as installer
import agent_core.upgrade as upgrade
from agent_core.config import ConfigError
from agent_core.installer import apply_install, build_release_manifest
from agent_core.promote import operation_lock
from agent_core.state import apply_attach, apply_init, binding_receipt_path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = "0.1.0.dev0"
TARGET_VERSION = "0.2.0"
TEST_EMAIL = "upgrade" + chr(64) + "invalid"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def _target_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    target = tmp_path / "target-release"
    target.mkdir()
    for name in installer.PAYLOAD_DIRS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in installer.PAYLOAD_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, target / name)
    init = target / "agent_core" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace(CURRENT_VERSION, TARGET_VERSION),
        encoding="utf-8",
    )
    manifest = target / "release-manifest.json"
    with monkeypatch.context() as scoped:
        scoped.setattr(installer, "__version__", TARGET_VERSION)
        payload = build_release_manifest(target)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target, manifest


def _refresh_manifest(target: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(installer, "__version__", TARGET_VERSION)
        payload = build_release_manifest(target)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    state_source = tmp_path / "state-source"
    apply_init(ROOT, state_source, git_name="Synthetic Upgrade", git_email=TEST_EMAIL)
    remote = tmp_path / "state-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(state_source, "remote", "add", "origin", str(remote))
    git(state_source, "push", "-q", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    state = tmp_path / "state"
    subprocess.run(["git", "clone", "-q", str(remote), str(state)], check=True)
    git(state, "config", "user.name", "Synthetic Upgrade")
    git(state, "config", "user.email", TEST_EMAIL)

    config_payload = json.loads((ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    config_payload["backup_root"] = str(tmp_path / "host" / "backups")
    for index, target in enumerate(config_payload["targets"]):
        target["root"] = str(tmp_path / "runtimes" / f"runtime-{index}")
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    config.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    apply_attach(state, config, confirm_private_remote=True)

    current_manifest = tmp_path / "current-release.json"
    current_manifest.write_text(
        json.dumps(build_release_manifest(ROOT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    apply_install(ROOT, config, state, ROOT, current_manifest, force=False)

    lock_path = state / "agent-core.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["engine_version"] = TARGET_VERSION
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    git(state, "add", "agent-core.lock.json")
    git(state, "commit", "-q", "-m", "Advance engine lock for upgrade fixture")
    git(state, "push", "-q", "origin", "main")
    target, manifest = _target_source(tmp_path, monkeypatch)
    install_root = tmp_path / "local-data" / "agent-core"
    return state, config, target, manifest, install_root, tmp_path / "control"


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file() and ".git" not in path.parts
    }


def _content_snapshot(root: Path) -> dict[str, bytes]:
    return {name: value[0] for name, value in _snapshot(root).items()}


def test_upgrade_plan_is_zero_write_and_apply_keeps_old_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, target, manifest, install_root, control = _fixture(tmp_path, monkeypatch)
    before = {
        "state": _snapshot(state),
        "host": _snapshot(config.parent),
        "install": _snapshot(install_root),
        "target": _snapshot(target),
    }
    state_head = git(state, "rev-parse", "HEAD").stdout.strip()
    plan = upgrade.plan_upgrade(ROOT, state, config, target, manifest, TARGET_VERSION)
    assert upgrade.render_plan(plan)[-1] == "DRY_RUN writes=0"
    assert before == {
        "state": _snapshot(state),
        "host": _snapshot(config.parent),
        "install": _snapshot(install_root),
        "target": _snapshot(target),
    }

    result = upgrade.apply_upgrade(
        ROOT, state, config, target, manifest, TARGET_VERSION, control, plan.plan_hash,
    )
    assert result[0] == f"APPLIED engine-upgrade to={TARGET_VERSION}"
    pin = json.loads((install_root / "engine-pin.json").read_text(encoding="utf-8"))
    receipt = json.loads((config.parent / "install-receipt.json").read_text(encoding="utf-8"))
    binding = json.loads(binding_receipt_path(config).read_text(encoding="utf-8"))
    assert pin["version"] == receipt["engine_version"] == TARGET_VERSION
    assert binding["state_lock_sha256"] == upgrade._sha256((state / "agent-core.lock.json").read_bytes())
    assert (install_root / "engine" / CURRENT_VERSION).is_dir()
    assert (install_root / "engine" / TARGET_VERSION).is_dir()
    assert git(state, "rev-parse", "HEAD").stdout.strip() == state_head
    assert not git(state, "status", "--porcelain").stdout.strip()


def test_upgrade_rejects_stale_plan_after_valid_artifact_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, target, manifest, _install_root, control = _fixture(tmp_path, monkeypatch)
    plan = upgrade.plan_upgrade(ROOT, state, config, target, manifest, TARGET_VERSION)
    source = target / "agent_core" / "upgrade.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# release rebuild\n", encoding="utf-8")
    _refresh_manifest(target, manifest, monkeypatch)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        upgrade.apply_upgrade(
            ROOT, state, config, target, manifest, TARGET_VERSION, control, plan.plan_hash,
        )


def test_upgrade_restores_binding_when_installer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, target, manifest, install_root, control = _fixture(tmp_path, monkeypatch)
    plan = upgrade.plan_upgrade(ROOT, state, config, target, manifest, TARGET_VERSION)
    receipt_path = binding_receipt_path(config)
    before = receipt_path.read_bytes()
    trees_before = {
        "state": _content_snapshot(state),
        "host": _content_snapshot(config.parent),
        "install": _content_snapshot(install_root),
        "runtimes": _content_snapshot(tmp_path / "runtimes"),
    }
    with operation_lock(control):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            upgrade.apply_upgrade(
                ROOT, state, config, target, manifest, TARGET_VERSION, control, plan.plan_hash,
            )
    assert receipt_path.read_bytes() == before

    real_verify = installer._verify_installed

    def fail_target(plan_value, *args, **kwargs):
        if plan_value.artifact.version == TARGET_VERSION:
            assert receipt_path.read_bytes() == plan.refreshed_binding
            raise ConfigError("FAIL_INSTALL_VERIFY", "injected")
        return real_verify(plan_value, *args, **kwargs)

    monkeypatch.setattr(installer, "_verify_installed", fail_target)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_VERIFY"):
        upgrade.apply_upgrade(
            ROOT, state, config, target, manifest, TARGET_VERSION, control, plan.plan_hash,
        )
    assert receipt_path.read_bytes() == before
    assert trees_before == {
        "state": _content_snapshot(state),
        "host": _content_snapshot(config.parent),
        "install": _content_snapshot(install_root),
        "runtimes": _content_snapshot(tmp_path / "runtimes"),
    }


def test_launcher_allows_only_upgrade_across_pin_lock_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, target, manifest, install_root, control = _fixture(tmp_path, monkeypatch)
    launcher = install_root / "bin" / "agent_core_launcher.py"
    environment = os.environ.copy()
    environment["AGENT_CORE_PYTHON"] = os.fspath(Path(os.sys.executable))

    def launch(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [os.sys.executable, str(launcher), "--state", str(state), *args],
            check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
        )

    blocked = launch("--version")
    assert blocked.returncode != 0 and "FAIL_ENGINE_PIN" in blocked.stderr
    confused = launch(
        "engine", "upgrade", "--state", str(tmp_path / "different-state"),
        "--config", str(config), "--source", str(target), "--to", TARGET_VERSION,
    )
    assert confused.returncode != 0 and "FAIL_STATE_ARGUMENT" in confused.stderr
    planned = launch(
        "engine", "upgrade", "--config", str(config), "--source", str(target),
        "--manifest", str(manifest), "--to", TARGET_VERSION, "--control-root", str(control),
    )
    assert planned.returncode == 0, planned.stderr
    plan_hash = next(line.split(" ", 1)[1] for line in planned.stdout.splitlines() if line.startswith("PLAN_HASH "))
    applied = launch(
        "engine", "upgrade", "--config", str(config), "--source", str(target),
        "--manifest", str(manifest), "--to", TARGET_VERSION, "--control-root", str(control),
        "--apply", "--plan-hash", plan_hash,
    )
    assert applied.returncode == 0, applied.stderr
    version = launch("--version")
    assert version.returncode == 0 and version.stdout.strip() == TARGET_VERSION


def test_upgrade_rejects_manifest_source_version_mismatch_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, target, manifest, install_root, _control = _fixture(tmp_path, monkeypatch)
    init = target / "agent_core" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(TARGET_VERSION, "0.2.1"), encoding="utf-8")
    _refresh_manifest(target, manifest, monkeypatch)
    before = _snapshot(install_root)
    with pytest.raises(ConfigError, match="FAIL_ARTIFACT_VERSION"):
        upgrade.plan_upgrade(ROOT, state, config, target, manifest, TARGET_VERSION)
    assert _snapshot(install_root) == before
