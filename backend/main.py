import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx
from pydantic import BaseModel, Field

from backend.ingest import delete_indexed_file, index_files
from backend.quiz_service import (
    QuizGenerationError,
    clear_quiz_progress,
    explain_quiz_question,
    generate_quiz,
    list_indexed_documents,
    list_completed_quiz_attempts,
    list_quiz_statuses,
    load_quiz_with_attempt,
    load_completed_quiz_attempt,
    load_quiz_for_retake,
    update_quiz_progress,
    submit_quiz_attempt,
    build_learning_dashboard,
)
from backend.quiz_store import delete_document_quiz_data
from backend.mastery_service import recompute_all_mastery, recompute_topic_mastery
from backend.knowledge_gap_service import detect_knowledge_gaps
from backend.recommendation_service import generate_recommendations
from backend.conversation_store import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    remove_source_from_conversations,
    set_conversation_sources,
    update_conversation_title,
)
from backend.rag_service import answer_conversation_message, list_uploaded_sources
from backend.model_registry import list_generation_models, prepare_generation_model, resolve_generation_model
from backend.summary_service import generate_document_summary
from backend.summary_store import delete_document_summaries
from backend.flashcard_service import authoritative_card_fields, generate_flashcards
from backend.flashcard_store import add_flashcard, delete_document_flashcards, delete_flashcard, update_flashcard
from config import AUTH_COOKIE_NAME, AUTH_COOKIE_SECURE, AUTH_SESSION_DAYS, CHAT_MODEL, DATA_DIR, EMBEDDING_MODEL, OLLAMA_BASE_URL
from backend.auth_store import (
    authenticate_user,
    create_session,
    create_user,
    get_user_for_session,
    revoke_session,
)


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


class HealthResponse(BaseModel):
    """Response body returned by GET /api/model/health."""
    ok: bool
    model: str
    status: str


class ServiceHealthResponse(BaseModel):
    status: str
    ollama: bool
    chat_model: str
    embedding_model: str
    models_ready: bool
    missing_models: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class SourceSummary(BaseModel):
    """
    Summary of an indexed/uploaded document.

    This shape is used by the AI Tutor source list.
    """
    sourceId: int
    title: str
    chunks: int
    path: str


class UploadResponse(BaseModel):
    """
    Response returned after uploading and indexing documents.

    It includes indexing stats plus the refreshed source list so the frontend can
    update immediately without making another GET /api/sources call.
    """
    new_files: int
    new_chunks: int
    skipped_files: list[str]
    total_indexed_files: int
    sources: list[SourceSummary]


class DeleteSourceResponse(BaseModel):
    """
    Response returned after deleting one indexed document.

    It includes the refreshed source list for the same reason as UploadResponse.
    """
    deleted: str
    deleted_chunks: int
    total_indexed_files: int
    sources: list[SourceSummary]


class DocumentSummary(BaseModel):
    """
    Summary of an indexed document used by the quiz document dropdown.

    This is similar to SourceSummary, but it uses id/title/chunks because the
    Practice page needs the document id as a select value.
    """
    id: str
    title: str
    chunks: int
    topics: list[dict] = Field(default_factory=list)


class SummaryGenerateRequest(BaseModel):
    model_id: Optional[str] = None


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


class QuizGenerateRequest(BaseModel):
    """Request body for POST /api/quiz/generate."""
    document_id: str
    assessment_scope: str = Field(pattern="^(topic|document)$")
    topic_id: Optional[str] = None
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    question_count: Literal[10, 15, 20, 25] = 10
    model_id: Optional[str] = None


class QuizRegenerateRequest(BaseModel):
    """Request body for POST /api/quiz/{document_id}/regenerate."""
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    model_id: Optional[str] = None
    assessment_scope: str = Field(pattern="^(topic|document)$")
    topic_id: Optional[str] = None
    question_count: Literal[10, 15, 20, 25] = 10


class QuizProgressRequest(BaseModel):
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    question_id: int = Field(ge=1)
    selected_answer: str = Field(pattern="^[ABCDabcd]$")
    topic_id: str


class MasteryRecomputeRequest(BaseModel):
    document_id: Optional[str] = None
    topic_id: Optional[str] = None


class QuizExplainRequest(BaseModel):
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str


class QuizQuestion(BaseModel):
    """One normalized quiz question returned to the frontend."""
    id: int
    question: str
    options: list[str]
    correct_answer: str
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


class QuizSubmitRequest(BaseModel):
    quiz_id: Optional[str] = None
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str
    answers: dict[str, str]


class QuizGenerateResponse(BaseModel):
    """Response body returned by POST /api/quiz/generate."""
    document_id: str
    quiz_id: Optional[str] = None
    document_hash: Optional[str] = None
    title: Optional[str] = None
    question_count: int
    difficulty: str
    topic_id: str
    topic_name: Optional[str] = None
    assessment_scope: str = "topic"
    assessment_plan: dict = Field(default_factory=dict)
    created_at: str
    questions: list[QuizQuestion]


class SignupRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def require_current_user(request: Request) -> dict:
    user = get_user_for_session(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _validate_email(email: str) -> str:
    import re
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return normalized


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


# FastAPI application object. Uvicorn imports this as backend.main:app.
app = FastAPI(title="Tutoring Backend")

# CORS allows the frontend dev server on port 3000 to call the backend on 8000.
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "AI_TUTOR_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/auth/signup", status_code=201)
def auth_signup(request: SignupRequest, response: Response) -> dict:
    display_name = request.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Display name cannot be blank.")
    try:
        user = create_user(display_name, _validate_email(request.email), request.password)
        token, _session = create_session(user["id"])
        _set_session_cookie(response, token)
        return user
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/auth/login")
def auth_login(request: LoginRequest, response: Response) -> dict:
    user = authenticate_user(_validate_email(request.email), request.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token, _session = create_session(user["id"])
    _set_session_cookie(response, token)
    return user


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    revoke_session(request.cookies.get(AUTH_COOKIE_NAME))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=AUTH_COOKIE_SECURE, samesite="lax")
    return {"signed_out": True}


@app.get("/api/auth/me")
def auth_me(current_user: dict = Depends(require_current_user)) -> dict:
    return current_user


@app.get("/")
def root() -> dict[str, str]:
    """
    Simple root endpoint to verify the backend is reachable.

    This does not check Ollama or Chroma; it only confirms FastAPI is running.
    """
    return {"message": "Tutoring backend is running"}


@app.get("/api/model/health", response_model=HealthResponse)
def model_health() -> HealthResponse:
    """
    Return a lightweight backend/model status response.

    The frontend can use this to display which chat model is configured. This is
    not a deep health check because it does not call Ollama.
    """
    return HealthResponse(
        ok=True,
        model=CHAT_MODEL,
        status="Backend is running",
    )


@app.get("/api/health", response_model=ServiceHealthResponse)
def service_health() -> ServiceHealthResponse:
    """Check Ollama and configured model availability without running inference."""
    try:
        response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3.0)
        response.raise_for_status()
        available = {
            str(item.get("name", ""))
            for item in response.json().get("models", [])
        }
        missing = [
            model for model in (CHAT_MODEL, EMBEDDING_MODEL)
            if model not in available and f"{model}:latest" not in available
        ]
        return ServiceHealthResponse(
            status="ok" if not missing else "degraded",
            ollama=True,
            chat_model=CHAT_MODEL,
            embedding_model=EMBEDDING_MODEL,
            models_ready=not missing,
            missing_models=missing,
            error=(f"Missing Ollama model(s): {', '.join(missing)}" if missing else None),
        )
    except Exception as error:
        return ServiceHealthResponse(
            status="degraded",
            ollama=False,
            chat_model=CHAT_MODEL,
            embedding_model=EMBEDDING_MODEL,
            models_ready=False,
            missing_models=[CHAT_MODEL, EMBEDDING_MODEL],
            error=f"Ollama is not ready at {OLLAMA_BASE_URL}: {error}",
        )


@app.get("/api/models")
def models(current_user: dict = Depends(require_current_user)) -> dict:
    return {"models": list_generation_models()}


@app.post("/api/models/{model_id}/prepare")
def prepare_model(model_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return prepare_generation_model(model_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=f"Could not prepare model: {error}") from error


@app.get("/api/sources", response_model=list[SourceSummary])
def sources(current_user: dict = Depends(require_current_user)) -> list[SourceSummary]:
    """
    Return all indexed sources for the AI Tutor source panel.

    Data comes from the owner-scoped SQLite indexed document registry.
    """
    try:
        return [SourceSummary(**source) for source in list_uploaded_sources(current_user["id"])]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load uploaded sources. Original error: {error}",
        ) from error


@app.get("/api/sources/{document_id}/content")
def source_content(document_id: str, current_user: dict = Depends(require_current_user)) -> FileResponse:
    """Serve one owned original upload for the Study Session document viewer."""
    source = next((item for item in list_uploaded_sources(current_user["id"]) if item["title"] == document_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Document not found.")
    path = Path(source["path"]).resolve()
    owner_dir = (DATA_DIR / "users" / current_user["id"]).resolve()
    if owner_dir not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Original upload is unavailable.")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")


@app.post("/api/sources/upload", response_model=UploadResponse)
async def upload_sources(files: list[UploadFile] = File(...), current_user: dict = Depends(require_current_user)) -> UploadResponse:
    """
    Upload PDF/TXT files and index them into the vectorstore.

    Full flow:
    1. Receive multipart files from frontend.
    2. Validate file extensions.
    3. Save files into data/.
    4. Call backend.ingest.index_files() to chunk/embed/store them in Chroma.
    5. Return indexing stats and refreshed source list.
    """
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one PDF or TXT file.")

    user_data_dir = DATA_DIR / "users" / current_user["id"]
    user_data_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    allowed_extensions = {".pdf", ".txt"}

    try:
        for uploaded_file in files:
            # Keep only the final file name so a browser-provided path cannot
            # write outside data/.
            file_name = Path(uploaded_file.filename or "").name
            suffix = Path(file_name).suffix.lower()

            if not file_name or suffix not in allowed_extensions:
                raise HTTPException(
                    status_code=400,
                    detail="Only PDF and TXT files can be uploaded.",
                )

            save_path = user_data_dir / file_name
            content = await uploaded_file.read()
            save_path.write_bytes(content)
            saved_paths.append(save_path)

        # index_files() handles duplicate detection through file hashes and
        # Writes owner-scoped Chroma vectors and SQLite indexed-document metadata.
        result = index_files(saved_paths, current_user["id"])
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources(current_user["id"])]

        return UploadResponse(
            new_files=result["new_files"],
            new_chunks=result["new_chunks"],
            skipped_files=result["skipped_files"],
            total_indexed_files=result["total_indexed_files"],
            sources=sources_result,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not upload and index the selected files. Please check that Ollama is running, "
                "the embedding model is available, and the files contain extractable text. "
                f"Original error: {error}"
            ),
        ) from error


@app.delete("/api/sources/{document_id}", response_model=DeleteSourceResponse)
def delete_source(document_id: str, current_user: dict = Depends(require_current_user)) -> DeleteSourceResponse:
    """
    Delete one indexed document from the system.

    This removes the owner's document from Chroma, the SQLite registry, and data/. It also
    deletes quiz data for that document so stale quiz questions/attempts cannot be reused.
    """
    try:
        result = delete_indexed_file(document_id, current_user["id"])
        delete_document_quiz_data(result["deleted"], current_user["id"])
        delete_document_summaries(current_user["id"], result["deleted"])
        delete_document_flashcards(current_user["id"], result["deleted"])
        remove_source_from_conversations(current_user["id"], result["deleted"])
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources(current_user["id"])]

        return DeleteSourceResponse(
            deleted=result["deleted"],
            deleted_chunks=result["deleted_chunks"],
            total_indexed_files=result["total_indexed_files"],
            sources=sources_result,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete the selected document. Original error: {error}",
        ) from error


@app.get("/api/documents", response_model=list[DocumentSummary])
def documents(current_user: dict = Depends(require_current_user)) -> list[DocumentSummary]:
    """
    Return indexed documents for the Practice quiz dropdown.

    This endpoint intentionally uses quiz_service.list_indexed_documents() so
    the quiz feature owns the shape it needs.
    """
    try:
        return [DocumentSummary(**document) for document in list_indexed_documents(current_user["id"])]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load indexed documents. Original error: {error}",
        ) from error


@app.get("/api/quizzes")
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


@app.get("/api/summary/{document_id}")
def summary_detail(document_id: str, model_id: Optional[str] = None, current_user: dict = Depends(require_current_user)) -> dict:
    """Return a compatible persisted summary or generate it from existing indexed chunks."""
    try:
        return generate_document_summary(current_user["id"], document_id, model_id=model_id)
    except ValueError as error:
        message = str(error)
        raise HTTPException(status_code=404 if message == "Document not found." else 400, detail=message) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not generate document summary: {error}") from error


@app.post("/api/summary/{document_id}/regenerate")
def summary_regenerate(document_id: str, request: SummaryGenerateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Explicitly generate and persist a fresh summary version."""
    try:
        return generate_document_summary(current_user["id"], document_id, model_id=request.model_id, regenerate=True)
    except ValueError as error:
        message = str(error)
        raise HTTPException(status_code=404 if message == "Document not found." else 400, detail=message) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not regenerate document summary: {error}") from error


@app.get("/api/flashcards/{document_id}")
def flashcards_detail(document_id: str, topic_ids: list[str] | None = Query(default=None),
                      model_id: Optional[str] = None, current_user: dict = Depends(require_current_user)) -> dict:
    """Reuse or generate grounded cards from existing owner-scoped indexed chunks."""
    try:
        return generate_flashcards(current_user["id"], document_id, topic_ids=topic_ids, model_id=model_id)
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Document not found." else 400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load flashcards: {error}") from error


@app.post("/api/flashcards/{document_id}/cards")
def flashcard_create(document_id: str, request: FlashcardCreateRequest,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        identity = authoritative_card_fields(current_user["id"], document_id, request.topic_id, request.subtopic_id)
        return add_flashcard(current_user["id"], document_id, request.set_id, {
            **identity, "front": request.front.strip(), "back": request.back.strip(), "source_chunk_ids": [],
        })
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.patch("/api/flashcards/{document_id}/cards/{flashcard_id}")
def flashcard_update(document_id: str, flashcard_id: str, request: FlashcardUpdateRequest,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return update_flashcard(current_user["id"], document_id, flashcard_id, request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Flashcard not found." else 400, detail=str(error)) from error


@app.delete("/api/flashcards/{document_id}/cards/{flashcard_id}")
def flashcard_delete(document_id: str, flashcard_id: str,
                     current_user: dict = Depends(require_current_user)) -> dict:
    try:
        delete_flashcard(current_user["id"], document_id, flashcard_id)
        return {"deleted": flashcard_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/quiz-history")
def quiz_history(document_id: Optional[str] = None, difficulty: Optional[str] = None, current_user: dict = Depends(require_current_user)) -> list[dict]:
    """List completed quiz attempts for the history UI."""
    try:
        return list_completed_quiz_attempts(document_id, difficulty, current_user["id"])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load quiz history: {error}") from error


@app.get("/api/quiz-history/{attempt_id}")
def quiz_history_detail(attempt_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    """Load one completed attempt with its question snapshots."""
    try:
        return load_completed_quiz_attempt(attempt_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/quiz-history/{attempt_id}/retake")
def quiz_history_retake(attempt_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    """Load the exact persisted quiz, including inactive and migrated legacy quizzes."""
    try:
        return load_quiz_for_retake(attempt_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/quiz/{document_id}")
def quiz_detail(
    document_id: str, topic_id: str, difficulty: str = "easy",
    current_user: dict = Depends(require_current_user),
) -> dict:
    """Load the saved quiz and latest attempt for one document."""
    try:
        return load_quiz_with_attempt(document_id, difficulty, topic_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load quiz. Original error: {error}",
        ) from error


@app.post("/api/quiz/generate", response_model=QuizGenerateResponse)
def quiz_generate(request: QuizGenerateRequest, current_user: dict = Depends(require_current_user)) -> QuizGenerateResponse:
    """
    Generate a grounded multiple-choice quiz from one indexed document.

    The real quiz-RAG logic lives in backend.quiz_service.generate_quiz(). This
    route validates the API-level request and translates service errors into
    HTTP errors.
    """
    if not request.document_id.strip():
        raise HTTPException(status_code=400, detail="document_id is required.")

    try:
        print(
            f"[quiz-api] document_id={request.document_id}, "
            f"assessment_scope={request.assessment_scope}, difficulty={request.difficulty}"
        )
        result = generate_quiz(
            document_id=request.document_id,
            difficulty=request.difficulty,
            assessment_scope=request.assessment_scope,
            topic_id=request.topic_id,
            question_count=request.question_count,
            owner_id=current_user["id"],
            model_id=resolve_generation_model(request.model_id),
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


@app.patch("/api/quiz/{document_id}/progress")
def quiz_progress(document_id: str, request: QuizProgressRequest, current_user: dict = Depends(require_current_user)) -> dict:
    """Check one selected answer and autosave current quiz progress."""
    try:
        return update_quiz_progress(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            question_id=request.question_id,
            selected_answer=request.selected_answer,
            student_id=current_user["id"],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not save quiz progress: {error}") from error


@app.post("/api/quiz/{document_id}/submit")
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
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not submit quiz: {error}") from error


@app.delete("/api/quiz/{document_id}/progress")
def quiz_progress_reset(
    document_id: str, topic_id: str, difficulty: str,
    current_user: dict = Depends(require_current_user),
) -> dict:
    """Clear current quiz progress while preserving completed history."""
    try:
        return clear_quiz_progress(document_id, difficulty, topic_id, current_user["id"])
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/quiz/{document_id}/questions/{question_id}/explain")
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


@app.post("/api/quiz/{document_id}/regenerate", response_model=QuizGenerateResponse)
def quiz_regenerate(document_id: str, request: QuizRegenerateRequest, current_user: dict = Depends(require_current_user)) -> QuizGenerateResponse:
    """
    Intentionally replace a saved quiz for one document.

    Attempts for that document are reset after a successful regeneration so
    old answers are not shown against a new question set.
    """
    try:
        result = generate_quiz(
            document_id=document_id,
            difficulty=request.difficulty,
            assessment_scope=request.assessment_scope,
            topic_id=request.topic_id,
            question_count=request.question_count,
            regenerate=True,
            owner_id=current_user["id"],
            model_id=resolve_generation_model(getattr(request, "model_id", None)),
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


@app.get("/api/mastery/{document_id}/{topic_id}")
def topic_mastery(
    document_id: str, topic_id: str, current_user: dict = Depends(require_current_user)
) -> dict:
    """Return mastery rebuilt from completed attempt-answer snapshots."""
    try:
        return recompute_topic_mastery(current_user["id"], document_id, topic_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not compute mastery: {error}") from error


@app.get("/api/dashboard")
def learning_dashboard(current_user: dict = Depends(require_current_user)) -> dict:
    """Return the real project state used by the Overview and mastery UI."""
    try:
        return build_learning_dashboard(current_user["id"])
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load dashboard: {error}") from error


@app.get("/api/knowledge-gaps")
def knowledge_gaps(current_user: dict = Depends(require_current_user)) -> list[dict]:
    """Return reliable mastery-derived gaps for the authenticated user."""
    return detect_knowledge_gaps(current_user["id"])


@app.get("/api/knowledge-gaps/{document_id}")
def document_knowledge_gaps(
    document_id: str, current_user: dict = Depends(require_current_user)
) -> list[dict]:
    """Return reliable mastery-derived gaps for one owned assessment history."""
    return detect_knowledge_gaps(current_user["id"], document_id)


@app.get("/api/recommendations")
def recommendations(current_user: dict = Depends(require_current_user)) -> list[dict]:
    """Return ranked next actions derived from the authenticated user's current state."""
    return generate_recommendations(current_user["id"])


@app.get("/api/recommendations/{document_id}")
def document_recommendations(
    document_id: str, current_user: dict = Depends(require_current_user)
) -> list[dict]:
    return generate_recommendations(current_user["id"], document_id)


@app.post("/api/mastery/recompute")
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


@app.post("/api/conversations")
def conversation_create(request: ConversationCreateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    _validate_conversation_document_ids(current_user["id"], request.document_ids)
    return create_conversation(current_user["id"], request.title, request.document_ids)


@app.get("/api/conversations")
def conversation_list(current_user: dict = Depends(require_current_user)) -> list[dict]:
    return list_conversations(current_user["id"])


@app.get("/api/conversations/{conversation_id}")
def conversation_get(conversation_id: str, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return get_conversation(current_user["id"], conversation_id, include_messages=True)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.patch("/api/conversations/{conversation_id}")
def conversation_update(conversation_id: str, request: ConversationUpdateRequest, current_user: dict = Depends(require_current_user)) -> dict:
    try:
        return update_conversation_title(current_user["id"], conversation_id, request.title)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/conversations/{conversation_id}")
def conversation_delete(conversation_id: str, current_user: dict = Depends(require_current_user)) -> dict[str, str]:
    try:
        delete_conversation(current_user["id"], conversation_id)
        return {"deleted": conversation_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.put("/api/conversations/{conversation_id}/sources")
def conversation_sources_update(conversation_id: str, request: ConversationSourcesRequest, current_user: dict = Depends(require_current_user)) -> dict:
    _validate_conversation_document_ids(current_user["id"], request.document_ids)
    try:
        return set_conversation_sources(current_user["id"], conversation_id, request.document_ids)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/conversations/{conversation_id}/messages")
def conversation_message(conversation_id: str, request: ConversationMessageRequest, current_user: dict = Depends(require_current_user)) -> dict:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message is required.")
    try:
        return answer_conversation_message(current_user["id"], conversation_id, request.message, request.model_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer this conversation. Original error: {error}",
        ) from error
