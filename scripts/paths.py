"""Folder layout of the migration project. Folder names are defined here only.

  1_input/     what you edit: migration_inventory.xlsx, overrides/<DashboardID>.json (optional corrections)
  2_work/      generated per dashboard, safe to delete: 2_work/<DashboardID>/1_extract, 2_transform
  3_powerbi/   final Power BI projects: 3_powerbi/<first 8 chars of ID>/<Report>.pbip
  scripts/     the code
  .env         MicroStrategy login and connection settings (project root)
"""
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
ENV_FILE = ROOT / ".env"
INPUT_DIR = ROOT / "1_input"
WORK_DIR = ROOT / "2_work"
POWERBI_DIR = ROOT / "3_powerbi"
TEMPLATES_DIR = SCRIPTS_DIR / "templates"
INVENTORY = INPUT_DIR / "migration_inventory.xlsx"
RUN_LOG = WORK_DIR / "migration_runs.csv"


def extract_dir(dashboard_id: str) -> Path:
    """Stage 1 output: raw API responses, migration_spec.json/.md."""
    return WORK_DIR / dashboard_id.upper() / "1_extract"


def transform_dir(dashboard_id: str) -> Path:
    """Stage 2 output: powerbi_build_spec.json, BUILD_PLAN.md."""
    return WORK_DIR / dashboard_id.upper() / "2_transform"


def overrides_file(dashboard_id: str) -> Path:
    """Optional per-dashboard corrections applied by the transform: what the MicroStrategy API cannot tell
    (chart type of a document grid, titles, positions)."""
    return INPUT_DIR / "overrides" / f"{dashboard_id.upper()}.json"


def powerbi_project_dir(dashboard_id: str, root: Path = POWERBI_DIR) -> Path:
    """Final Power BI project folder. Kept short: Windows paths stop at 260 characters."""
    return root / dashboard_id.upper()[:8]
