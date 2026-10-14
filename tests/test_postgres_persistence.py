"""Saved relational data survives a backend restart and stays isolated per user, on every backend.

The scenarios always run against SQLite. Set AI_TUTOR_TEST_POSTGRES_URL=postgresql://... to run the
same scenarios against a real PostgreSQL server as well. Each PostgreSQL test works in its own
throw-away schema (ai_tutor_test_<random>) that it creates and drops itself, so existing tables in
that database are never read, changed or dropped.
"""

import io
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from backend import (auth_store, conversation_store, db, flashcard_store, google_calendar_store,
                     indexed_document_store, quiz_store, study_planner_store, study_preferences, summary_jobs,
                     summary_store)
from backend.mastery_service import recompute_topic_mastery


POSTGRES_TEST_URL = os.getenv("AI_TUTOR_TEST_POSTGRES_URL", "").strip()
STORES = (auth_store, conversation_store, indexed_document_store, summary_store, summary_jobs,
          flashcard_store, quiz_store, study_planner_store)
PASSWORD = "correct-horse-123"
DOC = "lecture-01.pdf"
TOPIC = "topic_001"


def with_search_path(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def sample_quiz(quiz_id: str) -> dict:
    return {
        "quiz_id": quiz_id, "document_id": DOC, "title": "Week 1", "difficulty": "easy",
        "topic_id": TOPIC, "topic_name": "Networking",
        "questions": [{
            "id": index, "question": f"Which grounded statement {index} is correct?",
            "options": ["A. One", "B. Two", "C. Three", "D. Four"], "correct_answer": "A",
            "topic_id": TOPIC, "topic_name": "Networking", "difficulty": "easy",
            "explanation": "Supported by the source.", "source_chunk_ids": [f"hash_{index}"],
            "concept_id": f"concept_{index}",
        } for index in range(1, 4)],
    }


def answer(question_id: int, correct: bool) -> dict:
    return {
        "question_id": question_id, "question": f"Question {question_id}", "options": ["A", "B"],
        "selected_answer": "A" if correct else "B", "correct_answer": "A", "is_correct": correct,
        "question_difficulty": "easy", "validation_outcome": "accepted", "topic_id": TOPIC,
        "topic_name": "Networking", "concept_id": f"concept_{question_id}", "assessment_capacity": 3,
        "evidence_requirement_version": "concept_coverage_v1",
    }


class PersistenceScenarios:
    """Backend-independent scenarios; subclasses pick the backend in connect_backend()."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.local_db = Path(self.temp_dir.name) / "data" / "conversations.db"
        self.stack = ExitStack()
        for store in STORES:
            self.stack.enter_context(patch.object(store, "DATABASE_PATH", self.local_db))
        self.stack.enter_context(patch.object(db, "_schema_ready", set()))
        self.stack.enter_context(patch.object(summary_jobs, "_live_jobs", set()))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.connect_backend()

    def tearDown(self):
        self.stack.close()
        db.close_pools()
        self.temp_dir.cleanup()

    def restart_backend(self):
        """A new backend process: no pooled connections, no schema cache, no live summary workers."""
        db.close_pools()
        db._schema_ready.clear()
        summary_jobs._live_jobs.clear()
        self.after_restart()

    # -- helpers --------------------------------------------------------------------------------

    def save_everything(self, email: str) -> dict:
        """One user saves data in every relational domain; returns what to look for afterwards."""
        user = auth_store.create_user("Learner", email, PASSWORD)
        owner = user["id"]
        token, _session = auth_store.create_session(owner)
        indexed_document_store.upsert_indexed_document(owner, DOC, {
            "display_name": "Lecture 1", "hash": "hash-1", "chunks": 12, "path": f"data/users/{owner}/{DOC}",
            "topic_schema_version": 3, "topics": [{"topic_id": TOPIC, "name": "Networking"}],
        })
        conversation = conversation_store.create_conversation(owner, "Study chat", [DOC])
        conversation_store.add_message(owner, conversation["id"], "user", "What is 50% of a subnet?")
        conversation_store.add_message(owner, conversation["id"], "assistant", "Half of it.", "grounded",
                                       citations=[{"document_id": DOC, "page": 4, "chunk": 2, "content": "subnet"}])
        summary_identity = {"owner_id": owner, "document_id": DOC, "document_hash": "hash-1",
                            "topic_schema_version": 3, "summary_version": "summary_v2",
                            "model_id": "qwen-2.5-7b", "runtime_model": "qwen2.5:7b"}
        summary = summary_store.save_summary(summary_identity, [{"topic_id": TOPIC, "summary": "Subnets."}],
                                             {"overview": "Networking basics"}, {"seconds": 1.5})
        flashcard_identity = {"owner_id": owner, "document_id": DOC, "document_hash": "hash-1",
                              "topic_schema_version": 3, "flashcard_version": "flashcards_v3",
                              "model_id": "qwen-2.5-7b", "runtime_model": "qwen2.5:7b", "topic_ids": [TOPIC],
                              "flashcard_language": "auto"}
        flashcards = flashcard_store.save_flashcards(flashcard_identity, [
            {"topic_id": TOPIC, "topic_name": "Networking", "front": "Subnet?", "back": "A network slice."},
            {"topic_id": TOPIC, "topic_name": "Networking", "front": "CIDR?", "back": "Prefix notation."},
        ])
        favorite = flashcard_store.update_flashcard(owner, DOC, flashcards["cards"][0]["flashcard_id"], {"is_favorite": True})
        quiz_id = f"quiz-{uuid4().hex[:8]}"
        quiz_store.save_quiz(DOC, "easy", sample_quiz(quiz_id), owner)
        quiz_store.save_quiz_progress(DOC, "easy", {
            "quiz_id": quiz_id, "answers": {}, "question_results": [answer(1, True), answer(2, True), answer(3, False)],
            "score": 2, "answered": 3, "total": 3, "completed": True,
        }, TOPIC, owner)
        mastery = recompute_topic_mastery(owner, DOC, TOPIC)
        plan = study_planner_store.create_plan(owner, "Finals")
        study_planner_store.add_material(owner, plan["plan_id"], DOC, deadline="2026-12-01", goal_type="deadline",
                                         target_date="2026-11-25")
        study_planner_store.add_availability(owner, "18:00", "20:00", date="2026-11-02")
        confirmed = study_planner_store.confirm_plan_sessions(owner, plan["plan_id"], [
            {"document_id": DOC, "activity_type": "summary", "scheduled_start": "2026-11-02T18:00:00",
             "scheduled_end": "2026-11-02T18:45:00", "duration_minutes": 45, "priority_snapshot": 1.5},
            {"document_id": DOC, "activity_type": "quiz", "scheduled_start": "2026-11-03T18:00:00",
             "scheduled_end": "2026-11-03T18:30:00", "duration_minutes": 30},
        ])
        started, changed = study_planner_store.transition_session(owner, confirmed["sessions"][0]["session_id"], "start")
        self.assertTrue(changed)
        study_preferences.save_preferences(owner, {"daily_target_minutes": 120, "break_minutes": 10,
                                                   "strategy": "deadline_priority", "durations": {"summary": 60}})
        study_planner_store.reschedule_session(owner, confirmed["sessions"][1]["session_id"], "2026-11-04T18:00:00",
                                               "2026-11-04T18:40:00", duration_minutes=40, manual=True)
        google_calendar_store.save_connection(owner, "encrypted-refresh-token", "calendar.events")
        google_calendar_store.update_connection(owner, {"avoid_conflicts": False, "study_calendar_id": "cal-1"})
        google_calendar_store.save_event_link(owner, started["session_id"], "cal-1", "event-1",
                                              synced_hash="h", sync_status="synced")
        google_calendar_store.save_event_link(owner, confirmed["sessions"][1]["session_id"], "cal-1", "event-2",
                                              synced_hash=None, sync_status="error", last_error="quota")
        with patch.object(summary_jobs._executor, "submit"):  # never run the LLM in tests
            job, created = summary_jobs.start_summary_job(owner, DOC, "qwen-2.5-7b")
        self.assertTrue(created)
        return {"user": user, "token": token, "conversation_id": conversation["id"], "summary": summary,
                "flashcards": flashcards, "favorite": favorite, "quiz_id": quiz_id, "mastery": mastery,
                "plan_id": plan["plan_id"], "started_session": started, "job_id": job["job_id"],
                "summary_identity": summary_identity, "flashcard_identity": flashcard_identity}

    def assert_everything_readable(self, saved: dict) -> None:
        owner = saved["user"]["id"]
        self.assertEqual(indexed_document_store.get_indexed_document(owner, DOC)["chunks"], 12)
        conversation = conversation_store.get_conversation(owner, saved["conversation_id"])
        self.assertEqual([message["role"] for message in conversation["messages"]], ["user", "assistant"])
        self.assertEqual(conversation["messages"][0]["content"], "What is 50% of a subnet?")
        self.assertEqual(conversation["messages"][1]["citations"],
                         [{"document_id": DOC, "page": "4", "chunk": "2", "content": "subnet"}])
        summary = summary_store.get_compatible_summary(saved["summary_identity"])
        self.assertEqual(summary, saved["summary"])
        self.assertEqual(summary["metrics"], {"seconds": 1.5})
        cached = flashcard_store.get_compatible_flashcards(saved["flashcard_identity"])
        self.assertEqual(cached["set_id"], saved["flashcards"]["set_id"])
        self.assertEqual([card["is_favorite"] for card in cached["cards"]], [True, False])
        quiz = quiz_store.get_quiz_by_id(saved["quiz_id"], owner)
        self.assertEqual(len(quiz["questions"]), 3)
        history = quiz_store.list_quiz_history(document_id=DOC, student_id=owner)
        self.assertEqual([(item["quiz_id"], item["score"], item["total"]) for item in history],
                         [(saved["quiz_id"], 2, 3)])
        summary_stats = quiz_store.get_quiz_attempt_summary(saved["quiz_id"], owner)
        self.assertEqual(summary_stats["attempts"], 1)
        self.assertIsInstance(summary_stats["average_score"], float)
        self.assertAlmostEqual(summary_stats["average_score"], 66.67, places=2)
        mastery = quiz_store.get_topic_mastery(owner, DOC, TOPIC)
        self.assertAlmostEqual(mastery["mastery_score"], saved["mastery"]["mastery_score"], places=6)
        self.assertEqual(mastery["answered_questions"], 3)
        self.assertEqual([plan["plan_id"] for plan in study_planner_store.list_plans(owner)], [saved["plan_id"]])
        materials = study_planner_store.list_materials(owner, saved["plan_id"])
        self.assertEqual([(m["goal_type"], m["deadline"], m["target_date"]) for m in materials],
                         [("deadline", "2026-12-01", "2026-11-25")])
        preferences = study_preferences.get_preferences(owner)
        self.assertEqual((preferences["saved"], preferences["daily_target_minutes"], preferences["break_minutes"],
                          preferences["strategy"], preferences["durations"]["summary"]), (True, 120, 10, "deadline_priority", 60))
        sessions = study_planner_store.list_sessions(owner, plan_id=saved["plan_id"])
        self.assertEqual([session["status"] for session in sessions], ["in_progress", "rescheduled", "scheduled"])
        self.assertEqual((sessions[2]["duration_minutes"], sessions[2]["manual_edit"]), (40, True))
        self.assertEqual(sessions[0]["priority_snapshot"], 1.5)
        self.assertEqual(len(study_planner_store.list_availability(owner)), 1)
        connection = google_calendar_store.get_connection(owner)
        self.assertEqual((connection["avoid_conflicts"], connection["sync_sessions"]), (False, True))
        self.assertEqual(google_calendar_store.link_summary(owner)["errors"], 1)
        self.assertEqual(google_calendar_store.known_calendar_ids(owner), ["cal-1"])

    # -- scenarios ------------------------------------------------------------------------------

    def test_saved_data_survives_a_backend_restart(self):
        saved = self.save_everything("alice@example.com")
        self.assert_everything_readable(saved)
        self.restart_backend()

        owner = saved["user"]["id"]
        self.assertEqual(auth_store.authenticate_user("ALICE@example.com", PASSWORD)["id"], owner)
        self.assertEqual(auth_store.get_user_for_session(saved["token"])["id"], owner)
        with self.assertRaisesRegex(ValueError, "already exists"):
            auth_store.create_user("Alice again", "alice@example.com", PASSWORD)
        self.assert_everything_readable(saved)
        # The job's worker died with the old process: it is reported interrupted, not stuck.
        job = summary_jobs.get_summary_job(owner, saved["job_id"])
        self.assertEqual((job["status"], job["error_code"]), ("failed", "interrupted"))

    def test_users_only_ever_see_their_own_data(self):
        alice = self.save_everything("alice@example.com")
        bob = auth_store.create_user("Bob", "bob@example.com", PASSWORD)["id"]
        self.restart_backend()

        self.assertEqual(indexed_document_store.list_indexed_documents(bob), [])
        self.assertEqual(conversation_store.list_conversations(bob), [])
        with self.assertRaises(ValueError):
            conversation_store.get_conversation(bob, alice["conversation_id"])
        self.assertIsNone(summary_store.get_summary_by_id(bob, alice["summary"]["summary_id"]))
        self.assertEqual(flashcard_store.list_flashcards(bob, DOC), [])
        self.assertIsNone(quiz_store.get_quiz_by_id(alice["quiz_id"], bob))
        self.assertEqual(quiz_store.list_quiz_history(student_id=bob), [])
        self.assertEqual(quiz_store.list_topic_mastery(bob), [])
        self.assertEqual(study_planner_store.list_plans(bob), [])
        self.assertIsNone(study_planner_store.get_session(bob, alice["started_session"]["session_id"]))
        self.assertIsNone(google_calendar_store.get_connection(bob))
        self.assertIsNone(summary_jobs.get_summary_job(bob, alice["job_id"]))
        with self.assertRaises(ValueError):
            flashcard_store.update_flashcard(bob, DOC, alice["favorite"]["flashcard_id"], {"front": "stolen"})
        # Bob's attempts at Alice's records changed nothing.
        self.assert_everything_readable(alice)

    def test_schema_initialization_is_idempotent_across_restarts(self):
        saved = self.save_everything("alice@example.com")
        for _ in range(2):
            self.restart_backend()
            for initialize in (auth_store.initialize_auth_store, conversation_store.initialize_conversation_store,
                               indexed_document_store.initialize_indexed_document_store,
                               summary_store.initialize_summary_store, summary_jobs.initialize_summary_job_store,
                               flashcard_store.initialize_flashcard_store, quiz_store.initialize_quiz_store,
                               study_planner_store.initialize_study_planner_store,
                               google_calendar_store.initialize_google_calendar_store):
                initialize()
                initialize()
        self.assert_everything_readable(saved)


class SQLitePersistenceTests(PersistenceScenarios, unittest.TestCase):
    def connect_backend(self):
        environment = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))

    def after_restart(self):
        self.assertTrue(self.local_db.exists())  # local development keeps its file


@unittest.skipUnless(POSTGRES_TEST_URL, "set AI_TUTOR_TEST_POSTGRES_URL to run against a real PostgreSQL")
class PostgresPersistenceTests(PersistenceScenarios, unittest.TestCase):
    def connect_backend(self):
        import psycopg

        self.schema = f"ai_tutor_test_{uuid4().hex[:12]}"
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(self.drop_schema)
        self.stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": with_search_path(POSTGRES_TEST_URL, self.schema)}))

    def drop_schema(self):
        import psycopg

        db.close_pools()
        assert self.schema.startswith("ai_tutor_test_")  # only ever the schema this test created
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")

    def after_restart(self):
        # Kaggle's disk is wiped between sessions; with PostgreSQL nothing was written to it.
        self.assertFalse(self.local_db.exists())


if __name__ == "__main__":
    unittest.main()
