"""Stdlib coverage for standalone v1 and canonical v2 attachment evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_core.config import ConfigError
from agent_core.installer import build_release_manifest, plan_install
from agent_core.state import (
    apply_attach,
    apply_init,
    binding_receipt_path,
    plan_attach,
    validate_state_binding,
)


ENGINE = Path(__file__).resolve().parents[1]
EMAIL = "binding" + chr(64) + "invalid"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


def host_config(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = json.loads((ENGINE / "examples" / "host.example.json").read_text(encoding="utf-8"))
    payload["backup_root"] = str(root / "backups")
    for index, target in enumerate(payload["targets"]):
        target["root"] = str(root / f"runtime-{index}")
    path = root / "host.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


class BindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _init(self, repo: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "config", "user.name", "Synthetic Binding")
        git(repo, "config", "user.email", EMAIL)

    def _push_clone(self, source: Path, name: str) -> tuple[Path, Path]:
        remote = self.root / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        git(source, "remote", "add", "origin", str(remote))
        git(source, "push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        clone = self.root / f"{name}-clone"
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(remote), str(clone)], check=True)
        git(clone, "config", "core.autocrlf", "false")
        status = git(clone, "status", "--porcelain=v1", "--untracked-files=all").stdout
        if status:
            raise RuntimeError(f"synthetic clone is not clean: {status!r}")
        return clone, remote

    def _canonical(self, name: str = "canonical") -> tuple[Path, Path, Path]:
        source = self.root / f"{name}-source"
        source.mkdir()
        self._init(source)
        shutil.copytree(ENGINE, source / "engine", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ENGINE.parent / "state", source / "state", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        payload = build_release_manifest(source / "engine")
        (source / "engine" / "release-manifest.json").write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        git(source, "add", "engine", "state")
        staged = git(source, "write-tree").stdout.strip()
        tree = git(source, "rev-parse", f"{staged}:engine").stdout.strip()
        record = {
            "schema": "engine-provenance/1",
            "sequence": 1,
            "previous_record_sha256": None,
            "engine_tree_oid": tree,
            "release_artifact_sha256": payload["artifact_sha256"],
        }
        (source / "engine.provenance.json").write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        git(source, "add", "engine.provenance.json")
        git(source, "commit", "-q", "-m", "canonical fixture")
        clone, remote = self._push_clone(source, name)
        config = host_config(self.root / f"{name}-host")
        apply_attach(clone / "state", config, confirm_private_remote=True)
        return clone, config, remote

    def _standalone(self) -> tuple[Path, Path]:
        source = self.root / "standalone-source"
        apply_init(ENGINE, source, git_name="Synthetic Binding", git_email=EMAIL)
        clone, _remote = self._push_clone(source, "standalone")
        config = host_config(self.root / "standalone-host")
        apply_attach(clone, config, confirm_private_remote=True)
        return clone, config

    def test_canonical_v2_and_standalone_v1_green(self) -> None:
        canonical, config, _remote = self._canonical()
        receipt = json.loads(binding_receipt_path(config).read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "state-binding/2")
        self.assertEqual(set(receipt), {
            "schema", "state_root", "remote_name", "remote_url_sha256", "remote_revision",
            "state_lock_sha256", "config_sha256", "confirmed_private_remote",
            "repository_root_sha", "engine_provenance_sha256",
        })
        evidence = validate_state_binding(canonical / "state", config)
        self.assertEqual(evidence.layout, "canonical")
        self.assertEqual(evidence.schema, "state-binding/2")
        self.assertEqual(len(evidence.repository_root_sha or ""), 40)
        self.assertEqual(len(evidence.engine_provenance_sha256 or ""), 64)
        install_lines = plan_install(
            canonical / "engine", config, canonical / "state", canonical / "engine",
            canonical / "engine" / "release-manifest.json",
        )
        self.assertTrue(install_lines[-1].startswith("DRY_RUN writes=0"))

        standalone, standalone_config = self._standalone()
        standalone_receipt = json.loads(binding_receipt_path(standalone_config).read_text(encoding="utf-8"))
        self.assertEqual(standalone_receipt["schema"], "state-binding/1")
        self.assertEqual(validate_state_binding(standalone, standalone_config).layout, "standalone")

    def test_plan_attach_returns_current_revision_without_writes(self) -> None:
        canonical, config, _remote = self._canonical()
        canonical_before = (config.read_bytes(), binding_receipt_path(config).read_bytes())
        canonical_sha = git(canonical, "rev-parse", "origin/main").stdout.strip()
        canonical_plan = plan_attach(canonical / "state", config, confirm_private_remote=True)
        self.assertIn(f"EXPECTED_REMOTE_SHA {canonical_sha}", canonical_plan)
        self.assertEqual(canonical_plan[-1], "DRY_RUN writes=0")
        self.assertEqual((config.read_bytes(), binding_receipt_path(config).read_bytes()), canonical_before)

        standalone, standalone_config = self._standalone()
        standalone_before = (standalone_config.read_bytes(), binding_receipt_path(standalone_config).read_bytes())
        standalone_sha = git(standalone, "rev-parse", "origin/main").stdout.strip()
        standalone_plan = plan_attach(standalone, standalone_config, confirm_private_remote=True)
        self.assertIn(f"EXPECTED_REMOTE_SHA {standalone_sha}", standalone_plan)
        self.assertEqual(standalone_plan[-1], "DRY_RUN writes=0")
        self.assertEqual((standalone_config.read_bytes(), binding_receipt_path(standalone_config).read_bytes()), standalone_before)

    def test_schema_and_pin_mutations_fail_closed_without_url_disclosure(self) -> None:
        canonical, config, _remote = self._canonical()
        path = binding_receipt_path(config)
        original = path.read_bytes()
        remote_url = git(canonical / "state", "remote", "get-url", "origin").stdout.strip()
        receipt = json.loads(original)
        receipt["schema"] = "state-binding/1"
        receipt.pop("repository_root_sha")
        receipt.pop("engine_provenance_sha256")
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config)

        standalone, standalone_config = self._standalone()
        standalone_path = binding_receipt_path(standalone_config)
        standalone_receipt = json.loads(standalone_path.read_text(encoding="utf-8"))
        standalone_receipt.update({
            "schema": "state-binding/2",
            "repository_root_sha": "0" * 40,
            "engine_provenance_sha256": "0" * 64,
        })
        standalone_path.write_text(json.dumps(standalone_receipt), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(standalone, standalone_config)
        path.write_bytes(original)
        receipt = json.loads(original)
        receipt["engine_provenance_sha256"] = "0" * 64
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING") as raised:
            validate_state_binding(canonical / "state", config)
        self.assertNotIn(remote_url, str(raised.exception))
        path.write_bytes(original)
        config_original = config.read_bytes()
        config.write_bytes(config_original + b"\n")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config)
        config.write_bytes(config_original)
        inside_config = canonical / "host.json"
        inside_receipt = binding_receipt_path(inside_config)
        inside_config.write_bytes(config_original)
        inside_receipt.write_bytes(original)
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", inside_config)
        lock = canonical / "state" / "agent-core.lock.json"
        lock_original = lock.read_bytes()
        lock.write_bytes(lock_original + b"\n")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config)
        lock.write_bytes(lock_original)
        git(canonical / "state", "config", "remote.origin.pushurl", remote_url + "-different")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config)
        git(canonical / "state", "config", "--unset-all", "remote.origin.pushurl")
        git(canonical / "state", "remote", "set-url", "--add", "origin", remote_url + "-second")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config)

    def test_remote_fast_forward_can_be_identity_checked_without_clean_snapshot(self) -> None:
        canonical, config, remote = self._canonical("record")
        original_remote = git(canonical, "rev-parse", "origin/main").stdout.strip()
        writer = self.root / "writer"
        subprocess.run(["git", "clone", "-q", str(remote), str(writer)], check=True)
        git(writer, "config", "user.name", "Writer")
        git(writer, "config", "user.email", EMAIL)
        (writer / "state" / "rules" / "advanced.md").write_text("synthetic\n", encoding="utf-8")
        git(writer, "add", "state/rules/advanced.md")
        git(writer, "commit", "-q", "-m", "state fast forward")
        git(writer, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config, require_clean_snapshot=False)
        git(canonical, "fetch", "origin")
        evidence = validate_state_binding(canonical / "state", config, require_clean_snapshot=False)
        self.assertNotEqual(evidence.remote_revision, json.loads(binding_receipt_path(config).read_text(encoding="utf-8"))["remote_revision"])
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", config, require_clean_snapshot=True)
        git(canonical, "merge", "--ff-only", "origin/main")
        apply_attach(canonical / "state", config, confirm_private_remote=True)
        subprocess.run(
            ["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", original_remote],
            check=True,
        )
        git(canonical, "fetch", "origin")
        with self.assertRaisesRegex(ConfigError, "advertised revision is not accepted lineage"):
            validate_state_binding(canonical / "state", config, require_clean_snapshot=False)

    def test_advertised_engine_and_root_provenance_changes_are_rejected(self) -> None:
        canonical, config, remote = self._canonical("provenance")
        engine_writer = self.root / "engine-writer"
        subprocess.run(["git", "clone", "-q", str(remote), str(engine_writer)], check=True)
        git(engine_writer, "config", "user.name", "Writer")
        git(engine_writer, "config", "user.email", EMAIL)
        notice = engine_writer / "engine" / "NOTICE"
        notice.write_bytes(notice.read_bytes() + b"remote engine change\n")
        git(engine_writer, "add", "engine/NOTICE")
        git(engine_writer, "commit", "-q", "-m", "engine drift")
        git(engine_writer, "push", "-q", "origin", "main")
        git(canonical, "fetch", "origin")
        with self.assertRaisesRegex(ConfigError, "advertised engine tree changed"):
            validate_state_binding(canonical / "state", config, require_clean_snapshot=False)

        canonical, config, remote = self._canonical()
        record_writer = self.root / "record-writer"
        subprocess.run(["git", "clone", "-q", str(remote), str(record_writer)], check=True)
        git(record_writer, "config", "user.name", "Writer")
        git(record_writer, "config", "user.email", EMAIL)
        record = record_writer / "engine.provenance.json"
        record.write_bytes(record.read_bytes() + b"\n")
        git(record_writer, "add", "engine.provenance.json")
        git(record_writer, "commit", "-q", "-m", "root provenance drift")
        git(record_writer, "push", "-q", "origin", "main")
        git(canonical, "fetch", "origin")
        with self.assertRaisesRegex(ConfigError, "advertised engine provenance changed"):
            validate_state_binding(canonical / "state", config, require_clean_snapshot=False)

    def test_host_identity_uses_resolved_single_link_files(self) -> None:
        canonical, config, _remote = self._canonical()
        relative_config = config.parent / ".." / config.parent.name / config.name
        self.assertEqual(validate_state_binding(canonical / "state", relative_config).layout, "canonical")
        config_link = canonical / "host-config-link.json"
        receipt_link = canonical / "binding-receipt-link.json"
        try:
            os.link(config, config_link)
            with self.assertRaisesRegex(ConfigError, "host config must have one link"):
                validate_state_binding(canonical / "state", config)
            config_link.unlink()
            os.link(binding_receipt_path(config), receipt_link)
            with self.assertRaisesRegex(ConfigError, "binding receipt must have one link"):
                validate_state_binding(canonical / "state", config)
        except OSError as exc:
            self.skipTest(f"hardlink support unavailable: {exc.__class__.__name__}")
        finally:
            config_link.unlink(missing_ok=True)
            receipt_link.unlink(missing_ok=True)
        self.assertEqual(validate_state_binding(canonical / "state", config).layout, "canonical")

    @unittest.skipUnless(os.name == "nt", "Windows junction coverage")
    def test_windows_junction_state_root_is_rejected(self) -> None:
        canonical, config, _remote = self._canonical()
        host_alias = self.root / "host-alias"
        host_link = subprocess.run(["cmd", "/c", "mklink", "/J", str(host_alias), str(canonical)], capture_output=True)
        if host_link.returncode != 0:
            self.skipTest("junction creation unavailable")
        alias_config = host_alias / "host.json"
        alias_receipt = binding_receipt_path(alias_config)
        alias_config.write_bytes(config.read_bytes())
        alias_receipt.write_bytes(binding_receipt_path(config).read_bytes())
        with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
            validate_state_binding(canonical / "state", alias_config)
        outside = self.root / "outside"
        outside.mkdir()
        state = canonical / "state"
        saved = canonical / "state-real"
        state.rename(saved)
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(state), str(outside)], capture_output=True)
        if made.returncode != 0:
            self.skipTest("junction creation unavailable")
        with self.assertRaises(ConfigError) as raised:
            validate_state_binding(state, config)
        self.assertEqual((raised.exception.code, raised.exception.detail),
                         ("FAIL_STATE_BINDING", "binding validation failed"))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(str(state), str(raised.exception))
        self.assertNotIn(str(state), repr(raised.exception))


if __name__ == "__main__":
    unittest.main()
