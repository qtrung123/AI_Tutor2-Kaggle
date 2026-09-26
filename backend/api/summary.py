"""Document Summary routes: cached lookup/generation and explicit regeneration."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.deps import require_current_user
from backend.model_registry import prepare_generation_model
from backend.summary_service import generate_document_summary

router = APIRouter()


class SummaryGenerateRequest(BaseModel):
    model_id: Optional[str] = None


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
