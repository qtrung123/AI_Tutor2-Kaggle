"""Generic model registry: public model IDs mapped to allowlisted Ollama runtime references.

Every generation surface (Study Session chat, Quiz, Summary, Flashcards) resolves its model through
`resolve_generation_model` and never touches an Ollama runtime reference directly. Adding another
future model only requires a new `ModelSpec` entry in `_BUILT_INS` below with `enabled=True` and a
real `ollama_model` reference (either an `hf.co/<repo>:<quant>` GGUF reference or a plain Ollama
library reference such as `gemma3:12b-it-q4_K_M`) - no other file needs to change.

Only Qwen (the default) is pulled and warmed at Kaggle startup. Every other model - DeepSeek, Gemma
3, GLM-4, and any future entry - is "configured" (offered in the Study Session selector, resolvable,
persisted with generated artifacts) but "lazy": it is pulled on demand, the first time it is actually
selected and used, via `prepare_generation_model`. `_pull_model` below pulls it with the same
Kaggle-safe mechanism as `pull_model()` in deployment/start_kaggle.sh: any `hf.co/*` reference is
pulled with the `ollama` CLI's `--insecure` flag first (Ollama can otherwise refuse the Hugging Face
-> CDN redirect) and falls back to a plain `ollama pull` if that fails; every other reference
(official Ollama library models) is pulled directly through the HTTP API. See
`deployment/start_kaggle.sh` for the startup/preload split.
"""
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace

import httpx
from config import CHAT_MODEL, DEFAULT_GENERATION_MODEL, GENERATION_MODELS, OLLAMA_BASE_URL


def _parse_reference(reference: str) -> tuple[str, str | None]:
    """Split an Ollama runtime reference into (short display name, quantization)."""
    reference = str(reference or "").strip()
    name, _, quantization = reference.rpartition(":")
    if not name or "/" in quantization:  # no tag ("bge-m3"), or the colon belonged to a host:port
        name, quantization = reference, ""
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r"[-_.]gguf$", "", name, flags=re.IGNORECASE)
    return name or reference, quantization or None


@dataclass(frozen=True)
class ModelSpec:
    """One generation model the registry knows about.

    `ollama_model` is the exact runtime reference sent to Ollama (`ollama pull` / `ChatOllama(model=...)`).
    `quantization` is derived from that reference rather than hand-maintained, so it can't drift from
    what actually runs. A disabled entry (e.g. an unreleased model kept as a placeholder) is never
    offered by `list_generation_models` and never resolves, even if an operator lists its id in
    OLLAMA_GENERATION_MODELS.
    """
    model_id: str
    display_name: str
    ollama_model: str
    enabled: bool = True

    @property
    def quantization(self) -> str | None:
        return _parse_reference(self.ollama_model)[1] if self.ollama_model else None


QWEN_OLLAMA_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
QWEN_3B_OLLAMA_MODEL = os.getenv(
    "OLLAMA_QWEN_3B_MODEL",
    "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M",
)
# The second chat/Quiz model compared against Qwen2.5-7B: pulled from Hugging Face by Ollama
# exactly like the Qwen model above (hf.co/<repo>:<quant>, see deployment/start_kaggle.sh).
DEEPSEEK_R1_14B_OLLAMA_MODEL = os.getenv(
    "OLLAMA_DEEPSEEK_R1_14B_MODEL",
    "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M",
)
# The Qwen3 entries are kept resolvable (but no longer offered in quiz model selection, and no
# longer configured by kaggle_run.ipynb or start_kaggle.sh) so that quizzes generated earlier keep
# a readable model name and any external deployment that still sets
# OLLAMA_GENERATION_MODELS/OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL to qwen3-4b/qwen3-8b keeps working.
QWEN3_4B_OLLAMA_MODEL = os.getenv(
    "OLLAMA_QWEN3_4B_MODEL",
    "hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M",
)
QWEN3_8B_OLLAMA_MODEL = os.getenv(
    "OLLAMA_QWEN3_8B_MODEL",
    "hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M",
)
# Configured, lazily-pulled models: never downloaded/warmed at Kaggle startup (only Qwen is). Each
# is pulled on demand, the first time it is actually selected and used - see `ensure_model_prepared`.
# Official Ollama library references (not Hugging Face GGUF), pulled exactly as published by Ollama.
GEMMA3_12B_OLLAMA_MODEL = os.getenv("OLLAMA_GEMMA3_12B_MODEL", "gemma3:12b-it-q4_K_M")
GLM4_9B_OLLAMA_MODEL = os.getenv("OLLAMA_GLM4_9B_MODEL", "glm4:9b-chat-q4_K_M")
_BUILT_INS: dict[str, ModelSpec] = {
    "qwen-2.5-7b": ModelSpec("qwen-2.5-7b", "Qwen 2.5 7B", QWEN_OLLAMA_MODEL),
    "qwen-2.5-3b": ModelSpec("qwen-2.5-3b", "Qwen 2.5 3B", QWEN_3B_OLLAMA_MODEL),
    "deepseek-r1-14b": ModelSpec("deepseek-r1-14b", "DeepSeek R1 Distill Qwen 14B", DEEPSEEK_R1_14B_OLLAMA_MODEL),
    "qwen3-4b": ModelSpec("qwen3-4b", "Qwen3 4B", QWEN3_4B_OLLAMA_MODEL),
    "qwen3-8b": ModelSpec("qwen3-8b", "Qwen3 8B", QWEN3_8B_OLLAMA_MODEL),
    "gemma3-12b": ModelSpec("gemma3-12b", "Gemma 3 12B", GEMMA3_12B_OLLAMA_MODEL),
    "glm4-9b": ModelSpec("glm4-9b", "GLM-4 9B", GLM4_9B_OLLAMA_MODEL),
}


def _registry() -> dict[str, ModelSpec]:
    registry = dict(_BUILT_INS)
    # Compatibility: an actual OLLAMA_CHAT_MODEL gets the default public ID.
    registry["qwen-2.5-7b"] = replace(registry["qwen-2.5-7b"], ollama_model=CHAT_MODEL)
    for index, configured in enumerate(GENERATION_MODELS, start=1):
        if configured in registry:
            continue
        if configured == QWEN_OLLAMA_MODEL:
            registry["qwen-2.5-7b"] = replace(registry["qwen-2.5-7b"], ollama_model=configured)
        else:
            # Runtime references may be configured without manually-created aliases.
            public_id = f"configured-model-{index}"
            registry[public_id] = ModelSpec(public_id, f"Configured model {index}", configured)
    return registry


def _configured_ids() -> list[str]:
    registry = _registry()
    ids = []
    for configured in GENERATION_MODELS:
        if configured in registry and registry[configured].enabled:
            ids.append(configured)
        else:
            ids.extend(
                spec.model_id for spec in registry.values()
                if spec.enabled and spec.ollama_model and spec.ollama_model == configured
            )
    return list(dict.fromkeys(ids)) or ["qwen-2.5-7b"]


def resolve_generation_model(model_id: str | None) -> str:
    selected = model_id or DEFAULT_GENERATION_MODEL
    registry = _registry()
    spec = registry.get(selected)
    if spec is None or not spec.enabled or not spec.ollama_model or selected not in _configured_ids():
        raise ValueError("model_id is not an allowed generation model.")
    return spec.ollama_model


def list_generation_models() -> list[dict]:
    registry = _registry()
    return [{"id": model_id, "label": registry[model_id].display_name, "default": model_id == DEFAULT_GENERATION_MODEL,
             "ready": _is_installed(registry[model_id].ollama_model)} for model_id in _configured_ids()]


def describe_generation_model(ollama_model: str) -> dict:
    """Public description of the model that ACTUALLY ran a generation, from its runtime reference.

    `ollama_model` is the reference the backend passed to Ollama (what resolve_generation_model
    returned), so the name comes from the model used, never from anything the client typed. The
    runtime reference itself stays private: "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
    becomes {"model_id": "qwen-2.5-7b", "name": "Qwen2.5-7B-Instruct", "quantization": "Q4_K_M"}.
    `model_id` is None for a runtime reference that has no registry entry.
    """
    reference = str(ollama_model or "").strip()
    name, quantization = _parse_reference(reference)
    public_id = next(
        (spec.model_id for spec in _registry().values() if spec.ollama_model and spec.ollama_model == reference),
        None,
    )
    return {"model_id": public_id, "name": name or reference, "quantization": quantization}


def _is_installed(model: str) -> bool:
    try:
        with httpx.Client(timeout=3) as client:
            names = {item.get("name") for item in client.get(f"{OLLAMA_BASE_URL}/api/tags").json().get("models", [])}
        return model in names or f"{model}:latest" in names
    except httpx.HTTPError:
        return False


def _run_ollama_pull_cli(*args: str) -> None:
    """Run `ollama pull <args>` as an argv list (never shell=True) and raise on failure.

    Raises `subprocess.SubprocessError` (a non-zero exit or a timeout) or `OSError` (the `ollama`
    binary is missing) - never caught here to substitute another model.
    """
    print(f"[model-prepare] ollama pull {' '.join(args)}")
    subprocess.run(["ollama", "pull", *args], check=True, capture_output=True, text=True, timeout=900)


def _pull_model(model: str, client: httpx.Client) -> None:
    """Pull `model`, generically: the reference shape alone decides the mechanism, so no model gets
    model-specific handling (DeepSeek included).

    A `hf.co/*` reference goes through the exact same Kaggle-safe path as `pull_model()` in
    deployment/start_kaggle.sh: on Kaggle's Ollama 0.34.2, the HTTP API's `"insecure": true` is not
    equivalent to the CLI's `--insecure` flag (Ollama can still refuse the Hugging Face -> CDN
    redirect, "blocked redirect to a different host"), so the real `ollama` CLI is used - `ollama
    pull --insecure <model>` first and, only if that fails, `ollama pull <model>` once more, with no
    `shell=True`. Any other reference (an official Ollama library model, e.g. `gemma3:12b-it-q4_K_M`)
    keeps using the plain HTTP API pull - it never needs `--insecure` and never hits a Hugging Face
    redirect at all.

    Raises on a failed pull (the last attempt's error) - callers never catch that to fall back to
    another model; the caller's request simply fails, loudly.
    """
    if model.startswith("hf.co/"):
        try:
            _run_ollama_pull_cli("--insecure", model)
        except (subprocess.SubprocessError, OSError):
            _run_ollama_pull_cli(model)  # unguarded: this attempt's failure propagates as-is
        return
    response = client.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": model, "stream": False})
    response.raise_for_status()


def _ensure_installed(model: str) -> bool:
    """Pull `model` into the Ollama cache this process talks to if it isn't already there.

    Returns True if a pull actually happened. Raises `httpx.HTTPError` on a failed pull - callers
    never catch that to fall back to another model; the caller's request simply fails, loudly.
    """
    if _is_installed(model):
        return False
    with httpx.Client(timeout=900) as client:
        _pull_model(model, client)
    return True


def prepare_generation_model(model_id: str | None) -> dict:
    """Ensure the resolved model is pulled into the Ollama cache this process talks to.

    Reused as the single "make sure this model is ready" choke point: the frontend calls it (via
    `/api/models/{id}/prepare`) the moment the Study Session selector changes, and every generation
    route in backend/main.py (Quiz, Summary, Flashcards, chat) also calls it right before generating,
    so a lazily-pulled model - anything but the Kaggle-startup default - is fetched before its first
    real use instead of failing with "model not found". Never falls back to another model: a failed
    pull raises and the caller's request fails.

    Preparation timing (`model_prepare_ms`, always present in the result) is deliberately kept
    separate from generation timing: this function runs, and returns, before any generation-side
    instrumentation (e.g. backend/quiz_diagnostics.py's total_ms) starts its clock, so pull/load time
    is never counted as generation time and generation time never hides a pull inside it. This is one
    combined measurement (install-check plus pull, when needed) - Ollama does not expose "download"
    vs. "load into memory" as separate timings, so those are not faked as distinct numbers here.

    This never runs locally on its own: it only ever talks to the Ollama a Kaggle-hosted backend runs
    against. Local tests must patch `httpx.Client`/`subprocess.run` (see tests/test_model_registry.py)
    so no real pull ever happens off Kaggle.
    """
    started = time.perf_counter()
    model = resolve_generation_model(model_id)
    prepared = _ensure_installed(model)
    prepare_ms = round((time.perf_counter() - started) * 1000)
    print(f"[model-prepare] model_id={model_id} prepared={prepared} model_prepare_ms={prepare_ms}")
    return {
        "model_id": model_id, "status": "ready", "ready": True, "model_prepare_ms": prepare_ms,
        **({"prepared": True} if prepared else {}),
    }
