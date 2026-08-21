#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate global/profile/project LESSONS ledgers (stdlib only)."""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

LEGACY_ID = r'L-[A-Za-z]?\d+'
SCOPED_ID = r'[A-Z][A-Z0-9-]*-\d+'
ENTRY_RE = re.compile(
    rf'^\s*-\s+\*\*(?:({LEGACY_ID})|\[\[lesson:({SCOPED_ID})\]\])'
    r'\s*\[([^\]\s·]+)·(通用|领域|项目)\]')
REF_RE = re.compile(rf'(?<![\w-])({LEGACY_ID})\b|\[\[lesson:({SCOPED_ID})\]\]')
LEGACY_FIELD_RE = re.compile(rf'\blegacy_id:\s*({LEGACY_ID})\b')
VOID_MARK_RE = re.compile(r'(作废|空洞|void)', re.IGNORECASE)
MOVED_ID_RE = re.compile(r'<!--\s*(?:moved|merged)\b[^:]*:\s*(L-\d+)\b')
STATUS_OK = {'pending', 'checklist', 'enforced'}
SCOPE_LABEL = {'global': '通用', 'profile': '领域', 'project': '项目'}
ACTIVE_CAP = 30
ACTIVE_HEADINGS = frozenset({'active', '活跃', '活跃区'})
TEXT_EXT = {'.md', '.py', '.txt', '.json', '.rs', '.ts', '.tsx', '.js', '.mjs'}
SCHEMAS = {'lessons-ledger/1', 'lessons-ledger/2'}
ROUTING_SCHEMA = 'lessons-routing/1'
PROFILE_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def read_text(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def is_active_heading(heading):
    """Return whether one Markdown heading names the active lesson section."""
    normalized = unicodedata.normalize('NFKC', heading).strip().casefold()
    return normalized in ACTIVE_HEADINGS


def iter_refs(text):
    for match in REF_RE.finditer(text):
        yield match.group(1) or match.group(2)


def _meta(text, key):
    match = re.search(rf'^<!--\s*{re.escape(key)}:\s*([^>]+?)\s*-->\r?$', text, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_ledger(text, expected_scope=None, source='ledger', allow_scope_debt=False):
    """Return (defined id -> line, errors, warnings) for one ledger."""
    entries = []
    errors = []
    warns = []
    void_ids = {}
    moved_ids = set(MOVED_ID_RE.findall(text))
    active_count = 0
    in_active = False

    if expected_scope:
        schema = _meta(text, 'lessons-schema')
        scope = _meta(text, 'lessons-scope')
        if schema not in SCHEMAS:
            errors.append(f'{source}: lessons-schema 必须是 lessons-ledger/1 或 lessons-ledger/2,当前={schema!r}')
        if scope != expected_scope:
            errors.append(f'{source}: lessons-scope 必须是 {expected_scope},当前={scope!r}')

    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith('## '):
            in_active = is_active_heading(line.strip()[3:])
            continue
        match = ENTRY_RE.match(line)
        if match:
            lesson_id = match.group(1) or match.group(2)
            status, label = match.group(3), match.group(4)
            entries.append((lesson_id, status, label, lineno, line))
            if in_active:
                active_count += 1
            if status not in STATUS_OK:
                errors.append(f'{source}:{lineno} {lesson_id} 状态标签 {status!r} 非法')
            if expected_scope and label != SCOPE_LABEL[expected_scope]:
                message = (f'{source}:{lineno} {lesson_id} scope 标签为 {label},'
                           f'该 store 只允许 {SCOPE_LABEL[expected_scope]}')
                if allow_scope_debt and expected_scope == 'global':
                    warns.append('迁移债务 ' + message)
                else:
                    errors.append(message)
            if expected_scope == 'global' and not lesson_id.startswith('L-'):
                errors.append(f'{source}:{lineno} global 条目必须使用 L-n:{lesson_id}')
            if expected_scope in {'profile', 'project'} and lesson_id.startswith('L-'):
                errors.append(f'{source}:{lineno} {expected_scope} 条目必须使用 namespaced id:{lesson_id}')
            if '触发' not in line:
                warns.append(f'{source}:{lineno} {lesson_id} 缺触发字段')
            continue
        if VOID_MARK_RE.search(line):
            for lesson_id in iter_refs(line):
                void_ids.setdefault(lesson_id, lineno)

    seen = {}
    for lesson_id, _status, _label, lineno, _line in entries:
        if lesson_id in seen:
            errors.append(f'{source}:重号 {lesson_id}:第 {seen[lesson_id]} 与 {lineno} 行')
        else:
            seen[lesson_id] = lineno
    for lesson_id, lineno in void_ids.items():
        if lesson_id in seen:
            errors.append(f'{source}:矛盾:{lesson_id} 第 {lineno} 行声明作废/空洞,第 {seen[lesson_id]} 行仍活跃')
    if active_count > ACTIVE_CAP:
        warns.append(f'{source}:活跃区 {active_count} 条 > 上限 {ACTIVE_CAP}')
    if expected_scope == 'global':
        numeric_ids = set(seen) | moved_ids
        nums = sorted(int(x[2:]) for x in numeric_ids if re.fullmatch(r'L-\d+', x))
        if nums:
            missing = [n for n in range(1, nums[-1] + 1) if n not in nums]
            if missing:
                warns.append(f'{source}:编号空洞 L-{missing}')
    return seen, errors, warns


def ledger_summary(text):
    active = False
    archive = False
    counts = {status: 0 for status in sorted(STATUS_OK)}
    archived = 0
    missing_sink = []
    for line in text.splitlines():
        if line.strip().startswith('## '):
            active = is_active_heading(line.strip()[3:])
            archive = '归档' in line
            continue
        match = ENTRY_RE.match(line)
        if not match:
            continue
        lesson_id = match.group(1) or match.group(2)
        status = match.group(3)
        if active and status in counts:
            counts[status] += 1
        if archive:
            archived += 1
        if 'sink →' not in line:
            missing_sink.append(lesson_id)
    active_count = sum(counts.values())
    return counts, active_count, archived, missing_sink


def gather_ref_files(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                files.extend(os.path.join(root, name) for name in names
                             if os.path.splitext(name)[1].lower() in TEXT_EXT)
        elif os.path.isfile(path):
            files.append(path)
    return files


def check_refs(defined_ids, paths, ledger_paths):
    """References must resolve in the sources loaded for this workspace."""
    errors = []
    if isinstance(ledger_paths, (str, os.PathLike)):
        ledger_paths = [ledger_paths]
    ledger_abs = {os.path.abspath(path) for path in ledger_paths}
    for path in gather_ref_files(paths):
        if os.path.abspath(path) in ledger_abs:
            continue
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        for lesson_id in sorted(set(iter_refs(text)) - set(defined_ids)):
            errors.append(f'悬空引用 {lesson_id}:{path} 未在当前 workspace 的已加载 sources 中定义')
    return errors


def find_git_root(workspace):
    workspace = os.path.abspath(workspace)
    try:
        result = subprocess.run(
            ['git', '-c', f'safe.directory={workspace}', '-C', workspace,
             'rev-parse', '--show-toplevel'],
            check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return os.path.abspath(result.stdout.strip()) if result.returncode == 0 else None


def _profile_root(global_ledger):
    parent = os.path.dirname(os.path.abspath(global_ledger))
    if os.path.basename(parent) == 'experience':
        return os.path.join(parent, 'profiles')
    return os.path.join(parent, 'lesson-profiles')


def resolve_sources(global_ledger, workspace=None, all_profiles=False):
    """Return ordered (scope, name, path) sources plus routing errors/warnings."""
    sources = [('global', 'global', os.path.abspath(global_ledger))]
    errors = []
    warns = []
    if all_profiles:
        profile_root = _profile_root(global_ledger)
        if os.path.isdir(profile_root):
            for profile in sorted(os.listdir(profile_root)):
                path = os.path.join(profile_root, profile, 'LESSONS.md')
                if os.path.isfile(path):
                    sources.append(('profile', profile, path))
        return sources, errors, warns
    if not workspace:
        return sources, errors, warns
    root = find_git_root(workspace)
    if not root:
        warns.append(f'workspace 无 git root:{workspace};只加载 global')
        return sources, errors, warns
    config_path = os.path.join(root, '.agents', 'lessons.json')
    if not os.path.isfile(config_path):
        return sources, errors, warns
    try:
        config = json.loads(read_text(config_path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f'无法解析 {config_path}:{exc}')
        return sources, errors, warns
    project_id = config.get('project_id')
    profiles = config.get('profiles')
    if config.get('schema') != ROUTING_SCHEMA:
        errors.append(f'{config_path}:schema 必须是 {ROUTING_SCHEMA}')
    if not isinstance(project_id, str) or not PROFILE_RE.fullmatch(project_id):
        errors.append(f'{config_path}:project_id 必须是稳定 kebab-case 字符串')
    if not isinstance(profiles, list) or any(not isinstance(x, str) for x in profiles):
        errors.append(f'{config_path}:profiles 必须是字符串数组')
        profiles = []
    elif len(profiles) != len(set(profiles)) or any(not PROFILE_RE.fullmatch(x) for x in profiles):
        errors.append(f'{config_path}:profiles 必须唯一且为 kebab-case')
    for profile in profiles:
        path = os.path.join(_profile_root(global_ledger), profile, 'LESSONS.md')
        if not os.path.isfile(path):
            errors.append(f'声明的 profile 不存在:{profile} ({path})')
        else:
            sources.append(('profile', profile, path))
    project_path = os.path.join(root, '.agents', 'LESSONS.md')
    if os.path.isfile(project_path):
        sources.append(('project', project_id or '<invalid>', project_path))
    return sources, errors, warns


def validate_sources(sources, allow_scope_debt=False):
    defined = {}
    aliases = {}
    errors = []
    warns = []
    for scope, name, path in sources:
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f'无法读取 {path}:{exc}')
            continue
        ids, store_errors, store_warns = parse_ledger(
            text, scope, path, allow_scope_debt=allow_scope_debt)
        errors.extend(store_errors)
        warns.extend(store_warns)
        expected_name = _meta(text, 'lessons-profile' if scope == 'profile' else 'lessons-project')
        if scope in {'profile', 'project'} and expected_name != name:
            errors.append(f'{path}:store id 必须是 {name},当前={expected_name!r}')
        prefix = name.split('-', 1)[0].upper() if scope in {'profile', 'project'} else 'L'
        for lesson_id, lineno in ids.items():
            if scope in {'profile', 'project'} and not lesson_id.startswith(prefix + '-'):
                errors.append(f'{path}:{lineno} {lesson_id} 必须使用前缀 {prefix}-')
            if lesson_id in defined or lesson_id in aliases:
                errors.append(f'跨 store 重号:{lesson_id}')
            defined[lesson_id] = path
        for lineno, line in enumerate(text.splitlines(), 1):
            if not ENTRY_RE.match(line):
                continue
            for legacy_id in LEGACY_FIELD_RE.findall(line):
                if legacy_id in defined or legacy_id in aliases:
                    errors.append(f'legacy_id 冲突:{legacy_id} ({path}:{lineno})')
                aliases[legacy_id] = path
    return {**defined, **aliases}, errors, warns


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ledger', nargs='?', default='LESSONS.md')
    parser.add_argument('--workspace')
    parser.add_argument('--all-profiles', action='store_true')
    parser.add_argument('--refs', nargs='+', default=[])
    parser.add_argument('--allow-scope-debt', action='store_true',
                        help='E1a migration-only: downgrade global scope mismatches to WARN')
    return parser


def main(argv):
    try:
        args = build_parser().parse_args(argv[1:])
    except SystemExit as exc:
        return int(exc.code)
    if args.workspace and args.all_profiles:
        print('ERROR --workspace 与 --all-profiles 不能同时使用')
        return 2
    sources, errors, warns = resolve_sources(
        args.ledger, args.workspace, all_profiles=args.all_profiles)
    if args.allow_scope_debt and any(scope != 'global' for scope, _name, _path in sources):
        errors.append('--allow-scope-debt 只允许用于迁移前的 global-only 验收')
    defined, store_errors, store_warns = validate_sources(sources, args.allow_scope_debt)
    errors.extend(store_errors)
    warns.extend(store_warns)
    if args.refs:
        errors.extend(check_refs(defined, args.refs, [path for _scope, _name, path in sources]))
    for scope, name, path in sources:
        print(f'SOURCE {scope}:{name} {path}')
        try:
            counts, active, archived, missing = ledger_summary(read_text(path))
            remaining = max(0, ACTIVE_CAP - active)
            print('SUMMARY '
                  f'{scope}:{name} active={active} '
                  f'pending={counts["pending"]} checklist={counts["checklist"]} '
                  f'enforced={counts["enforced"]} remaining={remaining} '
                  f'archived={archived} missing_sink={",".join(missing) if missing else "none"}')
        except (OSError, UnicodeDecodeError):
            pass
    for warning in warns:
        print(f'WARN  {warning}')
    for error in errors:
        print(f'ERROR {error}')
    if errors:
        print(f'\nFAIL  {len(errors)} 个错误。')
        return 1
    print(f'PASS  已加载 {len(sources)} 个 store,可解析 {len(defined)} 个 lesson id/legacy_id' +
          (f';{len(warns)} 个告警' if warns else '') + '。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
