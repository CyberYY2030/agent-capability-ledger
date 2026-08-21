# Agent Core V0.1 Plan

V0.1 deliberately has one private runtime truth and four user commands. The private monorepo is synchronized between machines with ordinary Git. A future public repository is created only from an `engine/` whitelist export and never participates in runtime operation.

The earlier multi-command lifecycle plan remains available in Git history. Its transaction and migration implementation stays in the tree as frozen code, outside the V0.1 CLI path.

## C0 — Product surface

Limit top-level help to `install`, `sync`, `doctor`, and `lessons`, while retaining `--version`. Reject every known historical or maintenance entry point with `FAIL_COMMAND_FROZEN <command>`, exit 2, before argument parsing, locks, Git access, or file writes. Keep transaction modules intact. Prove the surface and upstream rejection with focused entrypoint tests.

Acceptance boundary: local source and tests only. No manifest/provenance update, commit, remote action, runtime mutation, or publication belongs to C0.

## C1 — Safe installation states

Status: accepted in the C2 isolated clean-tree Windows run.

The public installer classifies every prospective managed target as `missing`, `identical`, or `conflict`. A plan reports all conflicts with `writes=0`; apply has no force option, rejects a non-ready plan before freshness, snapshot, or target writes, skips identical targets, and snapshots every prospective write. Injected post-write failure must restore safe existing hook bytes, remove targets that were previously absent, and leave no receipt, install root, or rollback residue.

Acceptance evidence: a fresh plan classified 222 targets as `missing`; install completed and the next plan classified all 222 as `identical`. Aggregate conflicts remained zero-write and the public CLI exposed no force option. An injected post-write failure restored original bytes, removed targets that were absent before the attempt, and left no receipt, install-root, snapshot, or rollback residue. This isolated evidence does not prove live runtime cutover, a second full workspace, or macOS.

## C2 — Windows end-to-end

Status: accepted in an isolated Windows environment; live runtime cutover has not been executed.

Acceptance evidence: the installed wrapper exercised the four-command surface; doctor verified the installed artifact through its pin and release manifest while retaining private state, composition, consumer, and hook checks. A synthetic private-state update moved through an ordinary Git push and `git pull --ff-only`, after which `sync --apply` materialized it into both runtimes and installed `lessons` retrieved it. An immediate second sync reported `APPLIED writes=0` and `PASS backup_created=False` with all materialized regular-file records unchanged.

Acceptance boundary: isolated same-machine Windows clones and temporary runtimes. C2 does not prove live cutover, a second full workspace, macOS, cross-machine operation, or public release.

## C3 — Second workspace and macOS

Status: pending.

Repeat the accepted flow from a distinct full workspace and then on macOS. Compare the user-visible outcome and document platform-specific limits without treating synthetic or same-machine evidence as cross-machine proof.

Acceptance boundary: independently observed environments. Missing macOS access remains an explicit environment blocker.

C3 remains required evidence for second-workspace and macOS claims, but it does not block publishing the already accepted isolated Windows V0.1 work with those limits stated explicitly.

## C4 — Public export and release

Status: pending.

Fresh-clone the existing `CyberYY2030/agent-capability-ledger` public repository, overlay only the explicit `engine/` export whitelist, and preserve the public repository's own `.git` history. Run privacy and provenance gates on the candidate tree and reachable public history, verify release packaging, then use an ordinary commit and push only after every gate passes. Never copy private history, force-push, include private state, or make the public repository a dependency of the private runtime.

Acceptance boundary: publication occurs only after all export, privacy, provenance, and release checks pass. Second-workspace and macOS validation remain explicitly pending C3 and must not be claimed by the Windows publication.
