import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Local dev convenience: pull untracked settings (e.g. Google OAuth) from <repo>/.env.local.
# override=False keeps real environment variables authoritative; a missing file (Kaggle) is a no-op.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env.local", override=False)


def _env_path(name: str, default: Path) -> Path:
    """Return an absolute, cross-platform runtime path from an environment variable."""
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default

DATA_DIR = _env_path("AI_TUTOR_DATA_DIR", BASE_DIR / "data")
VECTORSTORE_DIR = _env_path("AI_TUTOR_VECTORSTORE_DIR", BASE_DIR / "vectorstore")
PROMPT_PATH = BASE_DIR / "prompts" / "rag_prompt.txt"
QUIZ_PROMPT_PATH = BASE_DIR / "prompts" / "quiz_prompt.txt"
INDEXED_FILES_PATH = _env_path("AI_TUTOR_INDEXED_FILES_PATH", BASE_DIR / "indexed_files.json")
DATABASE_PATH = _env_path("AI_TUTOR_DATABASE_PATH", DATA_DIR / "conversations.db")
AUTH_COOKIE_NAME = os.getenv("AI_TUTOR_AUTH_COOKIE_NAME", "ai_tutor_session")
AUTH_SESSION_DAYS = int(os.getenv("AI_TUTOR_AUTH_SESSION_DAYS", "14"))
AUTH_COOKIE_SECURE = os.getenv("AI_TUTOR_AUTH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
LEGACY_USER_EMAIL = os.getenv("AI_TUTOR_LEGACY_EMAIL", "legacy-local@invalid.local")
# Comma-separated allowlist of admin emails. Empty by default: nobody is an admin
# until an operator explicitly configures this. There is no self-serve way to
# become an admin from the app.
ADMIN_EMAILS = frozenset(
    email.strip().lower() for email in os.getenv("AI_TUTOR_ADMIN_EMAILS", "").split(",") if email.strip()
)

# Read once by the SQLite migration so existing local quiz data is preserved.
LEGACY_GENERATED_QUIZZES_PATH = DATA_DIR / "generated_quizzes.json"
LEGACY_QUIZ_ATTEMPTS_PATH = DATA_DIR / "quiz_attempts.json"
LEGACY_QUIZ_EXPLANATIONS_PATH = DATA_DIR / "quiz_explanations.json"

# Latest admin Quiz model benchmark (Qwen 2.5 7B vs Gemma 3 12B): config, warm-ups, raw runs and
# per-model aggregates. Written by backend/model_benchmark_service.py, read by Model Comparison.
QUIZ_MODEL_BENCHMARK_RESULTS_PATH = _env_path(
    "AI_TUTOR_QUIZ_MODEL_BENCHMARK_RESULTS_PATH", DATA_DIR / "quiz_model_benchmark_results.json"
)

COLLECTION_NAME = "study_documents"

# Chat-generation backend: "ollama" (default) or "vllm" (a local OpenAI-compatible vLLM server
# serving Qwen 2.5 7B; see backend/llm_backend.py and deployment/start_vllm.sh). Any other value
# falls back to Ollama. Embeddings always stay on Ollama.
_REQUESTED_LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama").strip().lower() or "ollama"
LLM_BACKEND = _REQUESTED_LLM_BACKEND if _REQUESTED_LLM_BACKEND in {"ollama", "vllm"} else "ollama"
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8001/v1").rstrip("/")
VLLM_MODEL = os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")

# Model dùng để trả lời
CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M")
# Comma-separated allowlist: which models the Study Session selector offers/resolves (never which
# models are pulled at Kaggle startup - only Qwen, the default, is pulled/warmed there; every other
# configured model here is pulled lazily on first use, see backend/model_registry.py and
# deployment/start_kaggle.sh). Existing OLLAMA_CHAT_MODEL deployments continue to expose one model.
# vLLM serves Qwen 2.5 7B only, so Gemma is not offered there.
_DEFAULT_GENERATION_MODELS = "qwen-2.5-7b" if LLM_BACKEND == "vllm" else "qwen-2.5-7b,gemma3-12b"
GENERATION_MODELS = tuple(dict.fromkeys(
    model.strip() for model in os.getenv("OLLAMA_GENERATION_MODELS", _DEFAULT_GENERATION_MODELS).split(",") if model.strip()
)) or tuple(_DEFAULT_GENERATION_MODELS.split(","))
DEFAULT_GENERATION_MODEL = os.getenv("OLLAMA_DEFAULT_GENERATION_MODEL", "qwen-2.5-7b")
QUIZ_DEFAULT_GENERATION_MODEL = os.getenv("OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL", "qwen-2.5-7b")
QUIZ_VALIDATION_MODEL = os.getenv("QUIZ_VALIDATION_MODEL", CHAT_MODEL)
QUIZ_SEMANTIC_VALIDATION_ENABLED = os.getenv(
    "QUIZ_SEMANTIC_VALIDATION_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}
QUIZ_VALIDATION_NUM_CTX = int(os.getenv("QUIZ_VALIDATION_NUM_CTX", "4096"))
QUIZ_VALIDATION_ATTEMPTS = int(os.getenv("QUIZ_VALIDATION_ATTEMPTS", "2"))
QUIZ_QUALITY_RETRY_LIMIT = int(os.getenv("QUIZ_QUALITY_RETRY_LIMIT", "1"))
QUIZ_GENERATION_RETRY_LIMIT = int(os.getenv("QUIZ_GENERATION_RETRY_LIMIT", "3"))

# Flashcard generation: bounded retry (never infinite) that also covers transient model failures
# such as an Ollama "token repeat limit reached" abort or a malformed/truncated JSON reply, not
# just the pre-existing "missing topic coverage" retry. FLASHCARD_MAX_CARDS_PER_TOPIC caps both the
# prompt's requested count and the parsed output per topic, which shortens generation and lowers
# the odds of a repetition-prone model (e.g. Gemma 3) looping into that same abort.
FLASHCARD_GENERATION_RETRY_LIMIT = int(os.getenv("FLASHCARD_GENERATION_RETRY_LIMIT", "2"))
FLASHCARD_MAX_CARDS_PER_TOPIC = int(os.getenv("FLASHCARD_MAX_CARDS_PER_TOPIC", "12"))

MASTERY_DIFFICULTY_WEIGHTS = {
    "easy": float(os.getenv("MASTERY_EASY_WEIGHT", "1.0")),
    "medium": float(os.getenv("MASTERY_MEDIUM_WEIGHT", "1.5")),
    "difficult": float(os.getenv("MASTERY_DIFFICULT_WEIGHT", "2.0")),
}
MASTERY_QUALITY_WEIGHTS = {
    "accepted": 1.0,
    "accepted_quality_warning": float(os.getenv("MASTERY_QUALITY_WARNING_WEIGHT", "0.75")),
}
MASTERY_MIN_QUESTIONS = int(os.getenv("MASTERY_MIN_QUESTIONS", "3"))
MASTERY_MIN_CONCEPT_COVERAGE = float(os.getenv("MASTERY_MIN_CONCEPT_COVERAGE", "0.6"))
MASTERY_DEVELOPING_THRESHOLD = float(os.getenv("MASTERY_DEVELOPING_THRESHOLD", "50"))
MASTERY_PROFICIENT_THRESHOLD = float(os.getenv("MASTERY_PROFICIENT_THRESHOLD", "70"))
MASTERY_MASTERED_THRESHOLD = float(os.getenv("MASTERY_MASTERED_THRESHOLD", "85"))

AUTO_QUIZ_MAX_PER_TOPIC = int(os.getenv("AUTO_QUIZ_MAX_PER_TOPIC", "8"))
AUTO_QUIZ_MAX_DOCUMENT = int(os.getenv("AUTO_QUIZ_MAX_DOCUMENT", "40"))
STRUCTURAL_EVIDENCE_MIN_OVERLAP_CHARS = int(os.getenv("STRUCTURAL_EVIDENCE_MIN_OVERLAP_CHARS", "80"))
STRUCTURAL_EVIDENCE_MIN_CHUNK_RATIO = float(os.getenv("STRUCTURAL_EVIDENCE_MIN_CHUNK_RATIO", "0.15"))
STRUCTURAL_EVIDENCE_MIN_SPAN_RATIO = float(os.getenv("STRUCTURAL_EVIDENCE_MIN_SPAN_RATIO", "0.20"))
STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS = int(os.getenv("STRUCTURAL_EVIDENCE_ONLY_CHUNK_MIN_CHARS", "32"))

# Model dùng để embedding
EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3")

# Ollama's standard environment variable. LangChain also honors this value.
OLLAMA_BASE_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

# Chunk config
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

# Số đoạn tài liệu lấy ra khi hỏi
TOP_K = 4

# Google Calendar integration (one-way: Google busy time -> Planner, confirmed sessions -> Google).
# Read at call time (never cached at import) so a missing value is reported clearly per request and
# tests can supply fake values. Secrets come only from the environment; never commit them.
GOOGLE_CALENDAR_ENV_VARS = (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_CALENDAR_REDIRECT_URI",
    "GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY",
)


def google_calendar_settings() -> dict:
    """The Google OAuth settings plus `missing`: the required variables that are not set."""
    values = {name: os.getenv(name, "").strip() for name in GOOGLE_CALENDAR_ENV_VARS}
    # Where the OAuth callback sends the browser back to (the frontend origin, e.g.
    # http://localhost:3000 in local dev). Empty = same origin as the backend (nginx on Kaggle).
    values["AI_TUTOR_FRONTEND_URL"] = os.getenv("AI_TUTOR_FRONTEND_URL", "").strip().rstrip("/")
    values["missing"] = [name for name in GOOGLE_CALENDAR_ENV_VARS if not values[name]]
    return values
