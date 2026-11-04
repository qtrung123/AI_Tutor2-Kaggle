"""Describe a figure with a local vision model through Ollama's /api/chat.

Always Ollama, whichever chat backend is selected (vLLM serves text only), and always the HTTP API,
because LangChain's chat model has no use here beyond this one request. The model is unloaded soon
after a burst of figures, so it does not keep GPU memory from Chat and Quiz.
"""
import base64
import functools
import io
import shutil
import subprocess
import threading

import httpx

from backend.prompt_loader import render_prompt
from config import (
    OLLAMA_BASE_URL,
    VISION_AUTO_PULL,
    VISION_KEEP_ALIVE,
    VISION_MAX_IMAGE_SIDE,
    VISION_MODE,
    VISION_MODEL,
    VISION_NUM_CTX,
    VISION_NUM_PREDICT,
    VISION_TIMEOUT_SECONDS,
)

PROMPT_VERSION = 1
MIN_DESCRIPTION_CHARS = 25
_NOTHING_REPLIES = ("none", "none.", "n/a", "no figure")
_pull_lock = threading.Lock()


class VisionUnavailable(RuntimeError):
    """Ollama or the vision model cannot be reached; retrying right away will not help."""


@functools.lru_cache(maxsize=1)
def _has_gpu() -> bool:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False
    try:
        return bool(subprocess.run([executable, "-L"], capture_output=True, text=True, timeout=5, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def enabled() -> bool:
    """Whether figures may be sent to the vision model (AI_TUTOR_VISION: auto, on or off)."""
    if VISION_MODE in {"on", "1", "true", "yes"}:
        return True
    if VISION_MODE == "auto":
        return _has_gpu()
    return False


def cache_key(image_digest: str) -> str:
    return f"vision|{VISION_MODEL}|{PROMPT_VERSION}|{image_digest}"


def _installed(client: httpx.Client) -> bool:
    response = client.post(f"{OLLAMA_BASE_URL}/api/show", json={"name": VISION_MODEL})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def model_available() -> bool:
    """Whether the vision model can be used, downloading it first only with VISION_AUTO_PULL."""
    try:
        with httpx.Client(timeout=10) as client:
            if _installed(client):
                return True
        if not VISION_AUTO_PULL:
            return False
        with _pull_lock, httpx.Client(timeout=1800) as client:
            if _installed(client):
                return True
            print("[vision] downloading the vision model")
            client.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": VISION_MODEL, "stream": False}).raise_for_status()
        return True
    except (httpx.HTTPError, ValueError) as error:
        print(f"[vision] model unavailable ({type(error).__name__})")
        return False


def _encode(image) -> str:
    copy = image.copy()
    copy.thumbnail((VISION_MAX_IMAGE_SIDE, VISION_MAX_IMAGE_SIDE))
    buffer = io.BytesIO()
    copy.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def clean_description(text: str) -> str:
    """The model's answer, or "" when it reports nothing worth indexing or the answer is too short."""
    text = (text or "").strip()
    if text.lower() in _NOTHING_REPLIES or len(text) < MIN_DESCRIPTION_CHARS:
        return ""
    return text


def describe_image(image, page_text: str = "", ocr_text: str = "") -> str:
    """A searchable description of the figure, or "" when it has nothing useful.

    Raises VisionUnavailable when Ollama cannot answer, so the caller can retry later."""
    prompt = render_prompt(
        "vision/figure_description.txt",
        page_text=" ".join(page_text.split())[:500] or "(none)",
        ocr_text=" ".join(ocr_text.split())[:500] or "(none)",
    )
    body = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [_encode(image)]}],
        "stream": False,
        "keep_alive": VISION_KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": VISION_NUM_PREDICT, "num_ctx": VISION_NUM_CTX},
    }
    try:
        with httpx.Client(timeout=VISION_TIMEOUT_SECONDS) as client:
            response = client.post(f"{OLLAMA_BASE_URL}/api/chat", json=body)
            response.raise_for_status()
            return clean_description(response.json().get("message", {}).get("content", ""))
    except (httpx.HTTPError, ValueError) as error:
        raise VisionUnavailable(type(error).__name__) from error
