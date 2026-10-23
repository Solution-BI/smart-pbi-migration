# MicroStrategy → Power BI migration POC: working context

This POC tests whether Claude can migrate MicroStrategy (MSTR) dashboards to Power BI. Most source dashboards read from **Data Import super cubes built on imported SQL**.

The pipeline runs in three stages:
1. Extract: the MSTR REST API (mstrio-py) is read into a migration spec.
2. Transform: the spec becomes a Power BI build spec.
3. Build: a **PBIP project** is written, containing a TMDL model and a PBIR report with the visuals as placeholders.

`README.md` is the user guide; this file is the context for a new chat.

## Rules

- **Never read `.env`.** Permissions block it, and it holds the MSTR login. Ask the user for any value it contains.
  - The MSTR login never goes into the inventory workbook or any shared document.
- **The project lives in OneDrive.** OneDrive briefly locks files while it syncs them, so:
  - build outputs in a local temp folder and copy them in with `scripts/fsutil.py` (`staging_dir`, `mirror`, `retry`);
  - never `rmtree` or rename folders inside the project.
- **Paths are limited to 260 characters on Windows.** Power BI projects therefore go in `3_powerbi/<first 8 chars of ID>/`.
- **`scripts/paths.py` is the single source of the folder layout.** Use it rather than hard-coding paths.
- **Use `.venv\Scripts\python.exe`.** Write throwaway scripts to the scratchpad rather than `scripts/`, because PowerShell mangles quoting in inline `python -c`.
- **Check before `--overwrite`.** A rebuild discards changes the user saved from Desktop. Compare file times in `3_powerbi/<ID8>/` with the row's `Last run`.
- **Excel locks the inventory.** `run_migration.read_shared()` can still read it while it is open.
- **Git.** Initialised 2026-10-23, remote `origin` = https://github.com/Solution-BI/smart-pbi-migration, which is **public**.
  - Branch `Panha` holds **only the POC**, with its own history (orphan branch): the user does not want `main`'s code yet. `main` holds a teammate's (Greg's) migration-factory scaffold (`.claude/skills`, `rules/`, `docs/Migration_Factory_Build_Spec.md`, own CLAUDE.md/README.md). Don't bring it in or delete it. To combine later: `git merge origin/main --allow-unrelated-histories` (CLAUDE.md, README.md, .gitignore will conflict).
  - `.env.example` is not committed: permissions block reading it, so its content is unverified. The user must confirm it holds only placeholders.
  - **Server names never go to the repo** (user's decision, 2026-10-23, repo still public). A required git filter `redact` (`.gitattributes`: `*.tmdl`, `*.md`) swaps them for placeholders (`<postgres-server>`, `<postgres-database>`, `<snowflake-account>`, `<mstr-server>`, `<mstr-dev-server>`) when staging and back on checkout. The sed rules and filter config are only in the local `.git` (`redact.sed`, `unredact.sed`, `.git/config`). A new server name must be added to both sed files before committing; `git grep` the index to check.
  - The inventory workbook is gitignored (it holds the real servers).
  - Author Panha Vuthy (repo-local git config).

## Layout

| Folder | Contents |
|---|---|
| `1_input/migration_inventory.xlsx` | User input. **Dashboards** sheet: one row per dashboard, Status=Ready to convert. **Settings** sheet: defaults, output folder, Overwrite. |
| `2_work/<DashboardID>/1_extract/` | `raw/` API responses, `_calls.json`, `migration_spec.json` and `.md` |
| `2_work/<DashboardID>/2_transform/` | `powerbi_build_spec.json` and `BUILD_PLAN.md`, which lists the issues needing manual work |
| `2_work/migration_runs.csv` | History of every run |
| `3_powerbi/<ID8>/<Report>.pbip` | Final output |
| `3_powerbi/manual_tests/` | Files saved by hand |
| `scripts/` | All code; the report template is in `scripts/templates/report` |

## Running

```powershell
.\.venv\Scripts\python.exe scripts\run_migration.py [--only <DASHBOARD_ID>] [--overwrite] [--dry-run] [--skip-extract] [--inventory <xlsx>]
```

`--skip-extract` reuses the extract already in `2_work` (MSTR unreachable, or only transform/build code changed). <mstr-server> was unreachable at 17:45 on 2026-10-21.

Each step can also be run on its own:
- `extract_dashboard_metadata.py <ID> --project-id <PID>`
- `transform_to_powerbi.py <ID> --source-type snowflake|postgresql|sqlserver [--server] [--warehouse] [--database]`
- `generate_pbip_report.py <ID> [--out <dir>] [--no-model] [--template <dir|.pbix>]`

How the runner behaves:
- **Source values.** The inventory's Source columns, or the Settings defaults, become the Power BI parameters `SourceServer`, `SourceWarehouse` and `SourceDatabase`. A rebuild therefore needs no parameter edits in Desktop.
- **Excel must be closed.** `run_migration.py` refuses to start while Excel has the workbook open, because it writes results back. To run anyway:
  1. Copy the workbook with `read_shared` to the scratchpad.
  2. Run with `--inventory <copy>`.

  The real workbook's row is not updated; `migration_runs.csv` is.
- **Desktop does not reload a PBIP changed on disk.** After a rebuild, close Desktop **without saving** (saving writes the old model back), then reopen the `.pbip`.
- **Status.** A row is Done when it has 0 errors. It is Needs review when it has errors. Warnings never change the status.

## Environments

`.env` points to one MSTR server at a time. Since 2026-10-21 it points to **<mstr-server>**.

| MSTR server | Project | Dashboard | Power BI source |
|---|---|---|---|
| <mstr-dev-server> | Fairthorne `741C53B811E551174FDA0080EFF5E6CA` | `9D1AA7CA0A4728650BE279A1DC273F93` "Sales Report" | Snowflake (MSTR data source NEW_FAIRTHORNE); values not given yet |
| <mstr-server> | TPCH - Demo `E41F5CB4494E5C1B729FEF8D268A4696` | `249C303B594D7B1186B08B8B8DD18F20` "Sales Dashboard" | Snowflake `<snowflake-account>.snowflakecomputing.com` / `COMPUTE_WH` / `SANDBOX_DB` |
| <mstr-server> | TPCH - Demo | `8DB69794CF4FACF9812A088695CF882E` "Sales Dashboard By Region, Manufacture" | same as above |

## MicroStrategy REST API findings

Calls use `Connection.get/post(endpoint=...)` with the `X-MSTR-ProjectID` header.

Endpoints that work:
- `/api/v2/dossiers/{id}/definition`
- `/api/documents/{id}/cubes`
- `/api/documents/{id}/prompts`
- `/api/objects/{id}?type=55|3`
- `/api/v2/cubes/{id}`
- **`/api/v2/cubes/{id}/sqlView`**, which returns the cube SQL.
- An instance flow for visualization details:
  1. `POST /api/dossiers/{id}/instances`
  2. `/api/v2/dossiers/{id}/instances/{mid}/definition`
  3. `.../chapters/{ck}/visualizations/{vk}`
  4. `DELETE /api/documents/{id}/instances/{mid}`

What does not work for Data Import cubes (subtype 779, extType 256):
- `/api/model/cubes/{id}` returns 400.
- `/api/model/metrics/{id}` returns "managed metric not supported".

Gaps:
- The DB connection is embedded in the cube and the API does not expose it.
- The public API gives no colour-by, element colours, legend, data labels or axis titles. Its catalogue (`/api/openapi.json`, 525 paths on 11.5) has no `colorBy`, `dropZone` or `legend` field.
- A new dashboard instance can open in the user's last-viewed state; on 249C303B its selector said 2019 while the saved definition said 2020. Selector defaults come from the saved definition.
- MSTR content changes between runs. On the dev server, the cube E33F… was renamed "Sales Report - Dataset".

### Reference rendering (PDF export)

The extractor calls `POST /api/documents/{id}/instances/{mid}/pdf` with `PDF_OPTIONS` (all pages, overview, filter bar, 16×10 in) and saves `raw/dashboard.pdf`, one PDF page per dashboard page.
- The response is `{"data": base64}`; a 202 means asynchronous, so it polls `.../pdf/results/{id}`.
- Each page's PDF page number is in the spec (`referencePdfPage`) and in BUILD_PLAN.
- This is the target for the Power BI look. Read it with the Read tool and compare it with the user's Power BI screenshot.
- The user agreed on 2026-10-21 to use the PDF, and to send screenshots only when it's not good enough.
- The PDF shows the state a new session opens in, which may not be the saved defaults. On 249C303B it showed Year 2019, while the saved default is 2020.

### The .mstr export (`scripts/mstr_file.py`, undocumented format, best effort)

`POST /api/documents/{id}/instances/{mid}/mstr` returns the dashboard file, which is a zip. The extractor saves it as `raw/dashboard.mstr`. Its `<dashboardId>.delta` member is MSTR's binary document definition. Parts of it are readable:
- **Chart properties** are XML: `<props><widgetProps><fmt>` with Shape, Subtype, LegendVisibleness, LegendPosition, DataLabel and xAxLbNestedLabel.
  - They are paired with charts in dashboard order, only when the counts match.
  - Only LegendPosition code 13 (bottom centre) is known so far.
- **Element colours** are stored as `{"color":<BGR int>}`. The preceding bytes hold the attribute ID and the element key.
  - IDs are encoded as `09 07 <DWORD LE> 05 <WORD LE> 05 <WORD LE> <DWORD LE> <DWORD LE>`, with the two words swapped relative to the hex string.
  - Integer element keys are encoded as `05 14 00 08 <int64 LE>`.
  - Each key is checked against the REST element ids (`h142;<attrId>`).
- **Metric colours** use the same `{"color":…}` format, preceded by the metric ID with no element key. An element key only counts when it comes after the attribute ID.
  - Element and metric colours are stored for the whole dashboard. A dashboard copied from another keeps the old element colours, as 8DB69794 kept the region colours from 249C303B.
- **Page layouts** (free-form) are JSON `{"floatChildren": {"t":"g", "children": [{"t":"flc", "rk": <page key>, "k": <visual/selector key>, "top"/"left"/"width"/"height": "pct|px"}]}}`, ending in a null byte.
  - Pages are 1204 px wide here. The generator scales positions uniformly to 1920 wide and keeps the page's proportions.
  - Auto (tiled) layouts aren't read; those pages fall back to a grid, with an info in BUILD_PLAN.
- **Colour-by** is inferred: it is the chart attribute whose elements have custom colours (MSTR only allows element colours in Color By). The drop-zone codes in the template are binary and not decoded.
- **Derived metrics** are metrics created in the dashboard. REST marks them `derived: true`, gives no formula, and `/api/objects/{id}` returns 404 for them.
  - The .mstr file holds the formula as object IDs in post-order between the metric's name and `TokenStream`.
  - Function IDs are checked with `GET /api/objects/{id}?type=11`. For example `8107C310…`/`11`/`12`/`13` + `DD9911D3B98100C04F2233EA` are + − × ÷.
  - Only + − × ÷ with two operands are rebuilt. Anything else becomes a BLANK() measure with an error in BUILD_PLAN.
  - Net Profit on 8DB69794 was rebuilt this way and checked against MSTR's totals.

### Report Services documents (first one: 8292160D "05 Occuppancy", 2026-10-21)

- **Detection.** Documents and dossiers both have object subtype 14081. A document is recognised because `/api/v2/dossiers/{id}/definition` fails (500); `/api/documents/{id}/definition` then gives layouts, panel stacks, panels, visualizations, selectors and fields.
- **Conversion to pages.** `document_as_dossier` turns each layout into a chapter with one page and flattens panel stacks. A page is named after the document when its layout name is the default ("Layout 1").
- **Endpoints that work for documents** (with a document session, `POST /api/documents/{id}/instances`):
  - grid and graph contents through `/api/v2/dossiers/{id}/instances/{mid}/chapters/{layoutKey}/visualizations/{key}`;
  - `.mstr` and PDF exports. The PDF uses the document's own page setup, not `PDF_OPTIONS`.
- **What the API doesn't give for documents:**
  - which grids MSTR displays as graphs (the API says "grid" for all);
  - text box contents and positions;
  - images.
  These go in `1_input/overrides/<ID>.json`: `items` keyed by MSTR key with `visualType`, `title`, `position` and `note`, plus `pages` sizes. Claude fills it from `dashboard.pdf`.
- **Datasets are reports on cubes.** The report's `source` (from `/api/v2/reports/{id}`) is the cube. The SQL comes from the cube's sqlView; the objects come from the report.
- **Selector element names are IDs** in documents (`201806`). They're translated with `/api/cubes/{cube}/attributes/{attr}/elements`; descriptions can have a leading space, e.g. " 2018 JUNE".
- **Thresholds** (arrows on %LM/%LY) are listed in BUILD_PLAN and not converted yet.

### Schema cubes (`scripts/mstr_schema.py`)

- **What they are.** Intelligent Cubes on the project schema: `extType` ≠ 256, subtype 776. Their SQL is multi-pass (`CREATE TEMP TABLE ZZ.. AS`), the final SELECT returns base aggregates named `WJXBFSn`, and MSTR computes ratio metrics on the fly.
- **Single query.** `single_query` turns the passes into WITH clauses.
- **Column lineage.** `column_lineage` traces each alias to its pass, its expression without qualifiers or brackets, its join columns and its GROUP BY.
- **Model definitions.** The extractor (`schema_model`) fetches `/api/model/metrics|facts|transformations|attributes`:
  - a base metric is aggregation + fact expressions + transformation member columns (e.g. `id_tm_yyyymm_ly`) + level ID columns;
  - a compound metric is an infix formula;
  - `ApplySimple(...)` fact expressions are expanded.
- **Matching.** `match_metric_columns` compares the aggregated fact expression and the time join of each pass; for level metrics the GROUP BY must equal the level. The result was checked by hand on 04 Occupancy: all 11 base metrics correct.
- **Model output: a star schema** (user request, 2026-10-21; replaces the cube SQL imported as one flat table, which is now only the fallback for extracts without table definitions or SQL without a fact).
  - **Extract.** `schema_model` also fetches `/api/model/tables/{id}` for every table the SQL names (schema `namespace`, columns with types, `tableKey` = key attribute(s)) and the definition of every cube attribute (lookup table, column per table per form). Facts get `factName`.
  - **SQL analysis** (`star_layout`): a join on a lookup table's key (ID column of its single key attribute) is a many-to-one relationship; pass columns are followed to their tables, `coalesce(pa11.x, pa12.x)` included. A join on a transformation column (`a11.id_tm_yyyymm = a13.id_tm_yyyymm_ly`) is a time shift. Other joins (fact to day table for the filter, pass to pass) give no relationship. Each pass's aggregate gives the fact table and the row-level expression. WHERE conditions are the cube filter.
  - **Tables** (`add_star_dataset`): one per database table, shared across datasets: `Fact <table>` and `Dim <key attribute>`. Only the facts of the dataset's metrics and the lookup tables on the join path to the dataset's attributes (snowflake kept as in the SQL); other cube attributes are left out (info in BUILD_PLAN). Dimensions hold the forms of the attributes whose lookup table they are (visible, same names as before), keys and FKs hidden.
  - **Power Query.** Navigation `Source{[Schema=..., Item=...]}[Data]` + `Table.SelectColumns`: folds to SQL, no native-query approval. Native SQL only when the schema is unknown, the table is a logical view, or a fact expression can't be written in DAX (then computed in the fact's SELECT).
  - **Measures.** Base: `SUM(col)` or `SUMX(fact, <SQL expression as DAX>)` (`sql_to_dax`: arithmetic, CASE → IF/SWITCH, IN, comparisons, COALESCE). Transformations: `CALCULATE(base, REMOVEFILTERS(dim + its parents), TREATAS(VALUES(dim[id_tm_yyyymm_ly]), fact[id_tm_yyyymm]))`. Level metrics: SUMX over the level keys with ALLEXCEPT (warning, untested: none so far). Compound metrics as before.
  - **Cube filter** (e.g. `id_tm_yyyy in (2018)`) → report-level filter (`report.json` filterConfig) on a dimension column holding it (`Dim Year Month[Year]`). Facts load every period so LY/LM reach the year before.
  - All non-ID forms sort by the ID form. With every year in the dimension that needs names unique per ID (ok on 05 Occuppancy: "JAN 18").

### Linked datasets

Each dataset has its own attribute IDs, even for the same attribute (Year was `E8384…` and `0DE496…`), and REST doesn't show the links. MSTR dashboards link same-name attributes, though: a selector on one dataset's Year filtered the other dataset's grid. The link definitions in the .mstr (`DI2ProjectAttributeLinks`, `JoinJson`) are binary.

The transform therefore links attributes by name. It creates a shared calculated table (DISTINCT UNION of the key columns), relates each fact table to it (many to one, single direction) and hides the fact columns. Visuals, slicers and page filters then use the shared table.

## Transform behaviour (`scripts/transform_to_powerbi.py`)

- **Column matching.** Columns are matched to SQL SELECT aliases with sqlglot plus fuzzy matching; fuzzy matches are reported as warnings.
- **Power Query (M).** Each table's query is `Value.NativeQuery` over `Snowflake.Databases`, `PostgreSQL.Database` or `Sql.Database`. Columns are renamed case-insensitively (`RenameMap` + `Text.Lower`).
- **Data types.** MSTR types map to TOM types through `PBI_TYPES`; this covers `Int64`, which <mstr-server> reports. Unknown types produce a warning.
- **Row Count metric.** Every Data Import dataset gets an automatic "Row Count - <table>" metric. It is **skipped unless a visual uses it**, in which case it becomes `COUNTROWS`.
- **Geography.**
  - Latitude and longitude forms that are not in the SQL are skipped.
  - Geographic attribute names get a `dataCategory` (Country, City and so on).
- **Number formats.** Power BI has no conditional sections such as `[>=1000000]`, so `pbi_format` keeps the section that fits the largest value the dashboard showed. Charts and cards then set display units to None, so Power BI doesn't scale the value a second time.
  - **Scaling commas must move** (`move_scaling_commas`). Power BI's visual formatter only finds them right before the decimal point or at the very end of the format string (`getCustomFormatScale` in powerbi-visuals-utils-formattingutils).
  - With MSTR's `#,##0,,"M"` the visuals showed 16424327305M; the user reported this on 2026-10-21. The engine (DAX `FORMAT`) handled it, so only visuals were wrong.
  - The fix produces `#,##0"M",,` or `#,##0,,.0"M"`, which work in both. Checked with `FORMAT` on the live model: 16,424M, 52K, 16,424.3M. `#,##0,,."M"` leaves a stray dot in the engine.
- **Sort order.** MSTR lists elements in ID order. A description column gets `sortByColumn` = its ID column, and charts sort by their axis fields ascending.
- **Charts.**
  - Every axis level is `active: true`, which shows the axis expanded with nested labels.
  - Colour-by becomes the Series (legend) with per-value `dataPoint` colours.
  - When the colour attribute is also on the axis, the chart switches from clustered to stacked so the bars stay full width.
  - Legend and data labels come from the .mstr properties.
- **Chapter filters** become page-level filters (`page.json` → `filterConfig`) on every page of the chapter. Single-select MSTR filters get `requireSingleSelect`. BUILD_PLAN warns when a page filter and a slicer on the same column can never both match.
- **KPI** visuals become cards with display units None and no category label (title and value only, like MSTR). Labels the dashboard shows, such as "Gross Sales 2", are kept as the field's `displayName`.
- **Chart colours by metric:** charts with several metrics are coloured by metric (`dataPoint` with a `metadata` selector).
- **Chart titles as MSTR writes them:**
  - value axis: the metric names joined by " | ";
  - category axis: the attribute name;
  - legend title: "Metrics", or the colour attribute's name.
- **Data labels** turn vertical (`labels.labelOrientation = 1D`) when a chart shows several metrics. MSTR does this automatically; the props XML has no rotation setting.
- **KPI cards:** the value font is sized to the card (`card_value_size`).
- **Page size:** pages are generated 1280 px wide (Power BI's default) so default text sizes keep MSTR's proportions. MSTR canvases are about 1204 px.
- **Target is "close enough".** The user said on 2026-10-21 that Power BI doesn't have to match MSTR exactly.
- **Selector styles:** link-bar and button selectors (`hyper_links` and others) become horizontal tile slicers (`general.orientation = 1D`).
- **Source server.** The value is cleaned (`clean_server`): a pasted `https://`, a path and doubled dots are removed, and a warning is added.

## Power BI build details

- **TOM library.** `Microsoft.PowerBI.Tabular.dll` comes from the Store-app install of Desktop (found with `Get-AppxPackage`).
- **Model build.**
  - `build_model_tmdl.ps1` builds the model offline: CompatibilityLevel 1606, PowerBI_V3, en-US.
  - Desktop must be installed but does not need to be running.
  - The TOM code it shares with `deploy_model_to_desktop.ps1` is in `PbiSpecModel.ps1`.
- **Working with the open Desktop.** `deploy_model_to_desktop.ps1` and `export_model_tmdl.ps1` connect to the running Desktop. The port comes from `Get-NetTCPConnection` on msmdsrv, because the Store app has no AnalysisServicesWorkspaces folder.
- **PBIR.** Verified on Desktop 2.159.
  - Schema versions: definition.pbir 4.0 (byPath), visualContainer 2.13.0, page 2.1.0, report 3.3.0.
  - Theme: Fluent2-CY26SU10.
  - Visual positions are scaled to 1920×1080.
  - Slicer defaults are set through `objects.general[].properties.filter.filter`.

## State as of 2026-10-21

- **249C303B Sales Dashboard: Done.**
  - Rebuilt at 13:22 with the source parameters from the inventory and the chart conversion: Region → Country axis, colours by region, data labels, `#,##0,,"M"` format, slicer default 2020.
  - The rendering has not been checked in Desktop yet.
  - The inventory cell for its server contains a typo (`azure..snowflake`). The tool cleans it, but the user should fix the cell.
- **8DB69794 Sales Dashboard By Region, Manufacture: Done** (2026-10-21 14:31), not yet checked in Desktop.
  - Two datasets linked on Year, a chapter filter Year=2023 (now a page filter), and the derived metric Net Profit.
  - Page 2's selector Year=2019 conflicts with the chapter filter, so the grid is empty. MSTR also returned 0 rows for it.
  - Layout and metric colours (Gross Sales #FF00FF, Net Profit #FF99CC) were rebuilt from the .mstr on 2026-10-21 and match the user's MSTR screenshot. Not yet checked in Desktop.
- **8292160D "05 Occuppancy" (document, project Mall Analytics (DEV) `2610DAB6411D8292095733ADEB0F6046`, PostgreSQL): Done**, 2026-10-21.
  - The connection is left blank on purpose; the user fills it later.
  - Chart types, titles and positions come from `1_input/overrides/8292160D4D31FC83FD0CDDB9FC190615.json`.
  - First build (one flat table from the 4-pass SQL) loaded no data: the user said the credential was set, but the open Desktop model still had `<server>`/`<database>` and the partition was never refreshed (state NoData, read at 17:20); the actual error was not seen.
  - Rebuilt 17:46 as a star schema: `Fact tr_oc_yyyymm`, 11 dimensions (Location → Shop → Tenant → Company → Tenant Group, Location → Area/Floor/Mall, Shop → Shop Type, Occupancy, Year Month), 11 relationships, report filter Year = 2018.
  - 17:50: the user filled the inventory (PostgreSQL `<postgres-server>`, database `<postgres-database>`); rebuilt with `--skip-extract`, 0 errors, 0 warnings. From this laptop TCP port 5432 on that host did not answer (Test-NetConnection), likely a firewall/VPN rule or another port: probably why no data loaded. Not yet refreshed against the database.
- **9D1AA7CA Sales Report (Fairthorne): stale.**
  - Its extract in `2_work` is broken: it was overwritten by 404s while `.env` pointed at <mstr-server>.
  - `3_powerbi/9D1AA7CA` uses the old table "Sales" and still has the Row Count measure.
  - To rebuild it, point `.env` back at <mstr-dev-server> and run with `--only 9D1AA7CA0A4728650BE279A1DC273F93 --overwrite`.

## Open items

- The shared team workflow doc (Claude Docs, https://claude.ai/code/artifact/5cf4b6c0-7340-4f4f-8236-407558b523b4) is outdated. It still has the old folder paths and says Row Count becomes COUNTROWS.
- Ideas not started yet:
  - a multi-environment inventory, with an Environment column and named logins in `.env`;
  - letting the runner work while Excel has the inventory open;
  - dynamic format strings for MSTR formats whose values vary in scale;
  - auto (tiled) page layouts;
  - a Power BI theme copying MSTR's look:
    - Open Sans font;
    - text #35383A, gridlines #F4F4F4, axis lines #E0E0E0;
    - purple selected selector tiles, which are MSTR's default style and not in the .mstr.
    The classic slicer can't colour selected tiles; the button slicer (`advancedSlicerVisual`) can, but its PBIR format for that is unverified;
  - the 249C303B project was saved from Desktop at 14:16 on 2026-10-21, after its last build, so it doesn't have the layout and title changes. Ask before rebuilding it with `--overwrite`;
  - decoding the drop-zone codes, which needs several dashboards with known settings.
