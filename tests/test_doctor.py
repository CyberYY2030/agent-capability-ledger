from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import __version__
from agent_core.config import ConfigError
from agent_core import doctor as doctor_module
from agent_core.installer import build_release_manifest
from agent_core.provenance import EngineLayout
from agent_core.doctor import (
    assert_repository_separation,
    assert_remote_role,
    run,
)
from agent_core.privacy import DEFAULT_MAX_BLOB_BYTES


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def repo_with_remote(tmp_path: Path, url: str, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", url)
    return repo


def installed_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    install_root = tmp_path / "agent-core"
    config = tmp_path / "host" / "host.json"
    version = __version__
    engine = install_root / "engine" / version
    (engine / "agent_core").mkdir(parents=True)
    (engine / "agent_core" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = build_release_manifest(engine)
    (engine / "release-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    pin = {
        "schema": "engine-pin/1",
        "version": version,
        "artifact_sha256": manifest["artifact_sha256"],
        "config_path": str(config.resolve()),
    }
    pin_path = install_root / "engine-pin.json"
    pin_path.write_text(json.dumps(pin, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return engine, config, pin_path, pin


def test_remote_role_rejects_matching_hosted_identity_without_leaking_url(tmp_path: Path) -> None:
    token = "synthetic-token"
    engine_url = "https://automation:" + token + chr(64) + "github.com/frozen-owner/capability-ledger.git"
    state_url = "git" + chr(64) + "github.com:frozen-owner/capability-ledger.git"
    engine = repo_with_remote(tmp_path, engine_url, "engine")
    state = repo_with_remote(tmp_path, state_url, "state")

    with pytest.raises(ConfigError) as caught:
        assert_remote_role(engine, state)

    assert caught.value.code == "FAIL_ENGINE_STATE_REMOTE_OVERLAP"
    message = str(caught.value)
    assert "(github.com, frozen-owner, capability-ledger)" in message
    assert token not in message
    assert engine_url not in message
    assert state_url not in message


def test_remote_role_allows_different_owners_with_same_repository_name(tmp_path: Path) -> None:
    engine = repo_with_remote(
        tmp_path, "https://github.com/engine-owner/capability-ledger.git", "engine",
    )
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == [
        "PASS remote_role=verified "
        "engine=(github.com, engine-owner, capability-ledger) "
        "state=(github.com, state-owner, capability-ledger)",
    ]


def test_remote_role_marks_missing_engine_remote_unverified_or_red(tmp_path: Path) -> None:
    engine = repo_with_remote(tmp_path, "https://github.com/unused/unused.git", "engine")
    git(engine, "remote", "remove", "origin")
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == ["UNVERIFIED remote_role=engine_remote_missing"]
    with pytest.raises(ConfigError, match="FAIL_ENGINE_REMOTE_UNVERIFIED"):
        assert_remote_role(engine, state, require_versioned=True)


def test_remote_role_marks_non_repository_engine_unverified_or_red(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = repo_with_remote(
        tmp_path, "git" + chr(64) + "github.com:state-owner/capability-ledger.git", "state",
    )

    assert assert_remote_role(engine, state) == ["UNVERIFIED remote_role=engine_not_repository"]
    with pytest.raises(ConfigError, match="FAIL_ENGINE_REPOSITORY"):
        assert_remote_role(engine, state, require_versioned=True)


def test_doctor_rejects_engine_implementation_in_state(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    implementation = state / "agent_core" / "cli.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_CONTAINS_ENGINE"):
        run(engine, tmp_path / "missing-host.json", state, None)


def test_doctor_rejects_nested_engine_implementation_in_state(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    implementation = state / "agent-core" / "agent_core" / "cli.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_STATE_CONTAINS_ENGINE") as caught:
        run(engine, tmp_path / "missing-host.json", state, None)
    assert "agent-core/agent_core/cli.py" in str(caught.value).replace("\\", "/")


def test_doctor_rejects_machine_identity_in_engine(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    private_label = "DESKTOP-" + "PRIVATE123"
    (engine / "notes.txt").write_text(private_label + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_ENGINE_KNOWN_HOST_LABEL"):
        run(engine, tmp_path / "missing-host.json", None, None)


@pytest.mark.parametrize(
    ("name", "content", "rule_id"),
    [
        ("id_rsa", b"synthetic private key fixture\n", "private_key_file"),
        ("opaque.bin", b"before\x00after", "binary_unscanned"),
        ("large.txt", b"x" * (DEFAULT_MAX_BLOB_BYTES + 1), "oversize_unscanned"),
    ],
    ids=["private-key", "binary", "oversize"],
)
def test_doctor_surfaces_structural_engine_findings(
    tmp_path: Path, name: str, content: bytes, rule_id: str,
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / name).write_bytes(content)
    with pytest.raises(ConfigError, match="FAIL_ENGINE_PUBLIC_SCAN") as caught:
        run(engine, tmp_path / "missing-host.json", None, None)
    assert rule_id in str(caught.value)


def test_repository_separation_allows_private_enforcement_data(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    state = tmp_path / "state"
    verifier = state / "enforcement" / "verifiers.json"
    verifier.parent.mkdir(parents=True)
    verifier.write_text('{"schema":"verifier-manifest/1","verifiers":[]}\n', encoding="utf-8")
    assert_repository_separation(engine, state)


def test_doctor_canonical_layout_requires_provenance_before_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr(doctor_module, "classify_engine_layout", lambda _root: EngineLayout.CANONICAL)
    monkeypatch.setattr(
        doctor_module, "validate_engine_provenance",
        lambda _root: (_ for _ in ()).throw(ConfigError("FAIL_ENGINE_PROVENANCE", "synthetic")),
    )
    with pytest.raises(ConfigError, match="FAIL_ENGINE_PROVENANCE"):
        doctor_module.run(engine, tmp_path / "missing-host.json", None, None)


def test_doctor_installed_artifact_uses_pin_manifest_not_source_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, config, _pin_path, pin = installed_fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("source Git helper reached for installed artifact")

    monkeypatch.setattr(doctor_module, "classify_engine_layout", forbidden)
    monkeypatch.setattr(doctor_module, "validate_engine_provenance", forbidden)
    monkeypatch.setattr(doctor_module, "assert_remote_role", forbidden)
    monkeypatch.setattr(doctor_module, "load_config", lambda _path: {"targets": []})
    monkeypatch.setattr(
        doctor_module, "compose_manifests",
        lambda *_args: SimpleNamespace(composition_hash="c2", capabilities=()),
    )
    monkeypatch.setattr(doctor_module, "assert_capability_sources", lambda *_args: None)

    lines = doctor_module.run(engine, config, None, None, require_versioned=True)
    assert (
        f"PASS installed_artifact version={pin['version']} "
        f"artifact_sha256={pin['artifact_sha256']} pin=verified"
    ) in lines
    assert not any("engine_provenance" in line or "remote_role" in line for line in lines)


@pytest.mark.parametrize(
    "mutation",
    ["digest", "config", "version", "shape", "pin-missing", "pin-directory", "engine-alias"],
)
def test_doctor_installed_artifact_mismatch_fails_without_source_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    engine, config, pin_path, pin = installed_fixture(tmp_path)
    if mutation == "digest":
        pin["artifact_sha256"] = "A" * 43
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "config":
        pin["config_path"] = str((tmp_path / "other.json").resolve())
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "version":
        pin["version"] = "different-version"
        pin_path.write_text(json.dumps(pin), encoding="utf-8")
    elif mutation == "shape":
        moved = engine.parent.parent / "payload" / engine.name
        moved.parent.mkdir()
        engine.rename(moved)
        engine = moved
    elif mutation == "pin-missing":
        pin_path.unlink()
    elif mutation == "pin-directory":
        pin_path.unlink()
        pin_path.mkdir()
    else:
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink",
            lambda path: path == engine or original_is_symlink(path),
        )

    monkeypatch.setattr(
        doctor_module, "classify_engine_layout",
        lambda _root: (_ for _ in ()).throw(AssertionError("installed failure downgraded")),
    )
    with pytest.raises(
        ConfigError, match="^FAIL_INSTALLED_ARTIFACT verification failed$",
    ):
        doctor_module.run(engine, config, None, None)


def test_doctor_standalone_skips_provenance_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr(doctor_module, "classify_engine_layout", lambda _root: EngineLayout.STANDALONE)
    monkeypatch.setattr(
        doctor_module, "validate_engine_provenance",
        lambda _root: (_ for _ in ()).throw(AssertionError("provenance")),
    )
    with pytest.raises(ConfigError, match="FAIL_CONFIG"):
        doctor_module.run(engine, tmp_path / "missing-host.json", None, None)
