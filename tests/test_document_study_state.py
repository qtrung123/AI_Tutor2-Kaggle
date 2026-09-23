"""DocumentStudyState reader, learning-state derivation, and scheduler contracts."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from backend import (
    auth_store, flashcard_store, indexed_document_store, quiz_store, study_planner_service,
    study_planner_store, summary_store,
)
from backend.document_study_state import (
    NEEDS_REVIEW_BELOW_PERCENT, ON_TRACK_FROM_PERCENT, STATE_REASON_LOW_QUIZ_SCORE, FlashcardState,
    QuizAttemptResult, QuizState, StateReason, SummaryState, derive_learning_state, get_document_study_state,
)
from backend.flashcard_service import FLASHCARD_VERSION
from backend.study_planner_store import SESSION_REASONS
from backend.study_scheduler_contracts import (
    REASON_LOW_QUIZ_SCORE, REASON_REVIEW_DUE, SCHEDULING_REASON_CODES, CandidateActivity, ProposedSession,
    SchedulingContext, SchedulingReason,
)
from backend.summary_service import SUMMARY_VERSION

HASH, SCHEMA = "hash-1", 3


class DocumentStudyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        db = root / "state.db"
        self.patches = [
            patch.object(module, "DATABASE_PATH", db)
            for module in (auth_store, study_planner_store, indexed_document_store, summary_store,
                           flashcard_store, quiz_store)
        ] + [
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", root / "missing-quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", root / "missing-attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", root / "missing-explanations.json"),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "missing-indexed.json"),
        ]
        for item in self.patches:
            item.start()
        self.alice = auth_store.create_user("Alice", "alice-state@example.com", "long-password-a")["id"]
        self.bob = auth_store.create_user("Bob", "bob-state@example.com", "long-password-b")["id"]
        self.add_document(self.alice, "doc-a", "Biology")
        self.clock = 0

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    # -- fixtures --------------------------------------------------------------

    def tick(self) -> str:
        self.clock += 1
        return f"2026-09-2{self.clock // 60}T10:{self.clock % 60:02d}:00+00:00"

    def add_document(self, owner, document_id, title="Doc", document_hash=HASH):
        indexed_document_store.upsert_indexed_document(owner, document_id, {
            "display_name": title, "hash": document_hash, "chunks": 12, "path": "x",
            "topic_schema_version": SCHEMA, "topics": [{"topic_id": "t1", "name": "T1"}],
        })

    def add_summary(self, owner, document_id, document_hash=HASH, version=SUMMARY_VERSION):
        summary_store.save_summary({
            "owner_id": owner, "document_id": document_id, "document_hash": document_hash,
            "topic_schema_version": SCHEMA, "summary_version": version, "model_id": "m1", "runtime_model": "r1",
        }, [], {}, {})

    def add_flashcards(self, owner, document_id, count, document_hash=HASH):
        return flashcard_store.save_flashcards({
            "owner_id": owner, "document_id": document_id, "document_hash": document_hash,
            "topic_schema_version": SCHEMA, "flashcard_version": FLASHCARD_VERSION, "model_id": "m1",
            "runtime_model": "r1", "topic_ids": ["t1"],
        }, [{"topic_id": "t1", "topic_name": "T1", "front": f"Q{i}", "back": f"A{i}"} for i in range(count)])

    def add_quiz(self, owner, document_id, quiz_id, title="Quiz"):
        quiz_store.initialize_quiz_store()
        with quiz_store._connect() as connection:
            connection.execute(
                """INSERT INTO quizzes (quiz_id, document_id, title, difficulty, question_count, created_at, owner_id)
                   VALUES (?, ?, ?, 'easy', 10, ?, ?)""",
                (quiz_id, document_id, title, self.tick(), owner),
            )

    def add_attempt(self, owner, document_id, quiz_id, score=0, answered=0, completed=False, total=10):
        stamp = self.tick()
        with quiz_store._connect() as connection:
            connection.execute("UPDATE quiz_attempts SET is_latest=0 WHERE quiz_id=? AND student_id=?", (quiz_id, owner))
            connection.execute(
                """INSERT INTO quiz_attempts (attempt_id, quiz_id, document_id, difficulty, started_at, updated_at,
                   completed_at, score, answered, total, completed, is_latest, student_id, percentage)
                   VALUES (?, ?, ?, 'easy', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (f"att-{stamp}", quiz_id, document_id, stamp, stamp, stamp if completed else None, score, answered,
                 total, int(completed), owner, round(100.0 * score / total, 2) if completed and total else 0),
            )

    def state(self, owner=None, document_id="doc-a", plan_id=None):
        return get_document_study_state(owner or self.alice, document_id, plan_id=plan_id)

    # -- reader ----------------------------------------------------------------

    def test_new_document(self):
        state = self.state()
        self.assertEqual((state.title, state.chunk_count, state.learning_state), ("Biology", 12, "new"))
        self.assertEqual(state.state_reason.code, "no_study_activity")
        self.assertFalse(state.summary.available)
        self.assertEqual((state.flashcards.available, state.flashcards.card_count), (False, 0))
        self.assertEqual((state.quiz.quiz_count, state.quiz.latest_quiz_status, state.quiz.latest_completed),
                         (0, None, None))
        self.assertIsNone(state.last_activity_at)
        self.assertNotIn("topic", str(state.to_dict().keys()))

    def test_summary_only(self):
        self.add_summary(self.alice, "doc-a")
        state = self.state()
        self.assertTrue(state.summary.available)
        self.assertEqual(state.summary.model_id, "m1")
        self.assertEqual((state.learning_state, state.state_reason.code, state.state_reason.metadata["evidence"]),
                         ("learning", "study_started", ["summary"]))

    def test_flashcards_only_with_count_and_no_review_data(self):
        cards = self.add_flashcards(self.alice, "doc-a", 3)
        flashcard_store.delete_flashcard(self.alice, "doc-a", cards["cards"][0]["flashcard_id"])
        state = self.state()
        self.assertEqual((state.flashcards.available, state.flashcards.card_count), (True, 2))
        self.assertEqual(state.flashcards.set_id, cards["set_id"])
        self.assertFalse(state.flashcards.review_data_available)
        self.assertIsNone(state.flashcards.review_stats)
        self.assertFalse(state.summary.available)
        self.assertEqual((state.learning_state, state.state_reason.metadata["evidence"]), ("learning", ["flashcards"]))

    def test_quiz_generated_but_not_taken(self):
        self.add_quiz(self.alice, "doc-a", "q1", title="Chapter 1")
        state = self.state()
        self.assertEqual((state.quiz.quiz_count, state.quiz.latest_quiz_id, state.quiz.latest_quiz_title,
                          state.quiz.latest_quiz_status), (1, "q1", "Chapter 1", "not_started"))
        self.assertEqual((state.quiz.completed_attempt_count, state.quiz.latest_completed), (0, None))
        self.assertEqual((state.learning_state, state.state_reason.code), ("learning", "study_started"))

    def test_in_progress_quiz(self):
        self.add_quiz(self.alice, "doc-a", "q1")
        self.add_attempt(self.alice, "doc-a", "q1", answered=4)
        state = self.state()
        self.assertEqual((state.quiz.latest_quiz_status, state.quiz.in_progress_count), ("in_progress", 1))
        self.assertEqual((state.learning_state, state.state_reason.code), ("learning", "quiz_in_progress"))
        self.assertIsNotNone(state.last_activity_at)

    def test_low_completed_quiz_score_needs_review(self):
        self.add_quiz(self.alice, "doc-a", "q1")
        self.add_attempt(self.alice, "doc-a", "q1", score=5, answered=10, completed=True)
        state = self.state()
        self.assertEqual((state.learning_state, state.state_reason.code), ("needs_review", STATE_REASON_LOW_QUIZ_SCORE))
        self.assertEqual(state.state_reason.metadata["percentage"], 50.0)
        self.assertEqual(state.quiz.latest_completed.score, 5)
        self.assertEqual(state.quiz.latest_quiz_status, "completed")

    def test_high_and_moderate_completed_quiz_scores(self):
        self.add_quiz(self.alice, "doc-a", "q1")
        self.add_attempt(self.alice, "doc-a", "q1", score=9, answered=10, completed=True)
        self.assertEqual((self.state().learning_state, self.state().state_reason.code), ("on_track", "strong_quiz_score"))
        self.add_attempt(self.alice, "doc-a", "q1", score=7, answered=10, completed=True)  # newer, 70%
        state = self.state()
        self.assertEqual((state.learning_state, state.state_reason.code), ("learning", "moderate_quiz_score"))
        self.assertEqual(state.quiz.completed_attempt_count, 2)

    def test_sibling_quizzes_and_exact_document_ownership(self):
        self.add_document(self.alice, "doc-b")
        self.add_quiz(self.alice, "doc-a", "q-old")
        self.add_attempt(self.alice, "doc-a", "q-old", score=9, answered=10, completed=True)
        self.add_quiz(self.alice, "doc-a", "q-new")  # sibling, newer, untouched
        self.add_quiz(self.alice, "doc-b", "q-b")    # other document, low score
        self.add_attempt(self.alice, "doc-b", "q-b", score=1, answered=10, completed=True)
        self.add_quiz(self.bob, "doc-a", "q-bob")    # other owner, same document id
        self.add_attempt(self.bob, "doc-a", "q-bob", score=0, answered=10, completed=True)
        # An attempt row whose quiz belongs to another document must never leak in.
        self.add_attempt(self.alice, "doc-a", "q-b", score=0, answered=10, completed=True)

        state = self.state()
        self.assertEqual((state.quiz.quiz_count, state.quiz.latest_quiz_id, state.quiz.latest_quiz_status),
                         (2, "q-new", "not_started"))
        self.assertEqual((state.quiz.completed_attempt_count, state.quiz.latest_completed.quiz_id), (1, "q-old"))
        self.assertEqual(state.learning_state, "on_track")
        self.assertEqual(self.state(document_id="doc-b").learning_state, "needs_review")

    def test_missing_or_stale_artifact_data_is_handled_safely(self):
        self.assertIsNone(self.state(document_id="no-such-doc"))
        self.add_summary(self.alice, "doc-a", document_hash="old-hash")        # document re-uploaded since
        self.add_summary(self.alice, "doc-a", version="old_summary_format")    # format no longer served
        self.add_flashcards(self.alice, "doc-a", 5, document_hash="old-hash")
        self.add_flashcards(self.alice, "doc-a", 0)                            # set with no cards left
        self.add_quiz(self.alice, "doc-a", "q1")
        self.add_attempt(self.alice, "doc-a", "q1", score=0, answered=0, completed=True, total=0)  # empty quiz
        with quiz_store._connect() as connection:  # attempt timestamps missing entirely
            connection.execute("UPDATE quiz_attempts SET completed_at=NULL, updated_at='not-a-date'")
        state = self.state()
        self.assertFalse(state.summary.available)
        self.assertEqual((state.flashcards.available, state.flashcards.card_count), (False, 0))
        self.assertEqual((state.quiz.latest_completed, state.quiz.completed_attempt_count), (None, 0))
        self.assertEqual((state.learning_state, state.state_reason.metadata["evidence"]), ("learning", ["quiz"]))
        self.assertIsNone(state.last_activity_at)

    def test_user_isolation(self):
        self.add_summary(self.alice, "doc-a")
        self.add_flashcards(self.alice, "doc-a", 4)
        self.add_quiz(self.alice, "doc-a", "q1")
        self.add_attempt(self.alice, "doc-a", "q1", score=9, answered=10, completed=True)
        self.assertIsNone(self.state(owner=self.bob))  # Bob does not own doc-a
        self.add_document(self.bob, "doc-a", "Bob's copy")
        bobs = self.state(owner=self.bob)
        self.assertEqual((bobs.title, bobs.learning_state), ("Bob's copy", "new"))
        self.assertFalse(bobs.summary.available or bobs.flashcards.available)
        self.assertEqual(bobs.quiz.quiz_count, 0)

    def test_planner_material_state_and_completed_sessions(self):
        plan = study_planner_store.create_plan(self.alice, "Finals")
        material = study_planner_store.add_material(self.alice, plan["plan_id"], "doc-a")
        session = study_planner_store.create_session(self.alice, plan["plan_id"], {
            "document_id": "doc-a", "activity_type": "summary", "scheduled_start": "2026-09-24T18:00:00",
            "scheduled_end": "2026-09-24T19:00:00", "duration_minutes": 60,
        })
        self.assertEqual(self.state(plan_id=plan["plan_id"]).planner_learning_state, "new")
        study_planner_store.update_session(self.alice, session["session_id"], {"status": "completed"})
        state = self.state(plan_id=plan["plan_id"])
        self.assertEqual((state.completed_session_count, state.learning_state), (1, "learning"))
        self.assertEqual(state.state_reason.metadata["evidence"], ["study_session"])
        self.assertEqual(state.last_activity_at, state.last_session_at)

        study_planner_store.update_material(self.alice, material["material_id"], {"learning_state": "completed"})
        self.assertEqual(self.state(plan_id=plan["plan_id"]).learning_state, "completed")
        self.assertEqual(self.state().learning_state, "learning")  # no plan context -> never "completed"
        self.assertIsNone(self.state(owner=self.alice, plan_id="someone-elses-plan").planner_learning_state)

    # -- derivation (pure) -----------------------------------------------------

    def test_derivation_is_deterministic_and_conservative(self):
        none = (SummaryState(False), FlashcardState(False), QuizState())

        def completed(percentage):
            return QuizState(quiz_count=1, completed_attempt_count=1, latest_completed=QuizAttemptResult(
                "a", "q", 0, 10, percentage, None))

        self.assertEqual(derive_learning_state(*none, 0, None)[0], "new")
        self.assertEqual(derive_learning_state(*none, 0, "needs_review")[0], "new")  # stored non-final state ignored
        self.assertEqual(derive_learning_state(*none, 0, "completed")[0], "completed")
        cases = {
            NEEDS_REVIEW_BELOW_PERCENT - 0.01: "needs_review", NEEDS_REVIEW_BELOW_PERCENT: "learning",
            ON_TRACK_FROM_PERCENT - 0.01: "learning", ON_TRACK_FROM_PERCENT: "on_track", 100.0: "on_track",
        }
        for percentage, expected in cases.items():
            self.assertEqual(derive_learning_state(none[0], none[1], completed(percentage), 0, None)[0], expected)
        # Perfect score never implies "completed" on its own.
        self.assertNotEqual(derive_learning_state(none[0], none[1], completed(100.0), 5, None)[0], "completed")
        self.assertEqual(derive_learning_state(*none, 0, None), derive_learning_state(*none, 0, None))

    # -- scheduler contracts ---------------------------------------------------

    def test_scheduler_contracts_validate_and_map_to_session_records(self):
        self.assertEqual(SCHEDULING_REASON_CODES, SESSION_REASONS)
        self.assertEqual(set(SESSION_REASONS), {
            "new_material", "deadline_approaching", "review_due", "low_quiz_score", "flashcard_review_due",
            "final_review", "quiz_in_progress", "rescheduled",
        })
        reason = SchedulingReason(REASON_LOW_QUIZ_SCORE, "Latest quiz score is low.", {"percentage": 50.0})
        for state_only_code in ("study_started", "marked_completed", "strong_quiz_score", "made_up_code"):
            with self.assertRaises(ValueError):
                SchedulingReason(state_only_code, "x")
        with self.assertRaises(ValueError):  # and the reverse: scheduling-only codes are not state reasons
            StateReason(REASON_REVIEW_DUE, "x")
        candidate = CandidateActivity("doc-a", "quiz_retry", reason, artifact_id="q1", estimated_minutes=20)
        self.assertEqual(candidate.activity_type, "quiz_retry")
        for bad in ({"activity_type": "reading"}, {"estimated_minutes": 0}, {"estimated_minutes": True}):
            with self.assertRaises(ValueError):
                CandidateActivity(**{"document_id": "doc-a", "activity_type": "review", "reason": reason, **bad})

        proposal = ProposedSession("doc-a", "review", "2026-09-24T18:00:00", "2026-09-24T18:30:00", 30, reason,
                                   priority_snapshot=1.5)
        with self.assertRaises(ValueError):
            ProposedSession("doc-a", "review", "2026-09-24T18:00:00", "2026-09-24T18:30:00", 45, reason)
        plan = study_planner_store.create_plan(self.alice, "Finals")
        study_planner_store.add_material(self.alice, plan["plan_id"], "doc-a")
        saved = study_planner_store.create_sessions(self.alice, plan["plan_id"], [proposal.to_session_record()])[0]
        self.assertEqual((saved["reason"], saved["priority_snapshot"], saved["status"]),
                         (REASON_LOW_QUIZ_SCORE, 1.5, "scheduled"))

    def test_build_scheduling_context(self):
        self.add_document(self.alice, "doc-b")
        plan = study_planner_store.create_plan(self.alice, "Finals")
        study_planner_store.add_material(self.alice, plan["plan_id"], "doc-a", deadline="2026-10-01")
        study_planner_store.add_material(self.alice, plan["plan_id"], "doc-b")
        study_planner_store.add_material(self.alice, plan["plan_id"], "doc-gone")  # document since deleted
        study_planner_store.add_availability(self.alice, "18:00", "20:00", date="2026-09-24")
        past, future, skipped = study_planner_store.create_sessions(self.alice, plan["plan_id"], [
            {"document_id": "doc-a", "activity_type": "summary", "scheduled_start": f"{day}T18:00:00",
             "scheduled_end": f"{day}T19:00:00", "duration_minutes": 60}
            for day in ("2026-09-20", "2026-09-24", "2026-09-25")
        ])
        study_planner_store.update_session(self.alice, skipped["session_id"], {"status": "skipped"})

        context = study_planner_service.build_scheduling_context(self.alice, plan["plan_id"], now=datetime(2026, 9, 23, 9, 0))
        self.assertIsInstance(context, SchedulingContext)
        self.assertEqual([m.document_id for m in context.materials], ["doc-a", "doc-b"])
        self.assertEqual((context.materials[0].deadline, context.materials[0].state.plan_id), ("2026-10-01", plan["plan_id"]))
        self.assertEqual(len(context.availability), 1)
        self.assertEqual([s["session_id"] for s in context.busy_sessions], [future["session_id"]])
        with self.assertRaises(ValueError):
            study_planner_service.build_scheduling_context(self.bob, plan["plan_id"])


if __name__ == "__main__":
    unittest.main()
