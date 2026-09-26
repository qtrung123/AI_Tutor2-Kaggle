"""Quiz generation routes: load a saved quiz, generate a new one, and regenerate an existing one.

Quiz attempt routes (progress, submit, explain, history) and the Quiz Library stay separate.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import require_current_user
from backend.model_registry import prepare_generation_model, resolve_generation_model
from backend.quiz_attempt_service import load_quiz_with_attempt
from backend.quiz_service import QuizGenerationError, generate_quiz
from config import QUIZ_DEFAULT_GENERATION_MODEL

router = APIRouter()
# Regenerate is a separate router only so main.py can include it at its original position (after the
# quiz attempt routes), keeping the app's route registration order unchanged.
regenerate_router = APIRouter()


class QuizGenerateRequest(BaseModel):
    """Request body for POST /api/quiz/generate."""
    document_id: str
    assessment_scope: str = Field(pattern="^(topic|document)$")
    topic_id: Optional[str] = None
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    question_count: Literal[12, 15, 18, 20] = 12
    model_id: Optional[str] = None
    quiz_name: str = Field(min_length=1, max_length=200)
    # The live frontend always sends true: an explicit Generate/Create Quiz click must always
    # create a new quiz artifact, never silently reuse an older compatible one (see
    # backend/quiz_service._generate_quiz's cache-hit branch). Defaults False only so any other,
    # non-generation caller of this same route keeps the old reuse-if-compatible behavior.
    regenerate: bool = False


class QuizRegenerateRequest(BaseModel):
    """Request body for POST /api/quiz/{document_id}/regenerate."""
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    model_id: Optional[str] = None
    assessment_scope: str = Field(pattern="^(topic|document)$")
    topic_id: Optional[str] = None
    question_count: Literal[12, 15, 18, 20] = 12
    # Omitted or blank keeps the quiz's existing name; regeneration must never
    # silently rename a quiz the user already gave a custom name.
    quiz_name: Optional[str] = Field(default=None, max_length=200)


class QuizQuestion(BaseModel):
    """One normalized quiz question returned to the frontend."""
    id: int
    question: str
    options: list[str]
    correct_answer: str
    question_type: Literal["single_choice", "true_false", "multi_select", "fill_blank"] = "single_choice"
    correct_answers: list[str] = Field(default_factory=list)
    topic_id: str
    topic_name: str = ""
    concept_id: str = ""
    concept_name: str = ""
    source_subtopic_ids: list[str] = Field(default_factory=list)
    concept_origin: str = ""
    concept_plan_id: str = ""
    assessment_capacity: int = 0
    difficulty: str
    explanation: str
    source_chunk_ids: list[str]


class QuizGenerateResponse(BaseModel):
    """Response body returned by POST /api/quiz/generate."""
    document_id: str
    quiz_id: Optional[str] = None
    document_hash: Optional[str] = None
    title: Optional[str] = None
    question_count: int
    # requested_count is the user's TARGET; actual_count is what was really returned. status is
    # "complete" when they match and "partial" when fewer valid questions than requested exist.
    requested_count: Optional[int] = None
    actual_count: Optional[int] = None
    status: Optional[str] = None
    difficulty: str
    topic_id: str
    topic_name: Optional[str] = None
    assessment_scope: str = "topic"
    assessment_plan: dict = Field(default_factory=dict)
    # The model that actually generated this quiz: {"model_id", "name", "quantization"}.
    generation_model: Optional[dict] = None
    created_at: str
    questions: list[QuizQuestion]


@router.get("/api/quiz/{document_id}")
def quiz_detail(
    document_id: str, topic_id: str, difficulty: str = "easy", quiz_id: Optional[str] = None,
    current_user: dict = Depends(require_current_user),
) -> dict:
    """Load a saved quiz and its latest attempt for one document.

    With `quiz_id`, loads exactly that quiz artifact (never a different one, even if a newer,
    "more compatible" quiz exists at the same document/topic/difficulty) -- required now that
    several quizzes can share that slot. Without it, falls back to the slot's most recently
    generated quiz (used only by flows that have no specific quiz_id to give, e.g. the create form's
    "saved" hint).
    """
    try:
        return load_quiz_with_attempt(document_id, difficulty, topic_id, current_user["id"], quiz_id=quiz_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load quiz. Original error: {error}",
        ) from error


@router.post("/api/quiz/generate", response_model=QuizGenerateResponse)
def quiz_generate(request: QuizGenerateRequest, current_user: dict = Depends(require_current_user)) -> QuizGenerateResponse:
    """
    Generate a grounded multiple-choice quiz from one indexed document.

    The real quiz-RAG logic lives in backend.quiz_service.generate_quiz(). This
    route validates the API-level request and translates service errors into
    HTTP errors.
    """
    if not request.document_id.strip():
        raise HTTPException(status_code=400, detail="document_id is required.")

    quiz_name = request.quiz_name.strip()
    if not quiz_name:
        raise HTTPException(status_code=400, detail="quiz_name must not be empty or whitespace-only.")

    try:
        print(
            f"[quiz-api] document_id={request.document_id}, "
            f"assessment_scope={request.assessment_scope}, difficulty={request.difficulty}"
        )
        quiz_model_id = request.model_id or QUIZ_DEFAULT_GENERATION_MODEL
        # Kaggle only pulls the default (Qwen) at startup; every other model is lazy, so make sure
        # the selected model is actually in the Ollama cache before generation ever touches it.
        # Never falls back to another model: a failed pull raises and this request fails.
        prepare_generation_model(quiz_model_id)
        result = generate_quiz(
            document_id=request.document_id,
            difficulty=request.difficulty,
            assessment_scope=request.assessment_scope,
            topic_id=request.topic_id,
            question_count=request.question_count,
            owner_id=current_user["id"],
            model_id=resolve_generation_model(quiz_model_id),
            quiz_name=quiz_name,
            regenerate=request.regenerate,
        )
        return QuizGenerateResponse(**result)
    except QuizGenerationError as error:
        raise HTTPException(status_code=422, detail=error.detail) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Assessment Agent failed to generate the quiz. Please check that Ollama is running, "
                "the configured model is available, and the selected document has indexed chunks. "
                f"Original error: {error}"
            ),
        ) from error


@regenerate_router.post("/api/quiz/{document_id}/regenerate", response_model=QuizGenerateResponse)
def quiz_regenerate(document_id: str, request: QuizRegenerateRequest, current_user: dict = Depends(require_current_user)) -> QuizGenerateResponse:
    """
    Intentionally replace a saved quiz for one document.

    Attempts for that document are reset after a successful regeneration so
    old answers are not shown against a new question set.
    """
    try:
        quiz_model_id = getattr(request, "model_id", None) or QUIZ_DEFAULT_GENERATION_MODEL
        prepare_generation_model(quiz_model_id)
        result = generate_quiz(
            document_id=document_id,
            difficulty=request.difficulty,
            assessment_scope=request.assessment_scope,
            topic_id=request.topic_id,
            question_count=request.question_count,
            regenerate=True,
            owner_id=current_user["id"],
            model_id=resolve_generation_model(quiz_model_id),
            quiz_name=(request.quiz_name or "").strip() or None,
        )
        return QuizGenerateResponse(**result)
    except QuizGenerationError as error:
        raise HTTPException(status_code=422, detail=error.detail) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not regenerate quiz. Original error: {error}",
        ) from error
