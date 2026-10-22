---
name: extract
description: Run stages 1–2 of the Smart Axiata migration factory – resolve the dashboard inventory to MicroStrategy object IDs, then extract dashboard definitions, cube definitions, cube SQL, metric and attribute expressions, schedules and access lists into extract/<id>/. Use only when the team runs /extract.
disable-model-invocation: true
---

# Extract – stages 1 and 2

Read docs/Migration_Factory_Build_Spec.md, sections Pipeline and Migration IR, first.

1. Load the client inventory into registry.yaml (ID, name, wave, status). Resolve names to dashboard IDs with the mstrio-py Dashboard module; unresolved rows get status `unresolved`.
2. For each dashboard, save JSON under extract/<id>/:
   - dashboard definition – GET /api/v2/dossiers/{id}/definition (chapters, pages, visualizations, selectors, datasets);
   - every dataset's cube definition and SQL view (mstrio-py OlapCube or SuperCube, export_sql_view);
   - metric and attribute definitions from the Modeling API, expressions as tree, with dimensionality;
   - schedules and access lists.
3. Metadata only: never call a data endpoint in this stage. Store a checksum per file.
4. Anything the API does not return becomes an EXTRACT_GAP flag in decisions/<id>.yaml.

Done when every registry row is extracted or flagged.
