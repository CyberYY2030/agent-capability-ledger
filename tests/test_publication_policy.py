"""Explicit publication semantics preserve strict scanning and capture."""
import json
import re
import subprocess

import pytest
from agent_core import capture, privacy

HOME = "$" + "HOME"
SYNTHETIC = "C:/Users/__AGENT_CORE_SYNTHETIC__/fixture.txt"
EMAIL = "fixture" + "@" + "example.invalid"
SAFE = [HOME + "/.local/share", "$" + "{HOME}/file", "%" + "HOME%/file",
        "%" + "USERPROFILE%/file", "~" + "/file", "~" + "\\file",
        '"C:' + '\\Program Files"', 'r"C:' + '\\\\Program Files"',
        EMAIL, "sample" + "@" + "sub.example.test", SYNTHETIC]


def hits(value, publication=True, rules=None):
    return privacy._scan_bytes(value.encode(), "sample.txt",
                               rules or privacy._default_rules(),
                               privacy.DEFAULT_MAX_BLOB_BYTES, publication)[0]


@pytest.mark.parametrize("value", SAFE)
def test_generic_values_require_explicit_publication(value):
    assert hits(value, publication=False)
    assert not hits(value)
    assert any(rule.regex.search(value) for rule in capture.PROJECT_CAPTURE_RULES)


@pytest.mark.parametrize("value", [
    "/".join(("C:", "Users", "person/file.txt")), "/" + "home/person/file.txt",
    "owner" + "@" + "real-domain.com", EMAIL + ".com", EMAIL + "-old",
    EMAIL + ".", EMAIL + ".evil",
    "ghp_" + "a" * 24, "sk-" + "a" * 24,
    "-----BEGIN " + "PRIVATE KEY-----",
    "/".join(("C:", "Users", "__AGENT_CORE_SYNTHETIC__/other.txt")),
    SYNTHETIC + "/secret", SYNTHETIC + " other.txt",
    "/".join(("C:", "Users", "prefix__AGENT_CORE_SYNTHETIC__/fixture.txt")),
    '"C:' + '\\Program Files\\Private\\file"',
    '"C:' + '\\Program Files Extra"', '"C:' + '\\Program"',
])
def test_non_generic_values_stay_blocked(value):
    assert hits(value)


@pytest.mark.parametrize("safe, sensitive", [
    (EMAIL, "person" + "@" + "real-domain.com"),
    (SYNTHETIC, "/".join(("C:", "Users", "person/secret.txt"))),
    (HOME, "ghp_" + "a" * 24),
    (EMAIL, "-----BEGIN " + "PRIVATE KEY-----"),
])
def test_safe_neighbor_does_not_hide_another_match(safe, sensitive):
    assert hits('"' + safe + '" "' + sensitive + '"')
    assert hits('"' + sensitive + '" "' + safe + '"')


def test_owner_rules_are_never_exempted_even_when_reusing_shared_id():
    owner = privacy.Rule("owner_policy", re.compile(re.escape(EMAIL)))
    assert hits(EMAIL, rules=[*privacy._default_rules(), owner])[0].rule_id == "owner_policy"
    copied = privacy.Rule("email_address", re.compile(re.escape(EMAIL)))
    assert hits(EMAIL, rules=[copied])


def test_cli_owner_policy_and_default_remain_strict(tmp_path):
    specimen = tmp_path / "sample.txt"
    specimen.write_text(EMAIL)
    assert privacy.main(["--tree", str(specimen), "--strict"]) == 1
    assert privacy.main(["--tree", str(specimen), "--strict", "--publication"]) == 0
    rules = tmp_path / "owner.json"
    rules.write_text(json.dumps({"schema": privacy.SCHEMA_RULES, "rules": [
        {"id": "owner_policy", "kind": "literal", "value": EMAIL},
    ]}))
    assert privacy.main(["--tree", str(specimen), "--strict", "--publication", "--rules", str(rules)]) == 1


def test_git_fixture_and_bundle_forward_publication(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, timeout=30)
    git("init", "-q")
    git("config", "user.name", "Publication Test")
    git("config", "user.email", EMAIL)
    (repo / "sample.txt").write_text(EMAIL)
    git("add", "sample.txt")
    git("commit", "-qm", "Generic sample")
    bundle = tmp_path / "sample.bundle"
    git("bundle", "create", str(bundle), "--all")
    monkeypatch.chdir(repo)
    for mode, target in [("--git-repo", repo), ("--git-fixture", repo), ("--bundle", bundle)]:
        assert privacy.main([mode, str(target), "--strict"]) == 1
        assert privacy.main([mode, str(target), "--strict", "--publication"]) == 0


def test_existing_strict_suffix_nonmatch_is_unchanged():
    value = EMAIL + "_owner"
    assert hits(value) == hits(value, publication=False) == []
