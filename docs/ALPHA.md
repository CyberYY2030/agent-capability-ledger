# Alpha candidate: 0.1.0.dev12

This candidate repairs Linux directory publication in the dev11 first-use
journey while retaining the lessons delivery, evaluation, and shell-format work.
It uses the kernel no-replace rename operation and fails closed when that
operation is unavailable; an existing target is never overwritten. Publication and runtime
activation are separate reviewed actions. No private data or private Git
history accompanies the public export.

## Changes since public dev2

| Capability | Implemented behavior | Evidence boundary |
| --- | --- | --- |
| Shared runtime ownership | Install and sync use a shared materialization receipt, reviewed plan tokens, drift checks, and retained-path checks. Identical content avoids rewrites. | Focused install/sync tests and bounded private host acceptance. |
| POSIX delivery | Required hook execution bits, installed launcher fallback, and Python version checks; doctor rejects non-executable hooks. | Focused tests and accepted dev7 Mac native host evidence. The manifest itself does not bind modes. |
| Task-local retrieval | Workspace declarations select profile/project sources; default source resolution and empty-result diagnostics are explicit. | Synthetic routing and matcher tests. |
| Reviewed lesson evolution | Promotion previews the resulting body and binds scope, candidate bytes, canonical preimage, action, and exact staged paths. | Focused promotion/capture tests. Apply remains local and does not sync, commit, push, fetch, or contact a remote. |
| Host delivery context | Prompt hooks remain text; PreToolUse emits JSON `additionalContext`; Stop remains empty while completion capture warnings use stderr. | Adapter tests for supported event shapes. Actual host emission and later model adoption remain host-specific. |
| Rendered evaluation | Development evaluation distinguishes matcher results from the entries that survive production rendering and budget limits. | Synthetic fixtures; no claim of natural-work value. |
| Shell delivery check | A dependency-free checker rejects CR or CRLF bytes in explicit regular `.sh` inputs without executing or modifying them. | Focused checker tests and CI invocation. It covers only this shell-file subproblem. |
| First-use preparation | An optional source helper prepares a new owner-controlled private engine/state repository and separate host config, then hands off to reviewed install. | Isolated first-use tests; no existing runtime activation or new Desktop event proof. |
| Publication policy | Explicit publication classification recognizes narrowly defined generic literals; additional private owner rules remain enforced. | Two-sided publication tests and unchanged default strict/capture checks. |

## Evidence and remaining limits

The reviewed dev7 closeout accepted the tested Windows/Mac propagation path and
native Claude and Codex Desktop prompt delivery on the tested Mac. Public
summaries omit private host configuration, source ledgers, reports, prompts,
and samples. Current Windows Desktop automatic dispatch remains unproven.

Natural-work retrieval quality, reduced rework, and whether benefits exceed
maintenance cost are not established. The README's `EXAMPLE-1`, evaluation
fixtures, and shell-format demonstration are controlled development evidence;
none counts as a natural-work observation.

Real installation uses the [first-use walkthrough](QUICKSTART.md) to prepare
private state and configuration, followed by reviewed binding. Historical
transaction, uninstall, and maintenance entry points remain frozen;
`commands.json` is retained historical developer material rather than the
supported install guide.

## Reproduce the public gate

From a public candidate checkout, create an isolated Python 3.11+ environment,
install `requirements-dev.txt`, and use that environment's `python`:

```sh
python -m pytest tests/test_entrypoint.py tests/test_public_content_contract.py tests/test_publication_policy.py tests/test_public_setup.py tests/test_no_replace.py tests/test_shell_format.py tests/test_privacy.py::PrivacyTreeTests tests/test_privacy.py::PrivacyGitTests tests/test_privacy.py::RuleContractTests -q
python templates/check_shell_format.py install.sh runtimes/generic/user_prompt.sh
python -m agent_core.privacy --tree . --publication --strict
python -m agent_core.privacy --git-repo . --publication --strict
```

These are developer-module privacy commands; the top-level privacy command is
frozen. Maintainers additionally supply explicit private owner/company rules
from outside the public checkout. Required rules must exist and load
successfully; default-only scans do not replace them. Review the exact export,
removed paths, final diff, and reachable public history before any push.
