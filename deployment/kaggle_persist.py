"""Persist AI Tutor state/cache across Kaggle sessions using a private Kaggle Dataset
as remote storage. Used only by kaggle_run.ipynb; not part of the local app.

Requires the `kaggle` CLI (preinstalled in Kaggle notebooks) and KAGGLE_USERNAME /
KAGGLE_KEY in the environment.

Every SQLite database in a snapshot is a self-contained copy (the notebook writes it with SQLite's
backup API), so the snapshot must never carry the live database's -journal / -wal / -shm files:
copied before that backup, they are older than it, and SQLite would replay them over the restored
database on first open -- silently rolling back everything written in between. Saving removes them
from the staging copy and checks every database; restoring removes stale ones (snapshots saved
before this check may contain them) and refuses a corrupted or incomplete state snapshot, so the
caller keeps its "nothing restored" path and never overwrites the Dataset with it.
"""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _ensure_kaggle_cli() -> None:
    if shutil.which("kaggle"):
        return
    subprocess.run(["pip", "install", "-q", "kaggle"], check=True)


def _run(args: list[str]) -> tuple[int, str, str]:
    _ensure_kaggle_cli()
    result = subprocess.run(["kaggle", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_HEADER
    except OSError:
        return False


def remove_sqlite_sidecars(root: Path) -> list[Path]:
    """Delete the -journal / -wal / -shm file next to each SQLite database under `root` (a snapshot
    copy, never the live data). Returns what was removed."""
    removed = []
    for database in [path for path in root.rglob("*") if path.is_file() and _is_sqlite_file(path)]:
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_file():
                sidecar.unlink()
                removed.append(sidecar)
    return removed


def snapshot_problems(root: Path) -> list[str]:
    """Why the snapshot under `root` must not be used ([] = usable): an SQLite database that fails
    SQLite's quick_check (corrupted / truncated), or AI Tutor state with no database under data/
    (incomplete: it would start the app empty and the next save would overwrite the Dataset)."""
    problems = []
    databases = sorted(path for path in root.rglob("*") if path.is_file() and _is_sqlite_file(path))
    for database in databases:
        relative = database.relative_to(root).as_posix()
        try:
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            problems.append(f"{relative}: unreadable SQLite database ({error})")
            continue
        if result != "ok":
            problems.append(f"{relative}: failed SQLite quick_check ({result})")
    is_state = (root / "data").is_dir() or (root / "vectorstore").is_dir()
    data_dir = root / "data"
    if is_state and not any(data_dir in database.parents for database in databases):
        problems.append("data/ has no SQLite database (incomplete state snapshot)")
    return problems


def restore_dataset(slug: str, dest_dir: Path) -> bool:
    """Download the latest version of `slug` into dest_dir.

    Returns True if a previous version was found, cleaned of stale SQLite journals and verified;
    False on a fresh (never-saved) dataset -- or a corrupted / incomplete one, with the reason
    printed -- so the caller starts with empty state and does not overwrite the Dataset.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    code, _, err = _run(["datasets", "download", "-d", slug, "-p", str(dest_dir), "--unzip", "-q"])
    if code != 0:
        print(f"[kaggle-persist] No previous version of {slug} found: {err.strip()[-300:]}")
        return False
    for sidecar in remove_sqlite_sidecars(dest_dir):
        print(f"[kaggle-persist] Removed stale SQLite journal from the snapshot: {sidecar.relative_to(dest_dir).as_posix()}")
    problems = snapshot_problems(dest_dir)
    if problems:
        print(f"[kaggle-persist] ERROR: the saved snapshot of {slug} is not usable and was NOT restored: "
              + "; ".join(problems))
        return False
    print(f"[kaggle-persist] Restored {slug} into {dest_dir}")
    return True


def save_dataset(slug: str, src_dir: Path, title: str, message: str) -> None:
    """Push the contents of src_dir as a new version of dataset `slug`.

    Creates the dataset on the first call; versions it on every call after that.
    src_dir must contain only the files meant to be uploaded (a staging copy).
    """
    src_dir.mkdir(parents=True, exist_ok=True)
    remove_sqlite_sidecars(src_dir)
    problems = snapshot_problems(src_dir)
    if problems:
        raise RuntimeError(f"Refusing to save {slug}: " + "; ".join(problems))
    metadata = {"title": title, "id": slug, "licenses": [{"name": "CC0-1.0"}]}
    (src_dir / "dataset-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    code, out, err = _run(["datasets", "version", "-p", str(src_dir), "-m", message, "--dir-mode", "zip", "-q"])
    if code != 0 and ("not found" in err.lower() or "404" in err or "could not find" in err.lower()):
        print(f"[kaggle-persist] Dataset {slug} does not exist yet, creating it...")
        code, out, err = _run(["datasets", "create", "-p", str(src_dir), "--dir-mode", "zip", "-q"])
    if code != 0:
        raise RuntimeError(f"Could not save dataset {slug}: {(err or out).strip()}")
    print(f"[kaggle-persist] Saved {slug}: {(out or 'ok').strip()}")
