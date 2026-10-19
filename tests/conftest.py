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
