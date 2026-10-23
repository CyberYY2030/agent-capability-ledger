from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_core import installer, materializer, sync
from agent_core.config import ConfigError, load_config
from tests.test_install import installed_fixture
from tests.test_sync import concrete_environment, _plan_hash_from


@pytest.fixture(autouse=True)
def isolated_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("AGENT_CORE_"):
            monkeypatch.delenv(key)
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))


def _snapshot(roots: tuple[Path, ...]) -> dict[Path, tuple[int, bytes | str | None]]:
    result = {}
    for root in roots:
        paths = [root, *root.rglob("*")] if root.exists() else [root]
        for path in paths:
            if path.is_symlink():
                result[path] = (path.lstat().st_mode, os.readlink(path))
            elif path.is_file():
                result[path] = (path.stat().st_mode, path.read_bytes())
            elif path.is_dir():
                result[path] = (path.stat().st_mode, None)
    return result


def _blocked(
    preview: Callable[[], list[str]], roots: tuple[Path, ...], expected: str,
) -> list[str]:
    before = _snapshot(roots)
    lines = preview()
    assert "ready=false" in lines[-1]
    assert not any(line.startswith("PLAN_HASH ") for line in lines)
    steps = [line for line in lines if line.startswith("NEXT ")]
    assert expected in steps
    assert len(steps) == len(set(steps))
    assert lines.index(expected) < len(lines) - 1
    assert not any(
        line.startswith(("TARGET ", "PLAN_OP ", "RETAIN ", "BLOCK_REASON "))
        for line in lines[lines.index(expected) + 1:]
    )
    assert _snapshot(roots) == before
    return lines


def _ready(preview: Callable[[], list[str]], apply: Callable[[str], list[str]]) -> str:
    lines = preview()
    assert "ready=true" in lines[-1]
    assert not any(line.startswith("NEXT ") for line in lines)
    token = _plan_hash_from(lines)
    assert apply(token)
    return token


def _aside(path: Path) -> None:
    path.rename(path.with_name(path.name + ".aside"))


def _install_environment(
    engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[], list[str]], Callable[[str], list[str]], tuple[Path, ...], Path, Path]:
    state, config, manifest, root = installed_fixture(engine, tmp_path, monkeypatch)

    def preview() -> list[str]:
        return installer.plan_install(engine, config, state, engine, manifest)

    def apply(token: str) -> list[str]:
        return installer.apply_install(
            engine, config, state, engine, manifest, force=False, plan_hash=token,
        )

    return preview, apply, (config.parent, root, tmp_path / "runtimes"), config, root


@pytest.mark.parametrize("kind", ("foreign", "file", "directory"))
def test_install_target_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    preview, apply, roots, _config, root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    baseline = _plan_hash_from(preview())
    if kind == "directory":
        target = root / "engine" / installer.__version__
        target.parent.mkdir(parents=True)
        target.write_bytes(b"foreign file\n")
        expected = f"NEXT indeterminate target: cannot read {target} as an ordinary directory; move it aside, then rerun the plan"
    else:
        target = root / "bin" / "agent-core"
        target.parent.mkdir(parents=True)
        if kind == "foreign":
            target.write_bytes(b"foreign wrapper\n")
            expected = f"NEXT foreign target: preserve or move aside {target}, then rerun the plan"
        else:
            target.mkdir()
            expected = f"NEXT indeterminate target: cannot read {target} as an ordinary file; move it aside, then rerun the plan"
    _blocked(preview, roots, expected)
    _aside(target)
    assert _plan_hash_from(preview()) == baseline
    _ready(preview, apply)


@pytest.mark.parametrize("resolution", ("remove-group", "move-file"))
def test_install_equivalent_unowned_hook_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolution: str,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    target_config = load_config(config)["targets"][0]
    target_root = Path(target_config["root"])
    target = installer.runtime_config_path(target_config["runtime"], target_root)
    desired = installer.render_fragment(
        standalone_engine / "runtimes" / target_config["runtime"] / "hook.fragment.json",
        target_root / target_config["hook_target"],
    )
    target.parent.mkdir(parents=True)
    payload = {"unrelated": "preserve", "hooks": {"UserPromptSubmit": desired["UserPromptSubmit"]}}
    target.write_text(json.dumps(payload), encoding="utf-8")
    reason = "INSTALL_CONFLICT equivalent unowned hook group: UserPromptSubmit"
    expected = f"NEXT foreign runtime-config: {reason}; keep a copy of {target}, then remove the conflicting hook group or move the file aside, and rerun the plan"
    _blocked(preview, roots, expected)
    if resolution == "remove-group":
        shutil.copy2(target, target.with_suffix(".copy"))
        payload["hooks"]["UserPromptSubmit"] = []
        target.write_text(json.dumps(payload), encoding="utf-8")
    else:
        _aside(target)
    _ready(preview, apply)
    if resolution == "remove-group":
        assert json.loads(target.read_text(encoding="utf-8"))["unrelated"] == "preserve"


def test_install_nonordinary_runtime_config_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    item = load_config(config)["targets"][0]
    target = installer.runtime_config_path(item["runtime"], Path(item["root"]))
    target.parent.mkdir(parents=True)
    target.mkdir()
    expected = f"NEXT foreign runtime-config: not an ordinary file; move aside {target}, then rerun the plan"
    _blocked(preview, roots, expected)
    _aside(target)
    _ready(preview, apply)


@pytest.mark.parametrize("fault", ("json", "objects", "identity", "ownership"))
def test_install_receipt_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    _ready(preview, apply)
    receipt = config.parent / "install-receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if fault == "json":
        receipt.write_text("{broken", encoding="utf-8")
    else:
        if fault == "objects":
            payload["objects"][0]["installed_sha256"] = "invalid"
        elif fault == "identity":
            payload["objects"][0]["label"] = "unexpected"
        else:
            payload["hook_bindings"][0]["ownership"] = {}
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    expected = f"NEXT indeterminate install-receipt: fix the cause in BLOCK_REASON, or move aside {receipt} (keep a copy), then rerun the plan"
    lines = _blocked(preview, roots, expected)
    assert any(line.startswith("BLOCK_REASON ") for line in lines)
    assert [line for line in lines if line.startswith("NEXT ")] == [expected]
    _aside(receipt)
    _ready(preview, apply)


def test_install_materialization_receipt_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    _ready(preview, apply)
    receipt = config.parent / "materialization-receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    expected = f"NEXT indeterminate materialization-receipt: FAIL_MATERIALIZATION_RECEIPT receipt fields mismatch; move aside {receipt} (keep a copy), then rerun the plan"
    lines = _blocked(preview, roots, expected)
    assert [line for line in lines if line.startswith("NEXT ")] == [expected]
    _aside(receipt)
    _ready(preview, apply)


def _sync_environment(
    engine: Path, tmp_path: Path,
) -> tuple[Callable[[], list[str]], Callable[[str], list[str]], tuple[Path, ...], Path, list[Path]]:
    state, config, targets = concrete_environment(tmp_path)

    def preview() -> list[str]:
        return sync.execute(engine, config, state, apply=False)

    def apply(token: str) -> list[str]:
        return sync.execute(engine, config, state, apply=True, plan_hash=token)

    return preview, apply, (config.parent,), config, targets


def test_sync_foreign_target_next_converges_with_stable_hash(
    standalone_engine: Path, tmp_path: Path,
) -> None:
    preview, apply, roots, _config, targets = _sync_environment(standalone_engine, tmp_path)
    baseline = _plan_hash_from(preview())
    target = targets[0] / "CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"foreign rules\n")
    expected = f"NEXT foreign target: preserve or move aside {target}, then rerun the plan"
    _blocked(preview, roots, expected)
    _aside(target)
    assert _plan_hash_from(preview()) == baseline
    _ready(preview, apply)


@pytest.mark.parametrize("fault", ("materialization", "legacy", "row-path", "row-identity"))
def test_sync_receipt_next_converges(
    standalone_engine: Path, tmp_path: Path, fault: str,
) -> None:
    preview, apply, roots, config, targets = _sync_environment(standalone_engine, tmp_path)
    if fault == "legacy":
        receipt = config.parent / "install-receipt.json"
        receipt.write_text("{}", encoding="utf-8")
        evidence = "install-receipt"
        reason = "FAIL_INSTALL_RECEIPT legacy state lock is unavailable"
    else:
        if fault == "row-identity":
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["targets"][0]["rules_target"] = "skills/ghost/rules.md"
            config.write_text(json.dumps(payload), encoding="utf-8")
        _ready(preview, apply)
        receipt = config.parent / "materialization-receipt.json"
        evidence = "materialization-receipt"
        if fault == "materialization":
            receipt.write_text("{}", encoding="utf-8")
            reason = "FAIL_MATERIALIZATION_RECEIPT receipt fields mismatch"
        else:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            row = next(item for item in payload["rows"] if item["source_label"] == "rules")
            if fault == "row-identity":
                row["source_label"] = "skill:ghost"
                reason = f"receipt row identity differs for {row['path']}"
            else:
                row["source_label"] = "lessons"
                reason = "FAIL_MATERIALIZATION_RECEIPT receipt row path identity mismatch"
            receipt.write_text(json.dumps(payload), encoding="utf-8")
    expected = f"NEXT indeterminate {evidence}: {reason}; move aside {receipt} (keep a copy), then rerun the plan"
    lines = _blocked(preview, roots, expected)
    assert [line for line in lines if line.startswith("NEXT ")] == [expected]
    _aside(receipt)
    _ready(preview, apply)


def test_ready_plans_have_no_next(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    preview, apply, _roots, _config, _root = _install_environment(standalone_engine, install_dir, monkeypatch)
    _ready(preview, apply)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    preview, apply, _roots, _config, _targets = _sync_environment(standalone_engine, sync_dir)
    _ready(preview, apply)


@pytest.mark.parametrize("repeat", (False, True))
def test_install_materialization_exception_keeps_failing_or_names_host_config(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeat: bool,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    original = installer._review_runtime_materialization
    calls = 0
    reason = f"FAIL_CONFIG cannot read {config}"

    def unreadable(plan: installer.InstallPlan) -> materializer.MaterializationReview:
        nonlocal calls
        calls += 1
        if repeat or calls == 1:
            raise ConfigError("FAIL_CONFIG", f"cannot read {config}")
        return original(plan)

    monkeypatch.setattr(installer, "_review_runtime_materialization", unreadable)
    if repeat:
        before = _snapshot(roots)
        with pytest.raises(ConfigError) as caught:
            preview()
        assert str(caught.value) == reason
        assert _snapshot(roots) == before
    else:
        expected = f"NEXT indeterminate host-config: {reason}; check {config}, then rerun the plan"
        lines = _blocked(preview, roots, expected)
        assert [line for line in lines if line.startswith("NEXT ")] == [expected]
    assert json.loads(config.read_text(encoding="utf-8"))["targets"]
    monkeypatch.setattr(installer, "_review_runtime_materialization", original)
    _ready(preview, apply)


def test_sync_reuses_the_payload_review(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview, apply, roots, _config, targets = _sync_environment(standalone_engine, tmp_path)
    target = targets[0] / "CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(b"foreign rules\n")
    original = sync._repository_review
    calls = 0

    def review(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert calls == 1, "dry-run observed a second review"
        return original(*args, **kwargs)

    monkeypatch.setattr(sync, "_repository_review", review)
    _blocked(preview, roots, f"NEXT foreign target: preserve or move aside {target}, then rerun the plan")
    monkeypatch.setattr(sync, "_repository_review", original)
    _aside(target)
    _ready(preview, apply)


def test_runtime_config_symlink_keeps_the_existing_preplan_rejection(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    item = load_config(config)["targets"][0]
    target = installer.runtime_config_path(item["runtime"], Path(item["root"]))
    target.parent.mkdir(parents=True)
    ordinary = target.with_suffix(".original")
    ordinary.write_text("{}", encoding="utf-8")
    target.symlink_to(ordinary)
    before = _snapshot(roots)
    with pytest.raises(ConfigError, match="FAIL_PATH_ESCAPE managed target is a symbolic link"):
        preview()
    assert _snapshot(roots) == before
    _aside(target)
    _ready(preview, apply)


def test_retained_drift_keeps_no_next(
    standalone_engine: Path, tmp_path: Path,
) -> None:
    state, config, targets = concrete_environment(tmp_path)
    preview = lambda: sync.execute(standalone_engine, config, state, apply=False)
    apply = lambda token: sync.execute(standalone_engine, config, state, apply=True, plan_hash=token)
    _ready(preview, apply)
    manifest = state / "manifest.yaml"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["capabilities"] = []
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    _ready(preview, apply)
    target = targets[0] / "skills" / "user-check" / "SKILL.md"
    original = target.read_bytes()
    target.write_bytes(b"retained drift\n")
    before = _snapshot((config.parent,))
    lines = preview()
    assert "ready=false" in lines[-1]
    assert any(line.startswith("RETAIN ") and "status=retained-drift" in line for line in lines)
    assert not any(line.startswith(("NEXT ", "PLAN_HASH ")) for line in lines)
    assert _snapshot((config.parent,)) == before
    target.write_bytes(original)
    _ready(preview, apply)


def _missing_owned_target_next_converges(
    preview: Callable[[], list[str]], apply: Callable[[str], list[str]],
    roots: tuple[Path, ...], config: Path, target: Path,
) -> None:
    receipt = config.parent / "materialization-receipt.json"
    expected = (
        f"NEXT foreign materialization-receipt: receipt-owned {target.resolve()} is missing; "
        f"move aside {receipt} (keep a copy), then rerun the plan"
    )
    lines = _blocked(preview, roots, expected)
    assert any(str(target) in line and "status=foreign" in line for line in lines) or any(
        line.startswith("PLAN_OP BLOCK ") and line.endswith("status=foreign") for line in lines
    )
    assert [line for line in lines if line.startswith("NEXT ")] == [expected]
    _aside(receipt)
    _ready(preview, apply)
    assert target.is_file()


def test_sync_missing_owned_target_next_converges(
    standalone_engine: Path, tmp_path: Path,
) -> None:
    preview, apply, roots, config, targets = _sync_environment(standalone_engine, tmp_path)
    _ready(preview, apply)
    target = targets[0] / "CLAUDE.md"
    target.unlink()
    _missing_owned_target_next_converges(preview, apply, roots, config, target)


def test_install_missing_owned_runtime_target_next_converges(
    standalone_engine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview, apply, roots, config, _root = _install_environment(standalone_engine, tmp_path, monkeypatch)
    _ready(preview, apply)
    line = next(item for item in preview() if item.startswith("TARGET runtime:"))
    target = Path(line.split(" path=", 1)[1])
    target.unlink()
    _missing_owned_target_next_converges(preview, apply, roots, config, target)
