from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import agent_core.launcher as launcher_module
from agent_core import installer as installer_module
from agent_core.config import ConfigError
from agent_core.doctor import run as run_doctor
from agent_core.installer import (
    _assert_within,
    apply_install,
    build_release_manifest,
    plan_install,
    verify_release_manifest,
)
from agent_core.promote import operation_lock
from agent_core.state import BindingEvidence, apply_attach, apply_init, binding_receipt_path
from agent_core.sync import execute as execute_sync


ROOT = Path(__file__).resolve().parents[1]
TEST_EMAIL = "installer" + chr(64) + "invalid"


@pytest.mark.parametrize("command", ["match", "hook"])
def test_launcher_defaults_to_bound_ledger_without_all_profiles(
    command: str, tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    expected = [
        "lessons", command, "--ledger", str(state / "experience" / "LESSONS.md"),
        "--stage", "prompt",
    ]

    assert launcher_module._inject_state(
        ["lessons", command, "--stage", "prompt"], state,
    ) == expected
    assert launcher_module._inject_state(
        ["lessons", command, "--all-profiles", "--stage", "prompt"], state,
    ) == [
        "lessons", command, "--ledger", str(state / "experience" / "LESSONS.md"),
        "--all-profiles", "--stage", "prompt",
    ]
    explicit = ["lessons", command, "--ledger", "custom.md", "--stage", "prompt"]
    assert launcher_module._inject_state(explicit, state) == explicit


def test_launcher_rejects_unsupported_python_before_reading_install_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(launcher_module.sys, "version_info", (3, 10))
    assert launcher_module.run([], launcher_path=tmp_path / "bin" / "launcher.py") == 1
    assert "FAIL_PYTHON_VERSION requires Python >=3.11" in capsys.readouterr().err


def _posix_shell() -> Path:
    if os.name != "nt":
        shell = shutil.which("sh")
    else:
        shell = os.fspath(Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe")
    if not shell or not Path(shell).is_file():
        pytest.fail("POSIX wrapper contract requires an executable POSIX shell")
    return Path(shell)


def _posix_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return os.fspath(resolved)
    drive = resolved.drive.rstrip(":").lower()
    if len(drive) != 1:
        pytest.fail("POSIX wrapper contract requires a drive-backed test path")
    return f"/{drive}/{resolved.as_posix()[3:]}"


def _write_posix_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o111)
        return
    shell = _posix_shell()
    marked = subprocess.run(
        [str(shell), "-c", '/usr/bin/chmod +x "$1"', "ignored", _posix_path(path)],
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert marked.returncode == 0, marked.stderr


def _generated_posix_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    wrapper = tmp_path / "agent-core"
    wrapper.write_bytes(installer_module._wrapper_content()[0])
    _write_posix_executable(wrapper, wrapper.read_text(encoding="utf-8"))
    (tmp_path / "agent_core_launcher.py").write_text("# synthetic launcher\n", encoding="utf-8")
    path_bin = tmp_path / "path"
    path_bin.mkdir()
    _write_posix_executable(
        path_bin / "dirname",
        "#!/bin/sh\nif [ \"$1\" = \"--\" ]; then shift; fi\n"
        "value=$1\nprintf '%s\\n' \"${value%/*}\"\n",
    )
    return wrapper, path_bin


@pytest.mark.skipif(installer_module.sys.platform != "darwin", reason="Darwin renamex_np contract")
def test_darwin_move_no_replace_places_directory_exclusively(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "proof.txt").write_text("proof\n", encoding="utf-8")

    installer_module._move_no_replace(source, destination)

    assert not source.exists()
    assert (destination / "proof.txt").read_text(encoding="utf-8") == "proof\n"


@pytest.mark.skipif(installer_module.sys.platform != "darwin", reason="Darwin renamex_np contract")
def test_darwin_move_no_replace_preserves_directory_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "source.txt").write_text("source\n", encoding="utf-8")
    (destination / "racer.txt").write_text("racer\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="destination was recreated"):
        installer_module._darwin_move_no_replace(source, destination)

    assert (source / "source.txt").read_text(encoding="utf-8") == "source\n"
    assert (destination / "racer.txt").read_text(encoding="utf-8") == "racer\n"


@pytest.mark.skipif(installer_module.sys.platform != "darwin", reason="Darwin renamex_np contract")
def test_darwin_move_no_replace_maps_other_errno_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "missing"
    destination = tmp_path / "destination"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exclusive rename failure used an overwrite-capable fallback")

    monkeypatch.setattr(installer_module.os, "rename", forbidden)
    monkeypatch.setattr(installer_module.os, "replace", forbidden)
    with pytest.raises(ConfigError, match="no-replace placement failed"):
        installer_module._move_no_replace(source, destination)
    assert not destination.exists()


def _run_generated_posix_wrapper(
    wrapper: Path, path_bin: Path, *, owner_override: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if owner_override is None:
        environment.pop("AGENT_CORE_PYTHON", None)
    else:
        environment["AGENT_CORE_PYTHON"] = _posix_path(owner_override)
    return subprocess.run(
        [
            str(_posix_shell()), "-c",
            'PATH="$1"; export PATH; exec "$3" "$2" --version',
            "ignored", _posix_path(path_bin), _posix_path(wrapper), _posix_path(_posix_shell()),
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
    )


def _run_public_posix_installer(path_bin: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("AGENT_CORE_PYTHON", None)
    return subprocess.run(
        [
            str(_posix_shell()), "-c",
            'PATH="$1:/usr/bin"; export PATH; exec "$3" "$2" --dry-run',
            "ignored", _posix_path(path_bin), _posix_path(ROOT / "install.sh"),
            _posix_path(_posix_shell()),
        ],
        check=False, capture_output=True, text=True, encoding="utf-8", env=environment,
    )


def _write_python_stub(
    path: Path, version: str, *, qualified: bool, runtime_exit: int = 0,
) -> None:
    _write_posix_executable(
        path,
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"-c\" ]; then printf '%s\\n' '{version}'; exit "
        f"{0 if qualified else 1}; fi\n"
        f"printf '%s\\n' 'selected={path.name} version={version}' >&2\n"
        f"exit {runtime_exit}\n",
    )


def test_generated_posix_wrapper_reports_unusable_path_python(tmp_path: Path) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)
    _write_posix_executable(path_bin / "python3", "#!/bin/sh\nexit 1\n")

    result = _run_generated_posix_wrapper(wrapper, path_bin)

    assert result.returncode == 2
    assert "requires Python >=3.11" in result.stderr
    assert "python3=unavailable" in result.stderr


def test_generated_posix_wrapper_rejects_when_path_has_no_python(tmp_path: Path) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)

    result = _run_generated_posix_wrapper(wrapper, path_bin)

    assert result.returncode == 2
    assert "requires Python >=3.11" in result.stderr
    assert "none found (python3.13 python3.12 python3.11 python3 python)" in result.stderr


def test_generated_posix_wrapper_uses_verified_path_python_and_propagates_exit(
    tmp_path: Path,
) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)
    _write_python_stub(path_bin / "python3", "3.11.9", qualified=True, runtime_exit=7)

    result = _run_generated_posix_wrapper(wrapper, path_bin)

    assert result.returncode == 7
    assert "selected=python3 version=3.11.9" in result.stderr


def test_generated_posix_wrapper_skips_low_python_and_uses_qualified_fallback(
    tmp_path: Path,
) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)
    _write_python_stub(path_bin / "python3", "3.9.6", qualified=False)
    _write_python_stub(path_bin / "python", "3.11.9", qualified=True, runtime_exit=7)

    result = _run_generated_posix_wrapper(wrapper, path_bin)

    assert result.returncode == 7
    assert "selected=python version=3.11.9" in result.stderr
    assert "selected=python3" not in result.stderr


def test_generated_posix_wrapper_reports_all_low_versions_and_floor(tmp_path: Path) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)
    _write_python_stub(path_bin / "python3", "3.9.6", qualified=False)
    _write_python_stub(path_bin / "python", "3.10.14", qualified=False)

    result = _run_generated_posix_wrapper(wrapper, path_bin)

    assert result.returncode == 2
    assert "requires Python >=3.11" in result.stderr
    assert "python3=3.9.6" in result.stderr
    assert "python=3.10.14" in result.stderr


def test_public_posix_installer_skips_low_python_and_uses_qualified_fallback(
    tmp_path: Path,
) -> None:
    path_bin = tmp_path / "path"
    path_bin.mkdir()
    _write_python_stub(path_bin / "python3", "3.9.6", qualified=False)
    _write_python_stub(path_bin / "python", "3.11.9", qualified=True, runtime_exit=7)

    result = _run_public_posix_installer(path_bin)

    assert result.returncode == 7
    assert "selected=python version=3.11.9" in result.stderr


def test_public_posix_installer_reports_all_low_versions_and_floor(tmp_path: Path) -> None:
    path_bin = tmp_path / "path"
    path_bin.mkdir()
    _write_python_stub(path_bin / "python3", "3.9.6", qualified=False)
    _write_python_stub(path_bin / "python", "3.10.14", qualified=False)

    result = _run_public_posix_installer(path_bin)

    assert result.returncode == 2
    assert "requires Python >=3.11" in result.stderr
    assert "python3=3.9.6" in result.stderr
    assert "python=3.10.14" in result.stderr


def test_generated_posix_wrapper_keeps_owner_python_override_direct(tmp_path: Path) -> None:
    wrapper, path_bin = _generated_posix_wrapper(tmp_path)
    _write_posix_executable(
        path_bin / "python3",
        "#!/bin/sh\nprintf '%s\\n' 'path-fallback-ran' >&2\nexit 0\n",
    )
    owner = tmp_path / "owner-python"
    _write_posix_executable(owner, "#!/bin/sh\nprintf '%s\\n' 'owner-override-ran' >&2\nexit 9\n")

    result = _run_generated_posix_wrapper(wrapper, path_bin, owner_override=owner)

    assert result.returncode == 9
    assert "owner-override-ran" in result.stderr
    assert "path-fallback-ran" not in result.stderr


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, default_config: bool = False,
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
    config = (
        tmp_path / "home" / ".agent-core" / "host.json"
        if default_config else tmp_path / "host" / "host.json"
    )
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    apply_attach(state, config, confirm_private_remote=True)

    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(
        json.dumps(build_release_manifest(ROOT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    install_root = (
        tmp_path / "local-data" / "agent-core"
        if os.name == "nt" else tmp_path / "xdg-data" / "agent-core"
    )
    return state, config, manifest, install_root


def canonical_installed_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path, Path]:
    """Create a private canonical clone with a valid v2 binding and install receipt."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    source = tmp_path / "canonical-source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    git(source, "config", "core.autocrlf", "false")
    git(source, "config", "user.name", "Synthetic Canonical Install")
    git(source, "config", "user.email", TEST_EMAIL)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(ROOT, source / "engine", ignore=ignored)
    shutil.copytree(ROOT.parent / "state", source / "state", ignore=ignored)
    # This synthetic repository has no root attributes file; retain raw fixture bytes consistently.
    (source / "engine" / ".gitattributes").unlink()
    git(source, "add", "engine", "state")
    git(source, "checkout-index", "-f", "-a")
    payload = build_release_manifest(source / "engine")
    (source / "engine" / "release-manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    git(source, "add", "engine", "state")
    staged = git(source, "write-tree").stdout.strip()
    record = {
        "schema": "engine-provenance/1",
        "sequence": 1,
        "previous_record_sha256": None,
        "engine_tree_oid": git(source, "rev-parse", f"{staged}:engine").stdout.strip(),
        "release_artifact_sha256": payload["artifact_sha256"],
    }
    (source / "engine.provenance.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    git(source, "add", "engine.provenance.json")
    git(source, "commit", "-q", "-m", "canonical fixture")
    remote = tmp_path / "canonical.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    clone = tmp_path / "canonical-clone"
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(remote), str(clone)], check=True)
    git(clone, "config", "core.autocrlf", "false")
    git(clone, "config", "core.eol", "lf")
    git(clone, "checkout-index", "-f", "-a")
    git(clone, "config", "user.name", "Synthetic Canonical Install")
    git(clone, "config", "user.email", TEST_EMAIL)
    config_payload = json.loads((ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
    config_payload["backup_root"] = str(tmp_path / "host" / "backups")
    for index, target in enumerate(config_payload["targets"]):
        target["root"] = str(tmp_path / "runtimes" / f"runtime-{index}")
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    config.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    apply_attach(clone / "state", config, confirm_private_remote=True)
    manifest = clone / "engine" / "release-manifest.json"
    install_root = (
        tmp_path / "local-data" / "agent-core"
        if os.name == "nt" else tmp_path / "xdg-data" / "agent-core"
    )
    apply_install(clone / "engine", config, clone / "state", clone / "engine", manifest, force=False)
    return source, clone, config, manifest, install_root


def advance_canonical_engine(source: Path, clone: Path) -> None:
    """Advance only the engine subtree and its chained provenance record."""
    notice = source / "engine" / "NOTICE"
    notice.write_text("agent-core\nCopyright 2026 Synthetic Canonical Update\n", encoding="utf-8")
    git(source, "add", "engine/NOTICE")
    git(source, "checkout-index", "-f", "--", "engine/NOTICE")
    payload = build_release_manifest(source / "engine")
    (source / "engine" / "release-manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    previous = (source / "engine.provenance.json").read_bytes()
    git(source, "add", "engine")
    staged = git(source, "write-tree").stdout.strip()
    record = {
        "schema": "engine-provenance/1",
        "sequence": 2,
        "previous_record_sha256": hashlib.sha256(previous).hexdigest(),
        "engine_tree_oid": git(source, "rev-parse", f"{staged}:engine").stdout.strip(),
        "release_artifact_sha256": payload["artifact_sha256"],
    }
    (source / "engine.provenance.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    git(source, "add", "engine.provenance.json")
    git(source, "commit", "-q", "-m", "advance engine provenance")
    git(source, "push", "-q", "origin", "main")
    git(clone, "fetch", "origin")
    git(clone, "merge", "--ff-only", "origin/main")


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
    floor = installer_module._python_floor(ROOT)
    assert floor == launcher_module._python_floor(ROOT / "pyproject.toml")
    assert f"{floor[0]}.{floor[1]}" == "3.11"
    assert "requires-python" in shell
    assert "python3.13 python3.12 python3.11 python3 python" in shell


def test_public_posix_installer_is_git_executable() -> None:
    if subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--git-dir"], capture_output=True,
    ).returncode != 0:
        pytest.skip("not a git checkout")
    staged = subprocess.run(
        [
            "git", "-c", f"safe.directory={ROOT.resolve().as_posix()}",
            "-C", str(ROOT), "ls-files", "--stage", "--", "install.sh",
        ],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.split()
    assert staged and staged[0] == "100755"
    if os.name != "nt":
        assert os.access(ROOT / "install.sh", os.X_OK)


def test_apply_install_builds_once_and_consumes_same_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = object()
    calls: list[tuple[str, object, bool | None]] = []

    def build(*_args, **_kwargs):
        calls.append(("build", plan, None))
        return plan

    def apply(built, *, force: bool, lock_token: object):
        assert lock_token is not None
        calls.append(("apply", built, force))
        return ["PASS synthetic"]

    monkeypatch.setattr(installer_module, "_build_plan", build)
    monkeypatch.setattr(
        installer_module, "_reviewed_install_plan",
        lambda candidate: (candidate, None, False, [], [], []),
    )
    monkeypatch.setattr(installer_module, "_apply_install_plan", apply)
    config = tmp_path / "host" / "host.json"
    config.parent.mkdir()
    result = apply_install(
        Path("engine"), config, Path("state"), Path("source"), None,
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
    plan, *_review = installer_module._reviewed_install_plan(plan)
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

    def no_changes(candidate):
        reached_preflight.append(candidate)
        return candidate, None, True, []

    monkeypatch.setattr(installer_module, "_assert_reviewed_plan_current", no_changes)
    assert installer_module._apply_install_plan(plan, force=False) == [
        f"PASS install version={plan.artifact.version} no_changes=true"
    ]
    assert reached_preflight == [plan, plan]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("binding drift crossed the zero-write boundary")

    for name in ("_assert_reviewed_plan_current", "_snapshot", "_atomic_write"):
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


def test_apply_install_plan_artifact_drift_fails_before_preflight_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    plan = installer_module._build_plan(ROOT, config, state, ROOT, manifest)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["artifact_sha256"] = "A" * 43
    manifest.write_text(json.dumps(manifest_payload, sort_keys=True) + "\n", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("artifact drift crossed the zero-write boundary")

    monkeypatch.setattr(installer_module, "_preflight", forbidden)
    monkeypatch.setattr(installer_module, "_snapshot", forbidden)
    with pytest.raises(ConfigError, match="^FAIL_ARTIFACT_HASH"):
        installer_module._apply_install_plan(plan, force=False)
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
    assert targets and all(" status=absent " in line for line in targets)
    assert lines[-1] == "DRY_RUN writes=0 ready=true no_changes=false"
    assert not install_root.exists()
    assert config.read_bytes() == config_before
    assert git(state, "rev-parse", "HEAD").stdout.strip() == state_head

    applied = apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert applied[-1].startswith("PASS artifact_sha256=")
    receipt_path = config.parent / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "install-receipt/1"
    assert receipt["engine_version"] == "0.1.0.dev9"
    assert (install_root / "engine" / receipt["engine_version"] / "agent_core" / "cli.py").is_file()

    assert (install_root / "bin" / "agent-core.cmd").is_file()
    assert (install_root / "bin" / "agent-core").is_file()
    assert (install_root / "engine" / "0.1.0.dev9" / "pyproject.toml").is_file()
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
    assert launched.stdout.strip() == "0.1.0.dev9"
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
    assert shadow_checked.stdout.strip() == "0.1.0.dev9"
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
    assert not list((install_root / "engine" / "0.1.0.dev9").rglob("__pycache__"))
    second_plan = plan_install(ROOT, config, state, ROOT, manifest)
    second_targets = [line for line in second_plan if line.startswith("TARGET ")]
    assert second_targets
    assert all(
        (
            " status=receipt-owned-identical " in line
            if line.startswith("TARGET runtime:")
            else " status=identical " in line
        )
        for line in second_targets
    )
    assert second_plan[-1] == "DRY_RUN writes=0 ready=true no_changes=true"
    second = apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert second == ["PASS install version=0.1.0.dev9 no_changes=true"]
    assert receipt_path.read_bytes() == receipt_before
    after = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert {item["path"]: item["installed_sha256"] for item in after["objects"]} == first_hashes


def test_install_noop_republishes_the_shared_remote_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    baseline = config.parent / "txn" / "remote-state.json"
    baseline.unlink()
    assert not baseline.exists()

    assert apply_install(ROOT, config, state, ROOT, manifest, force=False) == [
        "PASS install version=0.1.0.dev9 no_changes=true"
    ]
    observed = git(state, "rev-parse", "origin/main").stdout.strip()
    assert json.loads(baseline.read_text(encoding="utf-8")) == {
        "last_known_good": observed,
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode contract")
def test_install_mode_only_upgrade_is_planned_and_preserves_wrapper_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    wrapper = install_root / "bin" / "agent-core"
    before = wrapper.read_bytes()
    wrapper.chmod(0o644)

    planned = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(
        line.startswith("TARGET wrapper-posix status=mode-drift action=MODE ")
        for line in planned
    )
    assert planned[-1] == "DRY_RUN writes=0 ready=true no_changes=false"

    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert wrapper.read_bytes() == before
    assert os.access(wrapper, os.X_OK)
    assert plan_install(ROOT, config, state, ROOT, manifest)[-1] == (
        "DRY_RUN writes=0 ready=true no_changes=true"
    )


def test_install_plan_reports_and_hashes_default_legacy_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    current.unlink()
    legacy = config.parent / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    legacy.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    before = legacy.read_bytes()

    first = plan_install(ROOT, config, state, ROOT, manifest)
    first_token = next(line.removeprefix("PLAN_HASH ") for line in first if line.startswith("PLAN_HASH "))
    assert any(
        line.startswith("REMOTE_BASELINE role=legacy status=valid action=validate-legacy-and-publish ")
        and f"path={legacy}" in line
        for line in first
    )
    assert any(
        line.startswith("REMOTE_BASELINE role=current status=absent action=publish-reviewed-remote ")
        and f"path={current}" in line
        for line in first
    )
    assert not current.exists()
    assert legacy.read_bytes() == before

    legacy.write_text(json.dumps({"last_known_good": known}, indent=2) + "\n", encoding="utf-8")
    second = plan_install(ROOT, config, state, ROOT, manifest)
    second_token = next(line.removeprefix("PLAN_HASH ") for line in second if line.startswith("PLAN_HASH "))
    assert second_token != first_token
    assert not current.exists()


def test_install_plan_hash_binds_current_remote_baseline_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    current = config.parent / "txn" / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()

    first = plan_install(ROOT, config, state, ROOT, manifest)
    first_token = next(line.removeprefix("PLAN_HASH ") for line in first if line.startswith("PLAN_HASH "))
    assert any(
        line.startswith("REMOTE_BASELINE role=current status=valid action=use-current ")
        and f"path={current}" in line
        for line in first
    )

    current.write_text(f'{{ "last_known_good" : "{known}" }}\n', encoding="utf-8")
    second = plan_install(ROOT, config, state, ROOT, manifest)
    second_token = next(line.removeprefix("PLAN_HASH ") for line in second if line.startswith("PLAN_HASH "))
    assert second_token != first_token


@pytest.mark.parametrize("source", ("legacy", "current"))
def test_stale_remote_baseline_blocks_install_plan_without_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    state, config, manifest, install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    legacy = config.parent / "remote-state.json"
    current.unlink()
    selected = legacy if source == "legacy" else current
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(json.dumps({"last_known_good": "0" * 40}) + "\n", encoding="utf-8")
    before = selected.read_bytes()

    lines = plan_install(ROOT, config, state, ROOT, manifest)

    assert any(
        line.startswith(f"REMOTE_BASELINE role={source} status=remote-rewind action=block ")
        for line in lines
    )
    assert not any(line.startswith("PLAN_HASH ") for line in lines)
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    assert selected.read_bytes() == before
    if source == "legacy":
        assert not current.exists()
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


def test_install_apply_rejects_legacy_drift_before_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    current.unlink()
    legacy = config.parent / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    legacy.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    legacy.write_text(json.dumps({"last_known_good": known}, indent=2) + "\n", encoding="utf-8")
    drift = legacy.read_bytes()

    def forbidden_apply(*_args, **_kwargs) -> list[str]:
        raise AssertionError("install plan reached apply after reviewed baseline drift")

    monkeypatch.setattr(installer_module, "_apply_install_plan", forbidden_apply)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert legacy.read_bytes() == drift
    assert not current.exists()
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


def test_install_freshness_failure_leaves_legacy_and_current_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    current.unlink()
    legacy = config.parent / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    legacy.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    legacy_before = legacy.read_bytes()
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))

    def offline(*_args, **_kwargs):
        raise ConfigError("REMOTE_REQUIRED", "synthetic offline boundary")

    def forbidden_publish(*_args, **_kwargs) -> None:
        raise AssertionError("remote baseline published before freshness passed")

    monkeypatch.setattr(installer_module, "require_fresh", offline)
    monkeypatch.setattr(installer_module, "_publish_reviewed_remote_baseline", forbidden_publish)
    with pytest.raises(ConfigError, match="REMOTE_REQUIRED"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert legacy.read_bytes() == legacy_before
    assert not current.exists()
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


@pytest.mark.parametrize("source", ("legacy", "current"))
@pytest.mark.parametrize("mutation", ("create", "delete", "bytes", "type"))
def test_install_apply_rejects_every_reviewed_baseline_preimage_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, mutation: str,
) -> None:
    state, config, manifest, install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    legacy = config.parent / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    selected = legacy if source == "legacy" else current
    if source == "legacy":
        current.unlink()
    if mutation == "create":
        selected.unlink(missing_ok=True)
    elif source == "legacy":
        selected.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")

    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    if mutation == "create":
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text(json.dumps({"last_known_good": known}) + "\n", encoding="utf-8")
    elif mutation == "delete":
        selected.unlink()
    elif mutation == "bytes":
        selected.write_text(f'{{ "last_known_good" : "{known}" }}\n', encoding="utf-8")
    else:
        selected.unlink()
        selected.mkdir()

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


@pytest.mark.parametrize("source", ("legacy", "current"))
@pytest.mark.parametrize("invalid", ("corrupt", "symlink", "multilink"))
def test_install_plan_rejects_invalid_remote_baseline_objects_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, invalid: str,
) -> None:
    state, config, manifest, install_root = installed_fixture(
        tmp_path, monkeypatch, default_config=True,
    )
    current = config.parent / "txn" / "remote-state.json"
    legacy = config.parent / "remote-state.json"
    target = legacy if source == "legacy" else current
    if source == "legacy":
        current.unlink()
    target.unlink(missing_ok=True)
    if invalid == "corrupt":
        target.write_text("corrupt\n", encoding="utf-8")
    elif invalid == "symlink":
        outside = tmp_path / "outside-baseline.json"
        outside.write_text(json.dumps({"last_known_good": "0" * 40}) + "\n", encoding="utf-8")
        target.symlink_to(outside)
    else:
        target.write_text(json.dumps({"last_known_good": "0" * 40}) + "\n", encoding="utf-8")
        alias = tmp_path / f"{source}-baseline-alias.json"
        alias.hardlink_to(target)

    with pytest.raises(ConfigError, match="FAIL_REMOTE_STATE"):
        plan_install(ROOT, config, state, ROOT, manifest)
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


def test_install_apply_normalizes_baseline_type_race_after_freshness_to_plan_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    current = config.parent / "txn" / "remote-state.json"
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    original_freshness = installer_module.require_fresh

    def race(*args, **kwargs):
        result = original_freshness(*args, **kwargs)
        current.unlink()
        current.mkdir()
        return result

    monkeypatch.setattr(installer_module, "require_fresh", race)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert current.is_dir()
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


@pytest.mark.parametrize("before_exists", (False, True))
def test_install_baseline_publication_preserves_final_window_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_exists: bool,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    current = config.parent / "txn" / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    if before_exists:
        current.write_text(json.dumps({"last_known_good": known}, indent=2) + "\n", encoding="utf-8")
    else:
        current.unlink()
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    racer = f'{{ "last_known_good" : "{known}" }}\n'.encode()
    original_publish = installer_module._publish_reviewed_remote_baseline

    def race(path: Path, *args, **kwargs) -> str:
        path.write_bytes(racer)
        return original_publish(path, *args, **kwargs)

    monkeypatch.setattr(installer_module, "_publish_reviewed_remote_baseline", race)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert current.read_bytes() == racer
    assert (config.parent / "install-pending.json").is_file()
    assert list((config.parent / "rollback").glob("install-*"))
    assert not install_root.exists()


@pytest.mark.parametrize("before_exists", (False, True))
def test_install_baseline_rollback_preserves_postpublication_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_exists: bool,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    current = config.parent / "txn" / "remote-state.json"
    known = git(state, "rev-parse", "origin/main").stdout.strip()
    if before_exists:
        current.write_text(json.dumps({"last_known_good": known}, indent=2) + "\n", encoding="utf-8")
    else:
        current.unlink()
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    racer = f'{{ "last_known_good" : "{known}" }}\n'.encode()

    def fail_after_publication(_plan, _snapshot) -> None:
        current.write_bytes(racer)
        raise ConfigError("FAIL_INSTALL_VERIFY", "synthetic postpublication failure")

    monkeypatch.setattr(installer_module, "_install_engine", fail_after_publication)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert current.read_bytes() == racer
    assert (config.parent / "install-pending.json").is_file()
    assert list((config.parent / "rollback").glob("install-*"))
    assert not install_root.exists()


def test_already_locked_boolean_is_not_accepted_as_lock_ownership(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="FAIL_LOCK_TOKEN"):
        apply_install(
            tmp_path / "engine", tmp_path / "host" / "host.json", tmp_path / "state",
            tmp_path / "source", None, force=False, already_locked=True,
        )


def test_sync_held_lock_blocks_install_before_snapshot_or_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    with operation_lock(config.parent / "txn"):
        with pytest.raises(ConfigError, match="FAIL_LOCKED"):
            apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()
    assert not (config.parent / "install-pending.json").exists()


def test_install_first_binding_requires_confirmation_and_is_transactional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding = binding_receipt_path(config)
    binding.unlink()
    before_config = config.read_bytes()
    with pytest.raises(ConfigError, match="PRIVATE_REMOTE_CONFIRMATION_REQUIRED"):
        plan_install(ROOT, config, state, ROOT, manifest)
    assert config.read_bytes() == before_config
    assert not binding.exists() and not install_root.exists()
    plan = plan_install(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    assert plan[-1].endswith("ready=true no_changes=false")
    assert config.read_bytes() == before_config
    assert not binding.exists() and not install_root.exists()
    built = installer_module._build_plan(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    config.write_bytes(before_config + b"\n")
    with pytest.raises(ConfigError, match="FAIL_STATE_BINDING"):
        installer_module._apply_install_plan(built, force=False)
    assert not binding.exists() and not install_root.exists()
    config.write_bytes(before_config)
    applied = apply_install(
        ROOT, config, state, ROOT, manifest, force=False, confirm_private_remote=True,
    )
    assert applied[-1].startswith("PASS artifact_sha256=")
    assert binding.is_file() and (config.parent / "install-receipt.json").is_file()
    assert apply_install(ROOT, config, state, ROOT, manifest, force=False) == [
        "PASS install version=0.1.0.dev9 no_changes=true"
    ]


def test_install_first_binding_uses_rewritten_config_bytes_for_both_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding_receipt_path(config).unlink()
    (config.parent / "materialization-receipt.json").unlink(missing_ok=True)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["state_root"] = str(tmp_path / "old-state")
    config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    planned = plan_install(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    apply_install(
        ROOT, config, state, ROOT, manifest, force=False,
        confirm_private_remote=True, plan_hash=token,
    )

    config_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
    binding = json.loads(binding_receipt_path(config).read_text(encoding="utf-8"))
    materialization = json.loads(
        (config.parent / "materialization-receipt.json").read_text(encoding="utf-8")
    )
    assert binding["config_sha256"] == materialization["config"]["sha256"] == config_sha256
    assert plan_install(ROOT, config, state, ROOT, manifest)[-1] == (
        "DRY_RUN writes=0 ready=true no_changes=true"
    )
    assert execute_sync(ROOT, config, state, apply=False)[-1] == (
        "DRY_RUN writes=0 targets=2 planned_writes=0 ready=true"
    )
    assert run_doctor(
        install_root / "engine" / "0.1.0.dev9",
        config, state, state / "manifest.yaml",
    )


def test_true_first_binding_defers_absent_baseline_to_install_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding = binding_receipt_path(config)
    current = config.parent / "txn" / "remote-state.json"
    binding.unlink()
    current.unlink()
    planned = plan_install(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))

    def fail_after_baseline(plan, _snapshot) -> None:
        expected = git(state, "rev-parse", "origin/main").stdout.strip()
        assert json.loads(current.read_text(encoding="utf-8")) == {"last_known_good": expected}
        raise ConfigError("FAIL_INSTALL_VERIFY", "synthetic after deferred baseline")

    monkeypatch.setattr(installer_module, "_install_engine", fail_after_baseline)
    with pytest.raises(ConfigError, match="synthetic after deferred baseline"):
        apply_install(
            ROOT, config, state, ROOT, manifest, force=False,
            confirm_private_remote=True, plan_hash=token,
        )
    assert not binding.exists()
    assert not current.exists()
    assert not install_root.exists()
    assert not (config.parent / "install-pending.json").exists()
    assert not (config.parent / "rollback").exists()


def test_reaccept_defers_old_baseline_update_to_install_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding = binding_receipt_path(config)
    current = config.parent / "txn" / "remote-state.json"
    binding_before = binding.read_bytes()
    baseline_before = current.read_bytes()
    (state / "advance.txt").write_text("advance\n", encoding="utf-8")
    git(state, "add", "advance.txt")
    git(state, "commit", "-q", "-m", "advance binding revision")
    git(state, "push", "-q", "origin", "main")
    new_remote = git(state, "rev-parse", "origin/main").stdout.strip()
    planned = plan_install(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))

    def fail_after_baseline(plan, _snapshot) -> None:
        assert json.loads(current.read_text(encoding="utf-8")) == {"last_known_good": new_remote}
        raise ConfigError("FAIL_INSTALL_VERIFY", "synthetic after deferred reaccept")

    monkeypatch.setattr(installer_module, "_install_engine", fail_after_baseline)
    with pytest.raises(ConfigError, match="synthetic after deferred reaccept"):
        apply_install(
            ROOT, config, state, ROOT, manifest, force=False,
            confirm_private_remote=True, plan_hash=token,
        )
    assert binding.read_bytes() == binding_before
    assert current.read_bytes() == baseline_before
    assert not install_root.exists()
    assert not (config.parent / "install-pending.json").exists()
    assert not (config.parent / "rollback").exists()


def test_existing_invalid_binding_requires_confirmation_then_reaccepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, clone, config, manifest, install_root = canonical_installed_fixture(tmp_path, monkeypatch)
    advance_canonical_engine(source, clone)
    binding = binding_receipt_path(config)
    before = {
        "config": config.read_bytes(),
        "binding": binding.read_bytes(),
        "pin": (install_root / "engine-pin.json").read_bytes(),
    }
    with pytest.raises(ConfigError, match="FAIL_STATE_BINDING"):
        plan_install(clone / "engine", config, clone / "state", clone / "engine", manifest)
    assert before == {
        "config": config.read_bytes(),
        "binding": binding.read_bytes(),
        "pin": (install_root / "engine-pin.json").read_bytes(),
    }

    lines = plan_install(
        clone / "engine", config, clone / "state", clone / "engine", manifest,
        confirm_private_remote=True,
    )
    assert "BINDING_REFRESH pending=true" in lines
    token = next(line.split(" ", 1)[1] for line in lines if line.startswith("PLAN_HASH "))
    assert before == {
        "config": config.read_bytes(),
        "binding": binding.read_bytes(),
        "pin": (install_root / "engine-pin.json").read_bytes(),
    }
    applied = apply_install(
        clone / "engine", config, clone / "state", clone / "engine", manifest,
        force=False, confirm_private_remote=True, plan_hash=token,
    )
    assert applied[-1].startswith("PASS artifact_sha256=")
    record_sha256 = hashlib.sha256((clone / "engine.provenance.json").read_bytes()).hexdigest()
    assert json.loads(binding.read_text(encoding="utf-8"))["engine_provenance_sha256"] == record_sha256
    next_plan = plan_install(clone / "engine", config, clone / "state", clone / "engine", manifest)
    assert next_plan[-1] == "DRY_RUN writes=0 ready=true no_changes=true"


def test_reacceptance_token_rejects_reviewed_binding_drift_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, clone, config, manifest, install_root = canonical_installed_fixture(tmp_path, monkeypatch)
    advance_canonical_engine(source, clone)
    binding = binding_receipt_path(config)
    lines = plan_install(
        clone / "engine", config, clone / "state", clone / "engine", manifest,
        confirm_private_remote=True,
    )
    token = next(line.split(" ", 1)[1] for line in lines if line.startswith("PLAN_HASH "))
    config_before = config.read_bytes()
    binding_before = binding.read_bytes()
    pin_before = (install_root / "engine-pin.json").read_bytes()
    config.write_bytes(config_before + b"\n")
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(
            clone / "engine", config, clone / "state", clone / "engine", manifest,
            force=False, confirm_private_remote=True, plan_hash=token,
        )
    assert binding.read_bytes() == binding_before
    assert (install_root / "engine-pin.json").read_bytes() == pin_before
    assert not (config.parent / "install-pending.json").exists()
    config.write_bytes(config_before)


@pytest.mark.skipif(os.name != "nt", reason="Windows batch wrapper contract")
def test_repository_windows_wrapper_propagates_child_exit_status(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["AGENT_CORE_PYTHON"] = os.fspath(Path(os.sys.executable))
    wrapper = ROOT / "agent-core.cmd"
    invalid = subprocess.run(
        [str(wrapper), "install", "--unknown-argument"], cwd=tmp_path, env=environment,
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert invalid.returncode == 2
    version = subprocess.run(
        [str(wrapper), "--version"], cwd=tmp_path, env=environment,
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert version.returncode == 0


def test_existing_unowned_path_is_foreign_and_unowned_file_survives(
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
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=True)
    assert rules.read_bytes() == b"user-owned-before\n"
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
    conflicts = [line for line in lines if " status=foreign " in line]
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


def test_receipt_owned_old_bytes_are_managed_update(
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
        raise AssertionError("synthetic plan must stay read-only")

    monkeypatch.setattr(installer_module, "require_fresh", forbidden)
    monkeypatch.setattr(installer_module, "_snapshot", forbidden)
    lines = plan_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None)
    assert any(line.startswith("TARGET managed status=managed-update ") for line in lines)
    assert lines[-1] == "DRY_RUN writes=0 ready=true no_changes=false"
    assert target.read_bytes() == old_content
    assert receipt_path.read_bytes() == receipt_before
    assert not (receipt_path.parent / "rollback").exists()


def test_g19b_a_to_b_to_c_uses_materialization_receipt_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    target = install_root / "runtime" / "LESSONS.md"
    target.parent.mkdir(parents=True)
    generation_a = b"generation-a\n"
    generation_b = b"generation-b-from-sync\n"
    generation_c = b"generation-c-desired\n"
    target.write_bytes(generation_b)
    receipt_path = tmp_path / "host" / "install-receipt.json"
    receipt_path.parent.mkdir()
    artifact = installer_module.Artifact("synthetic", "A" * 43, (), b"{}")
    receipt_path.write_text(json.dumps({
        "schema": "install-receipt/1",
        "engine_version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "config_sha256": "0" * 64,
        "state_lock_sha256": "1" * 64,
        "snapshot_path": str(tmp_path / "old-snapshot"),
        "objects": [{
            "label": "runtime:codex:lessons",
            "path": str(target),
            "root": str(install_root),
            "kind": "file",
            "before_exists": False,
            "before_sha256": None,
            "installed_sha256": hashlib.sha256(generation_a).hexdigest(),
            "snapshot_rel": "objects/0",
        }],
        "hook_bindings": [],
    }), encoding="utf-8")
    config_path = tmp_path / "host" / "host.json"
    config_payload = {
        "targets": [{
            "id": "codex", "runtime": "codex", "root": str(install_root),
            "lessons_target": "runtime/LESSONS.md",
        }],
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    state_lock = state_root / "agent-core.lock.json"
    state_lock.write_bytes(b"{}\n")
    binding = synthetic_binding(config_path, tmp_path / "state")
    (config_path.parent / "materialization-receipt.json").write_text(json.dumps({
        "schema": "materialization-receipt/1",
        "config": {
            "path": str(config_path.resolve()),
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        },
        "state": {
            "root": str((tmp_path / "state").resolve()),
            "repository_root_sha": binding.repository_root_sha,
            "head": binding.remote_revision,
            "remote_revision": binding.remote_revision,
        },
        "generation": 1,
        "transaction_id": "7" * 64,
        "rows": [{
            "target_id": "codex",
            "source_label": "lessons",
            "root": str(install_root.resolve()),
            "path": str(target.resolve()),
            "kind": "file",
            "installed_sha256": hashlib.sha256(generation_b).hexdigest(),
            "status": "active",
        }],
    }), encoding="utf-8")
    plan = installer_module.InstallPlan(
        config_path, config_payload, tmp_path / "state", tmp_path / "source",
        artifact, install_root, install_root / "engine" / artifact.version, receipt_path,
        (installer_module.ManagedObject(
            "runtime:codex:lessons", target, install_root, "file",
            hashlib.sha256(generation_c).hexdigest(), generation_c,
        ),),
        (),
        binding,
    )
    monkeypatch.setattr(installer_module, "_build_plan", lambda *_args, **_kwargs: plan)
    before = target.read_bytes()

    lines = plan_install(tmp_path, plan.config_path, plan.state_root, plan.source_root, None)

    assert any(
        line.startswith("TARGET runtime:codex:lessons status=managed-update ")
        for line in lines
    )
    assert lines[-1] == "DRY_RUN writes=0 ready=true no_changes=false"
    assert target.read_bytes() == before == generation_b
    assert not (receipt_path.parent / "rollback").exists()


def test_install_unknown_writer_matching_new_desired_is_foreign_and_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    target = install_root / "runtime" / "LESSONS.md"
    target.parent.mkdir(parents=True)
    generation_a = b"generation-a\n"
    generation_b = b"unknown-writer-generation-b\n"
    target.write_bytes(generation_b)
    config_path = tmp_path / "host" / "host.json"
    config_path.parent.mkdir()
    config_payload = {"targets": [{
        "id": "codex", "root": str(install_root),
        "lessons_target": "runtime/LESSONS.md",
    }]}
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    artifact = installer_module.Artifact("synthetic", "A" * 43, (), b"{}")
    install_receipt = config_path.parent / "install-receipt.json"
    install_receipt.write_text(json.dumps({
        "schema": "install-receipt/1",
        "engine_version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "state_lock_sha256": "1" * 64,
        "snapshot_path": str(tmp_path / "old-snapshot"),
        "objects": [{
            "label": "runtime:codex:lessons", "path": str(target),
            "root": str(install_root), "kind": "file", "before_exists": False,
            "before_sha256": None,
            "installed_sha256": hashlib.sha256(generation_a).hexdigest(),
            "snapshot_rel": "objects/0",
        }],
        "hook_bindings": [],
    }), encoding="utf-8")
    binding = synthetic_binding(config_path, tmp_path / "state")
    materialization = config_path.parent / "materialization-receipt.json"
    materialization.write_text(json.dumps({
        "schema": "materialization-receipt/1",
        "config": {
            "path": str(config_path.resolve()),
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        },
        "state": {
            "root": str((tmp_path / "state").resolve()),
            "repository_root_sha": binding.repository_root_sha,
            "head": binding.remote_revision,
            "remote_revision": binding.remote_revision,
        },
        "generation": 1,
        "transaction_id": "7" * 64,
        "rows": [{
            "target_id": "codex", "source_label": "lessons",
            "root": str(install_root.resolve()), "path": str(target.resolve()),
            "kind": "file", "installed_sha256": hashlib.sha256(generation_a).hexdigest(),
            "status": "active",
        }],
    }), encoding="utf-8")
    plan = installer_module.InstallPlan(
        config_path, config_payload, tmp_path / "state", tmp_path / "source",
        artifact, install_root, install_root / "engine" / artifact.version, install_receipt,
        (installer_module.ManagedObject(
            "runtime:codex:lessons", target, install_root, "file",
            hashlib.sha256(generation_b).hexdigest(), generation_b,
        ),), (), binding,
    )
    monkeypatch.setattr(installer_module, "_build_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        installer_module, "validate_state_binding", lambda *_args, **_kwargs: binding,
    )
    install_receipt_before = install_receipt.read_bytes()
    materialization_before = materialization.read_bytes()

    planned = plan_install(tmp_path, config_path, plan.state_root, plan.source_root, None)

    assert any(
        line.startswith("TARGET runtime:codex:lessons status=foreign ")
        for line in planned
    )
    assert planned[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(
            tmp_path, config_path, plan.state_root, plan.source_root, None, force=False,
        )
    assert target.read_bytes() == generation_b
    assert install_receipt.read_bytes() == install_receipt_before
    assert materialization.read_bytes() == materialization_before
    assert not (config_path.parent / "install-pending.json").exists()
    assert not (config_path.parent / "rollback").exists()


@pytest.mark.parametrize(
    ("current_generation", "expected_status"),
    (
        (b"generation-a\n", "bootstrap-managed-update"),
        (b"generation-b\n", "adopt-identical"),
    ),
    ids=("lagging-host", "already-synced-host"),
)
def test_g19b_legacy_install_row_bootstrap_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    current_generation: bytes, expected_status: str,
) -> None:
    install_root = tmp_path / "install"
    target = install_root / "runtime" / "LESSONS.md"
    target.parent.mkdir(parents=True)
    generation_a = b"generation-a\n"
    generation_b = b"generation-b\n"
    target.write_bytes(current_generation)
    config_path = tmp_path / "host" / "host.json"
    config_path.parent.mkdir()
    config_payload = {
        "targets": [{
            "id": "codex", "runtime": "codex", "root": str(install_root),
            "lessons_target": "runtime/LESSONS.md",
        }],
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    state_lock = state_root / "agent-core.lock.json"
    state_lock.write_bytes(b"{}\n")
    receipt_path = config_path.parent / "install-receipt.json"
    artifact = installer_module.Artifact("synthetic", "A" * 43, (), b"{}")
    receipt_path.write_text(json.dumps({
        "schema": "install-receipt/1",
        "engine_version": artifact.version,
        "artifact_sha256": artifact.artifact_sha256,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "state_lock_sha256": hashlib.sha256(state_lock.read_bytes()).hexdigest(),
        "snapshot_path": str(tmp_path / "old-snapshot"),
        "objects": [{
            "label": "runtime:codex:lessons", "path": str(target),
            "root": str(install_root), "kind": "file", "before_exists": False,
            "before_sha256": None,
            "installed_sha256": hashlib.sha256(generation_a).hexdigest(),
            "snapshot_rel": "objects/0",
        }],
        "hook_bindings": [],
    }), encoding="utf-8")
    binding = synthetic_binding(config_path, state_root)
    plan = installer_module.InstallPlan(
        config_path, config_payload, state_root, tmp_path / "source",
        artifact, install_root, install_root / "engine" / artifact.version, receipt_path,
        (installer_module.ManagedObject(
            "runtime:codex:lessons", target, install_root, "file",
            hashlib.sha256(generation_b).hexdigest(), generation_b,
        ),), (), binding,
    )
    monkeypatch.setattr(installer_module, "_build_plan", lambda *_args, **_kwargs: plan)

    lines = plan_install(tmp_path, config_path, plan.state_root, plan.source_root, None)

    assert any(
        line.startswith(f"TARGET runtime:codex:lessons status={expected_status} ")
        for line in lines
    )
    assert lines[-1] == "DRY_RUN writes=0 ready=true no_changes=false"


@pytest.mark.parametrize("mismatch", ("config", "state-lock", "hook"))
def test_g19b_legacy_bridge_identity_mismatch_is_indeterminate_and_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    materialization = config.parent / "materialization-receipt.json"
    materialization.unlink()
    install_receipt = config.parent / "install-receipt.json"
    payload = json.loads(install_receipt.read_text(encoding="utf-8"))
    if mismatch == "config":
        payload["config_sha256"] = "f" * 64
    elif mismatch == "state-lock":
        payload["state_lock_sha256"] = "f" * 64
    else:
        assert payload["hook_bindings"]
        payload["hook_bindings"][0]["path"] = str((tmp_path / "redirected-hook.json").resolve())
    install_receipt.write_text(json.dumps(payload), encoding="utf-8")
    receipt_before = install_receipt.read_bytes()
    config_payload = json.loads(config.read_text(encoding="utf-8"))
    observed_roots = [install_root, *(
        Path(item["root"]) for item in config_payload["targets"]
    )]
    runtime_before = {
        path: path.read_bytes()
        for root in observed_roots for path in root.rglob("*") if path.is_file()
    }

    planned = plan_install(ROOT, config, state, ROOT, manifest)

    runtime = [line for line in planned if line.startswith("TARGET runtime:")]
    assert runtime and all("status=indeterminate" in line for line in runtime)
    assert planned[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert install_receipt.read_bytes() == receipt_before
    assert not materialization.exists()
    assert {path: path.read_bytes() for path in runtime_before} == runtime_before
    assert not (config.parent / "install-pending.json").exists()


def test_g19b_real_install_a_sync_b_install_c_has_no_foreign_false_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    rules_path = Path(payload["targets"][0]["root"]) / payload["targets"][0]["rules_target"]

    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    generation_a = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    install_receipt_a = (config.parent / "install-receipt.json").read_bytes()

    global_rules = state / "rules" / "global.md"
    global_rules.write_bytes(global_rules.read_bytes() + b"\n<!-- generation-b -->\n")
    git(state, "add", "rules/global.md")
    git(state, "commit", "-q", "-m", "generation b")
    git(state, "push", "-q", "origin", "main")
    sync_plan = execute_sync(ROOT, config, state, apply=False)
    assert any("status=managed-update" in line for line in sync_plan if line.startswith("PLAN_OP "))
    sync_token = next(line.removeprefix("PLAN_HASH ") for line in sync_plan if line.startswith("PLAN_HASH "))
    execute_sync(ROOT, config, state, apply=True, plan_hash=sync_token)
    generation_b = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    assert generation_b != generation_a
    assert (config.parent / "install-receipt.json").read_bytes() == install_receipt_a

    global_rules.write_bytes(global_rules.read_bytes() + b"\n<!-- generation-c -->\n")
    git(state, "add", "rules/global.md")
    git(state, "commit", "-q", "-m", "generation c")
    git(state, "push", "-q", "origin", "main")
    install_c_plan = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(
        line.startswith("TARGET runtime:") and "status=managed-update" in line
        for line in install_c_plan
    )
    assert install_c_plan[-1] == "DRY_RUN writes=0 ready=true no_changes=false"
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    generation_c = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    assert generation_c not in {generation_a, generation_b}
    materialization = json.loads(
        (config.parent / "materialization-receipt.json").read_text(encoding="utf-8")
    )
    row = next(item for item in materialization["rows"] if item["path"] == str(rules_path))
    assert row["installed_sha256"] == generation_c


def test_install_missing_ownership_receipt_bootstraps_before_final_install_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    materialization = config.parent / "materialization-receipt.json"
    materialization.unlink()
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    runtime = [line for line in planned if line.startswith("TARGET runtime:")]
    assert runtime and all("status=bootstrap-identical" in line for line in runtime)
    assert planned[-1] == "DRY_RUN writes=0 ready=true no_changes=false"

    events: list[str] = []
    original_publish = installer_module.publish_materialization_receipt
    original_replace = installer_module._replace_owned_bytes

    def publish(*args, **kwargs):
        events.append("materialization-receipt")
        return original_publish(*args, **kwargs)

    def replace_bytes(*args, **kwargs):
        key = kwargs.get("key") or (args[5] if len(args) >= 6 else None)
        if key == "install-receipt":
            events.append("install-receipt")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(installer_module, "publish_materialization_receipt", publish)
    monkeypatch.setattr(installer_module, "_replace_owned_bytes", replace_bytes)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert events == ["materialization-receipt", "install-receipt"]
    assert materialization.is_file()


def test_install_corrupt_materialization_receipt_is_indeterminate_and_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    materialization = config.parent / "materialization-receipt.json"
    materialization.write_text("{}", encoding="utf-8")
    before = {
        path: path.read_bytes() for path in install_root.rglob("*") if path.is_file()
    }

    planned = plan_install(ROOT, config, state, ROOT, manifest)

    runtime = [line for line in planned if line.startswith("TARGET runtime:")]
    assert runtime and all("status=indeterminate" in line for line in runtime)
    assert planned[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert materialization.read_text(encoding="utf-8") == "{}"
    assert {path: path.read_bytes() for path in install_root.rglob("*") if path.is_file()} == before
    assert not (config.parent / "install-pending.json").exists()


def test_install_exact_unowned_runtime_is_visibly_adopted_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    config_payload = json.loads(config.read_text(encoding="utf-8"))
    operations = installer_module.collect_operations(ROOT, config_payload, state)
    before: dict[Path, tuple[int, int, int]] = {}
    for operation in operations:
        operation.destination.parent.mkdir(parents=True, exist_ok=True)
        operation.destination.write_bytes(operation.content)
        stat = operation.destination.stat()
        before[operation.destination] = (stat.st_dev, stat.st_ino, stat.st_mtime_ns)

    planned = plan_install(ROOT, config, state, ROOT, manifest)
    runtime = [line for line in planned if line.startswith("TARGET runtime:")]
    assert runtime and all("status=adopt-identical" in line for line in runtime)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)

    after = {
        path: (path.stat().st_dev, path.stat().st_ino, path.stat().st_mtime_ns)
        for path in before
    }
    assert after == before


def test_install_refuses_materialization_pending_before_plan_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    pending = config.parent / "txn" / "materialization-pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECOVERY"):
        plan_install(ROOT, config, state, ROOT, manifest)
    with pytest.raises(ConfigError, match="FAIL_MATERIALIZATION_RECOVERY"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert not install_root.exists()
    assert pending.read_text(encoding="utf-8") == "{}"


def test_install_plan_binds_materialization_receipt_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    planned = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.removeprefix("PLAN_HASH ") for line in planned if line.startswith("PLAN_HASH "))
    receipt = config.parent / "materialization-receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    before = {path: path.read_bytes() for path in install_root.rglob("*") if path.is_file()}

    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(
            ROOT, config, state, ROOT, manifest, force=False, plan_hash=token,
        )

    assert {path: path.read_bytes() for path in install_root.rglob("*") if path.is_file()} == before
    assert not (config.parent / "install-pending.json").exists()


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
            line.startswith("TARGET runtime-config:claude-code status=foreign ")
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
            line.startswith("TARGET runtime-config:claude-code status=managed-update ")
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

    original = installer_module._replace_owned_bytes

    def fail_receipt(*args, **kwargs) -> None:
        if kwargs.get("key") == "install-receipt" or (len(args) >= 6 and args[5] == "install-receipt"):
            raise OSError("INJECTED_RECEIPT_FAILURE")
        original(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_replace_owned_bytes", fail_receipt)
    with pytest.raises(ConfigError, match="FAIL_INSTALL"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert not (install_root / "engine-pin.json").exists()
    assert not (install_root / "engine" / "0.1.0.dev9").exists()
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "materialization-receipt.json").exists()


def test_materialization_receipt_failure_rolls_back_before_install_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        installer_module, "publish_materialization_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConfigError("FAIL_TEST", "materialization receipt fault")
        ),
    )

    with pytest.raises(ConfigError, match="FAIL_TEST"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert not install_root.exists()
    assert not (config.parent / "materialization-receipt.json").exists()
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "install-pending.json").exists()


def test_materialization_receipt_race_preserves_racer_snapshot_and_install_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    racer = b'{"racer":true}\n'

    def race(review, *_args, **_kwargs):
        review.receipt_path.write_bytes(racer)
        raise ConfigError("FAIL_MATERIALIZER_RACE", "synthetic receipt race")

    monkeypatch.setattr(installer_module, "publish_materialization_receipt", race)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE") as caught:
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert (config.parent / "materialization-receipt.json").read_bytes() == racer
    pending = config.parent / "install-pending.json"
    assert pending.is_file()
    assert "detached pre-image retained" in str(caught.value)


@pytest.mark.parametrize("reinstall", (False, True), ids=("first-install", "reinstall"))
def test_install_receipt_race_preserves_racer_snapshot_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reinstall: bool,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    receipt = config.parent / "install-receipt.json"
    if reinstall:
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
        global_rules = state / "rules" / "global.md"
        global_rules.write_bytes(global_rules.read_bytes() + b"\nreinstall-generation\n")
        git(state, "add", "rules/global.md")
        git(state, "commit", "-q", "-m", "reinstall generation")
        git(state, "push", "-q", "origin", "main")
    racer = b'{"install-receipt-racer":true}\n'
    original = installer_module._replace_owned_bytes

    def race(*args, **kwargs):
        key = kwargs.get("key") or (args[5] if len(args) >= 6 else None)
        if key == "install-receipt":
            Path(args[0]).write_bytes(racer)
            raise ConfigError("FAIL_INSTALL_RACE", "synthetic install receipt race")
        return original(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_replace_owned_bytes", race)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE") as caught:
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert receipt.read_bytes() == racer
    pending = config.parent / "install-pending.json"
    assert pending.is_file()
    snapshot_id = json.loads(pending.read_text(encoding="utf-8"))["snapshot_id"]
    snapshot = config.parent / "rollback" / snapshot_id
    assert snapshot.is_dir()
    assert "raced bytes preserved" in str(caught.value)


@pytest.mark.parametrize(
    ("racer_kind", "fault"),
    (("runtime", "verify"), ("runtime", "receipt"), ("hook", "verify")),
)
def test_install_ordinary_failure_preserves_runtime_or_hook_racer_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, racer_kind: str, fault: str,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    if racer_kind == "runtime":
        raced_path = next(
            item.destination for item in installer_module.collect_operations(ROOT, payload, state)
            if item.source_label == "rules"
        )
    else:
        target = payload["targets"][0]
        raced_path = installer_module.runtime_config_path(
            target["runtime"], Path(target["root"]),
        )
    racer = f"{racer_kind}-racer\n".encode("utf-8")
    if fault == "verify":
        def fail_verify(*_args, **_kwargs):
            raced_path.parent.mkdir(parents=True, exist_ok=True)
            raced_path.write_bytes(racer)
            raise ConfigError("FAIL_TEST_VERIFY", "ordinary verify failure")

        monkeypatch.setattr(installer_module, "_verify_installed", fail_verify)
    else:
        original_replace = installer_module._replace_owned_bytes

        def fail_receipt(*args, **kwargs):
            key = kwargs.get("key") or (args[5] if len(args) >= 6 else None)
            if key == "install-receipt":
                raced_path.write_bytes(racer)
                raise OSError("ordinary receipt failure")
            return original_replace(*args, **kwargs)

        monkeypatch.setattr(installer_module, "_replace_owned_bytes", fail_receipt)

    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert raced_path.read_bytes() == racer
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "materialization-receipt.json").exists()
    pending = config.parent / "install-pending.json"
    assert pending.is_file()
    snapshot_id = json.loads(pending.read_text(encoding="utf-8"))["snapshot_id"]
    assert (config.parent / "rollback" / snapshot_id).is_dir()


@pytest.mark.parametrize("receipt_kind", ("install", "materialization"))
@pytest.mark.parametrize("reinstall", (False, True), ids=("first-install", "reinstall"))
def test_receipt_rollback_cas_preserves_racer_written_immediately_before_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    receipt_kind: str, reinstall: bool,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    install_receipt = config.parent / "install-receipt.json"
    materialization_receipt = config.parent / "materialization-receipt.json"
    if reinstall:
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
        global_rules = state / "rules" / "global.md"
        global_rules.write_bytes(global_rules.read_bytes() + b"\nrollback-cas-generation\n")
        git(state, "add", "rules/global.md")
        git(state, "commit", "-q", "-m", "rollback cas generation")
        git(state, "push", "-q", "origin", "main")
    install_before = install_receipt.read_bytes() if install_receipt.is_file() else None
    materialization_before = (
        materialization_receipt.read_bytes() if materialization_receipt.is_file() else None
    )
    racer = f"{receipt_kind}-rollback-racer\n".encode("utf-8")
    raced_path = install_receipt if receipt_kind == "install" else materialization_receipt
    original_restore = installer_module._restore_receipt_preimage_cas
    injected = False

    def inject_racer(path: Path, *args, **kwargs):
        nonlocal injected
        if not injected and path.resolve() == raced_path.resolve():
            injected = True
            path.write_bytes(racer)
        return original_restore(path, *args, **kwargs)

    def fail_cleanup(_config_path: Path) -> None:
        raise OSError("ordinary cleanup failure")

    monkeypatch.setattr(installer_module, "_restore_receipt_preimage_cas", inject_racer)
    monkeypatch.setattr(installer_module, "_clear_pending", fail_cleanup)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RACE"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert injected is True
    assert raced_path.read_bytes() == racer
    other_path, other_before = (
        (materialization_receipt, materialization_before)
        if receipt_kind == "install" else (install_receipt, install_before)
    )
    if other_before is None:
        assert not other_path.exists()
    else:
        assert other_path.read_bytes() == other_before
    pending = config.parent / "install-pending.json"
    assert pending.is_file()
    snapshot_id = json.loads(pending.read_text(encoding="utf-8"))["snapshot_id"]
    assert (config.parent / "rollback" / snapshot_id).is_dir()


def test_install_marker_cleanup_fault_rolls_back_both_receipts_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    original_clear = installer_module._clear_pending
    calls = 0

    def fail_once(config_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic marker cleanup fault")
        original_clear(config_path)

    monkeypatch.setattr(installer_module, "_clear_pending", fail_once)
    with pytest.raises(ConfigError, match="FAIL_INSTALL"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)

    assert not install_root.exists()
    assert not (config.parent / "materialization-receipt.json").exists()
    assert not (config.parent / "install-receipt.json").exists()
    assert not (config.parent / "install-pending.json").exists()


def test_existing_version_directory_is_foreign_even_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    version_root = install_root / "engine" / "0.1.0.dev9"
    version_root.mkdir(parents=True)
    (version_root / "foreign.txt").write_text("different artifact", encoding="utf-8")
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
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


def test_install_apply_requires_the_exact_reviewed_plan_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("token boundary reached install planning")

    monkeypatch.setattr(installer_module, "_build_plan", forbidden)
    assert installer_module.main(["install", "--config", str(config), "--apply"]) == 1
    assert "FAIL_PLAN_HASH" in capsys.readouterr().err
    assert installer_module.main(["install", "--config", str(config), "--plan-hash", "0" * 64]) == 1
    assert "FAIL_PLAN_HASH" in capsys.readouterr().err
    assert not install_root.exists()


def test_reviewed_plan_token_rejects_target_drift_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    lines = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.split(" ", 1)[1] for line in lines if line.startswith("PLAN_HASH "))
    payload = json.loads(config.read_text(encoding="utf-8"))
    target = Path(payload["targets"][0]["root"]) / payload["targets"][0]["rules_target"]
    target.parent.mkdir(parents=True)
    drift = b"reviewer-target-drift\n"
    target.write_bytes(drift)
    with pytest.raises(ConfigError, match="FAIL_PLAN_HASH"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert target.read_bytes() == drift
    assert not install_root.exists()
    assert not (config.parent / "rollback").exists()


def test_interrupted_first_binding_marker_is_diagnostic_then_replan_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding = binding_receipt_path(config)
    binding.unlink()
    config_before = config.read_bytes()
    lines = plan_install(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    token = next(line.split(" ", 1)[1] for line in lines if line.startswith("PLAN_HASH "))

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt("after first-binding host writes")

    original_install_engine = installer_module._install_engine
    monkeypatch.setattr(installer_module, "_install_engine", interrupted)
    with pytest.raises(KeyboardInterrupt, match="after first-binding host writes"):
        apply_install(
            ROOT, config, state, ROOT, manifest, force=False,
            confirm_private_remote=True, plan_hash=token,
        )
    pending = config.parent / "install-pending.json"
    assert pending.is_file() and binding.exists() and config.read_bytes() == config_before
    marker_before = pending.read_bytes()
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY") as planned:
        plan_install(ROOT, config, state, ROOT, manifest)
    assert "inspect and clear pending install marker" in str(planned.value)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    assert pending.read_bytes() == marker_before
    assert binding.exists() and config.read_bytes() == config_before and not install_root.exists()

    # The user explicitly clears only the diagnostic marker, then reviews a new plan.
    pending.unlink()
    monkeypatch.setattr(installer_module, "_install_engine", original_install_engine)
    replan = plan_install(ROOT, config, state, ROOT, manifest)
    replan_token = next(line.split(" ", 1)[1] for line in replan if line.startswith("PLAN_HASH "))
    applied = apply_install(
        ROOT, config, state, ROOT, manifest, force=False, plan_hash=replan_token,
    )
    assert applied[-1].startswith("PASS artifact_sha256=")
    assert binding.is_file() and (config.parent / "install-receipt.json").is_file()


def test_interrupted_exact_hooks_are_adopted_without_rewrite_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    initial = plan_install(ROOT, config, state, ROOT, manifest)
    token = next(line.split(" ", 1)[1] for line in initial if line.startswith("PLAN_HASH "))
    original_replace = installer_module._replace_owned_bytes

    def interrupt_receipt(*args, **kwargs) -> None:
        key = kwargs.get("key") or (args[5] if len(args) >= 6 else None)
        if key == "install-receipt":
            raise KeyboardInterrupt("receipt publication boundary")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_replace_owned_bytes", interrupt_receipt)
    with pytest.raises(KeyboardInterrupt, match="receipt publication boundary"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=token)
    pending = config.parent / "install-pending.json"
    receipt_path = config.parent / "install-receipt.json"
    assert pending.is_file() and not receipt_path.exists()
    payload = json.loads(config.read_text(encoding="utf-8"))
    settings = [
        Path(target["root"]) / ("settings.json" if target["runtime"] == "claude-code" else "hooks.json")
        for target in payload["targets"] if target["runtime"] in {"claude-code", "codex"}
    ]
    before = {path: path.read_bytes() for path in settings}

    pending.unlink()
    replan = plan_install(ROOT, config, state, ROOT, manifest)
    assert sum(" status=adopt-identical " in line for line in replan) == len(settings)
    replan_token = next(line.split(" ", 1)[1] for line in replan if line.startswith("PLAN_HASH "))
    writes: list[str] = []

    def record_hook_replacement(*args, **kwargs) -> None:
        key = kwargs.get("key") or (args[5] if len(args) >= 6 else None)
        writes.append(str(key))
        original_replace(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_replace_owned_bytes", record_hook_replacement)
    apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=replan_token)
    assert not any(key.startswith("hook-") for key in writes)
    assert {path: path.read_bytes() for path in settings} == before
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert {
        item["installed_sha256"] for item in receipt["hook_bindings"]
    } == {hashlib.sha256(content).hexdigest() for content in before.values()}


def test_competing_install_fails_before_snapshot_while_first_holds_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    entered_snapshot = threading.Event()
    release_first = threading.Event()
    first_errors: list[BaseException] = []
    original_snapshot = installer_module._snapshot

    def pause_first(*args, **kwargs):
        entered_snapshot.set()
        if not release_first.wait(timeout=30):
            raise AssertionError("first install did not receive release")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(installer_module, "_snapshot", pause_first)

    def first_apply() -> None:
        try:
            apply_install(ROOT, config, state, ROOT, manifest, force=False)
        except BaseException as exc:  # Preserve assertion failures from the worker thread.
            first_errors.append(exc)

    worker = threading.Thread(target=first_apply, daemon=True)
    worker.start()
    assert entered_snapshot.wait(timeout=30)
    before = {
        "config": config.read_bytes(),
        "receipt": (config.parent / "install-receipt.json").exists(),
        "marker": (config.parent / "install-pending.json").exists(),
        "install": install_root.exists(),
    }
    with pytest.raises(ConfigError, match="FAIL_LOCKED"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert config.read_bytes() == before["config"]
    assert (config.parent / "install-receipt.json").exists() is before["receipt"]
    assert (config.parent / "install-pending.json").exists() is before["marker"]
    assert install_root.exists() is before["install"]
    release_first.set()
    worker.join(timeout=120)
    assert not worker.is_alive() and not first_errors
    assert (config.parent / "install-receipt.json").is_file()


def test_first_attach_revalidates_complete_binding_evidence_before_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    binding = binding_receipt_path(config)
    binding.unlink()
    config_before = config.read_bytes()
    plan = installer_module._build_plan(
        ROOT, config, state, ROOT, manifest, confirm_private_remote=True,
    )
    plan, *_ignored = installer_module._reviewed_install_plan(plan)
    real_validate = installer_module.validate_state_binding

    def changed_evidence(*args, **kwargs):
        evidence = real_validate(*args, **kwargs)
        return replace(evidence, config_sha256="f" * 64)

    monkeypatch.setattr(installer_module, "validate_state_binding", changed_evidence)
    with pytest.raises(ConfigError, match="^FAIL_STATE_BINDING binding changed after install plan$"):
        installer_module._apply_install_plan(plan, force=False)
    assert config.read_bytes() == config_before
    assert not binding.exists() and not install_root.exists()
    assert not (config.parent / "install-pending.json").exists()


def test_pending_marker_is_read_only_even_with_hash_consistent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    snapshot = config.parent / "rollback" / ("install-" + "a" * 32)
    snapshot.mkdir(parents=True)
    snapshot_bytes = b'{"schema":"install-snapshot/1","objects":[],"hook_bindings":[],"host_records":[]}'
    (snapshot / "snapshot.json").write_bytes(snapshot_bytes)
    marker = config.parent / "install-pending.json"
    marker.write_text(json.dumps({
        "schema": "install-pending/1", "snapshot_id": snapshot.name,
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }) + "\n", encoding="utf-8")
    marker_before = marker.read_bytes()
    snapshot_before = (snapshot / "snapshot.json").read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pending marker crossed the read-only install boundary")

    monkeypatch.setattr(installer_module, "_build_plan", forbidden)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY") as planned:
        plan_install(ROOT, config, state, ROOT, manifest)
    assert str(marker) in str(planned.value)
    with pytest.raises(ConfigError, match="FAIL_INSTALL_RECOVERY"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert marker.read_bytes() == marker_before
    assert (snapshot / "snapshot.json").read_bytes() == snapshot_before
    assert not install_root.exists()


def test_foreign_and_indeterminate_targets_coexist_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text(encoding="utf-8"))
    runtime = Path(payload["targets"][0]["root"])
    foreign = runtime / payload["targets"][0]["rules_target"]
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign target\n")
    wrong_type = install_root / "engine" / "0.1.0.dev9"
    wrong_type.parent.mkdir(parents=True)
    wrong_type.write_bytes(b"not a managed directory\n")
    before = {foreign: foreign.read_bytes(), wrong_type: wrong_type.read_bytes()}
    lines = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(" status=foreign " in line for line in lines)
    assert any(" status=indeterminate " in line for line in lines)
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    with pytest.raises(ConfigError, match="INSTALL_CONFLICT"):
        apply_install(ROOT, config, state, ROOT, manifest, force=False)
    assert {path: path.read_bytes() for path in before} == before
    assert not (config.parent / "rollback").exists()


def test_apply_detach_race_preserves_recreated_destination_and_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, install_root = installed_fixture(tmp_path, monkeypatch)
    initial = plan_install(ROOT, config, state, ROOT, manifest)
    initial_token = next(line.split(" ", 1)[1] for line in initial if line.startswith("PLAN_HASH "))
    apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=initial_token)

    source = tmp_path / "updated-engine"
    shutil.copytree(ROOT, source)
    (source / "NOTICE").write_text("agent-core\nCopyright 2026 Synthetic Update\n", encoding="utf-8")
    updated_manifest = tmp_path / "updated-release-manifest.json"
    updated_manifest.write_text(
        json.dumps(build_release_manifest(source), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    plan = installer_module._build_plan(ROOT, config, state, source, updated_manifest)
    plan, *_ignored = installer_module._reviewed_install_plan(plan)
    old_engine_hash = plan.object_preimages[0]
    assert old_engine_hash is not None
    receipt_before = plan.receipt_path.read_bytes()
    competitor = b"competitor-raced-bytes\n"
    original_move = installer_module._move_no_replace

    def recreate_before_place(source_path: Path, destination: Path) -> None:
        if destination == plan.engine_root and source_path.name.startswith(".engine-install-"):
            destination.mkdir(parents=True)
            (destination / "competitor.txt").write_bytes(competitor)
        original_move(source_path, destination)

    monkeypatch.setattr(installer_module, "_move_no_replace", recreate_before_place)
    with pytest.raises(ConfigError, match="^FAIL_INSTALL_RACE"):
        installer_module._apply_install_plan(plan, force=False)
    assert (plan.engine_root / "competitor.txt").read_bytes() == competitor
    snapshots = sorted((config.parent / "rollback").glob("install-*"))
    assert len(snapshots) == 2
    detached = next(snapshot / "detached" / "object-0" for snapshot in snapshots
                    if (snapshot / "detached" / "object-0").exists())
    assert installer_module._path_hash(detached, "dir") == old_engine_hash
    assert plan.receipt_path.read_bytes() == receipt_before


def test_apply_detached_preimage_mismatch_preserves_raced_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    initial = plan_install(ROOT, config, state, ROOT, manifest)
    initial_token = next(line.split(" ", 1)[1] for line in initial if line.startswith("PLAN_HASH "))
    apply_install(ROOT, config, state, ROOT, manifest, force=False, plan_hash=initial_token)
    source = tmp_path / "updated-engine"
    shutil.copytree(ROOT, source)
    (source / "NOTICE").write_text("agent-core\nCopyright 2026 Synthetic Update\n", encoding="utf-8")
    updated_manifest = tmp_path / "updated-release-manifest.json"
    updated_manifest.write_text(
        json.dumps(build_release_manifest(source), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    plan = installer_module._build_plan(ROOT, config, state, source, updated_manifest)
    plan, *_ignored = installer_module._reviewed_install_plan(plan)
    old_engine_hash = plan.object_preimages[0]
    assert old_engine_hash is not None
    raced_notice = b"raced-after-detach\n"
    original_move = installer_module._move_no_replace

    def alter_detached(source_path: Path, destination: Path) -> None:
        original_move(source_path, destination)
        if destination.name == "object-0" and destination.parent.name == "detached":
            (destination / "NOTICE").write_bytes(raced_notice)

    monkeypatch.setattr(installer_module, "_move_no_replace", alter_detached)
    with pytest.raises(ConfigError, match="^FAIL_INSTALL_RACE"):
        installer_module._apply_install_plan(plan, force=False)
    assert (plan.engine_root / "NOTICE").read_bytes() == raced_notice
    snapshot = next(item for item in (config.parent / "rollback").glob("install-*")
                    if installer_module._path_hash(item / "objects" / "0", "dir") == old_engine_hash)
    assert snapshot.is_dir()


def test_invalid_install_receipt_is_indeterminate_and_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, config, manifest, _install_root = installed_fixture(tmp_path, monkeypatch)
    apply_install(ROOT, config, state, ROOT, manifest, force=False)
    receipt_path = config.parent / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["objects"][0]["installed_sha256"] = "G" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = receipt_path.read_bytes()
    rollback_before = sorted((config.parent / "rollback").iterdir())
    lines = plan_install(ROOT, config, state, ROOT, manifest)
    assert any(" status=indeterminate " in line for line in lines)
    assert lines[-1] == "DRY_RUN writes=0 ready=false no_changes=false"
    assert receipt_path.read_bytes() == before
    assert sorted((config.parent / "rollback").iterdir()) == rollback_before
