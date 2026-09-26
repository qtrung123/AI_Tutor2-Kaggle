"""Flashcard routes: cached lookup/generation, explicit regeneration and learner card CRUD."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.deps import require_current_user
from backend.flashcard_service import FlashcardGenerationError, authoritative_card_fields, generate_flashcards
from backend.flashcard_store import add_flashcard, delete_flashcard, update_flashcard
from backend.model_registry import prepare_generation_model

router = APIRouter()


class FlashcardGenerateRequest(BaseModel):
    model_id: Optional[str] = None
    language: Optional[str] = None


class FlashcardCreateRequest(BaseModel):
    set_id: str
    topic_id: str
    subtopic_id: Optional[str] = None
    front: str = Field(min_length=1, max_length=1000)
    back: str = Field(min_length=1, max_length=4000)


class FlashcardUpdateRequest(BaseModel):
    front: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    back: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    is_favorite: Optional[bool] = None


@router.get("/api/flashcards/{document_id}")
def flashcards_detail(document_id: str, topic_ids: list[str] | None = Query(default=None),
                      model_id: Optional[str] = None, language: Optional[str] = None, cache_only: bool = False,
                      current_user: dict = Depends(require_current_user)) -> dict:
    """Reuse or generate grounded cards from existing owner-scoped indexed chunks.

    With cache_only=true nothing is generated: missing cards are reported as status "not_generated".
    """
    try:
        if cache_only:
            # A pure existence check never prepares/pulls anything.
            return generate_flashcards(current_user["id"], document_id, topic_ids=topic_ids, model_id=model_id,
                                       language=language, cache_only=True)
        prepare_generation_model(model_id)
        return generate_flashcards(current_user["id"], document_id, topic_ids=topic_ids, model_id=model_id, language=language)
    except FlashcardGenerationError as error:
        # Technical detail (model/runtime failure, e.g. an Ollama repeat-limit abort or malformed
        # JSON) stays server-side; the client only ever sees the safe, model-agnostic message.
        print(f"[flashcards] generation failed for document_id={document_id}: {error.technical_message}")
        raise HTTPException(status_code=502, detail=error.safe_message) from error
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Document not found." else 400, detail=str(error)) from error
    except Exception as error:
        print(f"[flashcards] unexpected failure for document_id={document_id}: {error}")
        raise HTTPException(status_code=500, detail=FlashcardGenerationError.SAFE_MESSAGE) from error


@router.post("/api/flashcards/{document_id}/regenerate")
def flashcards_regenerate(document_id: str, request: FlashcardGenerateRequest,
                          current_user: dict = Depends(require_current_user)) -> dict:
    """Explicitly generate and persist a fresh flashcard set (bypasses the cache lookup)."""
    try:
        prepare_generation_model(request.model_id)
        return generate_flashcards(current_user["id"], document_id, model_id=request.model_id,
                                   language=request.language, regenerate=True)
    except FlashcardGenerationError as error:
        print(f"[flashcards] regeneration failed for document_id={document_id}: {error.technical_message}")
        raise HTTPException(status_code=502, detail=error.safe_message) from error
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Document not found." else 400, detail=str(error)) from error
    except Exception as error:
        print(f"[flashcards] unexpected regeneration failure for document_id={document_id}: {error}")
        raise HTTPException(status_code=500, detail=FlashcardGenerationError.SAFE_MESSAGE) from error


@router.post("/api/flashcards/{document_id}/cards")
def flashcard_create(document_id: str, request: FlashcardCreateRequest,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        identity = authoritative_card_fields(current_user["id"], document_id, request.topic_id, request.subtopic_id)
        return add_flashcard(current_user["id"], document_id, request.set_id, {
            **identity, "front": request.front.strip(), "back": request.back.strip(), "source_chunk_ids": [],
        })
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.patch("/api/flashcards/{document_id}/cards/{flashcard_id}")
def flashcard_update(document_id: str, flashcard_id: str, request: FlashcardUpdateRequest,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return update_flashcard(current_user["id"], document_id, flashcard_id, request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Flashcard not found." else 400, detail=str(error)) from error


@router.delete("/api/flashcards/{document_id}/cards/{flashcard_id}")
def flashcard_delete(document_id: str, flashcard_id: str,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        delete_flashcard(current_user["id"], document_id, flashcard_id)
        return {"deleted": flashcard_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
