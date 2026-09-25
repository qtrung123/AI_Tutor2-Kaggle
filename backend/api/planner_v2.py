"""Study Planner v2 routes -- document-centric plans, plan materials, schedule preview/confirm,
study sessions, adaptive replanning and progress.

Availability is shared with the legacy planner (/api/planner/availability in
backend/api/planner_legacy.py); both go through the same study_planner_store/study_plan_api_service.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend import study_plan_api_service, study_planner_store
from backend.api.deps import require_current_user
from backend.study_plan_api_service import (PlanConflictError, PlanNotFoundError, PlanValidationError,
                                            SessionConflictError)

router = APIRouter()


# Study Planner v2 (document-centric). extra="forbid": unknown fields such as topic_id are rejected.
class StudyPlanV2CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class StudyPlanV2UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[str] = None


class PlanMaterialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1)
    deadline: Optional[str] = None      # ISO date; omitted/null = no deadline
    familiarity: Optional[str] = None   # new_to_me | somewhat_familiar | reviewing


class PlanMaterialUpdateRequest(BaseModel):
    """Only fields present in the body change; an explicit null clears deadline/familiarity."""
    model_config = ConfigDict(extra="forbid")
    deadline: Optional[str] = None
    familiarity: Optional[str] = None


class AdaptationTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    document_id: Optional[str] = None
    session_id: Optional[str] = None


class PlanAdaptationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: AdaptationTriggerRequest
    utc_offset_minutes: int
    local_now: Optional[str] = None


class PlanAdaptationApplyRequest(PlanAdaptationRequest):
    # Only the trigger/context and an explicit confirmation for large proposals: the server
    # recomputes the changes itself and never accepts client-supplied session edits.
    confirm: bool = False


class PlanPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The learner's offset from UTC in minutes (e.g. 420 for UTC+7, 330 for UTC+5:30) -- required:
    # the server never infers the learner's timezone. Must be a real civil offset (validated).
    utc_offset_minutes: int
    # Optional naive local datetime override; defaults to the current UTC instant + utc_offset_minutes.
    local_now: Optional[str] = None


class PlacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A candidate the server itself proposed (its candidate_key) and the learner's chosen start.
    candidate_key: str = Field(min_length=1, max_length=300)
    scheduled_start: str = Field(min_length=1, max_length=40)


class PlanScheduleRequest(PlanPreviewRequest):
    # Learner placements (calendar drags); the server validates each against its own recomputation.
    placements: list[PlacementRequest] = Field(default_factory=list, max_length=100)


class SessionRescheduleRequest(PlanPreviewRequest):
    # A calendar drag: move to exactly this naive-local start (validated server-side). Omitted = first free slot.
    target_start: Optional[str] = Field(default=None, max_length=40)


class CandidatePlaceRequest(PlanPreviewRequest):
    candidate_key: str = Field(min_length=1, max_length=300)
    scheduled_start: str = Field(min_length=1, max_length=40)


def _plan_v2_error(error: Exception) -> HTTPException:
    if isinstance(error, PlanNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, PlanConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, PlanValidationError):
        return HTTPException(status_code=400, detail=error.payload)
    return HTTPException(status_code=400, detail=str(error))


_PLAN_V2_ERRORS = (PlanNotFoundError, PlanConflictError, ValueError)


@router.get("/api/planner/plans")
def planner_v2_list_plans(current_user: dict = Depends(require_current_user)) -> list[dict]:
    return study_planner_store.list_plans(current_user["id"])


@router.post("/api/planner/plans", status_code=201)
def planner_v2_create_plan(request: StudyPlanV2CreateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_planner_store.create_plan(current_user["id"], request.title)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.get("/api/planner/plans/{plan_id}")
def planner_v2_get_plan(plan_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_plan_api_service.get_plan_detail(current_user["id"], plan_id)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.patch("/api/planner/plans/{plan_id}")
def planner_v2_update_plan(plan_id: str, request: StudyPlanV2UpdateRequest,
                           current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_plan_api_service.update_plan(current_user["id"], plan_id, request.model_dump(exclude_none=True))
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.delete("/api/planner/plans/{plan_id}")
def planner_v2_delete_plan(plan_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        study_plan_api_service.delete_plan(current_user["id"], plan_id)
        return {"deleted": plan_id}
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.get("/api/planner/plans/{plan_id}/materials")
def planner_v2_list_materials(plan_id: str, current_user: dict = Depends(require_current_user)) -> list[dict]:
    try:
        return study_plan_api_service.list_plan_materials(current_user["id"], plan_id)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.post("/api/planner/plans/{plan_id}/materials", status_code=201)
def planner_v2_add_material(plan_id: str, request: PlanMaterialCreateRequest,
                            current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_plan_api_service.add_plan_material(
            current_user["id"], plan_id, request.document_id, deadline=request.deadline,
            familiarity=request.familiarity,
        )
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.patch("/api/planner/plans/{plan_id}/materials/{material_id}")
def planner_v2_update_material(plan_id: str, material_id: str, request: PlanMaterialUpdateRequest,
                               current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return study_plan_api_service.update_plan_material(
            current_user["id"], plan_id, material_id, request.model_dump(exclude_unset=True),
        )
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.delete("/api/planner/plans/{plan_id}/materials/{material_id}")
def planner_v2_remove_material(plan_id: str, material_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        study_plan_api_service.remove_plan_material(current_user["id"], plan_id, material_id)
        return {"deleted": material_id}
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.post("/api/planner/plans/{plan_id}/adaptation/preview")
def planner_v2_adaptation_preview(plan_id: str, request: PlanAdaptationRequest,
                                  current_user: dict = Depends(require_current_user)) -> dict:
    """Adaptive replanning proposal. Read-only: never changes study sessions."""
    try:
        return study_plan_api_service.propose_adaptation(
            current_user["id"], plan_id, request.trigger.model_dump(), request.utc_offset_minutes,
            local_now=request.local_now,
        )
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.post("/api/planner/plans/{plan_id}/adaptation/apply")
def planner_v2_adaptation_apply(plan_id: str, request: PlanAdaptationApplyRequest,
                                current_user: dict = Depends(require_current_user)) -> dict:
    """Recompute the adaptation proposal and apply it atomically (large ones need confirm=true)."""
    return _session_action(study_plan_api_service.apply_adaptation, current_user["id"], plan_id,
                           request.trigger.model_dump(), request.utc_offset_minutes, request.local_now,
                           request.confirm)


@router.post("/api/planner/plans/{plan_id}/preview")
def planner_v2_preview(plan_id: str, request: PlanScheduleRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Deterministic schedule preview (plus validated learner placements). Read-only: never saves study sessions."""
    try:
        return study_plan_api_service.preview_plan(
            current_user["id"], plan_id, request.utc_offset_minutes, local_now=request.local_now,
            placements=[p.model_dump() for p in request.placements],
        )
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.post("/api/planner/plans/{plan_id}/confirm", status_code=201)
def planner_v2_confirm(plan_id: str, request: PlanScheduleRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Recompute the schedule server-side and save it (sessions + schedule run, atomically). Same body
    as preview -- client-sent sessions are never accepted (extra fields are rejected); placements
    only move the server's own candidates and a stale/invalid one refuses the whole confirm (409)."""
    return _session_action(study_plan_api_service.confirm_plan, current_user["id"], plan_id,
                           request.utc_offset_minutes, request.local_now, [p.model_dump() for p in request.placements])


def _session_action(action, *args) -> dict:
    try:
        return action(*args)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=error.payload) from error
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.post("/api/planner/sessions/{session_id}/start")
def planner_v2_start_session(session_id: str, request: PlanPreviewRequest,
                             current_user: dict = Depends(require_current_user)) -> dict:
    # The learner's offset (as for reschedule/preview): a session whose time has passed is missed, not startable.
    return _session_action(study_plan_api_service.start_session, current_user["id"], session_id,
                           request.utc_offset_minutes, request.local_now)


@router.post("/api/planner/sessions/{session_id}/complete")
def planner_v2_complete_session(session_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    return _session_action(study_plan_api_service.complete_session, current_user["id"], session_id)


@router.post("/api/planner/sessions/{session_id}/skip")
def planner_v2_skip_session(session_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    return _session_action(study_plan_api_service.skip_session, current_user["id"], session_id)


@router.post("/api/planner/sessions/{session_id}/reschedule")
def planner_v2_reschedule_session(session_id: str, request: SessionRescheduleRequest,
                                  current_user: dict = Depends(require_current_user)) -> dict:
    return _session_action(study_plan_api_service.reschedule_session, current_user["id"], session_id,
                           request.utc_offset_minutes, request.local_now, request.target_start)


@router.get("/api/planner/plans/{plan_id}/candidates")
def planner_v2_live_candidates(plan_id: str, utc_offset_minutes: int, local_now: Optional[str] = None,
                               current_user: dict = Depends(require_current_user)) -> list[dict]:
    """What a confirmed plan still wants scheduled (read-only): the only activities a learner may place."""
    return _session_action(study_plan_api_service.list_live_candidates, current_user["id"], plan_id,
                           utc_offset_minutes, local_now)


@router.post("/api/planner/plans/{plan_id}/candidates/place", status_code=201)
def planner_v2_place_candidate(plan_id: str, request: CandidatePlaceRequest,
                               current_user: dict = Depends(require_current_user)) -> dict:
    """Schedule one server-recomputed candidate at the learner's chosen start (validated, atomic)."""
    return _session_action(study_plan_api_service.place_live_candidate, current_user["id"], plan_id,
                           request.candidate_key, request.scheduled_start, request.utc_offset_minutes,
                           request.local_now)


@router.get("/api/progress/documents/{document_id}")
def progress_document(document_id: str, utc_offset_minutes: int, local_now: Optional[str] = None,
                      current_user: dict = Depends(require_current_user)) -> dict:
    """Learner progress for one document: learning state, quiz results, study-plan figures."""
    try:
        return study_plan_api_service.document_progress(current_user["id"], document_id, utc_offset_minutes, local_now)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.get("/api/planner/plans/{plan_id}/progress")
def planner_v2_plan_progress(plan_id: str, utc_offset_minutes: int, local_now: Optional[str] = None,
                             current_user: dict = Depends(require_current_user)) -> dict:
    """Aggregate plan progress: sessions and minutes done vs planned, current quiz performance."""
    try:
        return study_plan_api_service.plan_progress(current_user["id"], plan_id, utc_offset_minutes, local_now)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error


@router.get("/api/planner/plans/{plan_id}/sessions")
def planner_v2_list_sessions(plan_id: str, current_user: dict = Depends(require_current_user)) -> list[dict]:
    try:
        return study_plan_api_service.list_plan_sessions(current_user["id"], plan_id)
    except _PLAN_V2_ERRORS as error:
        raise _plan_v2_error(error) from error
