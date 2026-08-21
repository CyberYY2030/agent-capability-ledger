from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from agent_core.config import ConfigError
from agent_core.freshness import _parse_status, inspect, is_repository, require_fresh
from agent_core.repository import resolve_repository_context


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.resolve().as_posix()}", "-C", str(root), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def candidate(root: Path, relative: str, head: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    identifier = "desk-20260815T000000Z-" + "a" * 32
    payload = {
        "schema": "candidate/1", "id": identifier, "created_utc": "2026-08-15T00:00:00Z",
        "host": "desk", "agent": "codex", "base_revision": head,
        "rule": "Keep writes transactional", "trigger": "synthetic trigger",
        "cost": "synthetic cost", "sink": "checks/synthetic.md", "scope_hint": "global",
        "evidence": "synthetic:test",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class RepositoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "engine").mkdir()
        (self.root / "state").mkdir()
        (self.root / "state" / "tracked.txt").write_text("state", encoding="utf-8")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@invalid")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "seed")
        self.repo_root = Path(git(self.root, "rev-parse", "--show-toplevel"))
        self.state_root = self.repo_root / "state"
        self.head = git(self.root, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assertCode(self, code: str, action: object) -> None:
        with self.assertRaisesRegex(ConfigError, code):
            action()  # type: ignore[operator]

    def test_canonical_context_and_temp_mapping(self) -> None:
        context = resolve_repository_context(self.state_root)
        self.assertEqual(context.repo_root, self.repo_root)
        self.assertEqual(context.state_root, self.state_root.resolve())
        self.assertEqual(context.state_prefix, "state/")
        self.assertEqual(context.layout, "canonical")
        self.assertEqual(context.state_to_repo_path("inbox/item.md"), "state/inbox/item.md")
        self.assertEqual(context.temporary_state_root(self.root / "worktree"), (self.root / "worktree").resolve() / "state")
        self.assertTrue(is_repository(self.repo_root / "engine"))
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.repo_root / "engine"))

    def test_canonical_root_cannot_fall_back_to_standalone(self) -> None:
        root_candidate = "inbox/desk-20260815T000000Z-" + "a" * 32 + ".md"
        candidate(self.root, root_candidate, self.head)
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.repo_root))
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: require_fresh(self.repo_root, "capture", self.root / "control"))

    def test_standalone_context(self) -> None:
        standalone_input = Path(self.temp.name) / "standalone"
        standalone_input.mkdir()
        git(standalone_input, "init", "-q", "-b", "main")
        git(standalone_input, "config", "user.name", "Test")
        git(standalone_input, "config", "user.email", "test@invalid")
        (standalone_input / "tracked.txt").write_text("state", encoding="utf-8")
        git(standalone_input, "add", ".")
        git(standalone_input, "commit", "-q", "-m", "seed")
        standalone = Path(git(standalone_input, "rev-parse", "--show-toplevel"))
        context = resolve_repository_context(standalone)
        self.assertEqual(context.layout, "standalone")
        self.assertEqual(context.state_prefix, "")
        self.assertEqual(context.temporary_state_root(standalone / "temporary"), (standalone / "temporary").resolve())

    def test_invalid_nesting_missing_sibling_and_symlink_fail_closed(self) -> None:
        nested = self.state_root / "nested"
        nested.mkdir()
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(nested))
        shutil.rmtree(self.repo_root / "engine")
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.state_root))
        (self.repo_root / "engine").mkdir()
        target = self.repo_root / "state-target"
        (target / "tracked.txt").parent.mkdir(parents=True)
        (target / "tracked.txt").write_text("state", encoding="utf-8")
        shutil.rmtree(self.state_root)
        try:
            os.symlink(target, self.state_root, target_is_directory=True)
        except OSError as exc:
            original = Path.is_symlink
            with patch.object(
                Path,
                "is_symlink",
                autospec=True,
                side_effect=lambda path: path.name == "state" or original(path),
            ):
                self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.state_root))
            return
        self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.state_root))

    def test_canonical_candidate_and_dirty_boundaries(self) -> None:
        candidate(self.root, "state/inbox/desk-20260815T000000Z-" + "a" * 32 + ".md", self.head)
        state = inspect(self.state_root, self.root / "control", fetch=False)
        self.assertEqual(state.dirty, ())
        self.assertEqual(state.context.layout if state.context else None, "canonical")
        self.assertEqual(require_fresh(self.state_root, "capture", self.root / "control").dirty, ())
        (self.root / "root.tmp").write_text("root", encoding="utf-8")
        self.assertCode("FAIL_DIRTY", lambda: require_fresh(self.state_root, "capture", self.root / "control"))
        (self.root / "root.tmp").unlink()
        (self.root / "engine" / "dirty.tmp").write_text("engine", encoding="utf-8")
        self.assertCode("FAIL_DIRTY", lambda: require_fresh(self.state_root, "capture", self.root / "control"))
        (self.root / "engine" / "dirty.tmp").unlink()
        (self.root / "state" / "dirty.tmp").write_text("state", encoding="utf-8")
        self.assertCode("FAIL_DIRTY", lambda: require_fresh(self.state_root, "capture", self.root / "control"))

    def test_non_ascii_and_root_inbox_mutation_red(self) -> None:
        candidate(self.root, "state/inbox/desk-20260815T000000Z-" + "a" * 32 + ".md", self.head)
        (self.root / "state" / "café.txt").write_text("dirty", encoding="utf-8")
        state = inspect(self.state_root, self.root / "control", fetch=False)
        self.assertIn("state/café.txt", state.dirty)
        (self.root / "state" / "café.txt").unlink()
        root_candidate = "inbox/desk-20260815T000000Z-" + "a" * 32 + ".md"
        candidate(self.root, root_candidate, self.head)
        with self.assertRaisesRegex(ConfigError, "FAIL_DIRTY"):
            require_fresh(self.state_root, "capture", self.root / "control")
        (self.root / root_candidate).unlink()
        self.assertEqual(inspect(self.state_root, self.root / "control", fetch=False).dirty, ())

    def test_raw_backslash_and_rename_followers_are_dirty(self) -> None:
        context = resolve_repository_context(self.state_root)
        identifier = "desk-20260815T000000Z-" + "a" * 32 + ".md"
        candidate(self.root, f"state/inbox/{identifier}", self.head)
        self.assertEqual(inspect(self.state_root, self.root / "control", fetch=False).dirty, ())
        self.assertIsNone(context.repo_to_state_path(f"state\\inbox\\{identifier}"))
        dirty, unmerged = _parse_status(context, f"?? state\\inbox\\{identifier}\0")
        self.assertEqual(dirty, (f"state\\inbox\\{identifier}",))
        self.assertEqual(unmerged, ())
        raw = "R  state/renamed.txt\0state/original.txt\0?? root.tmp\0?? engine/dirty.tmp\0UU state/conflict.txt\0"
        dirty, unmerged = _parse_status(context, raw)
        self.assertEqual(dirty, ("engine/dirty.tmp", "root.tmp", "state/renamed.txt"))
        self.assertEqual(unmerged, ("state/conflict.txt",))
        if os.name != "nt":
            literal = self.root / f"state\\inbox\\{identifier}"
            literal.write_text("synthetic", encoding="utf-8")
            self.assertIn(str(literal.relative_to(self.root)), inspect(self.state_root, self.root / "control", fetch=False).dirty)

    def test_rename_copy_followers_are_consumed_without_guessing(self) -> None:
        context = resolve_repository_context(self.state_root)
        raw = (
            "R  state/renamed.txt\0M follower.txt\0"
            "C  state/copied.txt\0?? follower.txt\0"
            "?? root.tmp\0?? engine/dirty.tmp\0"
        )
        dirty, unmerged = _parse_status(context, raw)
        self.assertEqual(dirty, ("engine/dirty.tmp", "root.tmp", "state/copied.txt", "state/renamed.txt"))
        self.assertEqual(unmerged, ())
        dirty, unmerged = _parse_status(context, "R  state/missing-follower.txt")
        self.assertEqual(dirty, ("state/missing-follower.txt",))
        self.assertEqual(unmerged, ())

    def test_candidate_alias_is_dirty(self) -> None:
        identifier = "desk-20260815T000000Z-" + "a" * 32 + ".md"
        target = self.root / "outside" / identifier
        candidate(self.root, str(target.relative_to(self.root)), self.head)
        link = self.state_root / "inbox" / identifier
        link.parent.mkdir()
        try:
            os.symlink(target, link)
        except OSError:
            candidate(self.root, f"state/inbox/{identifier}", self.head)
            from agent_core import freshness

            original = freshness._is_reparse_alias
            with patch.object(freshness, "_is_reparse_alias", side_effect=lambda path: path == link or original(path)):
                self.assertCode("FAIL_DIRTY", lambda: require_fresh(self.state_root, "capture", self.root / "control"))
            return
        self.assertCode("FAIL_DIRTY", lambda: require_fresh(self.state_root, "capture", self.root / "control"))

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_layouts_fail_closed(self) -> None:
        def junction(name: str) -> bool:
            link = self.repo_root / name
            target = self.repo_root / f"{name}-target"
            target.mkdir()
            if name == "state":
                (target / "tracked.txt").write_text("state", encoding="utf-8")
                shutil.rmtree(link)
            else:
                shutil.rmtree(link)
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=False, capture_output=True, text=True, encoding="utf-8",
            )
            if result.returncode == 0:
                self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.state_root))
                return True
            from agent_core import repository

            original = repository._is_reparse_alias
            with patch.object(repository, "_is_reparse_alias", side_effect=lambda path: path.name == name or original(path)):
                self.assertCode("FAIL_STATE_REPOSITORY", lambda: resolve_repository_context(self.state_root))
            return False

        state_real = junction("state")
        if state_real:
            (self.repo_root / "state").rmdir()
            (self.repo_root / "state").mkdir()
        junction("engine")

    def test_standalone_old_candidate_behavior(self) -> None:
        standalone_input = Path(self.temp.name) / "standalone-old"
        standalone_input.mkdir()
        git(standalone_input, "init", "-q", "-b", "main")
        git(standalone_input, "config", "user.name", "Test")
        git(standalone_input, "config", "user.email", "test@invalid")
        (standalone_input / "tracked.txt").write_text("state", encoding="utf-8")
        git(standalone_input, "add", ".")
        git(standalone_input, "commit", "-q", "-m", "seed")
        standalone = Path(git(standalone_input, "rev-parse", "--show-toplevel"))
        candidate(standalone, "inbox/desk-20260815T000000Z-" + "a" * 32 + ".md", git(standalone, "rev-parse", "HEAD"))
        self.assertEqual(inspect(standalone, self.root / "control", fetch=False).dirty, ())


if __name__ == "__main__":
    unittest.main()
