from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_core.cli import main as cli_main
from agent_core.config import ConfigError
from agent_core.promote import plan_advance
from agent_core.retire import inspect_lifecycle, verify_receipt


ENGINE = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.resolve().as_posix()}", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


def write_ledger(root: Path, *, status: str = "checklist", sink: str = "tests/enforcement/consumer.txt",
                 extra: str = "", count: int = 1) -> None:
    entries = []
    for number in range(1, count + 1):
        lesson_id = f"L-{number}"
        entries.append(
            f"- **{lesson_id} [{status}·通用] Synthetic rule {number}.** "
            f"触发: synthetic verification. 代价: silent failure. verifier: synthetic-contract. "
            f"sink → {sink}.{extra} when: {{\"tasks\":[\"audit\"]}}"
        )
    path = root / "experience" / "LESSONS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Lessons Ledger\n<!-- lessons-schema: lessons-ledger/2 -->\n"
        "<!-- lessons-scope: global -->\n\n## 活跃\n\n"
        + "\n".join(entries)
        + "\n\n## 归档\n",
        encoding="utf-8",
    )


def state_tree(root: Path, *, status: str = "checklist", sink: str = "tests/enforcement/consumer.txt",
               count: int = 1) -> Path:
    shutil.copytree(ENGINE / "tests" / "enforcement", root / "tests" / "enforcement")
    (root / "enforcement").mkdir(parents=True)
    shutil.copy2(ENGINE / "enforcement" / "verifiers.json", root / "enforcement" / "verifiers.json")
    write_ledger(root, status=status, sink=sink, count=count)
    return root


def file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file() and ".git" not in path.parts
    }


def verify_once(state: Path, control: Path, *, now: dt.datetime | None = None):
    report = inspect_lifecycle(state, control, verify=True, allow_exec=True, strict=True, now=now)
    assert report.exit_code == 0
    assert any(line.startswith("READY_TO_ENFORCED L-1 ") for line in report.lines)
    return report.receipts["L-1"]


def install_receipt(state: Path, receipt: Path) -> tuple[str, Path]:
    sha = receipt.stem
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    target = state / "evidence" / "L-1" / f"{sha}.json"
    target.parent.mkdir(parents=True)
    shutil.copy2(receipt, target)
    ledger = state / "experience" / "LESSONS.md"
    text = ledger.read_text(encoding="utf-8")
    text = text.replace("[checklist·通用]", "[enforced·通用]", 1)
    text = text.replace(
        " when:",
        f" last_verified: {payload['verified_utc'][:10]}. evidence: {sha}. when:",
        1,
    )
    ledger.write_text(text, encoding="utf-8")
    return sha, target


def init_remote_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    seed = state_tree(tmp_path / "seed")
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", f"test{chr(64)}invalid")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    subprocess.run(["git", "clone", "-q", str(remote), str(alpha)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(beta)], check=True)
    for clone in (alpha, beta):
        git(clone, "config", "user.name", "Test")
        git(clone, "config", "user.email", f"test{chr(64)}invalid")
    return alpha, beta, remote


def test_verify_runs_negative_positive_and_consumer_without_mutating_state(tmp_path: Path) -> None:
    state = state_tree(tmp_path / "state")
    before = file_hashes(state)
    receipt = verify_once(state, tmp_path / "control")
    assert file_hashes(state) == before
    assert receipt.parent == tmp_path / "control" / "evidence-pending"
    check = verify_receipt(state, receipt, expected_lesson_id="L-1", expected_hash=receipt.stem)
    assert check.fresh
    assert [run["kind"] for run in check.payload["runs"]] == ["negative", "positive", "consumer"]
    assert check.payload["runs"][0]["exit_code"] != 0
    assert check.payload["runs"][1]["exit_code"] == 0
    assert check.payload["runs"][2]["exit_code"] == 0


def test_missing_deliberate_failing_fixture_is_not_ready(tmp_path: Path) -> None:
    state = state_tree(tmp_path / "state")
    (state / "tests" / "enforcement" / "negative.txt").unlink()
    report = inspect_lifecycle(state, tmp_path / "control", verify=True, allow_exec=True, strict=True)
    assert report.exit_code == 1
    assert any(line.startswith("NOT_READY L-1 missing_input") for line in report.lines)
    assert not report.receipts


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("negative", "FAIL_NEGATIVE_GREEN"),
        ("positive", "FAIL_POSITIVE_RED"),
        ("consumer", "FAIL_CONSUMER_RED"),
    ],
)
def test_each_verifier_layer_blocks_ready(tmp_path: Path, broken: str, expected: str) -> None:
    state = state_tree(tmp_path / "state")
    if broken == "negative":
        path = state / "tests" / "enforcement" / "negative.txt"
        path.write_text("valid\n", encoding="utf-8")
    elif broken == "positive":
        path = state / "tests" / "enforcement" / "positive.txt"
        path.write_text("invalid\n", encoding="utf-8")
    else:
        path = state / "enforcement" / "verifiers.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verifiers"][0]["consumers"][0]["consumer_probe"]["argv"][-1] = "L-9"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    report = inspect_lifecycle(state, tmp_path / "control", verify=True, allow_exec=True, strict=True)
    assert report.exit_code == 1
    assert any(line.startswith(f"NOT_READY L-1 {expected}") for line in report.lines)
    assert not report.receipts


def test_receipt_hash_mismatch_is_stale_and_does_not_change_status(tmp_path: Path) -> None:
    state = state_tree(tmp_path / "state")
    receipt = verify_once(state, tmp_path / "control")
    sha, installed = install_receipt(state, receipt)
    payload = json.loads(installed.read_text(encoding="utf-8"))
    payload["engine_version"] = "tampered"
    installed.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    before = (state / "experience" / "LESSONS.md").read_bytes()
    report = inspect_lifecycle(state, tmp_path / "control", strict=False)
    assert f"STALE_EVIDENCE L-1 receipt_hash expected={sha}" in "\n".join(report.lines)
    assert (state / "experience" / "LESSONS.md").read_bytes() == before
    assert "[enforced·通用]" in before.decode("utf-8")


@pytest.mark.parametrize("changed", ["positive", "verifier", "consumer"])
def test_related_input_changes_make_evidence_stale(tmp_path: Path, changed: str) -> None:
    state = state_tree(tmp_path / "state")
    receipt = verify_once(state, tmp_path / "control")
    install_receipt(state, receipt)
    if changed == "positive":
        (state / "tests" / "enforcement" / "positive.txt").write_text("changed\n", encoding="utf-8")
    elif changed == "consumer":
        (state / "tests" / "enforcement" / "consumer.txt").write_text("L-1 changed wiring\n", encoding="utf-8")
    else:
        path = state / "enforcement" / "verifiers.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["verifiers"][0]["positive"]["argv"].append("changed")
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    report = inspect_lifecycle(state, tmp_path / "control")
    assert any(line.startswith("STALE_EVIDENCE L-1 related_input") for line in report.lines)


def test_unrelated_lesson_change_does_not_stale_evidence(tmp_path: Path) -> None:
    state = state_tree(tmp_path / "state")
    receipt = verify_once(state, tmp_path / "control")
    install_receipt(state, receipt)
    ledger = state / "experience" / "LESSONS.md"
    text = ledger.read_text(encoding="utf-8").replace(
        "\n## 归档",
        "\n- **L-2 [pending·通用] Unrelated rule.** 触发: unrelated. 代价: none. "
        "sink → tests/enforcement/consumer.txt. when: {\"tasks\":[\"build\"]}\n## 归档",
    )
    ledger.write_text(text, encoding="utf-8")
    report = inspect_lifecycle(state, tmp_path / "control")
    assert "FRESH_EVIDENCE L-1" in report.lines


def test_broken_sink_strict_and_capacity_warning(tmp_path: Path) -> None:
    broken = state_tree(tmp_path / "broken", sink="checks/missing.md")
    report = inspect_lifecycle(broken, tmp_path / "control-a", strict=True)
    assert report.exit_code == 1
    assert "BROKEN_SINK L-1 checks/missing.md" in report.lines
    crowded = state_tree(tmp_path / "crowded", count=24)
    crowded_report = inspect_lifecycle(crowded, tmp_path / "control-b")
    assert "WARN capacity 24/30" in crowded_report.lines


def test_evidence_older_than_ninety_days_cannot_archive(tmp_path: Path) -> None:
    state = state_tree(tmp_path / "state")
    old = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    receipt = verify_once(state, tmp_path / "control", now=old)
    install_receipt(state, receipt)
    report = inspect_lifecycle(
        state, tmp_path / "control", now=old + dt.timedelta(days=91), verify=True, allow_exec=True)
    assert "STALE_EVIDENCE L-1 expired age_days=91" in report.lines
    assert not any(line.startswith("READY_TO_ARCHIVE L-1") for line in report.lines)


def test_lessons_retire_cli_succeeds_and_frozen_promote_preserves_state(tmp_path: Path,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    alpha, _beta, _remote = init_remote_state(tmp_path)
    control = tmp_path / "control"
    assert cli_main([
        "lessons", "retire", "--workspace", str(alpha), "--control-root", str(control),
        "--report", "--verify", "--allow-exec", "--strict",
    ]) == 1
    assert "FAIL_REPORT_EXEC" in capsys.readouterr().err
    assert not (control / "evidence-pending").exists()
    assert cli_main(["lessons", "retire", "--workspace", str(alpha), "--control-root", str(control),
                     "--verify", "--allow-exec", "--strict"]) == 0
    output = capsys.readouterr().out
    ready = next(line for line in output.splitlines() if line.startswith("READY_TO_ENFORCED L-1 "))
    _tag, _lesson_id, receipt_text, receipt_sha = ready.split()
    receipt = Path(receipt_text)
    assert receipt.is_file()
    assert receipt.stem == receipt_sha
    before_head = git(alpha, "rev-parse", "HEAD").stdout.strip()
    ledger = alpha / "experience" / "LESSONS.md"
    before_ledger = ledger.read_bytes()
    evidence_root = alpha / "evidence"
    before_evidence = {
        path.relative_to(alpha).as_posix(): path.read_bytes()
        for path in evidence_root.rglob("*") if path.is_file()
    } if evidence_root.exists() else {}

    assert cli_main([
        "promote", "--state", str(alpha), "--control-root", str(control),
        "--advance", "L-1", "--evidence-receipt", str(receipt), "--apply",
    ]) == 2
    frozen = capsys.readouterr()
    assert frozen.out == ""
    assert frozen.err == "FAIL_COMMAND_FROZEN promote\n"
    assert git(alpha, "rev-parse", "HEAD").stdout.strip() == before_head
    assert ledger.read_bytes() == before_ledger
    after_evidence = {
        path.relative_to(alpha).as_posix(): path.read_bytes()
        for path in evidence_root.rglob("*") if path.is_file()
    } if evidence_root.exists() else {}
    assert after_evidence == before_evidence


def test_advance_rejects_tampered_receipt_before_transaction(tmp_path: Path) -> None:
    alpha, _beta, _remote = init_remote_state(tmp_path)
    control = tmp_path / "control"
    receipt = verify_once(alpha, control)
    receipt.write_text(receipt.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ConfigError, match="FAIL_EVIDENCE_HASH"):
        plan_advance(alpha, control, "L-1", receipt)


def test_advance_rejects_status_mismatch(tmp_path: Path) -> None:
    alpha, _beta, _remote = init_remote_state(tmp_path)
    control = tmp_path / "control"
    receipt = verify_once(alpha, control)
    ledger = alpha / "experience" / "LESSONS.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace("[checklist·通用]", "[pending·通用]", 1),
        encoding="utf-8",
    )
    git(alpha, "add", "experience/LESSONS.md")
    git(alpha, "commit", "-q", "-m", "change lesson status")
    git(alpha, "push", "-q", "origin", "main")
    assert verify_receipt(
        alpha, receipt, expected_lesson_id="L-1", expected_hash=receipt.stem,
    ).fresh
    with pytest.raises(ConfigError, match="FAIL_EVIDENCE_STATUS"):
        plan_advance(alpha, control, "L-1", receipt)


def test_advance_rejects_verifier_mismatch(tmp_path: Path) -> None:
    alpha, _beta, _remote = init_remote_state(tmp_path)
    control = tmp_path / "control"
    manifest = alpha / "enforcement" / "verifiers.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    alternate = dict(payload["verifiers"][0])
    alternate["id"] = "alternate-contract"
    payload["verifiers"].append(alternate)
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    git(alpha, "add", "enforcement/verifiers.json")
    git(alpha, "commit", "-q", "-m", "add alternate verifier")
    git(alpha, "push", "-q", "origin", "main")
    receipt = verify_once(alpha, control)
    ledger = alpha / "experience" / "LESSONS.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            "verifier: synthetic-contract", "verifier: alternate-contract", 1,
        ),
        encoding="utf-8",
    )
    git(alpha, "add", "experience/LESSONS.md")
    git(alpha, "commit", "-q", "-m", "change lesson verifier")
    git(alpha, "push", "-q", "origin", "main")
    assert verify_receipt(
        alpha, receipt, expected_lesson_id="L-1", expected_hash=receipt.stem,
    ).fresh
    with pytest.raises(ConfigError, match="FAIL_EVIDENCE_VERIFIER"):
        plan_advance(alpha, control, "L-1", receipt)
