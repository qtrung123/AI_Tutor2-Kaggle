"""Learning routes: Overview dashboard, topic mastery, knowledge gaps and recommendations."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import study_progress
from backend.api.deps import require_current_user
from backend.knowledge_gap_service import detect_knowledge_gaps
from backend.mastery_service import recompute_all_mastery, recompute_topic_mastery
from backend.quiz_service import build_learning_dashboard
from backend.recommendation_service import generate_recommendations
from backend.subject_grouping import group_documents_into_subjects

router = APIRouter()


class MasteryRecomputeRequest(BaseModel):
    document_id: Optional[str] = None
    topic_id: Optional[str] = None


@router.get("/api/mastery/{document_id}/{topic_id}")
def topic_mastery(
    document_id: str, topic_id: str, current_user: dict = Depends(require_current_user)
) -> dict:
    """Return mastery rebuilt from completed attempt-answer snapshots."""
    try:
        return recompute_topic_mastery(current_user["id"], document_id, topic_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not compute mastery: {error}") from error


@router.get("/api/dashboard")
def learning_dashboard(current_user: dict = Depends(require_current_user)) -> dict:
    """Return the real project state used by the Overview and mastery UI. Adds a subject-grouped
    view of the existing "materials" rows for the Overview's subject cards -- a pure, read-only
    aggregation; build_learning_dashboard's own quiz/mastery computation is untouched."""
    try:
        dashboard = build_learning_dashboard(current_user["id"])
        dashboard["subjects"] = group_documents_into_subjects(dashboard.get("materials") or [])
        # Current quiz performance: latest completed quiz per assessed document (unassessed ones left out).
        dashboard["metrics"]["current_quiz_performance"] = study_progress.current_quiz_performance({
            row["document_id"]: study_progress.latest_quiz_percentage(current_user["id"], row["document_id"])
            for row in dashboard.get("materials") or []
        })
        return dashboard
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load dashboard: {error}") from error


@router.get("/api/knowledge-gaps")
def knowledge_gaps(current_user: dict = Depends(require_current_user)) -> list[dict]:
    """Return reliable mastery-derived gaps for the authenticated user."""
    return detect_knowledge_gaps(current_user["id"])


@router.get("/api/knowledge-gaps/{document_id}")
def document_knowledge_gaps(
    document_id: str, current_user: dict = Depends(require_current_user)
) -> list[dict]:
    """Return reliable mastery-derived gaps for one owned assessment history."""
    return detect_knowledge_gaps(current_user["id"], document_id)


@router.get("/api/recommendations")
def recommendations(current_user: dict = Depends(require_current_user)) -> list[dict]:
    """Return ranked next actions derived from the authenticated user's current state."""
    return generate_recommendations(current_user["id"])


@router.get("/api/recommendations/{document_id}")
def document_recommendations(
    document_id: str, current_user: dict = Depends(require_current_user)
) -> list[dict]:
    return generate_recommendations(current_user["id"], document_id)


@router.post("/api/mastery/recompute")
def mastery_recompute(request: MasteryRecomputeRequest, current_user: dict = Depends(require_current_user)) -> list[dict] | dict:
    """Rebuild one topic or every historical mastery cache entry."""
    try:
        if request.document_id or request.topic_id:
            if not request.document_id or not request.topic_id:
                raise ValueError("document_id and topic_id are both required for one-topic recomputation.")
            return recompute_topic_mastery(
                current_user["id"], request.document_id, request.topic_id
            )
        return recompute_all_mastery(current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not rebuild mastery: {error}") from error
