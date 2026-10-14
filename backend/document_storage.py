"""Durable original uploads in a private Supabase Storage bucket, with Kaggle disk as a cache only.

Mode (AI_TUTOR_DOCUMENT_STORAGE): "supabase" by default when DATABASE_URL selects PostgreSQL,
"local" otherwise (local development keeps files under data/users/<owner>/). In supabase mode
SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required: there is no silent fallback to the
ephemeral local disk.

Objects are content-addressed and owner-scoped -- "<owner_id>/<sha256><ext>" -- so a path never
changes meaning, re-uploading identical bytes is idempotent, and one user's path can never name
another user's file. The service-role key is only ever sent to Supabase from this server; the
browser receives file bytes through the authenticated /api/sources/{id}/content route.
"""

import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from backend import db
from config import DATA_DIR, SUPABASE_STORAGE_BUCKET

SUPABASE = "supabase"
LOCAL = "local"
CONTENT_TYPES = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_http_transport = None  # tests inject an httpx.MockTransport


class DocumentStorageError(RuntimeError):
    """Durable storage failed; messages never contain credentials."""


class DocumentStorageConfigurationError(DocumentStorageError):
    """Durable storage is required but not configured correctly."""


def storage_mode() -> str:
    mode = os.getenv("AI_TUTOR_DOCUMENT_STORAGE", "").strip().lower()
    if not mode:
        return SUPABASE if db.selected_backend() == db.POSTGRES else LOCAL
    if mode not in (SUPABASE, LOCAL):
        raise DocumentStorageConfigurationError("AI_TUTOR_DOCUMENT_STORAGE must be 'supabase' or 'local'.")
    return mode


def durable_storage_enabled() -> bool:
    return storage_mode() == SUPABASE


def bucket_name() -> str:
    return SUPABASE_STORAGE_BUCKET


def _settings() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise DocumentStorageConfigurationError(
            "Durable document storage needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY "
            "(Kaggle Secrets); refusing to keep uploads only on the temporary local disk."
        )
    if not url.startswith("https://") and not url.startswith("http://"):
        raise DocumentStorageConfigurationError("SUPABASE_URL must be the project URL (https://<ref>.supabase.co).")
    return url, key


def _client() -> httpx.Client:
    url, key = _settings()
    headers = {"apikey": key}
    if key.count(".") == 2:  # legacy service_role JWT; new sb_secret_ keys go in apikey only
        headers["Authorization"] = f"Bearer {key}"
    return httpx.Client(
        base_url=f"{url}/storage/v1",
        headers=headers,
        timeout=_TIMEOUT,
        transport=_http_transport,
    )


def _fail(action: str, response: httpx.Response | None = None, error: Exception | None = None) -> DocumentStorageError:
    detail = f"HTTP {response.status_code}" if response is not None else type(error).__name__
    return DocumentStorageError(f"Supabase Storage {action} failed ({detail}).")


def object_path(owner_id: str, file_hash: str, suffix: str) -> str:
    """Immutable, owner-scoped object key: identical bytes always map to the same key."""
    suffix = suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise ValueError("Only PDF and TXT files can be stored.")
    if not file_hash or any(char not in "0123456789abcdef" for char in file_hash):
        raise ValueError("Object paths are addressed by a SHA-256 hex digest.")
    return f"{owner_id}/{file_hash}{suffix}"


def _owned(owner_id: str, path: str) -> bool:
    return bool(path) and path.startswith(f"{owner_id}/") and ".." not in path


def file_sha256(path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 16), b""):
            sha256.update(block)
    return sha256.hexdigest()


# -- bucket -----------------------------------------------------------------------------------

def ensure_bucket() -> str:
    """Make sure the private bucket exists (created private if missing); refuse a public bucket."""
    name = bucket_name()
    with _client() as client:
        try:
            response = client.get(f"/bucket/{name}")
            if response.status_code in (400, 404):
                created = client.post("/bucket", json={"id": name, "name": name, "public": False})
                if created.status_code not in (200, 201, 409):
                    raise _fail("bucket creation", created)
                response = client.get(f"/bucket/{name}")
            if response.status_code != 200:
                raise _fail("bucket lookup", response)
        except httpx.HTTPError as error:
            raise _fail("bucket lookup", error=error) from None
    if response.json().get("public"):
        raise DocumentStorageConfigurationError(
            f"Storage bucket '{name}' is public. Make it private in Supabase (Storage > bucket settings)."
        )
    return name


# -- objects ----------------------------------------------------------------------------------

def upload(owner_id: str, local_path: Path, file_hash: str) -> str:
    """Store the original file durably and return its object path. Idempotent for identical bytes."""
    local_path = Path(local_path)
    path = object_path(owner_id, file_hash, local_path.suffix)
    with _client() as client:
        try:
            response = client.post(
                f"/object/{bucket_name()}/{path}",
                content=local_path.read_bytes(),
                headers={"Content-Type": CONTENT_TYPES[local_path.suffix.lower()], "x-upsert": "true",
                         "cache-control": "max-age=3600"},
            )
        except httpx.HTTPError as error:
            raise _fail("upload", error=error) from None
    if response.status_code not in (200, 201):
        raise _fail("upload", response)
    return path


def download(owner_id: str, path: str, destination: Path, expected_hash: str | None = None) -> Path:
    """Fetch one of the owner's objects into the local cache, verifying its SHA-256."""
    if not _owned(owner_id, path):
        raise DocumentStorageError("Refusing to read another owner's object.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256()
    handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".download-")
    try:
        with os.fdopen(handle, "wb") as file, _client() as client:
            try:
                with client.stream("GET", f"/object/{bucket_name()}/{path}") as response:
                    if response.status_code != 200:
                        raise _fail("download", response)
                    for block in response.iter_bytes():
                        sha256.update(block)
                        file.write(block)
            except httpx.HTTPError as error:
                raise _fail("download", error=error) from None
        if expected_hash and sha256.hexdigest() != expected_hash:
            raise DocumentStorageError("Downloaded document does not match its recorded SHA-256.")
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def delete(owner_id: str, path: str) -> None:
    """Remove one of the owner's objects. A missing object counts as deleted."""
    if not _owned(owner_id, path):
        raise DocumentStorageError("Refusing to delete another owner's object.")
    with _client() as client:
        try:
            response = client.request("DELETE", f"/object/{bucket_name()}", json={"prefixes": [path]})
        except httpx.HTTPError as error:
            raise _fail("delete", error=error) from None
    if response.status_code not in (200, 204, 404):
        raise _fail("delete", response)


# -- local cache ------------------------------------------------------------------------------

def cache_path(owner_id: str, document_id: str) -> Path:
    return DATA_DIR / "users" / owner_id / Path(document_id).name


def ensure_local_copy(document: dict) -> Path:
    """Local file for one registered document, downloading it from durable storage when the
    temporary Kaggle cache lacks it (or holds different bytes)."""
    owner_id, document_id = document["owner_id"], document["document_id"]
    expected_hash = str(document.get("hash") or "")
    target = cache_path(owner_id, document_id)
    if target.is_file() and (not expected_hash or file_sha256(target) == expected_hash):
        return target
    path = str(document.get("object_path") or "")
    if path and durable_storage_enabled():
        return download(owner_id, path, target, expected_hash or None)
    recorded = Path(str(document.get("path") or ""))
    if recorded.is_file() and (not expected_hash or file_sha256(recorded) == expected_hash):
        return recorded
    raise FileNotFoundError("Original upload is unavailable.")
