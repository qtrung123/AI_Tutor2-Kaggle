"""Study Planner (Phase 1 / MVP) routes -- owner-scoped tasks, availability, and study blocks.

Planner v2 (plans/sessions) routes are separate; the availability routes here are shared with it.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import study_plan_api_service, study_planner_service, study_planner_store
from backend.api.deps import require_current_user
from backend.study_plan_api_service import PlanValidationError

router = APIRouter()


class StudyTaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    document_id: Optional[str] = None
    topic_id: Optional[str] = None
    deadline: str
    estimated_minutes: int = Field(gt=0, le=100_000)


class StudyTaskUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    document_id: Optional[str] = None
    topic_id: Optional[str] = None
    deadline: Optional[str] = None
    estimated_minutes: Optional[int] = Field(default=None, gt=0, le=100_000)
    remaining_minutes: Optional[int] = Field(default=None, ge=0, le=100_000)
    status: Optional[str] = None


class AvailabilityChangeRequest(BaseModel):
    start_at: str
    end_at: str
    date: Optional[str] = None
    is_recurring: bool = False
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    # The learner's UTC offset (and optional naive local "now"): when given, a dated slot that
    # starts in the learner's past is refused. The server never infers the learner's timezone.
    utc_offset_minutes: Optional[int] = None
    local_now: Optional[str] = None


class StudyBlockUpdateRequest(BaseModel):
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    status: Optional[str] = None
    actual_minutes: Optional[int] = None


class StudyBlockCompleteRequest(BaseModel):
    actual_minutes: Optional[int] = None


class StudyPlanGenerateRequest(BaseModel):
    # Naive local datetime (no timezone/UTC suffix) from the browser's own clock -- the planner
    # never assumes the server's timezone is the user's. Falls back to the server's local clock
    # only when the caller has none (e.g. a direct API call).
    local_now: Optional[str] = None


@router.post("/api/planner/tasks")
def planner_create_task(request: StudyTaskCreateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_planner_service.create_task(
            current_user["id"], request.title, request.deadline, request.estimated_minutes,
            document_id=request.document_id, topic_id=request.topic_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/planner/tasks")
def planner_list_tasks(current_user: dict = Depends(require_current_user)) -> list[dict]:
    return study_planner_store.list_tasks(current_user["id"])


@router.patch("/api/planner/tasks/{task_id}")
def planner_update_task(task_id: str, request: StudyTaskUpdateRequest,
                        current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_planner_store.update_task(current_user["id"], task_id, request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study task not found." else 400, detail=str(error)
        ) from error


@router.delete("/api/planner/tasks/{task_id}")
def planner_delete_task(task_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        study_planner_store.delete_task(current_user["id"], task_id)
        return {"deleted": task_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/planner/availability")
def planner_list_availability(current_user: dict = Depends(require_current_user)) -> list[dict]:
    return study_planner_store.list_availability(current_user["id"])


@router.post("/api/planner/availability")
def planner_add_availability(request: AvailabilityChangeRequest,
                             current_user: dict = Depends(require_current_user)) -> list[dict]:
    try:
        if request.utc_offset_minutes is not None:
            study_plan_api_service.check_availability_not_past(
                request.date, request.start_at, request.is_recurring, request.utc_offset_minutes, request.local_now)
        return study_planner_store.add_availability(
            current_user["id"], request.start_at, request.end_at, date=request.date,
            is_recurring=request.is_recurring, day_of_week=request.day_of_week,
        )
    except PlanValidationError as error:
        raise HTTPException(status_code=400, detail=error.payload) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/planner/availability/remove")
def planner_remove_availability(request: AvailabilityChangeRequest,
                                current_user: dict = Depends(require_current_user)) -> list[dict]:
    try:
        return study_planner_store.remove_availability(
            current_user["id"], request.start_at, request.end_at, date=request.date,
            is_recurring=request.is_recurring, day_of_week=request.day_of_week,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/api/planner/blocks")
def planner_list_blocks(task_id: Optional[str] = None,
                        current_user: dict = Depends(require_current_user)) -> list[dict]:
    return study_planner_store.list_blocks(current_user["id"], task_id=task_id)


@router.get("/api/planner/tasks/{task_id}/items")
def planner_list_plan_items(task_id: str, current_user: dict = Depends(require_current_user)) -> list[dict]:
    """Document-aware study plan items (Phase 2) for one task -- one per document topic, or an
    empty list for a task with no linked document (or none generated yet)."""
    return study_planner_store.list_plan_items(current_user["id"], task_id)


@router.patch("/api/planner/blocks/{block_id}")
def planner_update_block(block_id: str, request: StudyBlockUpdateRequest,
                         current_user: dict = Depends(require_current_user)) -> dict:
    try:
        result = None
        if request.start_at or request.end_at:
            result = study_planner_service.edit_block(
                current_user["id"], block_id, start_at=request.start_at, end_at=request.end_at,
            )
        if request.status:
            result = study_planner_store.update_block(current_user["id"], block_id, {"status": request.status})
        if request.actual_minutes is not None:
            result = study_planner_service.update_block_actual_minutes(
                current_user["id"], block_id, request.actual_minutes,
            )
        if result is None:
            raise ValueError("No block changes supplied.")
        return result
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study block not found." else 400, detail=str(error)
        ) from error


@router.delete("/api/planner/blocks/{block_id}")
def planner_delete_block(block_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        study_planner_store.delete_block(current_user["id"], block_id)
        return {"deleted": block_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/planner/tasks/{task_id}/generate")
def planner_generate_schedule(task_id: str, request: StudyPlanGenerateRequest,
                              current_user: dict = Depends(require_current_user)) -> dict:
    """Deterministically (no LLM) schedule this task's remaining minutes into suggested study
    blocks -- only inside the owner's selected availability, never after the deadline, never in
    the past, never overlapping an already-busy block. Returns a structured on_track/
    schedule_risk result."""
    try:
        now = datetime.fromisoformat(request.local_now) if request.local_now else None
        return study_planner_service.generate_schedule(current_user["id"], task_id, now=now)
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study task not found." else 400, detail=str(error)
        ) from error


@router.post("/api/planner/tasks/{task_id}/regenerate")
def planner_regenerate_schedule(task_id: str, request: StudyPlanGenerateRequest,
                                current_user: dict = Depends(require_current_user)) -> dict:
    """Orchestration-only re-run of the plan for a task that may already have blocks: removes
    stale suggested/unlocked blocks, keeps every completed/confirmed/locked block untouched, and
    reschedules with the unchanged scheduler over current availability."""
    try:
        now = datetime.fromisoformat(request.local_now) if request.local_now else None
        return study_planner_service.regenerate_schedule(current_user["id"], task_id, now=now)
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study task not found." else 400, detail=str(error)
        ) from error


@router.post("/api/planner/tasks/{task_id}/accept")
def planner_accept_plan(task_id: str, current_user: dict = Depends(require_current_user)) -> list[dict]:
    try:
        return study_planner_service.accept_plan(current_user["id"], task_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/planner/blocks/{block_id}/complete")
def planner_complete_block(block_id: str, request: StudyBlockCompleteRequest,
                           current_user: dict = Depends(require_current_user)) -> dict:
    """Mark a study block completed and roll its minutes into the linked topic's progress."""
    try:
        return study_planner_service.complete_block(
            current_user["id"], block_id, actual_minutes=request.actual_minutes,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study block not found." else 400, detail=str(error)
        ) from error


@router.post("/api/planner/blocks/{block_id}/skip")
def planner_skip_block(block_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_planner_service.skip_block(current_user["id"], block_id)
    except ValueError as error:
        raise HTTPException(
            status_code=404 if str(error) == "Study block not found." else 400, detail=str(error)
        ) from error


@router.get("/api/planner/topic-progress")
def planner_list_topic_progress(document_id: Optional[str] = None,
                                current_user: dict = Depends(require_current_user)) -> list[dict]:
    return study_planner_store.list_topic_progress(current_user["id"], document_id=document_id)


@router.get("/api/planner/tasks/{task_id}/progress")
def planner_get_task_progress(task_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_planner_service.get_task_progress(current_user["id"], task_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/api/planner/reset")
def planner_reset(current_user: dict = Depends(require_current_user)) -> dict:
    """Development/testing helper: wipes only the current user's Study Planner data (tasks,
    availability, blocks, plan items, topic progress). Never touches documents, quiz, flashcards,
    or chat history."""
    study_planner_store.reset_planner_data(current_user["id"])
    return {"reset": True}
