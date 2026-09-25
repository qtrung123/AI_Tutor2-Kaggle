"""Tests for the admin-only Quiz Model Comparison tab.

Covers: the admin allowlist gate, the benchmark results store, the
model-comparison service (Qwen vs Gemma roster + production/candidate status),
the admin-only API routes, and frontend wiring (nav visibility, a real
"Run benchmark" form, no subjective quality score and no overall winner).
The benchmark runner itself is covered by tests/test_quiz_model_benchmark.py.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth_store, model_benchmark_store, model_comparison_service
from backend.auth_store import is_admin_email
from backend.main import app
from backend.model_comparison_service import QUIZ_BENCHMARK_MODELS, get_quiz_model_comparison


class IsAdminEmailTests(unittest.TestCase):
    def test_configured_email_is_admin_case_and_whitespace_insensitive(self):
        with patch.object(auth_store, "ADMIN_EMAILS", frozenset({"admin@example.com"})):
            self.assertTrue(is_admin_email("  Admin@Example.com  "))
            self.assertFalse(is_admin_email("student@example.com"))

    def test_empty_allowlist_admits_nobody(self):
        with patch.object(auth_store, "ADMIN_EMAILS", frozenset()):
            self.assertFalse(is_admin_email("anyone@example.com"))


class ModelBenchmarkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_path = Path(self.temp_dir.name) / "quiz_model_benchmark_results.json"
        self.patcher = patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_missing_file_returns_empty_placeholder(self):
        self.assertEqual(model_benchmark_store.load_benchmark_results(), {"generated_at": None, "results": []})

    def test_malformed_json_falls_back_to_empty_placeholder(self):
        self.results_path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(model_benchmark_store.load_benchmark_results(), {"generated_at": None, "results": []})

    def test_save_then_load_roundtrip(self):
        results = [{"model_id": "qwen3-8b", "success_rate": 0.92}]
        model_benchmark_store.save_benchmark_results("2026-09-20T00:00:00+00:00", results)
        loaded = model_benchmark_store.load_benchmark_results()
        self.assertEqual(loaded["generated_at"], "2026-09-20T00:00:00+00:00")
        self.assertEqual(loaded["results"], results)


class ModelComparisonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_path = Path(self.temp_dir.name) / "quiz_model_benchmark_results.json"
        self.patcher = patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_full_roster_is_always_qwen_and_gemma_even_with_no_benchmark_data(self):
        comparison = get_quiz_model_comparison()
        self.assertIsNone(comparison["generated_at"])
        self.assertIsNone(comparison["benchmark_status"])
        self.assertEqual(comparison["runs"], [])
        self.assertEqual(
            [model["model_id"] for model in comparison["models"]], ["qwen-2.5-7b", "gemma3-12b"],
        )
        self.assertTrue(all(model["measured"] is False for model in comparison["models"]))
        self.assertTrue(all(
            model[field] is None for model in comparison["models"]
            for field in model_comparison_service.METRIC_FIELDS
        ))

    def test_measured_model_reports_stored_metrics_others_stay_unmeasured(self):
        model_benchmark_store.save_benchmark_results(
            "2026-09-20T12:00:00+00:00",
            [{
                "model_id": "qwen-2.5-7b", "success_rate": 0.95, "final_question_rate": 0.9,
                "grounding_rate": 0.88, "avg_latency_seconds": 12.4, "avg_retries": 0.6,
                "p95_latency_seconds": 20.1, "questions_per_minute": 4.2,
            }],
        )
        comparison = get_quiz_model_comparison()
        self.assertEqual(comparison["generated_at"], "2026-09-20T12:00:00+00:00")
        by_id = {model["model_id"]: model for model in comparison["models"]}
        self.assertTrue(by_id["qwen-2.5-7b"]["measured"])
        self.assertEqual(by_id["qwen-2.5-7b"]["success_rate"], 0.95)
        self.assertEqual(by_id["qwen-2.5-7b"]["p95_latency_seconds"], 20.1)
        self.assertIsNone(by_id["qwen-2.5-7b"]["avg_tokens_per_second"])   # unavailable -> null
        self.assertFalse(by_id["gemma3-12b"]["measured"])

    def test_current_production_status_follows_configured_quiz_default(self):
        # The status follows whatever the quiz default is; pin it so the test does not depend on it.
        with patch.object(model_comparison_service, "QUIZ_DEFAULT_GENERATION_MODEL", "gemma3-12b"):
            comparison = get_quiz_model_comparison()
        by_id = {model["model_id"]: model["status"] for model in comparison["models"]}
        self.assertEqual(by_id["gemma3-12b"], "current_production")
        self.assertEqual(by_id["qwen-2.5-7b"], "benchmark_candidate")

        with patch.object(model_comparison_service, "QUIZ_DEFAULT_GENERATION_MODEL", "qwen-2.5-7b"):
            comparison = get_quiz_model_comparison()
        by_id = {model["model_id"]: model["status"] for model in comparison["models"]}
        self.assertEqual(by_id["qwen-2.5-7b"], "current_production")
        self.assertEqual(by_id["gemma3-12b"], "benchmark_candidate")

    def test_roster_is_exactly_qwen_vs_gemma_with_no_combined_score(self):
        self.assertEqual([entry["model_id"] for entry in QUIZ_BENCHMARK_MODELS], ["qwen-2.5-7b", "gemma3-12b"])
        for forbidden in ("quality_score", "overall_score", "winner", "vram_mb"):
            self.assertNotIn(forbidden, model_comparison_service.METRIC_FIELDS)


class AdminRouteAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database_path = root / "auth.db"
        self.results_path = root / "quiz_model_benchmark_results.json"

        import backend.conversation_store as conversation_store
        import backend.indexed_document_store as indexed_document_store
        from backend import quiz_store

        self.patchers = [
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
            patch.object(quiz_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(auth_store, "ADMIN_EMAILS", frozenset({"admin@example.com"})),
            patch.object(model_benchmark_store, "QUIZ_MODEL_BENCHMARK_RESULTS_PATH", self.results_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _signup(self, client, email, display_name="Person"):
        response = client.post("/api/auth/signup", json={
            "display_name": display_name, "email": email, "password": "long-password-123",
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_unauthenticated_request_is_rejected(self):
        with TestClient(app) as client:
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 401)

    def test_non_admin_user_is_forbidden(self):
        with TestClient(app) as client:
            self._signup(client, "student@example.com")
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 403)

    def test_admin_user_receives_full_roster(self):
        with TestClient(app) as client:
            self._signup(client, "admin@example.com")
            response = client.get("/api/admin/quiz-model-comparison")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["generated_at"])
        self.assertEqual(len(body["models"]), 2)
        self.assertTrue(all(model["measured"] is False for model in body["models"]))

    def test_signup_login_and_me_all_report_is_admin_flag(self):
        with TestClient(app) as client:
            signup_body = self._signup(client, "admin@example.com")
            self.assertTrue(signup_body["is_admin"])
            me_body = client.get("/api/auth/me").json()
            self.assertTrue(me_body["is_admin"])

        with TestClient(app) as client:
            student_body = self._signup(client, "student2@example.com")
            self.assertFalse(student_body["is_admin"])
            login_body = client.post("/api/auth/login", json={
                "email": "student2@example.com", "password": "long-password-123",
            }).json()
            self.assertFalse(login_body["is_admin"])


class FrontendAdminModelComparisonWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.markup = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.script = (root / "frontend" / "app.js").read_text(encoding="utf-8")

    def test_nav_item_exists_and_is_hidden_by_default(self):
        self.assertIn('id="admin-model-comparison-nav"', self.markup)
        self.assertIn('data-page="model-comparison"', self.markup)
        # The button tag itself must carry the hidden attribute in the markup.
        start = self.markup.index('data-page="model-comparison"')
        tag_slice = self.markup[max(0, start - 80):start + 150]
        self.assertIn("hidden", tag_slice)

    def test_view_section_exists(self):
        self.assertIn('id="model-comparison-view"', self.markup)

    def test_admin_flag_gates_nav_visibility(self):
        self.assertIn('document.getElementById("admin-model-comparison-nav")?.toggleAttribute("hidden", !user.is_admin);', self.script)

    def test_page_load_calls_comparison_loader(self):
        self.assertIn('if (page === "model-comparison") loadQuizModelComparison();', self.script)
        self.assertIn("ADMIN_QUIZ_MODEL_COMPARISON_API_URL", self.script)

    def test_table_shows_only_the_requested_measured_metrics(self):
        for label in ("Success Rate", "Final-question Rate", "Grounding", "Avg Latency", '"p50"', '"p95"',
                      "Avg Retries", "Avg tokens/s", "Failures", "Questions/min"):
            self.assertIn(label, self.script)
        self.assertNotIn("VRAM", self.script)

    def test_status_badges_present(self):
        self.assertIn('"Current Production"', self.script)
        self.assertIn('"Benchmark Candidate"', self.script)

    def test_last_benchmark_timestamp_present(self):
        self.assertIn("Last benchmark:", self.script)

    def test_run_benchmark_form_posts_to_the_real_admin_endpoint(self):
        self.assertIn('const ADMIN_QUIZ_MODEL_BENCHMARK_API_URL = apiUrl("/api/admin/quiz-model-benchmark");', self.script)
        self.assertIn(">Run benchmark</button>", self.script)
        self.assertIn('fetchJson(ADMIN_QUIZ_MODEL_BENCHMARK_API_URL, {\n        method: "POST"', self.script)
        for field in ('name="document_ids"', 'name="difficulty"', 'name="question_count"', 'name="runs"'):
            self.assertIn(field, self.script)

    def test_raw_runs_are_expandable_and_progress_is_polled(self):
        self.assertIn('details.className = "benchmark-raw-runs";', self.script)
        self.assertIn("Raw runs \u2014", self.script)
        self.assertIn("All recorded fields (JSON)", self.script)
        self.assertIn("benchmarkPollTimer = setTimeout(loadQuizModelComparison, 5000);", self.script)

    def test_cold_start_is_shown_separately_from_latency(self):
        self.assertIn("Cold start (warm-up before measured runs)", self.script)
        self.assertIn("const warmups = data.warmups || [];", self.script)
        self.assertIn('"Load in run"', self.script)
        self.assertNotIn("run.model_prepare_ms", self.script)

    def test_no_subjective_quality_score_or_winner_surfaced(self):
        self.assertNotIn("quality_score", self.script)
        self.assertNotIn("qualityScore", self.script)
        self.assertNotIn("winner", self.script.lower())


if __name__ == "__main__":
    unittest.main()
