#!/usr/bin/env python3
"""Deterministic, dependency-free privacy gate for public candidate trees."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_RULES = "privacy-rules/1"
SCHEMA_ALLOWLIST = "privacy-allowlist/1"
SCHEMA_IDENTITY = "publication-identity/1"
DEFAULT_MAX_BLOB_BYTES = 1_048_576
PRIVATE_KEY_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "private.key",
    "server.key",
}


@dataclass(frozen=True, order=True)
class Finding:
    rule_id: str
    kind: str
    path: str
    line: int = 0

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"HIT {self.rule_id} {self.kind} {location}"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    regex: re.Pattern[str]


def _absolute_path_rules() -> tuple[Rule, ...]:
    """Return identity-bearing path rules shared by scanning and capture."""
    specs = (
        (
            "absolute_windows_path",
            r"(?i)(?<![A-Z0-9_])[A-Z]:[\\/][^\s\"'<>|]+",
        ),
        (
            "absolute_unix_path",
            r"(?<![A-Za-z0-9_:])/(?:Users|home|private|var|tmp)/[^\s\"'<>]+",
        ),
        (
            "home_reference",
            "(?i)(?:" + "~" + r"[/\\]|" + "%" + "USERPROFILE" + "%|" + "%" +
            "HOME" + "%|" + r"\$" + "HOME" + r"(?![A-Z0-9_])|\$\{" + "HOME" + r"\})",
        ),
    )
    return tuple(Rule(rule_id, re.compile(pattern)) for rule_id, pattern in specs)


ABSOLUTE_PATH_RULES = _absolute_path_rules()

# Capture must reject every platform-independent POSIX absolute path, while the
# publication scanner intentionally limits itself to identity-bearing prefixes.
CAPTURE_ABSOLUTE_PATH_RULES = (
    *ABSOLUTE_PATH_RULES,
    Rule(
        "absolute_posix_path",
        re.compile(
            r"(?:(?<![A-Za-z0-9_:])(?<![^\x00-\x7F])/(?!/)"
            r"[^/\s\"'<>|]+(?:/[^/\s\"'<>|]+)*|"
            r"(?<![A-Za-z0-9_:])/(?!/)[A-Za-z0-9._\-]"
            r"[^/\s\"'<>|]*(?:/[^/\s\"'<>|]+)*)"
        ),
    ),
)


def _sensitive_identity_rules() -> tuple[Rule, ...]:
    """Return non-path identity rules shared by scanning and capture."""
    specs = (
        (
            "email_address",
            r"(?i)(?<![A-Z0-9_])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}(?![A-Z0-9_])",
        ),
        (
            "credential_token",
            r"(?i)(?<![A-Z0-9_])(?:gh[pousr]_[A-Z0-9]{20,}|sk-[A-Z0-9]{20,}|xox[baprs]-[A-Z0-9\-]{20,})(?![A-Z0-9_])",
        ),
        (
            "machine_name",
            r"(?i)(?<![A-Z0-9_])(?:DESKTOP-[A-Z0-9\-]{4,}|[A-Z0-9][A-Z0-9\-]{1,}-MacBook-(?:Air|Pro)(?:-[A-Z0-9\-]+)?|[A-Z0-9][A-Z0-9\-]{2,}\.local)(?![A-Z0-9_])",
        ),
    )
    return tuple(Rule(rule_id, re.compile(pattern)) for rule_id, pattern in specs)


SENSITIVE_IDENTITY_RULES = _sensitive_identity_rules()


def absolute_path_rule(text: str) -> str | None:
    """Return the stricter capture path rule id, independent of host OS."""
    return next(
        (rule.rule_id for rule in CAPTURE_ABSOLUTE_PATH_RULES if rule.regex.search(text)),
        None,
    )


def _builtin_default_rules() -> list[Rule]:
    specs = [
        (
            "private_key_content",
            r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----",
        ),
        ("long_hex", r"(?i)(?<![A-Z0-9_])[A-F0-9]{40,}(?![A-Z0-9_])"),
    ]
    return [
        *ABSOLUTE_PATH_RULES,
        *SENSITIVE_IDENTITY_RULES,
        *(Rule(rule_id, re.compile(pattern)) for rule_id, pattern in specs),
    ]


def _default_rules() -> list[Rule]:
    candidate = Path(__file__).resolve().parents[1] / "privacy_rules.default.json"
    if candidate.is_file():
        shared = {
            rule.rule_id: rule
            for rule in (*ABSOLUTE_PATH_RULES, *SENSITIVE_IDENTITY_RULES)
        }
        return [shared.get(rule.rule_id, rule) for rule in _load_extra_rules(candidate)]
    return _builtin_default_rules()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _load_extra_rules(path: Path | None) -> list[Rule]:
    if path is None:
        return []
    payload = _load_json(path)
    if payload.get("schema") != SCHEMA_RULES:
        raise ValueError(f"unsupported rules schema: {payload.get('schema')!r}")
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError("rules must be a list")
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise ValueError("each rule must be an object")
        rule_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            raise ValueError(f"invalid or duplicate rule id: {rule_id!r}")
        seen.add(rule_id)
        flags = 0 if raw.get("case_sensitive", True) else re.IGNORECASE
        if kind == "literal":
            value = raw.get("value")
            if not isinstance(value, str) or not value:
                raise ValueError(f"literal rule {rule_id} needs a non-empty value")
            pattern = re.escape(value)
        elif kind == "regex":
            pattern = raw.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"regex rule {rule_id} needs a non-empty pattern")
        else:
            raise ValueError(f"unsupported rule kind for {rule_id}: {kind!r}")
        try:
            rules.append(Rule(rule_id, re.compile(pattern, flags)))
        except re.error as exc:
            raise ValueError(f"invalid regex for {rule_id}: {exc}") from exc
    return rules


def _parse_expiry(value: str) -> dt.datetime:
    if not value.endswith("Z"):
        raise ValueError("expires_utc must end in Z")
    parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("expires_utc must be timezone-aware")
    return parsed


def _load_allowlist(path: Path | None) -> dict[tuple[str, str, str], dict]:
    if path is None:
        return {}
    payload = _load_json(path)
    if payload.get("schema") != SCHEMA_ALLOWLIST:
        raise ValueError(f"unsupported allowlist schema: {payload.get('schema')!r}")
    raw_items = payload.get("exemptions")
    if not isinstance(raw_items, list):
        raise ValueError("exemptions must be a list")
    now = dt.datetime.now(dt.timezone.utc)
    result: dict[tuple[str, str, str], dict] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("each exemption must be an object")
        required = ("rule_id", "relative_path", "content_sha256", "reason", "expires_utc")
        if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
            raise ValueError("each exemption needs five non-empty string fields")
        digest = raw["content_sha256"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("content_sha256 must be 64 lowercase hex characters")
        expiry = _parse_expiry(raw["expires_utc"])
        if expiry <= now:
            continue
        key = (raw["rule_id"], Path(raw["relative_path"]).as_posix(), digest)
        if key in result:
            raise ValueError(f"duplicate exemption: {key}")
        result[key] = raw
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan_bytes(
    data: bytes,
    display_path: str,
    rules: Sequence[Rule],
    max_blob_bytes: int,
) -> tuple[list[Finding], str | None]:
    findings: list[Finding] = []
    name = Path(display_path).name.lower()
    if name in PRIVATE_KEY_NAMES or name.endswith((".pem", ".p12", ".pfx")):
        findings.append(Finding("private_key_file", "path", display_path))
    if len(data) > max_blob_bytes:
        findings.append(Finding("oversize_unscanned", "oversize", display_path))
        return findings, None
    if b"\x00" in data:
        findings.append(Finding("binary_unscanned", "binary", display_path))
        return findings, None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding("binary_unscanned", "binary", display_path))
        return findings, None
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule in rules:
            if rule.regex.search(line):
                findings.append(Finding(rule.rule_id, "content", display_path, line_number))
    return findings, _sha256(data)


def _apply_allowlist(
    findings: Iterable[Finding],
    digests: dict[str, str],
    allowlist: dict[tuple[str, str, str], dict],
) -> tuple[list[Finding], int]:
    kept: list[Finding] = []
    exemptions = 0
    for finding in findings:
        digest = digests.get(finding.path)
        key = (finding.rule_id, finding.path, digest or "")
        if key in allowlist:
            exemptions += 1
        else:
            kept.append(finding)
    return sorted(set(kept)), exemptions


def _apply_git_allowlist(
    repo: Path,
    findings: Iterable[Finding],
    allowlist: dict[tuple[str, str, str], dict],
) -> tuple[list[Finding], int]:
    """Apply path-and-content-bound exemptions to reachable Git blobs."""
    kept: list[Finding] = []
    exemptions = 0
    digests: dict[str, str] = {}
    for finding in findings:
        if finding.kind != "content" or not finding.path.startswith("git:"):
            kept.append(finding)
            continue
        _, object_id, relative_path = finding.path.split(":", 2)
        digest = digests.get(object_id)
        if digest is None:
            digest = _sha256(_git_blob(repo, object_id))
            digests[object_id] = digest
        key = (finding.rule_id, Path(relative_path).as_posix(), digest)
        if key in allowlist:
            exemptions += 1
        else:
            kept.append(finding)
    return sorted(set(kept)), exemptions


def _iter_tree_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if (
            not path.is_file()
            or ".git" in path.parts
            or "__pycache__" in path.parts
            or ".pytest_cache" in path.parts
            or path.suffix.lower() in {".pyc", ".pyo"}
        ):
            continue
        yield path


def scan_trees(
    roots: Sequence[Path],
    rules: Sequence[Rule],
    allowlist: dict[tuple[str, str, str], dict],
    max_blob_bytes: int,
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    digests: dict[str, str] = {}
    multiple = len(roots) > 1
    for root in roots:
        if not root.exists():
            findings.append(Finding("missing_path", "path", root.as_posix()))
            continue
        for path in _iter_tree_files(root):
            if root.is_file():
                display = root.name
            else:
                relative = path.relative_to(root).as_posix()
                display = f"{root.name}/{relative}" if multiple else relative
            data = path.read_bytes()
            scanned, digest = _scan_bytes(data, display, rules, max_blob_bytes)
            findings.extend(scanned)
            if digest is not None:
                digests[display] = digest
    return _apply_allowlist(findings, digests, allowlist)


GIT_TEXT_DECODE_ERROR = "Git text output is not valid UTF-8; privacy scan incomplete"
GIT_TEXT_TIMEOUT_ERROR = "Git text command timed out; privacy scan incomplete"


def _decode_git_text(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError(GIT_TEXT_DECODE_ERROR) from None


def _run_git_command(
    command: Sequence[str], *, text: bool = True
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(GIT_TEXT_TIMEOUT_ERROR) from None
    if not text:
        return completed
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        _decode_git_text(completed.stdout),
        _decode_git_text(completed.stderr),
    )


def _git(repo: Path, args: Sequence[str], text: bool = True) -> subprocess.CompletedProcess:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("git executable unavailable")
    return _run_git_command([executable, "-C", str(repo), *args], text=text)


def _git_blob(repo: Path, object_id: str) -> bytes:
    completed = _git(repo, ["cat-file", "blob", object_id], text=False)
    if completed.returncode != 0:
        raise ValueError(f"cannot read Git blob {object_id}")
    return completed.stdout


def scan_git_repo(
    repo: Path,
    rules: Sequence[Rule],
    max_blob_bytes: int,
) -> list[Finding]:
    inside = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    bare = _git(repo, ["rev-parse", "--is-bare-repository"])
    if (
        inside.returncode != 0
        or bare.returncode != 0
        or (inside.stdout.strip() != "true" and bare.stdout.strip() != "true")
    ):
        return [Finding("not_git_repo", "git", repo.as_posix())]
    listed = _git(repo, ["rev-list", "--objects", "--all"])
    if listed.returncode != 0:
        return [Finding("git_object_listing_failed", "git", repo.as_posix())]
    findings: list[Finding] = []
    seen_blobs: set[str] = set()
    for raw_line in listed.stdout.splitlines():
        object_id, _, object_path = raw_line.partition(" ")
        object_type = _git(repo, ["cat-file", "-t", object_id])
        if object_type.returncode != 0 or object_type.stdout.strip() != "blob":
            continue
        if object_id in seen_blobs:
            continue
        seen_blobs.add(object_id)
        display = f"git:{object_id}:{Path(object_path or '<unknown>').as_posix()}"
        scanned, _ = _scan_bytes(_git_blob(repo, object_id), display, rules, max_blob_bytes)
        findings.extend(scanned)
    fsck = _git(repo, ["fsck", "--unreachable", "--no-reflogs"])
    fsck_text = (fsck.stdout + "\n" + fsck.stderr).splitlines()
    for line in fsck_text:
        if line.startswith(("unreachable ", "dangling ")):
            parts = line.split()
            object_id = parts[-1] if parts else "unknown"
            findings.append(Finding("unreachable_git_object", "git", f"git:{object_id}"))
    return sorted(set(findings))


def _discover_git_repos(root: Path) -> list[Path]:
    if (root / ".git").exists():
        return [root]
    return sorted(
        (path.parent for path in root.rglob(".git") if path.is_dir()),
        key=lambda item: item.as_posix(),
    )


def scan_git_fixture(root: Path, rules: Sequence[Rule], max_blob_bytes: int) -> list[Finding]:
    repos = _discover_git_repos(root)
    if not repos:
        return [Finding("missing_git_fixture", "git", root.as_posix())]
    findings: list[Finding] = []
    for repo in repos:
        findings.extend(scan_git_repo(repo, rules, max_blob_bytes))
    return sorted(set(findings))


def scan_bundle(
    bundle: Path,
    rules: Sequence[Rule],
    allowlist: dict[tuple[str, str, str], dict],
    max_blob_bytes: int,
) -> tuple[list[Finding], int]:
    executable = shutil.which("git")
    if executable is None:
        return [Finding("git_unavailable", "git", bundle.as_posix())], 0
    verified = _run_git_command([executable, "bundle", "verify", str(bundle)])
    if verified.returncode != 0:
        return [Finding("invalid_bundle", "git", bundle.as_posix())], 0
    with tempfile.TemporaryDirectory(prefix="privacy-bundle-") as tmp:
        cloned = _run_git_command(
            [executable, "clone", "--bare", "--quiet", str(bundle), tmp]
        )
        if cloned.returncode != 0:
            return [Finding("bundle_clone_failed", "git", bundle.as_posix())], 0
        repo = Path(tmp)
        return _apply_git_allowlist(
            repo,
            scan_git_repo(repo, rules, max_blob_bytes),
            allowlist,
        )


def check_identity(repo: Path, contract_path: Path) -> list[Finding]:
    contract = _load_json(contract_path)
    if contract.get("schema") != SCHEMA_IDENTITY:
        raise ValueError(f"unsupported identity schema: {contract.get('schema')!r}")
    expected = (contract.get("name"), contract.get("email"))
    if not all(isinstance(value, str) and value for value in expected):
        raise ValueError("identity contract needs non-empty name and email")
    log = _git(repo, ["log", "--all", "--format=%an%x00%ae"])
    if log.returncode != 0:
        return [Finding("identity_git_log_failed", "identity", repo.as_posix())]
    identities: set[tuple[str, str]] = set()
    for line in log.stdout.splitlines():
        name, separator, email = line.partition("\x00")
        if separator:
            identities.add((name, email))
    findings: list[Finding] = []
    if not identities:
        findings.append(Finding("identity_empty_history", "identity", repo.as_posix()))
    for name, email in sorted(identities):
        if (name, email) != expected:
            digest = _sha256(f"{name}\0{email}".encode("utf-8"))[:12]
            findings.append(Finding("publication_identity_mismatch", "identity", f"identity:{digest}"))
    if len(identities) > 1:
        findings.append(Finding("publication_identity_not_unique", "identity", repo.as_posix()))
    return sorted(set(findings))


def _emit(findings: Sequence[Finding], exemptions: int, strict: bool) -> int:
    for finding in sorted(findings):
        print(finding.render())
    print(f"EXEMPTIONS {exemptions}")
    if findings:
        print(f"FAIL findings={len(findings)}")
        return 1 if strict else 0
    print("PASS findings=0")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tree", nargs="+", type=Path)
    mode.add_argument("--git-fixture", type=Path)
    mode.add_argument("--git-repo", type=Path)
    mode.add_argument("--bundle", type=Path)
    mode.add_argument("--identity", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--max-blob-bytes", type=int, default=DEFAULT_MAX_BLOB_BYTES)
    parser.add_argument("--strict", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.max_blob_bytes <= 0:
            raise ValueError("max-blob-bytes must be positive")
        rules = [*_default_rules(), *_load_extra_rules(args.rules)]
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique across default and external rules")
        exemptions = 0
        if args.tree:
            allowlist = _load_allowlist(args.allowlist)
            findings, exemptions = scan_trees(args.tree, rules, allowlist, args.max_blob_bytes)
        elif args.git_fixture:
            findings = scan_git_fixture(args.git_fixture, rules, args.max_blob_bytes)
        elif args.git_repo:
            allowlist = _load_allowlist(args.allowlist)
            findings, exemptions = _apply_git_allowlist(
                args.git_repo,
                scan_git_repo(args.git_repo, rules, args.max_blob_bytes),
                allowlist,
            )
        elif args.bundle:
            allowlist = _load_allowlist(args.allowlist)
            findings, exemptions = scan_bundle(
                args.bundle, rules, allowlist, args.max_blob_bytes
            )
        else:
            if args.repo is None or args.contract is None:
                parser.error("--identity requires --repo and --contract")
            findings = check_identity(args.repo, args.contract)
        return _emit(findings, exemptions, args.strict)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
