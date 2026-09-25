"""Study Session conversation routes: create, list, read, rename, delete, set sources and chat."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import require_current_user
from backend.conversation_store import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    set_conversation_sources,
    update_conversation_title,
)
from backend.model_registry import prepare_generation_model
from backend.rag_service import answer_conversation_message, list_uploaded_sources

router = APIRouter()


class ConversationCreateRequest(BaseModel):
    title: str = "New conversation"
    document_ids: list[str] = Field(default_factory=list)


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationSourcesRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list)


class ConversationMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    model_id: Optional[str] = None


def _validate_conversation_document_ids(owner_id: str, document_ids: list[str]) -> None:
    if len(set(document_ids)) != 1:
        raise HTTPException(status_code=400, detail="A Study Session conversation requires exactly one document.")
    available = {source["title"] for source in list_uploaded_sources(owner_id)}
    missing = sorted(set(document_ids) - available)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"These documents are not indexed: {', '.join(missing)}",
        )


@router.post("/api/conversations")
def conversation_create(request: ConversationCreateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    _validate_conversation_document_ids(current_user["id"], request.document_ids)
    return create_conversation(current_user["id"], request.title, request.document_ids)


@router.get("/api/conversations")
def conversation_list(current_user: dict = Depends(require_current_user)) -> list[dict]:
    return list_conversations(current_user["id"])


@router.get("/api/conversations/{conversation_id}")
def conversation_get(conversation_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return get_conversation(current_user["id"], conversation_id, include_messages=True)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.patch("/api/conversations/{conversation_id}")
def conversation_update(conversation_id: str, request: ConversationUpdateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return update_conversation_title(current_user["id"], conversation_id, request.title)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/api/conversations/{conversation_id}")
def conversation_delete(conversation_id: str, current_user: dict = Depends(require_current_user)) -> dict[str, str]:
    try:
        delete_conversation(current_user["id"], conversation_id)
        return {"deleted": conversation_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.put("/api/conversations/{conversation_id}/sources")
def conversation_sources_update(conversation_id: str, request: ConversationSourcesRequest, current_user: dict = Depends(require_current_user)) -> dict:
    _validate_conversation_document_ids(current_user["id"], request.document_ids)
    try:
        return set_conversation_sources(current_user["id"], conversation_id, request.document_ids)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/conversations/{conversation_id}/messages")
def conversation_message(conversation_id: str, request: ConversationMessageRequest, current_user: dict = Depends(require_current_user)) -> dict:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message is required.")
    try:
        prepare_generation_model(request.model_id)
        return answer_conversation_message(current_user["id"], conversation_id, request.message, request.model_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer this conversation. Original error: {error}",
        ) from error
