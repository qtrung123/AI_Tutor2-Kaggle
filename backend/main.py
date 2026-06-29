from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.ingest import delete_indexed_file, index_files
from backend.quiz_service import (
    generate_quiz,
    list_indexed_documents,
    list_quiz_statuses,
    load_quiz_with_attempt,
    submit_quiz_attempt,
)
from backend.quiz_store import delete_document_quiz_data
from backend.rag_service import answer_question, list_uploaded_sources
from config import CHAT_MODEL, DATA_DIR


class ChatRequest(BaseModel):
    """
    Request body for POST /api/chat.

    The frontend sends the user question in message. course/topic are currently
    accepted for future filtering, but the current RAG search uses message only.
    """
    message: str
    course: Optional[str] = None
    topic: Optional[str] = None


class Citation(BaseModel):
    """
    One retrieved source chunk used by the chatbot answer.

    The frontend shows these citations in the AI Tutor source panel after a
    question is answered.
    """
    sourceId: int
    title: str
    page: str
    content: Optional[str] = None


class ChatResponse(BaseModel):
    """Response body returned by POST /api/chat."""
    answer: str
    model: str
    citations: list[Citation]


class HealthResponse(BaseModel):
    """Response body returned by GET /api/model/health."""
    ok: bool
    model: str
    status: str


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


class QuizGenerateRequest(BaseModel):
    """Request body for POST /api/quiz/generate."""
    document_id: str


class QuizSubmitRequest(BaseModel):
    """Request body for POST /api/quiz/{document_id}/submit."""
    answers: dict[str, str]


class QuizSource(BaseModel):
    """Source metadata for one generated quiz question."""
    title: str
    page: str
    chunk: Optional[int] = None


class QuizQuestion(BaseModel):
    """One normalized quiz question returned to the frontend."""
    id: int
    question: str
    options: list[str]
    correct_answer: str
    explanation: str
    difficulty: str
    source: QuizSource


class QuizGenerateResponse(BaseModel):
    """Response body returned by POST /api/quiz/generate."""
    document_id: str
    document_hash: Optional[str] = None
    title: Optional[str] = None
    question_count: int
    difficulty_distribution: dict[str, int]
    created_at: str
    questions: list[QuizQuestion]


# FastAPI application object. Uvicorn imports this as backend.main:app.
app = FastAPI(title="Tutoring Backend")

# CORS allows the frontend dev server on port 3000 to call the backend on 8000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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


@app.get("/api/quiz/{document_id}")
def quiz_detail(document_id: str) -> dict:
    """Load the saved quiz and latest attempt for one document."""
    try:
        return load_quiz_with_attempt(document_id)
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
        result = generate_quiz(document_id=request.document_id)
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


@app.post("/api/quiz/{document_id}/submit")
def quiz_submit(document_id: str, request: QuizSubmitRequest) -> dict:
    """Score, save, and return the latest attempt for one generated quiz."""
    try:
        return submit_quiz_attempt(document_id=document_id, answers=request.answers)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not submit quiz attempt. Original error: {error}",
        ) from error


@app.post("/api/quiz/{document_id}/regenerate", response_model=QuizGenerateResponse)
def quiz_regenerate(document_id: str) -> QuizGenerateResponse:
    """
    Intentionally replace a saved quiz for one document.

    Attempts for that document are reset after a successful regeneration so
    old answers are not shown against a new question set.
    """
    try:
        result = generate_quiz(document_id=document_id, regenerate=True)
        return QuizGenerateResponse(**result)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not regenerate quiz. Original error: {error}",
        ) from error


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """
    Answer a user question using the chatbot RAG service.

    The real RAG logic lives in backend.rag_service.answer_question()/ask_rag().
    This route only validates the request and returns the API response shape.
    """
    if not request.message.strip():
        raise HTTPException(
            status_code=400,
            detail="Message is required. Please enter a question before sending.",
        )

    try:
        result = answer_question(request.message, request.course, request.topic)
        return ChatResponse(**result)
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "RAG backend failed to answer. Please check that Ollama is running, "
                "the configured models are pulled, and the Chroma vectorstore has data. "
                f"Original error: {error}"
            ),
        ) from error
