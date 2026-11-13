"""pytest: point every runtime data path at a throwaway directory before any test module imports
config, so a code path a test does not patch (for example the flashcard lookup of quiz generation)
can never read or write the developer's real data/ SQLite database, vectorstore or uploads."""

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="ai_tutor_tests_"))
os.environ.setdefault("AI_TUTOR_DATA_DIR", str(_ROOT / "data"))
os.environ.setdefault("AI_TUTOR_DATABASE_PATH", str(_ROOT / "data" / "conversations.db"))
os.environ.setdefault("AI_TUTOR_VECTORSTORE_DIR", str(_ROOT / "vectorstore"))
os.environ.setdefault("AI_TUTOR_INDEXED_FILES_PATH", str(_ROOT / "indexed_files.json"))
# No background figure processing in tests, unless a test runs it directly.
os.environ.setdefault("AI_TUTOR_VISUAL_ENRICHMENT", "0")

# Tests see config's committed defaults, never a developer's git-ignored .env.local (for example a
# local "Qwen 2.5 3B only" model setup, which made the model registry tests fail). Only that one
# file is skipped: real environment variables still apply, and tests that check overrides set their
# own (subprocess checks such as tests/test_llm_backend.py already import config without .env.local).
ENV_LOCAL_PATH = (Path(__file__).resolve().parents[1] / ".env.local").resolve()

try:
    import dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    dotenv = None

if dotenv is not None:
    _load_dotenv = dotenv.load_dotenv

    def _load_dotenv_except_env_local(dotenv_path=None, *args, **kwargs):
        if dotenv_path is not None and Path(dotenv_path).resolve() == ENV_LOCAL_PATH:
            return False
        return _load_dotenv(dotenv_path, *args, **kwargs)

    dotenv.load_dotenv = _load_dotenv_except_env_local
