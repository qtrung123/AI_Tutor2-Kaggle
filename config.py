from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data" 
VECTORSTORE_DIR = BASE_DIR / "vectorstore"
PROMPT_PATH = BASE_DIR / "prompts" / "rag_prompt.txt"
QUIZ_PROMPT_PATH = BASE_DIR / "prompts" / "quiz_prompt.txt"
INDEXED_FILES_PATH = BASE_DIR / "indexed_files.json"
GENERATED_QUIZZES_PATH = DATA_DIR / "generated_quizzes.json"
QUIZ_ATTEMPTS_PATH = DATA_DIR / "quiz_attempts.json"
QUIZ_EXPLANATIONS_PATH = DATA_DIR / "quiz_explanations.json"
CONVERSATIONS_DB_PATH = DATA_DIR / "conversations.db"

COLLECTION_NAME = "study_documents"

# Model dùng để trả lời
CHAT_MODEL = "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M"

# Model dùng để embedding
EMBEDDING_MODEL = "bge-m3"

# Chunk config
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

# Số đoạn tài liệu lấy ra khi hỏi
TOP_K = 4
