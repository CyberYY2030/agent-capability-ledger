# agent-core

`agent-core` is a personal alpha for keeping reusable agent rules, skills, and
lessons in one private Git source, delivering them to local agent runtimes,
and checking what was actually installed. Lessons are retrieved for a task so
useful guidance can reach the agent without loading the whole ledger.

The public repository is a reviewable engine export. Real use requires your own
private state and host configuration; this checkout contains neither. It is a
single-owner prototype, not a hosted service. Candidate version: **0.1.0.dev9**.

## Try a synthetic lesson (no installation)

From this checkout's root, with Python 3.11 or newer:

```sh
python3 -m agent_core.cli --version
python3 -m agent_core.cli lessons match --ledger seed/profiles/example-domain/LESSONS.md --workspace seed --stage prompt --text "public fixture" --explain
```

The version is `0.1.0.dev9`; the retrieval output includes `EXAMPLE-1` and
`Keep public fixtures synthetic.` The supplied ledger is **synthetic demonstration
data**. The explicit ledger and workspace keep this read-only example separate
from your installed private sources. No install, sync, account, or host config is
needed. On Windows use a compatible `python` interpreter in place of `python3`.
This demonstrates retrieval mechanics, not natural-work recall, P1 samples, or
business benefit.

See [alpha changes and evidence limits](docs/ALPHA.md) for the changes since dev2.

## First installation with your own private data

Use Python 3.11 or newer and Git. You need a private Git remote you control,
a new private workspace, a separate host-config location, and the target
runtime directory for Claude Code or Codex. The public checkout supplies only
engine code and synthetic seed data; it does not need access to the author's
private repository.

Start with the source-distributed preparation helper:

```sh
python3 examples/prepare_private.py --help
```

Follow the [complete first-installation walkthrough](docs/QUICKSTART.md). It
prepares a new private `engine/` and `state/` repository and host config, then
uses the existing reviewed `install` flow. Preparation never overwrites an
existing workspace/config, contacts a remote, or activates a runtime. You
confirm your own private remote and publish your own initial private commit
before binding it. No frozen `state init` or `state attach` CLI is needed.

The helper is optional source setup, not a fifth installed command. Existing
users retain `install`, `sync`, `doctor`, and `lessons`. Host approval/trust for
hooks is still a user action; a successful shell test cannot grant that trust
or prove a Desktop app emitted an event.

If `python3` is unavailable, use a compatible `python` interpreter. The wrappers
also accept `AGENT_CORE_PYTHON` for the current process. Do not store a
machine-specific bundled interpreter path in shared configuration.

## Source of truth

The private Git repository containing `engine/` and `state/` is the only runtime source of truth. Cross-machine synchronization uses ordinary Git operations on that private repository.

A public export checkout is a one-way whitelist export of `engine/`. It is a
publication artifact, never a runtime dependency, and never receives private
state, host bindings, credentials, sessions, caches, or machine-specific paths.

## V0.1 CLI

- `install` performs first private binding and receipt-owned engine updates. Runtime rows use the shared materialization receipt; engine, wrapper, pin, and hook rows retain the install receipt.
- `sync` materializes configured private state into local runtimes and publishes `materialization-receipt/1`; Git itself carries private state between machines.
- `doctor` verifies the configured engine, state, active and retained materialization rows, pending markers, and runtime bytes.
- `lessons` accesses the private lessons workflows behind the single supported lessons entry point.

`agent-core --version` remains available. Historical lifecycle, transaction, migration, maintenance, documentation, privacy, and uninstall entry points are frozen. Their implementation may remain in the source tree, but the public CLI rejects them before parsing or side effects:

```text
FAIL_COMMAND_FROZEN promote
```

The shortest supported journey is plan, apply, verify, then retrieve a lesson:

```console
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote
$ agent-core install --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --source '<ENGINE>' --artifact-manifest '<MANIFEST>' --confirm-private-remote --apply --plan-hash '<REVIEWED_INSTALL_PLAN_HASH>'
$ agent-core sync --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>'
$ agent-core sync --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>' --apply --plan-hash '<REVIEWED_SYNC_PLAN_HASH>'
$ agent-core doctor --config '<HOST_CONFIG>' --state '<PRIVATE_STATE>'
$ agent-core lessons match --stage prompt --text '<TASK>' --explain
```

The first two lines are the plan/apply phases of the single `install` command, so the user-facing surface remains four commands.

### Lessons promotion

`agent-core lessons promote --id <CANDIDATE>` reviews one local promotion and stops at the worktree/index boundary. Choose exactly one `--force-new` or scoped `--update <scope:store:lesson-id>`; `--scope global`, `--scope profile:<declared>`, and `--scope project:<current-project-id>` select the destination. An explicit same-repository override may move a project candidate into a bound global or declared profile ledger. A cross-repository request writes nothing and directs recapture in the target inbox. The reviewed plan hash binds the candidate, target, selected action, configuration, and exact staged paths. Apply uses the install transaction lock, writes the canonical ledger and consumed candidate only, and never syncs, commits, pushes, fetches, or contacts a remote.

## Installation preview

Installation planning and apply passed acceptance in an isolated environment. Preview every managed target before apply:

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

For a later receipt-owned update, omit `--confirm-private-remote` while the existing binding remains valid. After a valid engine or private-state change invalidates that binding, pass explicit `--state` and `--confirm-private-remote` to review and apply its transactional reacceptance with a new exact `PLAN_HASH`. `foreign` and `indeterminate` make the whole plan not ready and apply writes nothing. Runtime `managed-update` requires the shared receipt's exact root/path/current SHA; a legacy install runtime row is accepted only once while the shared receipt is absent and its SHA exactly matches current bytes. Exact unowned desired bytes are visibly adopted without a runtime rewrite. The installer has no force option. A pending install or materialization marker blocks both writers until its retained evidence is inspected and the marker is deliberately cleared. Windows flushes pre-image files and uses no-replace placement, but has no portable directory-fsync equivalent and cannot promise zero overwrite against a non-cooperating writer that retains an open handle.

Always run and review the plan before `install --apply` or `sync --apply`. First binding and explicit reacceptance of an invalid binding require the confirmation flag; later valid bound installs do not. `sync` materializes private state after ordinary Git synchronization and requires the exact `PLAN_HASH` printed by its dry-run. Removed desired paths remain as verified `retained` rows; retained drift blocks the whole plan, and uninstall never rewinds runtime bytes through an old install snapshot. Evidence is limited to the tested hosts and versions described below.

## Verified scope and limits

- Earlier isolated Windows acceptance covered installation plans, conflict
  zero-write behavior, reviewed apply, injected-failure restoration, and
  repeated sync with zero writes. Live Windows materialization and diagnostics
  were also accepted.
- The dev7 macOS and cross-machine closeout was accepted on 2026-09-09. It
  covered private Git propagation, receipt/byte parity, zero-write follow-up,
  and native Claude and Codex Desktop prompt delivery on the tested Mac.
  This is a sanitized summary of private reviewed evidence, not a new dev9
  live installation result or a claim about arbitrary machines or app versions.
- Current Windows Desktop automatic dispatch remains unproven. A registered
  hook or a successful shell replay does not prove a host emitted an event.
- Natural-work retrieval quality, sustained reduction in rework, and benefits
  exceeding lesson-maintenance costs remain unproven. Synthetic tests and the
  demonstration above do not establish those outcomes.
- Installation needs a clean reviewed private source window. Some Git failure
  diagnostics remain coarse. The release manifest binds content, not POSIX
  executable mode; delivery checks must also inspect required execution bits.
- Historical transaction, migration, and uninstall modules remain frozen and
  outside the four-command public path. `docs/commands.json` is a retained
  historical development contract containing frozen entries, not a quickstart.
- Every public update requires the [export/privacy/review gate](docs/PUBLICATION.md). Preparing
  this source does not activate it in an existing runtime.

## Development safety

Repository governance, transaction invariants, and privacy gates still protect retained implementation. Engine changes also require synchronized release-manifest and provenance governance before any commit or publication.

## License

The engine source is licensed under Apache-2.0. Redistributions must preserve the notices required by `NOTICE`.
