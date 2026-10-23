"""File operations that cope with OneDrive.

OneDrive briefly locks files it is uploading, so deleting or renaming whole folders inside the project
fails at random. Output is therefore built in a local temporary folder (outside OneDrive) and mirrored
into place file by file, with short retries.
"""
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

KEEP_DIRS = {".pbi"}  # Power BI Desktop's per-user settings and data cache inside a project


def retry(action, attempts: int = 6):
    """Run a file operation, retrying on PermissionError (a file held by OneDrive or an indexer)."""
    for i in range(attempts):
        try:
            return action()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5 * (i + 1))


def staging_dir(label: str) -> Path:
    """New empty folder under the local temp directory."""
    return Path(tempfile.mkdtemp(prefix=f"mstr2pbi_{label}_"))


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)  # read-only files
        path.unlink()


def mirror(src: Path, dst: Path) -> list[str]:
    """Make dst match src: copy every file of src, delete files src no longer has (except KEEP_DIRS).
    Returns the paths that could not be removed because they stayed locked."""
    dst.mkdir(parents=True, exist_ok=True)
    wanted = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    for rel in sorted(wanted):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        retry(lambda: shutil.copyfile(src / rel, target))

    leftovers = []
    for path in sorted(dst.rglob("*"), key=lambda p: len(p.parts), reverse=True):  # children first
        rel = path.relative_to(dst)
        if KEEP_DIRS & set(rel.parts):
            continue
        try:
            if path.is_file() and rel not in wanted:
                retry(lambda: _unlink(path))
            elif path.is_dir() and not any(path.iterdir()):
                retry(path.rmdir)
        except PermissionError:
            leftovers.append(str(rel))
    return leftovers
