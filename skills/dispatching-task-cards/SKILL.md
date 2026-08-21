---
name: dispatching-task-cards
description: Use when implementing approved task cards, changing shared definitions, or deciding whether bounded work may run in parallel.
---

# Dispatching Task Cards

Read the relevant rules in the installed `CASE_LAW.md` before dispatch [CASE-6]. Cases explain the cost of a rule; they do not expand scope.

## Truth card

Freeze these fields before implementation:

- definition: inputs, outputs, state meanings, and exclusions;
- invariants: formulas, safety gates, and fail-closed behavior;
- evidence: authoritative documents, code seams, and verified samples;
- topology: repositories, data channels, consumers, and writers;
- unknowns: mark `unproven`, `partial`, or `environment-blocked` explicitly;
- acceptance: runnable assertions, commands, and the minimum completion label;
- lessons input: resolve only global, declared profile, and current-project sources;
- lessons decision: `create`, `update`, `promote`, or `skip`, plus the narrowest valid scope.

Stop and request a decision when any field can change scope, ownership, privacy, or long-term maintenance [CASE-5].

## Ownership and ordering

- One card produces one independently reversible commit [CASE-4].
- Cards sharing a file or schema owner run serially.
- Parallel cards require disjoint writable paths and one integration owner.
- Inspect each card commit for unrelated changes before integration.

## Acceptance

- Run the smallest focused regression, then every broader gate required by the card.
- A shared literal or definition change must search the old literal and all consumers, including tests and comments [CASE-3].
- Report passed, failed, and environment-blocked evidence separately.
- Never upgrade a completion label beyond the evidence actually observed.
