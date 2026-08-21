"""Executable documentation contract with hermetic, argv-only verification."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import ConfigError
from .state import apply_init


SCHEMA = "commands/2"
TOKEN_RE = re.compile(r"^\{\{([a-z][a-z0-9_]*)\}\}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SHA40_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
SHA64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
UUID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
ARTIFACT_RE = re.compile(r"(?<=artifact_sha256=)[A-Za-z0-9_-]{43}")
ROLLBACK_RE = re.compile(r"(?<=rollback=)[0-9]{8}T[0-9]{12}Z")
LESSON_RE = re.compile(r"(?<![A-Z0-9_-])L-[0-9]+(?![A-Z0-9_-])")
MARKER_RE = re.compile(
    r"(?ms)^(?P<start><!-- COMMANDS:(?P<section>[a-z0-9_-]+):START -->)$.*?"
    r"^(?P<end><!-- COMMANDS:(?P=section):END -->)$"
)
BASE_DIFF_RE = re.compile(r"(?m)^BASE_DIFF .+$")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    stdout: str
    normalized_stdout: str
    entrypoint: str


@dataclass(frozen=True)
class Fixture:
    root: Path
    environment: dict[str, str]
    tokens: dict[str, str]


def _git(repo: Path, *args: str, environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=environment,
    )
    if completed.returncode != 0:
        raise ConfigError("FAIL_DOCS_FIXTURE", completed.stderr.strip() or "git failed")
    return completed.stdout.strip()


@contextmanager
def _temporary_environment(environment: dict[str, str]):
    previous = {name: os.environ.get(name) for name in environment}
    os.environ.update(environment)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _fixture(engine_root: Path, root: Path) -> Fixture:
    environment = os.environ.copy()
    environment.update({
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "LOCALAPPDATA": str(root / "local-data"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(engine_root),
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_CONFIG_GLOBAL": str(root / "git-global.config"),
        "GIT_CONFIG_SYSTEM": str(root / "git-system.config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "AGENT_CORE_PYTHON": sys.executable,
    })
    Path(environment["GIT_CONFIG_GLOBAL"]).write_text("", encoding="utf-8")
    Path(environment["GIT_CONFIG_SYSTEM"]).write_text("", encoding="utf-8")
    state_source = root / "state-source"
    with _temporary_environment(environment):
        apply_init(
            engine_root, state_source,
            git_name="Synthetic Documentation", git_email="docs@invalid",
        )
    remote = root / "state-remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)], check=True, timeout=30,
        env=environment,
    )
    _git(state_source, "remote", "add", "origin", str(remote), environment=environment)
    _git(state_source, "push", "-q", "-u", "origin", "main", environment=environment)
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True, timeout=30, env=environment,
    )
    state = root / "state"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(state)], check=True, timeout=30,
        env=environment,
    )
    _git(state, "config", "user.name", "Synthetic Documentation", environment=environment)
    _git(state, "config", "user.email", "docs@invalid", environment=environment)

    config = json.loads((engine_root / "examples" / "host.example.json").read_text(encoding="utf-8"))
    config["backup_root"] = str(root / "host" / "backups")
    for index, target in enumerate(config["targets"]):
        target["root"] = str(root / "runtimes" / f"runtime-{index}")
    config_path = root / "host" / "host.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    tokens = {
        "python": sys.executable,
        "engine": str(engine_root),
        "fixture_root": str(root),
        "state": str(state),
        "ledger": str(state / "experience" / "LESSONS.md"),
        "config": str(config_path),
        "host_data": str(config_path.parent),
        "control": str(root / "control"),
        "init_state": str(root / "first-state"),
        "manifest": str(engine_root / "release-manifest.json"),
        "git_name": "Synthetic Documentation",
        "git_email": "docs@invalid",
    }
    return Fixture(root, environment, tokens)


def _load_commands(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_DOCS_SCHEMA", str(exc)) from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "steps"}:
        raise ConfigError("FAIL_DOCS_SCHEMA", "root fields mismatch")
    if payload.get("schema") != SCHEMA or not isinstance(payload.get("steps"), list):
        raise ConfigError("FAIL_DOCS_SCHEMA", f"schema must be {SCHEMA}")
    expected = {
        "id", "section", "argv", "cwd_ref", "expect_exit", "expect_regex",
        "capture", "example_output", "omit",
    }
    seen: set[str] = set()
    for step in payload["steps"]:
        if not isinstance(step, dict) or set(step) != expected:
            raise ConfigError("FAIL_DOCS_SCHEMA", "step fields mismatch")
        command_id = step.get("id")
        if not isinstance(command_id, str) or not NAME_RE.fullmatch(command_id) or command_id in seen:
            raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid command id: {command_id!r}")
        seen.add(command_id)
        if step.get("section") not in {"quickstart", "lifecycle"} or step.get("cwd_ref") != "engine":
            raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid routing: {command_id}")
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid argv: {command_id}")
        for item in argv:
            if ("{{" in item or "}}" in item) and TOKEN_RE.fullmatch(item) is None:
                raise ConfigError("FAIL_DOCS_SCHEMA", f"token must occupy one argv item: {command_id}")
        if not isinstance(step.get("expect_exit"), int) or not isinstance(step.get("expect_regex"), str):
            raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid expectation: {command_id}")
        try:
            re.compile(step["expect_regex"])
        except re.error as exc:
            raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid expect regex: {exc}") from exc
        capture = step.get("capture")
        if not isinstance(capture, dict):
            raise ConfigError("FAIL_DOCS_SCHEMA", f"capture must be an object: {command_id}")
        for name, pattern in capture.items():
            if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(pattern, str):
                raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid capture: {command_id}")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid capture regex: {exc}") from exc
            if compiled.groups != 1:
                raise ConfigError("FAIL_DOCS_SCHEMA", f"capture needs exactly one group: {name}")
        examples = step.get("example_output")
        if not isinstance(examples, list) or not examples or any(
            not isinstance(item, str) or not item for item in examples
        ):
            raise ConfigError("FAIL_DOCS_SCHEMA", f"example_output is required: {command_id}")
        omitted = step.get("omit")
        if not isinstance(omitted, list):
            raise ConfigError("FAIL_DOCS_SCHEMA", f"omit must be a list: {command_id}")
        for item in omitted:
            if (
                not isinstance(item, dict)
                or set(item) != {"line", "reason"}
                or not isinstance(item.get("line"), str)
                or not item["line"]
                or not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
            ):
                raise ConfigError("FAIL_DOCS_SCHEMA", f"invalid omit entry: {command_id}")
    return payload["steps"]


def _resolve_argv(argv: list[str], tokens: dict[str, str]) -> list[str]:
    result: list[str] = []
    for item in argv:
        match = TOKEN_RE.fullmatch(item)
        if match is None:
            result.append(item)
            continue
        name = match.group(1)
        if name not in tokens:
            raise ConfigError("FAIL_DOCS_TOKEN", name)
        result.append(tokens[name])
    return result


def _normalize(text: str, tokens: dict[str, str]) -> str:
    value = text.replace("\r\n", "\n").strip()
    path_tokens = [
        (name, raw) for name, raw in tokens.items()
        if name not in {"python"} and ("/" in raw or "\\" in raw)
    ]
    for name, raw in sorted(path_tokens, key=lambda item: len(item[1]), reverse=True):
        for form in {raw, raw.replace("\\", "/"), raw.replace("/", "\\")}:
            value = value.replace(form, f"<{name.upper()}>")
    for name, raw in sorted(tokens.items(), key=lambda item: len(item[1]), reverse=True):
        if name.endswith("candidate") or name.endswith("plan_hash") or name.endswith("remote_sha"):
            value = value.replace(raw, f"<{name.upper()}>")
    value = BASE_DIFF_RE.sub("BASE_DIFF <CANDIDATE_DIFF>", value)
    value = SHA64_RE.sub("<SHA256>", value)
    value = SHA40_RE.sub("<SHA>", value)
    value = UUID_RE.sub("<ID>", value)
    value = ARTIFACT_RE.sub("<ARTIFACT_SHA256>", value)
    value = ROLLBACK_RE.sub("<ROLLBACK_ID>", value)
    value = LESSON_RE.sub("<LESSON_ID>", value)
    return value.replace("\\", "/")


def verify_commands(
    engine_root: Path,
    commands_path: Path,
    *,
    lifecycle_via_launcher: bool = False,
) -> tuple[CommandResult, ...]:
    steps = _load_commands(commands_path)
    results: list[CommandResult] = []
    with tempfile.TemporaryDirectory(prefix="agent-core-docs-") as temporary:
        fixture = _fixture(engine_root.resolve(), Path(temporary).resolve())
        tokens = dict(fixture.tokens)
        installed = False
        for step in steps:
            argv = _resolve_argv(step["argv"], tokens)
            entrypoint = "source-module"
            if lifecycle_via_launcher and installed and step["section"] == "lifecycle":
                install_root = Path(fixture.environment["LOCALAPPDATA"]) / "agent-core"
                if os.name == "nt":
                    argv = ["cmd", "/c", str(install_root / "bin" / "agent-core.cmd"), *argv[3:]]
                else:
                    argv = [str(install_root / "bin" / "agent-core"), *argv[3:]]
                entrypoint = "installed-stable-launcher"
            completed = subprocess.run(
                argv, cwd=engine_root, env=fixture.environment,
                check=False, capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            if completed.returncode != step["expect_exit"]:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ConfigError(
                    "FAIL_DOCS_COMMAND",
                    f"{step['id']} exit={completed.returncode} expected={step['expect_exit']} {detail}",
                )
            if re.search(step["expect_regex"], completed.stdout) is None:
                raise ConfigError(
                    "FAIL_DOCS_OUTPUT",
                    f"{step['id']} expectation not found stdout={completed.stdout!r}",
                )
            for name, pattern in step["capture"].items():
                if name in fixture.tokens or name in tokens:
                    raise ConfigError("FAIL_DOCS_TOKEN", f"capture would overwrite {name}")
                match = re.search(pattern, completed.stdout)
                if match is None:
                    raise ConfigError("FAIL_DOCS_CAPTURE", f"{step['id']}:{name}")
                tokens[name] = match.group(1)
            normalized = _normalize(completed.stdout, tokens)
            normalized_lines = normalized.splitlines()
            position = 0
            for line in step["example_output"]:
                try:
                    position = normalized_lines.index(line, position) + 1
                except ValueError as exc:
                    raise ConfigError(
                        "FAIL_DOCS_EXAMPLE",
                        f"{step['id']} missing-or-out-of-order={line!r} actual={normalized!r}",
                    ) from exc
            declared_lines = [
                *step["example_output"],
                *(item["line"] for item in step["omit"]),
            ]
            actual_count = Counter(normalized_lines)
            declared_count = Counter(declared_lines)
            if actual_count != declared_count:
                missing = list((actual_count - declared_count).elements())
                extra = list((declared_count - actual_count).elements())
                raise ConfigError(
                    "FAIL_DOCS_EXAMPLE",
                    f"{step['id']} undeclared={missing!r} extra={extra!r} actual={normalized!r}",
                )
            results.append(CommandResult(step["id"], completed.stdout, normalized, entrypoint))
            installed = installed or step["id"] == "install"
    return tuple(results)


def _command_block(steps: list[dict[str, Any]], section: str) -> str:
    blocks: list[str] = []
    for step in steps:
        if step["section"] != section:
            continue
        display = []
        for item in step["argv"]:
            match = TOKEN_RE.fullmatch(item)
            if match is None:
                display.append(item)
            elif match.group(1) == "python":
                display.append("python")
            else:
                display.append(f"<{match.group(1).upper()}>")
        blocks.append(
            "```console\n$ " + shlex.join(display) + "\n"
            + "\n".join(step["example_output"]) + "\n```"
        )
    return "\n\n".join(blocks)


def _render_file(path: Path, sections: dict[str, str], *, apply: bool) -> bool:
    original = path.read_text(encoding="utf-8")
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        section = match.group("section")
        if section not in sections:
            raise ConfigError("FAIL_DOCS_RENDER", f"unknown marker section: {section}")
        seen.add(section)
        return f"{match.group('start')}\n\n{sections[section]}\n\n{match.group('end')}"

    rendered = MARKER_RE.sub(replace, original)
    expected = {name for name in sections if f"COMMANDS:{name}:" in original}
    if seen != expected:
        raise ConfigError("FAIL_DOCS_RENDER", f"marker mismatch in {path}")
    changed = rendered != original
    if changed and apply:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    return changed


def render_docs(engine_root: Path, commands_path: Path, *, apply: bool) -> list[str]:
    steps = _load_commands(commands_path)
    sections = {name: _command_block(steps, name) for name in ("quickstart", "lifecycle")}
    files = (engine_root / "README.md", engine_root / "docs" / "LIFECYCLE.md")
    changed = [path for path in files if _render_file(path, sections, apply=apply)]
    if changed and not apply:
        raise ConfigError("FAIL_DOCS_DRIFT", ",".join(path.name for path in changed))
    return [f"PASS docs_render changed={len(changed)}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-core docs")
    commands = parser.add_subparsers(dest="docs_command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--commands", type=Path, required=True)
    render = commands.add_parser("render")
    render.add_argument("--commands", type=Path, default=Path("docs/commands.json"))
    mode = render.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None, engine_root: Path) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.docs_command == "verify":
            results = verify_commands(engine_root, args.commands)
            for result in results:
                print(f"PASS docs_command={result.command_id}")
            print(f"PASS docs_verify commands={len(results)}")
        else:
            print(*render_docs(engine_root, args.commands, apply=args.apply), sep="\n")
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
