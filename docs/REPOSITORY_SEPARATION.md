# Repository separation

The private monorepo is the only runtime source. Its `engine/` subtree is the
portable product; its `state/` subtree remains private runtime state.

A public checkout receives only a reviewed one-way whitelist export of
`engine/`. It is a release artifact, never a state remote or runtime dependency,
and must not receive private history, state, host bindings, or machine data.

The former private product checkout is a read-only archive. This workflow does
not install from it, synchronize it, clean it, or delete it.

Before a reviewed release operation, preserve a recoverable backup of the
private monorepo and record only privacy-safe role, commit, digest, command
result, and reviewer evidence. Do not record local paths, account identifiers,
remote addresses, credentials, or machine names in the public checkout.
