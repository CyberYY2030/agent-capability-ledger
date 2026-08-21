---
name: first-divergence-debugging
description: Use when behavior is wrong, missing, stale, failed, or otherwise unexpected, even if the report names a suspected layer.
---

# First-Divergence Debugging

Read the relevant installed case-law rules first [CASE-1].

## Workflow

1. Define expected behavior, observed behavior, and the invariant that distinguishes them.
2. Draw the shortest path from known-good input to wrong output and mark each consumer gate [CASE-2].
3. Starting upstream, record the last correct value and the first incorrect value.
4. Run the cheapest check that separates adjacent hypotheses.
5. Fix only the first gate that changes correct state into incorrect state.
6. Add a regression that fails before the fix and passes after it.
7. Check every consumer of a shared definition and search the old literal itself [CASE-3].
8. Match verification width to impact and stop after three no-signal attempts in one direction.

## Guardrails

- A presentation fix cannot repair incorrect upstream state.
- Removing an entire filter to admit one new valid input silently broadens the contract.
- A blocked end-to-end environment does not erase focused evidence; report the two layers separately.

Delivery states the first divergent gate, adjacent gates ruled out, minimal fix, actual test results, and the evidence-backed completion label.
