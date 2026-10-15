"""Study Session conversation routes: create, list, read, rename, delete, set sources and chat."""
import json
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
from backend.rag_service import answer_conversation_message, list_uploaded_sources, stream_conversation_message

router = APIRouter()


class ConversationCreateRequest(BaseModel):
    title: str = "New conversation"
    document_ids: list[str] = Field(default_factory=list)
    # False reuses the document's existing conversation; True starts another one ("New chat").
    new_conversation: bool = False


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
    return create_conversation(current_user["id"], request.title, request.document_ids, request.new_conversation)


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


STREAM_MEDIA_TYPE = "text/event-stream"
_END = object()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _event_stream(events, cancelled: threading.Event):
    """Server-Sent Events from the (blocking) answer generator, one event per `data:` frame.

    The generator runs in the threadpool one step at a time. When the client goes away -- this task
    is cancelled, or the next send fails and the stream is closed -- `finally` flags the generator
    and closes it, which closes the model request and skips persistence (see
    stream_conversation_message). A model error becomes one final {"type": "error"} event."""
    try:
        while True:
            event = await run_in_threadpool(next, events, _END)
            if event is _END:
                return
            yield _sse(event)
    except Exception as error:   # model/backend failure after the response started: report, save nothing
        yield _sse({"type": "error", "message": f"Could not answer this conversation. Original error: {error}"})
    finally:
        cancelled.set()
        try:
            events.close()
        except ValueError:
            pass   # still running in a worker thread: it sees `cancelled` at its next chunk


def _wants_stream(http_request: Request) -> bool:
    return STREAM_MEDIA_TYPE in http_request.headers.get("accept", "")


@router.post("/api/conversations/{conversation_id}/messages")
def conversation_message(conversation_id: str, request: ConversationMessageRequest, http_request: Request,
                         current_user: dict = Depends(require_current_user)):
    """One chat answer. `Accept: text/event-stream` streams it (see _event_stream); any other
    request gets the complete JSON answer, exactly as before."""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message is required.")
    try:
        prepare_generation_model(request.model_id)
        if _wants_stream(http_request):
            cancelled = threading.Event()
            events = stream_conversation_message(current_user["id"], conversation_id, request.message,
                                                 request.model_id, cancelled)
            return StreamingResponse(_event_stream(events, cancelled), media_type=STREAM_MEDIA_TYPE, headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",   # nginx: pass frames through unbuffered
            })
        return answer_conversation_message(current_user["id"], conversation_id, request.message, request.model_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer this conversation. Original error: {error}",
        ) from error
