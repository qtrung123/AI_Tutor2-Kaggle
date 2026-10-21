"""Qwen residency (CHAT-PERF-1): Chat and the quiz explanation use the live Quiz context window and
one shared keep_alive, the Kaggle warmup loads Qwen with exactly those values and keeps it, and
model timings are logged only on request. Prompts, retrieval and answers are unchanged."""

import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from backend import flashcard_service, llm_backend, perf_timing, quiz_service, rag_service
from backend.quiz_units import QUIZ_NUM_CTX
from config import CHAT_MAX_TOKENS, CHAT_NUM_CTX, GENERATION_KEEP_ALIVE

ROOT = Path(__file__).parents[1]
STARTUP = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
QWEN = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
METADATA = {"load_duration": 2_500_000, "prompt_eval_duration": 640_000_000, "eval_duration": 8_000_000_000,
            "total_duration": 8_700_000_000, "prompt_eval_count": 2150, "eval_count": 240, "done_reason": "stop"}


def config_defaults() -> dict:
    """config's residency settings as a fresh process sees them without any override."""
    script = ("import sys; sys.modules['dotenv'] = None; import json, config; "
              "print(json.dumps({'num_ctx': config.CHAT_NUM_CTX, 'keep_alive': config.GENERATION_KEEP_ALIVE}))")
    clean = {k: v for k, v in os.environ.items() if k not in {"AI_TUTOR_CHAT_NUM_CTX", "AI_TUTOR_GENERATION_KEEP_ALIVE"}}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=clean, capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


class FakeChat:
    calls = []

    def __init__(self, **options):
        self.options = options

    def invoke(self, prompt):
        FakeChat.calls.append((self.options, prompt))
        return AIMessage(content="Paging maps virtual pages to frames (lecture.pdf: 3).", response_metadata=METADATA)


def run_chat(model_reference=QWEN):
    """One Chat answer through rag_service with storage, retrieval and the model faked."""
    FakeChat.calls = []
    doc = Document(page_content="Paging maps virtual pages to physical frames.",
                   metadata={"source": "lecture.pdf", "page": 3, "owner_id": "u", "document_id": "lecture.pdf"})
    conversation = {"title": "Paging", "document_id": "lecture.pdf", "messages": []}
    with patch.object(rag_service, "ChatOllama", FakeChat),             patch.object(rag_service, "resolve_generation_model", return_value=model_reference),             patch.object(rag_service, "get_conversation", return_value=conversation),             patch.object(rag_service, "add_message", side_effect=lambda *args: {"role": args[2]}),             patch.object(rag_service, "_retrieve_conversation_docs", return_value=[doc]):
        result = rag_service.answer_conversation_message("u", "c1", "What is paging?", "qwen-2.5-7b")
    return result, doc


class ModelConfigurationTests(unittest.TestCase):
    def test_defaults_line_up_with_the_live_quiz_and_flashcards(self):
        self.assertEqual(config_defaults(), {"num_ctx": 16384, "keep_alive": "30m"})
        self.assertEqual(CHAT_NUM_CTX, QUIZ_NUM_CTX)
        self.assertEqual(CHAT_NUM_CTX, flashcard_service._NUM_CTX_TIERS[0])
        self.assertEqual(quiz_service.QUIZ_GENERATION_KEEP_ALIVE, GENERATION_KEEP_ALIVE)

    def test_chat_sends_the_shared_context_window_and_residency(self):
        result, doc = run_chat()
        [(options, prompt)] = FakeChat.calls
        self.assertEqual(options, {"model": QWEN, "temperature": 0, "num_ctx": CHAT_NUM_CTX,
                                   "num_predict": CHAT_MAX_TOKENS, "keep_alive": GENERATION_KEEP_ALIVE})
        # The RAG prompt, the answer and its citations are exactly what they were.
        expected = rag_service.load_prompt_template().format(
            context=rag_service.format_docs([doc]), conversation_history="No previous messages.",
            question="What is paging?")
        self.assertEqual(prompt, expected)
        self.assertEqual(result["answer"], "Paging maps virtual pages to frames (lecture.pdf: 3).")
        self.assertEqual((result["grounding_status"], len(result["citations"])), ("supported", 1))

    def test_the_selected_model_is_still_the_one_that_answers(self):
        run_chat("gemma3:12b-it-q4_K_M")
        self.assertEqual(FakeChat.calls[0][0]["model"], "gemma3:12b-it-q4_K_M")

    def test_quiz_explanation_uses_the_same_window_and_residency(self):
        FakeChat.calls = []
        store = type("Store", (), {"similarity_search": lambda self, *a, **k: [Document(page_content="x", metadata={})]})()
        with patch.object(rag_service, "ChatOllama", FakeChat), patch.object(rag_service, "load_vectorstore", return_value=store):
            rag_service.explain_quiz_answer("u", "doc", "Q?", ["A. one", "B. two"], "A")
        options = FakeChat.calls[0][0]
        self.assertEqual((options["num_ctx"], options["keep_alive"], options["num_predict"]),
                         (CHAT_NUM_CTX, GENERATION_KEEP_ALIVE, 220))

    def test_vllm_backend_accepts_and_drops_the_ollama_only_options(self):
        with patch.object(llm_backend, "LLM_BACKEND", "vllm"):
            model = llm_backend.ChatOllama(model=QWEN, temperature=0, num_ctx=CHAT_NUM_CTX, keep_alive=GENERATION_KEEP_ALIVE)
        self.assertIsInstance(model, llm_backend.VLLMChatModel)
        self.assertEqual(model.options, {"temperature": 0})


class TimingLogTests(unittest.TestCase):
    def test_timings_come_from_ollama_metadata(self):
        timing = perf_timing.llm_timing(AIMessage(content="", response_metadata=METADATA), 9.12)
        self.assertEqual(timing, {"wall_ms": 9120.0, "load_ms": 2.5, "prompt_eval_ms": 640.0, "eval_ms": 8000.0,
                                  "ollama_total_ms": 8700.0, "prompt_tokens": 2150, "output_tokens": 240,
                                  "output_tokens_per_s": 30.0})
        # vLLM: token counts only, no per-phase durations.
        vllm = perf_timing.llm_timing(AIMessage(content="", response_metadata={"prompt_eval_count": 10, "eval_count": 5}), 1.0)
        self.assertEqual(vllm, {"wall_ms": 1000.0, "prompt_tokens": 10, "output_tokens": 5})
        self.assertEqual(perf_timing.llm_timing(object(), 0.5), {"wall_ms": 500.0})

    def log(self, flag):
        out = io.StringIO()
        with patch.dict(os.environ, {"AI_TUTOR_PERF_TIMING": flag}), redirect_stdout(out):
            perf_timing.log_llm_call("chat", AIMessage(content="secret answer text", response_metadata=METADATA), 9.0)
        return out.getvalue()

    def test_logging_is_opt_in_and_never_prints_content(self):
        self.assertEqual(self.log(""), "")
        line = self.log("1")
        self.assertTrue(line.startswith("[llm] call=chat wall_ms=9000.0 load_ms=2.5 "), line)
        self.assertIn("prompt_tokens=2150 output_tokens=240", line)
        self.assertIn("done_reason=stop", line)
        self.assertNotIn("secret", line)

    def test_chat_logs_one_line_per_call_when_enabled(self):
        out = io.StringIO()
        with patch.dict(os.environ, {"AI_TUTOR_PERF_TIMING": "1"}), redirect_stdout(out):
            run_chat()
        self.assertEqual(len([line for line in out.getvalue().splitlines() if line.startswith("[llm] call=chat ")]), 1)


class KaggleStartupTests(unittest.TestCase):
    def warmup(self):
        return STARTUP[STARTUP.index("start_model_warmup() {"):STARTUP.index("assert_health_models() {")]

    def test_startup_defaults_mirror_config(self):
        defaults = config_defaults()
        self.assertIn(f'export AI_TUTOR_CHAT_NUM_CTX="${{AI_TUTOR_CHAT_NUM_CTX:-{defaults["num_ctx"]}}}"', STARTUP)
        self.assertIn(f'export AI_TUTOR_GENERATION_KEEP_ALIVE="${{AI_TUTOR_GENERATION_KEEP_ALIVE:-{defaults["keep_alive"]}}}"',
                      STARTUP)

    def test_ollama_server_default_residency_is_set_before_ollama_starts(self):
        export = STARTUP.index('export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-$AI_TUTOR_GENERATION_KEEP_ALIVE}"')
        self.assertLess(export, STARTUP.index("ollama serve"))

    def test_warmup_loads_qwen_with_the_chat_window_and_keeps_it(self):
        body = self.warmup()
        [qwen] = [line for line in body.splitlines() if 'os.environ["OLLAMA_CHAT_MODEL"]' in line]
        code = re.search(r"python -c '(.*?)'\)\"", qwen).group(1)
        env = {**os.environ, "OLLAMA_CHAT_MODEL": QWEN, "AI_TUTOR_GENERATION_KEEP_ALIVE": "30m",
               "AI_TUTOR_CHAT_NUM_CTX": "16384"}
        # The exact JSON body curl sends, produced by the script's own one-liner.
        request = json.loads(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                            check=True).stdout)
        self.assertEqual(request, {"model": QWEN, "prompt": "Reply with OK.", "stream": False, "keep_alive": "30m",
                                   "options": {"num_ctx": 16384}})
        self.assertNotIn('"keep_alive": 0', body)
        # The marker includes the settings, so changing them re-warms within the same runtime.
        key_line = body[body.index('key="$('):body.index("sha256sum")]
        self.assertIn("$AI_TUTOR_CHAT_NUM_CTX", key_line)
        self.assertIn("$AI_TUTOR_GENERATION_KEEP_ALIVE", key_line)
        self.assertIn("nvidia-smi", body)   # VRAM evidence in the startup log

    def test_vllm_still_skips_the_ollama_chat_warmup(self):
        body = self.warmup()
        vllm = body[body.index('if [[ "$LLM_BACKEND" == "vllm" ]]; then'):body.index("  else")]
        self.assertIn("skipping the Ollama chat warmup", vllm)
        self.assertNotIn("api/generate", vllm)


if __name__ == "__main__":
    unittest.main()
