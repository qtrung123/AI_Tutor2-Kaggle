"""PERF-3: the dashboard and study-progress reads are batched without changing what they return.

Each batched read is checked against the per-item read it replaces on the same seeded data
(retakes, several topics and quizzes, a concept-plan switch, another owner sharing document ids),
then the dashboard's query budget is pinned on tests/fake_postgres.py: it no longer grows with
the number of documents, topics or completed attempts.
"""

import io
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, db, indexed_document_store, quiz_store, study_progress
from backend.api import learning
from backend.quiz_service import build_learning_dashboard
from fake_postgres import FakePsycopg
from planner_fixtures import PlannerDatabaseMixin
from test_document_listing_queries import _fake_table_columns


SECRET_URL = "postgresql://alice:s3cr3t-pw@db.private-host.example:5432/tutor?sslmode=require"
TOPICS = ("t1", "t2")


def answer(question_id: int, correct: bool, topic_id: str, plan_id: str = "") -> dict:
    return {
        "question_id": question_id, "question": f"Q{question_id}", "options": ["A", "B", "C", "D"],
        "selected_answer": "A" if correct else "B", "correct_answer": "A", "is_correct": correct,
        "question_difficulty": "easy", "validation_outcome": "accepted",
        "topic_id": topic_id, "topic_name": topic_id.upper(), "concept_plan_id": plan_id,
    }


def submit(owner: str, document_id: str, quiz_id: str, topic_id: str, results: list[dict], completed: bool = True):
    quiz_store.save_quiz_progress(document_id, "easy", {
        "quiz_id": quiz_id,
        "answers": {str(item["question_id"]): item["selected_answer"] for item in results},
        "question_results": results, "score": sum(item["is_correct"] for item in results),
        "answered": len(results), "total": len(results) if completed else len(results) + 1, "completed": completed,
    }, topic_id, owner)


class SeededData:
    """Three documents for alice (one untouched), the same ids for bob with different answers."""

    def seed(self, add_document, add_quiz):
        for owner in (self.alice, self.bob):
            for document_id in ("d1", "d2", "d3"):
                add_document(owner, document_id)
        for owner, flip in ((self.alice, False), (self.bob, True)):
            for document_id in ("d1", "d2"):
                for topic in TOPICS:
                    quiz_id = f"{owner}-{document_id}-{topic}"
                    add_quiz(owner, document_id, quiz_id)
                    submit(owner, document_id, quiz_id, topic,
                           [answer(1, not flip, topic), answer(2, flip, topic), answer(3, True, topic)])
            # A retake (new attempt) of the same quiz: the newer response per question must win.
            quiz_store.reset_quiz_progress("d1", "easy", "t1", owner, quiz_id=f"{owner}-d1-t1")
            submit(owner, "d1", f"{owner}-d1-t1", "t1", [answer(1, flip, "t1"), answer(2, True, "t1")])
        # Concept-planned evidence: only the latest plan's answers count for that topic.
        add_quiz(self.alice, "d2", "alice-planned")
        submit(self.alice, "d2", "alice-planned", "t2", [answer(1, False, "t2", "plan-a")])
        submit(self.alice, "d2", "alice-planned", "t2", [answer(4, True, "t2", "plan-b")])
        # An unfinished attempt is never evidence and never a completed result.
        add_quiz(self.alice, "d3", "alice-open")
        submit(self.alice, "d3", "alice-open", "t1", [answer(1, True, "t1")], completed=False)


class BatchedReadEquivalenceTests(PlannerDatabaseMixin, SeededData, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice, self.bob = self.user("Alice"), self.user("Bob")
        self.seed(self.add_document, lambda owner, document_id, quiz_id: self.add_quiz(owner, document_id, quiz_id, 3))
        self.documents = ["d1", "d2", "d3"]

    def test_batched_snapshots_equal_the_per_topic_read(self):
        for owner in (self.alice, self.bob):
            batched = quiz_store.list_completed_answer_snapshots_by_topic(owner, self.documents)
            for document_id in self.documents:
                for topic in TOPICS:
                    single = quiz_store.list_completed_answer_snapshots(owner, document_id, topic)
                    self.assertEqual(batched.get((document_id, topic), []), single, (owner, document_id, topic))
        # Sanity: the seed exercises ranking (retake) and the concept-plan filter.
        retaken = {row["question_id"]: row["is_correct"]
                   for row in quiz_store.list_completed_answer_snapshots(self.alice, "d1", "t1")}
        self.assertEqual(retaken, {1: 0, 2: 1, 3: 1})
        planned = quiz_store.list_completed_answer_snapshots(self.alice, "d2", "t2")
        self.assertEqual({row["concept_plan_id"] for row in planned}, {"plan-b"})

    def test_batched_completed_attempts_equal_the_document_activity(self):
        for owner in (self.alice, self.bob):
            batched = quiz_store.list_completed_attempts_by_document(owner, self.documents)
            for document_id in self.documents:
                expected = quiz_store.get_document_quiz_activity(document_id, owner)["completed_attempts"]
                self.assertEqual(batched.get(document_id, []), expected)

    def test_batched_latest_percentages_equal_the_study_state(self):
        for owner in (self.alice, self.bob):
            expected = {document_id: study_progress.latest_quiz_percentage(owner, document_id)
                        for document_id in self.documents}
            self.assertEqual(study_progress.latest_quiz_percentages(owner, self.documents), expected)
        self.assertIsNone(study_progress.latest_quiz_percentages(self.alice, self.documents)["d3"])

    def test_quiz_activity_latest_attempt_per_quiz_is_unchanged(self):
        for owner in (self.alice, self.bob):
            for document_id in self.documents:
                for quiz in quiz_store.get_document_quiz_activity(document_id, owner)["quizzes"]:
                    with quiz_store._connect() as connection:
                        row = connection.execute(
                            """SELECT completed, answered, total, score, percentage, updated_at,
                                      COALESCE(completed_at, submitted_at) AS completed_at
                               FROM quiz_attempts WHERE student_id = ? AND quiz_id = ?
                               ORDER BY updated_at DESC LIMIT 1""",
                            (owner, quiz["quiz_id"]),
                        ).fetchone()
                    self.assertEqual(quiz["latest_attempt"], dict(row) if row else None)

    def test_batched_quiz_history_equals_per_attempt_rows(self):
        for owner in (self.alice, self.bob):
            history = quiz_store.list_quiz_history(student_id=owner, include_practice=False)
            with quiz_store._connect() as connection:
                expected = [quiz_store._row_to_attempt(connection, connection.execute(
                    "SELECT * FROM quiz_attempts WHERE attempt_id = ?", (item["attempt_id"],)).fetchone())
                    for item in history]
            self.assertEqual(history, expected)
            self.assertTrue(all(item["question_results"] for item in history))

    def test_get_indexed_document_is_owner_scoped(self):
        self.assertEqual(indexed_document_store.get_indexed_document(self.alice, "d1"),
                         next(d for d in indexed_document_store.list_indexed_documents(self.alice) if d["document_id"] == "d1"))
        self.assertIsNone(indexed_document_store.get_indexed_document(self.alice, "missing"))
        self.assertEqual(indexed_document_store.get_indexed_document(self.bob, "d1")["owner_id"], self.bob)

    def test_dashboard_response_matches_the_per_item_computation(self):
        dashboard = build_learning_dashboard(self.alice)
        from backend.mastery_service import calculate_mastery
        for row in dashboard["mastery"]:
            history = quiz_store.list_completed_answer_snapshots(self.alice, row["document_id"], row["topic_id"])
            expected = calculate_mastery(history, len({item["quiz_id"] for item in history}))
            self.assertEqual({key: row[key] for key in expected}, expected)
        self.assertEqual(dashboard["metrics"]["total_topics"], 3)  # the planner fixture gives one topic per document
        self.assertEqual(dashboard["metrics"]["answered_questions"],
                         sum(len(a["question_results"]) for a in quiz_store.list_quiz_history(
                             student_id=self.alice, include_practice=False)))


class DashboardQueryBudgetTests(SeededData, unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.backing = root / "postgres.db"
        self.fake = FakePsycopg(self.backing, reuse_connections=True)
        self.stack = ExitStack()
        from backend import flashcard_store, study_planner_store, summary_store
        for module in (auth_store, indexed_document_store, quiz_store, summary_store, flashcard_store, study_planner_store):
            self.stack.enter_context(patch.object(module, "DATABASE_PATH", root / "local.db"))
        self.stack.enter_context(patch.object(db, "_announced", set()))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.stack.enter_context(self.fake.installed(SECRET_URL))
        self.stack.enter_context(patch.object(db, "table_columns", _fake_table_columns))
        self.addCleanup(db.close_pools)
        self.alice, self.bob = "alice", "bob"
        quiz_store.initialize_quiz_store()
        indexed_document_store.initialize_indexed_document_store()
        for user in (self.alice, self.bob):
            with db.connect("unused.db") as connection:
                connection.execute("INSERT INTO users (id, display_name, email, password_hash, created_at) "
                                   "VALUES (?, ?, ?, 'x', '2026-10-15T00:00:00+00:00')", (user, user, f"{user}@example.com"))

    def tearDown(self):
        db.close_pools()
        self.stack.close()
        self.temp_dir.cleanup()

    def add_document(self, owner, document_id):
        indexed_document_store.upsert_indexed_document(owner, document_id, {
            "hash": "h", "chunks": 3, "path": "x", "topic_schema_version": 3,
            "topics": [{"topic_id": topic, "name": topic.upper()} for topic in TOPICS],
        })

    def add_quiz(self, owner, document_id, quiz_id):
        with db.connect("unused.db") as connection:
            connection.execute(
                "INSERT INTO quizzes (quiz_id, owner_id, document_id, title, difficulty, question_count, created_at, "
                "topic_id, topic_schema_version) VALUES (?, ?, ?, 'Quiz', 'easy', 3, '2026-10-15T00:00:00+00:00', ?, 3)",
                (quiz_id, owner, document_id, quiz_id),
            )

    def dashboard_cost(self) -> tuple[int, int]:
        self.fake.statements.clear()
        self.fake.round_trips.clear()
        units = []
        original = db.connect
        with patch.object(db, "connect", side_effect=lambda *a, **k: units.append(1) or original(*a, **k)):
            dashboard = build_learning_dashboard(self.alice)
            study_progress.latest_quiz_percentages(self.alice, [row["document_id"] for row in dashboard["materials"]])
        return len(units), len(self.fake.statements)

    def test_dashboard_cost_does_not_grow_with_documents_topics_or_attempts(self):
        self.seed(self.add_document, self.add_quiz)
        small = self.dashboard_cost()
        for index in range(7):  # 7 more documents with topics, quizzes and completed attempts
            document_id = f"extra-{index}"
            self.add_document(self.alice, document_id)
            for topic in TOPICS:
                self.add_quiz(self.alice, document_id, f"{document_id}-{topic}")
                submit(self.alice, document_id, f"{document_id}-{topic}", topic, [answer(1, True, topic), answer(2, False, topic)])
        large = self.dashboard_cost()
        self.assertEqual(small, large)
        units, statements = large
        # list documents + staleness check, snapshots, quiz history (+ its answers), latest percentages
        self.assertEqual((units, statements), (5, 6))

    def test_dashboard_api_uses_one_read_for_quiz_performance(self):
        self.seed(self.add_document, self.add_quiz)
        with patch.object(study_progress, "latest_quiz_percentage", side_effect=AssertionError("per-document read")):
            from fastapi.testclient import TestClient
            from backend.main import app
            app.dependency_overrides[learning.require_current_user] = lambda: {"id": self.alice}
            try:
                response = TestClient(app).get("/api/dashboard")
            finally:
                app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200, response.text)
        performance = response.json()["metrics"]["current_quiz_performance"]
        self.assertEqual((performance["assessed_documents"], performance["documents"]), (2, 3))


if __name__ == "__main__":
    unittest.main()
