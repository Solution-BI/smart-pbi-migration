"""Batch-migrate MicroStrategy dashboards to Power BI projects, driven by migration_inventory.xlsx.

  1. Create the workbook once:  .venv\\Scripts\\python.exe scripts\\run_migration.py --init
  2. In 1_input\\migration_inventory.xlsx add one row per dashboard and set Status = Ready.
  3. Close Excel, then run:      .venv\\Scripts\\python.exe scripts\\run_migration.py

For every Ready row: extract (MicroStrategy API) -> transform -> Power BI project.
  intermediate files -> 2_work/<DashboardID>/1_extract, 2_transform
  final project      -> 3_powerbi/<first 8 chars of ID>/<Report>.pbip
The outcome is written back to the row and appended to 2_work/migration_runs.csv.

Options: --only <DASHBOARD_ID>   run one row whatever its status
         --dry-run               list what would run, change nothing
         --overwrite             rebuild Power BI projects that already exist
         --skip-extract          reuse the extract already in 2_work (MicroStrategy unreachable or unchanged)
         --inventory <file>      another workbook (default migration_inventory.xlsx)
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from fsutil import mirror, staging_dir
from paths import INVENTORY, POWERBI_DIR, ROOT, RUN_LOG, extract_dir, powerbi_project_dir, transform_dir

HERE = Path(__file__).resolve().parent
PY = sys.executable
ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{32}$")
MAX_ROWS = 1000  # rows pre-formatted for input

STATUSES = ["Ready", "Done", "Needs review", "Failed", "On hold"]
SOURCE_TYPES = ["snowflake", "postgresql", "sqlserver"]

# (header, width, help shown as a cell comment)
INPUT_COLUMNS = [
    ("Status", 14, "Ready = convert on the next run. The run sets Done, Needs review or Failed. "
                   "On hold = skip."),
    ("Project ID", 36, "MicroStrategy project ID. Blank = 'Default project ID' on the Settings sheet."),
    ("Dashboard ID", 36, "MicroStrategy dashboard (dossier) ID, 32 characters."),
    ("Source type", 13, "Database the dashboard's cubes read from: snowflake, postgresql or sqlserver. "
                        "Blank = Settings default."),
    ("Source server", 34, "e.g. xy12345.ap-southeast-2.snowflakecomputing.com. Blank = Settings default; "
                          "can also be set later in Power BI."),
    ("Source warehouse", 18, "Snowflake warehouse. Blank = Settings default."),
    ("Source database", 18, "Database name. Blank = Settings default."),
    ("Notes", 30, "Free text for the team; not used by the tool."),
]
OUTPUT_COLUMNS = [
    ("Dashboard name", 28, "Filled by the run."),
    ("Datasets", 9, "Tables created (one per MicroStrategy dataset)."),
    ("Pages", 7, "Report pages created."),
    ("Visuals", 8, "Visuals created (slicers not included)."),
    ("Slicers", 8, "Slicers created from MicroStrategy selectors."),
    ("Errors", 7, "Items needing manual work. See BUILD_PLAN.md in 2_work\\<Dashboard ID>\\2_transform."),
    ("Warnings", 9, "Items to check, e.g. fuzzy column matches."),
    ("Last run", 17, "When the row was last processed."),
    ("Result", 55, "What happened on the last run."),
    ("Output", 42, "Power BI project to open in Power BI Desktop."),
]
SETTINGS = [
    ("Default project ID", "741C53B811E551174FDA0080EFF5E6CA", "Used when a row's Project ID is blank."),
    ("Default source type", "snowflake", "snowflake, postgresql or sqlserver."),
    ("Default source server", "", "Optional. Can be set later in Power BI (Transform data > Edit parameters)."),
    ("Default source warehouse", "", "Snowflake only. Optional."),
    ("Default source database", "", "Optional."),
    ("Power BI project folder", "3_powerbi", "Where final projects are written, relative to the project "
                                             "folder. Keep it short: Windows limits paths to 260 characters."),
    ("Overwrite existing project", "No", "Yes = rebuild a dashboard's Power BI project if it already exists. "
                                         "Changes made to that project in Power BI Desktop are lost."),
]
HOW_TO = [
    "MicroStrategy to Power BI migration inventory",
    "",
    "1. Dashboards sheet: add one row per dashboard. Fill the blue columns; grey columns are filled by the run.",
    "2. Set Status = Ready for the dashboards to convert. Leave Project ID or Source columns blank to use the Settings defaults.",
    "3. Save and close this workbook (the run writes results back into it).",
    "4. In the project folder (POC) run:  .venv\\Scripts\\python.exe scripts\\run_migration.py",
    "5. Open each row's Output (.pbip, in 3_powerbi) in Power BI Desktop, set the source parameters if needed, "
    "and Refresh.",
    "",
    "Status after a run: Done = converted with no errors. Needs review = converted, but BUILD_PLAN.md lists items "
    "needing manual work. Failed = see Result.",
    "",
    "Folders: 1_input = this workbook (you edit) | 2_work = intermediate files per dashboard, incl. BUILD_PLAN.md "
    "(generated, safe to delete) | 3_powerbi = final Power BI projects | scripts = code.",
    "MicroStrategy server address and login stay in the .env file in the project folder, never in this workbook.",
    "Every run is also logged in 2_work\\migration_runs.csv.",
]

INPUT_FILL = PatternFill("solid", fgColor="1F4E79")
OUTPUT_FILL = PatternFill("solid", fgColor="595959")
HEADER_FONT = Font(bold=True, color="FFFFFF")
STATUS_FILLS = {"Done": "C6EFCE", "Needs review": "FFEB9C", "Failed": "FFC7CE", "Ready": "DDEBF7"}


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------

def summarize(dashboard_id: str) -> dict | None:
    """Counts from a dashboard's powerbi_build_spec.json, or None if it was never transformed."""
    spec_file = transform_dir(dashboard_id) / "powerbi_build_spec.json"
    if not spec_file.exists():
        return None
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    pages = spec["report"]["pages"]
    issues = spec["issues"]
    return {
        "Dashboard name": spec["source"]["dashboardName"],
        "Datasets": len({d for t in spec["semanticModel"]["tables"]  # a star schema has several tables per dataset
                         for d in ([t["mstrDatasetId"]] if t.get("mstrDatasetId") else t.get("mstrDatasetIds", []))}),
        "Pages": len(pages),
        "Visuals": sum(len(p["visuals"]) for p in pages),
        "Slicers": sum(len(p["slicers"]) for p in pages),
        "Errors": sum(1 for i in issues if i["severity"] == "error"),
        "Warnings": sum(1 for i in issues if i["severity"] == "warning"),
    }


def init_workbook(path: Path, seed: list[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Dashboards"
    columns = INPUT_COLUMNS + OUTPUT_COLUMNS
    for idx, (name, width, help_text) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=idx, value=name)
        cell.fill = INPUT_FILL if idx <= len(INPUT_COLUMNS) else OUTPUT_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        cell.comment = Comment(help_text, "Migration tool", width=260, height=90)
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = width
        if name in ("Project ID", "Dashboard ID"):
            ws.column_dimensions[letter].number_format = "@"  # keep IDs as text
    ws.freeze_panes = "D2"
    last = get_column_letter(len(columns))
    ws.auto_filter.ref = f"A1:{last}{MAX_ROWS}"

    col = {name: idx for idx, (name, _, _) in enumerate(columns, start=1)}
    status_dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True)
    source_dv = DataValidation(type="list", formula1='"' + ",".join(SOURCE_TYPES) + '"', allow_blank=True)
    ws.add_data_validation(status_dv)
    ws.add_data_validation(source_dv)
    status_dv.add(f"A2:A{MAX_ROWS}")
    src_letter = get_column_letter(col["Source type"])
    source_dv.add(f"{src_letter}2:{src_letter}{MAX_ROWS}")
    for status, color in STATUS_FILLS.items():
        ws.conditional_formatting.add(f"A2:A{MAX_ROWS}", CellIsRule(
            operator="equal", formula=[f'"{status}"'], fill=PatternFill("solid", fgColor=color)))

    for r, row in enumerate(seed, start=2):
        for name, value in row.items():
            ws.cell(row=r, column=col[name], value=value)

    st = wb.create_sheet("Settings")
    for idx, (name, width) in enumerate((("Setting", 28), ("Value", 36), ("Description", 90)), start=1):
        cell = st.cell(row=1, column=idx, value=name)
        cell.fill, cell.font = INPUT_FILL, HEADER_FONT
        st.column_dimensions[get_column_letter(idx)].width = width
    for r, (name, value, desc) in enumerate(SETTINGS, start=2):
        st.cell(row=r, column=1, value=name).font = Font(bold=True)
        st.cell(row=r, column=2, value=value).number_format = "@"
        st.cell(row=r, column=3, value=desc)
    yes_no = DataValidation(type="list", formula1='"Yes,No"', allow_blank=False)
    st_source = DataValidation(type="list", formula1='"' + ",".join(SOURCE_TYPES) + '"', allow_blank=True)
    st.add_data_validation(yes_no)
    st.add_data_validation(st_source)
    for r, (name, _, _) in enumerate(SETTINGS, start=2):
        if name == "Overwrite existing project":
            yes_no.add(f"B{r}")
        elif name == "Default source type":
            st_source.add(f"B{r}")

    how = wb.create_sheet("How to use")
    how.column_dimensions["A"].width = 140
    for r, line in enumerate(HOW_TO, start=1):
        how.cell(row=r, column=1, value=line).font = Font(bold=(r == 1), size=13 if r == 1 else 11)
    wb.save(path)


def text(value) -> str:
    return "" if value is None else str(value).strip()


def read_shared(path: Path) -> bytes:
    """Read a file even while Excel has it open: Python's open() is refused then, a fully shared open is not."""
    if os.name != "nt":
        return path.read_bytes()
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    generic_read, share_all, open_existing = 0x80000000, 0x1 | 0x2 | 0x4, 3
    handle = kernel32.CreateFileW(str(path), generic_read, share_all, None, open_existing, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

class StepFailed(Exception):
    pass


def update_project(new: Path, target: Path) -> str:
    """Copy a project built in a local temp folder into place (see fsutil). Returns a note for Result."""
    try:
        leftovers = mirror(new, target)
    except PermissionError as e:
        raise StepFailed(f"Could not update {target.name}: {e.filename} is in use (Power BI Desktop, File "
                         "Explorer preview or OneDrive sync). Close it and run again.")
    return f" Could not remove old file(s) still in use: {', '.join(leftovers)}." if leftovers else ""


def run_step(label: str, cmd: list[str]) -> None:
    print(f"    {label} ...", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout).splitlines() if ln.strip()]
        raise StepFailed(f"{label} failed: {' | '.join(lines[-3:])[:400]}")


def process_row(row: dict, settings: dict, project_root: Path, overwrite: bool, skip_extract: bool = False) -> dict:
    """Run extract -> transform -> project for one row. Returns the output cells to write."""
    did = row["Dashboard ID"]
    pid = row["Project ID"] or text(settings.get("Default project ID"))
    source = {k: row[f"Source {k}"] or text(settings.get(f"Default source {k}"))
              for k in ("type", "server", "warehouse", "database")}
    if not pid:
        raise StepFailed("No Project ID on the row and no Default project ID in Settings.")
    if not source["type"]:
        raise StepFailed("No Source type on the row and no Default source type in Settings.")

    project_dir = powerbi_project_dir(did, project_root)
    if project_dir.exists():
        if not overwrite:
            return {"skip": f"Power BI project already exists in {project_dir}. "
                            "Set 'Overwrite existing project' = Yes on Settings to rebuild it."}
        if not any(project_dir.glob("*.pbip")):
            raise StepFailed(f"{project_dir} exists but is not a Power BI project; not touching it.")

    if not skip_extract:
        run_step("extract", [PY, str(HERE / "extract_dashboard_metadata.py"), did, "--project-id", pid])
    elif (extract_dir(did) / "migration_spec.json").exists():
        print("    extract skipped: using the one in 2_work", flush=True)
    else:
        raise StepFailed("--skip-extract: no earlier extract in 2_work for this dashboard; run without it.")
    transform = [PY, str(HERE / "transform_to_powerbi.py"), did, "--source-type", source["type"].lower()]
    for key in ("server", "warehouse", "database"):
        if source[key]:
            transform += [f"--{key}", source[key]]
    run_step("transform", transform)
    temp_root = staging_dir("pbip")
    try:
        staging = temp_root / did[:8]
        run_step("build Power BI project", [PY, str(HERE / "generate_pbip_report.py"), did, "--out", str(staging)])
        note = update_project(staging, project_dir)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    summary = summarize(did)
    pbip = next(project_dir.glob("*.pbip"))
    status = "Needs review" if summary["Errors"] else "Done"
    result = ("Converted." if not summary["Errors"] else
              f"Converted; {summary['Errors']} item(s) need manual work, see BUILD_PLAN.md.") + note
    return {**summary, "Status": status, "Result": result,
            "Output": str(pbip.relative_to(ROOT)) if pbip.is_relative_to(ROOT) else str(pbip)}


def append_log(entry: dict) -> None:
    log = RUN_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    with open(log, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(entry))
        if new:
            writer.writeheader()
        writer.writerow(entry)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", type=Path, default=INVENTORY)
    ap.add_argument("--init", action="store_true", help="create the inventory workbook")
    ap.add_argument("--only", help="process this dashboard ID only, whatever its status")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="rebuild existing Power BI projects for this run (same as Settings 'Overwrite' = Yes)")
    ap.add_argument("--skip-extract", action="store_true",
                    help="reuse the extract already in 2_work instead of calling MicroStrategy")
    args = ap.parse_args()

    if args.init:
        if args.inventory.exists():
            raise SystemExit(f"{args.inventory} already exists; not overwriting it.")
        poc = "9D1AA7CA0A4728650BE279A1DC273F93"
        seed = {"Status": "Done", "Project ID": "741C53B811E551174FDA0080EFF5E6CA", "Dashboard ID": poc,
                "Source type": "snowflake", "Notes": "POC dashboard"}
        if (s := summarize(poc)):
            seed.update(s, Result="Converted (POC).",
                        Output=next((str(p.relative_to(ROOT)) for p in powerbi_project_dir(poc).glob("*.pbip")), ""))
        args.inventory.parent.mkdir(parents=True, exist_ok=True)
        init_workbook(args.inventory, [seed])
        print(f"Created {args.inventory}")
        return

    if not args.inventory.exists():
        raise SystemExit(f"{args.inventory} not found. Create it with: run_migration.py --init")
    if not args.dry_run:
        try:
            with open(args.inventory, "r+b"):
                pass
        except PermissionError:
            raise SystemExit(f"{args.inventory.name} is open in Excel. Save and close it, then run again.")

    wb = load_workbook(io.BytesIO(read_shared(args.inventory)))
    ws = wb["Dashboards"]
    header = {text(c.value): c.column for c in ws[1] if c.value}
    missing = [n for n, _, _ in INPUT_COLUMNS + OUTPUT_COLUMNS if n not in header]
    if missing:
        raise SystemExit(f"Dashboards sheet is missing columns: {', '.join(missing)}")
    settings = {text(r[0].value): r[1].value for r in wb["Settings"].iter_rows(min_row=2) if r[0].value}
    project_root = Path(text(settings.get("Power BI project folder")) or POWERBI_DIR)
    if not project_root.is_absolute():
        project_root = ROOT / project_root
    overwrite = args.overwrite or text(settings.get("Overwrite existing project")).lower() in ("yes", "y", "true")

    todo = []
    for r in range(2, ws.max_row + 1):
        raw_id = ws.cell(row=r, column=header["Dashboard ID"]).value
        if raw_id is None or text(raw_id) == "":
            continue
        row = {n: text(ws.cell(row=r, column=header[n]).value) for n, _, _ in INPUT_COLUMNS}
        selected = (row["Dashboard ID"].upper() == args.only.upper()) if args.only else row["Status"] == "Ready"
        if selected:
            todo.append((r, row, raw_id))
    if args.only and not todo:
        raise SystemExit(f"Dashboard {args.only} is not on the Dashboards sheet of {args.inventory.name}. "
                         "Add a row with its Project ID and Dashboard ID first.")
    print(f"{len(todo)} dashboard(s) to process from {args.inventory.name}")

    counts = {}
    for r, row, raw_id in todo:
        did = row["Dashboard ID"].upper()
        print(f"\n[{did}] row {r}")
        if args.dry_run:
            continue
        out = {}
        try:
            if not isinstance(raw_id, str) or not ID_PATTERN.match(did):
                raise StepFailed("Dashboard ID is not a 32-character ID (Excel may have turned it into a number; "
                                 "format the column as Text and paste it again).")
            row["Dashboard ID"] = did
            out = process_row(row, settings, project_root, overwrite, args.skip_extract)
        except StepFailed as e:
            out = {"Status": "Failed", "Result": str(e)}
        except Exception as e:  # keep the batch going; record what broke
            out = {"Status": "Failed", "Result": f"Unexpected error: {type(e).__name__}: {e}"[:400]}

        if "skip" in out:
            out = {"Result": out["skip"]}
        out["Last run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        for name, value in out.items():
            ws.cell(row=r, column=header[name], value=value)
        wb.save(args.inventory)  # after every row, so progress survives a crash
        status = out.get("Status", row["Status"])
        counts[status] = counts.get(status, 0) + 1
        print(f"    -> {status}: {out['Result']}")
        append_log({"timestamp": out["Last run"], "dashboard_id": did,
                    "project_id": row["Project ID"] or text(settings.get("Default project ID")),
                    "dashboard_name": out.get("Dashboard name", ""), "status": status,
                    "errors": out.get("Errors", ""), "warnings": out.get("Warnings", ""),
                    "result": out["Result"], "output": out.get("Output", "")})

    if not args.dry_run and todo:
        print("\nSummary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
