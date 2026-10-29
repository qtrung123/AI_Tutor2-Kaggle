"""The LLM prompt texts, kept as files under prompts/<feature>/ instead of inside the Python code.

A prompt file holds the exact text sent to the model. Dynamic values are `str.format` fields
({context}, {question}, ...), so a literal brace is written doubled ({{ / }}), exactly as in the
f-strings these files were extracted from. The text is used as written: no stripping, so a file
must not gain a trailing newline it did not have. Preparing the values, batching, parsing,
validation and retries stay in the calling service.
"""

from functools import lru_cache
from pathlib import Path

from config import PROMPTS_DIR

_PROMPTS_ROOT = Path(PROMPTS_DIR).resolve()


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """The text of prompts/<name>, e.g. load_prompt("summary/topic_notes.txt"); read once per process.

    `name` must be a relative .txt path inside prompts/ (an absolute path or "..", which would
    escape the directory, is refused). The path does not depend on the working directory.
    """
    path = (_PROMPTS_ROOT / name).resolve()
    if path.suffix != ".txt" or not path.is_relative_to(_PROMPTS_ROOT):
        raise ValueError(f"Invalid prompt name: {name!r}")
    # Text mode: a CRLF checkout (git autocrlf on Windows) reads back as the same \n text.
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def render_prompt(name: str, **values) -> str:
    """prompts/<name> with its {fields} filled in from `values`."""
    return load_prompt(name).format(**values)
