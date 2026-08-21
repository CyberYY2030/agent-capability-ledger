from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from agent_core.config import ConfigError
from agent_core.installer import build_release_manifest
from agent_core.provenance import EngineLayout, _record, classify_engine_layout, validate_engine_provenance


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "-C", str(root), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def record_bytes(sequence: int, previous: str | None, tree: str, aggregate: str) -> bytes:
    return (json.dumps({
        "schema": "engine-provenance/1", "sequence": sequence,
        "previous_record_sha256": previous, "engine_tree_oid": tree,
        "release_artifact_sha256": aggregate,
    }, sort_keys=True, indent=2) + "\n").encode("utf-8")


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@invalid")
        self.engine = self.repo / "engine"
        (self.engine / "agent_core").mkdir(parents=True)
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "state").mkdir()
        (self.repo / "state" / "state.txt").write_text("state\n", encoding="utf-8")
        self.first = self.commit_record(1, None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit_record(self, sequence: int, previous: str | None, *, tree: str | None = None,
                      aggregate: str | None = None) -> bytes:
        manifest = build_release_manifest(self.engine)
        (self.engine / "release-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        git(self.repo, "add", "--", "engine")
        staged_root = git(self.repo, "write-tree")
        engine_tree = tree or git(self.repo, "rev-parse", f"{staged_root}:engine")
        raw = record_bytes(sequence, previous, engine_tree, aggregate or manifest["artifact_sha256"])
        (self.repo / "engine.provenance.json").write_bytes(raw)
        git(self.repo, "add", "--", "engine.provenance.json")
        git(self.repo, "commit", "-q", "-m", f"provenance {sequence}")
        return raw

    def test_sequence_one_and_sequence_two_with_state_only_commit_are_green(self) -> None:
        first = validate_engine_provenance(self.engine)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.record_sha256, hashlib.sha256(self.first).hexdigest())
        (self.repo / "state" / "state.txt").write_text("state two\n", encoding="utf-8")
        git(self.repo, "add", "--", "state/state.txt")
        git(self.repo, "commit", "-q", "-m", "state only")
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        second_raw = self.commit_record(2, hashlib.sha256(self.first).hexdigest())
        second = validate_engine_provenance(self.engine)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(second.record_sha256, hashlib.sha256(second_raw).hexdigest())

    def test_autocrlf_clone_hashes_head_blob_and_keeps_dirty_gate(self) -> None:
        self.assertFalse((self.repo / ".gitattributes").exists())
        git(self.repo, "add", "--", "state/state.txt")
        git(self.repo, "commit", "-q", "-m", "track state")
        clone = Path(self.temp.name) / "clone"
        subprocess.run(
            ["git", "-c", "core.autocrlf=true", "clone", "-q", str(self.repo), str(clone)],
            check=True, capture_output=True,
        )
        engine = clone / "engine"
        checkout = clone / "engine.provenance.json"
        checkout_raw = checkout.read_bytes()
        self.assertIn(b"\r\n", checkout_raw)
        self.assertEqual(git(clone, "status", "--short"), "")
        head_blob = subprocess.run(
            [
                "git", "-c", f"safe.directory={clone.resolve().as_posix()}",
                "-C", str(clone), "show", "HEAD:engine.provenance.json",
            ],
            check=True, capture_output=True,
        ).stdout
        self.assertNotIn(b"\r\n", head_blob)
        result = validate_engine_provenance(engine)
        self.assertEqual(result.record_sha256, hashlib.sha256(head_blob).hexdigest())

        changed = json.loads(checkout_raw)
        changed["sequence"] = 2
        checkout.write_text(
            json.dumps(changed, sort_keys=True, indent=2) + "\n",
            encoding="utf-8", newline="\r\n",
        )
        with self.assertRaisesRegex(
            ConfigError, "FAIL_ENGINE_PROVENANCE working tree dirty",
        ):
            validate_engine_provenance(engine)

    def test_layout_classification_is_proven_or_fail_closed(self) -> None:
        self.assertIs(classify_engine_layout(self.engine), EngineLayout.CANONICAL)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            classify_engine_layout(self.repo)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            classify_engine_layout(self.engine / "agent_core")
        standalone = Path(self.temp.name) / "standalone"
        standalone.mkdir()
        git(standalone, "init", "-q", "-b", "main")
        self.assertIs(classify_engine_layout(standalone), EngineLayout.STANDALONE)

    def test_strict_record_types_and_oid_lengths(self) -> None:
        valid = json.loads(record_bytes(1, None, "a" * 40, "A" * 43))
        _record(json.dumps(valid).encode("utf-8"))
        valid["engine_tree_oid"] = "b" * 64
        _record(json.dumps(valid).encode("utf-8"))
        for sequence in (True, False):
            value = dict(valid)
            value["sequence"] = sequence
            with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
                _record(json.dumps(value).encode("utf-8"))
        for size in (41, 63):
            value = dict(valid)
            value["engine_tree_oid"] = "c" * size
            with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
                _record(json.dumps(value).encode("utf-8"))

    def test_missing_untracked_dirty_and_manifest_tree_red(self) -> None:
        (self.repo / "engine.provenance.json").unlink()
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        git(self.repo, "checkout", "--", "engine.provenance.json")
        git(self.repo, "rm", "--cached", "--", "engine.provenance.json")
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        git(self.repo, "reset", "--hard", "HEAD")
        (self.engine / "agent_core" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        (self.engine / "agent_core" / "new.py").unlink()
        payload = json.loads((self.repo / "engine.provenance.json").read_text(encoding="utf-8"))
        payload["release_artifact_sha256"] = "A" * 43
        (self.repo / "engine.provenance.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)

    def test_chain_and_record_commit_facts_red(self) -> None:
        previous = hashlib.sha256(self.first).hexdigest()
        self.commit_record(3, previous)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        git(self.repo, "reset", "--hard", "HEAD~1")
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.commit_record(2, previous, tree="0" * 40)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)

    def test_duplicate_sequence_previous_hash_and_historical_manifest_red(self) -> None:
        previous = hashlib.sha256(self.first).hexdigest()
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.commit_record(1, None)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        git(self.repo, "reset", "--hard", "HEAD~1")
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 5\n", encoding="utf-8")
        self.commit_record(2, "f" * 64)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)
        git(self.repo, "reset", "--hard", "HEAD~1")
        (self.engine / "agent_core" / "sample.py").write_text("VALUE = 6\n", encoding="utf-8")
        self.commit_record(2, previous, aggregate="A" * 43)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.engine)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_engine_is_rejected(self) -> None:
        actual = self.repo / "engine-real"
        self.engine.rename(actual)
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.repo / "engine"), str(actual)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.repo / "engine")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_state_is_rejected(self) -> None:
        actual = self.repo / "state-real"
        (self.repo / "state").rename(actual)
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.repo / "state"), str(actual)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            classify_engine_layout(self.engine)

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior")
    def test_posix_symlink_engine_is_rejected(self) -> None:
        actual = self.repo / "engine-real"
        self.engine.rename(actual)
        (self.repo / "engine").symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "FAIL_ENGINE_PROVENANCE"):
            validate_engine_provenance(self.repo / "engine")


if __name__ == "__main__":
    unittest.main()
