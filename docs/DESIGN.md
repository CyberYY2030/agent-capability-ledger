# Design decisions

This document records the selected boundaries, the rejected alternatives, and the costs that remain visible to an operator.

## 1. Placement follows portability

- Decision: place public mechanism, private state content, and host-only paths in separate layers.
- Rejected alternative: synchronize complete dotfiles directories.
- Cost: configuration must declare which layer owns each path, and host-specific paths are populated separately.

## 2. Retrieval is deterministic lexical matching

- Decision: normalize Unicode, use CJK bigrams and predicates, and order results from lexical matches.
- Rejected alternative: vector retrieval.
- Cost: queries depend on declared words and predicates; semantic similarity is not used to select a result.

## 3. The tool does not edit the ledger automatically

- Decision: capture creates an immutable candidate and a person decides whether promotion writes canonical content.
- Rejected alternative: automatic persistence to the ledger.
- Cost: promotion requires review and an explicit transaction before canonical content changes.

## 4. Capture writes to inbox before canonical content

- Decision: write captured material to the append-only inbox, then promote a reviewed candidate.
- Rejected alternative: direct writes to canonical content.
- Evidence: in incident B, two agent sessions implemented the same integration work from the same baseline; only one result could become canonical after manual comparison.
- Cost: candidate review and promotion add a separate step before canonical content changes.

## 5. The retrieval budget is measured in characters

- Decision: limit retrieval output by character count.
- Rejected alternative: limit output by model tokens.
- Cost: the limit is not expressed in a model-specific token unit.

## Trust boundaries

V1 does not import third-party lessons or skills automatically. State consumes only content that its owner explicitly places and marks as trusted; unknown sources are rejected from injection and installation. Text normalization does not create trust for instructional content. Any future import capability requires a separate threat model, source or signature design, preview, and approval work.
