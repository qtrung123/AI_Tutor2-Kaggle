"""Sources/Documents routes: list, upload/index, serve the original file, delete, and the quiz
document list.

Deleting a document also clears every feature's data for it (quiz, summary, flashcards, planner
and chat sources) through each feature's own store.
"""
import shutil
import threading
from pathlib import Path
from typing import BinaryIO, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import document_storage, google_calendar_sync, perf_timing, study_planner_store, subject_store
from backend.api.deps import require_current_user
from backend.conversation_store import remove_source_from_conversations
from backend.flashcard_store import delete_document_flashcards
from backend.indexed_document_store import get_indexed_document
from backend.ingest import delete_indexed_file, index_files
from backend.quiz_common import list_indexed_documents
from backend.quiz_store import delete_document_quiz_data
from backend.rag_service import list_uploaded_sources
from backend.summary_store import delete_document_summaries
from config import DATA_DIR

router = APIRouter()


class SourceSummary(BaseModel):
    """
    Summary of an indexed/uploaded document.

    This shape is used by the AI Tutor source list.
    """
    sourceId: int
    title: str
    chunks: int
    path: str


class UploadResponse(BaseModel):
    """
    Response returned after uploading and indexing documents.

    It includes indexing stats plus the refreshed source list so the frontend can
    update immediately without making another GET /api/sources call.
    """
    new_files: int
    new_chunks: int
    skipped_files: list[str]
    total_indexed_files: int
    sources: list[SourceSummary]
    # Only when the upload chose a subject: it and the uploaded documents now in it.
    subject_id: Optional[str] = None
    subject_document_ids: Optional[list[str]] = None
    subject_kept_document_ids: Optional[list[str]] = None   # already in a subject: left where they are


class DeleteSourceResponse(BaseModel):
    """
    Response returned after deleting one indexed document.

    It includes the refreshed source list for the same reason as UploadResponse.
    """
    deleted: str
    deleted_chunks: int
    total_indexed_files: int
    sources: list[SourceSummary]


class DocumentSummary(BaseModel):
    """
    Summary of an indexed document used by the quiz document dropdown.

    This is similar to SourceSummary, but it uses id/title/chunks because the
    Practice page needs the document id as a select value.
    """
    id: str
    title: str
    chunks: int
    topics: list[dict] = Field(default_factory=list)


@router.get("/api/sources", response_model=list[SourceSummary])
def sources(current_user: dict = Depends(require_current_user)) -> list[SourceSummary]:
    """
    Return all indexed sources for the AI Tutor source panel.

    Data comes from the owner-scoped SQLite indexed document registry.
    """
    try:
        return [SourceSummary(**source) for source in list_uploaded_sources(current_user["id"])]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load uploaded sources. Original error: {error}",
        ) from error


@router.get("/api/sources/{document_id}/content")
def source_content(document_id: str, current_user: dict = Depends(require_current_user)) -> FileResponse:
    """Serve one owned original upload for the Study Session document viewer.

    With durable storage the Kaggle disk is only a cache: a missing local copy is downloaded from
    the private bucket (server-side; the browser never sees storage credentials)."""
    if document_storage.durable_storage_enabled():
        document = get_indexed_document(current_user["id"], document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found.")
        try:
            path = document_storage.ensure_local_copy(document)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Original upload is unavailable.") from error
        except document_storage.DocumentStorageError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")
    source = next((item for item in list_uploaded_sources(current_user["id"]) if item["title"] == document_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Document not found.")
    path = Path(source["path"]).resolve()
    owner_dir = (DATA_DIR / "users" / current_user["id"]).resolve()
    if owner_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Original upload is unavailable.")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")


# One indexing run (or document delete) at a time, across all users. Before uploads ran in the
# threadpool they were serialized by the blocked event loop; this keeps that guarantee, so two
# uploads never write the same data/ file or index the same document concurrently, a delete never
# races an index of the same document, and Ollama embeds one document at a time. A waiting upload
# holds a threadpool thread, never the event loop. This is a per-process lock: it relies on the
# single Uvicorn worker that deployment/start_kaggle.sh starts; several workers would need a
# cross-process lock (e.g. a PostgreSQL advisory lock) instead.
_INDEXING_LOCK = threading.Lock()


def _store_and_index_uploads(owner_id: str, uploads: list[tuple[str, BinaryIO]], subject: Optional[dict] = None):
    """The blocking part of an upload (runs in a worker thread): save, index, list sources. An
    upload never touches the Study Planner: a document joins a plan only through Add Material.
    `uploads` holds the multipart files Starlette already spooled, copied to disk here in chunks so a waiting upload never keeps its bytes in memory. Stage timing reaches the
    request's [upload] line through the copied context.

    `subject` ({"subject_id"} or {"new_name"}, already validated) groups the uploaded files once
    they are indexed -- the new ones and those already in the library alike. A new subject is only
    created after indexing succeeded, so a failed upload leaves no empty subject behind."""
    with perf_timing.stage("lock_wait"):
        _INDEXING_LOCK.acquire()
    try:
        with perf_timing.stage("write"):
            user_data_dir = DATA_DIR / "users" / owner_id
            user_data_dir.mkdir(parents=True, exist_ok=True)
            saved_paths = []
            for file_name, source in uploads:
                save_path = user_data_dir / file_name
                source.seek(0)
                with open(save_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                perf_timing.count(bytes=save_path.stat().st_size)
                saved_paths.append(save_path)

        # index_files() handles duplicate detection through file hashes and
        # Writes owner-scoped Chroma vectors and SQLite indexed-document metadata.
        with perf_timing.stage("index"):
            result = index_files(saved_paths, owner_id)
        perf_timing.count(new_files=result["new_files"], new_chunks=result["new_chunks"],
                          skipped_files=len(result["skipped_files"]))
        if subject:
            # Under the indexing lock, so a delete of the same document cannot slip in between.
            # A file already filed in a subject keeps it (reported back, never silently moved).
            names = [path.name for path in saved_paths]
            subject_id = subject.get("subject_id") or subject_store.get_or_create_subject(owner_id, subject["new_name"])["subject_id"]
            try:
                assigned = subject_store.assign_documents(owner_id, names, subject_id, ignore_missing=True, only_unassigned=True)
            except subject_store.SubjectNotFound:
                # The subject was deleted while this upload was indexing: the files are stored and
                # indexed, so the upload succeeds and they stay Unassigned.
                subject_id, assigned = None, []
            registered = set(subject_store.subject_ids_by_document(owner_id)) if subject_id else set()
            kept = [name for name in dict.fromkeys(names) if name not in assigned and name in registered]
            result = {**result, "subject_id": subject_id, "subject_document_ids": assigned,
                      "subject_kept_document_ids": kept}
    finally:
        _INDEXING_LOCK.release()
    with perf_timing.stage("sources"):
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources(owner_id)]
    return result, sources_result


@router.post("/api/sources/upload", response_model=UploadResponse, response_model_exclude_none=True)
async def upload_sources(
    files: list[UploadFile] = File(...),
    subject_id: Optional[str] = Form(None),
    new_subject_name: Optional[str] = Form(None),
    current_user: dict = Depends(require_current_user),
) -> UploadResponse:
    """
    Upload PDF/TXT files and index them into the vectorstore.

    Full flow:
    1. Receive multipart files from frontend.
    2. Validate file extensions.
    3. Save files into data/.
    4. Call backend.ingest.index_files() to store the original durably (Supabase Storage on
       PostgreSQL deployments) and chunk/embed/store it in the vector store.
    5. Return indexing stats and refreshed source list -- only after everything is stored.

    Optional: `subject_id` (an existing subject of the user) or `new_subject_name` (reused when
    the user already has a subject with that name) puts the uploaded files in that subject.
    Parsing, topic extraction and indexing are the same with or without one.
    """
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one PDF or TXT file.")

    owner_id = current_user["id"]
    allowed_extensions = {".pdf", ".txt"}

    # With AI_TUTOR_PERF_TIMING=1: one [upload] line (stage times and counts only) after the
    # per-file [ingest] lines printed by index_files(); off by default.
    timer = perf_timing.stage_timer("[upload]")
    with perf_timing.recording(timer):
        try:
            # Every name is checked before anything is written.
            uploads = []
            for uploaded_file in files:
                # Keep only the final file name so a browser-provided path cannot
                # write outside data/.
                file_name = Path(uploaded_file.filename or "").name
                suffix = Path(file_name).suffix.lower()

                if not file_name or suffix not in allowed_extensions:
                    raise HTTPException(
                        status_code=400,
                        detail="Only PDF and TXT files can be uploaded.",
                    )

                uploads.append((file_name, uploaded_file.file))
            perf_timing.count(files=len(uploads))
            subject = _upload_subject(owner_id, subject_id, new_subject_name)   # checked before anything is written

            # Everything after this point is blocking (disk, Storage, PDF parsing, Ollama, database):
            # it runs in the threadpool so the event loop keeps serving Chat streams and other
            # requests. The response still waits for it, so success means it is all stored.
            result, sources_result = await run_in_threadpool(_store_and_index_uploads, owner_id, uploads, subject)
            perf_timing.outcome("ok")

            return UploadResponse(
                new_files=result["new_files"],
                new_chunks=result["new_chunks"],
                skipped_files=result["skipped_files"],
                total_indexed_files=result["total_indexed_files"],
                sources=sources_result,
                subject_id=result.get("subject_id"),
                subject_document_ids=result.get("subject_document_ids"),
                subject_kept_document_ids=result.get("subject_kept_document_ids"),
            )
        except HTTPException:
            raise
        except document_storage.DocumentStorageError as error:
            raise HTTPException(
                status_code=503,
                detail=f"The file could not be saved to durable storage, so it was not added. {error}",
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Could not upload and index the selected files. Please check that Ollama is running, "
                    "the embedding model is available, and the files contain extractable text. "
                    f"Original error: {error}"
                ),
            ) from error
        finally:
            if timer is not None:
                timer.emit()


def _upload_subject(owner_id: str, subject_id: Optional[str], new_subject_name: Optional[str]) -> Optional[dict]:
    """The validated subject choice of an upload, or None for no subject (Unassigned)."""
    subject_id = (subject_id or "").strip()
    new_subject_name = (new_subject_name or "").strip()
    if subject_id and new_subject_name:
        raise HTTPException(status_code=400, detail="Choose an existing subject or a new subject name, not both.")
    if subject_id:
        if not subject_store.subject_exists(owner_id, subject_id):
            raise HTTPException(status_code=404, detail="Subject not found.")
        return {"subject_id": subject_id}
    if new_subject_name:
        try:
            return {"new_name": subject_store.normalize_subject_name(new_subject_name)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    return None


@router.delete("/api/sources/{document_id}", response_model=DeleteSourceResponse)
def delete_source(document_id: str, current_user: dict = Depends(require_current_user)) -> DeleteSourceResponse:
    """
    Delete one indexed document from the system.

    This removes the owner's document from Chroma, the SQLite registry, and data/. It also
    deletes quiz data for that document so stale quiz questions/attempts cannot be reused.
    """
    try:
        with _INDEXING_LOCK:   # never while the same document is being (re)indexed
            result = delete_indexed_file(document_id, current_user["id"])
        delete_document_quiz_data(result["deleted"], current_user["id"])
        delete_document_summaries(current_user["id"], result["deleted"])
        delete_document_flashcards(current_user["id"], result["deleted"])
        study_planner_store.delete_document_plan_data(current_user["id"], result["deleted"])
        google_calendar_sync.after_planner_change(current_user["id"])   # its sessions' Google events go too
        remove_source_from_conversations(current_user["id"], result["deleted"])
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources(current_user["id"])]

        return DeleteSourceResponse(
            deleted=result["deleted"],
            deleted_chunks=result["deleted_chunks"],
            total_indexed_files=result["total_indexed_files"],
            sources=sources_result,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete the selected document. Original error: {error}",
        ) from error


@router.get("/api/documents", response_model=list[DocumentSummary])
def documents(current_user: dict = Depends(require_current_user)) -> list[DocumentSummary]:
    """
    Return indexed documents for the Practice quiz dropdown.

    This endpoint intentionally uses quiz_common.list_indexed_documents() so
    the quiz feature owns the shape it needs.
    """
    try:
        return [DocumentSummary(**document) for document in list_indexed_documents(current_user["id"])]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load indexed documents. Original error: {error}",
        ) from error
