---
name: consolidate
description: Run stage 4 of the migration factory – propose shared semantic models from the IR files with the source-table scoring and hard conditions C1–C6 of the build spec, and write consolidation.yaml plus a review table for the Tech Lead. Use only when the team runs /consolidate.
disable-model-invocation: true
---

# Consolidate – stage 4

Apply section Consolidation rules of the build spec; thresholds live in config/thresholds.yaml.

1. Build the dashboard × source-table matrix; tag fact and dimension tables.
2. Score pairs with weighted Jaccard on fact tables, sizes from cube sizes and table statistics.
3. Merge greedily from the highest score: subset (1.0) or overlap of 0.6 or more.
4. Check C1–C6. C1, C4, C5 and C6 block; a C2 or C3 failure becomes a proposal with FILTER_DIVERGENCE or ROLE_PLAYING.
5. Write consolidation.yaml with status `proposed`, and review/consolidation.md: one row per model, then rejected merges with the failing condition.

Never set the status to approved – only the Tech Lead does.
