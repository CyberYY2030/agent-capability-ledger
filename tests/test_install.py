from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import agent_core.launcher as launcher_module
from agent_core import installer as installer_module
from agent_core.config import ConfigError
from agent_core.installer import (
    _assert_within,
    apply_install,
    build_release_manifest,
    plan_install,
    verify_release_manifest,
)
from agent_core.state import BindingEvidence, apply_attach, apply_init, binding_receipt_path


ROOT = Path(__file__).resolve().parents[1]
TEST_EMAIL = "installer" + chr(64) + "invalid"


def test_launcher_rejects_unsupported_python_before_reading_install_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(launcher_module.sys, "version_info", (3, 10))
    assert launcher_module.run([], launcher_path=tmp_path / "bin" / "launcher.py") == 1
    assert "FAIL_PYTHON_VERSION requires Python >=3.11" in capsys.readouterr().err


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


def synthetic_binding(config: Path, state: Path) -> BindingEvidence:
    return BindingEvidence(
        "canonical", "state-binding/2", binding_receipt_path(config), "0" * 64,
        state, "1" * 64, "2" * 40, "3" * 40, "4" * 64, "5" * 64, "6" * 64,
    )


def installed_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path]:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    state_source = tmp_path / "state-source"
    apply_init(ROOT, state_source, git_name="Synthetic Installer", git_email=TEST_EMAIL)
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
    git(state, "config", "user.name", "Synthetic Installer")
    git(state, "config", "user.email", TEST_EMAIL)

    config_payload = json.loads((ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    config_payload["backup_root"] = str(tmp_path / "host" / "backups")
    for index, target in enumerate(config_payload["targets"]):
        target["root"] = str(tmp_path / "runtimes" / f"runtime-{index}")
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    config.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    apply_attach(state, config, confirm_private_remote=True)

    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps(build_release_manifest(ROOT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return state, config, manifest, tmp_path / "local-data" / "agent-core"


def test_release_manifest_excludes_bytecode_and_detects_tamper(tmp_path: Path) -> None:
    payload = build_release_manifest(ROOT)
    paths = [item["path"] for item in payload["files"]]
    assert paths == sorted(paths)
    assert not any("__pycache__" in path or path.endswith((".pyc", ".pyo")) for path in paths)
    assert "agent-core" not in paths and "agent-core.cmd" not in paths
    manifest = tmp_path / "release.json"
    tampered = json.loads(json.dumps(payload))
    tampered["files"][0]["sha256"] = "A" * 43
    tampered_digest = hashlib.sha256(json.dumps(
        tampered["files"], sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).digest()
    tampered["artifact_sha256"] = base64.urlsafe_b64encode(
        tampered_digest
    ).decode("ascii").rstrip("=")
    manifest.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_ARTIFACT_HASH"):
        verify_release_manifest(ROOT, manifest)
    incomplete = json.loads(json.dumps(payload))
    incomplete["files"].pop()
    digest = hashlib.sha256(json.dumps(
        incomplete["files"], sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).digest()
    incomplete["artifact_sha256"] = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    manifest.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_ARTIFACT_MANIFEST"):
        verify_release_manifest(ROOT, manifest)


def test_release_attribution_is_complete() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "[yyyy]" not in notice
    assert "[name of copyright owner]" not in notice
    assert notice == "agent-core\nCopyright 2026 Cyber Y\n"
    paths = {item["path"] for item in build_release_manifest(ROOT)["files"]}
    assert {"LICENSE", "NOTICE"}.issubset(paths)


def test_checked_in_release_manifest_matches_runtime_payload() -> None:
    checked_in = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    assert checked_in == build_release_manifest(ROOT)
    assert verify_release_manifest(ROOT, ROOT / "release-manifest.json").artifact_sha256 == (
        checked_in["artifact_sha256"]
    )


def test_release_manifest_describes_what_a_fresh_clone_checks_out() -> None:
    """The manifest must match checkout content, not whatever this tree happens to hold.

    The test above compares the checked-in manifest to `build_release_manifest`,
    and both sides read the working tree, so it can never observe a working-tree
    versus checkout divergence. `.gitattributes` rewrites line endings on
    checkout, so a payload file left with CRLF here is hashed into the manifest
    as CRLF, checked out of a clone as LF, and fails FAIL_ARTIFACT_HASH on every
    public install. Line endings are the only rewrite these attributes declare,
    so the manifest describes checkout content exactly when each payload file is
    already in its declared form. The reference is `.gitattributes`, read
    through `git check-attr`, not a second read of the tree being checked.
    """
    if subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--git-dir"], capture_output=True,
    ).returncode != 0:
        pytest.skip("not a git checkout")

    checked_in = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in checked_in["files"]]
    declared = subprocess.run(
        ["git", "-C", str(ROOT), "check-attr", "eol", "--", *paths],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(declared) == len(paths)

    divergent = []
    for path, line in zip(paths, declared):
        eol = line.rsplit(": ", 1)[1]
        content = (ROOT / path).read_bytes()
        if eol == "lf" and b"\r\n" in content:
            divergent.append(f"{path} holds CRLF but is checked out as LF")
        elif eol == "crlf" and b"\n" in content.replace(b"\r\n", b""):
            divergent.append(f"{path} holds bare LF but is checked out as CRLF")
    assert not divergent, f"manifest does not describe checkout content: {divergent}"


def test_public_install_wrappers_are_thin_and_runtime_independent() -> None:
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for content in (shell, powershell):
        assert "agent_core.cli install" in content
        assert "--source" in content
        assert "npm" not in content and "node" not in content


def test_apply_install_builds_once_and_consumes_same_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = object()
    calls: list[tuple[str, object, bool | None]] = []

    def build(*_args, **_kwargs):
        calls.append(("build", plan, None))
        return plan

    def apply(built, *, force: bool):
        calls.append(("apply", built, force))
        return ["PASS synthetic"]

    monkeypatch.setattr(installer_module, "_build_plan", build)
    monkeypatch.setattr(installer_module, "_apply_install_plan", apply)
    result = apply_install(
        Path("engine"), Path("host.json"), Path("state"), Path("source"), None,
        force=False,
    )
    assert result == ["PASS synthetic"]
    assert calls == [("build", plan, None), ("apply", plan, False)]


@pytest.mark.parametrize("drift", ("config", "lock", "receipt", "origin"))
def test_apply_install_plan_binding_drift_fails_before_preflight_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    plan = installer_module._build_plan(ROOT, config, state, ROOT, manifest)
    managed = [item.path for item in plan.objects] + [item.path for item in plan.hook_bindings]
    before = {
        path: path.read_bytes() if path.is_file() else None
        for path in managed
    }

    local = installer_module.validate_state_binding(
        plan.state_root,
        plan.config_path,
        require_clean_snapshot=False,
        require_remote_observation=False,
        expected_remote_revision=plan.binding.remote_revision,
    )
    assert local == plan.binding
    reached_preflight: list[installer_module.InstallPlan] = []

    def no_changes(candidate, *, force: bool):
        assert force is False
        reached_preflight.append(candidate)
        return None, True, []

    monkeypatch.setattr(installer_module, "_preflight", no_changes)
    assert installer_module._apply_install_plan(plan, force=False) == [
        f"PASS install version={plan.artifact.version} no_changes=true"
    ]
    assert reached_preflight == [plan]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("binding drift crossed the zero-write boundary")

    for name in ("_preflight", "_snapshot", "_atomic_write"):
        monkeypatch.setattr(installer_module, name, forbidden)

    if drift == "config":
        config.write_bytes(config.read_bytes() + b"\n")
    elif drift == "lock":
        lock = state / "agent-core.lock.json"
        lock.write_bytes(lock.read_bytes() + b"\n")
    elif drift == "receipt":
        receipt = binding_receipt_path(config)
        receipt.write_bytes(receipt.read_bytes() + b"\n")
    else:
        git(state, "remote", "set-url", "origin", str(tmp_path / "different-origin.git"))

    with pytest.raises(ConfigError, match="^FAIL_STATE_BINDING "):
        installer_module._apply_install_plan(plan, force=False)
    for path, content in before.items():
        if content is None:
            assert not os.path.lexists(path)
        else:
            assert path.read_bytes() == content
    assert not install_root.exists()
    assert not plan.receipt_path.exists()
    assert not (config.parent / "rollback").exists()


def test_install_plan_is_zero_write_and_apply_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    config_before = config.read_bytes()
    state_head = git(state, "rev-parse", "HEAD").stdout.strip()
    lines = plan_install(ROOT, config, state, ROOT, manifest)
    targets = [line for line in lines if line.startswith("TARGET ")]
    assert targets and all(" status=missing " in line for line in targets)
    assert lines[-1] == "DRY_RUN writes=0 ready=true no_changes=false"
    assert not install_root.exists()
    assert config.read_bytes() == config_before
    assert git(state, "rev-parse", "HEAD").stdout.strip() == state_head

    applied = apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert applied[-1].startswith("PASS artifact_sha256=")
    receipt_path = config.parent / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "install-receipt/1"
    assert receipt["engine_version"] == "0.1.0.dev0"
    assert (install_root / "engine" / receipt["engine_version"] / "agent_core" / "cli.py").is_file()
    assert (install_root / "bin" / "agent-core.cmd").is_file()
    assert (install_root / "bin" / "agent-core").is_file()
    assert (install_root / "engine-pin.json").is_file()
    first_hashes = {
        item["path"]: item["installed_sha256"] for item in receipt["objects"]
    }
    receipt_before = receipt_path.read_bytes()
    environment = os.environ.copy()
    environment["AGENT_CORE_PYTHON"] = os.fspath(Path(os.sys.executable))
    wrapper = ["cmd", "/c", str(install_root / "bin" / "agent-core.cmd")] if os.name == "nt" else [
        str(install_root / "bin" / "agent-core")
    ]

    def launch(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*wrapper, "--state", str(state), *args],
            check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
        )

    launched = launch("--version")
    assert launched.returncode == 0
    assert launched.stdout.strip() == "0.1.0.dev0"
    checked = launch("check", "--all-profiles")
    assert checked.returncode == 2
    assert checked.stderr.strip() == "FAIL_COMMAND_FROZEN check"
    matched = launch("lessons", "match", "--stage", "prompt", "--text", "synthetic verification")
    assert matched.returncode == 0

    workspace = tmp_path / "workspace"
    shadow = workspace / "agent_core"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("\n", encoding="utf-8")
    (shadow / "cli.py").write_text(
        "print('SHADOW CHECKOUT RAN')\nraise SystemExit(42)\n", encoding="utf-8",
    )
    (workspace / "id_rsa").write_text("synthetic private key fixture\n", encoding="utf-8")
    scanned = subprocess.run(
        [*wrapper, "--state", str(state), "privacy", "scan", "--tree", ".", "--strict"],
        cwd=workspace, check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert scanned.returncode == 2
    assert scanned.stderr.strip() == "FAIL_COMMAND_FROZEN privacy"
    shadow_checked = subprocess.run(
        [*wrapper, "--state", str(state), "--version"],
        cwd=workspace, check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert shadow_checked.returncode == 0
    assert shadow_checked.stdout.strip() == "0.1.0.dev0"
    assert "SHADOW CHECKOUT RAN" not in shadow_checked.stdout + shadow_checked.stderr
    duplicate_state = subprocess.run(
        [
            *wrapper, "--state", str(state), "--version",
            "--state", str(tmp_path / "different-state"),
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert duplicate_state.returncode != 0
    assert "FAIL_STATE_ARGUMENT duplicate --state" in duplicate_state.stderr
    assert not list((install_root / "engine" / "0.1.0.dev0").rglob("__pycache__"))
    second_plan = plan_install(ROOT, config, state, ROOT, manifest)
    second_targets = [line for line in second_plan if line.startswith("TARGET ")]
    assert second_targets and all(" status=identical " in line for line in second_targets)
    assert second_plan[-1] == "DRY_RUN writes=0 ready=true no_changes=true"
    second = apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert second == ["PASS install version=0.1.0.dev0 no_changes=true"]
    assert receipt_path.read_bytes() == receipt_before
    after = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert {item["path"]: item["installed_sha256"] for item in after["objects"]} == first_hashes


def test_existing_owned_path_requires_force_and_unowned_file_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    runtime.mkdir(parents=True)
    rules = runtime / payload["targets"][0]["rules_target"]
    rules.write_bytes(b"user-owned-before\n")
    unrelated = runtime / "notes.txt"
    unrelated.write_bytes(b"never managed\n")
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert rules.read_bytes() == b"user-owned-before\n"
    apply_install(ROOT, config, state, ROOT, manifest, force=True)
    assert rules.read_bytes() != b"user-owned-before\n"
    assert unrelated.read_bytes() == b"never managed\n"


def test_plan_reports_all_conflicts_and_apply_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    before: dict[Path, bytes] = {}
    for index, target in enumerate(payload["targets"][:2]):
        path = Path(target["root"]) / target["rules_target"]
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"user-owned-{index}\n".encode()
        path.write_bytes(content)
        before[path] = content

    lines = plan_install(ROOT, config, state, ROOT, manifest)
    conflicts = [line for line in lines if " status=conflict " in line]
    assert len(conflicts) >= 2
    assert all(any(str(path) in line for line in conflicts) for path in before)
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    assert not install_root.exists()
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "rollback").exists()

    with pytest.raises(ConfigError, match="^INSTALL_CONFLICT install plan ready=false$"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert {path: path.read_bytes() for path in before} == before
    assert not install_root.exists()
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "rollback").exists()


def test_receipt_owned_old_bytes_conflict_with_new_desired_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    target = install_root / "managed.txt"
    target.parent.mkdir()
    old_content = b"old-owned-bytes\n"
    new_content = b"new-desired-bytes\n"
    target.write_bytes(old_content)
    receipt_path = tmp_path / "host" / "install-receipt.json"
    receipt_path.parent.mkdir()
    artifact = installer_module.Artifact("synthetic", "A" * 43, (), b"{}")
    receipt = {
        "schema": "install-receipt/1",
        "engine_version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "config_sha256": "0" * 64,
        "state_lock_sha256": "1" * 64,
        "snapshot_path": str(tmp_path / "old-snapshot"),
        "objects": [{
            "label": "managed",
            "path": str(target),
            "root": str(install_root),
            "kind": "file",
            "before_exists": False,
            "before_sha256": None,
            "installed_sha256": hashlib.sha256(old_content).hexdigest(),
            "snapshot_rel": "objects/0",
        }],
        "hook_bindings": [],
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_before = receipt_path.read_bytes()
    plan = installer_module.InstallPlan(
        tmp_path / "host" / "host.json", {}, tmp_path / "state", tmp_path / "source",
        artifact, install_root, install_root / "engine" / artifact.version, receipt_path,
        (installer_module.ManagedObject(
            "managed", target, install_root, "file",
            hashlib.sha256(new_content).hexdigest(), new_content,
        ),),
        (),
        synthetic_binding(tmp_path / "host" / "host.json", tmp_path / "state"),
    )
    monkeypatch.setattr(installer_module, "_build_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        installer_module, "validate_state_binding", lambda *_args, **_kwargs: plan.binding,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("conflict crossed the preflight boundary")

    monkeypatch.setattr(installer_module, "require_fresh", forbidden)
    monkeypatch.setattr(installer_module, "_snapshot", forbidden)
    lines = plan_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None)
    assert any(line.startswith("TARGET managed status=conflict ") for line in lines)
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="^INSTALL_CONFLICT install plan ready=false$"):
        apply_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None, force=False)
    assert target.read_bytes() == old_content
    assert receipt_path.read_bytes() == receipt_before
    assert not (receipt_path.parent / "rollback").exists()


def test_runtime_config_directory_collision_is_conflict_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    runtime_root = tmp_path / "runtime"
    settings = runtime_root / "settings.json"
    settings.mkdir(parents=True)
    marker = settings / "user-owned.txt"
    marker.write_bytes(b"preserve-directory\n")
    identity = (settings.stat().st_dev, settings.stat().st_ino)
    receipt_path = tmp_path / "host" / "install-receipt.json"
    artifact = installer_module.Artifact("synthetic", "A" * 43, (), b"{}")
    desired = {
        event: [{"hooks": [{"type": "command", "command": event}]}]
        for event in ("UserPromptSubmit", "PreToolUse", "Stop")
    }
    plan = installer_module.InstallPlan(
        tmp_path / "host" / "host.json", {}, tmp_path / "state", tmp_path / "source",
        artifact, install_root, install_root / "engine" / artifact.version, receipt_path,
        (),
        (installer_module.RuntimeBinding(
            "claude-code", "claude-code", settings, runtime_root, desired,
        ),),
        synthetic_binding(tmp_path / "host" / "host.json", tmp_path / "state"),
    )
    monkeypatch.setattr(installer_module, "_build_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        installer_module, "validate_state_binding", lambda *_args, **_kwargs: plan.binding,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("collision crossed the preflight boundary")

    monkeypatch.setattr(installer_module, "require_fresh", forbidden)
    monkeypatch.setattr(installer_module, "_snapshot", forbidden)
    lines = plan_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None)
    assert any(
        line.startswith("TARGET runtime-config:claude-code status=conflict ")
        for line in lines
    )
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="^INSTALL_CONFLICT install plan ready=false$"):
        apply_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None, force=False)
    assert settings.is_dir()
    assert (settings.stat().st_dev, settings.stat().st_ino) == identity
    assert marker.read_bytes() == b"preserve-directory\n"
    assert not receipt_path.exists()
    assert not (receipt_path.parent / "rollback").exists()
    assert not install_root.exists()


def test_install_verify_failure_rolls_back_every_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    runtime.mkdir(parents=True)
    settings = runtime / "settings.json"
    settings_before = b'{"theme":"pre-install"}\n'
    settings.write_bytes(settings_before)
    missing = runtime / payload["targets"][0]["rules_target"]
    captured: dict[str, object] = {}
    original_snapshot = installer_module._snapshot

    def snapshot_probe(*args, **kwargs):
        result = original_snapshot(*args, **kwargs)
        snapshot, objects, hooks = result
        missing_record = next(item for item in objects if Path(item["path"]) == missing)
        hook_record = next(item for item in hooks if Path(item["path"]) == settings)
        captured["missing_before_exists"] = missing_record["before_exists"]
        captured["hook_before_exists"] = hook_record["before_exists"]
        captured["hook_backup"] = (snapshot / hook_record["snapshot_rel"]).read_bytes()
        return result

    planned = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(
        line.startswith("TARGET runtime-config:claude-code status=missing ")
        for line in planned
    )
    assert not missing.exists()

    def fail_verify(*_args, **_kwargs) -> None:
        raise ConfigError("INJECTED_VERIFY_FAILURE", "synthetic")

    monkeypatch.setattr(installer_module, "_snapshot", snapshot_probe)
    monkeypatch.setattr("agent_core.installer._verify_installed", fail_verify)
    with pytest.raises(ConfigError, match="INJECTED_VERIFY_FAILURE"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert captured == {
        "missing_before_exists": False,
        "hook_before_exists": True,
        "hook_backup": settings_before,
    }
    assert settings.read_bytes() == settings_before
    assert not missing.exists()
    assert not install_root.exists()
    assert not (config.parent / "install-receipt.json").exists()
    rollback = config.parent / "rollback"
    assert not rollback.exists() or list(rollback.iterdir()) == []


def test_receipt_write_failure_rolls_back_pin_and_all_installed_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    from agent_core import installer as installer_module

    original = installer_module._atomic_write

    def fail_receipt(path: Path, content: bytes, *, executable: bool = False) -> None:
        if path.name == "install-receipt.json":
            raise OSError("INJECTED_RECEIPT_FAILURE")
        original(path, content, executable=executable)

    monkeypatch.setattr(installer_module, "_atomic_write", fail_receipt)
    with pytest.raises(ConfigError, match="FAIL_INSTALL"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert not (install_root / "engine-pin.json").exists()
    assert not (install_root / "engine" / "0.1.0.dev0").exists()
    assert not (config.parent / "install-receipt.json").exists()


def test_existing_version_directory_is_immutable_even_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    version_root = install_root / "engine" / "0.1.0.dev0"
    version_root.mkdir(parents=True)
    (version_root / "foreign.txt").write_text("different artifact", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_IMMUTABLE_ARTIFACT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=True)
    assert (version_root / "foreign.txt").read_text(encoding="utf-8") == "different artifact"


def test_runtime_symlink_escape_is_rejected_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = runtime / "hooks"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False, capture_output=True, text=True,
        )
        if created.returncode != 0:
            pytest.skip("directory junction creation unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="FAIL_PATH"):
        plan_install(ROOT, config, state, ROOT, manifest)
    assert not install_root.exists()
    assert list(outside.iterdir()) == []


def test_installer_path_guard_independently_rejects_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "declared-root"
    root.mkdir()
    outside = tmp_path / "outside-root"
    outside.mkdir()
    link = root / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False, capture_output=True, text=True,
        )
        if created.returncode != 0:
            pytest.skip("directory junction creation unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="FAIL_PATH_ESCAPE"):
        _assert_within(link / "managed.txt", root)


def test_state_manifest_requires_explicit_trust_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, _manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    manifest_path = state / "manifest.yaml"
    payload = {
        "schema": "capability-manifest/1",
        "capabilities": [{
            "id": "skill:owner-check", "kind": "skill", "source": "skills/owner-check",
            "requirement": "optional", "runtimes": ["codex"], "trusted": False,
        }],
    }
    (state / "skills" / "owner-check").mkdir(parents=True)
    (state / "skills" / "owner-check" / "SKILL.md").write_text("synthetic", encoding="utf-8")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    git(state, "add", ".")
    git(state, "commit", "-q", "-m", "untrusted capability")
    git(state, "push", "-q")
    release = tmp_path / "release-manifest-refreshed.json"
    release.write_text(json.dumps(build_release_manifest(ROOT)), encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_UNTRUSTED_CAPABILITY"):
        plan_install(ROOT, config, state, ROOT, release)
    assert not install_root.exists()


def test_public_install_force_is_rejected_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    from agent_core.cli import main as cli_main

    called = False

    def forbidden_apply(*_args, **_kwargs) -> list[str]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(installer_module, "apply_install", forbidden_apply)
    with pytest.raises(SystemExit) as exc_info:
        cli_main([
            "install", "--config", str(config), "--source", str(ROOT),
            "--artifact-manifest", str(manifest), "--apply", "--force",
        ])
    assert exc_info.value.code == 2
    assert called is False
    assert "unrecognized arguments: --force" in capsys.readouterr().err
