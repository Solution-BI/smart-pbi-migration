# Smart Axiata – MSTR → Power BI Migration Factory – Build Spec

22 Oct 2026 · Greg · Master copy: https://claude.ai/artifact/W5bzpmyrDGtsmNJEh5ybR9 (this file is a snapshot)

## At a glance

The factory turns all 151 MicroStrategy dashboards into a first Power BI version in one automated run – semantic models as TMDL, reports as PBIR – and hands each dashboard to the team with a short list of decisions. Human effort goes into those decisions and the refinements, not into rebuilding from scratch.

**Operating model.** Extraction and generation run once over the whole estate. The team then reviews and refines dashboards sprint by sprint, in P1/P2 priority order. Deployment to Fabric is a later stage and sits outside this spec.

**Scope.** Metadata translation only: definitions, SQL, formulas, layout. Client data never enters Claude's context – validation diffs run as scripts, and Claude reads their summaries.

**Principles**

1. Deterministic code where the rule is clear – extraction, SQL parsing, consolidation scoring, TMDL and PBIR generation, data diffs. Claude where judgement is needed – unusual SQL, metric logic, layout.
2. Never guess. Anything outside the rules becomes a decision flag with a proposed default; a human closes it.
3. Traceability. Every Power BI table, column, measure and visual keeps the MicroStrategy object ID it came from.
4. Query folding first. One query per source table, only the columns dashboards use, joins as relationships, no native SQL.
5. Validation twice: a sanity pass during generation, a scenario pass after human review.

## Pipeline

Seven stages run once over all 151 dashboards and produce version 1; two more run per dashboard inside the sprints, in priority order. Every stage writes files to the repo, so any stage can be re-run without touching the others.

**Pipeline in text** (drawn as a diagram in the master copy). Factory run, once over all 151 dashboards: 1 Inventory → 2 Extract → 3 Parse to IR → 4 Consolidate → *Tech Lead approves the model map* → 5 Build models → 6 Build reports → 7 Validation pass 1. Hand-off: version 1 of all 151, queued in P1/P2 order. Sprint loop, per dashboard: *8 Human refinement* → 9 Validation pass 2 → *Peer review, then UAT*. Deployment to Fabric follows later. Steps in italics are decided by people; automated stages never wait for them – open flags travel with version 1 and are closed in the sprint loop.

| Stage | Input | Method and tools | Output | Done when |
| --- | --- | --- | --- | --- |
| 1. Inventory | Client inventory (151 dashboards, P1/P2) | mstrio-py Dashboard module resolves names to object IDs in the project | `registry.yaml`: ID, name, wave, status | Every row resolved or flagged |
| 2. Extract | Registry | mstrio-py and REST: dashboard definition (v2), cube definitions, cube SQL view, metric and attribute definitions (Modeling API), schedules, access lists | `extract/<id>/*.json`, metadata only | All 151 extracted, with checksums |
| 3. Parse | Raw JSON | SQL parser per source dialect (sqlglot); definition walker | `ir/<id>.json` (section Migration IR) | Every SQL parsed or flagged |
| 4. Consolidate | All IRs | Scoring and hard conditions (section Consolidation rules) | `consolidation.yaml` and a review table | Tech Lead approves the model map |
| 5. Build models | IRs, model map, rule libraries | Template-driven TMDL generator; Claude for patterns outside the rules; Power BI Modeling MCP server for model checks | `models/<model>.SemanticModel` | Model opens, every measure evaluates, flags logged |
| 6. Build reports | IRs, models, theme | PBIR generator driven by the visual map; JSON schema validation | `reports/<dashboard>.Report` | PBIR validates and opens in Desktop |
| 7. Validation pass 1 | Reports, MicroStrategy instance | DAX runner and REST visual data; diff by script | `runs/<id>/scorecard.json` | Scorecard exists for all 151 |
| 8. Human refinement | Scorecard, flags | Developers in Desktop, sprint by sprint | `decisions/<id>.yaml`; dashboard locked | Flags closed, peer review done |
| 9. Validation pass 2 | Refined report, test scenarios | Same engine, scenario files | Evidence pack | Green, then UAT |

Deployment to Fabric follows as a later stage.

## Migration IR

Each dashboard gets one intermediate representation (IR) file: the contract between extraction and generation. Stages 4 to 7 read only IR files, the model map and the decision logs – never raw MicroStrategy JSON. The example uses placeholder names and IDs.

```json
{
  "dashboard": {"mstr_id": "<id>", "name": "Home Internet Performance", "wave": "P1", "status": "generated"},
  "datasets": [{
    "mstr_id": "<cube id>", "kind": "mtdi", "subtype": 779,
    "source": {"db_instance": "<MSTR DB instance>", "connector": "clickhouse"},
    "sql": "SELECT ... FROM mart.fact_kpi f JOIN mart.dim_site s ON f.site_id = s.site_id",
    "tables": [
      {"name": "mart.fact_kpi", "role": "fact", "columns": ["date_id", "site_id", "dl_mbps"], "where": []},
      {"name": "mart.dim_site", "role": "dimension", "columns": ["site_id", "province"], "where": []}
    ],
    "joins": [{"left": "mart.fact_kpi.site_id", "right": "mart.dim_site.site_id", "type": "inner", "predicate": "equi"}],
    "group_by": false,
    "refresh": "daily"
  }],
  "objects": {
    "attributes": [{"mstr_id": "<id>", "name": "Province", "column": "mart.dim_site.province", "derived": null}],
    "metrics": [{"mstr_id": "<id>", "name": "Avg DL Speed", "expression": "Avg(dl_mbps)", "dimty": "report_base_level", "derived": true}]
  },
  "layout": {"chapters": [{"key": "K36", "name": "Overview", "filters": [], "pages": [
    {"key": "K53", "name": "Summary", "selectors": [], "visualizations": [{"key": "W12", "type": "grid", "title": "<title>", "objects": ["<ids>"]}]}
  ]}]},
  "target": {"semantic_model": "SM_<Domain>_<Subject>", "report": "Home Internet Performance"},
  "flags": [{"code": "INNER_JOIN_FILTER", "ref": "joins[0]", "proposal": "relationship plus not-blank visual filter", "status": "open"}]
}
```

**Rules**

- Metadata only. No data value ever lands in an IR file, and validation results stay in `runs/`.
- Every object carries its `mstr_id`. Generators write it as an annotation on each TMDL table, column and measure and each PBIR visual, which is what makes validation and review traceable.
- Generation is a pure function of IR, model map and decision logs: a re-run produces the same files. Dashboards with status `locked` belong to the team and are skipped.

## Consolidation rules

Dashboards share a semantic model when they share fact tables and no hard condition forbids it. The factory proposes the model map and the Tech Lead approves it; one model per dashboard stays the fallback when no merge passes.

**Scoring – source tables first**

1. Build a dashboard × source-table matrix from the parsed cube SQL. Tag each table as fact (its columns feed metric aggregations) or dimension (joined on a key, used for attributes).
2. Score each pair of dashboards on fact tables only: weighted Jaccard = size of the shared fact tables ÷ size of all their fact tables. Sizes come from MicroStrategy cube sizes and source table statistics – metadata, no data read. Dimensions never drive a merge; they follow the facts into the model.
3. Merge greedily from the highest score. A dashboard joins a model when its fact tables are a subset of the model's (score 1.0) or overlap at 0.6 or more. Both values are starting points, calibrated in the pilot.

**Hard conditions – all must hold**

| # | Condition | Why |
| --- | --- | --- |
| C1 | Same audience: identical access groups, or no restricted rows anywhere in the model | Build permission exposes the whole model through Excel and XMLA; Smart's pre-filtered dashboards exist to hide data |
| C2 | Same row scope on every shared table: identical WHERE predicates, or none | A shared table loads once; different filters change what each dashboard shows |
| C3 | Same join semantics on every shared table pair: same keys, same join type | A table pair holds one active relationship; a second key means role-playing, a different join type is a filtering decision |
| C4 | Same refresh cadence class, daily or hourly | A model refreshes as a unit; mixing either refreshes daily data hourly or leaves hourly data stale |
| C5 | Estimated model size of 10 GB or less | A full refresh can need about twice the model size, and F64 caps one model at 25 GB |
| C6 | Same source connection for every shared table | The same table name in two databases is two tables |

C1, C4, C5 and C6 block a merge outright. A merge that fails only C2 or C3 is proposed with a flag (FILTER_DIVERGENCE or ROLE_PLAYING) and its options – load the widest scope and move the filters to report level, or add a role-playing dimension – for the Tech Lead to decide.

**Soft rules.** Prefer models within one subject area, with at most about 15 dashboards and 40 tables each. When a large fact table feeds dashboards in two subject areas, one shared model beats two copies of the fact.

**Output for approval.** `consolidation.yaml` plus a review table with one row per proposed model: name, dashboards, fact tables, estimated size, cadence, audience, score, open flags. Rejected merges are listed with the condition that failed. Once approved the map is frozen; a later change re-runs stages 5 to 7 for the affected dashboards only.

## Model rules

Every source table becomes one Power Query query that folds, every SQL join becomes a relationship, row-level logic goes to Power Query and aggregation logic to DAX measures.

**Queries – one per source table**

1. Navigate through the certified connector: PostgreSQL; Impala or Hive for Cloudera; ClickHouse, with its ODBC driver on every Desktop and on the gateway. Never `Value.NativeQuery` or `Odbc.Query`.
2. Steps in a fixed order, all taken from the folding whitelist in `rules/folding.yaml`: source → navigation → select columns (the union the model's dashboards use) → filter rows (single-table predicates identical across the model) → rename to business names → set types. Derived columns come last.
3. Server and database are M parameters in `expressions.tmdl`, so one model points at dev or prod.
4. Keys are hidden. Each displayed attribute uses the MicroStrategy DESC form, sorted by its ID form (Sort By Column).

**Joins → relationships**

| SQL in the cube | Power BI | Flag |
| --- | --- | --- |
| Equi-join on one column, unique dimension key | One-to-many, single direction, dimension → fact | – |
| LEFT JOIN fact → dimension | Same relationship, same semantics | – |
| INNER JOIN | Relationship; fact rows without a match now show under blank | INNER_JOIN_FILTER |
| GROUP BY or DISTINCT | Mart table imported as is, no grouping | GROUP_BY |
| Join on two or more columns | Concatenated key column on both sides | COMPOSITE_KEY |
| BETWEEN, <, > in the join | No relationship until mitigated | NON_EQUI_JOIN |
| Same dimension joined twice | Duplicate dimension, or inactive relationship plus USERELATIONSHIP | ROLE_PLAYING |
| Key not unique on either side | Bridge table | MANY_TO_MANY |
| Subquery, CTE, UNION | Split into table queries; UNION becomes an append | DERIVED_TABLE |
| Schema-based OLAP cube (about 10% of the estate) | Rebuilt from schema objects through the Modeling API, not from generated multi-pass SQL | SCHEMA_CUBE |

Each model gets one Date table, marked as date table and related to every fact date column used by a time-based metric.

**Derived attributes → Power Query**

- Row-level expression on one table → custom column in that table's query, after the folding steps. The function map lives in `rules/attributes.yaml`: Concat → `&`; If and Case → `if … then … else`; Year, Month, Quarter → `Date.Year`, `Date.Month`, `Date.QuarterOfYear`; Left, Right, SubStr, Upper, Lower, Trim → `Text.Start`, `Text.End`, `Text.Middle`, `Text.Upper`, `Text.Lower`, `Text.Trim`. SubStr counts from 1 and `Text.Middle` from 0, so the generator shifts the offset.
- Expression across two tables → DAX calculated column with RELATED, flagged CROSS_TABLE_ATTRIBUTE.
- ApplySimple and other pass-through SQL → flagged PASS_THROUGH.
- Derived elements (grouped attribute elements) → conditional column, or a mapping table when the groups are long.
- Data wrangling steps on an MTDI table → equivalent Power Query steps, flagged WRANGLE for review.

**Metrics → DAX measures**

Each metric's expression and dimensionality come from the Modeling API. A construct outside these patterns gets a Claude proposal flagged METRIC_PATTERN, which closes only after a test against MicroStrategy values.

| MicroStrategy | DAX pattern |
| --- | --- |
| Sum, Avg, Min, Max, Count on a fact | SUM, AVERAGE, MIN, MAX, COUNT on the fact column |
| Count distinct of an attribute | DISTINCTCOUNT on the fact table's key column, not the dimension's – an MTDI cube counts the elements present in its rows |
| Compound metric with smart totals | DIVIDE([A], [B]) – totals stay a ratio of totals |
| Compound metric without smart totals | SUMX over the visual grain, flagged SUBTOTAL_RULE |
| Level metric (dimensionality set to ignore or absolute) | CALCULATE([Base], REMOVEFILTERS(…)) or ALLEXCEPT, built from the dimensionality units |
| Conditional metric | CALCULATE([Base], <filter>), checking the metric's report-filter settings |
| Transformation: last year, YTD, MTD, previous month | SAMEPERIODLASTYEAR, DATESYTD, DATESMTD, DATEADD on the Date table; custom transformation tables flagged |
| Percent to total, rank | DIVIDE([M], CALCULATE([M], ALLSELECTED(…))); RANKX over ALLSELECTED |
| Running and moving functions | WINDOW or OFFSET patterns, flagged OLAP_FUNCTION |
| First or last in fact (semi-additive) | LASTNONBLANKVALUE or a closing-balance pattern, flagged SEMI_ADDITIVE |

Measures carry the MicroStrategy format string, sit in display folders that mirror the MicroStrategy folders, and keep the metric ID as an annotation.

## Report rules

Each dashboard becomes one PBIR report bound to its assigned semantic model. Pages, visuals and titles follow the MicroStrategy layout; dashboard filters move to the filter pane and selectors become slicers.

**Structure**

- The report is bound to its model by path (`definition.pbir`) during development; the connection switches at deployment.
- One Power BI page per MicroStrategy page. With more than one chapter, the page name reads "Chapter – Page".
- Canvas size and base page come from the template. Visual positions are mapped proportionally onto the template's content area; overlaps or off-canvas positions get LAYOUT_APPROX and an automatic grid.
- Visual titles are kept word for word; sort order and number formats carry over.
- Thresholds become conditional formatting rules; complex threshold logic is flagged.
- A prompted dashboard gets a slicer set to the prompt's default answer; a prompt without a default is flagged PROMPT.

**Visual mapping**

| MicroStrategy | Power BI | Flag |
| --- | --- | --- |
| Grid, flat | Table | – |
| Grid with attributes or metrics on columns | Matrix | – |
| Bar, vertical or horizontal – clustered, stacked or 100% | Column or bar chart of the same kind | – |
| Line, area | Line, area | – |
| Dual axis | Line and clustered column | – |
| Pie, ring | Pie, donut | – |
| KPI, big number | Card | – |
| Heat map | Treemap | – |
| Bubble, scatter | Scatter | – |
| Gauge, waterfall | Gauge, waterfall | – |
| Map with custom shapes for administrative divisions | Custom map visual (S1 deliverable); placeholder until it exists | – |
| Text, image | Text box, image | – |
| HTML container, network, Sankey, box plot, histogram | Closest visual plus a note | NO_EQUIVALENT |

**Filters and selectors**

| MicroStrategy | Power BI |
| --- | --- |
| Filter panel filter (chapter level) | Filter pane: report level when the dashboard has one chapter, page level on each page of the chapter otherwise |
| Element list filter | Basic filter, default selections kept |
| Qualification filter on an attribute or metric | Advanced filter, or Top N |
| Dropdown selector | Dropdown slicer |
| Checkbox or list selector | List slicer, multi-select |
| Radio button, link bar, button bar | Button slicer, single select |
| Slider on an attribute or metric | Range slicer |
| Search box | Slicer with search on |
| Selector targeting specific visuals | Edit interactions: visuals outside the target set to none |
| Visualization used as filter | Interaction set to filter, not highlight |
| Panel stack, information window, link to another dashboard | Bookmarks, tooltip page or drillthrough – proposed and flagged NO_EQUIVALENT |

**Theme.** The template supplies the theme JSON, the base page (background, header, logo) and the canvas size. The generator registers the theme on every report and starts each page from the template page. Colours and fonts are never set at visual level, except where a MicroStrategy threshold requires it.

## Decision flags

The factory never guesses: anything outside the rules becomes a flag with a default, and a person closes it during the sprint. Flags on a shared model go to the Tech Lead, because the decision applies to every dashboard on that model.

| Code | Raised when | Factory default | Decides |
| --- | --- | --- | --- |
| GROUP_BY | GROUP BY or DISTINCT in the cube SQL | Mart table imported without grouping | Tech Lead |
| INNER_JOIN_FILTER | INNER JOIN in the cube SQL | Relationship; not-blank filter proposed on affected visuals | Tech Lead |
| COMPOSITE_KEY | Join on two or more columns | Concatenated key column on both sides | Tech Lead |
| NON_EQUI_JOIN | Join predicate other than equality | No relationship; mitigation options listed | Tech Lead |
| ROLE_PLAYING | Same dimension joined on two keys | Duplicate dimension or inactive relationship | Tech Lead |
| MANY_TO_MANY | Key not unique on either side | Bridge table proposed | Tech Lead |
| DERIVED_TABLE | Subquery, CTE or UNION | Split into table queries; append for UNION | Tech Lead |
| SCHEMA_CUBE | Schema-based OLAP cube | Rebuilt from schema objects | Tech Lead |
| FILTER_DIVERGENCE | Shared table with different WHERE clauses | Widest scope loaded, filters at report level | Tech Lead |
| FOLD_RISK | Step outside the connector's folding whitelist | Step kept last, marked non-folding | Tech Lead |
| WRANGLE | Data wrangling steps on an MTDI table | Equivalent Power Query steps | Developer |
| CROSS_TABLE_ATTRIBUTE | Derived attribute spans two tables | DAX calculated column with RELATED | Developer |
| PASS_THROUGH | ApplySimple or pass-through SQL | Known pattern translated, otherwise an empty placeholder column | Developer |
| METRIC_PATTERN | Metric outside the DAX pattern library | Claude's proposal, untested | Developer, after a test |
| SUBTOTAL_RULE | Compound metric without smart totals | SUMX over the visual grain | Developer |
| OLAP_FUNCTION | Running or moving function | WINDOW or OFFSET pattern | Developer |
| SEMI_ADDITIVE | First or last in fact | Closing-balance pattern | Developer |
| NO_EQUIVALENT | Visual or feature without a direct equivalent | Closest visual plus a note | Developer |
| LAYOUT_APPROX | Overlap or off-canvas position | Automatic grid | Developer |
| PROMPT | Prompt without a default answer | Slicer with no selection | Developer |
| EXTRACT_GAP | Definition not returned by the API | Object listed for manual capture in Workstation | Developer |

**Lifecycle.** Each flag lands in `decisions/<dashboard>.yaml` with an ID, code, MicroStrategy object, proposal, status (open, accepted, overridden, won't fix), who decided, date and note. An open flag never blocks generation – the factory applies the default and marks the object – but it blocks the dashboard's move to validation pass 2. Generation reads the decision logs, so a decision survives every re-run.

## Validation

Validation runs twice. Pass 1, during generation, checks that every dashboard opens and its numbers make sense; pass 2, after human review, runs the test scenarios and produces the evidence pack. Both compare visual by visual against MicroStrategy, and neither puts data in front of Claude.

**How one comparison works**

1. MicroStrategy: create a dashboard instance through the REST API, apply the scenario's selector values, read each visualization's data.
2. Power BI: build a DAX query from the visual's fields and filters (SUMMARIZECOLUMNS over the same grouping) and run it with a DAX runner script – against the model open in Power BI Desktop now, against the workspace XMLA endpoint after deployment.
3. Same data date on both sides: compare a closed period, or run both after the same daily refresh.
4. The script matches rows on the grouping columns, compares each measure and writes to `runs/<id>/`. Claude reads the scorecard only.

**Pass 1 – during generation, all 151, automated**

- Structure: the model opens, no relationship is ambiguous, every measure evaluates, every visual's fields resolve, the PBIR files pass their JSON schemas.
- Sense check on the default filter state, per visual: row count within 5% and each measure total within 1% of MicroStrategy. Both thresholds are starting values for "makes sense", not acceptance criteria.
- Output: green, amber or red per visual and per dashboard, which feeds the sprint review queue together with the open flags.

**Pass 2 – after human review, per dashboard in sprint order**

- One scenario file per dashboard – selector values, date ranges, drill levels. The scenario catalog is designed later.
- Exact match at displayed precision on every scenario.
- Response time against the MicroStrategy baseline (acceptance criterion 3) and side-by-side page screenshots (criterion 2).
- Output: the evidence pack – screenshots plus side-by-side data export – for SBI peer review, then UAT.

**Data stays out of Claude.** The Power BI Modeling MCP server can run DAX queries and return their results into the agent's context. The factory uses it for modeling operations and for checking that DAX is valid, never to read query results; every data comparison runs through the script.

## Claude Code layout

The repo keeps code, rules and generated PBIP side by side; Claude Code works from a CLAUDE.md with hard rules and one skill per stage. Where the POC already covers a stage, keep its code and adopt the contracts – IR files, model map, decision logs.

```
smart-mstr2pbi/
├── CLAUDE.md
├── .claude/skills/
│   ├── extract/SKILL.md         # /extract – stages 1–2
│   ├── parse/SKILL.md           # /parse – stage 3
│   ├── consolidate/SKILL.md     # /consolidate – stage 4
│   ├── build-model/SKILL.md     # /build-model <model> – stage 5
│   ├── build-report/SKILL.md    # /build-report <dashboard> – stage 6
│   ├── validate/SKILL.md        # /validate <dashboard> --pass 1|2 – stages 7 and 9
│   └── dax-patterns/SKILL.md    # reference, loaded when Claude translates metrics
├── rules/        # folding, joins, attributes, metrics, visuals, flags (YAML)
├── src/          # Python: extract, parse, ir, consolidate, tmdl, pbir, validate
├── config/       # connections.example.yaml, thresholds.yaml
├── theme/        # template theme JSON and base page
├── registry.yaml       # 151 dashboards: ID, wave, model, status
├── consolidation.yaml  # approved model map
├── extract/  ir/       # metadata only
├── models/<model>.SemanticModel/   # TMDL
├── reports/<dashboard>.Report/     # PBIR
├── decisions/<dashboard>.yaml      # flags and decisions
├── runs/<id>/scorecard.json        # validation summaries
└── data/         # git-ignored: exports and diffs, never read by Claude
```

**Skills.** Stage skills set `disable-model-invocation: true`, so they run only when the team types the slash command, and per-dashboard generation sets `context: fork` to run in its own subagent context. Reference skills such as `dax-patterns` stay loadable by Claude on demand.

**CLAUDE.md** – the draft ships as a separate file in this starter kit.

**Tooling.** mstrio-py (Python 3.10+; the account needs the Use Library privilege), sqlglot, the Power BI Modeling MCP server (public preview) and a JSON schema validator for PBIR. Worth evaluating: the community `pbir-cli` and the power-bi-agentic-development skills – third-party, so reviewed before use.

## Pilot run

Before the full run, the factory runs on about ten dashboards that cover the hard cases. The results calibrate the thresholds and decide go or no-go for all 151.

**Sample, chosen from the extraction:** the four heaviest dashboards (46–49 cubes each), two with the custom-shape map, two with subscriptions, two simple single-page dashboards – covering ClickHouse, PostgreSQL and Cloudera, and at least one schema-based cube.

| Measure | Proposed go threshold |
| --- | --- |
| Objects translated without a flag | 80% or more |
| Visuals green in pass 1 | 70% or more |
| Flags with an unknown code | None |
| Data values found in Claude's context or logs | None |
| Folding confirmed per connector – generated SQL visible in Desktop's native query view or the source's query log | All three connectors |
| Human time to close flags and review, per dashboard | Recorded against the 1 h per cube and 0.5 h per visual budget |

After a go, the full run covers all 151 dashboards, writes every scorecard to the registry and builds the sprint queue in P1/P2 order.

## Open points and sources

Eight points need confirming in the pilot; none blocks starting the build.

| Open point | Why it matters | Fallback |
| --- | --- | --- |
| Derived metrics and attributes created inside dashboards are embedded objects: confirm the Modeling API returns their expressions and dimensionality | Every DAX and Power Query translation depends on them | Flag EXTRACT_GAP and capture in Workstation |
| Confirm the cube SQL view returns the import query for data import cubes (subtype 779) on Smart's version, and where wrangling steps live | Stage 3 parses this SQL | Read tables from the cube definition, flagged |
| Confirm the dashboard definition exposes visual positions | Layout fidelity | Automatic grid, LAYOUT_APPROX |
| ClickHouse: a `readonly=1` account fails when the ODBC driver's SqlCompatibilitySettings is on | Refresh through the gateway | `readonly=2` service account, or the setting left off |
| ClickHouse ODBC driver and the Impala or Hive drivers on every Desktop and on the gateway | Refresh and folding | – |
| Template still to be provided (theme JSON or a PBIP), and the PBIR version supported by the team's Desktop build | Stage 6 | – |
| Pass 1 refreshes models in Power BI Desktop, which needs network access to the sources (VPN or jump host) | Pass 1 data checks | Structural checks only until access exists |
| The Power BI Modeling MCP server is in public preview | Stability of model checks | Pin the version; the TMDL generator works without it |

**Documentation consulted**

- Strategy REST API – [retrieve definitions and data from the Intelligence Server](https://microstrategy.github.io/rest-api-docs/common-workflows/analytics/retrieve-data-from-the-intelligence-server/workflow-retrieve-data-from-the-intelligence-server), [selector definitions in the v2 dashboard definition](https://microstrategy.github.io/rest-api-docs/common-workflows/analytics/manage-selectors/retrieve-a-selectors-definition), [metric expression and dimensionality](https://microstrategy.github.io/rest-api-docs/common-workflows/modeling/manage-metric-objects/retrieve-a-metrics-definition)
- [mstrio-py](https://github.com/MicroStrategy/mstrio-py) and the Strategy Community example on [cube SQL extraction with export_sql_view](https://community.strategy.com/article/Extracting-SQL-details-from-all-OLAP-cubes-in-a-project-real-data-lineage)
- Microsoft – [PBIR report folder](https://learn.microsoft.com/power-bi/developer/projects/projects-report?tabs=desktop), [enhanced report format](https://learn.microsoft.com/en-us/power-bi/developer/embedded/projects-enhanced-report-format), [Power BI MCP servers](https://learn.microsoft.com/en-us/power-bi/developer/mcp/mcp-servers-overview) and the [Modeling MCP server repo](https://github.com/microsoft/powerbi-modeling-mcp), [ClickHouse connector](https://learn.microsoft.com/en-us/power-query/connectors/clickhouse)
- ClickHouse – [ODBC driver and Power BI](https://clickhouse.com/docs/interfaces/odbc)
- Claude Code – [skills](https://code.claude.com/docs/en/skills.md)
- Community, third-party – [pbir-format skill](https://openskillindex.com/skills/data-goblin-power-bi-agentic-development-pbir-format)
