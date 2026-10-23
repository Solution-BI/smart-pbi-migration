"""Example: pull a report or cube into a pandas DataFrame and save it as CSV.

Usage:
  .venv\\Scripts\\python.exe scripts\\example_extract.py report <REPORT_ID>
  .venv\\Scripts\\python.exe scripts\\example_extract.py cube   <CUBE_ID>

Saves the data as CSV in 2_work/data_extracts/.

Requires MSTR_PROJECT_NAME or MSTR_PROJECT_ID to be set in .env.
"""
import sys

from mstrio.project_objects import OlapCube, Report

from mstr_connection import get_connection
from paths import WORK_DIR


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("report", "cube"):
        print(__doc__)
        sys.exit(1)

    kind, object_id = sys.argv[1], sys.argv[2]
    conn = get_connection()
    try:
        obj = Report(conn, id=object_id) if kind == "report" else OlapCube(conn, id=object_id)
        df = obj.to_dataframe()
        print(f"{obj.name}: {len(df)} rows x {len(df.columns)} columns")
        print(df.head())

        out_dir = WORK_DIR / "data_extracts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{kind}_{object_id}.csv"
        df.to_csv(out_file, index=False)
        print(f"\nSaved to {out_file}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
