"""Chat-generation backend selection: Ollama (default) or a local vLLM OpenAI-compatible server.

Every generation module imports `ChatOllama` from here instead of from langchain_ollama, so each
call site keeps its prompts, sampling options, parsing, validation and retries exactly as they are.

- LLM_BACKEND=ollama (default, and the fallback for any unknown value): `ChatOllama(...)` builds the
  real langchain_ollama.ChatOllama - nothing changes.
- LLM_BACKEND=vllm: `ChatOllama(...)` builds a `VLLMChatModel`, which sends the same request to
  vLLM's /v1/chat/completions (VLLM_BASE_URL, default http://127.0.0.1:8001/v1). The Ollama-style
  options are translated (num_predict -> max_tokens, repeat_penalty -> repetition_penalty,
  format "json"/<JSON schema> -> response_format); num_ctx, keep_alive and reasoning do not apply
  to a server that loaded its model once at startup with a fixed --max-model-len. Responses carry
  Ollama-style metadata keys (done_reason, prompt_eval_count, eval_count) so the existing timing
  and diagnostics code keeps working; vLLM reports no per-phase durations, so those stay absent.

Only the Qwen 2.5 7B runtime reference maps to vLLM (VLLM_MODEL); any other model is refused rather
than silently served by a different model. Embeddings always stay on Ollama.
"""
import json

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_ollama import ChatOllama as _OllamaChatModel

from config import CHAT_MODEL, LLM_BACKEND, VLLM_API_KEY, VLLM_BASE_URL, VLLM_MODEL

_QWEN_OLLAMA_REFERENCES = frozenset({CHAT_MODEL, "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"})
# Ollama option -> OpenAI/vLLM request field.
_SAMPLING_OPTIONS = {
    "temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "seed": "seed",
    "num_predict": "max_tokens", "repeat_penalty": "repetition_penalty",
}
# Ollama-only options with no per-request meaning on vLLM (context size and residency are fixed at
# server startup; Qwen 2.5 has no reasoning mode).
_IGNORED_OLLAMA_OPTIONS = frozenset({"num_ctx", "keep_alive", "reasoning"})
_ROLES = {"human": "user", "user": "user", "ai": "assistant", "assistant": "assistant", "system": "system"}


def backend_description() -> str:
    model = VLLM_MODEL if LLM_BACKEND == "vllm" else CHAT_MODEL
    return f"[llm-backend] backend={LLM_BACKEND} model={model}"


def vllm_model_for(model: str) -> str:
    """The vLLM served-model name for an Ollama runtime reference; ValueError for anything else."""
    if model in _QWEN_OLLAMA_REFERENCES or model == VLLM_MODEL:
        return VLLM_MODEL
    raise ValueError(f"LLM_BACKEND=vllm serves only {VLLM_MODEL}; model '{model}' is not available on vLLM.")


def vllm_model_ready() -> bool:
    """True when the vLLM server answers /v1/models and lists VLLM_MODEL."""
    try:
        response = httpx.get(f"{VLLM_BASE_URL}/models", headers=_headers(), timeout=3)
        response.raise_for_status()
        return any(item.get("id") == VLLM_MODEL for item in response.json().get("data", []))
    except (httpx.HTTPError, ValueError):
        return False


def _headers() -> dict:
    return {"Authorization": f"Bearer {VLLM_API_KEY}"}


def _messages(prompt) -> list[dict]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    if hasattr(prompt, "to_messages"):
        prompt = prompt.to_messages()
    messages = []
    for message in prompt:
        role, content = message if isinstance(message, tuple) else (message.type, message.content)
        messages.append({"role": _ROLES[role], "content": content})
    return messages


class VLLMChatModel:
    """The `invoke`/`stream` surface the generation modules use, backed by vLLM's chat API."""

    def __init__(self, model: str, format=None, client_kwargs: dict | None = None, **options):
        unknown = set(options) - set(_SAMPLING_OPTIONS) - _IGNORED_OLLAMA_OPTIONS
        if unknown:
            raise TypeError(f"Unsupported option(s) for the vLLM backend: {', '.join(sorted(unknown))}")
        self.model = vllm_model_for(model)
        self.options = {target: options[source] for source, target in _SAMPLING_OPTIONS.items() if options.get(source) is not None}
        if format == "json":
            self.options["response_format"] = {"type": "json_object"}
        elif isinstance(format, dict):
            self.options["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": format}}
        elif format is not None:
            raise TypeError(f"Unsupported format for the vLLM backend: {format!r}")
        # Same as ChatOllama: no client timeout unless the call site sets one.
        self.timeout = (client_kwargs or {}).get("timeout")

    def _body(self, prompt, stream: bool) -> dict:
        body = {"model": self.model, "messages": _messages(prompt), "stream": stream, **self.options}
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _metadata(self, finish_reason, usage) -> dict:
        metadata = {"model_name": self.model, "backend": "vllm", "done_reason": finish_reason}
        if usage:
            metadata["prompt_eval_count"] = usage.get("prompt_tokens")
            metadata["eval_count"] = usage.get("completion_tokens")
        return metadata

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_error:
            response.read()
            raise RuntimeError(f"vLLM request failed ({response.status_code}): {response.text[:500]}")

    def invoke(self, prompt) -> AIMessage:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{VLLM_BASE_URL}/chat/completions", headers=_headers(), json=self._body(prompt, False))
            self._raise_for_status(response)
            data = response.json()
        choice = data["choices"][0]
        return AIMessage(
            content=choice["message"].get("content") or "",
            response_metadata=self._metadata(choice.get("finish_reason"), data.get("usage")),
        )

    def stream(self, prompt):
        finish_reason, usage = None, None
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", f"{VLLM_BASE_URL}/chat/completions", headers=_headers(), json=self._body(prompt, True)) as response:
                self._raise_for_status(response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    usage = data.get("usage") or usage
                    for choice in data.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield AIMessageChunk(content=content)
        # Like ChatOllama.stream, the last chunk carries the response metadata.
        yield AIMessageChunk(content="", response_metadata=self._metadata(finish_reason, usage))


def ChatOllama(**kwargs):  # noqa: N802 - keeps the name every call site (and test patch) already uses
    """Build the chat model for the selected backend (see the module docstring)."""
    if LLM_BACKEND == "vllm":
        return VLLMChatModel(**kwargs)
    return _OllamaChatModel(**kwargs)
