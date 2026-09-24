"""Learner progress read model (Phase 6A): persisted data only, no writes, no invented metrics.

- Learning state comes from DocumentStudyState (same rules the planner uses).
- Quiz results are the document's completed attempts (quiz_store); the "latest" one is the same
  attempt DocumentStudyState reads, so a retake always replaces the previous score.
- Current quiz performance = the average of each ASSESSED document's latest completed quiz
  percentage. Documents without a completed quiz are left out -- never counted as 0.
- Study-plan figures come from the plan's persisted sessions. Superseded ('rescheduled') and
  withdrawn ('cancelled') rows are history, not planned work, so they are not counted.
- Flashcards: only the persisted card count. There is no review data, so no flashcard progress.
"""

from datetime import datetime

from backend import quiz_store, study_planner_store
from backend.document_study_state import get_document_study_state

LEARNING_LABELS = {
    "new": "Not started", "learning": "Learning", "needs_review": "Needs review",
    "on_track": "On track", "completed": "Completed",
}
# Statuses that count as planned work (what the plan asked of the learner).
PLANNED_STATUSES = ("scheduled", "in_progress", "completed", "skipped", "missed")
REMAINING_STATUSES = ("scheduled", "in_progress")
# A score change smaller than this many points between the last two attempts reads as "steady".
TREND_THRESHOLD_POINTS = 5.0


def _attempts(owner_id: str, document_id: str) -> list[dict]:
    """Completed, gradable attempts, oldest first (for a trend)."""
    rows = quiz_store.get_document_quiz_activity(document_id, owner_id)["completed_attempts"]
    attempts = [{"attempt_id": row["attempt_id"], "quiz_id": row["quiz_id"], "score": int(row["score"] or 0),
                 "total": int(row["total"] or 0), "percentage": round(float(row["percentage"] or 0), 2),
                 "completed_at": row["completed_at"]}
                for row in rows if int(row["total"] or 0) > 0]
    return list(reversed(attempts))


def _trend(attempts: list[dict]) -> str | None:
    """Direction of the latest attempt against the one before; None with fewer than two."""
    if len(attempts) < 2:
        return None
    change = attempts[-1]["percentage"] - attempts[-2]["percentage"]
    if change >= TREND_THRESHOLD_POINTS:
        return "improving"
    if change <= -TREND_THRESHOLD_POINTS:
        return "declining"
    return "steady"


def current_quiz_performance(latest_percentages: dict) -> dict:
    """{average_percentage, assessed_documents}; average is None when nothing is assessed yet.
    `latest_percentages` maps document_id -> latest completed percentage, or None if unassessed."""
    assessed = [value for value in latest_percentages.values() if value is not None]
    return {"average_percentage": round(sum(assessed) / len(assessed), 1) if assessed else None,
            "assessed_documents": len(assessed), "documents": len(latest_percentages)}


def latest_quiz_percentage(owner_id: str, document_id: str) -> float | None:
    state = get_document_study_state(owner_id, document_id)
    result = state.quiz.latest_completed if state else None
    return round(result.percentage, 2) if result else None


def _session_row(session: dict) -> dict:
    return {key: session[key] for key in ("session_id", "document_id", "activity_type", "scheduled_start",
                                          "scheduled_end", "duration_minutes", "status")}


def _session_totals(sessions: list[dict], now_local: str) -> dict:
    planned = [s for s in sessions if s["status"] in PLANNED_STATUSES]
    completed = [s for s in planned if s["status"] == "completed"]
    remaining = [s for s in planned if s["status"] in REMAINING_STATUSES]
    running = [s for s in remaining if s["status"] == "in_progress"]
    ahead = sorted((s for s in remaining if s["status"] == "scheduled" and s["scheduled_end"] > now_local),
                   key=lambda s: (s["scheduled_start"], s["session_id"]))
    upcoming = (running + ahead)[:1]
    return {
        "completed_sessions": len(completed), "planned_sessions": len(planned),
        "completed_minutes": sum(s["duration_minutes"] for s in completed),
        "remaining_minutes": sum(s["duration_minutes"] for s in remaining),
        "next_session": _session_row(upcoming[0]) if upcoming else None,
    }


def _active_plan_for(owner_id: str, document_id: str) -> tuple[dict, dict] | tuple[None, None]:
    """The most recently created active plan holding the document, with its material row."""
    for plan in reversed(study_planner_store.list_plans(owner_id)):
        if plan["status"] != "active":
            continue
        material = study_planner_store.get_plan_material(owner_id, plan["plan_id"], document_id)
        if material:
            return plan, material
    return None, None


def document_progress(owner_id: str, document_id: str, now_local: datetime) -> dict | None:
    """Progress for one of the owner's documents, or None when it is not theirs."""
    state = get_document_study_state(owner_id, document_id)
    if state is None:
        return None
    attempts = _attempts(owner_id, document_id)
    latest = state.quiz.latest_completed
    plan, material = _active_plan_for(owner_id, document_id)
    plan_block = None
    if plan:
        sessions = study_planner_store.list_sessions(owner_id, plan_id=plan["plan_id"], document_id=document_id)
        plan_block = {"plan_id": plan["plan_id"], "plan_title": plan["title"], "deadline": material["deadline"],
                      **_session_totals(sessions, now_local.isoformat())}
    return {
        "document_id": document_id, "title": state.title,
        "learning": {"state": state.learning_state, "label": LEARNING_LABELS[state.learning_state],
                     "explanation": state.state_reason.message},
        "quiz": {
            "latest": ({"score": latest.score, "total": latest.total, "percentage": round(latest.percentage, 2),
                        "completed_at": latest.completed_at} if latest else None),
            "attempts": attempts, "attempt_count": len(attempts), "trend": _trend(attempts),
        },
        "flashcards": {"card_count": state.flashcards.card_count},
        # What the document's Study Pack actually holds right now (nothing inferred).
        "study_pack": {"summary_ready": state.summary.available,
                       "flashcard_count": state.flashcards.card_count if state.flashcards.available else 0,
                       "quiz_count": state.quiz.quiz_count},
        "plan": plan_block,
    }


def plan_progress(owner_id: str, plan_id: str, now_local: datetime) -> dict | None:
    """Aggregate progress for one of the owner's plans, or None when it is not theirs."""
    plan = study_planner_store.get_plan(owner_id, plan_id)
    if not plan:
        return None
    sessions = study_planner_store.list_sessions(owner_id, plan_id=plan_id)
    now_iso = now_local.isoformat()
    documents, latest = [], {}
    for material in study_planner_store.list_materials(owner_id, plan_id):
        state = get_document_study_state(owner_id, material["document_id"], plan_id=plan_id)
        if state is None:
            continue   # the document was deleted; nothing to report
        result = state.quiz.latest_completed
        latest[material["document_id"]] = round(result.percentage, 2) if result else None
        mine = [s for s in sessions if s["document_id"] == material["document_id"]]
        documents.append({
            "document_id": material["document_id"], "title": state.title, "deadline": material["deadline"],
            "learning": {"state": state.learning_state, "label": LEARNING_LABELS[state.learning_state]},
            "latest_quiz_percentage": latest[material["document_id"]],
            **_session_totals(mine, now_iso),
        })
    totals = _session_totals(sessions, now_iso)
    return {"plan_id": plan_id, "title": plan["title"], "status": plan["status"], **totals,
            "current_quiz_performance": current_quiz_performance(latest), "documents": documents}
