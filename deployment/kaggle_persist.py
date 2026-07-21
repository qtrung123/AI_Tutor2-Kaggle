"""Persist AI Tutor state/cache across Kaggle sessions using a private Kaggle Dataset
as remote storage. Used only by kaggle_run.ipynb; not part of the local app.

Requires the `kaggle` CLI (preinstalled in Kaggle notebooks) and KAGGLE_USERNAME /
KAGGLE_KEY in the environment.
"""
import json
import shutil
import subprocess
from pathlib import Path


def _ensure_kaggle_cli() -> None:
    if shutil.which("kaggle"):
        return
    subprocess.run(["pip", "install", "-q", "kaggle"], check=True)


def _run(args: list[str]) -> tuple[int, str, str]:
    _ensure_kaggle_cli()
    result = subprocess.run(["kaggle", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def restore_dataset(slug: str, dest_dir: Path) -> bool:
    """Download the latest version of `slug` into dest_dir.

    Returns True if a previous version was found and restored, False on a fresh
    (never-saved) dataset so the caller can start with empty state.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    code, _, err = _run(["datasets", "download", "-d", slug, "-p", str(dest_dir), "--unzip", "-q"])
    if code != 0:
        print(f"[kaggle-persist] No previous version of {slug} found: {err.strip()[-300:]}")
        return False
    print(f"[kaggle-persist] Restored {slug} into {dest_dir}")
    return True


def save_dataset(slug: str, src_dir: Path, title: str, message: str) -> None:
    """Push the contents of src_dir as a new version of dataset `slug`.

    Creates the dataset on the first call; versions it on every call after that.
    src_dir must contain only the files meant to be uploaded (a staging copy).
    """
    src_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"title": title, "id": slug, "licenses": [{"name": "CC0-1.0"}]}
    (src_dir / "dataset-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    code, out, err = _run(["datasets", "version", "-p", str(src_dir), "-m", message, "--dir-mode", "zip", "-q"])
    if code != 0 and ("not found" in err.lower() or "404" in err or "could not find" in err.lower()):
        print(f"[kaggle-persist] Dataset {slug} does not exist yet, creating it...")
        code, out, err = _run(["datasets", "create", "-p", str(src_dir), "--dir-mode", "zip", "-q"])
    if code != 0:
        raise RuntimeError(f"Could not save dataset {slug}: {(err or out).strip()}")
    print(f"[kaggle-persist] Saved {slug}: {(out or 'ok').strip()}")
