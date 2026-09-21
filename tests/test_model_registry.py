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

    def test_qwen3_4b_public_id_still_resolves_when_explicitly_configured(self):
        # qwen3-4b is no longer offered in quiz model selection or the default allowlist, but
        # stays resolvable so an existing deployment (e.g. an unmodified Kaggle notebook) that
        # still explicitly configures it does not break.
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "qwen3-4b")):
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

        self.assertIn("for required_model_id in qwen-2.5-7b deepseek-r1-14b", startup)
        self.assertIn('case ",$OLLAMA_GENERATION_MODELS,"', startup)
        self.assertIn('OLLAMA_GENERATION_MODELS = \\"qwen-2.5-7b,deepseek-r1-14b\\"', notebook)
        self.assertNotIn("qwen3-4b", startup)
        self.assertNotIn("qwen3-4b", notebook)

    def test_deepseek_public_id_resolves_to_exact_runtime_reference(self):
        self.assertEqual(model_registry.resolve_generation_model("deepseek-r1-14b"), "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M")

    def test_qwen3_8b_stays_resolvable_only_when_explicitly_configured(self):
        # Not offered any more, but old quizzes / external deployments keep a resolvable id.
        with self.assertRaisesRegex(ValueError, "not an allowed"):
            model_registry.resolve_generation_model("qwen3-8b")
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "qwen3-8b")):
            self.assertEqual(model_registry.resolve_generation_model("qwen3-8b"), "hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M")

    def test_the_two_offered_chat_models_are_qwen_and_deepseek(self):
        self.assertEqual(config.GENERATION_MODELS, ("qwen-2.5-7b", "deepseek-r1-14b"))
        self.assertEqual(config.DEFAULT_GENERATION_MODEL, "qwen-2.5-7b")
        self.assertEqual(config.QUIZ_DEFAULT_GENERATION_MODEL, "qwen-2.5-7b")
        self.assertEqual(model_registry.resolve_generation_model("qwen-2.5-7b"), "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M")
        self.assertEqual(model_registry.resolve_generation_model("deepseek-r1-14b"), "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M")
        with patch.object(model_registry, "_is_installed", return_value=True):
            offered = {item["id"]: item for item in model_registry.list_generation_models()}
        self.assertEqual(set(offered), {"qwen-2.5-7b", "deepseek-r1-14b"})
        self.assertEqual(offered["deepseek-r1-14b"]["label"], "DeepSeek R1 Distill Qwen 14B")
        self.assertTrue(all("ollama_model" not in item for item in offered.values()))

    def test_quiz_uses_backend_default_while_chat_and_summary_keep_selected_default(self):
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        main = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        summary = (ROOT / "backend" / "summary_service.py").read_text(encoding="utf-8")
        rag = (ROOT / "backend" / "rag_service.py").read_text(encoding="utf-8")

        self.assertIn('model_id: quizModelSelect?.value || "qwen-2.5-7b"', frontend)
        self.assertIn("model_id: request.model_id", frontend)
        self.assertIn("body: JSON.stringify(request)", frontend)
        self.assertGreaterEqual(main.count("or QUIZ_DEFAULT_GENERATION_MODEL"), 2)
        self.assertIn("model_id or DEFAULT_GENERATION_MODEL", summary)
        self.assertIn("resolve_generation_model(model_id)", rag)

    def test_create_quiz_model_selector_offers_exactly_qwen_and_deepseek_by_public_id(self):
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

        qwen = 'new Option("Qwen2.5-7B-Instruct", "qwen-2.5-7b", true, true)'
        deepseek = 'new Option("DeepSeek-R1-Distill-Qwen-14B", "deepseek-r1-14b")'
        self.assertIn(qwen, frontend)
        self.assertIn(deepseek, frontend)
        self.assertLess(frontend.index(qwen), frontend.index(deepseek))
        self.assertEqual(frontend.count("quizModelSelect.add(new Option("), 2)
        self.assertNotIn("hf.co/", frontend)
        self.assertNotIn("qwen3", frontend.lower())

    def test_quiz_calls_disable_reasoning_and_kaggle_prepares_without_residency(self):
        quiz = (ROOT / "backend" / "quiz_service.py").read_text(encoding="utf-8")
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")

        self.assertEqual(quiz.count("reasoning=False"), 7)
        self.assertIn('QUIZ_GENERATION_KEEP_ALIVE = "5m"', quiz)
        self.assertEqual(quiz.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE"), 7)
        self.assertIn('OLLAMA_DEEPSEEK_R1_14B_MODEL="${OLLAMA_DEEPSEEK_R1_14B_MODEL:-hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M}"', startup)
        self.assertIn('"think": False', startup)
        self.assertIn('"keep_alive": 0', startup)
        self.assertIn('OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL = \\"qwen-2.5-7b\\"', notebook)

    def test_kaggle_pulls_both_models_with_the_hugging_face_mechanism_and_no_qwen3(self):
        """The notebook configures exactly Qwen2.5-7B and DeepSeek-R1-Distill-Qwen-14B (both
        hf.co GGUF references pulled by Ollama with the existing pull_model/--insecure path into the
        same OLLAMA_MODELS store); Qwen3 is gone from the notebook and the startup script."""
        import json
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")
        json.loads(notebook)  # the notebook is still valid JSON
        config_cell = "".join(json.loads(notebook)["cells"][1]["source"])

        self.assertIn('OLLAMA_CHAT_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"', config_cell)
        self.assertIn('OLLAMA_DEEPSEEK_R1_14B_MODEL = "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M"', config_cell)
        self.assertIn('"OLLAMA_DEEPSEEK_R1_14B_MODEL": OLLAMA_DEEPSEEK_R1_14B_MODEL', config_cell)
        self.assertIn('AVAILABLE_MODELS = f"{OLLAMA_CHAT_MODEL},{OLLAMA_DEEPSEEK_R1_14B_MODEL}"', config_cell)
        self.assertIn('OLLAMA_GENERATION_MODELS = "qwen-2.5-7b,deepseek-r1-14b"', config_cell)
        self.assertIn('pull_model "$OLLAMA_DEEPSEEK_R1_14B_MODEL"', startup)
        self.assertIn('ollama_has_model "$OLLAMA_DEEPSEEK_R1_14B_MODEL"', startup)
        self.assertIn('pull_model "$OLLAMA_CHAT_MODEL"', startup)
        self.assertIn('ollama pull --insecure "$model"', startup)          # the shared hf.co mechanism
        self.assertIn('OLLAMA_EMBEDDING_MODEL = "bge-m3"', config_cell)     # embedding model is untouched
        self.assertIn('pull_model "$OLLAMA_EMBEDDING_MODEL"', startup)
        self.assertIn('os.environ["OLLAMA_MODELS"] = str(CACHE_DIR / "ollama-models")', notebook.replace("\\", ""))
        for text in (startup, notebook):
            self.assertNotIn("qwen3", text.lower())
            self.assertNotIn("QWEN3", text)

    def test_kaggle_warmup_never_keeps_two_chat_models_in_vram(self):
        """Both chat models are warmed sequentially with keep_alive 0 (unloaded as soon as each
        request ends); only the small embedding model stays resident. Nothing preloads both at once."""
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        body = startup[startup.index("warm_models_once() {"):startup.index("assert_health_models() {")]
        requests = [line for line in body.splitlines() if '"model": os.environ[' in line]
        self.assertEqual(len(requests), 3)
        qwen, deepseek, embedding = requests
        self.assertIn('OLLAMA_CHAT_MODEL', qwen)
        self.assertIn('"keep_alive": 0', qwen)
        self.assertIn('OLLAMA_DEEPSEEK_R1_14B_MODEL', deepseek)
        self.assertIn('"keep_alive": 0', deepseek)
        self.assertIn('OLLAMA_EMBEDDING_MODEL', embedding)
        self.assertIn('"keep_alive": "10m"', embedding)
        self.assertNotIn("&", body)  # no background job: the three requests run one after the other
        self.assertNotIn("PRELOAD", body)
        # the optional preload loop only pulls files: `ollama pull` never loads a model into VRAM
        preload = startup[startup.index('if [[ "$PRELOAD_ALL_MODELS"'):startup.index("Optional generation models are lazy")]
        self.assertNotIn("/api/generate", preload)
        self.assertNotIn("ollama run", startup)
        self.assertIn("ollama ps", body)

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
