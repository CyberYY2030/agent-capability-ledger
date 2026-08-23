# agent-core

`agent-core` V0.1 is a private, single-owner prototype for installing an agent capability engine, materializing private state into local runtimes, checking that setup, and using a private lessons ledger.

The exported `engine/` tree is also maintained as a small, reviewable public work. It demonstrates the portable mechanism without publishing private state or becoming part of the private runtime path.

## Requirements

- Python 3.11 or newer.
- A private state repository and reviewed host configuration for real use.

If `python` is unavailable on `PATH`, install Python or set `AGENT_CORE_PYTHON` for the current process to a compatible interpreter before invoking the wrapper. Do not persist a machine-specific bundled interpreter path as shared configuration.

## Source of truth

The private Git repository containing `engine/` and `state/` is the only runtime source of truth. Cross-machine synchronization uses ordinary Git operations on that private repository.

A public export checkout is a one-way whitelist export of `engine/`. It is a
publication artifact, never a runtime dependency, and never receives private
state, host bindings, credentials, sessions, caches, or machine-specific paths.

## V0.1 CLI

- `install` performs first private binding and receipt-owned engine updates through `absent`, `identical`, `managed-update`, `foreign`, and `indeterminate` states.
- `sync` materializes configured private state into local runtimes; Git itself carries that state between machines.
- `doctor` reports whether the configured engine, state, and runtime boundaries are healthy.
- `lessons` accesses the private lessons workflows behind the single supported lessons entry point.

`agent-core --version` remains available. Historical lifecycle, transaction, migration, maintenance, documentation, privacy, and uninstall entry points are frozen. Their implementation may remain in the source tree, but the public CLI rejects them before parsing or side effects:

```text
FAIL_COMMAND_FROZEN promote
```

The shortest supported journey is plan, apply, verify, then retrieve a lesson:

```console
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote --apply --plan-hash '<REVIEWED_INSTALL_PLAN_HASH>'
$ agent-core sync --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --apply
$ agent-core doctor --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>'
$ agent-core lessons match --stage prompt --text '<TASK>' --explain
```

The first two lines are the plan/apply phases of the single `install` command, so the user-facing surface remains four commands.

### Lessons promotion

`agent-core lessons promote --id <CANDIDATE>` reviews one local promotion and stops at the worktree/index boundary. Choose exactly one `--force-new` or scoped `--update <scope:store:lesson-id>`; `--scope global`, `--scope profile:<declared>`, and `--scope project:<current-project-id>` select the destination. An explicit same-repository override may move a project candidate into a bound global or declared profile ledger. A cross-repository request writes nothing and directs recapture in the target inbox. The reviewed plan hash binds the candidate, target, selected action, configuration, and exact staged paths. Apply uses the install transaction lock, writes the canonical ledger and consumed candidate only, and never syncs, commits, pushes, fetches, or contacts a remote.

## Installation preview

C1 installation planning and apply passed clean-tree acceptance in the isolated C2 environment. Preview every managed target before apply:

```console
$ python -m agent_core.cli install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote
PLAN operation=install version=<VERSION>
EXPECTED_REMOTE_SHA <REVIEWED_REMOTE_SHA>
PLAN_HASH <REVIEWED_INSTALL_PLAN_HASH>
TARGET <LABEL> status=absent|identical|managed-update|foreign|indeterminate path=<TARGET>
DRY_RUN writes=0 ready=true|false no_changes=true|false
```

Apply only a reviewed plan with `ready=true`:

```console
$ python -m agent_core.cli install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote --apply --plan-hash '<REVIEWED_INSTALL_PLAN_HASH>'
```

For a later receipt-owned update, omit `--confirm-private-remote` while the existing binding remains valid. After a valid engine or private-state change invalidates that binding, pass explicit `--state` and `--confirm-private-remote` to review and apply its transactional reacceptance with a new exact `PLAN_HASH`. `foreign` and `indeterminate` make the whole plan not ready and apply writes nothing. `managed-update` is allowed only when a valid receipt owns the exact path, type, hook identity, and current installed bytes. The installer has no force option. If an interrupted first binding leaves a pending marker, inspect its retained pre-image evidence, remove that host-local marker only after accepting it, then create and apply a new install plan; V0.1 never restores runtime, business, or config paths from that marker. Windows flushes pre-image files and uses no-replace placement, but has no portable directory-fsync equivalent and cannot promise zero overwrite against a non-cooperating writer that retains an open handle.

Always run and review the plan before `install --apply`. First binding and explicit reacceptance of an invalid binding require the confirmation flag; later valid bound installs do not. `sync` materializes private state after ordinary Git synchronization. Other real-machine environments remain pending acceptance.

## Current limits

- C1 clean-tree acceptance in C2 covered 222 `missing` targets, install, 222 `identical` targets, conflict zero-write behavior, the public no-force boundary, and injected-failure restoration of original bytes, prior absence, and residue cleanup.
- C2 acceptance includes live cutover on the current machine: 215 materializations classified as 7 `missing`, 150 `identical`, and 58 `conflict`, with `hook_conflict=0`; after the reviewed migration, all four installed wrapper commands returned zero, canonical bytes matched, and foreign hook fields were preserved.
- The earlier isolated run also covered doctor verification of the installed pin and manifest, ordinary Git push plus `pull --ff-only` before materialization, private lessons visibility, and a second sync with `writes=0`.
- A second full workspace and other real-machine evidence remain pending C3.
- C4 exported the engine through the one-way whitelist to `CyberYY2030/agent-capability-ledger` and passed the public privacy gate. Each subsequent release, including the export of current fixes, remains gated before publication.
- Complex transaction modules are retained as frozen implementation and are outside the V0.1 user path.

## Development safety

Repository governance, transaction invariants, and privacy gates still protect retained implementation. Engine changes also require synchronized release-manifest and provenance governance before any commit or publication.

## License

The engine source is licensed under Apache-2.0. Redistributions must preserve the notices required by `NOTICE`.
