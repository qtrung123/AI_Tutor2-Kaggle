"""LLM_BACKEND selection (backend/llm_backend.py): Ollama stays the default/fallback; vLLM requests go
to the local OpenAI-compatible server with the same prompts and translated options. No network:
httpx is driven through a MockTransport."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_ollama import ChatOllama as LangchainChatOllama

from backend import llm_backend, model_registry

ROOT = Path(__file__).parents[1]
QWEN_REFERENCE = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"


def config_under(env: dict) -> dict:
    """config's backend settings as a fresh process sees them under `env` (without .env.local)."""
    script = "import sys; sys.modules['dotenv'] = None; import json, config; print(json.dumps({'backend': config.LLM_BACKEND, 'models': list(config.GENERATION_MODELS), 'url': config.VLLM_BASE_URL, 'model': config.VLLM_MODEL}))"
    clean = {key: value for key, value in os.environ.items() if key not in {"LLM_BACKEND", "OLLAMA_GENERATION_MODELS", "VLLM_BASE_URL", "VLLM_MODEL"}}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env={**clean, **env}, capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


class Recorder:
    """MockTransport handler: records requests and answers like vLLM's chat completions API."""

    def __init__(self, stream_lines=None, models=("Qwen/Qwen2.5-7B-Instruct",)):
        self.requests, self.stream_lines, self.models = [], stream_lines, models

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model} for model in self.models]})
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content="\n\n".join(self.stream_lines).encode())
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        })


def mocked_httpx(recorder):
    real_client = httpx.Client
    transport = httpx.MockTransport(recorder)
    return (
        patch.object(llm_backend.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs)),
        patch.object(llm_backend.httpx, "get", lambda url, **kwargs: real_client(transport=transport).get(url, **kwargs)),
    )


class BackendSelectionTests(unittest.TestCase):
    def test_ollama_is_the_default_and_the_fallback_for_unknown_values(self):
        self.assertEqual(config_under({})["backend"], "ollama")
        self.assertEqual(config_under({"LLM_BACKEND": "something-else"})["backend"], "ollama")
        self.assertEqual(config_under({})["models"], ["qwen-2.5-7b", "gemma3-12b"])

    def test_vllm_is_selected_explicitly_and_offers_only_qwen(self):
        selected = config_under({"LLM_BACKEND": "VLLM"})
        self.assertEqual(selected["backend"], "vllm")
        self.assertEqual(selected["models"], ["qwen-2.5-7b"])
        self.assertEqual(selected["url"], "http://127.0.0.1:8001/v1")
        self.assertEqual(selected["model"], "Qwen/Qwen2.5-7B-Instruct")

    def test_startup_log_names_backend_and_model(self):
        with patch.object(llm_backend, "LLM_BACKEND", "ollama"):
            self.assertEqual(llm_backend.backend_description(), f"[llm-backend] backend=ollama model={llm_backend.CHAT_MODEL}")
        with patch.object(llm_backend, "LLM_BACKEND", "vllm"):
            self.assertEqual(llm_backend.backend_description(), "[llm-backend] backend=vllm model=Qwen/Qwen2.5-7B-Instruct")


class OllamaPathTests(unittest.TestCase):
    def test_ollama_backend_builds_the_real_langchain_chat_ollama_with_the_same_options(self):
        with patch.object(llm_backend, "LLM_BACKEND", "ollama"):
            llm = llm_backend.ChatOllama(model=QWEN_REFERENCE, temperature=0, format="json", num_ctx=32768, keep_alive=0)
        self.assertIsInstance(llm, LangchainChatOllama)
        self.assertEqual((llm.model, llm.temperature, llm.format, llm.num_ctx), (QWEN_REFERENCE, 0, "json", 32768))

    def test_generation_modules_use_the_backend_selecting_factory(self):
        from backend import (assessment_planner, flashcard_service, quiz_legacy_v2, quiz_service,
                             quiz_validation, rag_service, summary_service)
        for module in (assessment_planner, flashcard_service, quiz_legacy_v2, quiz_service, rag_service, summary_service):
            self.assertIs(module.ChatOllama, llm_backend.ChatOllama, module.__name__)
        self.assertIs(quiz_validation.validate_question_semantics.__defaults__[-1], llm_backend.ChatOllama)


class VLLMClientTests(unittest.TestCase):
    def setUp(self):
        backend = patch.object(llm_backend, "LLM_BACKEND", "vllm")
        backend.start()
        self.addCleanup(backend.stop)

    def test_invoke_posts_to_the_local_openai_compatible_server_with_translated_options(self):
        recorder = Recorder()
        client_patch, _ = mocked_httpx(recorder)
        with client_patch:
            response = llm_backend.ChatOllama(
                model=QWEN_REFERENCE, temperature=0.3, repeat_penalty=1.4, format="json",
                num_ctx=32768, num_predict=500, keep_alive=0, reasoning=False, client_kwargs={"timeout": 90},
            ).invoke("Summarize this.")
        request = recorder.requests[0]
        self.assertEqual(str(request.url), "http://127.0.0.1:8001/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer EMPTY")
        self.assertEqual(json.loads(request.content), {
            "model": "Qwen/Qwen2.5-7B-Instruct", "messages": [{"role": "user", "content": "Summarize this."}],
            "stream": False, "temperature": 0.3, "repetition_penalty": 1.4, "max_tokens": 500,
            "response_format": {"type": "json_object"},
        })
        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.response_metadata["done_reason"], "stop")
        self.assertEqual((response.response_metadata["prompt_eval_count"], response.response_metadata["eval_count"]), (12, 5))

    def test_a_json_schema_format_becomes_a_json_schema_response_format(self):
        schema = {"type": "object", "properties": {"questions": {"type": "array"}}}
        llm = llm_backend.ChatOllama(model=QWEN_REFERENCE, format=schema)
        self.assertEqual(llm.options["response_format"], {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}})

    def test_stream_yields_text_and_ends_with_ollama_style_metadata(self):
        lines = [
            'data: {"choices": [{"delta": {"content": "{\\"a\\""}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"content": ": 1}"}, "finish_reason": "length"}]}',
            'data: {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9}}',
            "data: [DONE]",
        ]
        recorder = Recorder(stream_lines=lines)
        client_patch, _ = mocked_httpx(recorder)
        with client_patch:
            chunks = list(llm_backend.ChatOllama(model=QWEN_REFERENCE, format="json").stream("prompt"))
        self.assertEqual("".join(chunk.content for chunk in chunks), '{"a": 1}')
        self.assertEqual(chunks[-1].response_metadata["done_reason"], "length")
        self.assertEqual(chunks[-1].response_metadata["eval_count"], 9)
        self.assertEqual(json.loads(recorder.requests[0].content)["stream_options"], {"include_usage": True})

    def test_models_other_than_qwen_are_refused_never_silently_served_by_qwen(self):
        with self.assertRaisesRegex(ValueError, "serves only Qwen/Qwen2.5-7B-Instruct"):
            llm_backend.ChatOllama(model="gemma3:12b-it-q4_K_M")

    def test_unsupported_options_fail_loudly(self):
        with self.assertRaises(TypeError):
            llm_backend.ChatOllama(model=QWEN_REFERENCE, mirostat=2)


class RegistryUnderVLLMTests(unittest.TestCase):
    def setUp(self):
        # The vLLM allowlist (config's default under LLM_BACKEND=vllm), independent of any .env.local.
        for target, name, value in ((llm_backend, "LLM_BACKEND", "vllm"), (model_registry, "LLM_BACKEND", "vllm"),
                                    (model_registry, "GENERATION_MODELS", ("qwen-2.5-7b",)),
                                    (model_registry, "DEFAULT_GENERATION_MODEL", "qwen-2.5-7b")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_prepare_checks_the_vllm_server_and_never_pulls_into_ollama(self):
        _, get_patch = mocked_httpx(Recorder())
        with get_patch, patch.object(model_registry.subprocess, "run") as run, patch.object(model_registry.httpx, "Client") as ollama_client:
            result = model_registry.prepare_generation_model("qwen-2.5-7b")
        self.assertTrue(result["ready"])
        self.assertNotIn("prepared", result)
        run.assert_not_called()
        ollama_client.assert_not_called()

    def test_prepare_fails_loudly_when_vllm_is_not_serving_qwen(self):
        _, get_patch = mocked_httpx(Recorder(models=()))
        with get_patch, patch.object(model_registry.subprocess, "run") as run, \
                self.assertRaisesRegex(RuntimeError, "vLLM is not serving"):
            model_registry.prepare_generation_model("qwen-2.5-7b")
        run.assert_not_called()

    def test_warm_never_loads_the_model_into_ollama(self):
        _, get_patch = mocked_httpx(Recorder())
        with get_patch, patch.object(model_registry.httpx, "Client") as ollama_client:
            result = model_registry.warm_generation_model("qwen-2.5-7b", num_ctx=8192, keep_alive="5m")
        ollama_client.assert_not_called()
        self.assertIsNone(result["ollama_load_ms"])


if __name__ == "__main__":
    unittest.main()
