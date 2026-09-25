import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx
from pydantic import BaseModel, Field

from backend.api.auth import router as auth_router
from backend.api.conversations import router as conversations_router
from backend.api.learning import router as learning_router
from backend.api.planner_legacy import router as planner_legacy_router
from backend.api.planner_v2 import router as planner_v2_router
from backend.api.deps import require_admin_user, require_current_user
from backend.ingest import delete_indexed_file, index_files
from backend.quiz_attempt_service import (
    clear_quiz_progress,
    explain_quiz_question,
    list_completed_quiz_attempts,
    load_quiz_with_attempt,
    load_completed_quiz_attempt,
    load_quiz_for_retake,
    update_quiz_progress,
    submit_quiz_attempt,
)
from backend.quiz_common import list_indexed_documents
from backend.quiz_service import (
    QuizGenerationError,
    delete_quiz,
    generate_quiz,
    list_quiz_statuses,
)
from backend.quiz_store import delete_document_quiz_data
from backend.conversation_store import remove_source_from_conversations
from backend.rag_service import list_uploaded_sources
from backend.model_registry import list_generation_models, prepare_generation_model, resolve_generation_model
from backend.summary_service import generate_document_summary
from backend.summary_store import delete_document_summaries
from backend.flashcard_service import FlashcardGenerationError, authoritative_card_fields, generate_flashcards
from backend.flashcard_store import add_flashcard, delete_document_flashcards, delete_flashcard, update_flashcard
from backend import study_planner_store
from backend.model_comparison_service import get_quiz_model_comparison
from backend.model_benchmark_service import DEFAULT_RUNS as BENCHMARK_DEFAULT_RUNS, BenchmarkAlreadyRunning, start_benchmark
from config import CHAT_MODEL, DATA_DIR, EMBEDDING_MODEL, OLLAMA_BASE_URL, QUIZ_DEFAULT_GENERATION_MODEL


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


class FlashcardGenerateRequest(BaseModel):
    model_id: Optional[str] = None
    language: Optional[str] = None


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


class QuizSubmitRequest(BaseModel):
    quiz_id: Optional[str] = None
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str
    answers: dict[str, str | list[str]]
    # The Quiz Player's "Submit Anyway": unanswered questions are graded as incorrect/unanswered
    # instead of rejecting the submission. Other callers keep the complete-answer-set rule.
    allow_unanswered: bool = False


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


app.include_router(auth_router)


@app.get("/api/admin/quiz-model-comparison")
def admin_quiz_model_comparison(_admin: dict = Depends(require_admin_user)) -> dict:
    """Admin-only: the latest stored Quiz model benchmark (config, progress, aggregates, raw runs)."""
    return get_quiz_model_comparison()


class QuizModelBenchmarkRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1)
    difficulty: Literal["easy", "medium", "difficult"] = "medium"
    question_count: int = 12
    runs: int = BENCHMARK_DEFAULT_RUNS


@app.post("/api/admin/quiz-model-benchmark", status_code=202)
def admin_run_quiz_model_benchmark(
    request: QuizModelBenchmarkRequest, admin: dict = Depends(require_admin_user),
) -> dict:
    """Admin-only: start one sequential Qwen vs Gemma benchmark in the background.

    Uses the admin's own documents and flashcards; benchmark quizzes are saved to the admin's
    library (titled "[Benchmark] ...") so each run's quiz_id can be opened. 409 while one runs.
    """
    try:
        start_benchmark(admin["id"], request.document_ids, request.difficulty, request.question_count, request.runs)
    except BenchmarkAlreadyRunning as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return get_quiz_model_comparison()


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
        study_planner_store.delete_document_plan_data(current_user["id"], result["deleted"])
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

    This endpoint intentionally uses quiz_common.list_indexed_documents() so
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


@app.delete("/api/quizzes/{quiz_id}")
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


@app.get("/api/summary/{document_id}")
def summary_detail(document_id: str, model_id: Optional[str] = None, cache_only: bool = False,
                   current_user: dict = Depends(require_current_user)) -> dict:
    """Return a compatible persisted summary or generate it from existing indexed chunks.

    With cache_only=true nothing is generated: a missing summary is reported as status "not_generated".
    """
    try:
        if cache_only:
            # A pure existence check never prepares/pulls anything.
            return generate_document_summary(current_user["id"], document_id, model_id=model_id, cache_only=True)
        prepare_generation_model(model_id)
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
        prepare_generation_model(request.model_id)
        return generate_document_summary(current_user["id"], document_id, model_id=request.model_id, regenerate=True)
    except ValueError as error:
        message = str(error)
        raise HTTPException(status_code=404 if message == "Document not found." else 400, detail=message) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not regenerate document summary: {error}") from error


@app.get("/api/flashcards/{document_id}")
def flashcards_detail(document_id: str, topic_ids: list[str] | None = Query(default=None),
                      model_id: Optional[str] = None, language: Optional[str] = None, cache_only: bool = False,
                      current_user: dict = Depends(require_current_user)) -> dict:
    """Reuse or generate grounded cards from existing owner-scoped indexed chunks.

    With cache_only=true nothing is generated: missing cards are reported as status "not_generated".
    """
    try:
        if cache_only:
            # A pure existence check never prepares/pulls anything.
            return generate_flashcards(current_user["id"], document_id, topic_ids=topic_ids, model_id=model_id,
                                       language=language, cache_only=True)
        prepare_generation_model(model_id)
        return generate_flashcards(current_user["id"], document_id, topic_ids=topic_ids, model_id=model_id, language=language)
    except FlashcardGenerationError as error:
        # Technical detail (model/runtime failure, e.g. an Ollama repeat-limit abort or malformed
        # JSON) stays server-side; the client only ever sees the safe, model-agnostic message.
        print(f"[flashcards] generation failed for document_id={document_id}: {error.technical_message}")
        raise HTTPException(status_code=502, detail=error.safe_message) from error
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Document not found." else 400, detail=str(error)) from error
    except Exception as error:
        print(f"[flashcards] unexpected failure for document_id={document_id}: {error}")
        raise HTTPException(status_code=500, detail=FlashcardGenerationError.SAFE_MESSAGE) from error


@app.post("/api/flashcards/{document_id}/regenerate")
def flashcards_regenerate(document_id: str, request: FlashcardGenerateRequest,
                          current_user: dict = Depends(require_current_user)) -> dict:
    """Explicitly generate and persist a fresh flashcard set (bypasses the cache lookup)."""
    try:
        prepare_generation_model(request.model_id)
        return generate_flashcards(current_user["id"], document_id, model_id=request.model_id,
                                   language=request.language, regenerate=True)
    except FlashcardGenerationError as error:
        print(f"[flashcards] regeneration failed for document_id={document_id}: {error.technical_message}")
        raise HTTPException(status_code=502, detail=error.safe_message) from error
    except ValueError as error:
        raise HTTPException(status_code=404 if str(error) == "Document not found." else 400, detail=str(error)) from error
    except Exception as error:
        print(f"[flashcards] unexpected regeneration failure for document_id={document_id}: {error}")
        raise HTTPException(status_code=500, detail=FlashcardGenerationError.SAFE_MESSAGE) from error


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


@app.patch("/api/quiz/{document_id}/progress")
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
            allow_unanswered=request.allow_unanswered,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not submit quiz: {error}") from error


@app.delete("/api/quiz/{document_id}/progress")
def quiz_progress_reset(
    document_id: str, topic_id: str, difficulty: str, quiz_id: Optional[str] = None,
    current_user: dict = Depends(require_current_user),
) -> dict:
    """Clear current quiz progress while preserving completed history."""
    try:
        return clear_quiz_progress(document_id, difficulty, topic_id, current_user["id"], quiz_id=quiz_id)
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


app.include_router(learning_router)


app.include_router(conversations_router)


app.include_router(planner_legacy_router)


app.include_router(planner_v2_router)
