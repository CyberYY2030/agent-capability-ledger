from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from agent_core.config import ConfigError
from agent_core.freshness import load_candidate, parse_candidate_bytes
from agent_core.installer import build_release_manifest
from agent_core import cli as cli_module
from agent_core.promote import (
    BlobMove,
    ChangeExpectation,
    Prepared,
    _assert_cached_scope,
    _assert_committed_scope,
    _assert_blob_moves,
    _capture_precommit_facts,
    _cleanup_worktree,
    _commit,
    _orphan_snapshot_ids,
    _read_journal,
    append_journal_event,
    create_canonical_journal,
    create_canonical_snapshot,
    _new_worktree,
    _parse_name_status,
    _validate_changed_paths,
    apply_canonical_recovery,
    apply_prepared,
    plan_advance,
    plan_canonical_recovery,
    plan_promote,
    plan_publish,
    prepare_promote,
    prepare_publish,
    prepare_advance,
    recover_local,
    rollback,
)
from agent_core.repository import resolve_repository_context
from agent_core.retire import LessonRecord, ReceiptCheck
from agent_core import retire as retire_module
from agent_core import state as state_module
from agent_core.state import apply_attach, binding_receipt_path
from agent_core import promote as promote_module


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "-C", str(root), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def candidate(state: Path, item_id: str, base: str, *, scope_hint: str = "global") -> bytes:
    path = state / "inbox" / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "candidate/1", "id": item_id, "created_utc": "2026-08-15T00:00:00Z",
        "host": "desk", "agent": "codex", "base_revision": base,
        "rule": "Synthetic monorepo rule", "trigger": "synthetic transaction",
        "cost": "synthetic loss", "sink": "checks/synthetic.md", "scope_hint": scope_hint,
        "evidence": "synthetic:test",
    }
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


class MonorepoTransactionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.local_appdata = base / "local-appdata"
        self.local_appdata.mkdir()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local_appdata)})
        self.environment.start()
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.seed = base / "seed"
        self.seed.mkdir()
        git(self.seed, "init", "-q", "-b", "main")
        git(self.seed, "config", "core.autocrlf", "false")
        git(self.seed, "config", "user.name", "Test")
        git(self.seed, "config", "user.email", "test@invalid")
        shutil.copytree(ENGINE_ROOT, self.seed / "engine", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ENGINE_ROOT.parent / "state", self.seed / "state", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = build_release_manifest(self.seed / "engine")
        (self.seed / "engine" / "release-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )
        git(self.seed, "add", "engine", "state")
        staged = git(self.seed, "write-tree")
        engine_tree = git(self.seed, "rev-parse", f"{staged}:engine")
        (self.seed / "engine.provenance.json").write_text(json.dumps({
            "schema": "engine-provenance/1", "sequence": 1, "previous_record_sha256": None,
            "engine_tree_oid": engine_tree, "release_artifact_sha256": manifest["artifact_sha256"],
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (self.seed / "obsolete.txt").write_text("obsolete\n", encoding="utf-8")
        git(self.seed, "add", "engine.provenance.json", "obsolete.txt")
        git(self.seed, "commit", "-q", "-m", "seed")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-q", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.repo = base / "clone"
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(self.remote), str(self.repo)], check=True)
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@invalid")
        self.repo = Path(git(self.repo, "rev-parse", "--show-toplevel")).resolve()
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.state = self.repo / "state"
        self.control = base / "control"
        self.config = base / "host" / "host.json"
        self.config.parent.mkdir()
        host = json.loads((ENGINE_ROOT / "examples" / "host.example.json").read_text(encoding="utf-8"))
        host["backup_root"] = str(base / "backups")
        for index, target in enumerate(host["targets"]):
            target["root"] = str(base / f"runtime-{index}")
        self.config.write_text(json.dumps(host, indent=2) + "\n", encoding="utf-8")
        apply_attach(self.state, self.config, confirm_private_remote=True)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def item_id(self, suffix: str) -> str:
        return f"desk-20260815T000000Z-{suffix * 32}"

    def cleanup(self, prepared) -> None:
        if prepared.txn.exists():
            _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
        self.assertFalse(prepared.txn.exists())

    def refresh_canonical_fixture_binding(self) -> None:
        """Advance the synthetic clone to its local bare remote, then rebuild its real binding receipt."""
        inbox = self.state / "inbox"
        for source in inbox.glob("desk-*.md"):
            if (not source.is_file()
                    or re.fullmatch(r"desk-\d{8}T\d{6}Z-[0-9a-f]{32}\.md", source.name) is None):
                continue
            relative = source.relative_to(self.repo).as_posix()
            if not git(self.repo, "ls-files", "--error-unmatch", "--", relative, check=False):
                source.unlink()
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "merge", "-q", "--ff-only", "origin/main")
        apply_attach(self.state, self.config, confirm_private_remote=True)

    def recovery_source(self, suffix: str, *, push_attempt: bool = True):
        """Create one immutable original apply source without using canonical apply."""
        self.refresh_canonical_fixture_binding()
        item = self.item_id(suffix)
        candidate(self.state, item, git(self.repo, "rev-parse", "origin/main"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
        )
        snapshot = create_canonical_snapshot(prepared)
        journal = create_canonical_journal(prepared, snapshot)
        if push_attempt:
            journal = append_journal_event(journal, "push_attempt", {"phase": "push"}, prepared=prepared)
        return prepared, snapshot, journal

    def rollback_recovery_source(self, suffix: str, *, push_attempt: bool = True):
        """C2b rollback fixtures use a legal candidate suffix; broader recovery tests retain their legacy labels."""
        if len(suffix) != 1 or suffix not in "0123456789abcdef":
            raise AssertionError(f"rollback fixture suffix must be one hex character: {suffix!r}")
        return self.recovery_source(suffix, push_attempt=push_attempt)

    def rollback_source(self, suffix: str):
        """Create a settled canonical source whose immutable snapshot can be reviewed for rollback."""
        prepared, snapshot, journal = self.rollback_recovery_source(suffix)
        try:
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            expected = self.state / "inbox" / f"{prepared.candidate_id}.md"
            source = prepared.source_path
            if source is None or source.resolve() != expected.resolve():
                raise AssertionError("rollback fixture source path")
            observed = os.lstat(source)
            raw = source.read_bytes()
            committed = promote_module._tree_blob(
                self.repo, prepared.sha, f"state/inbox/{prepared.candidate_id}.md",
            )
            if (promote_module._is_reparse_alias(source) or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1 or raw != prepared.source_content
                    or hashlib.sha256(raw).hexdigest() != prepared.input_digest_sha256
                    or committed != ("100644", hashlib.sha1(
                        b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
                    ).hexdigest(), raw)):
                raise AssertionError("rollback fixture source identity")
            source.unlink()
            if os.path.lexists(source):
                raise AssertionError("rollback fixture source removal")
            git(self.repo, "fetch", "-q", "origin")
            git(self.repo, "merge", "-q", "--ff-only", "origin/main")
            journal = append_journal_event(journal, "completed", {"phase": "completed"}, prepared=prepared)
            return prepared, snapshot, journal
        except Exception:
            if prepared.txn.exists():
                _cleanup_worktree(prepared.repo, prepared.control_root, prepared.txn)
            raise

    def rollback_promote_source(self, suffix: str):
        """Create a settled promote source with original A/M/D facts for rollback direction tests."""
        if len(suffix) != 1 or suffix not in "0123456789abcdef":
            raise AssertionError(f"rollback fixture suffix must be one hex character: {suffix!r}")
        item = self.item_id(suffix)
        candidate(self.seed / "state", item, git(self.seed, "rev-parse", "HEAD"))
        git(self.seed, "add", "state")
        git(self.seed, "commit", "-q", "-m", "published candidate")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        apply_attach(self.state, self.config, confirm_private_remote=True)
        plan = plan_promote(self.state, None, item, force_new=True,
                            reviewed_against=git(self.repo, "rev-parse", "origin/main"), config_path=self.config)
        prepared = prepare_promote(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
        )
        snapshot = create_canonical_snapshot(prepared)
        journal = create_canonical_journal(prepared, snapshot)
        journal = append_journal_event(journal, "push_attempt", {"phase": "push"}, prepared=prepared)
        git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "merge", "-q", "--ff-only", "origin/main")
        journal = append_journal_event(journal, "completed", {"phase": "completed"}, prepared=prepared)
        return prepared, snapshot, journal

    def rewrite_rollback_source(self, snapshot, journal):
        """Forge a schema-valid rollback-of-rollback source from immutable test artifacts only."""
        manifest_path = snapshot.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["operation"] = "rollback"
        manifest["candidate_id"] = snapshot.snapshot_id
        local_input = snapshot.path / "local-input.bin"
        local_stat = os.lstat(local_input)
        if (promote_module._is_reparse_alias(snapshot.path) or promote_module._is_reparse_alias(local_input)
                or not stat.S_ISREG(local_stat.st_mode) or local_stat.st_nlink != 1):
            raise AssertionError("rollback fixture local input identity")
        local_input.unlink()
        if os.path.lexists(local_input):
            raise AssertionError("rollback fixture local input removal")
        manifest["local_input"] = None
        manifest["manifest_sha256"] = promote_module._record_hash(manifest, "manifest_sha256")
        manifest_path.write_bytes(promote_module._canonical_bytes(manifest))
        records = tuple(json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines())
        rewritten = []
        previous = None
        for index, record in enumerate(records):
            current = dict(record)
            if index == 0:
                current["operation"] = "rollback"
                current["candidate_id"] = snapshot.snapshot_id
                current["snapshot_manifest_sha256"] = manifest["manifest_sha256"]
            current["previous_record_sha256"] = previous
            current["record_sha256"] = promote_module._record_hash(current, "record_sha256")
            previous = current["record_sha256"]
            rewritten.append(current)
        journal.path.write_bytes(b"".join(promote_module._canonical_bytes(record) + b"\n" for record in rewritten))
        return (
            promote_module.SnapshotRef(snapshot.snapshot_id, snapshot.path, manifest["manifest_sha256"]),
            promote_module.JournalRef(journal.operation_id, journal.path, len(rewritten) - 1,
                                     rewritten[-1]["record_sha256"], journal.control_identity_sha256),
        )

    def duplicate_original_journal(self, journal) -> Path:
        """Create a second hash-valid original journal only for ambiguity mutation tests."""
        duplicate = journal.path.parent / f"{promote_module._artifact_id()}.jsonl"
        records = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        records[0]["operation_id"] = duplicate.stem
        previous = None
        for record in records:
            record["previous_record_sha256"] = previous
            record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
            previous = record["record_sha256"]
        duplicate.write_bytes(b"".join(promote_module._canonical_bytes(record) + b"\n" for record in records))
        return duplicate

    def rewrite_legacy_rollback_candidate(self, snapshot, journal):
        """Keep a rollback snapshot/journal self-consistent while exercising its canonical-only ID gate."""
        legacy = "20260820T000000000000Z"
        manifest_path = snapshot.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["candidate_id"] = legacy
        manifest["manifest_sha256"] = promote_module._record_hash(manifest, "manifest_sha256")
        manifest_path.write_bytes(promote_module._canonical_bytes(manifest))
        records = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
        records[0]["candidate_id"] = legacy
        records[0]["snapshot_manifest_sha256"] = manifest["manifest_sha256"]
        previous = None
        for record in records:
            record["previous_record_sha256"] = previous
            record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
            previous = record["record_sha256"]
        journal.path.write_bytes(b"".join(promote_module._canonical_bytes(record) + b"\n" for record in records))
        return (
            promote_module.SnapshotRef(snapshot.snapshot_id, snapshot.path, manifest["manifest_sha256"]),
            promote_module.JournalRef(journal.operation_id, journal.path, len(records) - 1,
                                     records[-1]["record_sha256"], journal.control_identity_sha256),
        )

    def recovery_checkpoint(self, prepared, snapshot, original, action: str = "artifact-cleanup", *,
                            observed_sha: str | None = None, original_operation: str | None = None):
        binding = dict(prepared.binding or ())
        operation = original_operation or prepared.operation
        return promote_module.create_recovery_journal(prepared.control_root, {
            "original_operation_id": original.operation_id, "original_operation": operation,
            "target_sha": prepared.sha, "expected_base_sha": prepared.expected_remote_sha,
            "original_journal_final_record_sha256": original.record_sha256,
            "snapshot_id": snapshot.snapshot_id, "snapshot_manifest_sha256": snapshot.manifest_sha256,
            "snapshot_input_sha256": prepared.input_digest_sha256 if operation == "publish" else None,
            "plan_hash": prepared.plan_hash,
            "binding_digest_sha256": prepared.binding_digest_sha256,
            "lineage_root_sha": binding["repository_root_sha"],
            "control_identity_sha256": promote_module.hashlib.sha256(
                binding["control_identity"].encode()).hexdigest(),
            "control_filesystem_sha256": binding["control_filesystem_sha256"],
            "target_ref": promote_module.CANONICAL_TARGET_REF,
            "confirmed_observed_sha": observed_sha or prepared.expected_remote_sha, "action": action,
        })

    def recovery_checkpoint_details(self, prepared, event: str, *, observed_sha: str | None = None) -> dict[str, str]:
        if event in {"converged", "completed", "failed"}:
            return {}
        if event == "cleanup-pending":
            return {"kind": "worktree-cleanup"}
        role = promote_module.RECOVERY_EVENT_ROLES[event]
        if event in {"source-quarantine-intent", "source-quarantined", "source-restore-intent",
                     "source-restored", "source-preserved", "quarantine-delete-intent", "quarantine-deleted"}:
            return {"role": role, "input_sha256": prepared.input_digest_sha256,
                    "handle_identity_sha256": "a" * 64}
        if event in {"fast-forward-intent", "fast-forward-done", "pointer-updated"}:
            return {"role": role, "sha": observed_sha or prepared.expected_remote_sha}
        return {"role": role, "handle_identity_sha256": "a" * 64}

    def append_recovery_checkpoint(self, prepared, checkpoint, event: str, *, observed_sha: str | None = None):
        return promote_module.append_recovery_journal_event(
            checkpoint, event, self.recovery_checkpoint_details(prepared, event, observed_sha=observed_sha),
        )

    def canonical_advance_receipt(self) -> Path:
        state = self.seed / "state"
        (state / "enforcement").mkdir(parents=True)
        checks = state / "checks"
        checks.mkdir()
        (checks / "probe.py").write_text("# synthetic verifier input\n", encoding="utf-8")
        (checks / "negative.txt").write_text("invalid\n", encoding="utf-8")
        (checks / "positive.txt").write_text("valid\n", encoding="utf-8")
        (checks / "consumer.txt").write_text("L-1 synthetic consumer\n", encoding="utf-8")
        manifest = {
            "schema": "verifier/1",
            "verifiers": [{
                "id": "synthetic-contract",
                "negative": {"argv": ["<PYTHON>", "checks/probe.py", "negative", "checks/negative.txt"]},
                "positive": {"argv": ["<PYTHON>", "checks/probe.py", "positive", "checks/positive.txt"]},
                "cwd_ref": "state",
                "timeout_sec": 10,
                "consumers": [{
                    "path": "checks/consumer.txt",
                    "consumer_probe": {"argv": ["<PYTHON>", "checks/probe.py", "consumer", "checks/consumer.txt", "L-1"]},
                }],
                "enforcement_scope": ["checks/**"],
            }],
        }
        (state / "enforcement" / "verifiers.json").write_bytes(retire_module._canonical_bytes(manifest))
        ledger_path = state / "experience" / "LESSONS.md"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(
            "# Lessons Ledger\n<!-- lessons-schema: lessons-ledger/2 -->\n"
            "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
            "- **L-1 [checklist·通用] Synthetic rule.** 触发: synthetic. 代价: synthetic. "
            "verifier: synthetic-contract. sink → tests/enforcement/consumer.txt.\n\n## 归档\n",
            encoding="utf-8",
        )
        git(self.seed, "add", "state")
        git(self.seed, "commit", "-q", "-m", "canonical advance verifier fixture")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        verifier = retire_module.load_verifiers(self.state)["synthetic-contract"]
        payload = {
            "schema": "evidence/1", "lesson_id": "L-1", "from_status": "checklist",
            "to_status": "enforced", "verifier_id": verifier["id"],
            "verifier_sha256": retire_module._sha256(retire_module._canonical_bytes(verifier)),
            "enforcement_scope": verifier["enforcement_scope"],
            "inputs": retire_module._related_inputs(self.state, verifier),
            "runs": [
                {"kind": "negative", "argv": verifier["negative"]["argv"], "exit_code": 1,
                 "output_sha256": "0" * 64, "output_chars": 0, "truncated": False},
                {"kind": "positive", "argv": verifier["positive"]["argv"], "exit_code": 0,
                 "output_sha256": "0" * 64, "output_chars": 0, "truncated": False},
                {"kind": "consumer", "argv": verifier["consumers"][0]["consumer_probe"]["argv"], "exit_code": 0,
                 "output_sha256": "0" * 64, "output_chars": 0, "truncated": False},
            ],
            "verified_revision": "synthetic", "verified_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "engine_version": retire_module.__version__,
        }
        raw = retire_module._canonical_bytes(payload)
        receipt = Path(self.temp.name) / f"{retire_module._sha256(raw)}.json"
        receipt.write_bytes(raw)
        return receipt

    def test_publish_prepare_uses_prefixed_state_path_and_preserves_engine(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            self.assertEqual(prepared.target_ref, "refs/heads/main")
            self.assertEqual(prepared.config_path, self.config.resolve())
            self.assertEqual(prepared.changed_paths, (f"state/inbox/{item}.md",))
            self.assertEqual(prepared.plan_hash, plan.plan_hash)
            self.assertEqual(prepared.input_digest_sha256, plan.payload["source_sha256"])
            self.assertEqual(dict(prepared.binding or ()), plan.payload["binding"])
            self.assertEqual(prepared.tree_oid,
                             git(prepared.txn, "rev-parse", f"{prepared.sha}^{{tree}}"))
            self.assertEqual(
                git(prepared.txn, "rev-parse", f"{prepared.expected_remote_sha}:engine"),
                git(prepared.txn, "rev-parse", f"{prepared.sha}:engine"),
            )
            self.assertEqual(git(prepared.txn, "diff", "--name-only", prepared.expected_remote_sha, prepared.sha), f"state/inbox/{item}.md")
        finally:
            self.cleanup(prepared)

    def test_canonical_snapshot_and_journal_are_durable_and_append_only(self) -> None:
        item = self.item_id("a")
        source = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        self.assertEqual(plan.payload["target_ref"], "refs/heads/main")
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            snapshot = create_canonical_snapshot(prepared)
            manifest = json.loads((snapshot.path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["local_input"]["sha256"], plan.payload["source_sha256"])
            self.assertEqual((snapshot.path / "local-input.bin").read_bytes(), source)
            if os.name != "nt":
                self.assertGreaterEqual(snapshot.path.stat().st_nlink, 2)
            self.assertEqual(_orphan_snapshot_ids(prepared.control_root), (snapshot.snapshot_id,))
            orphan_plan = plan_publish(self.state, None, item, config_path=self.config)
            self.assertIn(f"ORPHAN_SNAPSHOT {snapshot.snapshot_id}", orphan_plan.lines)
            journal = create_canonical_journal(prepared, snapshot)
            self.assertEqual(_orphan_snapshot_ids(prepared.control_root), ())
            self.assertNotIn(f"ORPHAN_SNAPSHOT {snapshot.snapshot_id}",
                             plan_publish(self.state, None, item, config_path=self.config).lines)
            baseline = journal.path.read_bytes()
            for operation, candidate_id, accepted in (
                ("advance", "L-31", True),
                ("advance", "bad/path", False),
                ("rollback", "20260816T000000000000Z", True),
                ("rollback", "20260816T000000000000Z-aaaaaaaaaaaaaaaa", True),
                ("rollback", "20260816T000000000000Z-aaaaaaaaaaaaaaa", False),
            ):
                record = json.loads(baseline)
                record["operation"] = operation
                record["candidate_id"] = candidate_id
                record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
                journal.path.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                if accepted:
                    _read_journal(journal.path, journal.path.parent)
                else:
                    with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                        _read_journal(journal.path, journal.path.parent)
            journal.path.write_bytes(baseline)
            stale = journal
            journal = append_journal_event(journal, "preflight_ok", {"phase": "test"}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(stale, "snapshot_durable", {}, prepared=prepared)
            outside = replace(journal, path=Path(self.temp.name) / "outside.jsonl")
            outside.path.write_bytes(journal.path.read_bytes())
            before = outside.path.read_bytes()
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _read_journal(outside.path, journal.path.parent)
            self.assertEqual(outside.path.read_bytes(), before)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(outside, "completed", {}, prepared=prepared)
            self.assertEqual(outside.path.read_bytes(), before)
            records = _read_journal(journal.path, journal.path.parent)
            self.assertEqual((records[0]["sequence"], records[1]["sequence"]), (0, 1))
            self.assertEqual(records[1]["previous_record_sha256"], records[0]["record_sha256"])
            journal_text = journal.path.read_text(encoding="utf-8")
            self.assertNotIn(git(self.repo, "remote", "get-url", "origin"), journal_text)
            self.assertNotIn(str(self.config.resolve()), journal_text)
            self.assertNotIn(str(prepared.control_root), journal_text)
            self.assertNotIn(str(prepared.source_path), journal_text)
            valid = journal.path.read_bytes()
            bool_records = [json.loads(line) for line in valid.splitlines()]
            bool_records[1]["sequence"] = True
            bool_records[1]["record_sha256"] = promote_module._record_hash(bool_records[1], "record_sha256")
            bool_mutation = b"\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8") for item in bool_records) + b"\n"
            bad_event_records = [json.loads(line) for line in valid.splitlines()]
            bad_event_records[1]["event"] = "bad_event"
            bad_event_records[1]["record_sha256"] = promote_module._record_hash(bad_event_records[1], "record_sha256")
            bad_event_mutation = b"\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8") for item in bad_event_records) + b"\n"
            bad_details_records = [json.loads(line) for line in valid.splitlines()]
            bad_details_records[1]["details"] = {"status": "https://example.invalid/private"}
            bad_details_records[1]["record_sha256"] = promote_module._record_hash(bad_details_records[1], "record_sha256")
            path_details_records = [json.loads(line) for line in valid.splitlines()]
            path_details_records[1]["details"] = {"status": "C:" + "/" + "private/source"}
            path_details_records[1]["record_sha256"] = promote_module._record_hash(path_details_records[1], "record_sha256")
            encode_records = lambda rows: b"\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8") for item in rows) + b"\n"
            mutations = (valid[:-1], bool_mutation, bad_event_mutation, encode_records(bad_details_records), encode_records(path_details_records), valid + valid.splitlines()[1] + b"\n")
            for mutated in mutations:
                with self.subTest(mutated=mutated[:20]):
                    journal.path.write_bytes(mutated)
                    with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                        _read_journal(journal.path, journal.path.parent)
            journal.path.write_bytes(valid)
            hardlink = journal.path.with_name("hardlink.jsonl")
            try:
                os.link(journal.path, hardlink)
            except OSError:
                hardlink = None
            if hardlink is not None:
                original_bytes = journal.path.read_bytes()
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    append_journal_event(journal, "completed", {}, prepared=prepared)
                self.assertEqual(journal.path.read_bytes(), original_bytes)
                hardlink.unlink()

            def expect_bad_baseline(**changes: str) -> None:
                shadow = journal.path.with_name("20260816T000000000000Z-" + "b" * 16 + ".jsonl")
                record = json.loads(baseline)
                record["operation_id"] = shadow.stem
                record.update(changes)
                record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
                shadow.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    _orphan_snapshot_ids(prepared.control_root)
                shadow.unlink()

            expect_bad_baseline(snapshot_id="20260816T000000000000Z-" + "c" * 16)
            expect_bad_baseline(snapshot_manifest_sha256="d" * 64)
            for field, changed in {
                "operation": "promote",
                "candidate_id": self.item_id("z"),
                "input_digest_sha256": "e" * 64,
                "plan_hash": "f" * 64,
                "binding_digest_sha256": "0" * 64,
                "expected_remote_sha": "1" * 40,
                "prepared_commit_sha": "2" * 40,
                "prepared_tree_oid": "3" * 40,
                "target_ref": "refs/heads/other",
            }.items():
                with self.subTest(shared_field=field):
                    expect_bad_baseline(**{field: changed})
            duplicate = journal.path.with_name("20260816T000000000000Z-" + "f" * 16 + ".jsonl")
            duplicate_record = json.loads(baseline)
            duplicate_record["operation_id"] = duplicate.stem
            duplicate_record["record_sha256"] = promote_module._record_hash(duplicate_record, "record_sha256")
            duplicate.write_bytes(json.dumps(duplicate_record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _orphan_snapshot_ids(prepared.control_root)
            duplicate.unlink()
            backup = next(snapshot.path / fact["after"]["data"] for fact in manifest["files"] if fact["after"]["exists"])
            backup_bytes = backup.read_bytes()
            backup.write_bytes(b"tampered")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _orphan_snapshot_ids(prepared.control_root)
            backup.write_bytes(backup_bytes)
            (snapshot.path / "local-input.bin").write_bytes(b"tampered")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                create_canonical_journal(prepared, snapshot)
        finally:
            self.cleanup(prepared)

    def test_journal_terminal_grammar_rejects_postcompleted_rewrites(self) -> None:
        item = self.item_id("e")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            snapshot = create_canonical_snapshot(prepared)
            journal = create_canonical_journal(prepared, snapshot)
            completed = append_journal_event(journal, "completed", {}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(completed, "completed", {}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(completed, "failed", {}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(completed, "cleanup_pending", {}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(completed, "cleanup_pending", {"phase": "quarantine", "kind": "worktree"}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(completed, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine", "code": "x"}, prepared=prepared)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(journal, "preflight_ok", {"kind": "worktree"}, prepared=prepared)
            cleanup = append_journal_event(completed, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
            self.assertEqual(_read_journal(cleanup.path, cleanup.path.parent)[-1]["event"], "cleanup_pending")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                append_journal_event(cleanup, "preflight_ok", {}, prepared=prepared)

            rows = [json.loads(line) for line in cleanup.path.read_bytes().splitlines()]

            def add_event(base: list[dict[str, object]], event: str,
                          details: dict[str, object] | None = None) -> bytes:
                record = {
                    "schema": promote_module.JOURNAL_SCHEMA, "record_type": "event",
                    "sequence": len(base), "event": event,
                    "utc": base[-1]["utc"], "details": details or {},
                    "previous_record_sha256": base[-1]["record_sha256"],
                }
                record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
                return b"\n".join(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    for value in (*base, record)
                ) + b"\n"

            for base, event in ((rows[:-1], "completed"), (rows[:-1], "failed"), (rows, "preflight_ok")):
                with self.subTest(event=event):
                    cleanup.path.write_bytes(add_event(base, event))
                    with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                        _read_journal(cleanup.path, cleanup.path.parent)
            cleanup.path.write_bytes(add_event(rows[:1], "completed", {
                "site": "preflight", "reason": "validation",
            }))
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _read_journal(cleanup.path, cleanup.path.parent)
            cleanup.path.write_bytes(b"\n".join(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") for value in rows
            ) + b"\n")
        finally:
            self.cleanup(prepared)

    def test_snapshot_material_path_mutations_are_self_consistent_and_rejected(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            snapshot = create_canonical_snapshot(prepared)
            journal = create_canonical_journal(prepared, snapshot)
            original_manifest = (snapshot.path / "manifest.json").read_bytes()
            original_journal = journal.path.read_bytes()
            original_files = {path.relative_to(snapshot.path): path.read_bytes()
                              for path in snapshot.path.rglob("*") if path.is_file()}

            def restore() -> None:
                for path in snapshot.path.rglob("*"):
                    if path.is_file() and path.relative_to(snapshot.path) not in original_files:
                        path.unlink()
                for relative, data in original_files.items():
                    target = snapshot.path / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                journal.path.write_bytes(original_journal)

            def reject(payload: dict[str, object]) -> None:
                payload["manifest_sha256"] = promote_module._record_hash(payload, "manifest_sha256")
                (snapshot.path / "manifest.json").write_bytes(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                record = json.loads(original_journal)
                record["snapshot_manifest_sha256"] = payload["manifest_sha256"]
                record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
                journal.path.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                ref = promote_module.SnapshotRef(snapshot.snapshot_id, snapshot.path, payload["manifest_sha256"])
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._validate_snapshot_material(ref)
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    _orphan_snapshot_ids(prepared.control_root)

            base = json.loads(original_manifest)
            entry = base["files"][0]
            after = entry["after"]
            for kind in ("duplicate", "casefold", "unicode", "noop"):
                with self.subTest(kind=kind):
                    restore()
                    payload = json.loads(original_manifest)
                    if kind == "noop":
                        target = payload["files"][0]
                        if target["after"]["exists"]:
                            (snapshot.path / target["after"]["data"]).unlink()
                        target["before"] = {"exists": False}
                        target["after"] = {"exists": False}
                    else:
                        clone = json.loads(json.dumps(entry))
                        clone["after"] = dict(after)
                        clone["after"]["data"] = f"files/9999-{kind}.bin"
                        (snapshot.path / clone["after"]["data"]).write_bytes(
                            (snapshot.path / after["data"]).read_bytes())
                        if kind == "duplicate":
                            clone["path"] = entry["path"]
                        elif kind == "casefold":
                            clone["path"] = entry["path"].replace("inbox", "INBOX")
                        else:
                            payload["files"][0]["path"] = "state/inbox/é.md"
                            clone["path"] = "state/inbox/e\u0301.md"
                        payload["files"].append(clone)
                    reject(payload)
        finally:
            self.cleanup(prepared)

    def test_canonical_remote_truth_primitives_are_pinned_and_exact(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            facts = promote_module.revalidate_canonical_prepared_locked(prepared)
            self.assertEqual([(fact.status, fact.path) for fact in facts],
                             [("A", f"state/inbox/{item}.md")])
            original_source = prepared.source_path.read_bytes()
            prepared.source_path.write_bytes(original_source + b" ")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.revalidate_canonical_prepared_locked(prepared)
            prepared.source_path.write_bytes(original_source)
            with patch.object(promote_module, "_git", return_value=SimpleNamespace(returncode=1)) as call:
                promote_module.canonical_push_exact_lease(prepared)
            self.assertEqual(call.call_args.args[1:], (
                "push", "--porcelain",
                f"--force-with-lease=refs/heads/main:{prepared.expected_remote_sha}",
                "origin", f"{prepared.sha}:refs/heads/main",
            ))
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, prepared.expected_remote_sha).status,
                "NOT_COMMITTED")
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, prepared.sha).status,
                "COMMITTED")
            record = f"= {prepared.sha} {prepared.sha} refs/remotes/origin/main\n"
            def fetch_only(_repo: Path, *args: str, **_kwargs: object) -> SimpleNamespace:
                if args[:2] == ("rev-parse", "FETCH_HEAD"):
                    raise AssertionError("FETCH_HEAD must not be read")
                return SimpleNamespace(returncode=0, stdout=record, stderr="remote-secret://marker")

            with patch.object(promote_module, "_git", side_effect=fetch_only) as fetch:
                self.assertEqual(promote_module.observe_canonical_remote_head(prepared.repo), prepared.sha)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args_list[0].args[1:],
                             ("fetch", "--porcelain", "--verbose", "--no-write-fetch-head", "--no-tags",
                              "--no-recurse-submodules", "origin", "refs/heads/main"))
            for malformed in ("", record + record, f"= {'a' * 64} {'a' * 64} refs/remotes/origin/main\n",
                              f"= {'0' * 40} {'0' * 40} refs/remotes/origin/main\n",
                              "secret://remote\n"):
                with self.subTest(malformed=malformed), patch.object(
                        promote_module, "_git", return_value=SimpleNamespace(returncode=0, stdout=malformed)):
                    with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                        promote_module.observe_canonical_remote_head(prepared.repo)
            marker = "secret://push-output-marker"
            with patch.object(promote_module, "_git", return_value=SimpleNamespace(
                    returncode=1, stdout=marker, stderr=marker)):
                result = promote_module.canonical_push_exact_lease(prepared)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(tuple(result.__dataclass_fields__), ("returncode",))
            self.assertNotIn(marker, repr(result))
            with patch.object(promote_module, "_git", side_effect=OSError(marker)):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as error:
                    promote_module.canonical_push_exact_lease(prepared)
            self.assertNotIn(marker, str(error.exception))
            self.assertIsNone(error.exception.__cause__)
            with patch.object(promote_module, "_git", side_effect=UnicodeDecodeError(
                    "utf-8", b"\xff", 0, 1, "invalid start byte")):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as error:
                    promote_module.canonical_push_exact_lease(prepared)
            self.assertNotIn(marker, str(error.exception))
            self.assertIsNone(error.exception.__cause__)
            with patch.object(promote_module, "_git", side_effect=OSError(marker)):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as error:
                    promote_module.observe_canonical_remote_head(prepared.repo)
            self.assertNotIn(marker, repr(error.exception))
            self.assertIsNone(error.exception.__cause__)
            with patch.object(promote_module, "_git", side_effect=UnicodeDecodeError(
                    "utf-8", b"\xff", 0, 1, "invalid start byte")):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as error:
                    promote_module.observe_canonical_remote_head(prepared.repo)
            self.assertNotIn(marker, repr(error.exception))
            self.assertIsNone(error.exception.__cause__)
        finally:
            self.cleanup(prepared)

    def test_canonical_exact_lease_and_pinned_outcomes_use_only_synthetic_bare_remote(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            pushed = promote_module.canonical_push_exact_lease(prepared)
            self.assertEqual(pushed.returncode, 0)
            observer = Path(self.temp.name) / "first-observer"
            git(observer.parent, "init", "-q", observer.name)
            git(observer, "remote", "add", "origin", str(self.remote))
            self.assertEqual(promote_module.observe_canonical_remote_head(observer), prepared.sha)
            self.assertEqual(promote_module.observe_canonical_remote_head(observer), prepared.sha)
            observed = promote_module.observe_canonical_remote_head(prepared.repo)
            self.assertEqual(observed, prepared.sha)
            # A future orchestrator may receive this nonzero result even though the
            # preceding real push has already established the remote truth.
            ambiguous_push = promote_module.CanonicalPushResult(returncode=1)
            self.assertEqual(ambiguous_push.returncode, 1)
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, observed).status, "COMMITTED")

            git(self.seed, "pull", "-q", "--ff-only")
            candidate(self.seed / "state", self.item_id("b"), git(self.seed, "rev-parse", "HEAD"))
            git(self.seed, "add", "state")
            git(self.seed, "commit", "-q", "-m", "synthetic contender")
            git(self.seed, "push", "-q")
            pinned = promote_module.observe_canonical_remote_head(prepared.repo)
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, pinned).status, "COMMITTED")
            self.assertEqual(promote_module.canonical_push_exact_lease(prepared).returncode, 1)
            # A previously pinned observation stays the truth for this classifier call.
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, observed).status, "COMMITTED")
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                prepared.repo, prepared.expected_remote_sha, prepared.sha, prepared.expected_remote_sha).status,
                "NOT_COMMITTED")
            git(self.seed, "reset", "--hard", prepared.expected_remote_sha)
            (self.seed / "state" / "sibling.md").write_text("sibling\n", encoding="utf-8")
            git(self.seed, "add", "state/sibling.md")
            git(self.seed, "commit", "-q", "-m", "synthetic sibling")
            sibling = git(self.seed, "rev-parse", "HEAD")
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                self.seed, prepared.expected_remote_sha, prepared.sha, sibling).status, "LOST_RACE")
            git(self.seed, "checkout", "--orphan", "synthetic-unrelated")
            git(self.seed, "rm", "-rf", ".", check=False)
            (self.seed / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
            git(self.seed, "add", "unrelated.txt")
            git(self.seed, "commit", "-q", "-m", "synthetic unrelated")
            unrelated = git(self.seed, "rev-parse", "HEAD")
            self.assertEqual(promote_module.classify_canonical_remote_outcome(
                self.seed, prepared.expected_remote_sha, prepared.sha, unrelated).status, "UNSAFE")
        finally:
            self.cleanup(prepared)

    def test_canonical_descendant_scope_matrix(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        txn = prepared.txn

        def commit_state(name: str, payload: str | None) -> str:
            path = txn / "state" / name
            if payload is None:
                path.unlink()
                git(txn, "add", "-A", "--", f"state/{name}")
            else:
                path.write_text(payload, encoding="utf-8")
                git(txn, "add", "--", f"state/{name}")
            git(txn, "commit", "-q", "-m", "synthetic state")
            return git(txn, "rev-parse", "HEAD")

        try:
            promote_module.validate_canonical_descendant_scope(txn, prepared.sha, prepared.sha)
            first = commit_state("descendant.md", "one\n")
            second = commit_state("descendant.md", "two\n")
            third = commit_state("descendant.md", None)
            promote_module.validate_canonical_descendant_scope(txn, prepared.sha, third)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, third, max_depth=2)
            with patch.object(promote_module, "_git_bytes", return_value=SimpleNamespace(
                    returncode=0, stdout=b"x" * (4 * 1024 * 1024 + 1))):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module.validate_canonical_descendant_scope(txn, prepared.sha, third)
            many_paths = b"".join(
                b"100644 blob " + b"a" * 40 + f"\tstate/limit-{index}.md\0".encode("utf-8")
                for index in range(10_001)
            )
            with patch.object(promote_module, "_git_bytes", return_value=SimpleNamespace(
                    returncode=0, stdout=many_paths)):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module.validate_canonical_descendant_scope(txn, prepared.sha, third)
            one_state_path = b"100644 blob " + b"a" * 40 + b"\tstate/limit.md\0"
            with patch.object(promote_module, "_git_bytes", side_effect=(
                    SimpleNamespace(returncode=0, stdout=one_state_path),
                    SimpleNamespace(returncode=0, stdout=b"x" * (4 * 1024 * 1024 + 1)),
            )):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module.validate_canonical_descendant_scope(txn, prepared.sha, third)
            git(txn, "branch", "-f", "synthetic-descendant-base", third)

            def from_base(branch: str) -> None:
                git(txn, "checkout", "-q", "-B", branch, "synthetic-descendant-base")

            from_base("synthetic-descendant-engine")
            (txn / "engine" / "README.md").write_text("engine mutation\n", encoding="utf-8")
            git(txn, "add", "engine/README.md")
            git(txn, "commit", "-q", "-m", "synthetic engine")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, git(txn, "rev-parse", "HEAD"))

            from_base("synthetic-descendant-root")
            (txn / "root.txt").write_text("root\n", encoding="utf-8")
            git(txn, "add", "root.txt")
            git(txn, "commit", "-q", "-m", "synthetic root")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, git(txn, "rev-parse", "HEAD"))

            from_base("synthetic-descendant-mode")
            (txn / "state" / "mode.sh").write_text("echo synthetic\n", encoding="utf-8")
            git(txn, "add", "state/mode.sh")
            git(txn, "update-index", "--chmod=+x", "--", "state/mode.sh")
            git(txn, "commit", "-q", "-m", "synthetic mode")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, git(txn, "rev-parse", "HEAD"))

            from_base("synthetic-descendant-merge-left")
            (txn / "state" / "merge-left.md").write_text("left\n", encoding="utf-8")
            git(txn, "add", "state/merge-left.md")
            git(txn, "commit", "-q", "-m", "synthetic merge left")
            from_base("synthetic-descendant-merge-right")
            (txn / "state" / "merge-right.md").write_text("right\n", encoding="utf-8")
            git(txn, "add", "state/merge-right.md")
            git(txn, "commit", "-q", "-m", "synthetic merge right")
            git(txn, "checkout", "-q", "synthetic-descendant-merge-left")
            git(txn, "merge", "--no-ff", "--no-edit", "synthetic-descendant-merge-right")
            merge_fields = git(txn, "rev-list", "--parents", "-n", "1", "HEAD").split()
            self.assertEqual(len(merge_fields), 3)
            self.assertNotEqual(merge_fields[1], third)
            self.assertNotEqual(merge_fields[2], third)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, git(txn, "rev-parse", "HEAD"))

            from_base("synthetic-descendant-collision")
            oid = git(txn, "rev-parse", "HEAD:state/manifest.yaml")
            git(txn, "update-index", "--add", "--cacheinfo", f"100644,{oid},state/MANIFEST.yaml")
            git(txn, "commit", "-q", "-m", "synthetic case collision")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.validate_canonical_descendant_scope(txn, prepared.sha, git(txn, "rev-parse", "HEAD"))
        finally:
            for branch in ("synthetic-descendant-base", "synthetic-descendant-engine",
                           "synthetic-descendant-root", "synthetic-descendant-mode",
                           "synthetic-descendant-merge-left", "synthetic-descendant-merge-right",
                           "synthetic-descendant-collision"):
                git(txn, "branch", "-D", branch, check=False)
            self.cleanup(prepared)

    def test_snapshot_faults_clean_owned_temp_and_preserve_collision(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            fresh_control = Path(self.temp.name) / "fresh-control"
            with patch.object(promote_module, "_fsync_directory") as fsync:
                ensured = promote_module._ensure_artifact_root(fresh_control, "snapshots")
            self.assertEqual(ensured, (fresh_control / "snapshots").resolve())
            self.assertEqual([call.args[0].resolve() for call in fsync.call_args_list],
                             [fresh_control.parent.resolve(), fresh_control.resolve()])
            original_write = promote_module._write_owned
            for label, failing_call in (("data", 1), ("manifest", 3)):
                with self.subTest(fault=label):
                    calls = 0

                    def fail_write(path: Path, data: bytes) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == failing_call:
                            raise OSError(label)
                        original_write(path, data)

                    with patch.object(promote_module, "_write_owned", side_effect=fail_write):
                        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                            create_canonical_snapshot(prepared)
                    root = prepared.control_root / "snapshots"
                    self.assertFalse(root.exists() and any(path.name.startswith(".tmp-") for path in root.iterdir()))
                    journal_root = prepared.control_root / "journal"
                    self.assertFalse(journal_root.exists() and any(journal_root.iterdir()))

            fixed_id = "20260816T000000000000Z-" + "a" * 16
            root = prepared.control_root / "snapshots"
            original_finalize = promote_module._finalize_owned_directory

            def volume_false(temporary: Path, final: Path) -> None:
                real_stat = promote_module.os.stat

                def cross_volume(path: str | os.PathLike[str], *args: object, **kwargs: object) -> object:
                    if Path(path) == temporary:
                        return SimpleNamespace(st_dev=1)
                    if Path(path) == final.parent:
                        return SimpleNamespace(st_dev=2)
                    return real_stat(path, *args, **kwargs)

                with patch.object(promote_module.os, "stat", side_effect=cross_volume):
                    original_finalize(temporary, final)

            with patch.object(promote_module, "_artifact_id", return_value=fixed_id), \
                    patch.object(promote_module, "_finalize_owned_directory", side_effect=volume_false):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    create_canonical_snapshot(prepared)
            self.assertFalse((root / fixed_id).exists())
            self.assertFalse(any(path.name.startswith(".tmp-") for path in root.iterdir()))

            with patch.object(promote_module, "_artifact_id", return_value=fixed_id), \
                    patch.object(promote_module.os, "replace", side_effect=OSError("rename")):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    create_canonical_snapshot(prepared)
            self.assertFalse((root / fixed_id).exists())
            self.assertFalse(any(path.name.startswith(".tmp-") for path in root.iterdir()))

            with patch.object(promote_module, "_artifact_id", return_value=fixed_id):
                first = create_canonical_snapshot(prepared)
                first_manifest = (first.path / "manifest.json").read_bytes()
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    create_canonical_snapshot(prepared)
            self.assertEqual((first.path / "manifest.json").read_bytes(), first_manifest)
            shutil.rmtree(first.path)

            post_rename_id = "20260816T000000000000Z-" + "c" * 16
            original_fsync = promote_module._fsync_directory

            def fail_post_rename(path: Path) -> None:
                if path == root:
                    raise OSError("parent fsync")
                original_fsync(path)

            with patch.object(promote_module, "_artifact_id", return_value=post_rename_id), \
                    patch.object(promote_module, "_fsync_directory", side_effect=fail_post_rename):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    create_canonical_snapshot(prepared)
            post_rename = root / post_rename_id
            self.assertTrue(post_rename.is_dir())
            post_manifest = json.loads((post_rename / "manifest.json").read_text(encoding="utf-8"))
            promote_module._validate_snapshot_material(
                promote_module.SnapshotRef(post_rename_id, post_rename, post_manifest["manifest_sha256"]))
            self.assertIn(f"ORPHAN_SNAPSHOT {post_rename_id}",
                          plan_publish(self.state, None, item, config_path=self.config).lines)
            shutil.rmtree(post_rename)

            collision_id = "20260816T000000000000Z-" + "b" * 16
            collision = root / collision_id
            collision.mkdir(parents=True)
            sentinel = collision / "sentinel"
            sentinel.write_bytes(b"preserve")
            with patch.object(promote_module, "_artifact_id", return_value=collision_id):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    create_canonical_snapshot(prepared)
            self.assertEqual(sentinel.read_bytes(), b"preserve")
            self.assertFalse(any(path.name.startswith(".tmp-") for path in root.iterdir()))
        finally:
            self.cleanup(prepared)

    def test_promote_prepare_uses_prefixed_remote_blob_and_state_only_paths(self) -> None:
        item = self.item_id("b")
        remote_base = git(self.seed, "rev-parse", "HEAD")
        candidate(self.seed / "state", item, remote_base)
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-q", "-m", "published candidate")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        plan = plan_promote(self.state, None, item, force_new=True,
                            reviewed_against=git(self.repo, "rev-parse", "origin/main"), config_path=self.config)
        prepared = prepare_promote(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        try:
            facts = promote_module.revalidate_canonical_prepared_locked(prepared)
            self.assertEqual({fact.status for fact in facts}, {"A", "D", "M"})
            source = self.state / "inbox" / f"{item}.md"
            self.assertEqual(parse_candidate_bytes(source.read_bytes(), item), load_candidate(source))
            self.assertTrue((self.state / "experience" / "profiles" / "trading-execution" / "LESSONS.md").is_file())
            original_source = source.read_bytes()
            try:
                git(self.repo, "update-index", "--skip-worktree", "--", f"state/inbox/{item}.md")
                altered = json.loads(original_source)
                altered["scope_hint"] = "profile:trading-execution"
                source.write_bytes((json.dumps(altered, sort_keys=True) + "\n").encode("utf-8"))
                self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module.revalidate_canonical_prepared_locked(prepared)
            finally:
                git(self.repo, "update-index", "--no-skip-worktree", "--", f"state/inbox/{item}.md")
                source.write_bytes(original_source)
            wrong_ledger = replace(prepared, changed_paths=(
                f"state/inbox/{item}.md", f"state/inbox/consumed/{item}.md", "state/manifest.yaml",
            ))
            with patch.object(promote_module, "_assert_committed_scope", side_effect=AssertionError("late")):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module.revalidate_canonical_prepared_locked(wrong_ledger)
            self.assertEqual(prepared.config_path, self.config.resolve())
            self.assertEqual(prepared.plan_hash, plan.plan_hash)
            self.assertEqual(prepared.input_digest_sha256, plan.payload["candidate_sha256"])
            self.assertEqual(dict(prepared.binding or ()), plan.payload["binding"])
            self.assertEqual(prepared.tree_oid,
                             git(prepared.txn, "rev-parse", f"{prepared.sha}^{{tree}}"))
            self.assertEqual(set(prepared.changed_paths), {
                "state/experience/LESSONS.md", f"state/inbox/{item}.md", f"state/inbox/consumed/{item}.md",
            })
            self.assertEqual(
                git(prepared.txn, "rev-parse", f"{prepared.expected_remote_sha}:engine"),
                git(prepared.txn, "rev-parse", f"{prepared.sha}:engine"),
            )
        finally:
            self.cleanup(prepared)

    def test_canonical_promote_apply_records_pinned_head_without_quarantine(self) -> None:
        item = self.item_id("e")
        candidate(self.seed / "state", item, git(self.seed, "rev-parse", "HEAD"))
        git(self.seed, "add", "state")
        git(self.seed, "commit", "-q", "-m", "published candidate")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        plan = plan_promote(self.state, None, item, force_new=True,
                            reviewed_against=git(self.repo, "rev-parse", "origin/main"), config_path=self.config)
        prepared = prepare_promote(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), prepared.sha)
        self.assertFalse((self.state / "inbox" / f"{item}.md").exists())
        self.assertTrue((self.state / "inbox" / "consumed" / f"{item}.md").is_file())
        self.assertFalse(prepared.txn.exists())
        records = _read_journal(next((prepared.control_root / "journal").glob("*.jsonl")),
                                prepared.control_root / "journal")
        self.assertNotIn("source_removed", [record.get("event") for record in records])
        self.assertEqual(records[-1].get("event"), "completed")

    def test_promote_requires_source_blob_to_reach_consumed_unchanged(self) -> None:
        item = self.item_id("e")
        candidate(self.seed / "state", item, git(self.seed, "rev-parse", "HEAD"))
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-q", "-m", "published candidate")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        context = resolve_repository_context(self.state)
        expected = git(self.repo, "rev-parse", "origin/main")
        source = f"state/inbox/{item}.md"
        consumed = f"state/inbox/consumed/{item}.md"
        ledger = "state/experience/LESSONS.md"
        expectations = (
            ChangeExpectation("M", ledger), ChangeExpectation("D", source),
            ChangeExpectation("A", consumed),
        )
        move = (BlobMove(source, consumed),)
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            source_path = txn / source
            consumed_path = txn / consumed
            consumed_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.unlink()
            consumed_path.write_text("replacement", encoding="utf-8")
            ledger_path = txn / ledger
            ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _commit(txn, "bad move", expected, context, expectations, move)
            self.assertEqual(git(txn, "rev-parse", "HEAD"), expected)
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            source_path = txn / source
            consumed_path = txn / consumed
            consumed_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.replace(consumed_path)
            ledger_path = txn / ledger
            ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            sha = _commit(txn, "good move", expected, context, expectations, move)
            self.assertNotEqual(sha, expected)
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)
        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
            _assert_blob_moves((), (BlobMove(source, source),))
        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
            _assert_blob_moves((), (BlobMove(source, consumed), BlobMove(ledger, consumed)))

    def test_canonical_publish_apply_uses_pinned_truth_and_quarantine(self) -> None:
        item = self.item_id("c")
        original = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        result = apply_prepared(prepared, retry_inbox_race=True)
        self.assertEqual(result.sha, prepared.sha)
        self.assertTrue(result.rollback_id)
        self.assertFalse(prepared.txn.exists())
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.sha)
        self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), original)
        self.assertEqual(git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        journal_path = next((prepared.control_root / "journal").glob("*.jsonl"))
        records = _read_journal(journal_path, journal_path.parent)
        self.assertEqual([record.get("event", "baseline") for record in records], [
            "baseline", "preflight_ok", "snapshot_durable", "push_attempt", "ancestry_observed",
            "remote_pointer_updated", "source_removal_intent", "source_removed", "fast_forward_done", "completed",
        ])
        self.assertEqual(records[-1]["details"]["sha"], prepared.sha)
        self.assertEqual(_orphan_snapshot_ids(prepared.control_root), ())
        self.assertEqual(promote_module._quarantine_pending_ids(prepared.repo), ())

    def test_canonical_noncommitted_unknown_and_scope_fail_without_local_effects(self) -> None:
        item = self.item_id("d")
        original = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        before = git(self.repo, "rev-parse", "HEAD")
        cases = (
            ("not-committed", promote_module.CanonicalRemoteOutcome("NOT_COMMITTED", prepared.expected_remote_sha), None,
             "FAIL_REMOTE_RACE"),
            ("unsafe", promote_module.CanonicalRemoteOutcome("UNSAFE", prepared.expected_remote_sha), None,
             "FAIL_REMOTE_REWIND"),
            ("unknown", None, ConfigError("FAIL_TRANSACTION_SCOPE", "UNIQUE_OBSERVE_MARKER"), "REMOTE_OUTCOME_UNKNOWN"),
            ("scope", promote_module.CanonicalRemoteOutcome("COMMITTED", prepared.sha),
             ConfigError("FAIL_TRANSACTION_SCOPE", "UNIQUE_SCOPE_MARKER"), "REMOTE_COMMITTED_SCOPE_UNVERIFIED"),
        )
        for label, outcome, failure, code in cases:
            with self.subTest(label=label):
                if not prepared.txn.exists():
                    prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                               config_path=self.config)
                with patch.object(promote_module, "canonical_push_exact_lease", return_value=promote_module.CanonicalPushResult(1)) as push, \
                        patch.object(promote_module, "observe_canonical_remote_head",
                                     side_effect=failure if label == "unknown" else lambda _repo: outcome.observed_sha) as observe, \
                        patch.object(promote_module, "classify_canonical_remote_outcome",
                                     return_value=outcome if outcome is not None else None), \
                        patch.object(promote_module, "validate_canonical_descendant_scope",
                                     side_effect=failure if label == "scope" else None):
                    with self.assertRaisesRegex(ConfigError, code) as failure_context:
                        apply_prepared(prepared)
                if label in {"unknown", "scope"}:
                    error = failure_context.exception
                    self.assertEqual(error.code, code)
                    self.assertIsNone(error.__cause__)
                    self.assertNotIn("UNIQUE_", str(error))
                    self.assertNotIn("UNIQUE_", repr(error))
                self.assertEqual(push.call_count, 1)
                self.assertEqual(observe.call_count, 1)
                self.assertFalse(prepared.txn.exists())
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
                self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), original)
                self.assertFalse((prepared.control_root / "remote-state.json").exists())

    def test_canonical_trust_boundaries_hide_raw_process_causes(self) -> None:
        failures = (
            OSError("UNIQUE_OS_MARKER"),
            subprocess.TimeoutExpired(["synthetic", "UNIQUE_TIMEOUT_MARKER"], 1),
            UnicodeDecodeError("utf-8", b"x", 0, 1, "UNIQUE_UNICODE_MARKER"),
        )
        for boundary, code in (("observe", "REMOTE_OUTCOME_UNKNOWN"),
                               ("scope", "REMOTE_COMMITTED_SCOPE_UNVERIFIED")):
            for index, raw in enumerate(failures):
                item = self.item_id(f"{index + (3 if boundary == 'scope' else 0):x}")
                source = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
                plan = plan_publish(self.state, None, item, config_path=self.config)
                prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                           config_path=self.config)
                with self.subTest(boundary=boundary, raw=type(raw).__name__), \
                        patch.object(promote_module, "canonical_push_exact_lease",
                                     return_value=promote_module.CanonicalPushResult(1)), \
                        patch.object(promote_module, "observe_canonical_remote_head",
                                     side_effect=raw if boundary == "observe" else lambda _repo: prepared.sha), \
                        patch.object(promote_module, "classify_canonical_remote_outcome",
                                     return_value=promote_module.CanonicalRemoteOutcome("COMMITTED", prepared.sha)), \
                        patch.object(promote_module, "validate_canonical_descendant_scope",
                                     side_effect=raw if boundary == "scope" else None):
                    with self.assertRaisesRegex(ConfigError, code) as failure_context:
                        apply_prepared(prepared)
                error = failure_context.exception
                self.assertEqual(error.code, code)
                self.assertIsNone(error.__cause__)
                for marker in ("UNIQUE_OS_MARKER", "UNIQUE_TIMEOUT_MARKER", "UNIQUE_UNICODE_MARKER"):
                    self.assertNotIn(marker, str(error))
                    self.assertNotIn(marker, repr(error))
                self.assertFalse(prepared.txn.exists())
                self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), source)
                (self.state / "inbox" / f"{item}.md").unlink()

    def test_canonical_push_exception_still_uses_one_pinned_observation(self) -> None:
        item = self.item_id("f")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        real_push = promote_module.canonical_push_exact_lease
        real_observe = promote_module.observe_canonical_remote_head

        def push_then_raise(value):
            real_push(value)
            raise ConfigError("FAIL_TRANSACTION_SCOPE", "synthetic push result")

        with patch.object(promote_module, "canonical_push_exact_lease", side_effect=push_then_raise) as push, \
                patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
            result = apply_prepared(prepared)

        self.assertEqual(push.call_count, 1)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(result.sha, prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.sha)
        self.assertFalse(prepared.txn.exists())

    def test_publish_identity_swap_after_intent_is_stale_and_keeps_source(self) -> None:
        item = self.item_id("f")
        original = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        source = self.state / "inbox" / f"{item}.md"
        real_append = promote_module.append_journal_event
        real_lstat = promote_module.os.lstat
        intent_written = [False]

        def mark_intent(journal, event, details=None, *, prepared):
            result = real_append(journal, event, details, prepared=prepared)
            if event == "source_removal_intent":
                intent_written[0] = True
            return result

        def swapped_identity(path):
            observed = real_lstat(path)
            if intent_written[0] and Path(path) == source:
                values = list(observed)
                values[1] += 1
                return os.stat_result(values)
            return observed

        with patch.object(promote_module, "append_journal_event", side_effect=mark_intent), \
                patch.object(promote_module.os, "lstat", side_effect=swapped_identity):
            with self.assertRaisesRegex(ConfigError, "REMOTE_COMMITTED_LOCAL_STALE"):
                apply_prepared(prepared)
        self.assertTrue(intent_written[0])
        self.assertFalse(prepared.txn.exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.expected_remote_sha)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), prepared.sha)

    def test_quarantine_operation_identity_swap_keeps_sentinel_and_reports_pending(self) -> None:
        item = self.item_id("e")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        sentinel = Path(self.temp.name) / "outside-sentinel"
        sentinel.write_bytes(b"outside")
        real_cleanup = promote_module._cleanup_quarantine
        real_lstat = promote_module.os.lstat

        def swapped_cleanup(reference, value, journal):
            def swapped_lstat(path):
                observed = real_lstat(path)
                if Path(path) == reference.operation_dir:
                    fields = list(observed)
                    fields[1] += 1
                    return os.stat_result(fields)
                return observed
            with patch.object(promote_module.os, "lstat", side_effect=swapped_lstat):
                return real_cleanup(reference, value, journal)

        with patch.object(promote_module, "_cleanup_quarantine", side_effect=swapped_cleanup):
            result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertTrue(promote_module._quarantine_pending_ids(prepared.repo))
        warning_plan = plan_promote(self.state, None, item, reviewed_against=prepared.sha, config_path=self.config)
        self.assertTrue(any(line.startswith("QUARANTINE_PENDING ") for line in warning_plan.lines))

    def test_canonical_finalization_failure_keeps_committed_truth_and_quarantine(self) -> None:
        item = self.item_id("e")
        original = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        with patch.object(promote_module, "_cleanup_worktree",
                          side_effect=ConfigError("FAIL_TRANSACTION_SCOPE", "UNIQUE_FINALIZE_MARKER")):
            with self.assertRaisesRegex(ConfigError, "REMOTE_COMMITTED_FINALIZATION_INCOMPLETE") as failure_context:
                apply_prepared(prepared)
        error = failure_context.exception
        self.assertEqual(error.code, "REMOTE_COMMITTED_FINALIZATION_INCOMPLETE")
        self.assertIsNone(error.__cause__)
        self.assertNotIn("UNIQUE_FINALIZE_MARKER", str(error))
        self.assertNotIn("UNIQUE_FINALIZE_MARKER", repr(error))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.sha)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), prepared.sha)
        self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), original)
        self.assertTrue(promote_module._quarantine_pending_ids(prepared.repo))
        self.assertTrue((prepared.control_root / "remote-state.json").exists())
        self.cleanup(prepared)

    def test_canonical_local_stale_hides_postcommit_cause(self) -> None:
        item = self.item_id("d")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        with patch.object(promote_module, "fast_forward_local",
                          side_effect=OSError("UNIQUE_STALE_MARKER")):
            with self.assertRaisesRegex(ConfigError, "REMOTE_COMMITTED_LOCAL_STALE") as failure_context:
                apply_prepared(prepared)
        error = failure_context.exception
        self.assertEqual(error.code, "REMOTE_COMMITTED_LOCAL_STALE")
        self.assertIsNone(error.__cause__)
        self.assertNotIn("UNIQUE_STALE_MARKER", str(error))
        self.assertNotIn("UNIQUE_STALE_MARKER", repr(error))
        self.assertFalse(prepared.txn.exists())

    def test_canonical_completed_quarantine_gc_is_best_effort(self) -> None:
        item = self.item_id("a")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        real_unlink = Path.unlink

        def deny_quarantine_target(path, *args, **kwargs):
            candidate_path = Path(path)
            if candidate_path.name == f"{item}.md" and "agent-core-quarantine" in candidate_path.parts:
                raise OSError("synthetic quarantine cleanup")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=deny_quarantine_target):
            result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        journal_path = next((prepared.control_root / "journal").glob("*.jsonl"))
        records = _read_journal(journal_path, journal_path.parent)
        events = [record.get("event", "baseline") for record in records]
        self.assertLess(events.index("completed"), events.index("cleanup_pending"))
        self.assertNotIn("failed", events)
        self.assertTrue(promote_module._quarantine_pending_ids(prepared.repo))

    def test_canonical_completed_gc_double_failure_returns_result_and_reports_pending(self) -> None:
        item = self.item_id("b")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        real_unlink = Path.unlink
        real_append = promote_module.append_journal_event

        def deny_quarantine_target(path, *args, **kwargs):
            candidate_path = Path(path)
            if candidate_path.name == f"{item}.md" and "agent-core-quarantine" in candidate_path.parts:
                raise OSError("synthetic quarantine cleanup")
            return real_unlink(path, *args, **kwargs)

        def deny_cleanup_pending(journal, event, details=None, *, prepared):
            if event == "cleanup_pending":
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "UNIQUE_GC_MARKER")
            return real_append(journal, event, details, prepared=prepared)

        with patch.object(Path, "unlink", new=deny_quarantine_target), \
                patch.object(promote_module, "append_journal_event", side_effect=deny_cleanup_pending):
            result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        journal_path = next((prepared.control_root / "journal").glob("*.jsonl"))
        records = _read_journal(journal_path, journal_path.parent)
        events = [record.get("event", "baseline") for record in records]
        self.assertEqual(events[-1], "completed")
        self.assertNotIn("cleanup_pending", events)
        self.assertNotIn("failed", events)
        plan = plan_promote(self.state, None, item, reviewed_against=prepared.sha, config_path=self.config)
        warning = next(line for line in plan.lines if line.startswith("QUARANTINE_PENDING "))
        self.assertNotIn("agent-core-quarantine", warning)
        self.assertNotIn(str(prepared.repo), warning)

    def test_canonical_completed_empty_quarantine_dir_remains_visible_after_double_failure(self) -> None:
        item = self.item_id("d")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        real_rmdir = Path.rmdir
        real_append = promote_module.append_journal_event

        def deny_operation_rmdir(path):
            candidate_path = Path(path)
            if "agent-core-quarantine" in candidate_path.parts:
                raise OSError("synthetic empty operation directory")
            return real_rmdir(path)

        def deny_cleanup_pending(journal, event, details=None, *, prepared):
            if event == "cleanup_pending":
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "UNIQUE_EMPTY_GC_MARKER")
            return real_append(journal, event, details, prepared=prepared)

        with patch.object(Path, "rmdir", new=deny_operation_rmdir), \
                patch.object(promote_module, "append_journal_event", side_effect=deny_cleanup_pending):
            result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        operation_dir = next(promote_module._quarantine_root(prepared.repo).iterdir())
        self.assertEqual(tuple(operation_dir.iterdir()), ())
        journal_path = next((prepared.control_root / "journal").glob("*.jsonl"))
        events = [record.get("event", "baseline") for record in _read_journal(journal_path, journal_path.parent)]
        self.assertEqual(events[-1], "completed")
        self.assertNotIn("failed", events)
        self.assertNotIn("cleanup_pending", events)
        warning_plan = plan_promote(self.state, None, item, reviewed_against=prepared.sha, config_path=self.config)
        self.assertTrue(any(line.startswith("QUARANTINE_PENDING ") for line in warning_plan.lines))

    def test_canonical_completed_gc_namespace_invariant_is_nonfatal(self) -> None:
        item = self.item_id("c")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        real_root = promote_module._quarantine_root
        calls = [0]

        def wrong_root_after_move(repo):
            calls[0] += 1
            if calls[0] == 2:
                return repo / ".git" / "wrong-quarantine-root"
            return real_root(repo)

        with patch.object(promote_module, "_quarantine_root", side_effect=wrong_root_after_move):
            result = apply_prepared(prepared)
        self.assertEqual(result.sha, prepared.sha)
        journal_path = next((prepared.control_root / "journal").glob("*.jsonl"))
        events = [record.get("event", "baseline") for record in _read_journal(journal_path, journal_path.parent)]
        self.assertLess(events.index("completed"), events.index("cleanup_pending"))
        self.assertNotIn("failed", events)

    def test_plan_reports_quarantine_pending_without_path_disclosure(self) -> None:
        item = self.item_id("b")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        operation_id = "20260816T000000000000Z-" + "a" * 16
        pending = promote_module._quarantine_root(self.repo) / operation_id
        pending.mkdir(parents=True)
        (pending / "opaque").write_bytes(b"pending")
        plan = plan_publish(self.state, None, item, config_path=self.config)
        self.assertIn(f"QUARANTINE_PENDING {operation_id}", plan.lines)
        self.assertNotIn(str(pending), "\n".join(plan.lines))

    def test_canonical_preflight_requires_config_rejects_override_and_binds_identity(self) -> None:
        item = self.item_id("f")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
            with self.assertRaisesRegex(ConfigError, "FAIL_STATE_BINDING"):
                plan_publish(self.state, None, item)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                plan_publish(self.state, self.control, item, config_path=self.config)
        context = resolve_repository_context(self.state)
        derived = promote_module._canonical_control_root(context, None)
        self.assertFalse(derived.exists())
        plan = plan_publish(self.state, None, item, config_path=self.config)
        binding = plan.payload["binding"]
        self.assertEqual(plan.expected_remote_sha, binding["remote_revision"])
        self.assertEqual(set(binding), {
            "config_path", "receipt_path", "schema", "config_sha256", "receipt_sha256",
            "remote_url_sha256", "remote_revision", "repository_root_sha",
            "repository_root", "engine_provenance_sha256", "state_lock_sha256", "control_identity",
            "control_filesystem_sha256",
        })
        self.assertEqual(binding["schema"], "state-binding/2")
        self.assertEqual(binding["config_path"], str(self.config.resolve()))
        self.assertEqual(binding["control_identity"], str(derived))
        self.assertFalse(derived.exists())
        remote_url = git(self.state, "remote", "get-url", "origin")
        self.assertNotIn(remote_url, "\n".join(plan.lines))
        self.assertNotIn(remote_url, json.dumps(plan.payload, sort_keys=True))

    def test_canonical_prepare_rejects_bound_identity_and_input_mutations_before_txn(self) -> None:
        item = self.item_id("e")
        source = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        receipt = binding_receipt_path(self.config)
        original_config = self.config.read_bytes()
        original_receipt = receipt.read_bytes()
        mutations = (
            (self.config, original_config + b" ", "FAIL_STATE_BINDING"),
            (receipt, original_receipt + b" ", "FAIL_PLAN_HASH"),
        )
        for path, content, code in mutations:
            path.write_bytes(content)
            with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
                with self.assertRaisesRegex(ConfigError, code):
                    prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                    config_path=self.config)
            path.write_bytes(original_config if path == self.config else original_receipt)
        changed_source = source.replace(b"Synthetic monorepo rule", b"Changed monorepo rule")
        self.assertNotEqual(changed_source, source)
        (self.state / "inbox" / f"{item}.md").write_bytes(changed_source)
        with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
            with self.assertRaisesRegex(ConfigError, "FAIL_CANDIDATE_CHANGED"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)
        (self.state / "inbox" / f"{item}.md").write_bytes(source)
        (self.seed / "state" / "remote-marker.txt").write_text("advance\n", encoding="utf-8")
        git(self.seed, "add", "state/remote-marker.txt")
        git(self.seed, "commit", "-q", "-m", "state-only advance")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
            with self.assertRaisesRegex(ConfigError, "FAIL_PLAN_HASH"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)
        refreshed = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, refreshed, refreshed.plan_hash,
                                   refreshed.expected_remote_sha, config_path=self.config)
        try:
            self.assertEqual(prepared.config_path, Path(refreshed.payload["binding"]["config_path"]))
            self.assertEqual(dict(prepared.binding or ()), refreshed.payload["binding"])
            self.assertEqual(prepared.plan_hash, refreshed.plan_hash)
            self.assertEqual(prepared.input_digest_sha256, refreshed.payload["source_sha256"])
            self.assertEqual(prepared.binding_digest_sha256,
                             promote_module._binding_digest(refreshed.payload["binding"]))
            self.assertEqual(prepared.tree_oid,
                             git(prepared.txn, "rev-parse", f"{prepared.sha}^{{tree}}"))
            with self.assertRaises(TypeError):
                prepared.binding[0] = ("schema", "changed")  # type: ignore[index]
        finally:
            self.cleanup(prepared)

    def test_mutable_plan_payload_cannot_bypass_preflight(self) -> None:
        item = self.item_id("d")
        source = candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        original = json.loads(json.dumps(plan.payload))
        source_path = self.state / "inbox" / f"{item}.md"
        changed = source.replace(b"Synthetic monorepo rule", b"Changed monorepo rule")
        source_path.write_bytes(changed)
        plan.payload["source_sha256"] = promote_module.hashlib.sha256(changed).hexdigest()
        with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")), \
                patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
            with self.assertRaisesRegex(ConfigError, "FAIL_PLAN_HASH"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)
        source_path.write_bytes(source)
        mutations = (
            lambda payload: payload["binding"].__setitem__("remote_revision", "0" * 40),
            lambda payload: payload.__setitem__("operation", "promote"),
            lambda payload: payload.__setitem__("candidate_id", self.item_id("z")),
            lambda payload: payload.__setitem__("expected_remote_sha", "f" * 40),
            lambda payload: payload.__setitem__("target_ref", "refs/heads/other"),
        )
        for mutate in mutations:
            plan.payload.clear()
            plan.payload.update(json.loads(json.dumps(original)))
            mutate(plan.payload)
            with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")), \
                    patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
                with self.assertRaisesRegex(ConfigError, "FAIL_PLAN_HASH"):
                    prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                    config_path=self.config)
        self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), source)

    def test_prepare_uses_verified_payload_snapshot_not_caller_mapping(self) -> None:
        item = self.item_id("d")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        snapshot = replace(plan, payload=json.loads(json.dumps(plan.payload)))

        def verified(*_args):
            plan.payload["source_sha256"] = "0" * 64
            plan.payload["binding"]["remote_revision"] = "0" * 40
            return snapshot

        with patch.object(promote_module, "_verify_apply_inputs", side_effect=verified):
            prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                       config_path=self.config)
        try:
            self.assertEqual(prepared.input_digest_sha256, snapshot.payload["source_sha256"])
            self.assertEqual(dict(prepared.binding or ()), snapshot.payload["binding"])
        finally:
            self.cleanup(prepared)

    def test_control_locality_helpers_bind_plan_and_fail_closed(self) -> None:
        mountinfo = (
            "24 1 8:1 / / rw - ext4 /dev/sda rw\n"
            "35 24 0:42 / /control\\040space rw - xfs /dev/sdb rw\n"
        )
        digest = promote_module._linux_mountinfo_locality("/control space/agent-core", mountinfo)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn("control space", digest)
        for malformed in (
            "malformed",
            "24 1 0:1 / / rw - nfs server:/export rw\n",
            "24 1 0:1 / / rw - fuse.sshfs sshfs rw\n",
            "24 1 0:1 / / rw - tmpfs tmpfs rw\n",
        ):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module._linux_mountinfo_locality("/control space/agent-core", malformed)
        for filesystem in ("ceph", "glusterfs", "lustre", "afs", "virtiofs", "davfs2"):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module._linux_mountinfo_locality(
                    "/control", f"24 1 0:1 / /control rw - {filesystem} synthetic rw\n")
        self.assertRegex(promote_module._linux_mountinfo_locality(
            "/control", "24 1 0:1 / /control rw - futurelocalfs synthetic rw\n"), r"^[0-9a-f]{64}$")
        literal_backslash_mount = (
            "24 1 8:1 / / rw - ext4 /dev/sda rw\n"
            "35 24 0:42 / /control\\134remote rw - ceph cluster rw\n"
        )
        raw_path = SimpleNamespace(resolve=lambda: "/control\\remote/agent-core")
        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
            promote_module._linux_control_filesystem(raw_path, literal_backslash_mount)
        with patch.object(promote_module, "_darwin_statfs", return_value=(0x1000, "apfs")), \
                patch.object(promote_module.os, "stat", return_value=SimpleNamespace(st_dev=7)):
            self.assertRegex(promote_module._darwin_control_filesystem(Path("/synthetic")), r"^[0-9a-f]{64}$")
        for flags, filesystem, device in ((0, "apfs", 7), (0x1000, "fuse", 7), (0x1000, "apfs", None)):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module._darwin_control_filesystem(
                    Path("/synthetic"), flags=flags, filesystem=filesystem, device=device)
        for result in ((0, "apfs"), (0x1000, ""), (0x1000, "macfuse")):
            with patch.object(promote_module, "_darwin_statfs", return_value=result), \
                    patch.object(promote_module.os, "stat", return_value=SimpleNamespace(st_dev=7)):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._darwin_control_filesystem(Path("/synthetic"))
        for error in (OSError("statfs"), AttributeError("statfs symbol")):
            with patch.object(promote_module, "_darwin_statfs", side_effect=error):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._darwin_control_filesystem(Path("/synthetic"))
        with patch.object(Path, "read_text", side_effect=OSError("mountinfo")):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module._linux_control_filesystem(Path("/synthetic"))
        item = self.item_id("e")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        with patch.object(promote_module, "_control_filesystem_sha256", return_value="0" * 64), \
                patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")):
            with self.assertRaisesRegex(ConfigError, "FAIL_PLAN_HASH"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)

    def test_local_control_base_uses_macos_convention(self) -> None:
        home = Path("C:" + "/synthetic-home")
        self.assertEqual(
            promote_module._posix_control_base("darwin", home),
            home / "Library" / "Application Support",
        )

    def test_transaction_cli_accepts_explicit_config_without_legacy_default(self) -> None:
        candidate_args = cli_module._parser().parse_args([
            "candidate", "publish", "--state", "state", "--id", self.item_id("a"),
            "--config", "host.json",
        ])
        self.assertEqual(candidate_args.config, Path("host.json"))
        self.assertIsNone(candidate_args.control_root)
        recover_args = cli_module._parser().parse_args([
            "recover", "--state", "state", "--config", "host.json", "--sha", "a" * 40,
            "--apply", "--plan-hash", "b" * 64, "--expected-remote-sha", "c" * 40,
        ])
        self.assertEqual(recover_args.config, Path("host.json"))
        self.assertIsNone(recover_args.control_root)
        self.assertTrue(recover_args.apply)
        self.assertEqual(recover_args.plan_hash, "b" * 64)
        self.assertEqual(recover_args.expected_remote_sha, "c" * 40)

    @unittest.skipUnless(os.name == "nt", "Windows local control root behavior")
    def test_windows_control_base_rejects_unc_nonfixed_and_alias_before_fetch(self) -> None:
        item = self.item_id("b")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        attempts = (
            ({"LOCALAPPDATA": "\\\\server\\share\\agent-core"}, None),
            ({"LOCALAPPDATA": str(self.local_appdata)},
             SimpleNamespace(windll=SimpleNamespace(kernel32=SimpleNamespace(GetDriveTypeW=lambda _path: 4)))),
        )
        for environment, ctypes_module in attempts:
            context = patch.dict(os.environ, environment)
            modules = patch.dict(sys.modules, {"ctypes": ctypes_module}) if ctypes_module else None
            with context:
                if modules:
                    modules.__enter__()
                try:
                    with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
                        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                            plan_publish(self.state, None, item, config_path=self.config)
                finally:
                    if modules:
                        modules.__exit__(None, None, None)
        target = Path(self.temp.name) / "local-target"
        target.mkdir()
        alias = Path(self.temp.name) / "local-alias"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                                check=False, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            with patch.dict(os.environ, {"LOCALAPPDATA": str(alias)}):
                with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
                    with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                        plan_publish(self.state, None, item, config_path=self.config)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.repo)}):
            with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    plan_publish(self.state, None, item, config_path=self.config)

    def test_canonical_recover_and_invalid_rollback_reject_before_side_effects(self) -> None:
        forbidden = (
            patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")),
            patch.object(promote_module, "operation_lock", side_effect=AssertionError("lock")),
            patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("ff")),
        )
        for call, missing, supplied in (
            (lambda config: recover_local(self.state, control_root=None, config_path=config),
             "FAIL_STATE_BINDING", "FAIL_CANONICAL_TRANSACTION_PENDING"),
            (lambda config: rollback(self.state, None, "20260815T000000000000Z", apply=True,
                                     plan_hash="a" * 64, expected_remote_sha="b" * 40, config_path=config),
             "FAIL_STATE_BINDING", "FAIL_ROLLBACK_ID"),
        ):
            with forbidden[0], forbidden[1], forbidden[2]:
                with self.assertRaisesRegex(ConfigError, missing):
                    call(None)
                with self.assertRaisesRegex(ConfigError, supplied):
                    call(self.config)

    def test_canonical_rollback_r0_plans_settled_snapshot_without_mutation(self) -> None:
        prepared, snapshot, journal = self.rollback_source("1")
        marker = "ROLLBACK_R0_MARKER"
        try:
            before_snapshot = {
                path.relative_to(snapshot.path): path.read_bytes()
                for path in snapshot.path.rglob("*") if path.is_file()
            }
            before_journal = journal.path.read_bytes()
            before_control = {
                path.relative_to(prepared.control_root): path.read_bytes()
                for path in prepared.control_root.rglob("*") if path.is_file()
            }
            before_status = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(promote_module, "operation_lock", side_effect=AssertionError(marker)) as lock, \
                    patch.object(promote_module, "_new_worktree", side_effect=AssertionError(marker)), \
                    patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError(marker)), \
                    patch.object(promote_module, "_push", side_effect=AssertionError(marker)), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError(marker)):
                plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((plan.operation, plan.candidate_id, plan.expected_remote_sha),
                             ("rollback", snapshot.snapshot_id, prepared.sha))
            self.assertEqual(plan.payload["settlement_kind"], "original-completed")
            self.assertEqual(plan.payload["restore_count"], len(promote_module._validate_snapshot_material(snapshot)["files"]))
            self.assertTrue(any(line.startswith("ROLLBACK_RESTORE_FACTS ") for line in plan.lines))
            self.assertFalse(any("state/" in line for line in plan.lines))
            self.assertEqual(observe.call_count, 1)
            self.assertEqual(lock.call_count, 0)
            self.assertEqual(before_snapshot, {
                path.relative_to(snapshot.path): path.read_bytes()
                for path in snapshot.path.rglob("*") if path.is_file()
            })
            self.assertEqual(before_journal, journal.path.read_bytes())
            self.assertEqual(before_control, {
                path.relative_to(prepared.control_root): path.read_bytes()
                for path in prepared.control_root.rglob("*") if path.is_file()
            })
            self.assertEqual(before_status, git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_binds_inverse_a_m_d_restore_facts(self) -> None:
        prepared, snapshot, _journal = self.rollback_promote_source("2")
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            manifest = promote_module._validate_snapshot_material(snapshot)
            facts, facts_digest = promote_module._canonical_rollback_restore_facts(manifest)
            self.assertEqual({fact["restore_status"] for fact in facts}, {"A", "M", "D"})
            self.assertEqual(plan.payload["restore_count"], 3)
            self.assertEqual(plan.payload["restore_facts_sha256"], facts_digest)
            self.assertNotIn("restore_facts", plan.payload)
            for fact in facts:
                self.assertEqual(set(fact), {"path", "restore_status", "from", "to"})
                self.assertTrue(set(fact["from"]) <= {"exists", "mode", "oid", "sha256"})
                self.assertTrue(set(fact["to"]) <= {"exists", "mode", "oid", "sha256"})
                if fact["restore_status"] == "D":
                    self.assertEqual(fact["from"]["exists"], True)
                    self.assertEqual(fact["to"], {"exists": False})
                elif fact["restore_status"] == "A":
                    self.assertEqual(fact["from"], {"exists": False})
                    self.assertEqual(fact["to"]["exists"], True)
                else:
                    self.assertEqual((fact["from"]["exists"], fact["to"]["exists"]), (True, True))
            def public_values(value):
                if isinstance(value, dict):
                    for child in value.values():
                        yield from public_values(child)
                elif isinstance(value, (tuple, list)):
                    for child in value:
                        yield from public_values(child)
                else:
                    yield str(value)
            public_text = "\n".join((*public_values(plan.payload), str(plan), repr(plan), *plan.lines))
            self.assertNotIn("state/", public_text)
            self.assertNotIn(facts[0]["path"], public_text)
            if "oid" in facts[0]["from"]:
                self.assertNotIn(facts[0]["from"]["oid"], public_text)
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_accepts_only_settling_local_finalization_recovery(self) -> None:
        prepared, snapshot, journal = self.rollback_recovery_source("3")
        try:
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            checkpoint = self.recovery_checkpoint(
                prepared, snapshot, journal, "local-finalization", observed_sha=prepared.sha,
            )
            for event in ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"):
                checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event, observed_sha=prepared.sha)
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((plan.payload["settlement_kind"], plan.payload["settlement_record_sha256"]),
                             ("recovery-local-finalization", checkpoint.record_sha256))
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_cleanup_pending_before_observation(self) -> None:
        prepared, snapshot, journal = self.rollback_source("4")
        marker = "ROLLBACK_CLEANUP_MARKER"
        try:
            append_journal_event(journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_CLEANUP_PENDING", "canonical rollback cleanup required"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_binding_or_remote_drift(self) -> None:
        prepared, snapshot, _journal = self.rollback_source("5")
        marker = "ROLLBACK_DRIFT_MARKER"
        try:
            original_config = self.config.read_bytes()
            self.config.write_bytes(original_config + b"\n")
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_BINDING_CHANGED", "canonical rollback binding changed"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, repr(raised.exception))
            self.config.write_bytes(original_config)
            control, original_binding = promote_module._canonical_recovery_binding(
                resolve_repository_context(self.state), None, self.config, prepared.expected_remote_sha,
            )
            for field, replacement in {
                "receipt_sha256": "0" * 64,
                "repository_root_sha": "1" * 40,
                "control_identity": str(Path(self.temp.name) / "reattached-control"),
                "control_filesystem_sha256": "2" * 64,
            }.items():
                with self.subTest(binding_field=field):
                    drifted = dict(original_binding)
                    drifted[field] = replacement
                    with patch.object(promote_module, "_canonical_recovery_binding", return_value=(control, drifted)), \
                            patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                        with self.assertRaises(ConfigError) as raised:
                            rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_ROLLBACK_BINDING_CHANGED", "canonical rollback binding changed"))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertNotIn(marker, repr(raised.exception))
                    self.assertEqual(observe.call_count, 0)
            descendant = git(prepared.txn, "commit-tree", f"{prepared.sha}^{{tree}}", "-p", prepared.sha,
                             "-m", "rollback descendant")
            unrelated = git(prepared.txn, "commit-tree", f"{prepared.sha}^{{tree}}", "-m", "rollback unrelated")
            for label, observed in (
                ("base", prepared.expected_remote_sha),
                ("descendant", descendant),
                ("unrelated", unrelated),
            ):
                with self.subTest(observed=label):
                    with patch.object(promote_module, "observe_canonical_remote_head", return_value=observed) as observe:
                        with self.assertRaises(ConfigError) as raised:
                            rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_ROLLBACK_REMOTE_CHANGED", "canonical rollback remote changed"))
                    self.assertEqual(observe.call_count, 1)
                    self.assertIsNone(raised.exception.__cause__)
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_artifact_aliases_links_and_ambiguity_before_observation(self) -> None:
        marker = "ROLLBACK_ARTIFACT_IDENTITY_MARKER"

        def rejected(snapshot_id: str, expected: str) -> None:
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             (expected, "canonical rollback artifacts" if expected == "FAIL_ROLLBACK_ARTIFACT_SCOPE"
                              else "canonical rollback incomplete"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual(observe.call_count, 0)

        cases = ("snapshot-alias", "manifest-hardlink", "data-hardlink", "journal-hardlink", "duplicate-original")
        for suffix, kind in zip("6789a", cases, strict=True):
            with self.subTest(kind=kind):
                prepared, snapshot, journal = self.rollback_source(suffix)
                alias_target: Path | None = None
                external: Path | None = None
                external_bytes: bytes | None = None
                aliased_manifest: bytes | None = None
                duplicate: Path | None = None
                try:
                    if kind == "snapshot-alias":
                        alias_target = snapshot.path.with_name(snapshot.path.name + "-sentinel")
                        aliased_manifest = (snapshot.path / "manifest.json").read_bytes()
                        snapshot.path.rename(alias_target)
                        try:
                            os.symlink(alias_target, snapshot.path, target_is_directory=True)
                        except OSError:
                            alias_target.rename(snapshot.path)
                            self.skipTest("directory symlink unavailable for rollback alias identity subcase")
                    elif kind == "manifest-hardlink":
                        external = Path(self.temp.name) / f"{kind}.bin"
                        try:
                            os.link(snapshot.path / "manifest.json", external)
                            external_bytes = external.read_bytes()
                        except OSError:
                            self.skipTest("hardlinks unavailable for rollback manifest identity subcase")
                    elif kind == "data-hardlink":
                        manifest = promote_module._validate_snapshot_material(snapshot)
                        data = next(snapshot.path / item["after"]["data"] for item in manifest["files"]
                                    if item["after"]["exists"])
                        external = Path(self.temp.name) / f"{kind}.bin"
                        try:
                            os.link(data, external)
                            external_bytes = external.read_bytes()
                        except OSError:
                            self.skipTest("hardlinks unavailable for rollback data identity subcase")
                    elif kind == "journal-hardlink":
                        external = Path(self.temp.name) / f"{kind}.jsonl"
                        try:
                            os.link(journal.path, external)
                            external_bytes = external.read_bytes()
                        except OSError:
                            self.skipTest("hardlinks unavailable for rollback journal identity subcase")
                    else:
                        duplicate = self.duplicate_original_journal(journal)
                    rejected(snapshot.snapshot_id, "FAIL_ROLLBACK_ARTIFACT_SCOPE")
                    if external is not None:
                        self.assertTrue(external.exists())
                        self.assertEqual(external.read_bytes(), external_bytes)
                    if alias_target is not None and aliased_manifest is not None:
                        self.assertEqual((alias_target / "manifest.json").read_bytes(), aliased_manifest)
                finally:
                    if snapshot.path.is_symlink():
                        snapshot.path.unlink()
                    if alias_target is not None and alias_target.exists() and not snapshot.path.exists():
                        alias_target.rename(snapshot.path)
                    if external is not None and external.exists():
                        external.unlink()
                    if duplicate is not None and duplicate.exists():
                        duplicate.unlink()
                    self.cleanup(prepared)

        prepared, snapshot, journal = self.rollback_recovery_source("b")
        try:
            self.recovery_checkpoint(prepared, snapshot, journal)
            self.recovery_checkpoint(prepared, snapshot, journal)
            rejected(snapshot.snapshot_id, "FAIL_ROLLBACK_NOT_SETTLED")
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_recomputed_manifest_and_git_scope_mutations_before_observation(self) -> None:
        prepared, snapshot, journal = self.rollback_promote_source("c")
        marker = "ROLLBACK_SCOPE_MUTATION_MARKER"
        original_files = {
            path.relative_to(snapshot.path): path.read_bytes()
            for path in snapshot.path.rglob("*") if path.is_file()
        }
        original_journal = journal.path.read_bytes()

        def restore() -> None:
            for path in sorted(snapshot.path.rglob("*"), reverse=True):
                if path.is_file() and path.relative_to(snapshot.path) not in original_files:
                    path.unlink()
            for relative, data in original_files.items():
                target = snapshot.path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            journal.path.write_bytes(original_journal)

        def rewrite(payload: dict[str, object]) -> None:
            payload["manifest_sha256"] = promote_module._record_hash(payload, "manifest_sha256")
            (snapshot.path / "manifest.json").write_bytes(promote_module._canonical_bytes(payload))
            rows = [json.loads(line) for line in original_journal.splitlines()]
            rows[0]["snapshot_manifest_sha256"] = payload["manifest_sha256"]
            previous = None
            for row in rows:
                row["previous_record_sha256"] = previous
                row["record_sha256"] = promote_module._record_hash(row, "record_sha256")
                previous = row["record_sha256"]
            journal.path.write_bytes(b"".join(promote_module._canonical_bytes(row) + b"\n" for row in rows))

        def refused() -> None:
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual(observe.call_count, 0)

        try:
            original_manifest = json.loads(original_files[Path("manifest.json")].decode("utf-8"))
            original_entry = next(item for item in original_manifest["files"]
                                  if item["before"]["exists"] and item["after"]["exists"])
            for kind in ("swap", "base-tree", "target-tree", "mode", "oid", "data", "extra-file",
                         "extra-diff", "outside-state", "casefold", "engine", "root"):
                with self.subTest(kind=kind):
                    restore()
                    payload = json.loads(json.dumps(original_manifest))
                    entry = next(item for item in payload["files"] if item["path"] == original_entry["path"])
                    if kind == "swap":
                        entry["before"], entry["after"] = entry["after"], entry["before"]
                    elif kind == "base-tree":
                        entry["before"] = dict(entry["after"])
                    elif kind == "target-tree":
                        entry["after"] = dict(entry["before"])
                    elif kind == "mode":
                        entry["after"]["mode"] = "100755"
                    elif kind == "oid":
                        entry["after"]["oid"] = "0" * 40
                    elif kind == "data":
                        (snapshot.path / entry["after"]["data"]).write_bytes(b"ROLLBACK_DATA_TAMPER")
                    elif kind == "extra-file":
                        (snapshot.path / "files" / "extra.bin").write_bytes(b"extra")
                    elif kind in {"extra-diff", "casefold"}:
                        clone = json.loads(json.dumps(entry))
                        for side in ("before", "after"):
                            if clone[side]["exists"]:
                                clone[side]["data"] = f"files/{kind}-{side}.bin"
                                (snapshot.path / clone[side]["data"]).write_bytes(
                                    (snapshot.path / entry[side]["data"]).read_bytes())
                        clone["path"] = (entry["path"].replace("inbox", "INBOX") if kind == "casefold"
                                         else "state/extra-diff.md")
                        payload["files"].append(clone)
                    elif kind == "outside-state":
                        entry["path"] = "outside/private.md"
                    elif kind == "engine":
                        entry["path"] = "engine/agent_core/promote.py"
                    else:
                        entry["path"] = "ROOT_GOVERNANCE"
                    if kind != "data" and kind != "extra-file":
                        rewrite(payload)
                    refused()
        finally:
            restore()
            self.cleanup(prepared)

    def test_canonical_rollback_r0_stable_reread_rejects_post_observation_local_drift(self) -> None:
        """One pinned remote read cannot bless source artifacts changed while it was observed."""
        for suffix, kind in zip("def0", ("cleanup", "duplicate", "snapshot", "binding"), strict=True):
            with self.subTest(kind=kind):
                prepared, snapshot, journal = self.rollback_source(suffix)
                original_config = self.config.read_bytes()
                duplicate: Path | None = None
                original_manifest = (snapshot.path / "manifest.json").read_bytes()
                marker = f"ROLLBACK_STABLE_{kind.upper()}_MARKER"
                try:
                    def mutate() -> None:
                        nonlocal duplicate
                        if kind == "cleanup":
                            append_journal_event(journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
                        elif kind == "duplicate":
                            duplicate = journal.path.parent / f"{promote_module._artifact_id()}.jsonl"
                            records = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
                            records[0]["operation_id"] = duplicate.stem
                            previous = None
                            for record in records:
                                record["previous_record_sha256"] = previous
                                record["record_sha256"] = promote_module._record_hash(record, "record_sha256")
                                previous = record["record_sha256"]
                            duplicate.write_bytes(b"".join(promote_module._canonical_bytes(record) + b"\n" for record in records))
                        elif kind == "snapshot":
                            manifest = json.loads(original_manifest.decode("utf-8"))
                            manifest["plan_hash"] = "f" * 64
                            manifest["manifest_sha256"] = promote_module._record_hash(manifest, "manifest_sha256")
                            (snapshot.path / "manifest.json").write_bytes(promote_module._canonical_bytes(manifest))
                        else:
                            self.config.write_bytes(original_config + b"\n")

                    real_observe = promote_module.observe_canonical_remote_head
                    def observe_then_mutate(repo: Path) -> str:
                        observed = real_observe(repo)
                        mutate()
                        return observed
                    with patch.object(promote_module, "observe_canonical_remote_head", side_effect=observe_then_mutate) as observe, \
                            patch.object(promote_module, "operation_lock", side_effect=AssertionError(marker)), \
                            patch.object(promote_module, "_new_worktree", side_effect=AssertionError(marker)), \
                            patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError(marker)):
                        with self.assertRaises(ConfigError) as raised:
                            rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state"))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertNotIn(marker, str(raised.exception))
                    self.assertNotIn(marker, repr(raised.exception))
                    self.assertEqual(observe.call_count, 1)
                finally:
                    self.config.write_bytes(original_config)
                    (snapshot.path / "manifest.json").write_bytes(original_manifest)
                    if duplicate is not None and duplicate.exists():
                        duplicate.unlink()
                    self.cleanup(prepared)

    def test_canonical_rollback_r0_accepts_rollback_original_and_recovery_settlement(self) -> None:
        prepared, snapshot, journal = self.rollback_source("8")
        try:
            snapshot, _journal = self.rewrite_rollback_source(snapshot, journal)
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((plan.payload["original_operation"], plan.payload["settlement_kind"]),
                             ("rollback", "original-completed"))
        finally:
            self.cleanup(prepared)

        prepared, snapshot, journal = self.rollback_recovery_source("9")
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            checkpoint = self.recovery_checkpoint(
                prepared, snapshot, journal, "local-finalization", observed_sha=prepared.sha,
                original_operation="rollback",
            )
            for event in ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"):
                if event == "completed":
                    checkpoint = self.append_recovery_checkpoint(
                        prepared, checkpoint, "worktree-cleanup-intent", observed_sha=prepared.sha,
                    )
                checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event, observed_sha=prepared.sha)
            checkpoint = self.append_recovery_checkpoint(
                prepared, checkpoint, "cleanup-pending", observed_sha=prepared.sha,
            )
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((plan.payload["original_operation"], plan.payload["settlement_kind"]),
                             ("rollback", "recovery-local-finalization"))
            self.assertEqual(plan.payload["settlement_record_sha256"], checkpoint.record_sha256)
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_legacy_candidate_in_self_consistent_rollback_artifacts(self) -> None:
        prepared, snapshot, journal = self.rollback_source("a")
        marker = "ROLLBACK_LEGACY_CANDIDATE_MARKER"
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            snapshot, _journal = self.rewrite_legacy_rollback_candidate(snapshot, journal)
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual(observe.call_count, 0)
            # Counterfactual control: widening only the canonical proof's ID matcher
            # makes the otherwise self-consistent source plan, so this mutation is
            # pinned to the production ARTIFACT_ID gate rather than an early fixture error.
            legacy_or_artifact = re.compile(
                rf"(?:{promote_module.ARTIFACT_ID_RE.pattern}|{promote_module.ROLLBACK_ID_RE.pattern})"
            )
            with patch.object(promote_module, "ARTIFACT_ID_RE", legacy_or_artifact):
                counterfactual = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((counterfactual.payload["original_operation"], counterfactual.payload["settlement_kind"]),
                             ("rollback", "original-completed"))
        finally:
            self.cleanup(prepared)

    def test_canonical_rollback_r0_rejects_unsettling_rollback_recovery_records(self) -> None:
        scenarios = (
            ("wrong-action", "artifact-cleanup",
             ("worktree-cleanup-intent", "worktree-cleaned", "converged", "completed"), None,
             "FAIL_ROLLBACK_NOT_SETTLED"),
            ("bare-completed", "local-finalization",
             ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"),
             None, "FAIL_ROLLBACK_NOT_SETTLED"),
            ("multiple", "local-finalization",
             ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"),
             "duplicate", "FAIL_ROLLBACK_NOT_SETTLED"),
        )
        for item_suffix, (label, action, events, extra, expected) in zip("bcd", scenarios, strict=True):
            with self.subTest(scenario=label):
                prepared, snapshot, journal = self.rollback_recovery_source(item_suffix)
                try:
                    snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
                    git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
                    checkpoint = self.recovery_checkpoint(
                        prepared, snapshot, journal, action, observed_sha=prepared.sha,
                        original_operation="rollback",
                    )
                    for event in events:
                        checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event, observed_sha=prepared.sha)
                    if extra == "duplicate":
                        duplicate = self.recovery_checkpoint(
                            prepared, snapshot, journal, action, observed_sha=prepared.sha,
                            original_operation="rollback",
                        )
                        for event in events:
                            duplicate = self.append_recovery_checkpoint(prepared, duplicate, event, observed_sha=prepared.sha)
                    with self.assertRaises(ConfigError) as raised:
                        rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    self.assertEqual(raised.exception.code, expected)
                    self.assertIsNone(raised.exception.__cause__)
                finally:
                    self.cleanup(prepared)

    def test_canonical_rollback_r0_settlement_matrix_refuses_nonsettled_recovery_variants(self) -> None:
        scenarios = (
            ("artifact", "artifact-cleanup", ("worktree-cleanup-intent", "worktree-cleaned", "converged", "completed"), None),
            ("input", "input-disposition", ("source-preserved", "converged", "completed"), None),
            ("cleanup", "cleanup-only", ("quarantine-delete-intent", "quarantine-deleted", "converged", "completed"), None),
            ("failed", "local-finalization", ("failed",), None),
            ("converged", "local-finalization", ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged"), None),
            ("observed-mismatch", "local-finalization", ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"), "observed"),
            ("multiple", "local-finalization", ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"), "duplicate"),
        )
        for item_suffix, (label, action, events, extra) in zip("0123456", scenarios, strict=True):
            with self.subTest(scenario=label):
                prepared, snapshot, journal = self.rollback_recovery_source(item_suffix)
                try:
                    observed = prepared.expected_remote_sha if extra == "observed" else prepared.sha
                    checkpoint = self.recovery_checkpoint(prepared, snapshot, journal, action, observed_sha=observed)
                    for event in events:
                        checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event, observed_sha=observed)
                    if extra == "duplicate":
                        duplicate = self.recovery_checkpoint(prepared, snapshot, journal, action, observed_sha=prepared.sha)
                        for event in events:
                            duplicate = self.append_recovery_checkpoint(prepared, duplicate, event, observed_sha=prepared.sha)
                    with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("settlement observe")) as observe:
                        with self.assertRaises(ConfigError) as raised:
                            rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete"))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertEqual(observe.call_count, 0)
                finally:
                    self.cleanup(prepared)

    def test_canonical_rollback_r0_public_boundary_and_cli_close_lower_error_details(self) -> None:
        target = "20260820T000000000000Z-" + "a" * 16
        marker = "ROLLBACK_PUBLIC_BOUNDARY_MARKER/C:" + "/private"
        cases = (
            ("FAIL_ROLLBACK_BINDING_CHANGED", "canonical rollback binding changed", lambda: ConfigError("FAIL_ROLLBACK_BINDING_CHANGED", marker)),
            ("FAIL_ROLLBACK_CLEANUP_PENDING", "canonical rollback cleanup required", lambda: ConfigError("FAIL_ROLLBACK_CLEANUP_PENDING", marker)),
            ("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete", lambda: ConfigError("FAIL_ROLLBACK_NOT_SETTLED", marker)),
            ("FAIL_ROLLBACK_REMOTE_CHANGED", "canonical rollback remote changed", lambda: ConfigError("FAIL_ROLLBACK_REMOTE_CHANGED", marker)),
            ("FAIL_ROLLBACK_SCOPE_UNVERIFIED", "canonical rollback scope unverified", lambda: ConfigError("FAIL_ROLLBACK_SCOPE_UNVERIFIED", marker)),
            ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state", lambda: ConfigError("FAIL_ROLLBACK_INDETERMINATE", marker)),
            ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts", lambda: ConfigError("FAIL_ROLLBACK_ARTIFACT_SCOPE", marker)),
            ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts", lambda: ConfigError("FAIL_GIT", marker)),
            ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state", lambda: OSError(marker)),
            ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state", lambda: UnicodeError(marker)),
        )
        for expected_code, expected_detail, factory in cases:
            with self.subTest(expected=expected_code, exception=factory().__class__.__name__):
                with patch.object(promote_module, "_plan_canonical_rollback", side_effect=factory()):
                    with self.assertRaises(ConfigError) as raised:
                        rollback(self.state, None, target, apply=False, config_path=self.config)
                self.assertEqual((raised.exception.code, raised.exception.detail), (expected_code, expected_detail))
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))

                stderr = io.StringIO()
                with patch.object(promote_module, "_plan_canonical_rollback", side_effect=factory()), \
                        patch("sys.stderr", stderr):
                    self.assertEqual(cli_module.main([
                        "rollback", "--state", str(self.state), "--config", str(self.config), "--to", target,
                    ]), 1)
                rendered = stderr.getvalue()
                self.assertEqual(rendered, f"{expected_code} {expected_detail}\n")
                self.assertNotIn(marker, rendered)
                self.assertNotIn("C:" + "/private", rendered)

    def test_canonical_rollback_r1a_prepares_inverse_promote_snapshot(self) -> None:
        """R1a builds the inverse detached commit used by the R1b apply core."""
        source, snapshot, _journal = self.rollback_promote_source("e")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            before_main = git(self.repo, "rev-parse", "HEAD")
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            self.assertEqual((rollback_prepared.operation, rollback_prepared.candidate_id,
                              rollback_prepared.expected_remote_sha),
                             ("rollback", snapshot.snapshot_id, source.sha))
            self.assertEqual(rollback_prepared.input_digest_sha256, snapshot.manifest_sha256)
            self.assertIsNotNone(rollback_prepared.rollback_evidence)
            self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
            self.assertEqual(git(rollback_prepared.txn, "rev-list", "--parents", "-n", "1", rollback_prepared.sha).split(),
                             [rollback_prepared.sha, source.sha])
            manifest = promote_module._validate_snapshot_material(snapshot)
            expected = []
            for item in manifest["files"]:
                expected.append(("D" if item["before"] == {"exists": False}
                                 else "A" if item["after"] == {"exists": False} else "M", item["path"]))
            actual = promote_module._parse_name_status(promote_module._git_bytes(
                rollback_prepared.txn, "diff", "--no-renames", "--name-status", "-z", source.sha,
                rollback_prepared.sha,
            ))
            self.assertEqual(set(actual), set(expected))
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_main)
            # R1a does not call these writers, but their direct validators must
            # accept an ARTIFACT_ID rollback Prepared for the later R1b phase.
            derived_snapshot = create_canonical_snapshot(rollback_prepared)
            derived_journal = create_canonical_journal(rollback_prepared, derived_snapshot)
            self.assertEqual((promote_module._validate_snapshot(rollback_prepared, derived_snapshot)["operation"],
                              promote_module._read_journal(derived_journal.path, derived_journal.path.parent)[0]["operation"]),
                             ("rollback", "rollback"))
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1a_rejects_tokens_before_worktree_and_revalidates_locally(self) -> None:
        source, snapshot, _journal = self.rollback_source("f")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("rollback worktree")) as worktree:
                with self.assertRaises(ConfigError):
                    promote_module.prepare_rollback(
                        self.state, None, plan, "0" * 64, plan.expected_remote_sha, config_path=self.config,
                    )
            self.assertEqual(worktree.call_count, 0)
            with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("rollback worktree")) as worktree:
                with self.assertRaises(ConfigError) as raised:
                    promote_module.prepare_rollback(
                        self.state, None, plan, plan.plan_hash, "0" * 40, config_path=self.config,
                    )
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_REMOTE_SHA", f"expected={plan.expected_remote_sha} actual={'0' * 40}"))
            self.assertEqual(worktree.call_count, 0)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("observe")), \
                    patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
                facts = promote_module.revalidate_canonical_prepared_locked(rollback_prepared)
            self.assertEqual(tuple(item.path for item in facts), rollback_prepared.changed_paths)
            promote_module._git(source.repo, "worktree", "remove", "--force", str(rollback_prepared.txn))
            with self.assertRaises(ConfigError) as raised:
                promote_module.revalidate_canonical_prepared_locked(rollback_prepared)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical rollback revalidation"))
            rollback_prepared = None
        finally:
            if rollback_prepared is not None and rollback_prepared.txn.exists():
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_apply_retains_registered_worktree(self) -> None:
        """R1b applies the reviewed inverse once and journals inert worktree residue."""
        source, snapshot, _journal = self.rollback_promote_source("0")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            original_snapshot = snapshot.path / "manifest.json"
            before_snapshot = original_snapshot.read_bytes()
            real_observe = promote_module.observe_canonical_remote_head
            real_push = promote_module.canonical_push_exact_lease

            def pushed_with_nonzero_return(candidate):
                real_push(candidate)
                return promote_module.CanonicalPushResult(1)

            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(promote_module, "canonical_push_exact_lease", side_effect=pushed_with_nonzero_return) as push, \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError("rollback cleanup")) as cleanup:
                result = apply_prepared(rollback_prepared)
            self.assertEqual((observe.call_count, push.call_count, cleanup.call_count), (2, 1, 0))
            self.assertEqual((result.sha, result.cleanup_pending, result.cleanup_kind),
                             (rollback_prepared.sha, True, "worktree"))
            self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
            self.assertEqual(original_snapshot.read_bytes(), before_snapshot)
            records = [
                promote_module._read_journal(path, path.parent)
                for path in (rollback_prepared.control_root / "journal").glob("*.jsonl")
            ]
            applied = next(chain for chain in records if chain[0]["operation"] == "rollback")
            self.assertEqual(
                (applied[-2]["event"], applied[-1]["event"], applied[-1]["details"]),
                ("completed", "cleanup_pending", {"phase": "worktree", "kind": "worktree"}),
            )
        finally:
            if rollback_prepared is not None and rollback_prepared.txn.exists():
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_cli_apply_emits_closed_residue_result(self) -> None:
        source, snapshot, _journal = self.rollback_promote_source("1")
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            before_txns = set((source.control_root / "txn").iterdir())
            before_journals = set((source.control_root / "journal").glob("*.jsonl"))
            output = io.StringIO()
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch("sys.stdout", output):
                self.assertEqual(cli_module.main([
                    "rollback", "--state", str(self.state), "--config", str(self.config), "--to", snapshot.snapshot_id,
                    "--apply", "--plan-hash", plan.plan_hash, "--expected-remote-sha", plan.expected_remote_sha,
                ]), 0)
            self.assertEqual(observe.call_count, 2)
            self.assertTrue(output.getvalue().startswith("PASS remote_sha="))
            new_journals = set((source.control_root / "journal").glob("*.jsonl")) - before_journals
            self.assertEqual(len(new_journals), 1)
            applied = promote_module._read_journal(next(iter(new_journals)), source.control_root / "journal")
            self.assertIn(f"rollback={applied[0]['snapshot_id']}", output.getvalue())
            self.assertTrue(output.getvalue().endswith("cleanup_pending=true cleanup_kind=worktree\n"))
            proof = promote_module._canonical_rollback_proof(
                promote_module.resolve_repository_context(self.state), snapshot.snapshot_id, self.config,
            )
            original_binding = proof[3]
            self.assertEqual(original_binding["remote_revision"], plan.payload["expected_base_sha"])
            artifact_binding = dict(original_binding)
            artifact_binding["remote_revision"] = plan.expected_remote_sha
            self.assertNotEqual(promote_module._binding_digest(original_binding), promote_module._binding_digest(artifact_binding))
            self.assertEqual(applied[0]["binding_digest_sha256"], promote_module._binding_digest(artifact_binding))
            for txn in set((source.control_root / "txn").iterdir()) - before_txns:
                _cleanup_worktree(self.repo, source.control_root, txn)
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1b1_prepush_token_drift_retains_capsule(self) -> None:
        source, snapshot, _journal = self.rollback_source("2")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            marker = "ROLLBACK_PREPUSH_DRIFT_MARKER/C:" + "/private"
            drifted = replace(rollback_prepared, expected_remote_sha="f" * 40)
            with patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError(marker)) as push, \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError(marker)) as cleanup:
                with self.assertRaises(ConfigError) as raised:
                    apply_prepared(drifted)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_INPUT_CHANGED", "canonical rollback plan"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual((push.call_count, cleanup.call_count), (0, 0))
            self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_postpush_crash_recovers_with_retained_worktree(self) -> None:
        source, snapshot, _journal = self.rollback_source("3")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            marker = "ROLLBACK_POSTPUSH_MARKER/C:" + "/private"
            with patch.object(promote_module, "fast_forward_local", side_effect=OSError(marker)), \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError(marker)) as cleanup:
                with self.assertRaises(ConfigError) as raised:
                    apply_prepared(rollback_prepared)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("REMOTE_COMMITTED_LOCAL_STALE", "rollback"))
            self.assertEqual(cleanup.call_count, 0)
            self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
            rollback_chain = next(
                promote_module._read_journal(path, path.parent)
                for path in (rollback_prepared.control_root / "journal").glob("*.jsonl")
                if promote_module._read_journal(path, path.parent)[0].get("prepared_commit_sha") == rollback_prepared.sha
            )
            rollback_snapshot_id = rollback_chain[0]["snapshot_id"]
            self.cleanup(source)
            recovery = plan_canonical_recovery(self.state, rollback_prepared.sha, config_path=self.config)
            self.assertEqual(recovery.payload["action"], "local-finalization")
            with patch.object(promote_module, "_recovery_postconvergence_worktrees", side_effect=AssertionError(marker)) as remover:
                result = apply_canonical_recovery(
                    self.state, rollback_prepared.sha, recovery.plan_hash, recovery.expected_remote_sha,
                    config_path=self.config,
                )
            self.assertEqual((result.converged, result.cleanup_pending, result.cleanup_kind), (True, True, "worktree"))
            self.assertEqual(remover.call_count, 0)
            next_plan = rollback(self.state, None, rollback_snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((next_plan.payload["original_operation"], next_plan.payload["settlement_kind"]),
                             ("rollback", "recovery-local-finalization"))
        finally:
            if rollback_prepared is not None and rollback_prepared.txn.exists():
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_direct_boundary_sanitizes_and_preserves_capsule(self) -> None:
        source, snapshot, _journal = self.rollback_source("4")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            marker = "ROLLBACK_DIRECT_BOUNDARY_MARKER/C:" + "/private"
            control_marker = Path(self.temp.name) / "ROLLBACK_CONTROL_DRIFT_MARKER"
            cases = (
                ("resolver", patch.object(promote_module, "resolve_repository_context",
                                             side_effect=ConfigError("FAIL_GIT", marker)),
                 ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts"), rollback_prepared),
                ("lock", patch.object(promote_module, "operation_lock", side_effect=OSError(marker)),
                 ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state"), rollback_prepared),
                ("capsule", None, ("FAIL_ROLLBACK_ARTIFACT_SCOPE", "canonical rollback artifacts"),
                 replace(rollback_prepared, tree_oid="0" * 40)),
                ("control", None, ("FAIL_ROLLBACK_INDETERMINATE", "canonical rollback state"),
                 replace(rollback_prepared, control_root=control_marker)),
            )
            for label, failure_patch, expected, candidate in cases:
                with self.subTest(boundary=label), \
                        patch.object(promote_module, "create_canonical_snapshot", side_effect=AssertionError(marker)) as snapshot_writer, \
                        patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError(marker)) as push, \
                        patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError(marker)) as cleanup:
                    if failure_patch is None:
                        with self.assertRaises(ConfigError) as raised:
                            apply_prepared(candidate)
                    else:
                        with failure_patch:
                            with self.assertRaises(ConfigError) as raised:
                                apply_prepared(candidate)
                self.assertEqual((raised.exception.code, raised.exception.detail), expected)
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertEqual((snapshot_writer.call_count, push.call_count, cleanup.call_count), (0, 0, 0))
                self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
                self.assertFalse(os.path.lexists(control_marker))
        finally:
            if rollback_prepared is not None and rollback_prepared.txn.exists():
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_scope_failure_is_not_local_finalization(self) -> None:
        source, snapshot, _journal = self.rollback_source("5")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            marker = "ROLLBACK_SCOPE_MARKER/C:" + "/private"
            real_observe = promote_module.observe_canonical_remote_head
            real_push = promote_module.canonical_push_exact_lease
            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(promote_module, "canonical_push_exact_lease", wraps=real_push) as push, \
                    patch.object(promote_module, "validate_canonical_descendant_scope",
                                 side_effect=ConfigError("FAIL_GIT", marker)), \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError(marker)) as cleanup:
                with self.assertRaises(ConfigError) as raised:
                    apply_prepared(rollback_prepared)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("REMOTE_COMMITTED_SCOPE_UNVERIFIED", "rollback"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual((observe.call_count, push.call_count, cleanup.call_count), (2, 1, 0))
            self.assertTrue(promote_module._rollback_worktree_registered(rollback_prepared))
        finally:
            if rollback_prepared is not None and rollback_prepared.txn.exists():
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b1_remote_outcomes_use_postpush_truth(self) -> None:
        """Each outcome is classified from the sole post-lease observation, never its return code."""
        cases = (
            ("lease-reject", "6", "FAIL_REMOTE_RACE"),
            ("lost", "7", "FAIL_REMOTE_RACE"),
            ("unsafe", "8", "FAIL_REMOTE_REWIND"),
            ("unknown", "9", "REMOTE_OUTCOME_UNKNOWN"),
            ("descendant", "a", None),
        )
        for label, suffix, expected in cases:
            with self.subTest(outcome=label):
                source, snapshot, _journal = self.rollback_source(suffix)
                rollback_prepared = None
                extra = None
                try:
                    plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    rollback_prepared = promote_module.prepare_rollback(
                        self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                    )
                    real_push = promote_module.canonical_push_exact_lease
                    real_observe = promote_module.observe_canonical_remote_head

                    def external_child(parent: str, name: str) -> tuple[Path, str]:
                        txn = _new_worktree(self.repo, self.control, parent)
                        path = txn / "state" / f"r1b-{name}.md"
                        path.write_text(f"{name}\n", encoding="utf-8")
                        git(txn, "add", path.relative_to(txn).as_posix())
                        git(txn, "commit", "-q", "-m", f"r1b {name}")
                        return txn, git(txn, "rev-parse", "HEAD")

                    def outcome_push(candidate):
                        nonlocal extra
                        if label == "lease-reject":
                            return promote_module.CanonicalPushResult(1)
                        if label == "lost":
                            extra, sha = external_child(candidate.expected_remote_sha, label)
                            git(extra, "push", "-q", "origin", f"{sha}:refs/heads/main")
                            return promote_module.CanonicalPushResult(1)
                        if label == "unsafe":
                            extra, sha = external_child(candidate.rollback_evidence.expected_base_sha, label)
                            git(extra, "push", "-q", "origin", f"{sha}:refs/heads/r1b-unsafe")
                            git(self.remote, "update-ref", "refs/heads/main", sha)
                            return promote_module.CanonicalPushResult(1)
                        if label == "descendant":
                            real_push(candidate)
                            extra, sha = external_child(candidate.sha, label)
                            git(extra, "push", "-q", "origin", f"{sha}:refs/heads/main")
                            return promote_module.CanonicalPushResult(1)
                        return promote_module.CanonicalPushResult(1)

                    observed_calls = 0

                    def outcome_observe(repo: Path) -> str:
                        nonlocal observed_calls
                        observed_calls += 1
                        if label == "unknown" and observed_calls == 2:
                            raise ConfigError("FAIL_GIT", "ROLLBACK_POSTOBSERVE_MARKER/C:" + "/private")
                        return real_observe(repo)

                    with patch.object(promote_module, "canonical_push_exact_lease", side_effect=outcome_push) as push, \
                            patch.object(promote_module, "observe_canonical_remote_head", side_effect=outcome_observe) as observe, \
                            patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError("cleanup")) as cleanup:
                        if expected is None:
                            result = apply_prepared(rollback_prepared)
                            self.assertTrue(result.cleanup_pending)
                            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), result.sha)
                        else:
                            with self.assertRaises(ConfigError) as raised:
                                apply_prepared(rollback_prepared)
                            self.assertEqual((raised.exception.code, raised.exception.detail), (expected, "rollback"))
                            self.assertIsNone(raised.exception.__cause__)
                            self.assertNotIn("ROLLBACK_POSTOBSERVE_MARKER", str(raised.exception))
                            self.assertNotIn("C:" + "/private", repr(raised.exception))
                    self.assertEqual((push.call_count, observe.call_count, cleanup.call_count), (1, 2, 0))
                    if expected is not None:
                        rollback_chain = next(
                            promote_module._read_journal(path, path.parent)
                            for path in (rollback_prepared.control_root / "journal").glob("*.jsonl")
                            if promote_module._read_journal(path, path.parent)[0].get("prepared_commit_sha") == rollback_prepared.sha
                        )
                        self.assertEqual(rollback_chain[-1]["details"]["code"], expected)
                finally:
                    if label == "unsafe" and rollback_prepared is not None:
                        git(self.remote, "update-ref", "refs/heads/main", rollback_prepared.expected_remote_sha)
                    if extra is not None and extra.exists():
                        _cleanup_worktree(self.repo, self.control, extra)
                    if rollback_prepared is not None and rollback_prepared.txn.exists():
                        self.cleanup(rollback_prepared)
                    self.cleanup(source)

    def test_canonical_rollback_r1b1_cli_lease_reject_is_closed(self) -> None:
        source, snapshot, _journal = self.rollback_source("b")
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            before_txns = set((source.control_root / "txn").iterdir())
            stderr = io.StringIO()
            with patch.object(promote_module, "canonical_push_exact_lease",
                              return_value=promote_module.CanonicalPushResult(1)) as push, \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError("cleanup")) as cleanup, \
                    patch("sys.stderr", stderr):
                self.assertEqual(cli_module.main([
                    "rollback", "--state", str(self.state), "--config", str(self.config), "--to", snapshot.snapshot_id,
                    "--apply", "--plan-hash", plan.plan_hash, "--expected-remote-sha", plan.expected_remote_sha,
                ]), 1)
            self.assertEqual(stderr.getvalue(), "FAIL_REMOTE_RACE rollback\n")
            self.assertEqual((push.call_count, cleanup.call_count), (1, 0))
            for txn in set((source.control_root / "txn").iterdir()) - before_txns:
                _cleanup_worktree(self.repo, source.control_root, txn)
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1b1_settlement_requires_exact_recovery_worktree_pending(self) -> None:
        cases = (("bare", "c"), ("worktree", "d"), ("artifact", "e"))
        for label, suffix in cases:
            with self.subTest(settlement=label):
                source, snapshot, _journal = self.rollback_source(suffix)
                rollback_prepared = None
                try:
                    plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
                    rollback_prepared = promote_module.prepare_rollback(
                        self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                    )
                    if label == "artifact":
                        with patch.object(promote_module, "canonical_push_exact_lease",
                                          return_value=promote_module.CanonicalPushResult(1)), \
                                patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError("cleanup")):
                            with self.assertRaisesRegex(ConfigError, "FAIL_REMOTE_RACE"):
                                apply_prepared(rollback_prepared)
                    else:
                        with patch.object(promote_module, "fast_forward_local", side_effect=OSError("ff seam")), \
                                patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError("cleanup")):
                            with self.assertRaisesRegex(ConfigError, "REMOTE_COMMITTED_LOCAL_STALE"):
                                apply_prepared(rollback_prepared)
                    chain_path = next(
                        path for path in (rollback_prepared.control_root / "journal").glob("*.jsonl")
                        if promote_module._read_journal(path, path.parent)[0].get("prepared_commit_sha") == rollback_prepared.sha
                    )
                    chain = promote_module._read_journal(chain_path, chain_path.parent)
                    derived_snapshot = promote_module.SnapshotRef(
                        chain[0]["snapshot_id"], rollback_prepared.control_root / "snapshots" / chain[0]["snapshot_id"],
                        chain[0]["snapshot_manifest_sha256"],
                    )
                    derived_journal = promote_module.JournalRef(
                        chain[0]["operation_id"], chain_path, len(chain) - 1, chain[-1]["record_sha256"],
                        chain[0]["control_identity_sha256"],
                    )
                    self.cleanup(source)
                    recovery = plan_canonical_recovery(self.state, rollback_prepared.sha, config_path=self.config)
                    expected_action = "artifact-cleanup" if label == "artifact" else "local-finalization"
                    self.assertEqual(recovery.payload["action"], expected_action)
                    if label == "bare":
                        checkpoint = promote_module.create_recovery_journal(
                            rollback_prepared.control_root,
                            promote_module._recovery_apply_baseline(recovery, chain[0]),
                        )
                        for event in ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"):
                            checkpoint = self.append_recovery_checkpoint(
                                rollback_prepared, checkpoint, event, observed_sha=rollback_prepared.sha,
                            )
                    else:
                        with patch.object(promote_module, "_recovery_postconvergence_worktrees",
                                          side_effect=AssertionError("remover")) as remover:
                            result = apply_canonical_recovery(
                                self.state, rollback_prepared.sha, recovery.plan_hash, recovery.expected_remote_sha,
                                config_path=self.config,
                            )
                        self.assertEqual((result.cleanup_pending, result.cleanup_kind), (True, "worktree"))
                        self.assertEqual(remover.call_count, 0)
                    if label == "worktree":
                        accepted = rollback(
                            self.state, None, derived_snapshot.snapshot_id, apply=False, config_path=self.config,
                        )
                        self.assertEqual(accepted.payload["settlement_kind"], "recovery-local-finalization")
                    else:
                        with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("observe")) as observe:
                            with self.assertRaises(ConfigError) as raised:
                                rollback(self.state, None, derived_snapshot.snapshot_id, apply=False, config_path=self.config)
                        self.assertEqual((raised.exception.code, raised.exception.detail),
                                         ("FAIL_ROLLBACK_NOT_SETTLED", "canonical rollback incomplete"))
                        self.assertIsNone(raised.exception.__cause__)
                        self.assertEqual(observe.call_count, 0)
                finally:
                    if rollback_prepared is not None and rollback_prepared.txn.exists():
                        self.cleanup(rollback_prepared)
                    self.cleanup(source)

    def test_canonical_rollback_r1a_revalidation_rejects_local_capsule_drift(self) -> None:
        source, snapshot, _journal = self.rollback_source("2")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            evidence = rollback_prepared.rollback_evidence
            self.assertIsNotNone(evidence)
            cases = (
                ("binding", replace(rollback_prepared, binding_digest_sha256="0" * 64)),
                ("txn-tree", replace(rollback_prepared, tree_oid="0" * 40)),
                ("registration", replace(rollback_prepared, txn=rollback_prepared.txn.parent / "missing-txn")),
                ("inverse-facts", replace(rollback_prepared, rollback_evidence=replace(
                    evidence, restore_facts_sha256="f" * 64))),
            )
            for label, mutated in cases:
                with self.subTest(drift=label), \
                        patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("observe")), \
                        patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")):
                    with self.assertRaises(ConfigError) as raised:
                        promote_module.revalidate_canonical_prepared_locked(mutated)
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_TRANSACTION_SCOPE", "canonical rollback revalidation"))
                    self.assertIsNone(raised.exception.__cause__)
            drift = rollback_prepared.txn / "state" / "r1a-drift.txt"
            drift.write_text("drift\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(ConfigError, "canonical rollback revalidation"):
                    promote_module.revalidate_canonical_prepared_locked(rollback_prepared)
            finally:
                drift.unlink()
            manifest_path = snapshot.path / "manifest.json"
            original_manifest = manifest_path.read_bytes()
            try:
                manifest = json.loads(original_manifest)
                manifest["plan_hash"] = "f" * 64 if manifest["plan_hash"] != "f" * 64 else "e" * 64
                # Keep the old manifest hash: this is a semantic artifact drift,
                # not harmless JSON whitespace.
                manifest_path.write_bytes(promote_module._canonical_bytes(manifest))
                with self.assertRaisesRegex(ConfigError, "canonical rollback revalidation"):
                    promote_module.revalidate_canonical_prepared_locked(rollback_prepared)
            finally:
                manifest_path.write_bytes(original_manifest)
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1a_locked_revalidation_real_mutation_reds(self) -> None:
        """Every R1a local drift fails closed without a second remote operation."""
        source, snapshot, _journal = self.rollback_promote_source("5")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            evidence = rollback_prepared.rollback_evidence
            self.assertIsNotNone(evidence)
            marker = "R1A_LOCKED_MARKER/C:" + "/private"

            def refused(candidate: Prepared) -> None:
                with patch.object(promote_module, "require_fresh", side_effect=AssertionError(marker)) as fresh, \
                        patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                    with self.assertRaises(ConfigError) as raised:
                        promote_module.revalidate_canonical_prepared_locked(candidate)
                self.assertEqual((raised.exception.code, raised.exception.detail),
                                 ("FAIL_TRANSACTION_SCOPE", "canonical rollback revalidation"))
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertEqual((fresh.call_count, observe.call_count), (0, 0))

            original_config = self.config.read_bytes()
            with self.subTest(drift="binding-bytes"):
                try:
                    self.config.write_bytes(original_config + marker.encode("utf-8"))
                    refused(rollback_prepared)
                finally:
                    self.config.write_bytes(original_config)

            with self.subTest(drift="repo-head"):
                try:
                    git(self.repo, "reset", "--hard", evidence.expected_base_sha)
                    refused(rollback_prepared)
                finally:
                    git(self.repo, "reset", "--hard", evidence.prepared_target_sha)

            with self.subTest(drift="txn-head"):
                temporary = _new_worktree(self.repo, rollback_prepared.control_root, evidence.prepared_target_sha)
                try:
                    refused(replace(rollback_prepared, txn=temporary))
                finally:
                    _cleanup_worktree(self.repo, rollback_prepared.control_root, temporary)

            with self.subTest(drift="wrong-parent"):
                forged_sha = git(self.repo, "commit-tree", f"{rollback_prepared.sha}^{{tree}}", "-p",
                                 evidence.expected_base_sha, "-m", "wrong parent")
                temporary = _new_worktree(self.repo, rollback_prepared.control_root, forged_sha)
                try:
                    refused(replace(rollback_prepared, txn=temporary, sha=forged_sha,
                                    tree_oid=git(temporary, "rev-parse", "HEAD^{tree}")))
                finally:
                    _cleanup_worktree(self.repo, rollback_prepared.control_root, temporary)

            with self.subTest(drift="extra-diff"):
                temporary = _new_worktree(self.repo, rollback_prepared.control_root, rollback_prepared.sha)
                try:
                    path = temporary / "state" / "r1a-extra-diff.md"
                    path.write_text("extra diff\n", encoding="utf-8")
                    git(temporary, "add", "state/r1a-extra-diff.md")
                    git(temporary, "commit", "-q", "--amend", "--no-edit")
                    forged_sha = git(temporary, "rev-parse", "HEAD")
                    refused(replace(rollback_prepared, txn=temporary, sha=forged_sha,
                                    tree_oid=git(temporary, "rev-parse", "HEAD^{tree}")))
                finally:
                    _cleanup_worktree(self.repo, rollback_prepared.control_root, temporary)

            with self.subTest(drift="expected-observation"):
                refused(replace(rollback_prepared, expected_remote_sha="0" * 40))

            with self.subTest(drift="inverse-fact-content"):
                inverse = [json.loads(json.dumps(item)) for item in evidence.inverse_facts]
                changed = next(item for item in inverse if item["before"]["exists"])
                changed["before"]["sha256"] = "f" * 64
                refused(replace(rollback_prepared, rollback_evidence=replace(evidence, inverse_facts=tuple(inverse))))
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1a_locked_revalidation_rejects_txn_path_alias(self) -> None:
        source, snapshot, _journal = self.rollback_promote_source("6")
        rollback_prepared = None
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            alias = rollback_prepared.txn.parent / "rollback-txn-alias"
            try:
                os.symlink(rollback_prepared.txn, alias, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("platform cannot create directory alias")
            try:
                with patch.object(promote_module, "require_fresh", side_effect=AssertionError("fetch")) as fresh, \
                        patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("observe")) as observe:
                    with self.assertRaises(ConfigError) as raised:
                        promote_module.revalidate_canonical_prepared_locked(replace(rollback_prepared, txn=alias))
                self.assertEqual((raised.exception.code, raised.exception.detail),
                                 ("FAIL_TRANSACTION_SCOPE", "canonical rollback revalidation"))
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual((fresh.call_count, observe.call_count), (0, 0))
            finally:
                alias.unlink()
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_rollback_r1b0_accepts_rollback_worktree_residue_settlement(self) -> None:
        """Only a settled rollback may carry inert worktree residue into its next rollback plan."""
        source, snapshot, journal = self.rollback_source("7")
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            journal = append_journal_event(
                journal, "cleanup_pending", {"phase": "worktree", "kind": "worktree"}, prepared=source,
            )
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((plan.payload["original_operation"], plan.payload["settlement_kind"],
                              plan.payload["settlement_record_sha256"]),
                             ("rollback", "original-completed", journal.record_sha256))
            self.assertEqual(plan.candidate_id, snapshot.snapshot_id)
        finally:
            self.cleanup(source)

    def test_canonical_recovery_r1b0_refuses_rollback_worktree_residue_before_observation(self) -> None:
        source, snapshot, journal = self.rollback_source("8")
        marker = "ROLLBACK_WORKTREE_RESIDUE_MARKER/C:" + "/private"
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            append_journal_event(
                journal, "cleanup_pending", {"phase": "worktree", "kind": "worktree"}, prepared=source,
            )
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe, \
                    patch.object(promote_module, "_cleanup_worktree", side_effect=AssertionError(marker)) as remover:
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, source.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_RECOVERY_CLEANUP_NOT_ACTIONABLE", "canonical recovery worktree residue"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual((observe.call_count, remover.call_count), (0, 0))
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1b0_quarantine_cleanup_still_blocks_settlement(self) -> None:
        source, snapshot, journal = self.rollback_source("9")
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            append_journal_event(
                journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=source,
            )
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError("observe")) as observe:
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_CLEANUP_PENDING", "canonical rollback cleanup required"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(observe.call_count, 0)
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1b0_scope_unverified_blocks_worktree_settlement_before_observation(self) -> None:
        source, snapshot, journal = self.rollback_recovery_source("a")
        marker = "ROLLBACK_SCOPE_WORKTREE_MARKER/C:" + "/private"
        try:
            snapshot, journal = self.rewrite_rollback_source(snapshot, journal)
            git(source.txn, "push", "-q", "origin", f"{source.sha}:refs/heads/main")
            journal = append_journal_event(
                journal, "failed", {"phase": "apply", "code": "REMOTE_COMMITTED_SCOPE_UNVERIFIED"}, prepared=source,
            )
            journal = append_journal_event(journal, "completed", {"phase": "completed"}, prepared=source)
            append_journal_event(
                journal, "cleanup_pending", {"phase": "worktree", "kind": "worktree"}, prepared=source,
            )
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)) as observe:
                with self.assertRaises(ConfigError) as raised:
                    rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_ROLLBACK_SCOPE_UNVERIFIED", "canonical rollback scope unverified"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            self.assertEqual(observe.call_count, 0)
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1a_rejects_snapshot_material_before_owned_worktree(self) -> None:
        source, snapshot, _journal = self.rollback_source("3")
        try:
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            manifest = promote_module._validate_snapshot_material(snapshot)
            material = next(fact[side]["data"] for fact in manifest["files"] for side in ("before", "after")
                            if fact[side]["exists"])
            data = snapshot.path / material
            linked = snapshot.path / "files" / "r1a-hardlink.bin"
            os.link(data, linked)
            try:
                with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("worktree")) as worktree:
                    with self.assertRaises(ConfigError):
                        promote_module.prepare_rollback(
                            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                        )
                self.assertEqual(worktree.call_count, 0)
            finally:
                linked.unlink()
            original = data.read_bytes()
            try:
                data.write_bytes(b"R1A_SNAPSHOT_TAMPER")
                with patch.object(promote_module, "_new_worktree", side_effect=AssertionError("worktree")) as worktree:
                    with self.assertRaises(ConfigError):
                        promote_module.prepare_rollback(
                            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                        )
                self.assertEqual(worktree.call_count, 0)
            finally:
                data.write_bytes(original)
        finally:
            self.cleanup(source)

    def test_canonical_rollback_r1a_accepts_rollback_of_rollback_prepared(self) -> None:
        source, snapshot, journal = self.rollback_source("4")
        rollback_prepared = None
        try:
            snapshot, _journal = self.rewrite_rollback_source(snapshot, journal)
            plan = rollback(self.state, None, snapshot.snapshot_id, apply=False, config_path=self.config)
            rollback_prepared = promote_module.prepare_rollback(
                self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            self.assertEqual((rollback_prepared.operation, rollback_prepared.rollback_evidence.original_operation),
                             ("rollback", "rollback"))
            self.assertEqual(promote_module.revalidate_canonical_prepared_locked(rollback_prepared)[0].mode, "100644")
        finally:
            if rollback_prepared is not None:
                self.cleanup(rollback_prepared)
            self.cleanup(source)

    def test_canonical_recovery_r0_plans_original_uncommitted_publish_without_mutation(self) -> None:
        item = self.item_id("b")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
        )
        try:
            snapshot = create_canonical_snapshot(prepared)
            journal = create_canonical_journal(prepared, snapshot)
            journal = append_journal_event(journal, "preflight_ok", {"phase": "preflight"}, prepared=prepared)
            journal = append_journal_event(journal, "push_attempt", {"phase": "push"}, prepared=prepared)
            before_head = git(self.repo, "rev-parse", "HEAD")
            before_source = (self.state / "inbox" / f"{item}.md").read_bytes()
            before_control = {
                path.relative_to(self.control): path.read_bytes()
                for path in self.control.rglob("*") if path.is_file()
            }
            fetch_head = self.repo / ".git" / "FETCH_HEAD"
            fetch_head.write_text("RECOVERY_FETCH_HEAD_MARKER\n", encoding="utf-8")
            git(self.repo, "update-ref", "refs/remotes/origin/main", prepared.sha)
            real_observe = promote_module.observe_canonical_remote_head
            real_binding_git = state_module._binding_git

            def local_binding_git(repo, *args):
                if args and args[0] == "ls-remote":
                    raise AssertionError("binding-network")
                return real_binding_git(repo, *args)

            with patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")), \
                    patch.object(promote_module, "_push", side_effect=AssertionError("legacy-push")), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("fast-forward")), \
                    patch.object(promote_module, "record_remote_head", side_effect=AssertionError("pointer")), \
                    patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(state_module, "_binding_git", side_effect=local_binding_git):
                recovery = plan_canonical_recovery(
                    self.state, prepared.sha, config_path=self.config,
                )
            self.assertEqual(recovery.operation, "recover")
            self.assertEqual(recovery.expected_remote_sha, prepared.expected_remote_sha)
            self.assertEqual(recovery.payload["action"], "artifact-cleanup")
            self.assertIn(f"RECOVERY_SOURCE {journal.operation_id}", recovery.lines)
            self.assertIn(f"RECOVERY_OBSERVED {prepared.expected_remote_sha}", recovery.lines)
            self.assertIn("RECOVERY_ACTION artifact-cleanup", recovery.lines)
            for field, value in (("action", "local-finalization"),
                                 ("original_operation_id", "20260820T000000000000Z-" + "e" * 16),
                                 ("observed_sha", "f" * 40),
                                 ("snapshot_id", "20260820T000000000000Z-" + "f" * 16)):
                mutated = dict(recovery.payload)
                mutated[field] = value
                self.assertNotEqual(promote_module._canonical_hash(mutated), recovery.plan_hash)
            self.assertEqual(observe.call_count, 1)
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual((self.state / "inbox" / f"{item}.md").read_bytes(), before_source)
            self.assertEqual(fetch_head.read_text(encoding="utf-8"), "RECOVERY_FETCH_HEAD_MARKER\n")
            self.assertEqual({
                path.relative_to(self.control): path.read_bytes()
                for path in self.control.rglob("*") if path.is_file()
            }, before_control)
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_r0_rejects_missing_original_before_observation(self) -> None:
        marker = "RECOVERY_UNIQUE_MARKER"
        with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
            with self.assertRaises(ConfigError) as raised:
                plan_canonical_recovery(self.state, "a" * 40, config_path=self.config)
        error = raised.exception
        self.assertEqual((error.code, error.detail), ("FAIL_RECOVERY_NOT_FOUND", "canonical recovery source"))
        self.assertIsNone(error.__cause__)
        self.assertNotIn(marker, str(error))
        self.assertNotIn(marker, repr(error))

    def test_canonical_recovery_r0_broken_journal_alias_is_scope_before_observation(self) -> None:
        context = resolve_repository_context(self.state)
        control = promote_module._canonical_control_root(context, None)
        journal = control / "journal"
        journal.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(Path(self.temp.name) / "missing-journal", journal, target_is_directory=True)
        except OSError:
            self.skipTest("symlink unavailable for broken recovery journal alias test")
        marker = "RECOVERY_BROKEN_ALIAS_MARKER"
        with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
            with self.assertRaises(ConfigError) as raised:
                plan_canonical_recovery(self.state, "a" * 40, config_path=self.config)
        self.assertEqual((raised.exception.code, raised.exception.detail),
                         ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(marker, str(raised.exception))
        self.assertNotIn(marker, repr(raised.exception))

    def test_canonical_recovery_r0_public_boundary_and_terminal_are_sanitized(self) -> None:
        resolution_marker = "RECOVERY_RESOLUTION_MARKER"
        with patch.object(promote_module, "resolve_repository_context", side_effect=OSError(resolution_marker)):
            with self.assertRaises(ConfigError) as raised:
                plan_canonical_recovery(self.state, "a" * 40, config_path=self.config)
        self.assertEqual((raised.exception.code, raised.exception.detail),
                         ("FAIL_STATE_REPOSITORY", "canonical recovery repository"))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(resolution_marker, str(raised.exception))
        self.assertNotIn(resolution_marker, repr(raised.exception))

        prepared, _snapshot, journal = self.recovery_source("d")
        marker = "RECOVERY_BOUNDARY_MARKER"
        try:
            with patch.object(promote_module, "_canonical_recovery_binding",
                              side_effect=ConfigError("FAIL_STATE_BINDING", marker)), \
                    patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_STATE_BINDING", "canonical recovery binding"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))

            append_journal_event(journal, "completed", {"phase": "completed"}, prepared=prepared)
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_RECOVERY_COMPLETE", "canonical recovery source"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_r0_action_boundary_sanitizes_ancestry_failures(self) -> None:
        prepared, _snapshot, _journal = self.recovery_source("e")
        marker = "RECOVERY_ANCESTRY_MARKER"
        try:
            reviewed = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            recovery_root = prepared.control_root / "recovery-journal"
            before = set(recovery_root.glob("*.jsonl")) if recovery_root.exists() else set()
            for failure in (OSError(marker), subprocess.TimeoutExpired("git", 1), UnicodeError(marker),
                            ConfigError("FAIL_GIT", marker)):
                calls = (
                    lambda: plan_canonical_recovery(self.state, prepared.sha, config_path=self.config),
                    lambda: promote_module.dispatch_recovery(
                        self.state, prepared.sha, None, apply=False, plan_hash=None,
                        expected_observed_sha=None, config_path=self.config, requested_mode="canonical",
                    ),
                    lambda: apply_canonical_recovery(
                        self.state, prepared.sha, reviewed.plan_hash, reviewed.expected_remote_sha,
                        config_path=self.config,
                    ),
                )
                for invoke in calls:
                    with patch.object(promote_module, "observe_canonical_remote_head", return_value="f" * 40) as observe, \
                            patch.object(promote_module, "_is_ancestor", side_effect=failure), \
                            patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError(marker)), \
                            patch.object(promote_module, "fast_forward_local", side_effect=AssertionError(marker)), \
                            patch.object(promote_module, "record_remote_head", side_effect=AssertionError(marker)):
                        with self.assertRaises(ConfigError) as raised:
                            invoke()
                    self.assertEqual((raised.exception.code, raised.exception.detail),
                                     ("FAIL_RECOVERY_INDETERMINATE", "canonical recovery state"))
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertNotIn(marker, str(raised.exception))
                    self.assertNotIn(marker, repr(raised.exception))
                    self.assertEqual(observe.call_count, 1)
                    after = set(recovery_root.glob("*.jsonl")) if recovery_root.exists() else set()
                    self.assertEqual(after, before)
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_artifact_failures_remain_scope_after_planning(self) -> None:
        prepared, _snapshot, _journal = self.recovery_source("f", push_attempt=False)
        marker = "RECOVERY_R1_ARTIFACT_MARKER"
        try:
            reviewed = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            recovery_root = prepared.control_root / "recovery-journal"
            for failure in (OSError(marker), ConfigError("FAIL_GIT", marker)):
                real_observe = promote_module.observe_canonical_remote_head
                with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                        patch.object(promote_module, "create_recovery_journal", side_effect=failure):
                    with self.assertRaises(ConfigError) as raised:
                        apply_canonical_recovery(
                            self.state, prepared.sha, reviewed.plan_hash, reviewed.expected_remote_sha,
                            config_path=self.config,
                        )
                self.assertEqual((raised.exception.code, raised.exception.detail),
                                 ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertEqual(observe.call_count, 1)
                self.assertFalse(os.path.lexists(recovery_root))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_r0_rejects_existing_or_corrupt_recovery_journal(self) -> None:
        item = self.item_id("c")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
        )
        try:
            snapshot = create_canonical_snapshot(prepared)
            original = create_canonical_journal(prepared, snapshot)
            checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
            path = checkpoint.path
            root = path.parent
            for raw, code in ((path.read_bytes(), "FAIL_RECOVERY_INTERRUPTED"),):
                path.write_bytes(raw)
                with self.assertRaisesRegex(ConfigError, code) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(raised.exception.detail,
                                 "canonical recovery interrupted action=artifact-cleanup step=baseline")
                marker = str(prepared.control_root / "RECOVERY_ACTIONABLE_MARKER")
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
            hardlink = root / ("20260820T000000000000Z-" + "e" * 16 + ".jsonl")
            try:
                os.link(path, hardlink)
            except OSError:
                self.skipTest("hardlinks unavailable for recovery journal identity test")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as raised:
                plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertIsNone(raised.exception.__cause__)
            hardlink.unlink()
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleanup-intent")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleaned")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "converged")
            self.append_recovery_checkpoint(prepared, checkpoint, "completed")
            with self.assertRaisesRegex(ConfigError, "FAIL_RECOVERY_ALREADY_COMPLETED") as raised:
                plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertIsNone(raised.exception.__cause__)
            marker = b"RECOVERY_CORRUPT_MARKER"
            path.write_bytes(marker)
            with self.assertRaises(ConfigError) as raised:
                plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker.decode(), str(raised.exception))
            self.assertNotIn(marker.decode(), repr(raised.exception))
        finally:
            self.cleanup(prepared)

    def test_recovery_checkpoint_schema_accepts_closed_action_chains(self) -> None:
        cases = {
            "artifact-cleanup": ("worktree-cleanup-intent", "worktree-cleaned", "converged", "completed"),
            "cleanup-only": ("quarantine-delete-intent", "quarantine-deleted", "converged", "completed"),
            "input-disposition": (
                "source-restore-intent", "source-restored", "converged", "worktree-cleanup-intent",
                "completed", "cleanup-pending",
            ),
            "local-finalization": (
                "pointer-updated", "source-quarantine-intent", "source-quarantined", "fast-forward-intent",
                "fast-forward-done", "converged", "worktree-cleanup-intent", "completed", "cleanup-pending",
            ),
        }
        prepared_items = []
        try:
            for index, (action, events) in enumerate(cases.items()):
                prepared, snapshot, original = self.recovery_source(f"{index + 1:x}")
                prepared_items.append(prepared)
                checkpoint = self.recovery_checkpoint(prepared, snapshot, original, action)
                for event in events:
                    checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event)
                records = promote_module._recovery_journal_records(checkpoint.path, checkpoint.path.parent)
                self.assertEqual(records[0]["original_operation"], "publish")
                self.assertEqual(records[0]["original_journal_final_record_sha256"], original.record_sha256)
                self.assertEqual(records[-1]["event"], events[-1])
                self.assertTrue(checkpoint.path.read_bytes().endswith(b"\n"))
                self.assertEqual(os.lstat(checkpoint.path).st_nlink, 1)
            checkpoint = self.recovery_checkpoint(prepared, snapshot, original, "input-disposition")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "source-preserved")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "converged")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "completed")
            self.assertEqual(promote_module._recovery_journal_records(checkpoint.path, checkpoint.path.parent)[-1]["event"],
                             "completed")
        finally:
            for prepared in prepared_items:
                self.cleanup(prepared)

    def test_recovery_checkpoint_rejects_closed_transition_mutations(self) -> None:
        prepared, snapshot, original = self.recovery_source("f")
        try:
            def refused(checkpoint, event: str, details: dict[str, str]) -> None:
                with self.assertRaises(ConfigError) as raised:
                    promote_module.append_recovery_journal_event(checkpoint, event, details)
                self.assertEqual((raised.exception.code, raised.exception.detail),
                                 ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
                self.assertIsNone(raised.exception.__cause__)

            checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
            refused(checkpoint, "worktree-cleanup-intent", {"role": "worktree-cleanup"})
            refused(checkpoint, "quarantine-delete-intent",
                    self.recovery_checkpoint_details(prepared, "quarantine-delete-intent"))
            refused(checkpoint, "completed", {})

            input_checkpoint = self.recovery_checkpoint(prepared, snapshot, original, "input-disposition")
            refused(input_checkpoint, "converged", {})
            restoring = self.append_recovery_checkpoint(prepared, input_checkpoint, "source-restore-intent")
            refused(restoring, "converged", {})
            preserved_checkpoint = self.recovery_checkpoint(prepared, snapshot, original, "input-disposition")
            refused(preserved_checkpoint, "source-preserved", {"role": "source-preserved"})
            preserved = self.append_recovery_checkpoint(prepared, preserved_checkpoint, "source-preserved")
            refused(preserved, "source-restore-intent",
                    self.recovery_checkpoint_details(prepared, "source-restore-intent"))

            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "failed")
            refused(checkpoint, "worktree-cleanup-intent",
                    self.recovery_checkpoint_details(prepared, "worktree-cleanup-intent"))

            checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleanup-intent")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleaned")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "converged")
            refused(checkpoint, "failed", {})
            refused(checkpoint, "converged", {})
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "completed")
            refused(checkpoint, "completed", {})
            refused(checkpoint, "cleanup-pending", {"kind": "worktree-cleanup"})

            for action in ("input-disposition", "local-finalization"):
                checkpoint = self.recovery_checkpoint(prepared, snapshot, original, action)
                events = (("source-restore-intent", "source-restored", "converged", "completed")
                          if action == "input-disposition" else
                          ("pointer-updated", "fast-forward-intent", "fast-forward-done", "converged", "completed"))
                events = events[:-1] + ("worktree-cleanup-intent", "completed")
                for event in events:
                    checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, event)
                checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "cleanup-pending")
                refused(checkpoint, "cleanup-pending", {"kind": "worktree-cleanup"})
        finally:
            self.cleanup(prepared)

    def test_recovery_checkpoint_binds_observation_pairs_and_operation_input(self) -> None:
        prepared, snapshot, original = self.recovery_source("a")
        try:
            observed = "d" * 40
            checkpoint = self.recovery_checkpoint(
                prepared, snapshot, original, "local-finalization", observed_sha=observed,
            )
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.append_recovery_journal_event(
                    checkpoint, "pointer-updated", self.recovery_checkpoint_details(prepared, "pointer-updated"),
                )
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "pointer-updated", observed_sha=observed)
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "fast-forward-intent", observed_sha=observed)
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "fast-forward-done", observed_sha=observed)
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "converged")
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleanup-intent")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.append_recovery_journal_event(
                    checkpoint, "worktree-cleaned", {"role": "worktree-cleanup", "handle_identity_sha256": "b" * 64})

            for operation in ("promote", "advance"):
                valid = self.recovery_checkpoint(prepared, snapshot, original, "artifact-cleanup",
                                                 original_operation=operation)
                baseline = promote_module._recovery_journal_records(valid.path, valid.path.parent)[0]
                self.assertIsNone(baseline["snapshot_input_sha256"])
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    self.recovery_checkpoint(prepared, snapshot, original, "input-disposition",
                                             original_operation=operation)
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    self.recovery_checkpoint(prepared, snapshot, original, "cleanup-only",
                                             original_operation=operation)
        finally:
            self.cleanup(prepared)

    def test_recovery_checkpoint_failed_diagnostic_and_multi_source_fail_closed(self) -> None:
        prepared, snapshot, original = self.recovery_source("b")
        try:
            checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
            checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleanup-intent")
            self.append_recovery_checkpoint(prepared, checkpoint, "failed")
            marker = str(prepared.control_root / "RECOVERY_FAILED_DIAGNOSTIC_MARKER")
            with self.assertRaises(ConfigError) as raised:
                plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail), (
                "FAIL_RECOVERY_INTERRUPTED",
                "canonical recovery interrupted action=artifact-cleanup step=worktree-cleanup-intent",
            ))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))

            self.recovery_checkpoint(prepared, snapshot, original)
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
        finally:
            self.cleanup(prepared)

    def test_recovery_r0_rejects_nonpublish_original_cleanup_pending(self) -> None:
        prepared, snapshot, journal = self.recovery_source("c")
        try:
            journal = append_journal_event(journal, "completed", {"phase": "completed"}, prepared=prepared)
            journal = append_journal_event(journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
            records = _read_journal(journal.path, journal.path.parent)
            baseline = dict(records[0])
            baseline["operation"] = "promote"
            manifest = promote_module._validate_snapshot_material(snapshot)
            with self.assertRaises(ConfigError) as raised:
                promote_module._recovery_action(
                    resolve_repository_context(self.state), baseline, records, snapshot, manifest, prepared.sha,
                )
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
            self.assertIsNone(raised.exception.__cause__)
        finally:
            self.cleanup(prepared)

    def test_recovery_checkpoint_cross_checks_original_immutable_baseline(self) -> None:
        prepared, snapshot, original = self.recovery_source("d")
        try:
            checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
            records = [json.loads(line) for line in checkpoint.path.read_text(encoding="utf-8").splitlines()]
            records[0]["target_sha"] = "f" * 40
            records[0]["record_sha256"] = promote_module._record_hash(records[0], "record_sha256")
            checkpoint.path.write_bytes(b"\n".join(promote_module._canonical_bytes(record) for record in records) + b"\n")
            marker = "RECOVERY_IMMUTABLE_CROSSCHECK_MARKER"
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
        finally:
            self.cleanup(prepared)

    def test_recovery_finalizer_never_replaces_collision_and_keeps_single_success(self) -> None:
        prepared, snapshot, original = self.recovery_source("e")
        try:
            root = prepared.control_root / "recovery-journal"
            root.mkdir(parents=True, exist_ok=True)
            temporary = root / "owned.tmp"
            final = root / "collision.jsonl"
            temporary_token = promote_module._write_owned(temporary, b"owned")

            def racing_link(_source, destination):
                Path(destination).write_bytes(b"sentinel")
                raise FileExistsError

            with patch.object(promote_module.os, "link", side_effect=racing_link):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._finalize_owned_file(temporary, final, temporary_token)
            self.assertEqual(final.read_bytes(), b"sentinel")
            self.assertEqual(temporary.read_bytes(), b"owned")

            directory = root / "directory-collision"
            directory.mkdir()
            other = root / "other.tmp"
            other_token = promote_module._write_owned(other, b"owned")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module._finalize_owned_file(other, directory, other_token)
            self.assertTrue(directory.is_dir())

            published_temp = root / "published.tmp"
            published_final = root / "published.jsonl"
            published_token = promote_module._write_owned(published_temp, b"owned")

            def replace_after_link(path, _token, *, links):
                path.unlink()
                path.write_bytes(b"external")
                return False

            with patch.object(promote_module, "_unlink_owned_file", side_effect=replace_after_link):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._finalize_owned_file(published_temp, published_final, published_token)
            self.assertEqual(published_final.read_bytes(), b"owned")
            self.assertEqual(published_temp.read_bytes(), b"external")

            replaced_final_temp = root / "replaced-final.tmp"
            replaced_final = root / "replaced-final.jsonl"
            replaced_final_token = promote_module._write_owned(replaced_final_temp, b"owned")
            real_unlink = promote_module._unlink_owned_file

            def unlink_then_replace_final(path, token, *, links):
                self.assertTrue(real_unlink(path, token, links=links))
                replaced_final.unlink()
                replaced_final.write_bytes(b"external-final")
                return True

            with patch.object(promote_module, "_unlink_owned_file", side_effect=unlink_then_replace_final):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    promote_module._finalize_owned_file(
                        replaced_final_temp, replaced_final, replaced_final_token)
            self.assertEqual(replaced_final.read_bytes(), b"external-final")
            self.assertFalse(os.path.lexists(replaced_final_temp))

            fixed = "20260820T000000000000Z-" + "f" * 16
            with patch.object(promote_module, "_artifact_id", return_value=fixed):
                checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
                published = checkpoint.path.read_bytes()
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    self.recovery_checkpoint(prepared, snapshot, original)
            self.assertEqual(checkpoint.path.read_bytes(), published)
        finally:
            self.cleanup(prepared)

    def test_recovery_create_preserves_preexisting_temporary_sentinel(self) -> None:
        prepared, snapshot, original = self.recovery_source("f")
        try:
            operation_id = "20260820T000000000000Z-" + "a" * 16
            root = prepared.control_root / "recovery-journal"
            root.mkdir(parents=True, exist_ok=True)
            temporary = root / f".{operation_id}.tmp"
            temporary.write_bytes(b"external-temp-sentinel")
            identity = (os.lstat(temporary).st_dev, os.lstat(temporary).st_ino)
            with patch.object(promote_module, "_artifact_id", return_value=operation_id):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    self.recovery_checkpoint(prepared, snapshot, original)
            self.assertEqual(temporary.read_bytes(), b"external-temp-sentinel")
            self.assertEqual((os.lstat(temporary).st_dev, os.lstat(temporary).st_ino), identity)
            self.assertFalse(os.path.lexists(root / f"{operation_id}.jsonl"))

            replacement_id = "20260820T000000000000Z-" + "b" * 16

            def replace_written_temporary(path, _final, _token):
                path.unlink()
                path.write_bytes(b"external-replacement")
                raise ConfigError("FAIL_TRANSACTION_SCOPE", "synthetic")

            with patch.object(promote_module, "_artifact_id", return_value=replacement_id), \
                    patch.object(promote_module, "_finalize_owned_file", side_effect=replace_written_temporary):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    self.recovery_checkpoint(prepared, snapshot, original)
            replacement = root / f".{replacement_id}.tmp"
            self.assertEqual(replacement.read_bytes(), b"external-replacement")
            self.assertFalse(os.path.lexists(root / f"{replacement_id}.jsonl"))
        finally:
            self.cleanup(prepared)

    def test_recovery_finalizer_rejects_broken_alias_when_supported(self) -> None:
        root = self.control / "recovery-finalizer-alias"
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / "owned.tmp"
        broken = root / "broken.jsonl"
        temporary_token = promote_module._write_owned(temporary, b"owned")
        try:
            os.symlink(root / "absent", broken)
        except OSError:
            self.skipTest("symlink unavailable for broken finalizer alias test")
        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
            promote_module._finalize_owned_file(temporary, broken, temporary_token)
        self.assertTrue(os.path.lexists(broken))
        self.assertEqual(temporary.read_bytes(), b"owned")

    def test_canonical_recovery_apply_artifact_cleanup_uses_one_reviewed_plan(self) -> None:
        prepared, snapshot, original = self.recovery_source("0", push_attempt=False)
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(plan.payload["action"], "artifact-cleanup")
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")):
                result = apply_canonical_recovery(
                    self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                )
            self.assertEqual((result.action, result.converged, result.cleanup_pending),
                             ("artifact-cleanup", True, False))
            self.assertEqual(observe.call_count, 1)
            self.assertFalse(prepared.txn.exists())
            recovery = promote_module._recovery_journal_records(
                prepared.control_root / "recovery-journal" / f"{result.operation_id}.jsonl",
                prepared.control_root / "recovery-journal",
            )
            self.assertEqual(tuple(record.get("event") for record in recovery[1:]),
                             ("worktree-cleanup-intent", "worktree-cleaned", "converged", "completed"))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_input_disposition_restores_or_preserves_source(self) -> None:
        prepared_items = []
        try:
            for suffix, restore in (("1", True), ("2", False)):
                prepared, snapshot, original = self.recovery_source(suffix, push_attempt=False)
                prepared_items.append(prepared)
                original, _quarantine = promote_module._quarantine_publish_source(prepared, original)
                source = self.state / "inbox" / f"{prepared.candidate_id}.md"
                if not restore:
                    source.write_bytes(prepared.source_content or b"")
                plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
                self.assertEqual(plan.payload["action"], "input-disposition")
                result = apply_canonical_recovery(
                    self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                )
                self.assertEqual((result.action, result.converged), ("input-disposition", True))
                self.assertEqual(source.read_bytes(), prepared.source_content)
                records = promote_module._recovery_journal_records(
                    prepared.control_root / "recovery-journal" / f"{result.operation_id}.jsonl",
                    prepared.control_root / "recovery-journal",
                )
                events = tuple(record.get("event") for record in records[1:])
                self.assertIn("source-restored" if restore else "source-preserved", events)
                self.assertEqual(events[-1], "completed")
        finally:
            for prepared in prepared_items:
                self.cleanup(prepared)

    def test_canonical_recovery_apply_local_finalization_fast_forwards_reviewed_target(self) -> None:
        prepared, snapshot, original = self.recovery_source("3")
        try:
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(plan.payload["action"], "local-finalization")
            result = apply_canonical_recovery(
                self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            self.assertEqual((result.action, git(self.repo, "rev-parse", "HEAD")),
                             ("local-finalization", prepared.sha))
            records = promote_module._recovery_journal_records(
                prepared.control_root / "recovery-journal" / f"{result.operation_id}.jsonl",
                prepared.control_root / "recovery-journal",
            )
            self.assertEqual(tuple(record.get("event") for record in records[1:]),
                             ("pointer-updated", "source-quarantine-intent", "source-quarantined",
                              "fast-forward-intent", "fast-forward-done", "converged",
                              "quarantine-delete-intent", "quarantine-deleted",
                              "worktree-cleanup-intent", "worktree-cleaned", "completed"))
            self.assertFalse(prepared.txn.exists())
            self.assertEqual(promote_module._quarantine_pending_ids(self.repo), ())
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_cleanup_only_reproves_target_before_delete(self) -> None:
        prepared, snapshot, original = self.recovery_source("4")
        try:
            original, quarantine = promote_module._quarantine_publish_source(prepared, original)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            original = append_journal_event(original, "completed", {"phase": "completed"}, prepared=prepared)
            original = append_journal_event(original, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(plan.payload["action"], "cleanup-only")
            result = apply_canonical_recovery(
                self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
            )
            self.assertEqual(result.action, "cleanup-only")
            self.assertFalse(os.path.lexists(quarantine.target))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_refuses_review_or_source_drift_before_baseline(self) -> None:
        prepared, snapshot, original = self.recovery_source("5", push_attempt=False)
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            root = prepared.control_root / "recovery-journal"
            with self.assertRaises(ConfigError) as raised:
                apply_canonical_recovery(
                    self.state, prepared.sha, "a" * 64, plan.expected_remote_sha, config_path=self.config,
                )
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_INPUT_CHANGED", "canonical recovery plan"))
            self.assertFalse(os.path.lexists(root))

            original, _quarantine = promote_module._quarantine_publish_source(prepared, original)
            source = self.state / "inbox" / f"{prepared.candidate_id}.md"
            source.write_bytes(b"conflicting-source")
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            with self.assertRaises(ConfigError) as raised:
                apply_canonical_recovery(
                    self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                )
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts"))
            self.assertFalse(os.path.lexists(root))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_refuses_observation_drift_before_baseline(self) -> None:
        prepared, snapshot, original = self.recovery_source("6", push_attempt=False)
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            root = prepared.control_root / "recovery-journal"
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe, \
                    patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")):
                with self.assertRaises(ConfigError) as raised:
                    apply_canonical_recovery(
                        self.state, prepared.sha, plan.plan_hash, "f" * 40, config_path=self.config,
                    )
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_INPUT_CHANGED", "canonical recovery plan"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(observe.call_count, 1)
            self.assertFalse(os.path.lexists(root))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_and_router_map_well_formed_review_mismatches(self) -> None:
        prepared, snapshot, original = self.recovery_source("d", push_attempt=False)
        marker = str(Path(self.temp.name) / "RECOVERY_REVIEW_MISMATCH_MARKER")
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            calls = (
                ("direct-hash", "a" * 64, plan.expected_remote_sha),
                ("direct-observed", plan.plan_hash, "b" * 40),
                ("router-hash", "c" * 64, plan.expected_remote_sha),
                ("router-observed", plan.plan_hash, "d" * 40),
            )
            real_resolve = promote_module.resolve_repository_context
            real_observe = promote_module.observe_canonical_remote_head
            for mode, plan_hash, observed in calls:
                with patch.object(promote_module, "resolve_repository_context", wraps=real_resolve) as resolver, \
                        patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
                    with self.assertRaises(ConfigError) as raised:
                        if mode.startswith("direct"):
                            apply_canonical_recovery(
                                self.state, prepared.sha, plan_hash, observed, config_path=self.config,
                            )
                        else:
                            promote_module.dispatch_recovery(
                                self.state, prepared.sha, None, apply=True, plan_hash=plan_hash,
                                expected_observed_sha=observed, config_path=self.config, requested_mode="canonical",
                            )
                self.assertEqual((raised.exception.code, raised.exception.detail),
                                 ("FAIL_INPUT_CHANGED", "canonical recovery plan"))
                self.assertIsNone(raised.exception.__cause__)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))
                self.assertEqual((resolver.call_count, observe.call_count), (1, 1))
                self.assertFalse(os.path.lexists(prepared.control_root / "recovery-journal"))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_existing_journal_blocks_without_second_baseline(self) -> None:
        prepared, snapshot, original = self.recovery_source("7", push_attempt=False)
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            for terminal, code in ((False, "FAIL_RECOVERY_INTERRUPTED"), (True, "FAIL_RECOVERY_ALREADY_COMPLETED")):
                checkpoint = self.recovery_checkpoint(prepared, snapshot, original)
                if terminal:
                    checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleanup-intent")
                    checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "worktree-cleaned")
                    checkpoint = self.append_recovery_checkpoint(prepared, checkpoint, "converged")
                    self.append_recovery_checkpoint(prepared, checkpoint, "completed")
                with self.assertRaises(ConfigError) as raised:
                    apply_canonical_recovery(
                        self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertIsNone(raised.exception.__cause__)
                checkpoint.path.unlink()
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_cleanup_reproof_and_artifact_preflight_refuse(self) -> None:
        prepared, snapshot, original = self.recovery_source("8", push_attempt=False)
        extra = None
        try:
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            for variant in ("dirty", "multiple"):
                if variant == "dirty":
                    (prepared.txn / "untracked.txt").write_text("dirty\n", encoding="utf-8")
                else:
                    extra = promote_module._new_worktree(prepared.repo, prepared.control_root, prepared.sha)
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    apply_canonical_recovery(
                        self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                    )
                self.assertFalse(os.path.lexists(prepared.control_root / "recovery-journal"))
                if extra is not None:
                    self.cleanup(SimpleNamespace(repo=prepared.repo, control_root=prepared.control_root, txn=extra))
                    extra = None
                if (prepared.txn / "untracked.txt").exists():
                    (prepared.txn / "untracked.txt").unlink()
        finally:
            if extra is not None:
                self.cleanup(SimpleNamespace(repo=prepared.repo, control_root=prepared.control_root, txn=extra))
            self.cleanup(prepared)

        cleanup_prepared, cleanup_snapshot, cleanup_original = self.recovery_source("c", push_attempt=True)
        try:
            cleanup_original, quarantine = promote_module._quarantine_publish_source(cleanup_prepared, cleanup_original)
            git(cleanup_prepared.txn, "push", "-q", "origin", f"{cleanup_prepared.sha}:refs/heads/main")
            cleanup_original = append_journal_event(
                cleanup_original, "completed", {"phase": "completed"}, prepared=cleanup_prepared,
            )
            cleanup_original = append_journal_event(
                cleanup_original, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=cleanup_prepared,
            )
            plan = plan_canonical_recovery(self.state, cleanup_prepared.sha, config_path=self.config)
            self.assertEqual(plan.payload["action"], "cleanup-only")
            with patch.object(promote_module, "_recovery_quarantine_valid", side_effect=(True, False)):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                    apply_canonical_recovery(
                        self.state, cleanup_prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                    )
            self.assertTrue(os.path.lexists(quarantine.target))
        finally:
            self.cleanup(cleanup_prepared)

    def test_canonical_recovery_apply_refuses_non_fast_forward_local_state(self) -> None:
        prepared, snapshot, original = self.recovery_source("9")
        try:
            original, _quarantine = promote_module._quarantine_publish_source(prepared, original)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            (self.repo / "local-only.txt").write_text("local\n", encoding="utf-8")
            git(self.repo, "add", "local-only.txt")
            git(self.repo, "commit", "-q", "-m", "local divergence")
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                apply_canonical_recovery(
                    self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                )
            self.assertFalse(os.path.lexists(prepared.control_root / "recovery-journal"))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_local_finalization_refuses_unexpected_dirty(self) -> None:
        prepared, snapshot, original = self.recovery_source("a")
        try:
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            (self.repo / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
            plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                apply_canonical_recovery(
                    self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                )
            self.assertFalse(os.path.lexists(prepared.control_root / "recovery-journal"))
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_apply_crash_prefixes_and_public_sanitization(self) -> None:
        for suffix, seam, expected in (
                ("a", "operation", "failed"), ("b", "done", "failed"), ("c", "completed", "converged")):
            prepared, snapshot, original = self.recovery_source(suffix, push_attempt=False)
            try:
                plan = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
                root = prepared.control_root / "recovery-journal"
                before = set(root.glob("*.jsonl")) if root.exists() else set()
                real_git = promote_module._git
                real_append = promote_module.append_recovery_journal_event
                completed_calls = []

                def crash_git(repo, *args, check=True):
                    if seam == "operation" and args[:2] == ("worktree", "remove"):
                        raise OSError("RECOVERY_OPERATION_MARKER")
                    return real_git(repo, *args, check=check)

                def crash_append(journal, event, details=None):
                    if (seam == "done" and event == "worktree-cleaned") or (seam == "completed" and event == "completed"):
                        if seam == "completed":
                            completed_calls.append((journal.operation_id, event))
                        raise OSError("RECOVERY_APPEND_MARKER")
                    return real_append(journal, event, details)

                with patch.object(promote_module, "_git", side_effect=crash_git), \
                        patch.object(promote_module, "append_recovery_journal_event", side_effect=crash_append):
                    if seam == "completed":
                        result = apply_canonical_recovery(
                            self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                        )
                        self.assertEqual((result.converged, result.cleanup_pending), (True, False))
                    else:
                        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as raised:
                            apply_canonical_recovery(
                                self.state, prepared.sha, plan.plan_hash, plan.expected_remote_sha, config_path=self.config,
                            )
                        self.assertIsNone(raised.exception.__cause__)
                        self.assertNotIn("RECOVERY_", str(raised.exception))
                created = set(root.glob("*.jsonl")) - before
                self.assertEqual(len(created), 1)
                journal_path = next(iter(created))
                if seam == "completed":
                    self.assertEqual(journal_path, root / f"{result.operation_id}.jsonl")
                    self.assertEqual(completed_calls, [(result.operation_id, "completed")])
                records = promote_module._recovery_journal_records(journal_path, root)
                self.assertEqual(records[0]["original_operation_id"], original.operation_id)
                self.assertEqual(records[0]["target_sha"], prepared.sha)
                self.assertEqual(records[-1].get("event"), expected)
                self.assertNotIn("failed", tuple(record.get("event") for record in records[1:])[-1:] if seam == "completed" else ())
                if seam == "completed":
                    self.assertFalse(prepared.txn.exists())
                    self.assertEqual(promote_module._recovery_pending_kind(None, (prepared.txn,)), None)
                    self.assertNotIn(str(prepared.txn), git(prepared.repo, "worktree", "list", "--porcelain"))
                    self.assertEqual(promote_module._quarantine_pending_ids(prepared.repo), ())
            finally:
                self.cleanup(prepared)

    def test_canonical_recovery_r0_classifies_pinned_remote_matrix(self) -> None:
        base = git(self.repo, "rev-parse", "origin/main")
        prepared_items = []

        def reset_remote() -> None:
            git(self.seed, "push", "-q", "--force", "origin", f"{base}:refs/heads/main")
            git(self.repo, "fetch", "-q", "origin")

        def inspect(prepared, expected_action: str):
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")), \
                    patch.object(promote_module, "_push", side_effect=AssertionError("legacy-push")), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("fast-forward")), \
                    patch.object(promote_module, "record_remote_head", side_effect=AssertionError("pointer")), \
                    patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
                recovery = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(recovery.payload["action"], expected_action)
            self.assertEqual(observe.call_count, 1)

        try:
            # B: prepared was never pushed; even without push_attempt only cleanup is safe.
            prepared, _snapshot, _journal = self.recovery_source("1", push_attempt=False)
            prepared_items.append(prepared)
            inspect(prepared, "artifact-cleanup")

            reset_remote()
            prepared, _snapshot, _journal = self.recovery_source("2")
            prepared_items.append(prepared)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            inspect(prepared, "local-finalization")

            reset_remote()
            prepared, _snapshot, _journal = self.recovery_source("3")
            prepared_items.append(prepared)
            descendant_path = prepared.txn / "state" / "recovery-descendant.md"
            descendant_path.write_text("synthetic\n", encoding="utf-8")
            git(prepared.txn, "add", "state/recovery-descendant.md")
            git(prepared.txn, "commit", "-q", "-m", "synthetic state descendant")
            descendant = git(prepared.txn, "rev-parse", "HEAD")
            git(prepared.txn, "push", "-q", "origin", f"{descendant}:refs/heads/main")
            inspect(prepared, "local-finalization")

            reset_remote()
            prepared, _snapshot, _journal = self.recovery_source("4")
            prepared_items.append(prepared)
            git(self.seed, "checkout", "-q", "-B", "recovery-race", base)
            (self.seed / "state" / "recovery-race.md").write_text("synthetic\n", encoding="utf-8")
            git(self.seed, "add", "state/recovery-race.md")
            git(self.seed, "commit", "-q", "-m", "synthetic recovery race")
            race = git(self.seed, "rev-parse", "HEAD")
            git(self.seed, "push", "-q", "origin", f"{race}:refs/heads/main")
            inspect(prepared, "artifact-cleanup")

            reset_remote()
            prepared, _snapshot, _journal = self.recovery_source("7")
            prepared_items.append(prepared)
            unrelated = git(self.repo, "commit-tree", f"{base}^{{tree}}", "-m", "synthetic unrelated root")
            git(self.repo, "push", "-q", "--force", "origin", f"{unrelated}:refs/heads/main")
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("fast-forward")), \
                    patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual((raised.exception.code, raised.exception.detail),
                             ("FAIL_RECOVERY_UNSAFE", "canonical recovery source"))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(observe.call_count, 1)

            reset_remote()
            prepared, snapshot, journal = self.recovery_source("5")
            prepared_items.append(prepared)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            journal = append_journal_event(journal, "completed", {"phase": "completed"}, prepared=prepared)
            append_journal_event(journal, "cleanup_pending", {"phase": "quarantine", "kind": "quarantine"}, prepared=prepared)
            inspect(prepared, "cleanup-only")
        finally:
            for prepared in prepared_items:
                self.cleanup(prepared)

    def test_canonical_recovery_r0_rejects_multi_binding_and_snapshot_before_observation(self) -> None:
        prepared, snapshot, journal = self.recovery_source("6")
        marker = "RECOVERY_EARLY_MARKER"

        def rejected(code: str) -> ConfigError:
            with patch.object(promote_module, "observe_canonical_remote_head", side_effect=AssertionError(marker)):
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(raised.exception.code, code)
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn(marker, str(raised.exception))
            self.assertNotIn(marker, repr(raised.exception))
            return raised.exception

        try:
            duplicate_id = "20260820T000000000000Z-" + "a" * 16
            duplicate = journal.path.with_name(f"{duplicate_id}.jsonl")
            baseline = json.loads(journal.path.read_text(encoding="utf-8").splitlines()[0])
            baseline["operation_id"] = duplicate_id
            baseline["record_sha256"] = promote_module._record_hash(baseline, "record_sha256")
            duplicate.write_bytes(promote_module._canonical_bytes(baseline) + b"\n")
            rejected("FAIL_RECOVERY_AMBIGUOUS")
            duplicate.unlink()

            original_config = self.config.read_bytes()
            self.config.write_bytes(original_config + b"\n")
            rejected("FAIL_STATE_BINDING")
            self.config.write_bytes(original_config)

            original_journal = journal.path.read_bytes()
            forged = json.loads(original_journal.splitlines()[0])
            forged["prepared_tree_oid"] = git(self.repo, "rev-parse", f"{prepared.expected_remote_sha}^{{tree}}")
            forged["record_sha256"] = promote_module._record_hash(forged, "record_sha256")
            journal.path.write_bytes(promote_module._canonical_bytes(forged) + b"\n")
            error = rejected("FAIL_TRANSACTION_SCOPE")
            self.assertEqual(error.detail, "canonical recovery artifacts")
            journal.path.write_bytes(original_journal)

            manifest = snapshot.path / "manifest.json"
            manifest.write_bytes(b"RECOVERY_SNAPSHOT_MARKER")
            error = rejected("FAIL_TRANSACTION_SCOPE")
            self.assertEqual(error.detail, "canonical recovery artifacts")
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_r0_event_gates_refuse_after_one_observation(self) -> None:
        prepared_items = []

        def refused(prepared, code: str) -> None:
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("fast-forward")), \
                    patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
                with self.assertRaises(ConfigError) as raised:
                    plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(raised.exception.code, code)
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(observe.call_count, 1)

        try:
            prepared, _snapshot, journal = self.recovery_source("9")
            prepared_items.append(prepared)
            append_journal_event(journal, "failed", {"phase": "apply", "code": "REMOTE_OUTCOME_UNKNOWN"}, prepared=prepared)
            refused(prepared, "FAIL_RECOVERY_INDETERMINATE")

            prepared, _snapshot, journal = self.recovery_source("a")
            prepared_items.append(prepared)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            append_journal_event(journal, "failed", {"phase": "apply", "code": "REMOTE_COMMITTED_SCOPE_UNVERIFIED"}, prepared=prepared)
            refused(prepared, "FAIL_RECOVERY_SCOPE_UNVERIFIED")
            real_observe = promote_module.observe_canonical_remote_head
            with patch.object(promote_module, "_recovery_quarantine_valid", return_value=True) as quarantine, \
                    patch.object(promote_module, "canonical_push_exact_lease", side_effect=AssertionError("push")), \
                    patch.object(promote_module, "fast_forward_local", side_effect=AssertionError("fast-forward")), \
                    patch.object(promote_module, "observe_canonical_remote_head", wraps=real_observe) as observe:
                recovery = plan_canonical_recovery(self.state, prepared.sha, config_path=self.config)
            self.assertEqual(recovery.payload["action"], "cleanup-only")
            self.assertEqual((quarantine.call_count, observe.call_count), (1, 1))

            base = prepared.expected_remote_sha
            git(self.seed, "push", "-q", "--force", "origin", f"{base}:refs/heads/main")
            git(self.repo, "fetch", "-q", "origin")
            prepared, _snapshot, _journal = self.recovery_source("b", push_attempt=False)
            prepared_items.append(prepared)
            git(prepared.txn, "push", "-q", "origin", f"{prepared.sha}:refs/heads/main")
            refused(prepared, "FAIL_RECOVERY_UNSAFE")
        finally:
            for prepared in prepared_items:
                self.cleanup(prepared)

    def test_canonical_recovery_cli_plan_pending_apply_and_standalone_regression(self) -> None:
        prepared, _snapshot, _journal = self.recovery_source("8")
        try:
            rendered = io.StringIO()
            with patch("sys.stdout", rendered):
                self.assertEqual(cli_module.main([
                    "recover", "--state", str(self.state), "--config", str(self.config), "--sha", prepared.sha,
                ]), 0)
            self.assertIn(f"RECOVERY_SOURCE {_journal.operation_id}", rendered.getvalue())
            self.assertIn("RECOVERY_ACTION artifact-cleanup", rendered.getvalue())

            rendered = io.StringIO()
            result = promote_module.RecoveryResult("artifact-cleanup", "20260820T000000000000Z-" + "a" * 16,
                                                   True, False)
            with patch.object(cli_module, "dispatch_recovery", return_value=promote_module.CanonicalApplyDispatch(result)) as dispatch, \
                    patch("sys.stdout", rendered):
                self.assertEqual(cli_module.main([
                    "recover", "--state", str(self.state), "--config", str(self.config), "--sha", prepared.sha,
                    "--apply", "--plan-hash", "a" * 64, "--expected-remote-sha", prepared.expected_remote_sha,
                ]), 0)
            dispatch.assert_called_once_with(
                self.state, prepared.sha, None, apply=True, plan_hash="a" * 64,
                expected_observed_sha=prepared.expected_remote_sha, config_path=self.config, requested_mode="canonical",
            )
            self.assertEqual(rendered.getvalue().splitlines(), [
                f"PASS recovery operation_id={result.operation_id} action=artifact-cleanup converged=true cleanup_pending=false",
            ])

            rendered = io.StringIO()
            with patch.object(cli_module, "dispatch_recovery", return_value=promote_module.StandaloneDispatch("b" * 40, "--sha")) as dispatch, \
                    patch("sys.stdout", rendered):
                self.assertEqual(cli_module.main(["recover", "--state", str(self.state), "--sha", "b" * 40]), 0)
            self.assertEqual(dispatch.call_args.kwargs["requested_mode"], "standalone")
            self.assertEqual(rendered.getvalue().splitlines(), [
                "RECOVERY_SOURCE --sha", f"PASS local_recovered={'b' * 40}",
            ])
        finally:
            self.cleanup(prepared)

    def test_canonical_recovery_cli_resolution_hides_marker(self) -> None:
        marker = str(Path(self.temp.name) / "RECOVERY_CLI_RESOLUTION_MARKER")
        errors = io.StringIO()
        with patch.object(cli_module, "dispatch_recovery", side_effect=ConfigError("FAIL_STATE_REPOSITORY", "canonical recovery repository")), \
                patch("sys.stderr", errors):
            self.assertEqual(cli_module.main(["recover", "--state", str(self.state), "--config", str(self.config), "--sha", "a" * 40]), 1)
        rendered = errors.getvalue()
        self.assertEqual(rendered, "FAIL_STATE_REPOSITORY canonical recovery repository\n")
        self.assertNotIn(marker, rendered)
        self.assertNotIn(str(self.state), rendered)

    def test_canonical_recovery_cli_apply_second_resolution_hides_marker(self) -> None:
        marker = str(Path(self.temp.name) / "RECOVERY_CLI_APPLY_RESOLUTION_MARKER")
        errors = io.StringIO()
        with patch.object(cli_module, "dispatch_recovery", side_effect=ConfigError("FAIL_TRANSACTION_SCOPE", "canonical recovery artifacts")) as dispatch, \
                patch("sys.stderr", errors):
            self.assertEqual(cli_module.main([
                "recover", "--state", str(self.state), "--config", str(self.config), "--sha", "a" * 40,
                "--apply", "--plan-hash", "b" * 64, "--expected-remote-sha", "c" * 40,
            ]), 1)
        rendered = errors.getvalue()
        self.assertEqual(rendered, "FAIL_TRANSACTION_SCOPE canonical recovery artifacts\n")
        self.assertEqual(dispatch.call_count, 1)
        self.assertNotIn(marker, rendered)
        self.assertNotIn(str(self.state), rendered)

    def test_scope_mode_and_normalization_gates_fail_closed(self) -> None:
        context = resolve_repository_context(self.state)
        expected = git(self.repo, "rev-parse", "origin/main")
        path = f"state/inbox/{self.item_id('d')}.md"
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            destination = context.temporary_state_root(txn) / "inbox" / f"{self.item_id('d')}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("candidate", encoding="utf-8")
            (txn / "engine" / "extra.txt").write_text("extra", encoding="utf-8")
            git(txn, "add", "engine/extra.txt")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _commit(txn, "scope", expected, context, (ChangeExpectation("A", path),))
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)

        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            oid = git(txn, "hash-object", "-w", "engine/agent_core/promote.py")
            git(txn, "update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _assert_cached_scope(txn, (path,))
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)
        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
            _validate_changed_paths(("state/inbox/Café.md", "state/inbox/Café.md"))

    def test_missing_context_fails_before_lock_snapshot_remote_or_push(self) -> None:
        txn = self.control / "txn" / "owned"
        txn.mkdir(parents=True)
        prepared = Prepared(
            self.repo, self.control, txn, "a" * 40, "b" * 40, "publish",
            self.item_id("1"), (),
        )
        with (
            patch.object(promote_module, "operation_lock", side_effect=AssertionError("lock")),
            patch.object(promote_module, "_snapshot", side_effect=AssertionError("snapshot")),
            patch.object(promote_module, "_remote_head", side_effect=AssertionError("remote")),
            patch.object(promote_module, "_push", side_effect=AssertionError("push")),
        ):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_CONTEXT"):
                apply_prepared(prepared)
        self.assertFalse(txn.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_junction_preflight_blocks_live_read_and_txn_write(self) -> None:
        item = self.item_id("2")
        outside = Path(self.temp.name) / "outside"
        candidate(outside, item, git(self.repo, "rev-parse", "HEAD"))
        inbox = self.state / "inbox"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(inbox), str(outside / "inbox")],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        before = tuple(sorted(path.name for path in (outside / "inbox").iterdir()))
        with self.assertRaisesRegex(ConfigError, "FAIL_(TRANSACTION_SCOPE|DIRTY)"):
            plan_publish(self.state, None, item, config_path=self.config)
        self.assertEqual(tuple(sorted(path.name for path in (outside / "inbox").iterdir())), before)
        inbox.rmdir()
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        txn = _new_worktree(resolve_repository_context(self.state).repo_root,
                             Path(plan.payload["binding"]["control_identity"]), plan.expected_remote_sha)
        external_txn = Path(self.temp.name) / "external-txn"
        external_txn.mkdir()
        txn_inbox = txn / "state" / "inbox"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(txn_inbox), str(external_txn)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(promote_module, "_new_worktree", return_value=txn):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)
        self.assertFalse((external_txn / f"{item}.md").exists())
        self.assertFalse(txn.exists())

    def test_strict_name_status_and_final_commit_gates(self) -> None:
        good = subprocess.CompletedProcess(
            ("git",), 0, b"A\0state/inbox/caf\xc3\xa9.md\0M\0state/experience/LESSONS.md\0D\0state/inbox/old.md\0T\0state/x.md\0", b"",
        )
        self.assertEqual(len(_parse_name_status(good)), 4)
        for bad in (
            subprocess.CompletedProcess(("git",), 1, b"", b"bad"),
            subprocess.CompletedProcess(("git",), 0, b"A\0state/x.md", b""),
            subprocess.CompletedProcess(("git",), 0, b"A\0state/x.md\0M\0", b""),
            subprocess.CompletedProcess(("git",), 0, b"R100\0state/x.md\0state/y.md\0", b""),
        ):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _parse_name_status(bad)
        context = resolve_repository_context(self.state)
        expected = git(self.repo, "rev-parse", "origin/main")
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            (txn / "state" / "inbox").mkdir(exist_ok=True)
            (txn / "state" / "inbox" / "café.md").write_text("utf8", encoding="utf-8")
            (txn / "state" / "experience" / "LESSONS.md").write_text("changed", encoding="utf-8")
            (txn / "obsolete.txt").unlink()
            oid = git(txn, "hash-object", "-w", "engine/agent_core/promote.py")
            git(txn, "update-index", "--cacheinfo", f"120000,{oid},engine/agent_core/promote.py")
            git(txn, "add", "state/inbox/café.md", "state/experience/LESSONS.md", "obsolete.txt")
            actual = subprocess.run(
                ["git", "-C", str(txn), "diff", "--cached", "--no-renames", "--name-status", "-z"],
                check=False, capture_output=True,
            )
            self.assertEqual({status for status, _path in _parse_name_status(actual)}, {"A", "M", "D", "T"})
            self.assertIn(("A", "state/inbox/café.md"), _parse_name_status(actual))
            git(txn, "reset", "-q")
            (txn / "state" / "inbox" / "café.md").unlink()
            (txn / "state" / "experience" / "LESSONS.md").write_text(
                (self.state / "experience" / "LESSONS.md").read_text(encoding="utf-8"), encoding="utf-8",
            )
            (txn / "obsolete.txt").write_text("obsolete\n", encoding="utf-8")
            (txn / "state" / "inbox").mkdir(exist_ok=True)
            (txn / "state" / "inbox" / "allowed.md").write_text("allowed", encoding="utf-8")
            (txn / "root-extra.txt").write_text("extra", encoding="utf-8")
            git(txn, "add", "state/inbox/allowed.md", "root-extra.txt")
            git(txn, "commit", "-q", "-m", "extra")
            sha = git(txn, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _assert_committed_scope(
                    txn, expected, sha,
                    (ChangeExpectation("A", "state/inbox/allowed.md"),), context,
                )
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _assert_committed_scope(txn, expected, expected, (), context)
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)

    def test_ledger_wrong_type_fails_before_promote_read(self) -> None:
        item = self.item_id("4")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        ledger = self.state / "experience" / "LESSONS.md"
        ledger.unlink()
        ledger.mkdir()
        with self.assertRaisesRegex(ConfigError, "FAIL_(TRANSACTION_SCOPE|DIRTY)"):
            plan_promote(self.state, None, item, force_new=True, config_path=self.config)

    def test_commit_disables_observable_hook(self) -> None:
        item = self.item_id("3")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        marker = Path(self.temp.name) / "hook-marker"
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\nprintf hook > '{marker.as_posix()}'\n", encoding="utf-8")
        plan = plan_publish(self.state, None, item, config_path=self.config)
        prepared = prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   config_path=self.config)
        self.assertFalse(marker.exists())
        self.cleanup(prepared)

    def test_commit_uses_unique_empty_hooks_directories(self) -> None:
        marker = Path(self.temp.name) / "legacy-hook-marker"
        legacy = self.control / "txn" / "hooks-empty"
        legacy.mkdir(parents=True)
        (legacy / "post-commit").write_text(
            f"#!/bin/sh\nprintf legacy > '{marker.as_posix()}'\n", encoding="utf-8",
        )
        seen: list[Path] = []
        original = promote_module._git

        def observed(repo: Path, *args: str, **kwargs):
            for arg in args:
                if arg.startswith("core.hooksPath="):
                    seen.append(Path(arg.split("=", 1)[1]))
            return original(repo, *args, **kwargs)

        with patch.object(promote_module, "_git", side_effect=observed):
            for suffix in ("a", "b"):
                item = self.item_id(suffix)
                candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
                plan = plan_publish(self.state, None, item, config_path=self.config)
                prepared = prepare_publish(
                    self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                    config_path=self.config,
                )
                self.cleanup(prepared)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertTrue(all(not path.exists() for path in seen))
        self.assertFalse(marker.exists())

    def test_apply_reproves_forged_or_stale_context_before_side_effects(self) -> None:
        context = resolve_repository_context(self.state)
        forged = (
            replace(context, layout="standalone", state_prefix=""),
            replace(context, state_prefix="other/"),
        )
        for index, supplied in enumerate(forged):
            txn = self.control / "txn" / f"forged-{index}"
            txn.mkdir(parents=True)
            prepared = Prepared(
                self.repo, self.control, txn, "a" * 40, "b" * 40, "publish",
                self.item_id("j"), (), context=supplied,
            )
            with (
                patch.object(promote_module, "operation_lock", side_effect=AssertionError("lock")),
                patch.object(promote_module, "_snapshot", side_effect=AssertionError("snapshot")),
                patch.object(promote_module, "_remote_head", side_effect=AssertionError("remote")),
                patch.object(promote_module, "_push", side_effect=AssertionError("push")),
            ):
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_CONTEXT"):
                    apply_prepared(prepared)
            self.assertFalse(txn.exists())

    def test_parent_files_fail_closed_before_live_or_txn_access(self) -> None:
        item = self.item_id("c")
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        source = self.state / "inbox" / f"{item}.md"
        source.unlink()
        inbox = self.state / "inbox"
        inbox.rmdir()
        inbox.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_(TRANSACTION_SCOPE|DIRTY)"):
            plan_publish(self.state, None, item, config_path=self.config)
        inbox.unlink()
        candidate(self.state, item, git(self.repo, "rev-parse", "HEAD"))
        plan = plan_publish(self.state, None, item, config_path=self.config)
        context = resolve_repository_context(self.state)
        txn = _new_worktree(context.repo_root, Path(plan.payload["binding"]["control_identity"]), plan.expected_remote_sha)
        txn_inbox = txn / "state" / "inbox"
        txn_inbox.write_text("not a directory", encoding="utf-8")
        with patch.object(promote_module, "_new_worktree", return_value=txn):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                prepare_publish(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                config_path=self.config)
        self.assertFalse(txn.exists())
        promoted = self.item_id("e")
        candidate(self.seed / "state", promoted, git(self.seed, "rev-parse", "HEAD"))
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-q", "-m", "published candidate")
        git(self.seed, "push", "-q")
        git(self.repo, "pull", "-q", "--ff-only")
        promote_plan = plan_promote(
            self.state, None, promoted, force_new=True,
            reviewed_against=git(self.repo, "rev-parse", "origin/main"),
            config_path=self.config,
        )
        before_source = (self.state / "inbox" / f"{promoted}.md").read_bytes()
        before_ledger = (self.state / "experience" / "LESSONS.md").read_bytes()
        txn = _new_worktree(context.repo_root, Path(promote_plan.payload["binding"]["control_identity"]),
                             promote_plan.expected_remote_sha)
        txn_consumed = txn / "state" / "inbox" / "consumed"
        txn_consumed.write_text("not a directory", encoding="utf-8")
        with patch.object(promote_module, "_new_worktree", return_value=txn):
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                prepare_promote(
                    self.state, None, promote_plan, promote_plan.plan_hash,
                    promote_plan.expected_remote_sha, config_path=self.config,
                )
        self.assertFalse(txn.exists())
        self.assertEqual((self.state / "inbox" / f"{promoted}.md").read_bytes(), before_source)
        self.assertEqual((self.state / "experience" / "LESSONS.md").read_bytes(), before_ledger)
        ledger = self.state / "experience" / "LESSONS.md"
        ledger.unlink()
        shutil.move(self.state / "experience", self.state / "experience-real")
        (self.state / "experience").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "FAIL_(TRANSACTION_SCOPE|DIRTY)"):
            plan_promote(self.state, None, item, force_new=True, config_path=self.config)

    def test_commit_rejects_status_exchange_and_publish_executable(self) -> None:
        context = resolve_repository_context(self.state)
        expected = git(self.repo, "rev-parse", "origin/main")
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            path = f"state/inbox/{self.item_id('l')}.md"
            target = txn / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("candidate", encoding="utf-8")
            git(txn, "add", path)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _commit(txn, "wrong-status", expected, context, (ChangeExpectation("D", path),))
            git(txn, "update-index", "--chmod=+x", "--", path)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _commit(txn, "publish-mode", expected, context, (ChangeExpectation("A", path, "100644"),))
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)

    def test_deletion_symlink_parent_and_canonical_rollback_fail_closed(self) -> None:
        context = resolve_repository_context(self.state)
        oid = git(self.repo, "hash-object", "-w", "engine/agent_core/promote.py")
        path = "state/inbox/link.md"
        git(self.repo, "update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
        git(self.repo, "commit", "-q", "-m", "symlink parent")
        git(self.repo, "push", "-q")
        expected = git(self.repo, "rev-parse", "HEAD")
        txn = _new_worktree(context.repo_root, self.control, expected)
        try:
            git(txn, "update-index", "--force-remove", path)
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                _capture_precommit_facts(txn, expected, (ChangeExpectation("D", path),))
        finally:
            _cleanup_worktree(context.repo_root, self.control, txn)
        rollback_id = "20260815T000000000000Z"
        with (
            patch.object(promote_module, "require_fresh", side_effect=AssertionError("freshness")),
            patch.object(promote_module, "operation_lock", side_effect=AssertionError("lock")),
            patch.object(promote_module, "_new_worktree", side_effect=AssertionError("txn")),
            patch.object(promote_module, "_remote_head", side_effect=AssertionError("remote")),
            patch.object(promote_module, "_push", side_effect=AssertionError("push")),
        ):
            with self.assertRaisesRegex(ConfigError, "FAIL_ROLLBACK_ID"):
                rollback(
                    self.state, None, rollback_id, apply=True,
                    plan_hash="a" * 64, expected_remote_sha=expected,
                    config_path=self.config,
                )

    def test_canonical_advance_uses_committed_evidence_without_quarantine(self) -> None:
        lesson_id = "L-1"
        receipt = self.canonical_advance_receipt()
        receipt_sha = receipt.stem
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        plan = plan_advance(self.state, None, lesson_id, receipt, config_path=self.config)
        prepared = prepare_advance(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, receipt,
            config_path=self.config,
        )
        try:
            self.assertEqual(prepared.operation, "advance")
            self.assertEqual(prepared.input_digest_sha256, receipt_sha)
            self.assertEqual(dict(prepared.advance_evidence or ()), {
                "ledger_path": "experience/LESSONS.md", "from_status": "checklist",
                "to_status": "enforced", "verifier_id": "synthetic-contract",
                "verified_utc": payload["verified_utc"],
            })
            self.assertEqual(set(prepared.changed_paths), {
                f"state/evidence/{lesson_id}/{receipt_sha}.json", "state/experience/LESSONS.md",
            })
            committed_evidence = promote_module._git_bytes(
                prepared.txn, "show", f"{prepared.sha}:state/evidence/{lesson_id}/{receipt_sha}.json",
            )
            self.assertEqual(committed_evidence.returncode, 0)
            self.assertEqual(promote_module.hashlib.sha256(committed_evidence.stdout).hexdigest(), receipt_sha)
            self.assertEqual(prepared.input_digest_sha256, receipt_sha)
            self.assertEqual(promote_module.revalidate_canonical_prepared_locked(prepared)[0].status, "A")

            # The caller-owned receipt stops being an input after prepare.
            receipt.write_bytes(b"changed externally")
            receipt.unlink()
            with patch.object(promote_module, "_quarantine_publish_source", side_effect=AssertionError("quarantine")), \
                    patch.object(promote_module, "_cleanup_quarantine", side_effect=AssertionError("quarantine")):
                result = apply_prepared(prepared)
            self.assertEqual(result.sha, prepared.sha)
            self.assertEqual(result.rollback_id, promote_module._read_journal(
                next((prepared.control_root / "journal").glob("*.jsonl")),
                prepared.control_root / "journal",
            )[0]["snapshot_id"])
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), prepared.sha)
            self.assertEqual(git(self.remote, "rev-parse", "main"), prepared.sha)
            manifest = json.loads((next((prepared.control_root / "snapshots").iterdir()) / "manifest.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["local_input"])
            journal = promote_module._read_journal(
                next((prepared.control_root / "journal").glob("*.jsonl")), prepared.control_root / "journal",
            )
            events = [entry.get("event", "baseline") for entry in journal]
            self.assertIn("completed", events)
            self.assertNotIn("source_removed", events)
        finally:
            self.cleanup(prepared)

    def test_canonical_advance_forged_prepared_rejects_before_push(self) -> None:
        lesson_id = "L-1"
        receipt = self.canonical_advance_receipt()
        plan = plan_advance(self.state, None, lesson_id, receipt, config_path=self.config)
        prepared = prepare_advance(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                   receipt, config_path=self.config)
        try:
            mutated = replace(prepared, changed_paths=prepared.changed_paths + ("state/manifest.yaml",))
            semantics = dict(prepared.advance_evidence or ())
            semantics["to_status"] = "archived"
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.revalidate_canonical_prepared_locked(
                    replace(prepared, advance_evidence=tuple(sorted(semantics.items()))))
            with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE"):
                promote_module.revalidate_canonical_prepared_locked(mutated)
            forged = replace(prepared, advance_evidence=tuple(sorted(semantics.items())))
            with patch.object(promote_module, "create_canonical_snapshot") as snapshot, \
                    patch.object(promote_module, "canonical_push_exact_lease") as push, \
                    patch.object(promote_module, "observe_canonical_remote_head") as observe:
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as failure_context:
                    apply_prepared(forged)
            error = failure_context.exception
            self.assertEqual((error.code, error.detail), ("FAIL_TRANSACTION_SCOPE", "canonical advance revalidation"))
            self.assertIsNone(error.__cause__)
            self.assertEqual((snapshot.call_count, push.call_count, observe.call_count), (0, 0, 0))
            self.assertFalse(forged.txn.exists())
        finally:
            self.cleanup(prepared)

    def test_canonical_advance_revalidation_hides_boundary_failures(self) -> None:
        lesson_id = "L-1"
        receipt = self.canonical_advance_receipt()
        plan = plan_advance(self.state, None, lesson_id, receipt, config_path=self.config)
        cases = (
            ("_git", ConfigError("FAIL_GIT", "UNIQUE_ADVANCE_GIT_MARKER"), "UNIQUE_ADVANCE_GIT_MARKER"),
            ("_git_bytes", OSError("UNIQUE_ADVANCE_OS_MARKER"), "UNIQUE_ADVANCE_OS_MARKER"),
            ("_git_bytes", subprocess.TimeoutExpired(["git", "UNIQUE_ADVANCE_TIMEOUT_MARKER"], 1),
             "UNIQUE_ADVANCE_TIMEOUT_MARKER"),
            ("_git_bytes", UnicodeDecodeError("utf-8", b"x", 0, 1, "UNIQUE_ADVANCE_UNICODE_MARKER"),
             "UNIQUE_ADVANCE_UNICODE_MARKER"),
        )
        for primitive, failure, marker in cases:
            with self.subTest(marker=marker):
                prepared = prepare_advance(
                    self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, receipt,
                    config_path=self.config,
                )
                try:
                    real_git = promote_module._git

                    def fail_preflight_head(repo: Path, *args: str, **kwargs):
                        if repo == prepared.repo and args == ("rev-parse", "HEAD"):
                            raise failure
                        return real_git(repo, *args, **kwargs)

                    failure_patch = (patch.object(promote_module, "_git", side_effect=fail_preflight_head)
                                     if primitive == "_git"
                                     else patch.object(promote_module, primitive, side_effect=failure))
                    with failure_patch, \
                            patch.object(promote_module, "create_canonical_snapshot") as snapshot, \
                            patch.object(promote_module, "canonical_push_exact_lease") as push, \
                            patch.object(promote_module, "observe_canonical_remote_head") as observe:
                        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as failure_context:
                            apply_prepared(prepared)
                    error = failure_context.exception
                    self.assertEqual((error.code, error.detail),
                                     ("FAIL_TRANSACTION_SCOPE", "canonical advance revalidation"))
                    self.assertIsNone(error.__cause__)
                    self.assertNotIn(marker, str(error))
                    self.assertNotIn(marker, repr(error))
                    self.assertEqual((snapshot.call_count, push.call_count, observe.call_count), (0, 0, 0))
                    self.assertFalse(prepared.txn.exists())
                finally:
                    self.cleanup(prepared)

    def test_canonical_advance_under_lock_failure_records_closed_discriminator(self) -> None:
        lesson_id = "L-1"
        receipt = self.canonical_advance_receipt()
        plan = plan_advance(self.state, None, lesson_id, receipt, config_path=self.config)
        prepared = prepare_advance(
            self.state, None, plan, plan.plan_hash, plan.expected_remote_sha, receipt,
            config_path=self.config,
        )
        real_require_fresh = promote_module.require_fresh
        fetches: list[bool] = []

        def fail_second(repo: Path, operation: str, control_root: Path, *, fetch: bool = True):
            fetches.append(fetch)
            fresh = real_require_fresh(repo, operation, control_root, fetch=fetch)
            if len(fetches) == 2:
                raise OSError("UNIQUE_ADVANCE_UNDER_LOCK_MARKER")
            return fresh

        try:
            with patch.object(promote_module, "require_fresh", side_effect=fail_second), \
                    patch.object(promote_module, "canonical_push_exact_lease") as push, \
                    patch.object(promote_module, "observe_canonical_remote_head") as observe:
                with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as failure_context:
                    apply_prepared(prepared)
            error = failure_context.exception
            self.assertEqual((error.code, error.detail), ("FAIL_TRANSACTION_SCOPE", "canonical advance revalidation"))
            self.assertIsNone(error.__cause__)
            self.assertNotIn("UNIQUE_ADVANCE_UNDER_LOCK_MARKER", str(error))
            self.assertNotIn("UNIQUE_ADVANCE_UNDER_LOCK_MARKER", repr(error))
            self.assertEqual((push.call_count, observe.call_count), (0, 0))
            self.assertEqual(fetches, [True, False])
            self.assertFalse(prepared.txn.exists())
            journal = promote_module._read_journal(
                next((prepared.control_root / "journal").glob("*.jsonl")), prepared.control_root / "journal",
            )
            failed = next(record for record in journal if record.get("event") == "failed")
            self.assertEqual(failed["details"], {
                "phase": "apply", "code": "FAIL_TRANSACTION_SCOPE", "site": "under_lock", "reason": "os",
            })
        finally:
            self.cleanup(prepared)

    def test_canonical_advance_committed_decode_failures_hide_causes_before_snapshot(self) -> None:
        lesson_id = "L-1"
        ledger_relative = "state/experience/LESSONS.md"
        receipt = self.canonical_advance_receipt()
        raw = receipt.read_bytes()
        payload = json.loads(raw)
        plan = plan_advance(self.state, None, lesson_id, receipt, config_path=self.config)

        def prepare_corrupt(evidence_raw: bytes, ledger_raw: bytes) -> Prepared:
            prepared = prepare_advance(self.state, None, plan, plan.plan_hash, plan.expected_remote_sha,
                                       receipt, config_path=self.config)
            try:
                digest = promote_module.hashlib.sha256(evidence_raw).hexdigest()
                evidence_relative = f"state/evidence/{lesson_id}/{digest}.json"
                original_evidence = f"state/evidence/{lesson_id}/{prepared.input_digest_sha256}.json"
                if original_evidence != evidence_relative:
                    (prepared.txn / original_evidence).unlink()
                evidence_path = prepared.txn / evidence_relative
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_bytes(evidence_raw)
                (prepared.txn / ledger_relative).write_bytes(ledger_raw)
                git(prepared.txn, "add", "-A", "--", original_evidence, evidence_relative, ledger_relative)
                git(prepared.txn, "commit", "--amend", "--no-edit", "-q")
                sha = git(prepared.txn, "rev-parse", "HEAD")
                self.assertEqual(
                    git(prepared.txn, "rev-list", "--parents", "-n", "1", sha),
                    f"{sha} {prepared.expected_remote_sha}",
                )
                self.assertEqual(set(promote_module._parse_name_status(promote_module._git_bytes(
                    prepared.txn, "diff", "--no-renames", "--name-status", "-z",
                    prepared.expected_remote_sha, sha,
                ))), {("A", evidence_relative), ("M", ledger_relative)})
                self.assertEqual(promote_module._tree_entry(prepared.txn, sha, evidence_relative)[0], "100644")
                self.assertEqual(promote_module._tree_entry(prepared.txn, sha, ledger_relative)[0], "100644")
                return replace(
                    prepared,
                    sha=sha,
                    tree_oid=git(prepared.txn, "rev-parse", f"{sha}^{{tree}}"),
                    input_digest_sha256=digest,
                    changed_paths=(evidence_relative, ledger_relative),
                )
            except Exception:
                self.cleanup(prepared)
                raise

        base_ledger = (self.state / "experience" / "LESSONS.md").read_text(encoding="utf-8")
        line_index = next(index for index, line in enumerate(base_ledger.splitlines()) if lesson_id in line)
        corrupt_digest = promote_module.hashlib.sha256(b"\xffUNIQUE_ADVANCE_EVIDENCE_MARKER").hexdigest()
        deterministic_ledger = promote_module._render_advance(
            base_ledger, line_index, lesson_id, payload, corrupt_digest,
        ).encode("utf-8")
        cases = (
            (b"\xffUNIQUE_ADVANCE_EVIDENCE_MARKER", deterministic_ledger,
             "UNIQUE_ADVANCE_EVIDENCE_MARKER"),
            (raw, b"\xffUNIQUE_ADVANCE_LEDGER_MARKER", "UNIQUE_ADVANCE_LEDGER_MARKER"),
        )
        for evidence_raw, ledger_raw, marker in cases:
            with self.subTest(marker=marker):
                prepared = prepare_corrupt(evidence_raw, ledger_raw)
                try:
                    with patch.object(promote_module, "create_canonical_snapshot") as snapshot, \
                            patch.object(promote_module, "canonical_push_exact_lease") as push, \
                            patch.object(promote_module, "observe_canonical_remote_head") as observe:
                        with self.assertRaisesRegex(ConfigError, "FAIL_TRANSACTION_SCOPE") as failure_context:
                            apply_prepared(prepared)
                    error = failure_context.exception
                    self.assertEqual(error.code, "FAIL_TRANSACTION_SCOPE")
                    self.assertIsNone(error.__cause__)
                    self.assertNotIn(marker, str(error))
                    self.assertNotIn(marker, repr(error))
                    self.assertEqual(snapshot.call_count, 0)
                    self.assertEqual(push.call_count, 0)
                    self.assertEqual(observe.call_count, 0)
                    self.assertFalse(prepared.txn.exists())
                finally:
                    self.cleanup(prepared)

    def test_standalone_publish_and_promote_paths_remain_unprefixed(self) -> None:
        remote = Path(self.temp.name) / "standalone.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        seed = Path(self.temp.name) / "standalone-seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "config", "user.name", "Test")
        git(seed, "config", "user.email", "test@invalid")
        (seed / "experience").mkdir()
        (seed / "experience" / "LESSONS.md").write_text(
            "# Lessons Ledger\n<!-- lessons-schema: lessons-ledger/2 -->\n"
            "<!-- lessons-scope: global -->\n\n## 活跃\n\n## 归档\n",
            encoding="utf-8",
        )
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(remote))
        git(seed, "push", "-q", "-u", "origin", "main")
        git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        standalone_input = Path(self.temp.name) / "standalone"
        subprocess.run(["git", "clone", "-q", str(remote), str(standalone_input)], check=True)
        git(standalone_input, "config", "user.name", "Test")
        git(standalone_input, "config", "user.email", "test@invalid")
        standalone = Path(git(standalone_input, "rev-parse", "--show-toplevel"))
        item = self.item_id("e")
        candidate(standalone, item, git(standalone, "rev-parse", "HEAD"))
        plan = plan_publish(standalone, self.control, item)
        prepared = prepare_publish(standalone, self.control, plan, plan.plan_hash, plan.expected_remote_sha)
        self.assertEqual(prepared.changed_paths, (f"inbox/{item}.md",))
        apply_prepared(prepared)
        self.assertTrue((standalone / "inbox" / f"{item}.md").is_file())
        self.assertEqual(git(standalone, "status", "--porcelain=v1"), "")
        promoted = self.item_id("f")
        git(seed, "pull", "-q", "--ff-only")
        candidate(seed, promoted, git(seed, "rev-parse", "HEAD"))
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "published standalone candidate")
        git(seed, "push", "-q")
        git(standalone, "pull", "-q", "--ff-only")
        promote_plan = plan_promote(
            standalone, self.control, promoted, force_new=True,
            reviewed_against=git(standalone, "rev-parse", "origin/main"),
        )
        prepared = prepare_promote(
            standalone, self.control, promote_plan, promote_plan.plan_hash,
            promote_plan.expected_remote_sha,
        )
        self.assertEqual(set(prepared.changed_paths), {
            "experience/LESSONS.md", f"inbox/{promoted}.md", f"inbox/consumed/{promoted}.md",
        })
        result = apply_prepared(prepared)
        self.assertFalse(prepared.txn.exists())
        self.assertTrue((standalone / "inbox" / "consumed" / f"{promoted}.md").is_file())
        rollback_plan = rollback(standalone, self.control, result.rollback_id, apply=False)
        rollback(
            standalone, self.control, result.rollback_id, apply=True,
            plan_hash=rollback_plan.plan_hash, expected_remote_sha=rollback_plan.expected_remote_sha,
        )
        self.assertTrue((standalone / "inbox" / f"{promoted}.md").is_file())
        ledger_path = standalone / "experience" / "LESSONS.md"
        ledger_path.write_text(
            ledger_path.read_text(encoding="utf-8").replace(
                "## 归档", "- **L-1 [checklist·通用] Existing rule.** 触发: existing. "
                "代价: synthetic. sink → checks/existing.md.\n\n## 归档",
            ), encoding="utf-8",
        )
        git(standalone, "add", "experience/LESSONS.md")
        git(standalone, "commit", "-q", "-m", "checklist")
        git(standalone, "push", "-q")
        line_index = next(index for index, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines())
                          if "L-1" in line)
        receipt = Path(self.temp.name) / "receipt.json"
        receipt.write_bytes(b"synthetic receipt")
        receipt_sha = promote_module.hashlib.sha256(receipt.read_bytes()).hexdigest()
        payload = {"from_status": "checklist", "to_status": "enforced",
                   "verifier_id": "synthetic", "verified_utc": "2026-08-15T00:00:00Z"}

        def checked(workspace: Path, _lesson: str, _receipt: Path, _expected: str | None = None):
            record = LessonRecord("L-1", "checklist", "checks/existing.md", "synthetic", None,
                                  None, workspace / "experience" / "LESSONS.md", line_index, "")
            return record, ReceiptCheck(True, "", payload, receipt_sha)

        with patch.object(promote_module, "_checked_receipt", side_effect=checked):
            advance_plan = plan_advance(standalone, self.control, "L-1", receipt)
            advanced = prepare_advance(
                standalone, self.control, advance_plan, advance_plan.plan_hash,
                advance_plan.expected_remote_sha, receipt,
            )
            apply_prepared(advanced)
        self.assertIn("[enforced·通用]", ledger_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
