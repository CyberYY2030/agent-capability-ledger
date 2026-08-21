from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_no_remote_submodule_or_symlink() -> None:
    remotes = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.resolve().as_posix()}", "-C", str(ROOT), "remote"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert remotes.stdout.strip() == ""
    assert not (ROOT / ".gitmodules").exists()
    assert not [path for path in ROOT.rglob("*") if path.is_symlink()]


def test_public_configs_bind_no_private_state_or_runtime_root() -> None:
    configs = [ROOT / "examples" / "host.example.json", *(ROOT / "tests" / "fixtures" / "config").glob("*.json")]
    for path in configs:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state_root"] == "<STATE>"
        assert payload["backup_root"].startswith("<")
        assert all(target["root"].startswith("<") for target in payload["targets"])
    manifest = json.loads((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert all(not Path(item["source"]).is_absolute() and ".." not in Path(item["source"]).parts for item in manifest["capabilities"])
