"""Public model IDs mapped to allowlisted Ollama runtime references."""
import httpx
from config import CHAT_MODEL, DEFAULT_GENERATION_MODEL, GENERATION_MODELS, OLLAMA_BASE_URL

QWEN_OLLAMA_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
_BUILT_INS = {
    "qwen-2.5-7b": {"id": "qwen-2.5-7b", "label": "Qwen 2.5 7B", "ollama_model": QWEN_OLLAMA_MODEL},
}


def _registry() -> dict[str, dict]:
    registry = dict(_BUILT_INS)
    # Compatibility: an actual OLLAMA_CHAT_MODEL gets the default public ID.
    registry["qwen-2.5-7b"] = {**registry["qwen-2.5-7b"], "ollama_model": CHAT_MODEL}
    for index, configured in enumerate(GENERATION_MODELS, start=1):
        if configured in registry:
            continue
        if configured == QWEN_OLLAMA_MODEL:
            registry["qwen-2.5-7b"] = {**registry["qwen-2.5-7b"], "ollama_model": configured}
        else:
            # Runtime references may be configured without manually-created aliases.
            public_id = f"configured-model-{index}"
            registry[public_id] = {"id": public_id, "label": f"Configured model {index}", "ollama_model": configured}
    return registry


def _configured_ids() -> list[str]:
    registry = _registry()
    ids = []
    for configured in GENERATION_MODELS:
        if configured in registry:
            ids.append(configured)
        else:
            ids.extend(item["id"] for item in registry.values() if item["ollama_model"] == configured)
    return list(dict.fromkeys(ids)) or ["qwen-2.5-7b"]


def resolve_generation_model(model_id: str | None) -> str:
    selected = model_id or DEFAULT_GENERATION_MODEL
    registry = _registry()
    if selected not in _configured_ids() or selected not in registry:
        raise ValueError("model_id is not an allowed generation model.")
    return registry[selected]["ollama_model"]


def list_generation_models() -> list[dict]:
    registry = _registry()
    return [{"id": model_id, "label": registry[model_id]["label"], "default": model_id == DEFAULT_GENERATION_MODEL,
             "ready": _is_installed(registry[model_id]["ollama_model"])} for model_id in _configured_ids()]


def _is_installed(model: str) -> bool:
    try:
        with httpx.Client(timeout=3) as client:
            names = {item.get("name") for item in client.get(f"{OLLAMA_BASE_URL}/api/tags").json().get("models", [])}
        return model in names or f"{model}:latest" in names
    except httpx.HTTPError:
        return False


def prepare_generation_model(model_id: str | None) -> dict:
    model = resolve_generation_model(model_id)
    if _is_installed(model):
        return {"model_id": model_id, "status": "ready", "ready": True}
    with httpx.Client(timeout=900) as client:
        response = client.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": model, "stream": False})
        response.raise_for_status()
    return {"model_id": model_id, "status": "ready", "ready": True, "prepared": True}
