"""Plan-first bootstrap and attachment for the private state repository."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __version__, ledger
from .config import ConfigError, load_config, load_manifest
from .freshness import SHA_RE, is_repository, require_fresh
from .repository import RepositoryContext, _is_reparse_alias, resolve_repository_context


BINDING_SCHEMA_V1 = "state-binding/1"
BINDING_SCHEMA_V2 = "state-binding/2"
BINDING_V1_FIELDS = {
    "schema", "state_root", "remote_name", "remote_url_sha256", "remote_revision",
    "state_lock_sha256", "config_sha256", "confirmed_private_remote",
}
BINDING_V2_FIELDS = BINDING_V1_FIELDS | {"repository_root_sha", "engine_provenance_sha256"}
LOCK_FIELDS = {"engine_version", "engine_source", "schema_version", "pinned_at"}
OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@dataclass(frozen=True)
class BindingEvidence:
    """Privacy-safe identity evidence for an attached state repository."""

    layout: str
    schema: str
    receipt_path: Path
    receipt_sha256: str
    state_root: Path
    remote_url_sha256: str
    remote_revision: str
    repository_root_sha: str | None
    engine_provenance_sha256: str | None
    config_sha256: str
    state_lock_sha256: str


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    resolved = repo.resolve()
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GCM_INTERACTIVE", "Never")
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("FAIL_GIT", str(exc)) from exc


def _required_git(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ConfigError("FAIL_GIT", detail)
    return result.stdout.strip()


def _assert_bootstrap_target(engine_root: Path, target: Path) -> Path:
    if target.is_symlink():
        raise ConfigError("FAIL_PATH", "state target must not be a symbolic link")
    resolved = target.resolve()
    engine = engine_root.resolve()
    if resolved == resolved.parent:
        raise ConfigError("FAIL_PATH", "state target must not be a filesystem root")
    try:
        resolved.relative_to(engine)
    except ValueError:
        pass
    else:
        raise ConfigError("FAIL_PATH", "private state must stay outside the public engine")
    if not resolved.parent.is_dir():
        raise ConfigError("FAIL_PATH", f"state parent does not exist: {resolved.parent}")
    if resolved.exists():
        if not resolved.is_dir():
            raise ConfigError("FAIL_PATH", "state target must be a directory")
        if any(resolved.iterdir()):
            raise ConfigError("FAIL_STATE_NOT_EMPTY", str(resolved))
    return resolved


def _copy_state_seed(engine_root: Path, staging: Path) -> None:
    experience = staging / "experience"
    experience.mkdir()
    shutil.copy2(engine_root / "seed" / "LESSONS.md", experience / "LESSONS.md")
    shutil.copy2(engine_root / "seed" / "CASE_LAW.md", experience / "CASE_LAW.md")
    shutil.copytree(engine_root / "seed" / "profiles", experience / "profiles")

    rules = (engine_root / "examples" / "rules.global.example.md").read_text(encoding="utf-8")
    rules = rules.removeprefix("<!-- RUNTIME_HEAD -->\n")
    (staging / "rules").mkdir()
    (staging / "rules" / "global.md").write_text(rules, encoding="utf-8", newline="\n")
    (staging / "manifest.yaml").write_bytes(_json_bytes({
        "schema": "capability-manifest/1", "capabilities": [],
    }))
    (staging / "agent-core.lock.json").write_bytes(_json_bytes({
        "engine_version": __version__,
        "engine_source": f"local@{__version__}",
        "schema_version": "lessons-ledger/2",
        "pinned_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    }))


def _configure_identity(repo: Path, name: str | None, email: str | None) -> None:
    if (name is None) != (email is None):
        raise ConfigError("FAIL_ARGUMENT", "--git-name and --git-email must be supplied together")
    if name is not None and email is not None:
        if not name.strip() or not email.strip():
            raise ConfigError("FAIL_GIT_IDENTITY", "Git identity values must be non-empty")
        _required_git(repo, "config", "user.name", name)
        _required_git(repo, "config", "user.email", email)
    effective_name = _required_git(repo, "config", "--get", "user.name") if _git(
        repo, "config", "--get", "user.name"
    ).returncode == 0 else ""
    effective_email = _required_git(repo, "config", "--get", "user.email") if _git(
        repo, "config", "--get", "user.email"
    ).returncode == 0 else ""
    if not effective_name or not effective_email:
        raise ConfigError(
            "FAIL_GIT_IDENTITY",
            "supply --git-name and --git-email; they are written only to the new state repo",
        )


def _commit_state(repo: Path) -> str:
    _required_git(repo, "add", ".")
    _required_git(repo, "commit", "-q", "-m", "state: initialize agent-core state")
    sha = _required_git(repo, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(sha):
        raise ConfigError("FAIL_GIT", "initial commit did not produce a valid revision")
    return sha


def plan_init(engine_root: Path, target: Path) -> list[str]:
    resolved = _assert_bootstrap_target(engine_root, target)
    return [
        f"PLAN operation=state-init path={resolved}",
        "PLAN creates=experience,rules,manifest.yaml,agent-core.lock.json,.git",
        "DRY_RUN writes=0",
    ]


def apply_init(
    engine_root: Path,
    target: Path,
    *,
    git_name: str | None = None,
    git_email: str | None = None,
) -> list[str]:
    resolved = _assert_bootstrap_target(engine_root, target)
    staging = Path(tempfile.mkdtemp(prefix=".state-bootstrap-", dir=resolved.parent))
    target_was_present = resolved.exists()
    try:
        _copy_state_seed(engine_root, staging)
        initialized = subprocess.run(
            ["git", "init", "-q", "-b", "main", str(staging)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if initialized.returncode != 0:
            raise ConfigError("FAIL_GIT", initialized.stderr.strip() or "git init failed")
        _configure_identity(staging, git_name, git_email)
        sha = _commit_state(staging)
        if target_was_present:
            resolved.rmdir()
        try:
            os.replace(staging, resolved)
        except Exception:
            if target_was_present and not resolved.exists():
                resolved.mkdir()
            raise
        return [f"APPLIED operation=state-init path={resolved}", f"PASS revision={sha}"]
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("FAIL_STATE_INIT", str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _load_lock(state_root: Path) -> tuple[dict[str, str], bytes]:
    path = state_root / "agent-core.lock.json"
    try:
        content = path.read_bytes()
        payload: Any = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_STATE_LOCK", f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != LOCK_FIELDS:
        raise ConfigError("FAIL_STATE_LOCK", "agent-core.lock.json fields mismatch")
    if payload.get("schema_version") != "lessons-ledger/2":
        raise ConfigError("FAIL_STATE_LOCK", "unsupported ledger schema")
    if payload.get("engine_version") != __version__:
        raise ConfigError(
            "FAIL_ENGINE_VERSION",
            f"state={payload.get('engine_version')} engine={__version__}",
        )
    if not isinstance(payload.get("engine_source"), str) or not payload["engine_source"]:
        raise ConfigError("FAIL_STATE_LOCK", "engine_source must be a non-empty string")
    pinned_at = payload.get("pinned_at")
    try:
        parsed = dt.datetime.fromisoformat(pinned_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ConfigError("FAIL_STATE_LOCK", "pinned_at must be a UTC timestamp") from exc
    if not pinned_at.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
        raise ConfigError("FAIL_STATE_LOCK", "pinned_at must be a UTC timestamp")
    return payload, content


def _validate_state_content(state_root: Path) -> bytes:
    if not is_repository(state_root):
        raise ConfigError("FAIL_STATE_REPOSITORY", str(state_root))
    _payload, lock_content = _load_lock(state_root)
    global_path = state_root / "experience" / "LESSONS.md"
    sources, errors, _warnings = ledger.resolve_sources(str(global_path), all_profiles=True)
    _defined, store_errors, _store_warnings = ledger.validate_sources(sources)
    errors.extend(store_errors)
    if errors:
        raise ConfigError("FAIL_LEDGER", "; ".join(errors))
    load_manifest(state_root / "manifest.yaml", "state")
    if not (state_root / "rules" / "global.md").is_file():
        raise ConfigError("FAIL_STATE_SCHEMA", "missing rules/global.md")
    if not (state_root / "experience" / "CASE_LAW.md").is_file():
        raise ConfigError("FAIL_STATE_SCHEMA", "missing experience/CASE_LAW.md")
    return lock_content


def _read_remote_main(state_root: Path) -> tuple[str, str]:
    origin = _git(state_root, "remote", "get-url", "origin")
    if origin.returncode != 0 or not origin.stdout.strip():
        raise ConfigError("REMOTE_REQUIRED", "state attach requires origin/main")
    remote_url = origin.stdout.strip()
    advertised = _git(state_root, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    if advertised.returncode != 0:
        raise ConfigError("REMOTE_REQUIRED", "state attach requires a fetchable origin/main")
    fields = advertised.stdout.strip().split()
    if not fields or not SHA_RE.fullmatch(fields[0]):
        raise ConfigError("REMOTE_REQUIRED", "origin/main did not advertise a valid revision")
    local = _git(state_root, "rev-parse", "--verify", "origin/main")
    if local.returncode != 0 or not SHA_RE.fullmatch(local.stdout.strip()):
        raise ConfigError("REMOTE_REQUIRED", "the clone has no local origin/main reference")
    return remote_url, fields[0]


def _binding_fail(detail: str, cause: Exception | None = None) -> None:
    if cause is None:
        raise ConfigError("FAIL_STATE_BINDING", detail)
    raise ConfigError("FAIL_STATE_BINDING", detail) from cause


def _ordinary_file(path: Path, label: str, *, single_link: bool = False) -> bytes:
    try:
        if _is_reparse_alias(path) or not path.is_file():
            _binding_fail(f"{label} must be an ordinary file")
        if single_link and path.stat().st_nlink != 1:
            _binding_fail(f"{label} must have one link")
        return path.read_bytes()
    except ConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        _binding_fail(f"{label} unreadable", exc)


def _host_path_outside_repository(path: Path, repo_root: Path, label: str) -> Path:
    """Reject host files that are in, or reach, the tracked repository through an alias."""
    try:
        candidate = Path(path).absolute()
        current = candidate
        while True:
            if _is_reparse_alias(current):
                _binding_fail(f"{label} path alias")
            parent = current.parent
            if parent == current:
                break
            current = parent
        resolved = candidate.resolve()
        repository = Path(repo_root).resolve()
        try:
            resolved.relative_to(repository)
        except ValueError:
            return resolved
        _binding_fail(f"{label} must remain outside the repository")
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        _binding_fail(f"{label} location invalid", exc)


def _binding_git(repo: Path, *args: str) -> str:
    try:
        result = _git(repo, *args)
    except ConfigError as exc:
        _binding_fail("git identity check failed", exc)
    if result.returncode != 0:
        _binding_fail("git identity check failed")
    return result.stdout


def _binding_git_bytes(repo: Path, *args: str) -> bytes:
    resolved = Path(repo).resolve()
    environment = os.environ.copy()
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GCM_INTERACTIVE", "Never")
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), *args],
            check=False, capture_output=True, timeout=15, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _binding_fail("git identity check failed", exc)
    if result.returncode != 0:
        _binding_fail("git identity check failed")
    return result.stdout


def _single_remote_url(state_root: Path, *, push: bool) -> str:
    args = ("remote", "get-url", "--push", "--all", "origin") if push else (
        "remote", "get-url", "--all", "origin",
    )
    values = [line for line in _binding_git(state_root, *args).splitlines() if line]
    if len(values) != 1:
        _binding_fail("origin URL identity is ambiguous")
    return values[0]


def _valid_oid(value: str) -> str:
    candidate = value.strip()
    if OID_RE.fullmatch(candidate) is None:
        _binding_fail("remote revision is invalid")
    return candidate


def _remote_snapshot(context: RepositoryContext) -> tuple[str, str]:
    """Return only a URL digest and advertised main revision; never expose the URL."""
    state_root = context.state_root
    if context.layout == "canonical":
        fetch_url = _single_remote_url(state_root, push=False)
        push_url = _single_remote_url(state_root, push=True)
        if fetch_url != push_url:
            _binding_fail("origin fetch and push identities differ")
        remote_url = fetch_url
    else:
        try:
            remote_url, _ignored = _read_remote_main(state_root)
        except ConfigError as exc:
            _binding_fail("origin/main is unavailable", exc)
    advertised = _binding_git(state_root, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    fields = advertised.strip().split()
    if len(fields) != 2 or fields[1] != "refs/heads/main":
        _binding_fail("origin/main advertisement is invalid")
    revision = _valid_oid(fields[0])
    local = _valid_oid(_binding_git(state_root, "rev-parse", "--verify", "origin/main"))
    if local != revision:
        _binding_fail("local origin/main differs from advertised main")
    return _sha256(remote_url.encode("utf-8")), revision


def _local_remote_identity(context: RepositoryContext) -> str:
    """Return the configured canonical origin identity without contacting it."""
    state_root = context.state_root
    fetch_url = _single_remote_url(state_root, push=False)
    push_url = _single_remote_url(state_root, push=True)
    if fetch_url != push_url:
        _binding_fail("origin fetch and push identities differ")
    return _sha256(fetch_url.encode("utf-8"))


def _require_clean_snapshot(context: RepositoryContext, advertised: str) -> None:
    repo = context.repo_root
    head = _valid_oid(_binding_git(repo, "rev-parse", "--verify", "HEAD"))
    tracked = _valid_oid(_binding_git(repo, "rev-parse", "--verify", "origin/main"))
    status = _binding_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    unmerged = _binding_git(repo, "ls-files", "-u")
    if head != advertised or tracked != advertised or status or unmerged:
        _binding_fail("state is not a clean origin/main snapshot")


def _lineage(context: RepositoryContext, advertised: str) -> str:
    roots = [item for item in _binding_git(
        context.repo_root, "rev-list", "--max-parents=0", advertised,
    ).splitlines() if item]
    if len(roots) != 1:
        _binding_fail("repository lineage is ambiguous")
    root = _valid_oid(roots[0])
    ancestry = _git(context.repo_root, "merge-base", "--is-ancestor", root, advertised)
    if ancestry.returncode != 0:
        _binding_fail("repository lineage is invalid")
    return root


def _advertised_engine_identity(
    context: RepositoryContext, advertised: str, *, expected_tree: str, expected_record_sha256: str,
) -> None:
    """Bind canonical advertised main to the accepted ENGINE tree and root record blob."""
    try:
        tree = _binding_git_bytes(context.repo_root, "rev-parse", f"{advertised}:engine")
        tree_oid = _valid_oid(tree.decode("ascii", "strict").strip())
        if tree_oid != expected_tree:
            _binding_fail("advertised engine tree changed")
        metadata = _binding_git_bytes(
            context.repo_root, "ls-tree", "-z", advertised, "--", "engine.provenance.json",
        )
        if not metadata.endswith(b"\0") or metadata.count(b"\0") != 1:
            _binding_fail("advertised provenance entry invalid")
        entry = metadata[:-1]
        prefix, separator, name = entry.partition(b"\t")
        fields = prefix.split()
        if (
            not separator or name != b"engine.provenance.json" or len(fields) != 3
            or fields[0] != b"100644" or fields[1] != b"blob"
            or OID_RE.fullmatch(fields[2].decode("ascii", "strict")) is None
        ):
            _binding_fail("advertised provenance entry invalid")
        raw = _binding_git_bytes(context.repo_root, "show", f"{advertised}:engine.provenance.json")
        if _sha256(raw) != expected_record_sha256:
            _binding_fail("advertised engine provenance changed")
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        _binding_fail("advertised engine identity invalid", exc)


def _parse_binding(raw: bytes, context: RepositoryContext) -> dict[str, Any]:
    try:
        receipt: Any = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _binding_fail("binding receipt is invalid", exc)
    expected_schema = BINDING_SCHEMA_V2 if context.layout == "canonical" else BINDING_SCHEMA_V1
    expected_fields = BINDING_V2_FIELDS if context.layout == "canonical" else BINDING_V1_FIELDS
    if not isinstance(receipt, dict) or set(receipt) != expected_fields or receipt.get("schema") != expected_schema:
        _binding_fail("binding receipt fields mismatch")
    if receipt.get("state_root") != str(context.state_root) or receipt.get("remote_name") != "origin":
        _binding_fail("binding state identity differs")
    if receipt.get("confirmed_private_remote") is not True:
        _binding_fail("private remote is not confirmed")
    for field in ("remote_url_sha256", "state_lock_sha256", "config_sha256"):
        if not isinstance(receipt.get(field), str) or len(receipt[field]) != 64 or any(
            char not in "0123456789abcdef" for char in receipt[field]
        ):
            _binding_fail("binding digest is invalid")
    _valid_oid(receipt.get("remote_revision") if isinstance(receipt.get("remote_revision"), str) else "")
    if context.layout == "canonical":
        _valid_oid(receipt.get("repository_root_sha") if isinstance(receipt.get("repository_root_sha"), str) else "")
        pin = receipt.get("engine_provenance_sha256")
        if not isinstance(pin, str) or len(pin) != 64 or any(char not in "0123456789abcdef" for char in pin):
            _binding_fail("engine provenance pin is invalid")
    return receipt


def _validate_state_binding_context(
    context: RepositoryContext,
    config_path: Path,
    *,
    receipt_bytes: bytes | None = None,
    require_clean_snapshot: bool = True,
    expected_state_lock_sha256: str | None = None,
    require_remote_observation: bool = True,
    expected_remote_revision: str | None = None,
) -> BindingEvidence:
    """Validate a v1 standalone or v2 canonical attachment with no URL disclosure."""
    try:
        if require_remote_observation and expected_remote_revision is not None:
            _binding_fail("observed binding cannot provide an expected revision")
        if not require_remote_observation and require_clean_snapshot:
            _binding_fail("local binding validation cannot require a clean remote snapshot")
        config_path = _host_path_outside_repository(config_path, context.repo_root, "host config")
        config_bytes = _ordinary_file(config_path, "host config", single_link=True)
        receipt_path = _host_path_outside_repository(
            binding_receipt_path(config_path), context.repo_root, "binding receipt",
        )
        on_disk_receipt = _ordinary_file(receipt_path, "binding receipt", single_link=True)
        raw_receipt = on_disk_receipt if receipt_bytes is None else receipt_bytes
        if not isinstance(raw_receipt, bytes):
            _binding_fail("binding receipt override is invalid")
        config = load_config(config_path)
        if config.get("state_root") != str(context.state_root):
            _binding_fail("host config state_root differs")
        lock_bytes = _ordinary_file(context.state_root / "agent-core.lock.json", "state lock")
        receipt = _parse_binding(raw_receipt, context)
        if receipt["config_sha256"] != _sha256(config_bytes):
            _binding_fail("host config changed after attach")
        lock_sha256 = _sha256(lock_bytes)
        if receipt["state_lock_sha256"] != lock_sha256:
            if expected_state_lock_sha256 != lock_sha256:
                _binding_fail("state lock changed after attach")
        if require_remote_observation:
            remote_url_sha256, advertised = _remote_snapshot(context)
        else:
            if not isinstance(expected_remote_revision, str):
                _binding_fail("local binding validation requires expected revision")
            advertised = _valid_oid(expected_remote_revision)
            remote_url_sha256 = _local_remote_identity(context)
        if receipt["remote_url_sha256"] != remote_url_sha256:
            _binding_fail("remote identity changed")
        if require_clean_snapshot:
            _require_clean_snapshot(context, advertised)
        root_sha: str | None = None
        provenance_sha: str | None = None
        if context.layout == "canonical":
            root_sha = _lineage(context, advertised)
            if receipt["repository_root_sha"] != root_sha:
                _binding_fail("repository lineage changed")
            from .installer import verify_release_manifest
            from .provenance import validate_engine_provenance

            provenance = validate_engine_provenance(context.repo_root / "engine")
            verify_release_manifest(context.repo_root / "engine", context.repo_root / "engine" / "release-manifest.json")
            provenance_sha = provenance.record_sha256
            if receipt["engine_provenance_sha256"] != provenance_sha:
                _binding_fail("engine provenance changed")
            ancestry = _git(
                context.repo_root, "merge-base", "--is-ancestor", receipt["remote_revision"], advertised,
            )
            if ancestry.returncode != 0:
                _binding_fail("advertised revision is not accepted lineage")
            _advertised_engine_identity(
                context, advertised, expected_tree=provenance.engine_tree_oid,
                expected_record_sha256=receipt["engine_provenance_sha256"],
            )
        return BindingEvidence(
            context.layout, receipt["schema"], receipt_path, _sha256(raw_receipt), context.state_root,
            remote_url_sha256, advertised, root_sha, provenance_sha, _sha256(config_bytes), lock_sha256,
        )
    except ConfigError as exc:
        if exc.code == "FAIL_STATE_BINDING":
            raise
        raise ConfigError("FAIL_STATE_BINDING", "binding validation failed") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_STATE_BINDING", "binding validation failed") from exc


def validate_state_binding(
    state_root: Path,
    config_path: Path,
    *,
    receipt_bytes: bytes | None = None,
    require_clean_snapshot: bool = True,
    expected_state_lock_sha256: str | None = None,
    require_remote_observation: bool = True,
    expected_remote_revision: str | None = None,
) -> BindingEvidence:
    """Validate attachment after resolving the repository exactly once."""
    try:
        context = resolve_repository_context(state_root)
    except Exception:
        raise ConfigError("FAIL_STATE_BINDING", "binding validation failed") from None
    return _validate_state_binding_context(
        context, config_path, receipt_bytes=receipt_bytes, require_clean_snapshot=require_clean_snapshot,
        expected_state_lock_sha256=expected_state_lock_sha256,
        require_remote_observation=require_remote_observation, expected_remote_revision=expected_remote_revision,
    )


def _validate_attach(
    state_root: Path,
    config_path: Path,
    *,
    confirm_private_remote: bool,
) -> tuple[RepositoryContext, dict[str, Any], bytes, bytes, str, str, str | None, str | None]:
    if not confirm_private_remote:
        raise ConfigError(
            "PRIVATE_REMOTE_CONFIRMATION_REQUIRED",
            "Git cannot prove visibility; pass --confirm-private-remote only after verifying it",
        )
    try:
        context = resolve_repository_context(state_root)
        lock_content = _validate_state_content(context.state_root)
        config_path = _host_path_outside_repository(config_path, context.repo_root, "host config")
        _host_path_outside_repository(binding_receipt_path(config_path), context.repo_root, "binding receipt")
        config_content = _ordinary_file(config_path, "host config", single_link=True)
        config = load_config(config_path)
        remote_sha256, remote_sha = _remote_snapshot(context)
        _require_clean_snapshot(context, remote_sha)
        root_sha: str | None = None
        provenance_sha: str | None = None
        if context.layout == "canonical":
            root_sha = _lineage(context, remote_sha)
            from .installer import verify_release_manifest
            from .provenance import validate_engine_provenance

            provenance = validate_engine_provenance(context.repo_root / "engine")
            verify_release_manifest(context.repo_root / "engine", context.repo_root / "engine" / "release-manifest.json")
            provenance_sha = provenance.record_sha256
            _advertised_engine_identity(
                context, remote_sha, expected_tree=provenance.engine_tree_oid,
                expected_record_sha256=provenance_sha,
            )
        return context, config, config_content, lock_content, remote_sha256, remote_sha, root_sha, provenance_sha
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError("FAIL_STATE_ATTACH", "attach preflight failed") from exc


def binding_receipt_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + ".state-binding.json")


def plan_attach(
    state_root: Path,
    config_path: Path,
    *,
    confirm_private_remote: bool,
) -> list[str]:
    context, _config, _config_content, _lock, _remote, remote_sha, _root, _provenance = _validate_attach(
        state_root, config_path, confirm_private_remote=confirm_private_remote,
    )
    config_path = _host_path_outside_repository(config_path, context.repo_root, "host config")
    return [
        f"PLAN operation=state-attach path={context.state_root}",
        f"PLAN config={config_path.resolve()}",
        f"EXPECTED_REMOTE_SHA {remote_sha}",
        "DRY_RUN writes=0",
    ]


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_attach(
    state_root: Path,
    config_path: Path,
    *,
    confirm_private_remote: bool,
) -> list[str]:
    context, config, previous_config, lock_content, remote_sha256, advertised_sha, root_sha, provenance_sha = _validate_attach(
        state_root, config_path, confirm_private_remote=confirm_private_remote,
    )
    config_path = _host_path_outside_repository(config_path, context.repo_root, "host config")
    receipt_path = _host_path_outside_repository(
        binding_receipt_path(config_path), context.repo_root, "binding receipt",
    )
    remote_state_path = _host_path_outside_repository(
        config_path.parent / "remote-state.json", context.repo_root, "remote state",
    )
    previous_remote_state = (
        _ordinary_file(remote_state_path, "remote state", single_link=True)
        if os.path.lexists(remote_state_path)
        else None
    )
    fetched = _git(context.state_root, "fetch", "origin", "--quiet")
    if fetched.returncode != 0:
        raise ConfigError("REMOTE_REQUIRED", "state attach requires a fetchable origin/main")
    refreshed = _validate_attach(
        context.state_root, config_path, confirm_private_remote=confirm_private_remote,
    )
    refreshed_context, _fresh_config, fresh_config_content, fresh_lock_content, fresh_url_sha256, fresh_sha, fresh_root_sha, fresh_provenance_sha = refreshed
    if (
        refreshed_context != context or fresh_url_sha256 != remote_sha256
        or fresh_sha != advertised_sha or fresh_root_sha != root_sha or fresh_provenance_sha != provenance_sha
        or fresh_config_content != previous_config or fresh_lock_content != lock_content
    ):
        raise ConfigError("FAIL_REMOTE_RACE", "attachment identity changed during fetch")
    remote_sha = fresh_sha
    config["state_root"] = str(context.state_root)
    rendered_config = _json_bytes(config)
    previous_receipt = receipt_path.read_bytes() if receipt_path.is_file() else None
    receipt = _json_bytes({
        "schema": BINDING_SCHEMA_V2 if context.layout == "canonical" else BINDING_SCHEMA_V1,
        "state_root": str(context.state_root),
        "remote_name": "origin",
        "remote_url_sha256": remote_sha256,
        "remote_revision": remote_sha,
        "state_lock_sha256": _sha256(lock_content),
        "config_sha256": _sha256(rendered_config),
        "confirmed_private_remote": True,
    })
    if context.layout == "canonical":
        payload = json.loads(receipt)
        payload["repository_root_sha"] = root_sha
        payload["engine_provenance_sha256"] = provenance_sha
        receipt = _json_bytes(payload)
    remote_state = _json_bytes({"last_known_good": remote_sha})
    try:
        _atomic_write(config_path, rendered_config)
        _atomic_write(receipt_path, receipt)
        _atomic_write(remote_state_path, remote_state)
        _ordinary_file(config_path, "host config", single_link=True)
        _ordinary_file(receipt_path, "binding receipt", single_link=True)
        _ordinary_file(remote_state_path, "remote state", single_link=True)
    except Exception as exc:
        try:
            if previous_remote_state is None:
                remote_state_path.unlink(missing_ok=True)
            else:
                _atomic_write(remote_state_path, previous_remote_state)
            if previous_receipt is None:
                receipt_path.unlink(missing_ok=True)
            else:
                _atomic_write(receipt_path, previous_receipt)
            _atomic_write(config_path, previous_config)
        except Exception as rollback_exc:
            raise ConfigError("FAIL_ATTACH_ROLLBACK", str(rollback_exc)) from rollback_exc
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("FAIL_STATE_ATTACH", str(exc)) from exc
    return [
        f"APPLIED operation=state-attach config={config_path.resolve()}",
        f"PASS revision={remote_sha} receipt={receipt_path.resolve()}",
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core state")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--path", type=Path, required=True)
    init.add_argument("--apply", action="store_true")
    init.add_argument("--git-name")
    init.add_argument("--git-email")
    attach = commands.add_parser("attach")
    attach.add_argument("--path", type=Path, required=True)
    attach.add_argument("--config", type=Path, required=True)
    attach.add_argument("--confirm-private-remote", action="store_true")
    attach.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, engine_root: Path | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    root = (engine_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        if args.command == "init":
            if not args.apply and (args.git_name is not None or args.git_email is not None):
                raise ConfigError("FAIL_ARGUMENT", "Git identity flags require --apply")
            lines = apply_init(
                root, args.path, git_name=args.git_name, git_email=args.git_email,
            ) if args.apply else plan_init(root, args.path)
        else:
            lines = apply_attach(
                args.path, args.config,
                confirm_private_remote=args.confirm_private_remote,
            ) if args.apply else plan_attach(
                args.path, args.config,
                confirm_private_remote=args.confirm_private_remote,
            )
        print(*lines, sep="\n")
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
