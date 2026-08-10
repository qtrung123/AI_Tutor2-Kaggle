import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth_store, conversation_store, indexed_document_store, quiz_store
from backend.auth_store import LEGACY_USER_ID
from backend.main import app
from backend.recommendation_service import generate_recommendations, mastery_to_recommendation


def mastery(level, score=0, evidence=True, sufficient=True):
    return {
        "student_id": "u", "document_id": "doc.pdf", "document_name": "Document",
        "topic_id": "topic", "topic_name": "Topic", "mastery_score": score,
        "mastery_level": level, "has_evidence": evidence, "has_sufficient_evidence": sufficient,
        "assessment_capacity": 5, "distinct_concepts_assessed": 3,
        "concept_coverage_ratio": 0.6, "required_concept_coverage": 0.6,
        "answered_questions": 3, "completed_attempts": 1,
    }


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db = root / "recommendations.db"
        self.patchers = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(conversation_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "none.json"),
            patch.object(quiz_store, "DATABASE_PATH", self.db),
        ]
        for patcher in self.patchers: patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers): patcher.stop()
        gc.collect()
        self.temp_dir.cleanup()

    def add_document(self, user=LEGACY_USER_ID, document="doc.pdf", topics=None):
        topics = topics or [{"topic_id": "topic", "name": "Topic"}]
        indexed_document_store.upsert_indexed_document(user, document, {
            "display_name": "Document", "hash": "hash", "chunks": 5,
            "path": document, "topic_schema_version": 1, "topics": topics,
        })

    def save_attempt(self, user, topic, correct, total, capacity=5, document="doc.pdf", quiz_id=None):
        results = []
        for index in range(total):
            is_correct = index < correct
            results.append({
                "question_id": index + 1, "question": f"Q{index + 1}", "options": ["A", "B"],
                "selected_answer": "A" if is_correct else "B", "correct_answer": "A", "is_correct": is_correct,
                "question_difficulty": "easy", "validation_outcome": "accepted",
                "topic_id": topic, "topic_name": topic.title(), "concept_id": f"{topic}-c{index + 1}",
                "assessment_capacity": capacity, "evidence_requirement_version": "concept_coverage_v1",
            })
        quiz_store.save_quiz_progress(document, "easy", {
            "quiz_id": quiz_id or f"quiz-{topic}", "answers": {}, "question_results": results,
            "score": correct, "answered": total, "total": total, "completed": True,
        }, topic, user)

    def test_rule_types_reasons_and_navigation_context(self):
        weak = mastery_to_recommendation(mastery("Weak", 40))
        self.assertEqual((weak["priority"], weak["reason_code"], weak["is_knowledge_gap"]), ("high", "weak_mastery", True))
        self.assertIn("40.0%", weak["reason_text"])
        self.assertIn("3 of 5", weak["reason_text"])
        self.assertEqual(weak["primary_action"]["navigation_context"], {
            "page": "practice", "assessment_scope": "topic", "document_id": "doc.pdf", "topic_id": "topic",
        })
        developing = mastery_to_recommendation(mastery("Developing", 60))
        self.assertEqual(developing["priority"], "moderate")
        insufficient = mastery_to_recommendation(mastery("Insufficient evidence", 100, True, False))
        self.assertEqual((insufficient["reason_code"], insufficient["is_knowledge_gap"]), ("insufficient_evidence", False))
        self.assertEqual(insufficient["primary_action"]["label"], "Continue assessment")
        unassessed = mastery_to_recommendation(mastery("Not assessed", 0, False, False))
        self.assertEqual(unassessed["primary_action"]["label"], "Start assessment")
        self.assertIsNone(mastery_to_recommendation(mastery("Proficient", 75)))
        self.assertIsNone(mastery_to_recommendation(mastery("Mastered", 90)))

    def test_complete_mixed_document_order_and_exclusion(self):
        topics = [{"topic_id": name, "name": name.title()} for name in ["weak", "developing", "proficient", "insufficient", "unassessed"]]
        self.add_document(topics=topics)
        self.save_attempt(LEGACY_USER_ID, "weak", 0, 3)
        self.save_attempt(LEGACY_USER_ID, "developing", 2, 3)
        self.save_attempt(LEGACY_USER_ID, "proficient", 3, 4)
        self.save_attempt(LEGACY_USER_ID, "insufficient", 1, 1)
        rows = generate_recommendations(LEGACY_USER_ID)
        self.assertEqual([row["topic_id"] for row in rows], ["weak", "developing", "insufficient", "unassessed"])
        self.assertEqual([row["reason_code"] for row in rows], ["weak_mastery", "developing_mastery", "insufficient_evidence", "not_assessed"])

    def test_deterministic_ordering_and_document_filtering(self):
        self.add_document(document="two.pdf", topics=[{"topic_id": "z", "name": "Z"}])
        self.add_document(document="one.pdf", topics=[{"topic_id": "b", "name": "B"}, {"topic_id": "a", "name": "A"}])
        rows = generate_recommendations(LEGACY_USER_ID)
        self.assertEqual([(row["document_id"], row["topic_id"]) for row in rows], [("one.pdf", "a"), ("one.pdf", "b"), ("two.pdf", "z")])
        self.assertEqual([row["topic_id"] for row in generate_recommendations(LEGACY_USER_ID, "two.pdf")], ["z"])

    def test_mastery_update_removes_stale_recommendation_without_persistence(self):
        self.add_document()
        self.save_attempt(LEGACY_USER_ID, "topic", 0, 3, quiz_id="same-quiz")
        self.assertEqual(generate_recommendations(LEGACY_USER_ID)[0]["reason_code"], "weak_mastery")
        self.save_attempt(LEGACY_USER_ID, "topic", 3, 3, quiz_id="same-quiz")
        self.assertEqual(generate_recommendations(LEGACY_USER_ID), [])
        with quiz_store._connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("recommendations", tables)

    def test_authenticated_isolation_api_and_no_user_parameter(self):
        alice = auth_store.create_user("Alice", "alice@rec.test", "long-password-123")
        bob = auth_store.create_user("Bob", "bob@rec.test", "long-password-123")
        self.add_document(alice["id"], topics=[{"topic_id": "alice-topic", "name": "Alice Topic"}])
        self.add_document(bob["id"], topics=[{"topic_id": "bob-topic", "name": "Bob Topic"}])
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/recommendations").status_code, 401)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"email": "alice@rec.test", "password": "long-password-123"})
            rows = client.get("/api/recommendations", params={"user_id": bob["id"]}).json()
            self.assertEqual([row["topic_id"] for row in rows], ["alice-topic"])
            parameters = app.openapi()["paths"]["/api/recommendations"]["get"].get("parameters", [])
            self.assertNotIn("user_id", {parameter["name"] for parameter in parameters})

    def test_overview_integration_and_real_empty_states(self):
        self.assertEqual(generate_recommendations(LEGACY_USER_ID), [])
        markup = Path("frontend/index.html").read_text(encoding="utf-8")
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="overview-recommendations-list"', markup)
        self.assertIn("/api/recommendations", script)
        self.assertIn("No priority learning actions right now.", script)
        self.assertIn("Add learning material", script)
        self.assertIn("openPracticeContext(recommendation.document_id, recommendation.topic_id)", script)


if __name__ == "__main__":
    unittest.main()
