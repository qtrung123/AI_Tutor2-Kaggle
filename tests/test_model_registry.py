import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import config
from backend import model_registry
from frontend_source import frontend_script_text


ROOT = Path(__file__).parents[1]
DEEPSEEK_REF = "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:Q4_K_M"


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

        self.assertIn("for required_model_id in qwen-2.5-7b gemma3-12b; do", startup)
        self.assertIn('case ",$OLLAMA_GENERATION_MODELS,"', startup)
        self.assertIn('OLLAMA_GENERATION_MODELS = \\"qwen-2.5-7b,gemma3-12b\\"', notebook)
        self.assertNotIn("qwen3-4b", startup)
        self.assertNotIn("qwen3-4b", notebook)

    def test_deepseek_and_glm_are_rejected_for_new_generation_even_if_configured(self):
        """Temporarily inactive: not offered and never resolved, even when an older environment
        (e.g. a stale Kaggle notebook) still lists them in OLLAMA_GENERATION_MODELS."""
        for model_id in ("deepseek-r1-14b", "glm4-9b"):
            with self.subTest(model_id=model_id):
                with self.assertRaisesRegex(ValueError, "not an allowed"):
                    model_registry.resolve_generation_model(model_id)
                with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "gemma3-12b", model_id)):
                    with self.assertRaisesRegex(ValueError, "not an allowed"):
                        model_registry.resolve_generation_model(model_id)
                    with patch.object(model_registry, "_is_installed", return_value=True):
                        self.assertNotIn(model_id, {item["id"] for item in model_registry.list_generation_models()})

    def test_inactive_models_keep_their_historical_description(self):
        """Artifacts generated earlier by DeepSeek/GLM keep a recognizable model."""
        self.assertEqual(model_registry.describe_generation_model(DEEPSEEK_REF)["model_id"], "deepseek-r1-14b")
        self.assertEqual(model_registry.describe_generation_model("glm4:9b-chat-q4_K_M")["model_id"], "glm4-9b")
        self.assertEqual(model_registry.describe_generation_model("glm4:9b-chat-q4_K_M")["name"], "glm4")

    def test_gemma3_public_id_resolves_to_exact_official_ollama_reference(self):
        self.assertEqual(model_registry.resolve_generation_model("gemma3-12b"), "gemma3:12b-it-q4_K_M")

    def test_qwen3_8b_stays_resolvable_only_when_explicitly_configured(self):
        # Not offered any more, but old quizzes / external deployments keep a resolvable id.
        with self.assertRaisesRegex(ValueError, "not an allowed"):
            model_registry.resolve_generation_model("qwen3-8b")
        with patch.object(model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "qwen3-8b")):
            self.assertEqual(model_registry.resolve_generation_model("qwen3-8b"), "hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M")

    def test_the_active_models_are_exactly_qwen_default_and_gemma(self):
        self.assertEqual(config.GENERATION_MODELS, ("qwen-2.5-7b", "gemma3-12b"))
        self.assertEqual(config.DEFAULT_GENERATION_MODEL, "qwen-2.5-7b")
        self.assertEqual(config.QUIZ_DEFAULT_GENERATION_MODEL, "qwen-2.5-7b")
        self.assertEqual(model_registry.resolve_generation_model("qwen-2.5-7b"), "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M")
        self.assertEqual(model_registry.resolve_generation_model("gemma3-12b"), "gemma3:12b-it-q4_K_M")
        with patch.object(model_registry, "_is_installed", return_value=True):
            offered = model_registry.list_generation_models()
        self.assertEqual(offered, [
            {"id": "qwen-2.5-7b", "label": "Qwen 2.5 7B", "default": True, "ready": True},
            {"id": "gemma3-12b", "label": "Gemma 3 12B", "default": False, "ready": True},
        ])

    def test_ready_false_does_not_remove_a_configured_but_uninstalled_model_from_the_list(self):
        """A model that is configured/allowed but not yet pulled on Kaggle still shows up in the
        Study Session selector - it is just reported not ready, never hidden."""
        with patch.object(model_registry, "_is_installed", return_value=False):
            offered = {item["id"]: item for item in model_registry.list_generation_models()}
        self.assertEqual(list(offered), ["qwen-2.5-7b", "gemma3-12b"])
        self.assertFalse(offered["gemma3-12b"]["ready"])

    def test_quiz_uses_backend_default_while_chat_and_summary_keep_selected_default(self):
        frontend = frontend_script_text()
        quiz_api = (ROOT / "backend" / "api" / "quiz_generation.py").read_text(encoding="utf-8")
        summary = (ROOT / "backend" / "summary_service.py").read_text(encoding="utf-8")
        rag = (ROOT / "backend" / "rag_service.py").read_text(encoding="utf-8")

        self.assertIn('model_id: selectedModelId || "qwen-2.5-7b"', frontend)
        self.assertIn("model_id: request.model_id", frontend)
        self.assertIn("body: JSON.stringify(request)", frontend)
        self.assertGreaterEqual(quiz_api.count("or QUIZ_DEFAULT_GENERATION_MODEL"), 2)
        self.assertIn("model_id or DEFAULT_GENERATION_MODEL", summary)
        self.assertIn("resolve_generation_model(model_id)", rag)

    def test_the_only_model_selector_is_the_study_session_one_and_it_lists_the_registry_models(self):
        frontend = frontend_script_text()

        self.assertEqual(frontend.count('select.id = "generation-model-select"'), 1)
        self.assertNotIn("quizModelSelect", frontend)              # the Create Quiz form has no model field of its own
        self.assertIn("generationModels.forEach((model) => select.add(new Option(model.label, model.id", frontend)
        self.assertNotIn("hf.co/", frontend)                       # public ids only, never runtime references
        self.assertNotIn("qwen3", frontend.lower())

    def test_every_generation_route_prepares_the_selected_model_before_generating(self):
        """Every Study Session-driven generation entry point in the API layer (backend/main.py and
        its backend/api/ routers) reuses the same prepare_generation_model choke point (never a
        bespoke, duplicated pull) before calling into Quiz/Summary/Flashcards/chat, so a
        lazily-pulled model is fetched before its first real use."""
        api_sources = [ROOT / "backend" / "main.py", *sorted((ROOT / "backend" / "api").glob("*.py"))]
        api = "".join(path.read_text(encoding="utf-8") for path in api_sources)
        self.assertGreaterEqual(api.count("prepare_generation_model("), 6)

    def test_quiz_calls_disable_reasoning_and_kaggle_prepares_without_residency(self):
        quiz = (ROOT / "backend" / "quiz_service.py").read_text(encoding="utf-8")
        legacy = (ROOT / "backend" / "quiz_legacy_v2.py").read_text(encoding="utf-8")
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")

        # Live pipeline (single-choice calls only; fill_blank needs no call) plus the legacy V2 engines.
        self.assertEqual(quiz.count("reasoning=False"), 1)
        self.assertEqual(legacy.count("reasoning=False"), 6)
        self.assertEqual(quiz.count("reasoning=False") + legacy.count("reasoning=False"), 7)
        self.assertIn('QUIZ_GENERATION_KEEP_ALIVE = "5m"', quiz)
        self.assertEqual(quiz.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE"), 1)
        self.assertEqual(legacy.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE"), 6)
        self.assertEqual(
            quiz.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE") + legacy.count("keep_alive=QUIZ_GENERATION_KEEP_ALIVE"), 7
        )
        self.assertIn('OLLAMA_GEMMA3_12B_MODEL="${OLLAMA_GEMMA3_12B_MODEL:-gemma3:12b-it-q4_K_M}"', startup)
        self.assertIn('"keep_alive": 0', startup)
        self.assertIn('OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL = \\"qwen-2.5-7b\\"', notebook)

    def test_kaggle_pulls_qwen_with_the_hugging_face_mechanism_and_no_qwen3(self):
        """The notebook configures exactly Qwen2.5-7B (default, hf.co GGUF) and Gemma 3 12B (an
        official Ollama library model); DeepSeek, GLM and Qwen3 are gone from the notebook and the
        startup script."""
        import json
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        notebook = (ROOT / "kaggle_run.ipynb").read_text(encoding="utf-8")
        json.loads(notebook)  # the notebook is still valid JSON
        config_cell = "".join(json.loads(notebook)["cells"][1]["source"])

        self.assertIn('OLLAMA_CHAT_MODEL = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"', config_cell)
        self.assertIn('OLLAMA_GEMMA3_12B_MODEL = "gemma3:12b-it-q4_K_M"', config_cell)
        self.assertIn('"OLLAMA_GEMMA3_12B_MODEL": OLLAMA_GEMMA3_12B_MODEL', config_cell)
        self.assertIn('AVAILABLE_MODELS = f"{OLLAMA_CHAT_MODEL},{OLLAMA_GEMMA3_12B_MODEL}"', config_cell)
        self.assertIn('OLLAMA_GENERATION_MODELS = "qwen-2.5-7b,gemma3-12b"', config_cell)
        self.assertIn('AVAILABLE_MODELS="${AVAILABLE_MODELS:-$OLLAMA_CHAT_MODEL,$OLLAMA_GEMMA3_12B_MODEL}"', startup)
        # DeepSeek and GLM are neither configured nor pulled by the normal runtime.
        for text in (startup, config_cell):
            for inactive in ("OLLAMA_DEEPSEEK_R1_14B_MODEL", "OLLAMA_GLM4_9B_MODEL", "deepseek-r1-14b", "glm4-9b"):
                self.assertNotIn(inactive, text)
        # Only Qwen (the default) is pulled unconditionally at startup.
        self.assertIn('pull_model "$OLLAMA_CHAT_MODEL"', startup)
        self.assertIn('ollama pull --insecure "$model"', startup)          # the shared hf.co mechanism
        self.assertIn('OLLAMA_EMBEDDING_MODEL = "bge-m3"', config_cell)     # embedding model is untouched
        self.assertIn('pull_model "$OLLAMA_EMBEDDING_MODEL"', startup)
        self.assertIn('os.environ["OLLAMA_MODELS"] = str(CACHE_DIR / "ollama-models")', notebook.replace("\\", ""))
        for text in (startup, notebook):
            self.assertNotIn("qwen3", text.lower())
            self.assertNotIn("QWEN3", text)

    def test_gemma_is_not_pulled_unconditionally_at_startup(self):
        """Gemma is configured/offered but lazy: nothing in the unconditional startup path (before
        the opt-in PRELOAD_ALL_MODELS block) pulls or checks it (DeepSeek/GLM are not configured)."""
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        unconditional_startup = startup[
            startup.index('log "Starting Ollama"'):startup.index('if [[ "$PRELOAD_ALL_MODELS"')
        ]
        for reference_var in ("$OLLAMA_GEMMA3_12B_MODEL",):
            self.assertNotIn(f'pull_model "{reference_var}"', unconditional_startup)
            self.assertNotIn(f'ollama_has_model "{reference_var}"', unconditional_startup)
        # They are still reachable through the generic, opt-in preload loop (benchmarking only).
        preload = startup[startup.index('if [[ "$PRELOAD_ALL_MODELS"'):startup.index("Optional generation models are lazy")]
        self.assertIn('IFS=\',\' read -r -a generation_models <<< "$AVAILABLE_MODELS"', preload)
        self.assertNotIn("hf.co/*", preload)   # generic now: pulls any reference kind, not just hf.co

    def test_kaggle_warmup_only_warms_the_startup_default_and_embedding_model(self):
        """Only Qwen (the startup default) and the embedding model are warmed at startup, both with
        keep_alive 0/short residency; DeepSeek/Gemma/GLM are never pulled or warmed here."""
        startup = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
        body = startup[startup.index("warm_models_once() {"):startup.index("assert_health_models() {")]
        requests = [line for line in body.splitlines() if '"model": os.environ[' in line]
        self.assertEqual(len(requests), 2)
        qwen, embedding = requests
        self.assertIn('OLLAMA_CHAT_MODEL', qwen)
        self.assertIn('"keep_alive": 0', qwen)
        self.assertIn('OLLAMA_EMBEDDING_MODEL', embedding)
        self.assertIn('"keep_alive": "10m"', embedding)
        self.assertNotIn("OLLAMA_DEEPSEEK_R1_14B_MODEL", body)
        self.assertNotIn("OLLAMA_GEMMA3_12B_MODEL", body)
        self.assertNotIn("OLLAMA_GLM4_9B_MODEL", body)
        self.assertNotIn("&", body)  # no background job: the requests run one after the other
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

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_prepare_pulls_only_resolved_selected_runtime_model(self, _installed, run):
        """Qwen's reference is hf.co/*, so preparing it (e.g. from the selector's own "prepare on
        change" call) goes through the same Kaggle-safe CLI insecure-first path as any other hf.co
        model - the real `ollama` CLI, run as an argv list, never shell=True."""
        model_registry.prepare_generation_model("qwen-2.5-7b")
        run.assert_called_once_with(
            ["ollama", "pull", "--insecure", "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"],
            check=True, capture_output=True, text=True, timeout=900,
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_inactive_deepseek_and_glm_are_never_pulled(self, _installed, run):
        """Preparing a temporarily inactive model is rejected before any pull is attempted."""
        with patch("backend.model_registry.httpx.Client") as client_type:
            for model_id in ("deepseek-r1-14b", "glm4-9b"):
                with self.assertRaisesRegex(ValueError, "not an allowed"):
                    model_registry.prepare_generation_model(model_id)
        run.assert_not_called()
        client_type.assert_not_called()

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_hf_co_pull_falls_back_to_a_plain_cli_pull_when_insecure_fails(self, _installed, run):
        """Mirrors start_kaggle.sh's pull_model(): `ollama pull --insecure` can still fail on Kaggle
        ("blocked redirect to a different host"); the second, plain `ollama pull` is then tried and,
        if it succeeds, no error reaches the caller."""
        run.side_effect = [
            subprocess.CalledProcessError(1, ["ollama", "pull", "--insecure"], stderr="blocked redirect to a different host"),
            subprocess.CompletedProcess(args=[], returncode=0),
        ]
        model_registry.prepare_generation_model("qwen-2.5-7b")
        self.assertEqual(run.call_count, 2)
        first_call, second_call = run.call_args_list
        self.assertIn("--insecure", first_call.args[0])
        self.assertNotIn("--insecure", second_call.args[0])
        self.assertEqual(first_call.args[0][-1], second_call.args[0][-1])   # same model reference both times
        self.assertEqual(first_call.args[0][-1], "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M")

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_hf_co_pull_raises_and_never_falls_back_to_qwen_when_both_cli_attempts_fail(self, _installed, run):
        """No silent fallback, even for the two-attempt hf.co CLI path: if both `ollama pull
        --insecure` and the plain `ollama pull` fail, the caller sees the real (second) error,
        never a quiet swap to Qwen."""
        run.side_effect = subprocess.CalledProcessError(1, ["ollama", "pull"], stderr="simulated failure")
        with self.assertRaises(subprocess.CalledProcessError):
            model_registry.prepare_generation_model("qwen-2.5-7b")
        self.assertEqual(run.call_count, 2)   # insecure attempt, then the plain fallback

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_hf_co_pull_falls_through_to_plain_pull_when_the_ollama_binary_is_missing(self, _installed, run):
        """An OSError (e.g. the `ollama` binary not found) from the insecure attempt is treated the
        same as a failed pull: fall through to the plain attempt rather than crashing differently."""
        run.side_effect = [FileNotFoundError("ollama not found"), subprocess.CompletedProcess(args=[], returncode=0)]
        model_registry.prepare_generation_model("qwen-2.5-7b")
        self.assertEqual(run.call_count, 2)

    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_plain_ollama_ref_pull_never_uses_insecure_and_never_retries(self, _installed, client_type):
        """Gemma (an official Ollama library reference, not Hugging Face) keeps using a normal,
        single Ollama pull - never the hf.co insecure/fallback mechanism, and never a silent retry
        with a different model on failure."""
        client = client_type.return_value.__enter__.return_value
        client.post.side_effect = httpx.ConnectError("simulated: no route to the Ollama host")
        with self.assertRaises(httpx.ConnectError):
            model_registry.prepare_generation_model("gemma3-12b")
        client.post.assert_called_once_with(
            "http://127.0.0.1:11434/api/pull", json={"name": "gemma3:12b-it-q4_K_M", "stream": False},
        )

    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_gemma_is_lazy_and_pulled_with_its_exact_reference_when_missing(self, _installed, client_type):
        """Mocked-only: proves the pull call is issued with exactly the official Ollama references,
        without ever touching a real Ollama or downloading anything."""
        client = client_type.return_value.__enter__.return_value
        response = client.post.return_value
        response.raise_for_status.return_value = None

        model_registry.prepare_generation_model("gemma3-12b")
        client.post.assert_called_with(
            "http://127.0.0.1:11434/api/pull", json={"name": "gemma3:12b-it-q4_K_M", "stream": False},
        )

        self.assertEqual(client.post.call_count, 1)

    @patch("backend.model_registry.subprocess.run")
    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=True)
    def test_preparing_an_already_cached_model_never_pulls_again(self, _installed, client_type, run):
        for model_id in ("gemma3-12b", "qwen-2.5-7b"):
            with self.subTest(model_id=model_id):
                result = model_registry.prepare_generation_model(model_id)
                self.assertEqual(result["model_id"], model_id)
                self.assertEqual((result["status"], result["ready"]), ("ready", True))
                self.assertNotIn("prepared", result)
                self.assertIsInstance(result["model_prepare_ms"], int)
        client_type.assert_not_called()
        run.assert_not_called()   # not even the hf.co CLI path (qwen-2.5-7b) touches subprocess

    @patch("backend.model_registry._is_installed", return_value=True)
    def test_prepare_never_touches_the_network_when_the_model_is_already_cached(self, _installed):
        """Model preparation is testable without downloading any model: an already-installed model
        short-circuits before any httpx.Client or subprocess is even constructed/run, so this proves
        no pull is issued - neither an API call nor a real `ollama pull`."""
        with patch("backend.model_registry.httpx.Client") as client_type, \
             patch("backend.model_registry.subprocess.run") as run:
            result = model_registry.prepare_generation_model("qwen-2.5-7b")
        client_type.assert_not_called()
        run.assert_not_called()
        self.assertEqual(result["model_id"], "qwen-2.5-7b")
        self.assertEqual((result["status"], result["ready"]), ("ready", True))
        self.assertNotIn("prepared", result)
        self.assertIsInstance(result["model_prepare_ms"], int)

    @patch("backend.model_registry.httpx.Client")
    @patch("backend.model_registry._is_installed", return_value=False)
    def test_a_failed_pull_raises_and_never_silently_falls_back_to_qwen(self, _installed, client_type):
        """Preparation failure produces an error, never a silent switch to another model: the caller
        gets the real failure tied to the model it actually asked for."""
        client = client_type.return_value.__enter__.return_value
        client.post.side_effect = httpx.ConnectError("simulated: no route to the Ollama host")
        with self.assertRaises(httpx.ConnectError):
            model_registry.prepare_generation_model("gemma3-12b")
        # the call that failed was for gemma3, never silently redirected to qwen's reference
        client.post.assert_called_once_with(
            "http://127.0.0.1:11434/api/pull", json={"name": "gemma3:12b-it-q4_K_M", "stream": False},
        )


class ModelSpecStructureTests(unittest.TestCase):
    """The registry is generic: every entry is a structured ModelSpec, so adding another future
    model is one more entry instead of new code in Quiz/Summary/Flashcards."""

    def test_every_built_in_entry_is_a_structured_model_spec(self):
        for model_id, spec in model_registry._BUILT_INS.items():
            self.assertIsInstance(spec, model_registry.ModelSpec)
            self.assertEqual(spec.model_id, model_id)
            self.assertTrue(spec.display_name)
            self.assertIsInstance(spec.enabled, bool)

    def test_quantization_is_derived_from_the_runtime_reference_not_hand_maintained(self):
        registry = model_registry._registry()
        self.assertEqual(registry["qwen-2.5-7b"].quantization, "Q4_K_M")
        self.assertEqual(registry["deepseek-r1-14b"].quantization, "Q4_K_M")

    def test_gemma_is_enabled_and_deepseek_glm_are_kept_but_inactive(self):
        gemma = model_registry._BUILT_INS["gemma3-12b"]
        self.assertTrue(gemma.enabled)
        self.assertEqual(gemma.ollama_model, "gemma3:12b-it-q4_K_M")
        # Kept only so artifacts they generated earlier stay readable.
        deepseek = model_registry._BUILT_INS["deepseek-r1-14b"]
        glm = model_registry._BUILT_INS["glm4-9b"]
        self.assertEqual((deepseek.enabled, glm.enabled), (False, False))
        self.assertEqual(deepseek.ollama_model, DEEPSEEK_REF)
        self.assertEqual(glm.ollama_model, "glm4:9b-chat-q4_K_M")
        # the old inert placeholder ids are gone, not just disabled
        self.assertNotIn("gemma-2-9b", model_registry._BUILT_INS)
        self.assertNotIn("glm-4-9b", model_registry._BUILT_INS)

    def test_a_disabled_model_stays_rejected_even_if_an_operator_configures_it(self):
        """No silent fallback: explicitly allowlisting a disabled entry must not make it resolvable,
        so a misconfiguration fails loudly instead of quietly serving another model."""
        fake = model_registry.ModelSpec("future-model-x", "Future Model X", "", enabled=False)
        with patch.dict(model_registry._BUILT_INS, {"future-model-x": fake}), patch.object(
            model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "future-model-x")
        ):
            with self.assertRaisesRegex(ValueError, "not an allowed"):
                model_registry.resolve_generation_model("future-model-x")
            self.assertNotIn("future-model-x", {item["id"] for item in model_registry.list_generation_models()})

    def test_enabling_a_future_model_is_one_registry_entry(self):
        """Flipping enabled=True and setting a real ollama_model is the only change a new model
        needs: list_generation_models and resolve_generation_model pick it up with no other code."""
        fake = model_registry.ModelSpec("future-model-x", "Future Model X", "hf.co/example/Future-Model-X-GGUF:Q4_K_M")
        with patch.dict(model_registry._BUILT_INS, {"future-model-x": fake}), patch.object(
            model_registry, "GENERATION_MODELS", ("qwen-2.5-7b", "future-model-x")
        ):
            self.assertEqual(
                model_registry.resolve_generation_model("future-model-x"), "hf.co/example/Future-Model-X-GGUF:Q4_K_M"
            )
            with patch.object(model_registry, "_is_installed", return_value=True):
                offered = {item["id"] for item in model_registry.list_generation_models()}
            self.assertIn("future-model-x", offered)


class HistoricalArtifactTests(unittest.TestCase):
    """Quizzes generated earlier by now-inactive models (DeepSeek/GLM) stay readable and listed."""

    def setUp(self):
        import tempfile
        from backend import auth_store, quiz_store
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.quiz_store = quiz_store
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", root / "app.db"),
            patch.object(quiz_store, "DATABASE_PATH", root / "app.db"),
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", root / "q.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", root / "a.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", root / "e.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_quizzes_generated_by_inactive_models_remain_readable(self):
        from backend.quiz_service import _quiz_variant_status
        for model_id, reference in (("deepseek-r1-14b", DEEPSEEK_REF), ("glm4-9b", "glm4:9b-chat-q4_K_M")):
            with self.subTest(model_id=model_id):
                info = model_registry.describe_generation_model(reference)
                saved = self.quiz_store.save_quiz("doc.pdf", "easy", {
                    "title": f"{model_id} quiz", "topic_id": "document", "assessment_scope": "document",
                    "assessment_plan": {"planner_version": "study_units_v1", "requested_count": 1, "generation_model": info},
                    "questions": [{"id": 1, "question": "Q?", "options": ["A. a", "B. b", "C. c", "D. d"],
                                   "correct_answer": "A", "question_type": "single_choice"}],
                }, "owner-1")
                stored = self.quiz_store.get_quiz_by_id(saved["quiz_id"], "owner-1")
                self.assertEqual(stored["generation_model"]["model_id"], model_id)
                self.assertEqual(len(stored["questions"]), 1)
                variant = _quiz_variant_status(stored, "owner-1")
                self.assertEqual((variant["model_id"], variant["model_name"]), (model_id, info["name"]))


if __name__ == "__main__":
    unittest.main()
