"""Quiz attempt routes: completed-attempt history/results/retake and the Quiz Player lifecycle
(progress autosave, submit, progress reset and answer explanations).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import require_current_user
from backend.quiz_attempt_service import (
    clear_quiz_progress,
    explain_quiz_question,
    list_completed_quiz_attempts,
    load_completed_quiz_attempt,
    load_quiz_for_retake,
    submit_quiz_attempt,
    update_quiz_progress,
)

# Two routers only so main.py can include each group at its original position (history before
# the quiz generation routes, the Quiz Player routes after them), keeping route order unchanged.
history_router = APIRouter()
router = APIRouter()


class QuizProgressRequest(BaseModel):
    """Autosave payload for the Quiz Player: an answer, a position, or both.

    `quiz_id` is optional in the schema only for backward compatibility with any non-live caller --
    the live Quiz Player always sends it (see backend/quiz_attempt_service.update_quiz_progress, which never
    falls back to "the newest quiz in this slot" once quiz_id is given).
    """
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str
    quiz_id: Optional[str] = None
    question_id: Optional[int] = Field(default=None, ge=1)
    selected_answer: Optional[str | list[str]] = None
    current_question_index: Optional[int] = Field(default=None, ge=0)
    # The Quiz Player's full local answer snapshot; replaces the saved answers when given.
    answers: Optional[dict[str, str | list[str]]] = None


class QuizExplainRequest(BaseModel):
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str


class QuizSubmitRequest(BaseModel):
    quiz_id: Optional[str] = None
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str
    answers: dict[str, str | list[str]]
    # The Quiz Player's "Submit Anyway": unanswered questions are graded as incorrect/unanswered
    # instead of rejecting the submission. Other callers keep the complete-answer-set rule.
    allow_unanswered: bool = False


@history_router.get("/api/quiz-history")
def quiz_history(document_id: Optional[str] = None, difficulty: Optional[str] = None, current_user: dict = Depends(require_current_user)) -> list[dict]:
    """List completed quiz attempts for the history UI."""
    try:
        return list_completed_quiz_attempts(document_id, difficulty, current_user["id"])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load quiz history: {error}") from error


@history_router.get("/api/quiz-history/{attempt_id}")
def quiz_history_detail(attempt_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    """Load one completed attempt with its question snapshots."""
    try:
        return load_completed_quiz_attempt(attempt_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@history_router.get("/api/quiz-history/{attempt_id}/retake")
def quiz_history_retake(attempt_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    """Load the exact persisted quiz, including inactive and migrated legacy quizzes."""
    try:
        return load_quiz_for_retake(attempt_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.patch("/api/quiz/{document_id}/progress")
def quiz_progress(document_id: str, request: QuizProgressRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Autosave the Quiz Player's current answers and/or position for one exact quiz_id."""
    try:
        return update_quiz_progress(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            quiz_id=request.quiz_id,
            question_id=request.question_id,
            selected_answer=request.selected_answer,
            current_question_index=request.current_question_index,
            answers=request.answers,
            student_id=current_user["id"],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not save quiz progress: {error}") from error


@router.post("/api/quiz/{document_id}/submit")
def quiz_submit(document_id: str, request: QuizSubmitRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Grade one complete set of answers and persist a new immutable attempt."""
    try:
        return submit_quiz_attempt(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            answers=request.answers,
            student_id=current_user["id"],
            quiz_id=request.quiz_id,
            allow_unanswered=request.allow_unanswered,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not submit quiz: {error}") from error


@router.delete("/api/quiz/{document_id}/progress")
def quiz_progress_reset(
    document_id: str, topic_id: str, difficulty: str, quiz_id: Optional[str] = None,
    current_user: dict = Depends(require_current_user),
) -> dict:
    """Clear current quiz progress while preserving completed history."""
    try:
        return clear_quiz_progress(document_id, difficulty, topic_id, current_user["id"], quiz_id=quiz_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/quiz/{document_id}/questions/{question_id}/explain")
def quiz_explain(document_id: str, question_id: int, request: QuizExplainRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Generate a short, document-grounded explanation on demand."""
    try:
        return explain_quiz_question(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            question_id=question_id,
            owner_id=current_user["id"],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not explain quiz answer: {error}") from error
