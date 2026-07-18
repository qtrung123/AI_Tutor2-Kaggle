import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


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

# Read once by the SQLite migration so existing local quiz data is preserved.
LEGACY_GENERATED_QUIZZES_PATH = DATA_DIR / "generated_quizzes.json"
LEGACY_QUIZ_ATTEMPTS_PATH = DATA_DIR / "quiz_attempts.json"
LEGACY_QUIZ_EXPLANATIONS_PATH = DATA_DIR / "quiz_explanations.json"

COLLECTION_NAME = "study_documents"

# Model dùng để trả lời
CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M")

# Model dùng để embedding
EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3")

# Ollama's standard environment variable. LangChain also honors this value.
OLLAMA_BASE_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

# Chunk config
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

# Số đoạn tài liệu lấy ra khi hỏi
TOP_K = 4
