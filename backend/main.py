import os
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field

from backend.api.auth import router as auth_router
from backend.api.conversations import router as conversations_router
from backend.api.documents import router as documents_router
from backend.api.flashcards import router as flashcards_router
from backend.api.learning import router as learning_router
from backend.api.planner_legacy import router as planner_legacy_router
from backend.api.planner_v2 import router as planner_v2_router
from backend.api.quiz_attempts import history_router as quiz_history_router
from backend.api.quiz_attempts import router as quiz_attempts_router
from backend.api.quiz_generation import regenerate_router as quiz_regenerate_router
from backend.api.quiz_generation import router as quiz_generation_router
from backend.api.quiz_library import router as quiz_library_router
from backend.api.summary import router as summary_router
from backend.api.deps import require_admin_user, require_current_user
from backend.model_registry import list_generation_models, prepare_generation_model
from backend.model_comparison_service import get_quiz_model_comparison
from backend.model_benchmark_service import DEFAULT_RUNS as BENCHMARK_DEFAULT_RUNS, BenchmarkAlreadyRunning, start_benchmark
from config import CHAT_MODEL, EMBEDDING_MODEL, OLLAMA_BASE_URL


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


app.include_router(documents_router)


app.include_router(quiz_library_router)


app.include_router(summary_router)


app.include_router(flashcards_router)


app.include_router(quiz_history_router)


app.include_router(quiz_generation_router)


app.include_router(quiz_attempts_router)


app.include_router(quiz_regenerate_router)


app.include_router(learning_router)


app.include_router(conversations_router)


app.include_router(planner_legacy_router)


app.include_router(planner_v2_router)
