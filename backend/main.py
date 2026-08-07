import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field

from backend.ingest import delete_indexed_file, index_files
from backend.quiz_service import (
    clear_quiz_progress,
    explain_quiz_question,
    generate_quiz,
    list_indexed_documents,
    list_completed_quiz_attempts,
    list_quiz_statuses,
    load_quiz_with_attempt,
    load_completed_quiz_attempt,
    update_quiz_progress,
)
from backend.quiz_store import delete_document_quiz_data
from backend.mastery_service import recompute_all_mastery, recompute_topic_mastery
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
from config import CHAT_MODEL, DATA_DIR, EMBEDDING_MODEL, OLLAMA_BASE_URL


class ConversationCreateRequest(BaseModel):
    title: str = "New conversation"
    document_ids: list[str] = Field(default_factory=list)


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationSourcesRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list)


class ConversationMessageRequest(BaseModel):
    message: str = Field(min_length=1)


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


class QuizGenerateRequest(BaseModel):
    """Request body for POST /api/quiz/generate."""
    document_id: str
    topic_id: str
    question_count: int = Field(ge=1, le=40)
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")


class QuizRegenerateRequest(BaseModel):
    """Request body for POST /api/quiz/{document_id}/regenerate."""
    question_count: int = Field(ge=1, le=40)
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    topic_id: str


class QuizProgressRequest(BaseModel):
    difficulty: str = Field(pattern="^(easy|medium|difficult)$")
    question_id: int = Field(ge=1)
    selected_answer: str = Field(pattern="^[ABCDabcd]$")
    topic_id: str
    student_id: str = "local_student"


class MasteryRecomputeRequest(BaseModel):
    student_id: Optional[str] = None
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
    difficulty: str
    topic_id: str
    topic_name: Optional[str] = None
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


@app.get("/api/sources", response_model=list[SourceSummary])
def sources() -> list[SourceSummary]:
    """
    Return all indexed sources for the AI Tutor source panel.

    Data comes from indexed_files.json through rag_service.list_uploaded_sources().
    """
    try:
        return [SourceSummary(**source) for source in list_uploaded_sources()]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load uploaded sources. Original error: {error}",
        ) from error


@app.post("/api/sources/upload", response_model=UploadResponse)
async def upload_sources(files: list[UploadFile] = File(...)) -> UploadResponse:
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

    DATA_DIR.mkdir(exist_ok=True)
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

            save_path = DATA_DIR / file_name
            content = await uploaded_file.read()
            save_path.write_bytes(content)
            saved_paths.append(save_path)

        # index_files() handles duplicate detection through file hashes and
        # writes both Chroma vectors and indexed_files.json metadata.
        result = index_files(saved_paths)
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources()]

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
def delete_source(document_id: str) -> DeleteSourceResponse:
    """
    Delete one indexed document from the system.

    This removes the document from Chroma, indexed_files.json, and data/. It also
    deletes quiz data for that document so stale quiz questions/attempts cannot be reused.
    """
    try:
        result = delete_indexed_file(document_id)
        delete_document_quiz_data(result["deleted"])
        remove_source_from_conversations(result["deleted"])
        sources_result = [SourceSummary(**source) for source in list_uploaded_sources()]

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
def documents() -> list[DocumentSummary]:
    """
    Return indexed documents for the Practice quiz dropdown.

    This endpoint intentionally uses quiz_service.list_indexed_documents() so
    the quiz feature owns the shape it needs.
    """
    try:
        return [DocumentSummary(**document) for document in list_indexed_documents()]
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load indexed documents. Original error: {error}",
        ) from error


@app.get("/api/quizzes")
def quizzes() -> list[dict]:
    """
    Return quiz generation and completion status for all indexed documents.

    The Practice page uses this to decide whether the primary action should be
    Generate Quiz, Start Quiz, or Review Quiz.
    """
    try:
        return list_quiz_statuses()
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load quiz statuses. Original error: {error}",
        ) from error


@app.get("/api/quiz-history")
def quiz_history(document_id: Optional[str] = None, difficulty: Optional[str] = None) -> list[dict]:
    """List completed quiz attempts for the history UI."""
    try:
        return list_completed_quiz_attempts(document_id, difficulty)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not load quiz history: {error}") from error


@app.get("/api/quiz-history/{attempt_id}")
def quiz_history_detail(attempt_id: str) -> dict:
    """Load one completed attempt with its question snapshots."""
    try:
        return load_completed_quiz_attempt(attempt_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/quiz/{document_id}")
def quiz_detail(
    document_id: str, topic_id: str, difficulty: str = "easy",
    student_id: str = "local_student",
) -> dict:
    """Load the saved quiz and latest attempt for one document."""
    try:
        return load_quiz_with_attempt(document_id, difficulty, topic_id, student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load quiz. Original error: {error}",
        ) from error


@app.post("/api/quiz/generate", response_model=QuizGenerateResponse)
def quiz_generate(request: QuizGenerateRequest) -> QuizGenerateResponse:
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
            f"question_count={request.question_count}, difficulty={request.difficulty}"
        )
        result = generate_quiz(
            document_id=request.document_id,
            topic_id=request.topic_id,
            question_count=request.question_count,
            difficulty=request.difficulty,
        )
        return QuizGenerateResponse(**result)
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
def quiz_progress(document_id: str, request: QuizProgressRequest) -> dict:
    """Check one selected answer and autosave current quiz progress."""
    try:
        return update_quiz_progress(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            question_id=request.question_id,
            selected_answer=request.selected_answer,
            student_id=request.student_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not save quiz progress: {error}") from error


@app.delete("/api/quiz/{document_id}/progress")
def quiz_progress_reset(
    document_id: str, topic_id: str, difficulty: str,
    student_id: str = "local_student",
) -> dict:
    """Clear current quiz progress while preserving completed history."""
    try:
        return clear_quiz_progress(document_id, difficulty, topic_id, student_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/quiz/{document_id}/questions/{question_id}/explain")
def quiz_explain(document_id: str, question_id: int, request: QuizExplainRequest) -> dict:
    """Generate a short, document-grounded explanation on demand."""
    try:
        return explain_quiz_question(
            document_id=document_id,
            difficulty=request.difficulty,
            topic_id=request.topic_id,
            question_id=question_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not explain quiz answer: {error}") from error


@app.post("/api/quiz/{document_id}/regenerate", response_model=QuizGenerateResponse)
def quiz_regenerate(document_id: str, request: QuizRegenerateRequest) -> QuizGenerateResponse:
    """
    Intentionally replace a saved quiz for one document.

    Attempts for that document are reset after a successful regeneration so
    old answers are not shown against a new question set.
    """
    try:
        result = generate_quiz(
            document_id=document_id,
            topic_id=request.topic_id,
            question_count=request.question_count,
            difficulty=request.difficulty,
            regenerate=True,
        )
        return QuizGenerateResponse(**result)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not regenerate quiz. Original error: {error}",
        ) from error


@app.get("/api/mastery/{document_id}/{topic_id}")
def topic_mastery(
    document_id: str, topic_id: str, student_id: str = "local_student"
) -> dict:
    """Return mastery rebuilt from completed attempt-answer snapshots."""
    try:
        return recompute_topic_mastery(student_id, document_id, topic_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not compute mastery: {error}") from error


@app.post("/api/mastery/recompute")
def mastery_recompute(request: MasteryRecomputeRequest) -> list[dict] | dict:
    """Rebuild one topic or every historical mastery cache entry."""
    try:
        if request.document_id or request.topic_id:
            if not request.student_id or not request.document_id or not request.topic_id:
                raise ValueError("student_id, document_id, and topic_id are all required for one-topic recomputation.")
            return recompute_topic_mastery(
                request.student_id, request.document_id, request.topic_id
            )
        return recompute_all_mastery(request.student_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Could not rebuild mastery: {error}") from error


def _validate_conversation_document_ids(document_ids: list[str]) -> None:
    available = {source["title"] for source in list_uploaded_sources()}
    missing = sorted(set(document_ids) - available)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"These documents are not indexed: {', '.join(missing)}",
        )


@app.post("/api/conversations")
def conversation_create(request: ConversationCreateRequest) -> dict:
    _validate_conversation_document_ids(request.document_ids)
    return create_conversation(request.title, request.document_ids)


@app.get("/api/conversations")
def conversation_list() -> list[dict]:
    return list_conversations()


@app.get("/api/conversations/{conversation_id}")
def conversation_get(conversation_id: str) -> dict:
    try:
        return get_conversation(conversation_id, include_messages=True)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.patch("/api/conversations/{conversation_id}")
def conversation_update(conversation_id: str, request: ConversationUpdateRequest) -> dict:
    try:
        return update_conversation_title(conversation_id, request.title)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/conversations/{conversation_id}")
def conversation_delete(conversation_id: str) -> dict[str, str]:
    try:
        delete_conversation(conversation_id)
        return {"deleted": conversation_id}
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.put("/api/conversations/{conversation_id}/sources")
def conversation_sources_update(conversation_id: str, request: ConversationSourcesRequest) -> dict:
    _validate_conversation_document_ids(request.document_ids)
    try:
        return set_conversation_sources(conversation_id, request.document_ids)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/conversations/{conversation_id}/messages")
def conversation_message(conversation_id: str, request: ConversationMessageRequest) -> dict:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message is required.")
    try:
        return answer_conversation_message(conversation_id, request.message)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer this conversation. Original error: {error}",
        ) from error
