import unittest
from pathlib import Path
from unittest.mock import patch

import config
from backend import model_registry


ROOT = Path(__file__).parents[1]


class ModelRegistryTests(unittest.TestCase):
    def test_qwen_public_id_resolves_to_real_runtime_reference(self):
        self.assertEqual(
            model_registry.resolve_generation_model("qwen-2.5-7b"),
            "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        )

    def test_local_qwen_3b_public_id_resolves_to_installed_runtime_reference(self):
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-3b",)):
            self.assertEqual(
                model_registry.resolve_generation_model("qwen-2.5-3b"),
                "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M",
            )

    def test_qwen3_4b_public_id_resolves_to_exact_runtime_reference(self):
        self.assertEqual(
            model_registry.resolve_generation_model("qwen3-4b"),
            "hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M",
        )

    def test_environment_configured_allowlist_accepts_both_public_ids(self):
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "qwen3-4b")):
            self.assertEqual(
                model_registry.resolve_generation_model("qwen-2.5-7b"),
                "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
            )
            self.assertEqual(
                model_registry.resolve_generation_model("qwen3-4b"),
                "hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M",
            )

    def test_kaggle_startup_repairs_an_older_environment_allowlist(self):
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")

        self.assertIn("for required_model_id in qwen-2.5-7b qwen3-4b", startup)
        self.assertIn('case ",$OLLAMA_GENERATION_MODELS,"', startup)
        self.assertIn('OLLAMA_GENERATION_MODELS = \\"qwen-2.5-7b,qwen3-4b\\"', notebook)

    def test_feature_defaults_keep_chat_summary_on_7b_and_quiz_on_4b(self):
        self.assertEqual(config.DEFAULT_GENERATION_MODEL, "qwen-2.5-7b")
        self.assertEqual(config.QUIZ_DEFAULT_GENERATION_MODEL, "qwen3-4b")
        self.assertEqual(
            model_registry.resolve_generation_model(config.DEFAULT_GENERATION_MODEL),
            "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M",
        )
        self.assertEqual(
            model_registry.resolve_generation_model(config.QUIZ_DEFAULT_GENERATION_MODEL),
            "hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M",
        )

    def test_quiz_uses_backend_default_while_chat_and_summary_keep_selected_default(self):
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        main = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        summary = (ROOT / "backend" / "summary_service.py").read_text(encoding="utf-8")
        rag = (ROOT / "backend" / "rag_service.py").read_text(encoding="utf-8")

        self.assertIn('model_id: quizModelSelect?.value || "qwen3-4b"', frontend)
        self.assertIn("model_id: request.model_id", frontend)
        self.assertIn("body: JSON.stringify(request)", frontend)
        self.assertGreaterEqual(main.count("or QUIZ_DEFAULT_GENERATION_MODEL"), 2)
        self.assertIn("model_id or DEFAULT_GENERATION_MODEL", summary)
        self.assertIn("resolve_generation_model(model_id)", rag)

    def test_create_quiz_model_selector_uses_public_ids_and_fast_default(self):
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

        fast = 'new Option("Qwen3-4B (Fast)", "qwen3-4b", true, true)'
        quality = 'new Option("Qwen2.5-7B (Higher Quality)", "qwen-2.5-7b")'
        self.assertIn(fast, frontend)
        self.assertIn(quality, frontend)
        self.assertLess(frontend.index(fast), frontend.index(quality))
        self.assertNotIn("hf.co/", frontend)

    def test_quiz_calls_disable_reasoning_and_kaggle_prepares_without_residency(self):
        quiz = (ROOT / "backend" / "quiz_service.py").read_text(encoding="utf-8")
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")

        self.assertEqual(quiz.count("reasoning=False"), 5)
        self.assertIn('QUIZ_GENERATION_KEEP_ALIVE = "5m"', quiz)
        self.assertEqual(quiz.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE"), 5)
        self.assertIn('OLLAMA_QWEN3_4B_MODEL="${OLLAMA_QWEN3_4B_MODEL:-hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M}"', startup)
        self.assertIn('"think": False', startup)
        self.assertIn('"keep_alive": 0', startup)
        self.assertIn('OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL = \\"qwen3-4b\\"', notebook)

    @patch("backend.model_registry._is_installed", return_value=True)
    def test_local_qwen_3b_model_list_exposes_only_public_data(self, _installed):
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-3b",)), patch.object(
            model_registry, "DEFAULT_GENERATION_MODEL", "qwen-2.5-3b"
        ):
            model = model_registry.list_generation_models()[0]
        self.assertEqual(model, {"id": "qwen-2.5-3b", "label": "Qwen 2.5 3B", "default": True, "ready": True})

    @patch("backend.model_registry._is_installed", return_value=True)
    def test_models_return_only_public_data(self, _installed):
        model = model_registry.list_generation_models()[0]
        self.assertEqual(model["id"], "qwen-2.5-7b")
        self.assertEqual(model["label"], "Qwen 2.5 7B")
        self.assertNotIn("ollama_model", model)

    def test_unknown_public_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not an allowed"):
            model_registry.resolve_generation_model("not-a-model")

    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_prepare_pulls_only_resolved_selected_runtime_model(self, _installed, client_type):
        client = client_type.return_value.__enter__.return_value
        response = client.post.return_value
        response.raise_for_status.return_value = None
        model_registry.prepare_generation_model("qwen-2.5-7b")
        client.post.assert_called_once_with(
            "http://127.0.0.1:11434/api/pull",
            json={"name": "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M", "stream": False},
        )
