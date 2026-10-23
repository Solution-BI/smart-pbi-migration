# MicroStrategy → Power BI migration POC

## Folder map

```
POC\
├── 1_input\                        YOU EDIT
│   ├── migration_inventory.xlsx      dashboards and documents to migrate (Status = Ready) + Settings sheet
│   └── overrides\<ID>.json           optional: chart types, titles, positions the API cannot give (documents)
│
├── 2_work\                         INTERMEDIATE: generated, safe to delete (rebuilt on the next run)
│   ├── <DashboardID>\
│   │   ├── 1_extract\                MicroStrategy API responses (raw\), migration_spec.json / .md
│   │   │                               raw\dashboard.pdf = each page as MicroStrategy draws it (compare with Power BI)
│   │   └── 2_transform\              powerbi_build_spec.json, BUILD_PLAN.md  <- issues to fix are listed here
│   └── migration_runs.csv            history of every run
│
├── 3_powerbi\                      FINAL: open these in Power BI Desktop
│   ├── <first 8 chars of ID>\<Report>.pbip
│   └── manual_tests\                 files saved by hand during the POC
│
├── scripts\                        code, no need to edit
├── .env                            YOU EDIT ONCE: MicroStrategy login (never share or commit)
├── .env.example                    template of .env
└── requirements.txt                Python packages
```

## Setup (once)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then fill in the MicroStrategy values
```

## Migrate dashboards

1. Open `1_input\migration_inventory.xlsx`.
2. On the **Dashboards** sheet, add one row per dashboard and set **Status = Ready**:
   - Fill the blue columns: Project ID, Dashboard ID, and optionally the source columns.
   - Blank values use the **Settings** sheet.
3. Save and close Excel, then run from the POC folder:

```powershell
.\.venv\Scripts\python.exe scripts\run_migration.py            # every Ready row
.\.venv\Scripts\python.exe scripts\run_migration.py --dry-run  # only list what would run
.\.venv\Scripts\python.exe scripts\run_migration.py --only <DASHBOARD_ID>
.\.venv\Scripts\python.exe scripts\run_migration.py --only <DASHBOARD_ID> --overwrite   # rebuild an existing project
.\.venv\Scripts\python.exe scripts\run_migration.py --only <DASHBOARD_ID> --overwrite --skip-extract   # rebuild without calling MicroStrategy
```

Dashboards on Intelligent Cubes built on the project schema become a **star schema**: one fact table per database table the cube aggregates and one dimension per lookup table, related as the cube's SQL joins them. Each table reads its database table directly. The cube's filter becomes a report-level filter.

New outputs are built in a local temp folder and then copied into place file by file. OneDrive briefly locks files it is syncing, which makes deleting or renaming whole folders inside the project unreliable.

For each row, the run does three things:
- It writes the intermediate files to `2_work\<DashboardID>\` and the Power BI project to `3_powerbi\`.
- It fills the grey columns of the row: Status (Done, Needs review or Failed), counts, Result and Output.
- It adds a line to `2_work\migration_runs.csv`.

To finish each dashboard:
1. Open its `.pbip` in Power BI Desktop.
2. Click Refresh.

The Source columns of the row, or the Settings defaults, are written into the Power BI parameters. You only need Transform data → Edit parameters if they were blank.

After a rebuild, Power BI Desktop does not reload the project by itself. Close it **without saving**, because saving writes the old version back, then reopen the `.pbip`.

A Needs review row means items in `2_work\<DashboardID>\2_transform\BUILD_PLAN.md` need manual work.

## Scripts (reference)

| Script | Stage | What it does |
|---|---|---|
| `run_migration.py` | all | Batch runner driven by the inventory workbook (`--init` creates a new workbook) |
| `extract_dashboard_metadata.py` | 1 | Reads the dashboard, datasets and cube SQL from the MicroStrategy REST API, plus the dashboard's .mstr file |
| `mstr_file.py` | 1 | Reads chart colours, legend and data labels from the .mstr file (undocumented format, best effort) |
| `mstr_schema.py` | 2 | Schema cubes: which SQL column holds which metric, and the SQL's tables and joins as a star schema |
| `transform_to_powerbi.py` | 2 | Converts the definition into a Power BI build spec and `BUILD_PLAN.md` |
| `generate_pbip_report.py` | 3 | Writes the Power BI project: report pages and visuals, plus the model via `build_model_tmdl.ps1` |
| `build_model_tmdl.ps1`, `PbiSpecModel.ps1` | 3 | Build the data model files (Power BI Desktop installed, not necessarily open) |
| `deploy_model_to_desktop.ps1` | optional | Creates the data model inside an open Power BI Desktop file |
| `export_model_tmdl.ps1` | optional | Exports the model of an open Power BI Desktop file |
| `mstr_connection.py`, `paths.py` | shared | MicroStrategy login from `.env`; the folder layout |
| `test_connection.py`, `example_extract.py` | tools | Check the MicroStrategy login; export one report or cube to CSV |
