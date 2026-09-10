# Alpha candidate: 0.1.0.dev9

This candidate updates the public dev2 engine with the implemented changes below.
It is prepared from the private engine source; publication and runtime activation
are separate reviewed actions. No private data or Git history accompanies it.

## Changes since dev2

| Capability | Implemented behavior | Evidence boundary |
| --- | --- | --- |
| Shared runtime ownership | Install and sync use a shared materialization receipt, reviewed plan tokens, drift checks, and retained-path checks. Identical content avoids rewrites. | Focused install/sync tests; bounded private Windows and Mac acceptance. |
| POSIX delivery | Required hook execution bits, installed launcher fallback, and Python version checks; doctor rejects non-executable hooks. | Focused tests and accepted dev7 Mac native host evidence. The manifest itself does not bind modes. |
| Task-local retrieval | Workspace declarations select profile/project sources; default source resolution and empty-result diagnostics are explicit. Retrieval diagnostics and noisy-term filtering are included. | Synthetic routing/matcher tests. No claim of improved natural-work recall. |
| Reviewed lesson evolution | Promotion previews the resulting body and can bind explicit retrieval predicates. Local candidate rejection is available through `lessons reject`. | Focused promotion/capture/reject tests. Promotion stops at the local ledger/index boundary. |
| Failure reporting | More actionable remote parity failures and transport diagnostics. | Focused regressions. Some timeout and binding-error classifications remain coarse. |
| First-use preparation | An optional source helper prepares a new owner-controlled private engine/state repository and separate host config, then hands off to existing reviewed install. | Isolated first-use tests; no existing runtime activation or new Desktop event proof. |
| Publication policy | Explicit publication classification recognizes narrowly defined generic literals; additional private owner rules remain enforced. | Two-sided publication tests and unchanged default strict/capture checks. |
| Public explanation | A read-only synthetic retrieval demo and corrected bounded Mac/cross-machine acceptance summary. | Candidate demo and privacy CI tests; no dev9 live activation. |

The first five rows were already implemented before this documentation candidate.
This update adds a fresh private-workspace preparation helper and explicit
public-source privacy classification. It does not tune the matcher, change
candidate lifecycle behavior, or unfreeze transaction modules.

## Evidence and remaining limits

The reviewed dev7 closeout accepted the tested Windows/Mac propagation path and
native Claude/Codex Desktop prompt delivery on the tested Mac. Public summaries
omit private host configuration, source ledgers, reports, prompts, and samples.
Current Windows Desktop automatic dispatch is still unproven.

P1 is an ongoing natural-work evaluation. Retrieval accuracy, reduced rework,
and whether benefits exceed maintenance cost are not yet established. The
README's EXAMPLE-1 is synthetic data and is never counted as a P1 observation.

Real installation uses the [first-use walkthrough](QUICKSTART.md) to prepare
your private state/configuration, followed by reviewed binding. Historical transaction, uninstall, and maintenance entry points remain
frozen; `docs/commands.json` is retained historical developer material and is
not the supported install guide. Use the README for the current four-command
surface. A full historical test-suite pass is not claimed.

## Reproduce the public gate

From the public checkout root, create an isolated Python 3.11+ environment and
install `requirements-dev.txt` in it. Then use that environment's `python`:

```sh
python -m pytest tests/test_entrypoint.py tests/test_public_content_contract.py tests/test_publication_policy.py tests/test_public_setup.py tests/test_privacy.py::PrivacyTreeTests tests/test_privacy.py::PrivacyGitTests tests/test_privacy.py::RuleContractTests -q
python -m agent_core.privacy --tree . --publication --strict
python -m agent_core.privacy --git-repo . --publication --strict
```

These are developer-module privacy commands; the top-level privacy command is
frozen. Maintainers must additionally supply their explicit private owner/company
rules outside this checkout. Required rules must exist and load successfully;
public-only scans are not a replacement. No historical allowlist is required. Review the exact staged tree,
removed paths, final diff, and reachable public history before any push.
