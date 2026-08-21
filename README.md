# agent-core

`agent-core` V0.1 is a private, single-owner prototype for installing an agent capability engine, materializing private state into local runtimes, checking that setup, and using a private lessons ledger.

The exported `engine/` tree is also maintained as a small, reviewable public work. It demonstrates the portable mechanism without publishing private state or becoming part of the private runtime path.

## Requirements

- Python 3.11 or newer.
- A private state repository and reviewed host configuration for real use.

If `python` is unavailable on `PATH`, install Python or set `AGENT_CORE_PYTHON` for the current process to a compatible interpreter before invoking the wrapper. Do not persist a machine-specific bundled interpreter path as shared configuration.

## Source of truth

The private Git repository containing `engine/` and `state/` is the only runtime source of truth. Cross-machine synchronization uses ordinary Git operations on that private repository.

A public repository, when C4 is accepted, is a one-way whitelist export of `engine/`. It is a publication artifact and never a runtime dependency. Private state, host bindings, credentials, sessions, caches, and machine-specific paths must not enter that export.

## V0.1 CLI

- `install` plans or applies the engine to configured local runtimes through the accepted C1 `missing`, `identical`, and `conflict` states.
- `sync` materializes configured private state into local runtimes; Git itself carries that state between machines.
- `doctor` reports whether the configured engine, state, and runtime boundaries are healthy.
- `lessons` accesses the private lessons workflows behind the single supported lessons entry point.

`agent-core --version` remains available. Historical lifecycle, transaction, migration, maintenance, documentation, privacy, and uninstall entry points are frozen. Their implementation may remain in the source tree, but the public CLI rejects them before parsing or side effects:

```text
FAIL_COMMAND_FROZEN promote
```

The shortest supported journey is plan, apply, verify, then retrieve a lesson:

```console
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>'
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --apply
$ agent-core sync --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --apply
$ agent-core doctor --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>'
$ agent-core lessons match --stage prompt --text '<TASK>' --explain
```

The first two lines are the plan/apply phases of the single `install` command, so the user-facing surface remains four commands.

## Installation preview

C1 installation planning and apply passed clean-tree acceptance in the isolated C2 Windows environment. Preview every managed target before apply:

```console
$ python -m agent_core.cli install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>'
PLAN operation=install version=<VERSION>
TARGET <LABEL> status=missing|identical|conflict path=<TARGET>
DRY_RUN writes=0 ready=true|false no_changes=true|false
```

Apply only a reviewed plan with `ready=true`:

```console
$ python -m agent_core.cli install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --apply
```

`conflict` means user-owned or previously managed bytes differ. Stop, resolve every reported target manually, then plan again. The public installer has no force option and does not recommend overwriting conflicts.

Always run and review the plan before `install --apply`. Live runtime cutover has not been executed; stop whenever the plan reports a conflict.

## Current limits

- C1 clean-tree acceptance in C2 covered 222 `missing` targets, install, 222 `identical` targets, conflict zero-write behavior, the public no-force boundary, and injected-failure restoration of original bytes, prior absence, and residue cleanup.
- C2 isolated Windows acceptance covered the installed four-command surface, doctor verification of the installed pin and manifest, ordinary Git push plus `pull --ff-only` before materialization, private lessons visibility, and a second sync with `writes=0`.
- Live runtime cutover has not been executed. A second full workspace and macOS evidence remain pending C3; the isolated same-machine evidence is not cross-machine or macOS proof.
- Public whitelist export, privacy verification, release packaging, and publication approval are pending C4.
- Complex transaction modules are retained as frozen implementation and are outside the V0.1 user path.

## Development safety

Repository governance, transaction invariants, and privacy gates still protect retained implementation. Engine changes also require synchronized release-manifest and provenance governance before any commit or publication.

## License

The engine source is licensed under Apache-2.0. Redistributions must preserve the notices required by `NOTICE`.
