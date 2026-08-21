# Repository separation

`agent-core` is the runtime product. The public engine repository currently uses the `agent-capability-ledger` slug; the private state repository is `agent-core`. Repository slugs are movable addresses, not code identities: engine and state roles are established by remote identity comparison.

## Separation invariants

- The public engine repository and the private state repository are separate from their creation.
- The engine never renames an account repository, changes a real remote, or deletes unknown private-state content.
- The public engine repository is never used as a state remote.
- A state remote must remain private and must never identify the public engine repository.

## Recovery before remote risk

Before any operation that can change a remote, create a bundle backup from the private state checkout and prove that it is recoverable. Keep the verified bundle until the operator accepts the result.

```text
git -C <STATE_CHECKOUT> bundle create <BACKUP>/state-before-remote-change.bundle --all
git -C <STATE_CHECKOUT> bundle verify <BACKUP>/state-before-remote-change.bundle
git clone <BACKUP>/state-before-remote-change.bundle <DISPOSABLE_RESTORE>
git -C <DISPOSABLE_RESTORE> fsck --full
```

The restore must pass `git fsck --full` before the remote operation proceeds. Record only privacy-safe evidence: repository role, commit identifier, bundle digest, command result, and reviewer decision. Do not record absolute paths, account identifiers, remote addresses, credentials, or machine names in the public engine repository.
