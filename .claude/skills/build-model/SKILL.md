---
name: build-model
description: Run stage 5 of the migration factory for one semantic model – generate TMDL in models/<model>.SemanticModel from the IR files, the approved model map, the decision logs and the rule libraries. Use only when the team runs /build-model <model>.
disable-model-invocation: true
context: fork
---

# Build model – stage 5

Prerequisite: consolidation.yaml has status `approved`.

- One Power Query query per source table, steps only from rules/folding.yaml; no Value.NativeQuery, no Odbc.Query.
- Joins become relationships per the table in section Model rules; anything else becomes a flag, never a guess.
- Derived attributes go to Power Query (rules/attributes.yaml); metrics become DAX measures (dax-patterns skill).
- One marked Date table per model; server and database as M parameters in expressions.tmdl.
- Annotate every table, column and measure with its MicroStrategy ID.
- Apply every decision in decisions/*.yaml for the model's dashboards.
- Check the model with the Power BI Modeling MCP server – it opens, every measure evaluates – and never read query results.
