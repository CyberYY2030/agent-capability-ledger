"""Surgical JSON hook-group ownership without rewriting unrelated bytes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError


RUNTIME_CONFIG_NAMES = {"claude-code": "settings.json", "codex": "hooks.json"}


@dataclass(frozen=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True)
class Member:
    key: str
    key_span: Span
    value_span: Span


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _string_end(text: str, index: int) -> int:
    if index >= len(text) or text[index] != '"':
        raise ConfigError("FAIL_RUNTIME_CONFIG", "expected JSON string")
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    raise ConfigError("FAIL_RUNTIME_CONFIG", "unterminated JSON string")


def _value_end(text: str, index: int) -> int:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise ConfigError("FAIL_RUNTIME_CONFIG", "missing JSON value")
    if text[index] == '"':
        return _string_end(text, index)
    if text[index] in "[{":
        opening = text[index]
        closing = "]" if opening == "[" else "}"
        depth = 1
        cursor = index + 1
        while cursor < len(text):
            if text[cursor] == '"':
                cursor = _string_end(text, cursor)
                continue
            if text[cursor] == opening:
                depth += 1
            elif text[cursor] == closing:
                depth -= 1
                if depth == 0:
                    return cursor + 1
            cursor += 1
        raise ConfigError("FAIL_RUNTIME_CONFIG", "unterminated JSON container")
    cursor = index
    while cursor < len(text) and text[cursor] not in ",]}":
        cursor += 1
    return cursor


def _object_members(text: str, object_span: Span) -> list[Member]:
    if text[object_span.start] != "{" or text[object_span.end - 1] != "}":
        raise ConfigError("FAIL_RUNTIME_CONFIG", "expected JSON object")
    members: list[Member] = []
    cursor = _skip_ws(text, object_span.start + 1)
    while cursor < object_span.end - 1 and text[cursor] != "}":
        key_start = cursor
        key_end = _string_end(text, cursor)
        try:
            key = json.loads(text[key_start:key_end])
        except json.JSONDecodeError as exc:
            raise ConfigError("FAIL_RUNTIME_CONFIG", str(exc)) from exc
        cursor = _skip_ws(text, key_end)
        if cursor >= len(text) or text[cursor] != ":":
            raise ConfigError("FAIL_RUNTIME_CONFIG", "expected colon")
        value_start = _skip_ws(text, cursor + 1)
        value_end = _value_end(text, value_start)
        members.append(Member(key, Span(key_start, key_end), Span(value_start, value_end)))
        cursor = _skip_ws(text, value_end)
        if cursor < object_span.end - 1 and text[cursor] == ",":
            cursor = _skip_ws(text, cursor + 1)
            continue
        if cursor < object_span.end - 1 and text[cursor] != "}":
            raise ConfigError("FAIL_RUNTIME_CONFIG", "expected comma or object end")
    return members


def _array_elements(text: str, array_span: Span) -> list[Span]:
    if text[array_span.start] != "[" or text[array_span.end - 1] != "]":
        raise ConfigError("FAIL_RUNTIME_CONFIG", "expected JSON array")
    elements: list[Span] = []
    cursor = _skip_ws(text, array_span.start + 1)
    while cursor < array_span.end - 1 and text[cursor] != "]":
        end = _value_end(text, cursor)
        elements.append(Span(cursor, end))
        cursor = _skip_ws(text, end)
        if cursor < array_span.end - 1 and text[cursor] == ",":
            cursor = _skip_ws(text, cursor + 1)
            continue
        if cursor < array_span.end - 1 and text[cursor] != "]":
            raise ConfigError("FAIL_RUNTIME_CONFIG", "expected comma or array end")
    return elements


def _root_span(text: str) -> Span:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("FAIL_RUNTIME_CONFIG", str(exc)) from exc
    if not isinstance(payload, dict):
        raise ConfigError("FAIL_RUNTIME_CONFIG", "runtime config root must be an object")
    start = _skip_ws(text, 0)
    end = _value_end(text, start)
    if _skip_ws(text, end) != len(text):
        raise ConfigError("FAIL_RUNTIME_CONFIG", "trailing JSON content")
    return Span(start, end)


def _member(text: str, object_span: Span, key: str) -> Member | None:
    matches = [item for item in _object_members(text, object_span) if item.key == key]
    if len(matches) > 1:
        raise ConfigError("FAIL_RUNTIME_CONFIG", f"duplicate JSON member: {key}")
    return matches[0] if matches else None


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_member(text: str, object_span: Span, key: str, value: Any) -> str:
    close = object_span.end - 1
    insert = close
    while insert > object_span.start + 1 and text[insert - 1].isspace():
        insert -= 1
    prefix = "" if text[object_span.start + 1:insert].strip() == "" else ","
    addition = prefix + _compact(key) + ":" + _compact(value)
    return text[:insert] + addition + text[insert:]


def _append_element(text: str, array_span: Span, value: Any) -> str:
    close = array_span.end - 1
    insert = close
    while insert > array_span.start + 1 and text[insert - 1].isspace():
        insert -= 1
    prefix = "" if text[array_span.start + 1:insert].strip() == "" else ","
    return text[:insert] + prefix + _compact(value) + text[insert:]


def _replace_span(text: str, span: Span, value: Any) -> str:
    return text[:span.start] + _compact(value) + text[span.end:]


def _remove_element(text: str, array_span: Span, index: int) -> str:
    elements = _array_elements(text, array_span)
    if index < 0 or index >= len(elements):
        raise ConfigError("FAIL_RUNTIME_CONFIG", "owned hook index is missing")
    target = elements[index]
    if len(elements) == 1:
        return text[:target.start] + text[target.end:]
    if index < len(elements) - 1:
        end = elements[index + 1].start
        return text[:target.start] + text[end:]
    start = elements[index - 1].end
    return text[:start] + text[target.end:]


def _remove_member(text: str, object_span: Span, key: str) -> str:
    members = _object_members(text, object_span)
    indexes = [index for index, item in enumerate(members) if item.key == key]
    if len(indexes) != 1:
        raise ConfigError("UNINSTALL_CONFLICT", f"owned JSON member differs: {key}")
    index = indexes[0]
    target = members[index]
    start = target.key_span.start
    end = target.value_span.end
    if len(members) == 1:
        return text[:start] + text[end:]
    if index < len(members) - 1:
        return text[:start] + text[members[index + 1].key_span.start:]
    return text[:members[index - 1].value_span.end] + text[end:]


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_fragment(
    fragment_path: Path, hook_target: Path, *, windows: bool | None = None,
) -> dict[str, list[Any]]:
    try:
        payload = json.loads(fragment_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("FAIL_HOOK_FRAGMENT", f"{fragment_path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "hooks"}:
        raise ConfigError("FAIL_HOOK_FRAGMENT", "fragment fields mismatch")
    if payload.get("schema") != "hook-fragment/1" or not isinstance(payload.get("hooks"), dict):
        raise ConfigError("FAIL_HOOK_FRAGMENT", "fragment schema mismatch")

    use_windows = os.name == "nt" if windows is None else windows
    windows_target = hook_target.with_suffix(".ps1")

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{{HOOK_TARGET}}", str(hook_target))
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    hooks = replace(payload["hooks"])
    if set(hooks) != {"UserPromptSubmit", "PreToolUse", "Stop"}:
        raise ConfigError("FAIL_HOOK_FRAGMENT", "required hook events differ")
    if any(not isinstance(groups, list) or len(groups) != 1 for groups in hooks.values()):
        raise ConfigError("FAIL_HOOK_FRAGMENT", "each event must own exactly one group")
    if use_windows:
        stages = {"UserPromptSubmit": "prompt", "PreToolUse": "pretool", "Stop": "completion"}
        for event, groups in hooks.items():
            handler = groups[0].get("hooks", [{}])[0]
            if not isinstance(handler, dict) or handler.get("type") != "command":
                raise ConfigError("FAIL_HOOK_FRAGMENT", f"command handler missing: {event}")
            stage = stages[event]
            if fragment_path.parent.name == "claude-code":
                handler.clear()
                handler.update({
                    "type": "command",
                    "shell": "powershell",
                    "command": (
                        "& powershell.exe -NoProfile -NonInteractive "
                        "-ExecutionPolicy Bypass -File "
                        f"{_powershell_quote(str(windows_target))} {stage}"
                    ),
                })
            else:
                handler["commandWindows"] = (
                    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                    f'"{windows_target}" {stage}'
                )
    return hooks


def merge_owned_hooks(
    original: bytes | None,
    desired: dict[str, list[Any]],
    previous: dict[str, Any] | None,
    *,
    force: bool,
) -> tuple[bytes, dict[str, Any], bool]:
    text = original.decode("utf-8") if original is not None else "{}\n"
    root = _root_span(text)
    hooks_member = _member(text, root, "hooks")
    hooks_created = hooks_member is None if previous is None else bool(previous.get("hooks_created"))
    if hooks_member is None:
        text = _append_member(text, root, "hooks", {})
    elif text[hooks_member.value_span.start] != "{":
        raise ConfigError("INSTALL_CONFLICT", "runtime hooks must be an object")

    records: list[dict[str, Any]] = []
    previous_by_event = {
        item["event"]: item for item in (previous or {}).get("groups", [])
    }
    changed = original is None
    for event in ("UserPromptSubmit", "PreToolUse", "Stop"):
        desired_group = desired[event][0]
        root = _root_span(text)
        hooks_member = _member(text, root, "hooks")
        assert hooks_member is not None
        event_member = _member(text, hooks_member.value_span, event)
        old = previous_by_event.get(event)
        event_created = event_member is None if old is None else bool(old.get("event_created"))
        if event_member is None:
            text = _append_member(text, hooks_member.value_span, event, [desired_group])
            index = 0
            changed = True
            original_exists = False
            original_value = None
        else:
            elements = _array_elements(text, event_member.value_span)
            values = [json.loads(text[item.start:item.end]) for item in elements]
            if old is None:
                matches = [index for index, value in enumerate(values) if value == desired_group]
                if matches and not force:
                    raise ConfigError("INSTALL_CONFLICT", f"equivalent unowned hook group: {event}")
                if matches:
                    index = matches[0]
                    original_exists = True
                    original_value = desired_group
                else:
                    text = _append_element(text, event_member.value_span, desired_group)
                    index = len(elements)
                    changed = True
                    original_exists = False
                    original_value = None
            else:
                old_value = old.get("installed_value")
                matches = [index for index, value in enumerate(values) if value == old_value]
                if len(matches) == 1:
                    index = matches[0]
                    if old_value != desired_group:
                        text = _replace_span(text, elements[index], desired_group)
                        changed = True
                elif not force:
                    raise ConfigError("INSTALL_CONFLICT", f"owned hook group changed: {event}")
                else:
                    index = old.get("index")
                    if not isinstance(index, int) or index < 0 or index >= len(elements):
                        raise ConfigError("INSTALL_CONFLICT", f"owned hook index missing: {event}")
                    text = _replace_span(text, elements[index], desired_group)
                    changed = True
                original_exists = bool(old.get("original_exists"))
                original_value = old.get("original_value")
        records.append({
            "event": event,
            "index": index,
            "event_created": event_created,
            "original_exists": original_exists,
            "original_value": original_value,
            "installed_value": desired_group,
        })
    return text.encode("utf-8"), {"hooks_created": hooks_created, "groups": records}, changed


def remove_owned_hooks(current: bytes, ownership: dict[str, Any]) -> bytes:
    text = current.decode("utf-8")
    for record in reversed(ownership.get("groups", [])):
        root = _root_span(text)
        hooks_member = _member(text, root, "hooks")
        if hooks_member is None:
            raise ConfigError("UNINSTALL_CONFLICT", "hooks object missing")
        event_member = _member(text, hooks_member.value_span, record["event"])
        if event_member is None:
            raise ConfigError("UNINSTALL_CONFLICT", f"event missing: {record['event']}")
        elements = _array_elements(text, event_member.value_span)
        matches = [
            index for index, span in enumerate(elements)
            if json.loads(text[span.start:span.end]) == record["installed_value"]
        ]
        if len(matches) != 1:
            raise ConfigError("UNINSTALL_CONFLICT", f"owned hook changed: {record['event']}")
        if record.get("original_exists") and record.get("original_value") == record.get("installed_value"):
            continue
        if record.get("original_exists"):
            text = _replace_span(text, elements[matches[0]], record.get("original_value"))
        else:
            text = _remove_element(text, event_member.value_span, matches[0])
        if record.get("event_created"):
            root = _root_span(text)
            hooks_member = _member(text, root, "hooks")
            assert hooks_member is not None
            event_member = _member(text, hooks_member.value_span, record["event"])
            if event_member is None:
                raise ConfigError("UNINSTALL_CONFLICT", f"owned event missing: {record['event']}")
            if not _array_elements(text, event_member.value_span):
                text = _remove_member(text, hooks_member.value_span, record["event"])
    if ownership.get("hooks_created"):
        root = _root_span(text)
        hooks_member = _member(text, root, "hooks")
        if hooks_member is None:
            raise ConfigError("UNINSTALL_CONFLICT", "owned hooks object missing")
        if not _object_members(text, hooks_member.value_span):
            text = _remove_member(text, root, "hooks")
    return text.encode("utf-8")


def runtime_config_path(runtime: str, target_root: Path) -> Path:
    try:
        name = RUNTIME_CONFIG_NAMES[runtime]
    except KeyError as exc:
        raise ConfigError("FAIL_RUNTIME_CONFIG", f"unsupported runtime: {runtime}") from exc
    return target_root / name


def runtime_hook_path(hook_target: Path) -> Path:
    return hook_target.with_suffix(".ps1") if os.name == "nt" else hook_target
