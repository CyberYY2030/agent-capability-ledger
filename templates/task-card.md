# Task Card Template

Use this sink for checklist lessons that govern bounded implementation work. Seed consumers: L-2, L-6, L-8.

## Contract

- Goal:
- Inputs and authoritative sources:
- Outputs:
- Invariants and forbidden changes:
- Unknowns and decision points:

## Acceptance

- Focused positive case:
- Deliberate failing case:
- Broad gate:
- Evidence and completion label:

## Delivery checks

- Name the absolute engine root and list each final artifact explicitly; do not use a recursive scan or an unreviewed glob.
- For every final `.sh` artifact, run `python "$ENGINE_ROOT/templates/check_shell_format.py" "$ENGINE_ROOT/path/to/final.sh"` and record the command, exit status, and `PASS` output. Quote every path so spaces remain one argument.
- Record `SKIP ... reason=not-shell` only when a regular documentation or PowerShell artifact was deliberately included in the reviewed list. Treat missing paths, directories, symbolic links, unreadable files, and `FAIL ... reason=carriage-return` as delivery failures.
- Run the focused test first, then the card's broader consumer gates. Label only the behavior actually observed.

## Reverse validation

- Remove or bypass each guard independently and name the assertion that turns red.
- Restore the guard and rerun the focused and broad gates.
