# Publishing an alpha update

The public repository contains the portable engine and synthetic examples.
It must not contain an owner's personal/company data, private state, config,
credentials, runtime receipts, sessions, business datasets, or private history.
License notices, public authorship and normal product implementation remain.

## Two privacy uses

The default scanner is deliberately strict, and capture keeps its existing
strict rules. Public-source scanning explicitly uses `--publication`: it can
recognize unexpanded home-variable expressions, enumerated system paths,
reserved test identities, and a fixed synthetic fixture path. This changes
source-publication classification only. It does not weaken capture or admit
an arbitrary file because it is under `tests/` or labeled synthetic. Real
credentials and private keys remain findings, including beside safe examples.

From the candidate root, using the isolated Python environment:

```sh
python -m agent_core.privacy --tree . --publication --strict
python -m agent_core.privacy --git-repo . --publication --strict
```

Maintainers also run those commands with `--rules` pointing to their explicit
private owner/company rules outside this checkout. Missing or invalid required
rules block that release; a default-only run cannot stand in for them. These
private rules and raw scan findings are never copied here.

Automated scanning cannot identify every confidential business fact. A reviewer
checks the exact export, removed paths, final diff and reachable public history.
Whenever publication semantics change, first compare old and new results on
the already-published history. Keep both sensitive and normal-content regression
cases; do not weaken assertions or use blanket or periodically renewed history
exemptions to manufacture a pass.

Source preparation, public publication and activation on an existing machine
are distinct actions. Passing a synthetic demonstration does not establish
natural-task retrieval quality or economic benefit.
