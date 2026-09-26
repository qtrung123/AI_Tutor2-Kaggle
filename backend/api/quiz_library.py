"""Quiz Library routes: per-document quiz statuses and permanent quiz deletion."""
from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import require_current_user
from backend.quiz_service import delete_quiz, list_quiz_statuses

router = APIRouter()


@router.get("/api/quizzes")
def quizzes(current_user: dict = Depends(require_current_user)) -> list[dict]:
    """
    Return quiz generation and completion status for all indexed documents.

    The Practice page uses this to decide whether the primary action should be
    Generate Quiz, Start Quiz, or Review Quiz.
    """
    try:
        return list_quiz_statuses(current_user["id"])
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load quiz statuses. Original error: {error}",
        ) from error


@router.delete("/api/quizzes/{quiz_id}")
def quiz_delete(quiz_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    """
    Permanently delete one quiz and its own attempts and answers.

    The source document and topic hierarchy are left untouched. Mastery for
    any topic this quiz's completed attempts fed is recomputed afterward.
    """
    try:
        return delete_quiz(quiz_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete quiz. Original error: {error}",
        ) from error
