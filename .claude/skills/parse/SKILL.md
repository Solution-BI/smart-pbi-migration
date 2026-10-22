---
name: parse
description: Run stage 3 of the migration factory – parse extracted MicroStrategy JSON and cube SQL into one IR file per dashboard (ir/<id>.json), following the IR contract in the build spec. Use only when the team runs /parse.
disable-model-invocation: true
---

# Parse to IR – stage 3

Follow section Migration IR of the build spec exactly.

- Parse SQL with sqlglot in the dialect of the source connection: clickhouse, postgres or hive.
- Per dataset: tables tagged fact or dimension, used columns, WHERE predicates, joins (keys, type, predicate), GROUP BY, refresh cadence.
- Raise flags from rules/flags.yaml – GROUP_BY, INNER_JOIN_FILTER, COMPOSITE_KEY, NON_EQUI_JOIN, ROLE_PLAYING, DERIVED_TABLE, SCHEMA_CUBE, WRANGLE – and never fix them here.
- Every object keeps its mstr_id. No data values.

Done when every SQL is parsed or flagged.
