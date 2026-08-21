from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core import capture, privacy


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Frozen from 139f40b. These constants intentionally do not read production
# patterns: a boundary rewrite must preserve every legacy match independently.
LEGACY_BOUNDARY_PATTERNS = {
    "absolute_windows_path": r"(?i)(?:^|[\s\"'(])(?:[A-Z]:[\\/][^\s\"'<>|]+)",
    "absolute_unix_path": r"(?:^|[\s\"'(])/(?:Users|home|private|var|tmp)/[^\s\"'<>]+",
    "home_reference": (
        r"(?i)(?:~[/\\]|%" + "USERPROFILE" + r"%|%" + "HOME" + r"%|\$"
        + "HOME" + r"\b|\$\{" + "HOME" + r"\})"
    ),
    "email_address": r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    "credential_token": r"(?i)\b(?:gh[pousr]_[A-Z0-9]{20,}|sk-[A-Z0-9]{20,}|xox[baprs]-[A-Z0-9-]{20,})\b",
    "machine_name": r"(?i)\b(?:DESKTOP-[A-Z0-9-]{4,}|[A-Z0-9][A-Z0-9-]{1,}-MacBook-(?:Air|Pro)(?:-[A-Z0-9-]+)?|[A-Z0-9][A-Z0-9-]{2,}\.local)\b",
    "long_hex": r"(?i)\b[A-F0-9]{40,}\b",
}

LEGACY_BOUNDARY_SEEDS = {
    "absolute_windows_path": "C" + ":/Users/example/private/ledger.md",
    "absolute_unix_path": "/" + "Users/example/private/ledger.md",
    "home_reference": "$" + "HOME",
    "email_address": "owner" + "@" + "example.invalid",
    "credential_token": "ghp_" + "A" * 24,
    "machine_name": "buildbox" + ".local",
    "long_hex": "a" * 40,
}

README_EXCERPT = (
    "Public, deterministic mechanics for a private lessons ledger. "
    "Private state and host bindings stay outside this repository."
)

NORMAL_CONTENT_SAMPLES = (
    "当多个步骤共享同一项定义时，应先确认所有消费者，再更新文档并执行验证。",
    "请更新 docs/PLAN.md 和 tests/test_privacy.py，然后运行本地检查。",
    "The installer preserves existing settings and reports each planned change before writing files.",
    "render_fragment, STATE_CAPTURE_RULES, and result_nonempty are stable interface names.",
    "The route label is " + "route:" + "/" + "home/dashboard, not a filesystem path.",
    "The suffix fixture is " + "owner" + "@" + "example.invalid_suffix.",
)


def run_main(*args: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = privacy.main(list(args))
    return code, output.getvalue()


class PrivacyTreeTests(unittest.TestCase):
    def test_clean_fixture_passes(self) -> None:
        code, output = run_main("--tree", str(FIXTURES / "clean"), "--strict")
        self.assertEqual(code, 0, output)
        self.assertIn("PASS findings=0", output)

    def test_fragment_fixture_reconstructs_all_default_rules(self) -> None:
        payload = json.loads((FIXTURES / "dirty" / "cases.json").read_text(encoding="utf-8"))
        expected = {case["rule_id"] for case in payload["cases"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.txt"
            path.write_text("\n".join("".join(case["parts"]) for case in payload["cases"]), encoding="utf-8")
            code, output = run_main("--tree", str(path), "--strict")
        self.assertEqual(code, 1, output)
        found = {line.split()[1] for line in output.splitlines() if line.startswith("HIT ")}
        self.assertTrue(expected.issubset(found), (expected, found, output))

    def test_allowlist_is_bound_to_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            root = temp_root / "candidate"
            root.mkdir()
            target = root / "sample.txt"
            value = "owner" + "@" + "example.invalid"
            target.write_text(value, encoding="utf-8")
            digest = hashlib.sha256(value.encode()).hexdigest()
            allowlist = temp_root / "allowlist.json"
            allowlist.write_text(json.dumps({
                "schema": "privacy-allowlist/1",
                "exemptions": [{
                    "rule_id": "email_address",
                    "relative_path": "sample.txt",
                    "content_sha256": digest,
                    "reason": "synthetic unit-test value",
                    "expires_utc": "2099-01-01T00:00:00Z"
                }]
            }), encoding="utf-8")
            code, output = run_main("--tree", str(root), "--allowlist", str(allowlist), "--strict")
            self.assertEqual(code, 0, output)
            self.assertIn("EXEMPTIONS 1", output)
            target.write_text(value + " changed", encoding="utf-8")
            code, output = run_main("--tree", str(root), "--allowlist", str(allowlist), "--strict")
            self.assertEqual(code, 1, output)

    def test_binary_and_oversize_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "binary.dat").write_bytes(b"a\x00b")
            (root / "large.txt").write_text("x" * 20, encoding="utf-8")
            code, output = run_main("--tree", str(root), "--max-blob-bytes", "10", "--strict")
        self.assertEqual(code, 1, output)
        self.assertIn("binary_unscanned", output)
        self.assertIn("oversize_unscanned", output)


class PrivacyGitTests(unittest.TestCase):
    def git(self, repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def init_repo(self, repo: Path) -> None:
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Fixture Author")
        self.git(repo, "config", "user.email", "fixture" + "@" + "example.invalid")

    def commit(self, repo: Path, message: str) -> None:
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        self.git(repo, "add", "--all", env=env)
        self.git(repo, "commit", "-m", message, env=env)

    def run_history_cli(
        self, repo: Path, allowlist: Path, *, disable_utf8_mode: bool,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        command = [sys.executable]
        if disable_utf8_mode:
            command.extend(["-X", "utf8=0"])
        command.extend([
            "-m", "agent_core.privacy", "--git-repo", str(repo),
            "--allowlist", str(allowlist), "--strict",
        ])
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            cwd=ROOT,
            env=env,
        )

    def write_git_stub(
        self,
        root: Path,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        clone_stdout: bytes | None = None,
    ) -> Path:
        encoded_stdout = base64.b64encode(stdout).decode("ascii")
        encoded_stderr = base64.b64encode(stderr).decode("ascii")
        encoded_clone = base64.b64encode(
            stdout if clone_stdout is None else clone_stdout
        ).decode("ascii")
        script = (
            "import base64,sys;"
            f"out=base64.b64decode('{encoded_clone}' if 'clone' in sys.argv[1:] else '{encoded_stdout}');"
            f"err=base64.b64decode('{encoded_stderr}');"
            f"sys.stdout.buffer.write(out);sys.stderr.buffer.write(err);sys.exit({exit_code})"
        )
        if os.name == "nt":
            stub = root / "git.cmd"
            stub.write_text(
                "@echo off\r\n" + f'"{sys.executable}" -c "{script}" -- %*\r\n',
                encoding="utf-8",
                newline="",
            )
            return stub
        stub = root / "git"
        stub.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)} \"$@\"\n",
            encoding="utf-8",
        )
        stub.chmod(0o700)
        return stub

    def assert_safe_git_error(self, code: int, output: str, marker: str) -> None:
        self.assertEqual(code, 2, output)
        self.assertEqual(
            output,
            "ERROR Git text output is not valid UTF-8; privacy scan incomplete\n",
        )
        for forbidden in ("Traceback", "PASS", "EXEMPTIONS", marker):
            self.assertNotIn(forbidden, output)

    def test_deleted_secret_is_found_in_reachable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.init_repo(repo)
            secret = repo / "secret.txt"
            secret.write_text("DESKTOP-" + "FIXTURE123", encoding="utf-8")
            self.commit(repo, "fixture: add synthetic value")
            secret.unlink()
            self.commit(repo, "fixture: remove synthetic value")
            code, output = run_main("--git-repo", str(repo), "--strict")
        self.assertEqual(code, 1, output)
        self.assertIn("machine_name", output)

    def test_windows_path_is_not_duplicated_as_unix_path_in_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.init_repo(repo)
            value = "C" + ":/Users/example/private/ledger.md"
            (repo / "synthetic.txt").write_text(value, encoding="utf-8")
            self.commit(repo, "fixture: add synthetic Windows path")
            code, output = run_main("--git-repo", str(repo), "--strict")
        self.assertEqual(code, 1, output)
        hits = [line for line in output.splitlines() if line.startswith("HIT ")]
        self.assertEqual(sum(" absolute_windows_path " in line for line in hits), 1, output)
        self.assertFalse(any(" absolute_unix_path " in line for line in hits), output)

    def test_history_allowlist_is_bound_to_path_and_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.init_repo(repo)
            secret = repo / "secret.txt"
            value = "DESKTOP-" + "FIXTURE123"
            secret.write_text(value, encoding="utf-8")
            self.commit(repo, "fixture: add synthetic value")
            secret.unlink()
            self.commit(repo, "fixture: remove synthetic value")
            allowlist = repo / "allowlist.json"
            allowlist.write_text(json.dumps({
                "schema": "privacy-allowlist/1",
                "exemptions": [{
                    "rule_id": "machine_name",
                    "relative_path": "secret.txt",
                    "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
                    "reason": "synthetic historical unit-test value",
                    "expires_utc": "2099-01-01T00:00:00Z"
                }]
            }), encoding="utf-8")
            code, output = run_main(
                "--git-repo", str(repo), "--allowlist", str(allowlist), "--strict"
            )
            self.assertEqual(code, 0, output)
            self.assertIn("EXEMPTIONS 1", output)

            secret.write_text("DESKTOP-" + "CHANGED123", encoding="utf-8")
            self.commit(repo, "fixture: add changed synthetic value")
            code, output = run_main(
                "--git-repo", str(repo), "--allowlist", str(allowlist), "--strict"
            )
        self.assertEqual(code, 1, output)
        self.assertIn("machine_name", output)

    def test_history_cli_is_encoding_independent_for_utf8_paths_and_allowlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            (repo / "合法-\U0001f680.txt").write_text("clean UTF-8 content", encoding="utf-8")
            secret = repo / "secret.txt"
            value = "DESKTOP-" + "UTF8FIXTURE"
            secret.write_text(value, encoding="utf-8")
            self.commit(repo, "fixture: add UTF-8 path and synthetic private marker")
            secret.unlink()
            self.commit(repo, "fixture: remove synthetic private marker")
            allowlist = root / "allowlist.json"
            allowlist.write_text(json.dumps({
                "schema": "privacy-allowlist/1",
                "exemptions": [{
                    "rule_id": "machine_name",
                    "relative_path": "secret.txt",
                    "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
                    "reason": "synthetic UTF-8 subprocess contract",
                    "expires_utc": "2099-01-01T00:00:00Z",
                }],
            }), encoding="utf-8")
            for quote_path in ("true", "false"):
                with self.subTest(core_quote_path=quote_path):
                    self.git(repo, "config", "core.quotePath", quote_path)
                    for disable_utf8_mode in (False, True):
                        with self.subTest(disable_utf8_mode=disable_utf8_mode):
                            result = self.run_history_cli(
                                repo, allowlist, disable_utf8_mode=disable_utf8_mode,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn("EXEMPTIONS 1", result.stdout)
                            self.assertIn("PASS findings=0", result.stdout)
                            self.assertNotIn("Traceback", result.stdout + result.stderr)
            unicode_repo = root / "unicode-repo"
            unicode_repo.mkdir()
            self.init_repo(unicode_repo)
            unicode_name = "合法-🚀.txt"
            (unicode_repo / unicode_name).write_text(
                "unicode-marker", encoding="utf-8"
            )
            self.commit(unicode_repo, "fixture: add Unicode path marker")
            self.git(unicode_repo, "config", "core.quotePath", "false")
            findings = privacy.scan_git_repo(
                unicode_repo,
                [privacy.Rule("fixture_marker", re.compile("unicode-marker"))],
                privacy.DEFAULT_MAX_BLOB_BYTES,
            )
            self.assertTrue(
                any(unicode_name in finding.path for finding in findings), findings
            )

    def test_invalid_git_text_output_fails_closed_without_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                ("invalid-stdout", b"PRIVATE-INVALID-STDOUT\xff", b"", "PRIVATE-INVALID-STDOUT"),
                ("invalid-stderr", b"valid", b"PRIVATE-INVALID-STDERR\xff", "PRIVATE-INVALID-STDERR"),
                ("invalid-trailer", b"PRIVATE-INVALID-TRAILER\xff", b"", "PRIVATE-INVALID-TRAILER"),
            )
            for label, stdout, stderr, marker in cases:
                with self.subTest(label=label):
                    stub = self.write_git_stub(root, stdout=stdout, stderr=stderr)
                    with mock.patch.object(privacy.shutil, "which", return_value=str(stub)):
                        code, output = run_main("--git-repo", str(root), "--strict")
                    self.assert_safe_git_error(code, output, marker)
            stub = self.write_git_stub(
                root, stdout=b"verified", clone_stdout=b"PRIVATE-CLONE\xff"
            )
            with mock.patch.object(privacy.shutil, "which", return_value=str(stub)):
                code, output = run_main("--bundle", str(root / "fixture.bundle"), "--strict")
            self.assert_safe_git_error(code, output, "PRIVATE-CLONE")

    def test_git_timeout_nonzero_and_binary_blob_preserve_boundaries(self) -> None:
        timeout = subprocess.TimeoutExpired(
            ["PRIVATE-TIMEOUT-CANARY"], 30,
            output=b"PRIVATE-TIMEOUT-STDOUT", stderr=b"PRIVATE-TIMEOUT-STDERR",
        )
        with mock.patch.object(privacy.subprocess, "run", side_effect=timeout):
            code, output = run_main("--git-repo", "PRIVATE-TIMEOUT-ROOT", "--strict")
        self.assertEqual(
            output,
            "ERROR Git text command timed out; privacy scan incomplete\n",
        )
        self.assertEqual(code, 2, output)
        for forbidden in ("Traceback", "PASS", "EXEMPTIONS", "PRIVATE-TIMEOUT"):
            self.assertNotIn(forbidden, output)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nonzero = self.write_git_stub(
                root, stdout=b"ordinary stdout", stderr=b"PRIVATE-NONZERO", exit_code=17,
            )
            with mock.patch.object(privacy.shutil, "which", return_value=str(nonzero)):
                code, output = run_main("--git-repo", str(root), "--strict")
            self.assertEqual(code, 1, output)
            self.assertIn("not_git_repo", output)
            self.assertNotIn("PRIVATE-NONZERO", output)
            blob = self.write_git_stub(root, stdout=b"\xff\x00binary")
            with mock.patch.object(privacy.shutil, "which", return_value=str(blob)):
                self.assertEqual(privacy._git_blob(root, "synthetic"), b"\xff\x00binary")

    def test_identity_contract_requires_one_exact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.init_repo(repo)
            (repo / "README.md").write_text("synthetic", encoding="utf-8")
            self.commit(repo, "fixture: identity")
            contract = repo / "identity.json"
            contract.write_text(json.dumps({
                "schema": "publication-identity/1",
                "name": "Fixture Author",
                "email": "fixture" + "@" + "example.invalid"
            }), encoding="utf-8")
            code, output = run_main("--identity", "--repo", str(repo), "--contract", str(contract), "--strict")
            self.assertEqual(code, 0, output)
            payload = json.loads(contract.read_text(encoding="utf-8"))
            payload["name"] = "Different Author"
            contract.write_text(json.dumps(payload), encoding="utf-8")
            code, output = run_main("--identity", "--repo", str(repo), "--contract", str(contract), "--strict")
        self.assertEqual(code, 1, output)
        self.assertIn("publication_identity_mismatch", output)


class PreCommitHookTests(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def init_repo(self, root: Path, name: str = "repo") -> Path:
        repo = root / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "Fixture Author")
        self.git(repo, "config", "user.email", "fixture" + chr(64) + "example.invalid")
        hooks = repo / ".githooks"
        hooks.mkdir()
        shutil.copy2(ROOT / ".githooks" / "pre-commit", hooks / "pre-commit")
        shutil.copy2(ROOT / ".githooks" / "setup.py", hooks / "setup.py")
        (repo / "clean.txt").write_text("synthetic clean content\n", encoding="utf-8")
        return repo

    def synthetic_external_privacy(self, root: Path) -> tuple[Path, Path]:
        external = root / "external"
        external.mkdir(exist_ok=True)
        rules = external / "rules.json"
        allowlist = external / "allowlist.json"
        rules.write_text('{"schema":"privacy-rules/1","rules":[]}\n', encoding="utf-8")
        allowlist.write_text('{"schema":"privacy-allowlist/1","exemptions":[]}\n', encoding="utf-8")
        return rules, allowlist

    def configure_private_privacy(self, repo: Path) -> tuple[Path, Path]:
        rules, allowlist = self.synthetic_external_privacy(repo.parent)
        result = self.run_setup(
            repo, "install", "--rules", str(rules), "--allowlist", str(allowlist)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return rules, allowlist

    def local_config(self, repo: Path, key: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get", key],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        self.assertEqual(result.returncode, 1, result.stderr)
        return None

    def hook_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        environment["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00Z"
        environment["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        return environment

    def run_hook(self, repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), "hook", "run", "pre-commit"],
            check=False,
            capture_output=True,
            text=True,
            env=self.hook_environment(),
        )

    def commit(self, repo: Path) -> subprocess.CompletedProcess[str]:
        self.git(repo, "add", "--all")
        return subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "fixture"],
            check=False,
            capture_output=True,
            text=True,
            env=self.hook_environment(),
        )

    def run_setup(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(repo / ".githooks" / "setup.py"), *args],
            check=False,
            capture_output=True,
            text=True,
            cwd=repo,
            env=self.hook_environment(),
        )

    def test_fresh_clone_requires_setup_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.init_repo(root, "source")
            self.git(source, "add", "--all")
            self.git(source, "commit", "-qm", "source")
            rules, allowlist = self.synthetic_external_privacy(root)

            clone = root / "ordinary-clone"
            subprocess.run(
                ["git", "clone", "-q", str(source), str(clone)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.git(clone, "config", "user.name", "Fixture Author")
            self.git(clone, "config", "user.email", "fixture" + chr(64) + "example.invalid")
            missing = self.run_setup(clone, "check")
            self.assertEqual(missing.returncode, 2, missing.stderr)
            self.assertIn("core.hooksPath", missing.stderr)

            installed = self.run_setup(
                clone, "install", "--rules", str(rules), "--allowlist", str(allowlist)
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            checked = self.run_setup(clone, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS privacy gate active", checked.stdout)
            self.assertNotIn(str(rules), checked.stdout + checked.stderr)
            self.assertNotIn(str(allowlist), checked.stdout + checked.stderr)
            repeated = self.run_setup(
                clone, "install", "--rules", str(rules), "--allowlist", str(allowlist)
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)

            before = self.git(clone, "rev-list", "--count", "HEAD").stdout.strip()
            (clone / "marker.txt").write_text("DESKTOP-" + "SETUP123\n", encoding="utf-8")
            rejected = self.commit(clone)
            after = self.git(clone, "rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertIn("machine_name", rejected.stdout + rejected.stderr)
        self.assertEqual(after, before)

    def test_guarded_clone_is_active_without_setup_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.init_repo(root, "source")
            self.git(source, "add", "--all")
            self.git(source, "commit", "-qm", "source")
            rules, allowlist = self.synthetic_external_privacy(root)

            clone = root / "guarded-clone"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "-q",
                    "-c",
                    "core.hooksPath=.githooks",
                    "-c",
                    f"privacy.rules={rules.as_posix()}",
                    "-c",
                    f"privacy.allowlist={allowlist.as_posix()}",
                    "-c",
                    f"privacy.python={Path(sys.executable).resolve().as_posix()}",
                    str(source),
                    str(clone),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.git(clone, "config", "user.name", "Fixture Author")
            self.git(clone, "config", "user.email", "fixture" + chr(64) + "example.invalid")
            checked = self.run_setup(clone, "check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS privacy gate active", checked.stdout)

            before = self.git(clone, "rev-list", "--count", "HEAD").stdout.strip()
            (clone / "marker.txt").write_text("DESKTOP-" + "GUARD123\n", encoding="utf-8")
            rejected = self.commit(clone)
            after = self.git(clone, "rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertIn("machine_name", rejected.stdout + rejected.stderr)
        self.assertEqual(after, before)

    def test_setup_rejects_repo_internal_privacy_files_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.init_repo(Path(tmp))
            rules = repo / "rules.json"
            allowlist = repo / "allowlist.json"
            rules.write_text('{"schema":"privacy-rules/1","rules":[]}\n', encoding="utf-8")
            allowlist.write_text('{"schema":"privacy-allowlist/1","exemptions":[]}\n', encoding="utf-8")
            result = self.run_setup(
                repo, "install", "--rules", str(rules), "--allowlist", str(allowlist)
            )
            hooks_path = self.local_config(repo, "core.hooksPath")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("privacy.rules", result.stderr)
        self.assertIsNone(hooks_path)

    def test_setup_rejects_subdirectory_of_a_parent_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.init_repo(root, "parent")
            child = parent / "ordinary-child"
            hooks = child / ".githooks"
            hooks.mkdir(parents=True)
            shutil.copy2(ROOT / ".githooks" / "pre-commit", hooks / "pre-commit")
            shutil.copy2(ROOT / ".githooks" / "setup.py", hooks / "setup.py")
            rules, allowlist = self.synthetic_external_privacy(root)
            installed = self.run_setup(
                child, "install", "--rules", str(rules), "--allowlist", str(allowlist)
            )
            checked = self.run_setup(child, "check")
            parent_hooks_path = self.local_config(parent, "core.hooksPath")
        self.assertEqual(installed.returncode, 2, installed.stderr)
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertIsNone(parent_hooks_path)

    def test_hook_ignores_ambient_privacy_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.init_repo(root)
            rules, allowlist = self.synthetic_external_privacy(root)
            self.git(repo, "config", "--local", "core.hooksPath", ".githooks")
            self.git(repo, "config", "--local", "privacy.python", Path(sys.executable).resolve().as_posix())
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    f"privacy.rules={rules.as_posix()}",
                    "-c",
                    f"privacy.allowlist={allowlist.as_posix()}",
                    "hook",
                    "run",
                    "pre-commit",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self.hook_environment(),
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("privacy.rules", result.stderr)
        self.assertIn("privacy.allowlist", result.stderr)

    def test_pre_commit_rejects_missing_private_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.init_repo(Path(tmp))
            _rules, _allowlist = self.configure_private_privacy(repo)
            self.git(repo, "config", "--local", "--unset", "privacy.rules")
            hook_result = self.run_hook(repo)
            commit_result = self.commit(repo)
        self.assertEqual(hook_result.returncode, 2, hook_result.stderr)
        self.assertIn("privacy.rules", hook_result.stderr)
        self.assertNotEqual(commit_result.returncode, 0, commit_result.stderr)
        self.assertIn("privacy.rules", commit_result.stderr)

    def test_pre_commit_rejects_missing_private_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.init_repo(Path(tmp))
            _rules, _allowlist = self.configure_private_privacy(repo)
            self.git(repo, "config", "--local", "--unset", "privacy.allowlist")
            hook_result = self.run_hook(repo)
            commit_result = self.commit(repo)
        self.assertEqual(hook_result.returncode, 2, hook_result.stderr)
        self.assertIn("privacy.allowlist", hook_result.stderr)
        self.assertNotEqual(commit_result.returncode, 0, commit_result.stderr)
        self.assertIn("privacy.allowlist", commit_result.stderr)

    def test_pre_commit_reports_both_missing_private_configuration_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.init_repo(Path(tmp))
            self.configure_private_privacy(repo)
            self.git(repo, "config", "--local", "--unset", "privacy.rules")
            self.git(repo, "config", "--local", "--unset", "privacy.allowlist")
            hook_result = self.run_hook(repo)
            commit_result = self.commit(repo)
        self.assertEqual(hook_result.returncode, 2, hook_result.stderr)
        self.assertIn("privacy.rules", hook_result.stderr)
        self.assertIn("privacy.allowlist", hook_result.stderr)
        self.assertNotEqual(commit_result.returncode, 0, commit_result.stderr)

    def test_pre_commit_rejects_missing_or_non_regular_private_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for key, value in (("privacy.rules", "missing-rules.json"), ("privacy.allowlist", ".")):
                with self.subTest(key=key, value=value):
                    repo = self.init_repo(root / key.replace(".", "-"))
                    self.configure_private_privacy(repo)
                    self.git(repo, "config", "--local", key, value)
                    hook_result = self.run_hook(repo)
                    commit_result = self.commit(repo)
                    self.assertEqual(hook_result.returncode, 2, hook_result.stderr)
                    self.assertIn(key, hook_result.stderr)
                    self.assertNotEqual(commit_result.returncode, 0, commit_result.stderr)

    def test_pre_commit_allows_clean_tree_with_private_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.init_repo(Path(tmp))
            self.configure_private_privacy(repo)
            result = self.commit(repo)
        self.assertEqual(result.returncode, 0, result.stderr)


class RuleContractTests(unittest.TestCase):
    def test_default_and_capture_rules_accept_normal_content(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(README_EXCERPT, readme)
        samples = (*NORMAL_CONTENT_SAMPLES, README_EXCERPT)
        rule_sets = {
            "default": privacy._default_rules(),
            "state_paths": capture.STATE_CAPTURE_PATH_RULES,
            "state_capture": capture.STATE_CAPTURE_RULES,
            "project_capture": capture.PROJECT_CAPTURE_RULES,
        }
        for set_name, rules in rule_sets.items():
            for sample in samples:
                with self.subTest(set_name=set_name, sample=sample):
                    hits = [rule.rule_id for rule in rules if rule.regex.search(sample)]
                    self.assertEqual(hits, [])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "normal-content.txt"
            path.write_text("\n".join(samples), encoding="utf-8")
            code, output = run_main("--tree", str(path), "--strict")
        self.assertEqual(code, 0, output)
        self.assertIn("EXEMPTIONS 0", output)
        self.assertIn("PASS findings=0", output)

    def test_default_rule_file_matches_builtin_rules(self) -> None:
        file_rules = privacy._load_extra_rules(ROOT / "privacy_rules.default.json")
        builtin_rules = privacy._builtin_default_rules()
        self.assertEqual(
            [(item.rule_id, item.regex.pattern) for item in file_rules],
            [(item.rule_id, item.regex.pattern) for item in builtin_rules],
        )

    def test_capture_reuses_scanner_identity_rule_objects(self) -> None:
        value = "/" + "Users/example/private/notes.md"
        rule_id = privacy.absolute_path_rule(value)
        defaults = {rule.rule_id: rule for rule in privacy._default_rules()}
        self.assertEqual(rule_id, "absolute_unix_path")
        self.assertIsNotNone(defaults[rule_id].regex.search(value))
        shared = {rule.rule_id: rule for rule in privacy.ABSOLUTE_PATH_RULES}
        self.assertIs(defaults[rule_id], shared[rule_id])
        capture_rules = {rule.rule_id: rule for rule in privacy.CAPTURE_ABSOLUTE_PATH_RULES}
        self.assertIs(capture_rules[rule_id], shared[rule_id])

    def test_capture_reuses_scanner_sensitive_rule_objects(self) -> None:
        defaults = {rule.rule_id: rule for rule in privacy._default_rules()}
        shared = {rule.rule_id: rule for rule in privacy.SENSITIVE_IDENTITY_RULES}
        self.assertEqual(set(shared), {"machine_name", "email_address", "credential_token"})
        for rule_id, rule in shared.items():
            with self.subTest(rule_id=rule_id):
                self.assertIs(defaults[rule_id], rule)

    def test_ascii_boundaries_detect_english_and_cjk_adjacent_values(self) -> None:
        cases = (
            ("absolute_windows_path", "C" + ":/Users/example/private/ledger.md"),
            ("absolute_unix_path", "/" + "Users/example/private/ledger.md"),
            ("home_reference", "$" + "HOME"),
            ("machine_name", "DESKTOP-" + "FIXTURE123"),
            ("email_address", "owner" + "@" + "example.invalid"),
            ("credential_token", "ghp_" + "A" * 24),
            ("long_hex", "a" * 40),
        )
        for rule_id, value in cases:
            for text in (f"evidence at {value} do not share", f"证据见{value}，不要外传"):
                with self.subTest(rule_id=rule_id, text=text), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "candidate.txt"
                    path.write_text(text, encoding="utf-8")
                    code, output = run_main("--tree", str(path), "--strict")
                self.assertEqual(code, 1, output)
                self.assertIn(f"HIT {rule_id} ", output)

    def test_ascii_embedding_does_not_create_substring_findings(self) -> None:
        values = (
            "xC" + ":/Users/example/private/ledger.md",
            "x/" + "Users/example/private/ledger.md",
            "xghp_" + "A" * 24,
            "x" + "a" * 40,
            "$" + "HOMEx",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.txt"
            path.write_text("\n".join(values), encoding="utf-8")
            code, output = run_main("--tree", str(path), "--strict")
        self.assertEqual(code, 0, output)
        self.assertIn("PASS findings=0", output)

    def test_default_boundaries_preserve_the_ac54_match_surface(self) -> None:
        current = {rule.rule_id: rule.regex for rule in privacy._default_rules()}
        neighbors = ("", " ", "(", ".", "-", ",", ")", "A", "0", "_", "汉")
        for rule_id, pattern in LEGACY_BOUNDARY_PATTERNS.items():
            legacy = re.compile(pattern)
            seed = LEGACY_BOUNDARY_SEEDS[rule_id]
            for left in neighbors:
                for right in neighbors:
                    text = f"{left}{seed}{right}"
                    if not legacy.search(text):
                        continue
                    with self.subTest(rule_id=rule_id, left=left, right=right):
                        self.assertIsNotNone(current[rule_id].search(text))

    def test_sensitive_rules_keep_common_ascii_trailing_forms(self) -> None:
        cases = (
            ("email_address", "Questions go to owner" + "@" + "example.invalid."),
            ("email_address", "mailto:owner" + "@" + "example.invalid-old"),
            ("credential_token", "Revoked token was " + "ghp_" + "A" * 24 + "-old."),
            ("machine_name", "The box was " + "buildbox" + ".local-old."),
        )
        rules = {rule.rule_id: rule.regex for rule in privacy._default_rules()}
        for rule_id, text in cases:
            with self.subTest(rule_id=rule_id, text=text):
                self.assertIsNotNone(rules[rule_id].search(text))

    def test_scanner_keeps_identity_path_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.txt"
            path.write_text("\n".join((
                "/" + "dev/null",
                "/" + "etc/hosts",
            )), encoding="utf-8")
            code, output = run_main("--tree", str(path), "--strict")
        self.assertEqual(code, 0, output)
        self.assertIn("PASS findings=0", output)

        for value in ("foo(/" + "tmp/x/y)", "~" + "/.claude/LESSONS.md"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "candidate.txt"
                path.write_text(value, encoding="utf-8")
                code, output = run_main("--tree", str(path), "--strict")
            self.assertEqual(code, 1, output)

    def test_capture_detector_is_stricter_than_scanner(self) -> None:
        scanner_rules = privacy._default_rules()
        for value in ("/" + "x", "/" + "opt/x/y.md", "/" + "dev/null"):
            with self.subTest(value=value):
                self.assertEqual(privacy.absolute_path_rule(value), "absolute_posix_path")
                self.assertEqual(
                    privacy.absolute_path_rule(f"证据见{value}，不要外传"),
                    "absolute_posix_path",
                )
                self.assertFalse(any(rule.regex.search(value) for rule in scanner_rules))


if __name__ == "__main__":
    unittest.main()
