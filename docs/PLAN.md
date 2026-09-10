# Agent Core V0.1 Plan

V0.1 deliberately has one private runtime truth and four user commands. The private monorepo uses ordinary Git for synchronization; bounded cross-machine acceptance is recorded below. The public `CyberYY2030/agent-capability-ledger` repository receives only a one-way `engine/` whitelist export and never participates in runtime operation.

The earlier multi-command lifecycle plan remains available in Git history. Its transaction and migration implementation stays in the tree as frozen code, outside the V0.1 CLI path.

## C0 — Product surface

Limit top-level help to `install`, `sync`, `doctor`, and `lessons`, while retaining `--version`. Reject every known historical or maintenance entry point with `FAIL_COMMAND_FROZEN <command>`, exit 2, before argument parsing, locks, Git access, or file writes. Keep transaction modules intact. Prove the surface and upstream rejection with focused entrypoint tests.

Acceptance boundary: local source and tests only. No manifest/provenance update, commit, remote action, runtime mutation, or publication belongs to C0.

## C1 — Safe installation states

Status: accepted in the C2 isolated clean-tree Windows run.

`install` owns first private binding and receipt-owned engine updates. Every ready install or sync plan emits an opaque `PLAN_HASH` and reviewed remote revision; apply requires that exact token and rebuilds the same facts before writing. First binding, and explicit reacceptance after a valid engine or private-state change invalidates an existing binding, require confirmation, a read-only plan, then tokenized apply. Runtime paths are classified from `materialization-receipt/1` as receipt-owned identical, managed update, bootstrap, absent, visible exact adoption, foreign, or indeterminate; the final two states make the entire plan not ready with zero writes. The receipt binds config/state/repository identity, generation, transaction identity, and every active or retained path SHA. `sync` and `install` share one host lock; standalone sync publishes its receipt after verified runtime writes, while install publishes the materialization receipt before the final install receipt. Pending markers and durable snapshots guard rollback. Removed desired paths are carried as retained rows after current-SHA reproof, retained drift blocks the plan, and uninstall retains runtime bytes and this receipt while removing install-owned objects. macOS acceptance is bounded by the C3 closeout below.

Acceptance evidence: a fresh plan classified 222 targets as `missing`; install completed and the next plan classified all 222 as `identical`. Aggregate conflicts remained zero-write and the public CLI exposed no force option. An injected post-write failure restored original bytes, removed targets that were absent before the attempt, and left no receipt, install-root, snapshot, or rollback residue. C2 later added live Windows evidence; subsequent macOS and cross-machine evidence is summarized under C3.

## C2 — Windows end-to-end

Status: accepted in isolated Windows testing and live on the current Windows machine.

Acceptance evidence: the isolated run exercised the installed four-command surface; doctor verified the installed artifact through its pin and release manifest while retaining private state, composition, consumer, and hook checks. A synthetic private-state update moved through an ordinary Git push and `git pull --ff-only`, after which a reviewed sync apply materialized it into both runtimes and installed `lessons` retrieved it. An immediate second sync reported `APPLIED writes=0` and `PASS backup_created=False` with all materialized regular-file records unchanged. The live cutover classified 215 materializations as 7 `missing`, 150 `identical`, and 58 `conflict`, with `hook_conflict=0`; all four installed wrapper commands returned zero, canonical bytes matched, and foreign hook fields were preserved.

Acceptance boundary: live Windows operation is accepted for the current machine. C2 does not prove a second full workspace, macOS, or cross-machine operation.

## C3 — Second workspace and macOS

Status: accepted in the reviewed dev7 closeout on 2026-09-09.

The tested Mac completed private-state propagation, runtime byte/receipt parity,
zero-write follow-up, and native Claude and Codex Desktop prompt delivery.
Cross-machine acceptance covers the tested Windows and Mac pair. Current
Windows Desktop automatic dispatch remains unproven. The private evidence is
summarized without publishing host identities, prompts, paths, or reports.

Acceptance boundary: those tested hosts and versions only. This does not prove
natural-work retrieval quality, business benefit, or a new candidate's live
installation. Candidate preparation leaves the ongoing private cohort frozen.

## C4 — Public export and release

Status: accepted for the current public release; every later release remains gated.

The existing `CyberYY2030/agent-capability-ledger` public repository received only the explicit `engine/` export whitelist while preserving its own `.git` history. The candidate tree and reachable public history passed the privacy gate before an ordinary publication. Never copy private history, force-push, include private state, or make the public repository a dependency of the private runtime. Current fixes will be exported only through the same whitelist and must pass the release gates again.

Acceptance boundary: every publication occurs only after all export, privacy, provenance, and release checks pass. The earlier Windows publication did not itself establish C3; subsequent bounded C3 evidence is described above.
