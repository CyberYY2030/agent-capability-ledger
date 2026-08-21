"""Local composition and runtime capability checks."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import ConfigError, assert_capability_sources, compose_manifests, load_config
from .freshness import is_repository, record_remote_head, require_fresh
from .privacy import DEFAULT_MAX_BLOB_BYTES, SENSITIVE_IDENTITY_RULES, scan_trees
from .provenance import EngineLayout, classify_engine_layout, validate_engine_provenance
from .runtime_config import runtime_hook_path


HOOK_MARKER = "# agent-core-lessons-hook/1"
HOOK_HEARTBEAT = ".lessons-hook-heartbeat.json"
STATE_FORBIDDEN_ENGINE_DIRS = {"agent_core", "runtimes", "seed"}
STATE_FORBIDDEN_ENGINE_FILES = {
    "install.ps1", "install.sh", "privacy_rules.default.json", "release-manifest.json",
}
STATE_ENGINE_SIGNATURES = (
    ("agent_core", "cli.py"),
    ("agent_core", "installer.py"),
    ("enforcement", "verifiers.json"),
    ("runtimes", "generic", "user_prompt.sh"),
    ("seed", "CASE_LAW.md"),
)
STATE_ALLOWED_SIGNATURES = {("enforcement", "verifiers.json")}


def _installed_artifact_line(engine_root: Path, config_path: Path) -> str | None:
    root = Path(os.path.abspath(engine_root))
    install_root = root.parent.parent
    pin_path = install_root / "engine-pin.json"
    installed_shape = root.parent.name == "engine"
    if not installed_shape and not os.path.lexists(pin_path):
        return None

    def is_alias(path: Path) -> bool:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or (callable(junction) and junction())

    try:
        manifest_path = root / "release-manifest.json"
        if (
            not installed_shape
            or any(is_alias(path) for path in (install_root, root.parent, root, pin_path, manifest_path))
            or not root.is_dir()
            or not pin_path.is_file()
            or not manifest_path.is_file()
        ):
            raise ValueError("installed artifact shape invalid")
        pin = json.loads(pin_path.read_bytes())
        if (
            not isinstance(pin, dict)
            or set(pin) != {"schema", "version", "artifact_sha256", "config_path"}
            or pin.get("schema") != "engine-pin/1"
            or not isinstance(pin.get("version"), str)
            or not pin["version"]
            or not isinstance(pin.get("artifact_sha256"), str)
            or not isinstance(pin.get("config_path"), str)
        ):
            raise ValueError("installed pin invalid")
        pinned_config = Path(pin["config_path"])
        if (
            root.name != pin["version"]
            or not pinned_config.is_absolute()
            or pinned_config.resolve() != config_path.expanduser().resolve()
        ):
            raise ValueError("installed pin binding invalid")
        from .installer import verify_release_manifest

        artifact = verify_release_manifest(
            root, manifest_path, expected_version=pin["version"],
        )
        if artifact.artifact_sha256 != pin["artifact_sha256"]:
            raise ValueError("installed artifact digest invalid")
    except (ConfigError, OSError, UnicodeError, ValueError):
        raise ConfigError("FAIL_INSTALLED_ARTIFACT", "verification failed") from None
    return (
        f"PASS installed_artifact version={artifact.version} "
        f"artifact_sha256={artifact.artifact_sha256} pin=verified"
    )


def hook_retrieval_status(script: Path) -> tuple[str, str]:
    if not script.is_file():
        raise ConfigError("FAIL_HOOK_MISSING", str(script))
    try:
        content = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("FAIL_HOOK_READ", f"{script}: {exc}") from exc
    if HOOK_MARKER not in content or "lessons hook" not in content:
        raise ConfigError("FAIL_RETRIEVAL_DISCONNECTED", str(script))
    heartbeat = script.parent / HOOK_HEARTBEAT
    if not heartbeat.is_file():
        return "WARN", "retrieval_connected_unobserved"
    try:
        payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_HOOK_HEARTBEAT", str(exc)) from exc
    expected_fields = {
        "schema", "runtime", "stage", "status", "retrieval_invoked", "result_nonempty",
        "validation_ran", "source_mtime_sha256", "hook_sha256", "observed_utc",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ConfigError("FAIL_HOOK_HEARTBEAT", "fields mismatch")
    if payload.get("schema") != "lessons-hook-heartbeat/1" or payload.get("retrieval_invoked") is not True:
        raise ConfigError("FAIL_HOOK_HEARTBEAT", "retrieval invocation missing")
    if (
        payload.get("runtime") not in {"claude-code", "codex"}
        or payload.get("stage") not in {"prompt", "pretool", "completion"}
        or payload.get("status") not in {"pass", "warning"}
        or not isinstance(payload.get("result_nonempty"), bool)
        or not isinstance(payload.get("validation_ran"), bool)
        or not isinstance(payload.get("observed_utc"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload.get("hook_sha256") or "") is None
    ):
        raise ConfigError("FAIL_HOOK_HEARTBEAT", "value contract mismatch")
    source_signature = payload.get("source_mtime_sha256")
    if source_signature is not None and re.fullmatch(r"[0-9a-f]{64}", source_signature) is None:
        raise ConfigError("FAIL_HOOK_HEARTBEAT", "source signature mismatch")
    if payload.get("status") == "pass" and source_signature is None:
        raise ConfigError("FAIL_HOOK_HEARTBEAT", "pass missing source signature")
    actual_hash = hashlib.sha256(script.read_bytes()).hexdigest()
    if payload.get("hook_sha256") != actual_hash:
        return "WARN", "retrieval_connected_current_version_unobserved"
    if payload.get("status") == "pass" and payload.get("result_nonempty") is True:
        return "PASS", f"retrieval_nonempty stage={payload.get('stage')}"
    if payload.get("status") == "warning":
        return "WARN", f"retrieval_warning stage={payload.get('stage')}"
    return "WARN", f"retrieval_empty stage={payload.get('stage')}"


def check_remote_parity(state_root: Path, control_root: Path | None = None) -> str:
    """Fetch and prove that a versioned state checkout matches origin/main."""
    root = control_root or Path.home() / ".agent-core"
    try:
        state = require_fresh(state_root, "doctor", root)
    except ConfigError as exc:
        if exc.code == "FAIL_REMOTE_PARITY":
            raise
        raise ConfigError("FAIL_REMOTE_PARITY", str(exc)) from exc
    remote = state.remote or ""
    record_remote_head(root, remote)
    return remote


def _remote_identity(url: str) -> tuple[str, str, str] | None:
    """Return a credential-free hosted repository identity for a Git URL."""
    normalized = url.strip()
    if not normalized:
        return None
    try:
        if "://" in normalized:
            parsed = urlsplit(normalized)
            host = parsed.hostname
            path = unquote(parsed.path)
        else:
            if re.match(r"^[A-Za-z]:[\\/]", normalized):
                return None
            match = re.fullmatch(
                r"(?:[^@/\s:]+@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)", normalized,
            )
            if match is None:
                return None
            host = match.group("host")
            path = match.group("path")
    except ValueError:
        return None
    parts = [part for part in path.replace("\\", "/").strip("/").split("/") if part]
    if not host or len(parts) < 2:
        return None
    repository = parts[-1]
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not repository:
        return None
    owner = "/".join(parts[:-1])
    return host.casefold(), owner.casefold(), repository.casefold()


def _remote_identities(repo: Path, role: str) -> tuple[set[tuple[str, str, str]], str | None]:
    if not is_repository(repo):
        return set(), "not_repository"
    resolved = repo.resolve()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "-C", str(resolved), "remote", "-v"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if result.returncode != 0:
        raise ConfigError("FAIL_GIT_REMOTE_READ", role)
    urls = {
        fields[1]
        for line in result.stdout.splitlines()
        if len(fields := line.split()) >= 2
    }
    if not urls:
        return set(), "remote_missing"
    identities: set[tuple[str, str, str]] = set()
    for url in urls:
        identity = _remote_identity(url)
        if identity is None:
            return identities, "remote_unparseable"
        identities.add(identity)
    return identities, None


def _render_remote_identities(identities: set[tuple[str, str, str]]) -> str:
    return ", ".join(f"({host}, {owner}, {repository})" for host, owner, repository in sorted(identities))


def assert_remote_role(
    engine_root: Path, state_root: Path | None, *, require_versioned: bool = False,
) -> list[str]:
    """Prove engine and state remotes identify different hosted repositories."""
    engine_identities, engine_status = _remote_identities(engine_root, "engine")
    state_identities, state_status = (
        _remote_identities(state_root, "state") if state_root is not None else (set(), "missing")
    )
    overlap = engine_identities & state_identities
    if overlap:
        raise ConfigError("FAIL_ENGINE_STATE_REMOTE_OVERLAP", _render_remote_identities(overlap))
    if engine_status is not None:
        if require_versioned:
            if engine_status == "not_repository":
                raise ConfigError("FAIL_ENGINE_REPOSITORY", "engine")
            raise ConfigError("FAIL_ENGINE_REMOTE_UNVERIFIED", engine_status)
        return [f"UNVERIFIED remote_role=engine_{engine_status}"]
    if state_status is not None:
        return [f"UNVERIFIED remote_role=state_{state_status}"]
    return [
        "PASS remote_role=verified "
        f"engine={_render_remote_identities(engine_identities)} "
        f"state={_render_remote_identities(state_identities)}",
    ]


def assert_repository_separation(engine_root: Path, state_root: Path | None) -> None:
    """Reject engine payloads in state and known host labels or unsafe files in engine."""
    engine_root = engine_root.resolve()
    machine_rule = next(
        rule for rule in SENSITIVE_IDENTITY_RULES if rule.rule_id == "machine_name"
    )
    findings, _exemptions = scan_trees(
        [engine_root], [machine_rule], {}, DEFAULT_MAX_BLOB_BYTES,
    )
    if findings:
        first = findings[0]
        if first.rule_id != "machine_name":
            raise ConfigError(
                "FAIL_ENGINE_PUBLIC_SCAN",
                first.render(),
            )
        raise ConfigError(
            "FAIL_ENGINE_KNOWN_HOST_LABEL",
            f"{first.path}:{first.line}",
        )
    if state_root is None:
        return
    state_root = state_root.resolve()
    if not state_root.is_dir():
        return
    for name in sorted(STATE_FORBIDDEN_ENGINE_DIRS | STATE_FORBIDDEN_ENGINE_FILES):
        candidate = state_root / name
        if candidate.exists() or candidate.is_symlink():
            raise ConfigError("FAIL_STATE_CONTAINS_ENGINE", name)
    for candidate in sorted(state_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(state_root)
        if ".git" in relative.parts:
            continue
        parts = relative.parts
        for signature in STATE_ENGINE_SIGNATURES:
            if len(parts) < len(signature) or tuple(parts[-len(signature):]) != signature:
                continue
            if tuple(parts) in STATE_ALLOWED_SIGNATURES:
                break
            raise ConfigError("FAIL_STATE_CONTAINS_ENGINE", relative.as_posix())


def run(engine_root: Path, config_path: Path, state_root: Path | None, state_manifest: Path | None,
        *, require_versioned: bool = False) -> list[str]:
    provenance = None
    installed_line = _installed_artifact_line(engine_root, config_path)
    if installed_line is None:
        layout = classify_engine_layout(engine_root)
        if layout is EngineLayout.CANONICAL:
            from .installer import verify_release_manifest

            provenance = validate_engine_provenance(engine_root)
            verify_release_manifest(engine_root, engine_root / "release-manifest.json")
    assert_repository_separation(engine_root, state_root)
    remote_role_lines = [] if installed_line is not None else assert_remote_role(
        engine_root, state_root, require_versioned=require_versioned,
    )
    if state_root is not None:
        if require_versioned and not is_repository(state_root):
            raise ConfigError("FAIL_STATE_REPOSITORY", str(state_root))
    config = load_config(config_path)
    composition = compose_manifests(engine_root / "manifest.yaml", state_manifest, config)
    assert_capability_sources(composition, engine_root, state_root)
    lines = [
        f"PASS interpreter={Path(sys.executable).name} version={platform.python_version()}",
        f"PASS composition_hash={composition.composition_hash}",
    ]
    if provenance is not None:
        lines.append(
            f"PASS engine_provenance sequence={provenance.sequence} record={provenance.record_sha256}"
        )
    if installed_line is not None:
        lines.append(installed_line)
    lines.extend(remote_role_lines)
    if state_root is not None and is_repository(state_root):
        remote = check_remote_parity(state_root)
        lines.append(f"PASS git_remote_parity={remote}")
    for target in config["targets"]:
        active_skills = [
            item for item in composition.capabilities
            if item["kind"] == "skill" and item["state"] == "active" and target["runtime"] in item["runtimes"]
        ]
        for skill in active_skills:
            raw_root = target["root"]
            if raw_root.startswith("<") and raw_root.endswith(">"):
                raise ConfigError("FAIL_TARGET_UNBOUND", target["id"])
            installed = Path(raw_root).expanduser().resolve() / target["skills_root"] / Path(skill["source"]).name / "SKILL.md"
            if not installed.is_file():
                raise ConfigError("FAIL_CONSUMER_MISSING", f"{target['id']}:{skill['id']}")
            lines.append(f"PASS consumer target={target['id']} capability={skill['id']}")
        raw_root = target["root"]
        if raw_root.startswith("<") and raw_root.endswith(">"):
            raise ConfigError("FAIL_TARGET_UNBOUND", target["id"])
        script = runtime_hook_path(
            Path(raw_root).expanduser().resolve() / target["hook_target"]
        )
        level, detail = hook_retrieval_status(script)
        lines.append(f"{level} lessons_hook target={target['id']} {detail}")
    return lines
