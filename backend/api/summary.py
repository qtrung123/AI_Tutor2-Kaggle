"""Document Summary routes: cached lookup, background generation jobs (start + poll), legacy sync routes."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.api.deps import require_current_user
from backend.model_registry import prepare_generation_model
from backend.summary_jobs import find_active_summary_job, get_summary_job, start_summary_job
from backend.summary_service import generate_document_summary
from backend.summary_store import get_summary_by_id

router = APIRouter()


class SummaryGenerateRequest(BaseModel):
    model_id: Optional[str] = None


class SummaryJobStartRequest(BaseModel):
    document_id: str
    model_id: Optional[str] = None
    regenerate: bool = False


def _document_error(error: ValueError) -> HTTPException:
    message = str(error)
    return HTTPException(status_code=404 if message == "Document not found." else 400, detail=message)


def _job_response(owner_id: str, job: dict) -> dict:
    if job["status"] == "complete":
        summary = get_summary_by_id(owner_id, job["summary_id"]) if job.get("summary_id") else None
        if summary is None:
            return {"status": "failed", "job_id": job["job_id"],
                    "error": {"code": "summary_missing", "message": "The generated summary is no longer available. Please try again."}}
        summary["cache_hit"] = False
        return {"status": "complete", "job_id": job["job_id"], "summary": summary}
    if job["status"] == "failed":
        return {"status": "failed", "job_id": job["job_id"],
                "error": {"code": job.get("error_code") or "generation_failed",
                          "message": job.get("error_message") or "Could not generate document summary."}}
    return {"status": "processing", "job_id": job["job_id"]}


@router.post("/api/summary/generate")
def summary_generate_start(request: SummaryJobStartRequest, current_user: dict = Depends(require_current_user)):
    """Start (or join) a background Summary generation; never holds the request open for the LLM.

    A compatible saved summary (regenerate=false) is returned immediately as before; otherwise the
    response is 202 {"status": "processing", "job_id", "document_id"} and the client polls the job.
    """
    owner_id = current_user["id"]
    try:
        if not request.regenerate:
            cached = generate_document_summary(owner_id, request.document_id, model_id=request.model_id, cache_only=True)
            if cached.get("status") != "not_generated" and cached.get("final_summary"):
                return cached
        job, _created = start_summary_job(owner_id, request.document_id, request.model_id, regenerate=request.regenerate)
    except ValueError as error:
        raise _document_error(error) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not start document summary generation: {error}") from error
    return JSONResponse(status_code=202, content={"status": "processing", "job_id": job["job_id"],
                                                  "document_id": request.document_id})


@router.get("/api/summary/jobs")
def summary_active_job(document_id: str, model_id: Optional[str] = None,
                       current_user: dict = Depends(require_current_user)) -> dict:
    """The still-running job for this document/model (refresh/reopen resumes polling), or status "none"."""
    try:
        job = find_active_summary_job(current_user["id"], document_id, model_id)
    except ValueError as error:
        raise _document_error(error) from error
    return {"status": "processing", "job_id": job["job_id"], "document_id": document_id} if job else {"status": "none"}


@router.get("/api/summary/jobs/{job_id}")
def summary_job_status(job_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    job = get_summary_job(current_user["id"], job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Summary job not found.")
    return _job_response(current_user["id"], job)


@router.get("/api/summary/{document_id}")
def summary_detail(document_id: str, model_id: Optional[str] = None, cache_only: bool = False,
                   current_user: dict = Depends(require_current_user)) -> dict:
    """Return a compatible persisted summary or generate it from existing indexed chunks.

    With cache_only=true nothing is generated: a missing summary is reported as status "not_generated".
    """
    try:
        if cache_only:
            # A pure existence check never prepares/pulls anything.
            return generate_document_summary(current_user["id"], document_id, model_id=model_id, cache_only=True)
        prepare_generation_model(model_id)
        return generate_document_summary(current_user["id"], document_id, model_id=model_id)
    except ValueError as error:
        message = str(error)
        raise HTTPException(status_code=404 if message == "Document not found." else 400, detail=message) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not generate document summary: {error}") from error


@router.post("/api/summary/{document_id}/regenerate")
def summary_regenerate(document_id: str, request: SummaryGenerateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Explicitly generate and persist a fresh summary version."""
    try:
        prepare_generation_model(request.model_id)
        return generate_document_summary(current_user["id"], document_id, model_id=request.model_id, regenerate=True)
    except ValueError as error:
        message = str(error)
        raise HTTPException(status_code=404 if message == "Document not found." else 400, detail=message) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not regenerate document summary: {error}") from error
