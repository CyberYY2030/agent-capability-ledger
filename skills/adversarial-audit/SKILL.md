---
name: adversarial-audit
description: Use when auditing permissions, recovery, data integrity, or another high-impact surface for reproducible system defects.
---

# Adversarial Audit

Read the relevant installed case-law rules before auditing, especially the high-impact review contract [CASE-6]. Keep evidence gathering separate from severity judgment and remediation.

## Defect prototypes

Inspect in blast-radius order:

1. fail-open: broken configuration, data, or dependencies incorrectly allow progress;
2. dimension mismatch: units, identifiers, accounts, or key spaces mix at a boundary;
3. restart amnesia: memory-only state lacks an equivalent recovery path;
4. contract without a consumer: a rule, error, or schema has no real execution path [CASE-7].

## Evidence contract

- Trace the shortest control or data path and cite the first divergent boundary.
- Reproduce each candidate defect before assigning a final severity.
- Preserve an explicit list of audited surfaces with no confirmed defect.
- Do not patch during the audit; approved remediation receives a separate task card [CASE-4].
- Label unobserved real-environment behavior as unproven.

Stop when every scoped surface has a verdict or when evidence cannot distinguish high-impact explanations without a user decision.
