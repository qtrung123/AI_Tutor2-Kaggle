import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import auth_store, conversation_store, indexed_document_store, quiz_store
from backend.knowledge_gap_service import detect_knowledge_gaps, mastery_to_gap
from backend.main import app
from backend.mastery_service import recompute_topic_mastery


def mastery(level: str, score: float = 30, topic: str = "topic_a", user: str = "user-a", coverage: float = 0.6) -> dict:
    return {
        "student_id": user, "document_id": "doc.pdf", "topic_id": topic,
        "topic_name": f"Topic {topic}", "mastery_score": score, "mastery_level": level,
        "has_evidence": level != "Not assessed",
        "has_sufficient_evidence": level not in {"Not assessed", "Insufficient evidence"},
        "assessment_capacity": 5, "distinct_concepts_assessed": 3,
        "concept_coverage_ratio": coverage, "required_concept_coverage": 0.6,
        "answered_questions": 3, "completed_attempts": 1,
    }


def stored_mastery(user: str, document: str, topic: str, level: str, score: float, coverage: float = 0.6) -> dict:
    return {
        **mastery(level, score, topic, user, coverage), "document_id": document,
        "earned_weight": score / 100, "possible_weight": 1, "correct_answers": 1,
        "minimum_questions_required": 3, "required_concepts": 3,
        "formula_version": "weighted_accuracy_v1", "formula_config": {},
    }


class KnowledgeGapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "gaps.db"
        self.patchers = [
            patch.object(auth_store, "DATABASE_PATH", self.database_path),
            patch.object(conversation_store, "DATABASE_PATH", self.database_path),
            patch.object(quiz_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "DATABASE_PATH", self.database_path),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp_dir.name) / "missing.json"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        gc.collect()
        self.temp_dir.cleanup()

    def test_mastery_level_rules_and_unreliable_evidence_exclusion(self):
        self.assertEqual(mastery_to_gap(mastery("Weak"))["severity"], "high")
        self.assertEqual(mastery_to_gap(mastery("Developing"))["severity"], "moderate")
        self.assertIsNone(mastery_to_gap(mastery("Proficient", 75)))
        self.assertIsNone(mastery_to_gap(mastery("Mastered", 90)))
        self.assertIsNone(mastery_to_gap(mastery("Not assessed", 0)))
        self.assertIsNone(mastery_to_gap(mastery("Insufficient evidence", 10)))

    def test_deterministic_ranking_and_empty_state(self):
        rows = [
            stored_mastery("u", "doc.pdf", "topic_z", "Developing", 50),
            stored_mastery("u", "doc.pdf", "topic_b", "Weak", 20, 0.8),
            stored_mastery("u", "doc.pdf", "topic_c", "Weak", 10, 0.7),
            stored_mastery("u", "doc.pdf", "topic_a", "Weak", 20, 0.4),
        ]
        for row in rows:
            quiz_store.save_topic_mastery(row)
        self.assertEqual(
            [gap["topic_id"] for gap in detect_knowledge_gaps("u")],
            ["topic_c", "topic_a", "topic_b", "topic_z"],
        )
        self.assertEqual(detect_knowledge_gaps("nobody"), [])

    def test_user_isolation_and_document_filtering(self):
        for row in [
            stored_mastery("alice", "one.pdf", "a", "Weak", 10),
            stored_mastery("alice", "two.pdf", "b", "Developing", 50),
            stored_mastery("bob", "one.pdf", "private", "Weak", 0),
        ]:
            quiz_store.save_topic_mastery(row)
        self.assertEqual([g["topic_id"] for g in detect_knowledge_gaps("alice", "one.pdf")], ["a"])
        self.assertEqual({g["topic_id"] for g in detect_knowledge_gaps("alice")}, {"a", "b"})
        self.assertEqual([g["topic_id"] for g in detect_knowledge_gaps("bob")], ["private"])

    def test_gaps_are_rebuildable_from_attempt_history(self):
        answers = []
        for index in range(1, 4):
            answers.append({
                "question_id": index, "question": f"Question {index}", "options": ["A", "B"],
                "selected_answer": "B", "correct_answer": "A", "is_correct": False,
                "question_difficulty": "easy", "validation_outcome": "accepted",
                "topic_id": "net", "topic_name": "Networking", "concept_id": f"c{index}",
                "assessment_capacity": 5, "evidence_requirement_version": "concept_coverage_v1",
            })
        quiz_store.save_quiz_progress("history.pdf", "easy", {
            "quiz_id": "q1", "answers": {}, "question_results": answers,
            "score": 0, "answered": 3, "total": 3, "completed": True,
        }, "net", "student")
        recompute_topic_mastery("student", "history.pdf", "net")
        original = detect_knowledge_gaps("student")
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DELETE FROM topic_mastery")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(detect_knowledge_gaps("student"), [])
        recompute_topic_mastery("student", "history.pdf", "net")
        rebuilt = detect_knowledge_gaps("student")
        self.assertEqual(rebuilt, original)
        self.assertEqual(rebuilt[0]["topic_name"], "Networking")

    def test_authenticated_api_and_frontend_integration(self):
        user = auth_store.create_user("Alice", "alice@gaps.test", "long-password-123")
        quiz_store.save_topic_mastery(stored_mastery(user["id"], "doc.pdf", "weak", "Weak", 15))
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/knowledge-gaps").status_code, 401)
        with TestClient(app) as client:
            login = client.post("/api/auth/login", json={"email": "alice@gaps.test", "password": "long-password-123"})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(client.get("/api/knowledge-gaps").json()[0]["topic_id"], "weak")
            self.assertEqual(client.get("/api/knowledge-gaps/doc.pdf").status_code, 200)
        markup = Path("frontend/index.html").read_text(encoding="utf-8")
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="overview-knowledge-gaps-list"', markup)
        self.assertIn('/api/knowledge-gaps', script)
        # Knowledge gaps are now presented per Study Session Progress view, not
        # as a standalone Overview panel; API wiring remains the contract here.
        self.assertNotIn('class="panel knowledge-gaps-panel"', markup)


if __name__ == "__main__":
    unittest.main()
