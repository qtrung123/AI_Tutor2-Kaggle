"""Flashcards -> written practice quiz (backend/flashcard_quiz_service.py) through the real API and
SQLite store: exact counts with no MCQ fallback, fill_blank grounded in the document's own chunks
(never in flashcard text), short_answer fallback per card, Mixed ~50/50, the "No flashcards
available" state, owner/document/set isolation, autosave/resume of typed answers, submit/results/
history, learner-marked self-check questions, and compatibility with ordinary quizzes. No LLM is
called anywhere in this flow.
"""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import (
    auth_store, conversation_store, flashcard_quiz_service, flashcard_store, indexed_document_store, quiz_store,
    study_planner_store, study_progress, summary_store,
)
from backend.document_study_state import get_document_study_state
from backend.quiz_service import list_quiz_statuses
from backend.flashcard_service import FLASHCARD_VERSION
from backend.main import app
from backend.quiz_units import FILL_BLANK_MARKER, squash

DOC = "notes.pdf"
OTHER_DOC = "other.pdf"
TOPICS = [{"topic_id": "sched", "name": "Scheduling", "subtopics": []},
          {"topic_id": "sync", "name": "Synchronization", "subtopics": []}]

# The document's own text. Only these chunks may serve as fill_blank evidence.
CHUNKS = [
    {"content": "The scheduler orders runnable processes for the CPU in every time slice. "
                "A context switch saves the registers of the running process before another one runs.",
     "metadata": {"chunk_id": "c1", "chunk": 0}},
    {"content": "A semaphore guards shared counters so that two threads never update them together. "
                "Virtual memory is divided into fixed size pages that the kernel maps to frames.",
     "metadata": {"chunk_id": "c2", "chunk": 1}},
]

# (front, back, topic, source chunks). Cards 1-4 have a concise answer written in the document; card 5
# is a concise term that the document never states; cards 6-10 have long sentence answers.
CARDS = [
    ("Which component orders runnable processes for the CPU?", "The scheduler", "sched", ["c1"]),
    ("What saves the registers of the running process?", "context switch", "sched", ["c1"]),
    ("What guards shared counters between threads?", "Semaphore", "sync", ["c2"]),
    ("What are the fixed size units of virtual memory called?", "pages", "sync", ["c2"]),
    ("Which effect lets particles cross barriers?", "Quantum tunneling", "sync", []),
    ("Why do processes need a scheduler?", "Because many runnable processes compete for a limited number of CPU cores at the same time.", "sched", ["c1"]),
    ("Explain what a context switch costs.", "It spends CPU time saving and restoring registers instead of running useful work for processes.", "sched", ["c1"]),
    ("How does a semaphore protect a counter?", "It lets only one thread at a time enter the section that updates the shared counter value.", "sync", ["c2"]),
    ("Why is memory split into pages?", "Fixed size pages let the kernel map memory flexibly and avoid external fragmentation problems.", "sync", ["c2"]),
    ("What happens without synchronization?", "Two threads can interleave their updates and the final counter value becomes wrong or lost.", "sync", ["c2"]),
]
CONCISE_GROUNDED = 4


def save_cards(owner_id: str, document_id: str, cards=CARDS, model_id: str = "model-a") -> dict:
    identity = {
        "owner_id": owner_id, "document_id": document_id, "document_hash": "hash-1", "topic_schema_version": 2,
        "flashcard_version": FLASHCARD_VERSION, "model_id": model_id, "runtime_model": "runtime",
        "topic_ids": ["sched", "sync"], "flashcard_language": "auto",
    }
    return flashcard_store.save_flashcards(identity, [
        {"topic_id": topic, "topic_name": topic.title(), "front": front, "back": back, "source_chunk_ids": sources}
        for front, back, topic, sources in cards
    ])


class FlashcardWrittenQuizTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        db = root / "app.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", db),
            patch.object(conversation_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "DATABASE_PATH", db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", root / "indexed_files.json"),
            patch.object(flashcard_store, "DATABASE_PATH", db),
            patch.object(quiz_store, "DATABASE_PATH", db),
            patch.object(study_planner_store, "DATABASE_PATH", db),
            patch.object(summary_store, "DATABASE_PATH", db),
            patch.object(quiz_store, "LEGACY_GENERATED_QUIZZES_PATH", root / "missing_quizzes.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_ATTEMPTS_PATH", root / "missing_attempts.json"),
            patch.object(quiz_store, "LEGACY_QUIZ_EXPLANATIONS_PATH", root / "missing_explanations.json"),
            patch.object(flashcard_quiz_service, "get_document_chunks", return_value=CHUNKS),
            # Nothing in this flow may reach a language model.
            patch("backend.flashcard_service.ChatOllama", side_effect=AssertionError("LLM called")),
            patch("backend.rag_service.explain_quiz_answer", side_effect=AssertionError("LLM called")),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.chunks_mock = self.mocks[11]
        self.alice_client = TestClient(app)
        self.bob_client = TestClient(app)
        self.alice = self.signup(self.alice_client, "alice-fc@example.com")
        self.bob = self.signup(self.bob_client, "bob-fc@example.com")
        for owner in (self.alice, self.bob):
            for document_id in (DOC, OTHER_DOC):
                indexed_document_store.upsert_indexed_document(owner, document_id, {
                    "hash": "hash-1", "chunks": 2, "path": document_id, "topic_schema_version": 2, "topics": TOPICS,
                })
        self.alice_set = save_cards(self.alice, DOC)

    def tearDown(self):
        self.alice_client.close()
        self.bob_client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def signup(client, email):
        response = client.post("/api/auth/signup", json={"display_name": "Learner", "email": email, "password": "long-password-123"})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def create(self, client=None, document_id=DOC, **body):
        return (client or self.alice_client).post(f"/api/flashcards/{document_id}/practice-quiz", json=body)

    def types(self, quiz):
        return [question["question_type"] for question in quiz["questions"]]

    # 1 ------------------------------------------------------------------------------------------
    def test_ten_cards_short_answer_returns_exactly_ten_short_answers_from_the_cards(self):
        response = self.create(mode="short_answer", question_count=10)
        self.assertEqual(response.status_code, 200, response.text)
        quiz = response.json()
        self.assertEqual(self.types(quiz), ["short_answer"] * 10)
        self.assertEqual([q["question"] for q in quiz["questions"]], [card[0] for card in CARDS])   # deck order
        self.assertEqual([q["correct_answer"] for q in quiz["questions"]], [card[1] for card in CARDS])
        self.assertTrue(all(q["options"] == [] for q in quiz["questions"]))
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"short_answer": 10})
        self.assertEqual(quiz["assessment_plan"]["flashcard_set_id"], self.alice_set["set_id"])
        self.chunks_mock.assert_not_called()   # short answers need no document lookup at all
        # Long card answers are self-check; concise ones are auto-graded.
        self.assertEqual(quiz["assessment_plan"]["self_check_question_ids"], [6, 7, 8, 9, 10])

    def test_all_counts_every_usable_card_and_more_than_available_is_rejected(self):
        self.assertEqual(len(self.create(mode="short_answer").json()["questions"]), 10)
        too_many = self.create(mode="mixed", question_count=11)
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("Only 10 flashcards", too_many.json()["detail"])

    # 2 ------------------------------------------------------------------------------------------
    def test_fill_blank_questions_are_document_sentences_with_the_card_answer_blanked(self):
        quiz = self.create(mode="fill_blank", question_count=10).json()
        fills = [q for q in quiz["questions"] if q["question_type"] == "fill_blank"]
        self.assertEqual(len(fills), CONCISE_GROUNDED)
        document_text = squash(" ".join(chunk["content"] for chunk in CHUNKS))
        chunk_ids = {chunk["metadata"]["chunk_id"] for chunk in CHUNKS}
        for question in fills:
            self.assertEqual(question["question"].count(FILL_BLANK_MARKER), 1)
            completed = question["question"].replace(FILL_BLANK_MARKER, question["correct_answer"])
            self.assertIn(squash(completed), document_text)            # the whole sentence is the document's
            self.assertTrue(set(question["source_chunk_ids"]) <= chunk_ids)
            self.assertTrue(question["explanation"].startswith("From the document:"))
            self.assertNotIn(question["question"], [card[0] for card in CARDS])   # not the flashcard prompt
        by_concept = {q["concept_id"]: q for q in fills}
        scheduler = next(q for q in fills if "orders runnable processes" in q["question"])
        self.assertEqual(scheduler["question"], f"The {FILL_BLANK_MARKER} orders runnable processes for the CPU in every time slice.")
        self.assertEqual(scheduler["correct_answer"], "scheduler")
        self.assertIn("The scheduler", scheduler["correct_answers"])   # the flashcard answer is accepted as written
        self.assertEqual(len(by_concept), CONCISE_GROUNDED)

    def test_flashcard_text_is_never_used_as_evidence(self):
        # "Quantum tunneling" is written in a flashcard (its own back, and another card's front below)
        # but never in the document: it must not become a fill_blank question.
        save_cards(self.alice, DOC, cards=[*CARDS[:4], CARDS[4], ("Quantum tunneling is?", "barrier crossing", "sync", [])])
        quiz = self.create(mode="fill_blank").json()
        fills = [q for q in quiz["questions"] if q["question_type"] == "fill_blank"]
        self.assertNotIn("quantum", " ".join(q["question"] + q["correct_answer"] for q in fills).lower())
        tunneling = next(q for q in quiz["questions"] if q["correct_answer"] == "Quantum tunneling")
        self.assertEqual(tunneling["question_type"], "short_answer")

    # 3 ------------------------------------------------------------------------------------------
    def test_cards_that_cannot_be_blanked_fall_back_to_short_answer_never_mcq(self):
        quiz = self.create(mode="fill_blank", question_count=10).json()
        self.assertEqual(len(quiz["questions"]), 10)
        self.assertEqual(sorted(set(self.types(quiz))), ["fill_blank", "short_answer"])
        self.assertEqual(self.types(quiz).count("short_answer"), 10 - CONCISE_GROUNDED)
        self.assertTrue(all(q["options"] == [] for q in quiz["questions"]))
        # K smaller than the blankable cards: every slot is a grounded blank.
        self.assertEqual(self.types(self.create(mode="fill_blank", question_count=3).json()), ["fill_blank"] * 3)

    # 4 ------------------------------------------------------------------------------------------
    def test_mixed_is_about_half_fill_blank_and_only_written_types(self):
        for count, expected_fill in ((10, CONCISE_GROUNDED), (6, 3), (5, 2), (1, 0)):
            quiz = self.create(mode="mixed", question_count=count).json()
            types = self.types(quiz)
            self.assertEqual(len(types), count)
            self.assertTrue(set(types) <= {"fill_blank", "short_answer"}, types)
            self.assertEqual(types.count("fill_blank"), expected_fill, count)

    def test_selection_is_random_but_always_exact_and_distinct(self):
        seen = set()
        for seed in range(8):
            quiz = flashcard_quiz_service.create_flashcard_written_quiz(
                self.alice, DOC, "mixed", 5, rng=random.Random(seed))
            ids = [q["concept_id"] for q in quiz["questions"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(ids), 5)
            seen.add(tuple(ids))
        self.assertGreater(len(seen), 1)

    # 5 ------------------------------------------------------------------------------------------
    def test_no_flashcards_is_a_clear_409(self):
        response = self.create(self.bob_client, mode="mixed", question_count=5)
        self.assertEqual(response.status_code, 409)
        self.assertIn("No flashcards available", response.json()["detail"])
        self.assertEqual(self.create(document_id="missing.pdf").status_code, 404)

    # 6 ------------------------------------------------------------------------------------------
    def test_owner_document_and_set_isolation(self):
        other_set = save_cards(self.alice, OTHER_DOC, cards=[("Other doc prompt?", "other answer", "sched", [])])
        bob_set = save_cards(self.bob, DOC, cards=[("Bob prompt?", "bob answer", "sched", [])])
        # Another owner's set, or another document's set, yields no cards for this request.
        self.assertEqual(self.create(set_id=bob_set["set_id"], mode="short_answer").status_code, 409)
        self.assertEqual(self.create(set_id=other_set["set_id"], mode="short_answer").status_code, 409)
        quiz = self.create(set_id=self.alice_set["set_id"], mode="short_answer").json()
        alice_ids = {card["flashcard_id"] for card in self.alice_set["cards"]}
        self.assertEqual({q["concept_id"] for q in quiz["questions"]}, alice_ids)
        bob_quiz = self.create(self.bob_client, mode="short_answer").json()
        self.assertEqual([q["question"] for q in bob_quiz["questions"]], ["Bob prompt?"])
        # Bob cannot open Alice's quiz.
        response = self.bob_client.get(f"/api/quiz/{DOC}", params={"topic_id": "document", "difficulty": "easy", "quiz_id": quiz["quiz_id"]})
        self.assertIsNone(response.json().get("quiz"))

    def test_an_explicit_older_set_is_used_exactly(self):
        newer = save_cards(self.alice, DOC, cards=[("Newer prompt?", "newer", "sched", [])])
        self.assertEqual([q["question"] for q in self.create(mode="short_answer").json()["questions"]], ["Newer prompt?"])
        quiz = self.create(mode="short_answer", set_id=self.alice_set["set_id"]).json()
        self.assertEqual(len(quiz["questions"]), 10)
        self.assertNotEqual(quiz["assessment_plan"]["flashcard_set_id"], newer["set_id"])

    # 7 ------------------------------------------------------------------------------------------
    def test_typed_answers_autosave_and_resume(self):
        quiz = self.create(mode="fill_blank", question_count=10).json()
        fill_id = next(q["id"] for q in quiz["questions"] if q["question_type"] == "fill_blank")
        short_id = next(q["id"] for q in quiz["questions"] if q["question_type"] == "short_answer")
        long_text = "It lets only one thread at a time enter the section. " * 8   # > 200 chars is fine for a short answer
        saved = self.alice_client.patch(f"/api/quiz/{DOC}/progress", json={
            "difficulty": "easy", "topic_id": "document", "quiz_id": quiz["quiz_id"], "current_question_index": 3,
            "answers": {str(fill_id): "  Scheduler ", str(short_id): long_text},
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        detail = self.alice_client.get(f"/api/quiz/{DOC}", params={"topic_id": "document", "difficulty": "easy", "quiz_id": quiz["quiz_id"]}).json()
        attempt = detail["latest_attempt"]
        self.assertFalse(attempt["completed"])
        self.assertEqual(attempt["current_question_index"], 3)
        self.assertEqual(attempt["answers"][str(fill_id)], "Scheduler")
        self.assertEqual(attempt["answers"][str(short_id)], long_text.strip())
        self.assertEqual(detail["quiz"]["quiz_id"], quiz["quiz_id"])

    # 8 ------------------------------------------------------------------------------------------
    def test_submit_results_history_and_self_check(self):
        quiz = self.create(mode="short_answer", question_count=10).json()
        answers = {
            "1": "the SCHEDULER.",        # normalized exact match -> correct
            "2": "Context  Switch",       # correct
            "3": "mutex",                 # wrong
            "6": CARDS[5][1].upper(),     # self-check typed exactly -> correct
            "7": "Registers are saved.",  # self-check, not an exact match -> incorrect until marked
        }
        submit = self.alice_client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document", "answers": answers,
            "allow_unanswered": True,
        })
        self.assertEqual(submit.status_code, 200, submit.text)
        attempt = submit.json()
        results = {r["question_id"]: r for r in attempt["question_results"]}
        self.assertEqual({qid: results[qid]["is_correct"] for qid in (1, 2, 3, 6, 7)}, {1: True, 2: True, 3: False, 6: True, 7: False})
        self.assertEqual(results[1]["selected_answer"], "the SCHEDULER.")          # typed answer kept as written
        self.assertEqual(results[1]["correct_answers"], ["The scheduler"])        # canonical flashcard answer
        self.assertEqual(results[1]["question_type"], "short_answer")
        self.assertEqual(attempt["score"], 3)

        history = self.alice_client.get("/api/quiz-history", params={"document_id": DOC}).json()
        self.assertEqual([row["attempt_id"] for row in history], [attempt["attempt_id"]])
        detail = self.alice_client.get(f"/api/quiz-history/{attempt['attempt_id']}").json()
        self.assertEqual(detail["quiz"]["source"], "flashcards")
        self.assertEqual(detail["quiz"]["self_check_question_ids"], [6, 7, 8, 9, 10])

        marked = self.alice_client.post(f"/api/quiz-history/{attempt['attempt_id']}/self-check", json={"question_id": 7, "is_correct": True})
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(marked.json()["score"], 4)
        self.assertEqual(marked.json()["percentage"], 40.0)
        self.assertTrue(next(r for r in marked.json()["question_results"] if r["question_id"] == 7)["is_correct"])
        # Only answered self-check questions of the learner's own attempt can be marked.
        self.assertEqual(self.alice_client.post(f"/api/quiz-history/{attempt['attempt_id']}/self-check", json={"question_id": 3, "is_correct": True}).status_code, 400)
        self.assertEqual(self.alice_client.post(f"/api/quiz-history/{attempt['attempt_id']}/self-check", json={"question_id": 8, "is_correct": True}).status_code, 400)
        self.assertEqual(self.bob_client.post(f"/api/quiz-history/{attempt['attempt_id']}/self-check", json={"question_id": 7, "is_correct": True}).status_code, 404)

        # Flashcard practice (partly self-marked) is not mastery evidence.
        self.assertEqual(quiz_store.list_completed_answer_snapshots(self.alice, DOC, "sched"), [])

        retake = self.alice_client.get(f"/api/quiz-history/{attempt['attempt_id']}/retake")
        self.assertEqual(retake.status_code, 200)
        self.assertEqual(retake.json()["quiz"]["quiz_id"], quiz["quiz_id"])

    def test_library_lists_the_written_quiz(self):
        quiz = self.create(mode="mixed", question_count=5).json()
        library = quiz_store.list_document_quizzes(DOC, self.alice)
        self.assertIn(quiz["quiz_id"], library)
        self.assertEqual(library[quiz["quiz_id"]]["assessment_scope"], "document")

    # 9 ------------------------------------------------------------------------------------------
    def test_ordinary_quizzes_stay_compatible_and_unaffected(self):
        normal = quiz_store.save_quiz(DOC, "easy", {
            "title": "Normal quiz", "topic_id": "document", "assessment_scope": "document", "topic_schema_version": 2,
            "assessment_plan": {"planner_version": "study_units_v1"},
            "questions": [
                {"id": 1, "question": "Which one orders processes?", "options": ["A. scheduler", "B. pager", "C. cache", "D. disk"],
                 "correct_answer": "a", "question_type": "single_choice", "topic_id": "sched"},
                {"id": 2, "question": "A ____ guards counters.", "options": [], "correct_answer": "semaphore",
                 "correct_answers": ["semaphore"], "question_type": "fill_blank", "topic_id": "sync"},
            ],
        }, self.alice)
        written = self.create(mode="short_answer", question_count=2).json()
        # The slot lookup used by normal generation/cache ignores flashcard quizzes.
        self.assertEqual(quiz_store.get_quiz(DOC, "easy", "document", self.alice)["quiz_id"], normal["quiz_id"])
        stored = quiz_store.get_quiz_by_id(normal["quiz_id"], self.alice)
        self.assertEqual(stored["questions"][0]["correct_answer"], "A")      # option letters still upper-cased
        self.assertEqual(stored["questions"][1]["correct_answer"], "semaphore")
        submit = self.alice_client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": normal["quiz_id"], "difficulty": "easy", "topic_id": "document", "answers": {"1": "A", "2": " Semaphore "},
        })
        self.assertEqual(submit.status_code, 200, submit.text)
        self.assertEqual(submit.json()["score"], 2)
        # Normal quiz answers remain mastery evidence.
        self.assertEqual(len(quiz_store.list_completed_answer_snapshots(self.alice, DOC, "sched")), 1)
        self.assertIsNotNone(quiz_store.get_quiz_by_id(written["quiz_id"], self.alice))

    # Practice isolation ---------------------------------------------------------------------------
    def normal_quiz_at_40_percent(self) -> dict:
        questions = [
            {"id": index, "question": f"Normal question {index}?", "options": ["A. a", "B. b", "C. c", "D. d"],
             "correct_answer": "A", "question_type": "single_choice", "topic_id": "sched", "concept_id": f"n{index}",
             "concept_plan_id": "study_units_v1"}
            for index in range(1, 6)
        ]
        normal = quiz_store.save_quiz(DOC, "easy", {
            "title": "Normal quiz", "topic_id": "document", "assessment_scope": "document", "topic_schema_version": 2,
            "assessment_plan": {"planner_version": "study_units_v1", "requested_count": 5}, "questions": questions,
        }, self.alice)
        submit = self.alice_client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": normal["quiz_id"], "difficulty": "easy", "topic_id": "document",
            "answers": {"1": "A", "2": "A", "3": "B", "4": "B", "5": "B"},
        })
        self.assertEqual(submit.json()["percentage"], 40.0)
        return {"quiz": normal, "attempt": submit.json()}

    def perfect_practice(self) -> dict:
        quiz = self.create(mode="mixed", question_count=10).json()
        submit = self.alice_client.post(f"/api/quiz/{DOC}/submit", json={
            "quiz_id": quiz["quiz_id"], "difficulty": "easy", "topic_id": "document",
            "answers": {str(q["id"]): q["correct_answer"] for q in quiz["questions"]},
        })
        self.assertEqual(submit.json()["percentage"], 100.0)
        self.assertEqual(submit.json()["mastery_by_topic"], {})   # practice recomputes no mastery
        return {"quiz": quiz, "attempt": submit.json()}

    def test_later_perfect_practice_never_replaces_the_normal_quiz_score(self):
        normal = self.normal_quiz_at_40_percent()
        mastery_before = quiz_store.list_topic_mastery(self.alice, DOC)
        practice = self.perfect_practice()
        # Creating and submitting practice leaves the cached mastery rows exactly as they were.
        self.assertEqual(quiz_store.list_topic_mastery(self.alice, DOC), mastery_before)
        self.assertEqual(len(mastery_before), 1)

        # DocumentStudyState (Planner scheduling/adaptation input): still the normal 40% -> low_quiz_score.
        state = get_document_study_state(self.alice, DOC)
        self.assertEqual(state.quiz.latest_completed.percentage, 40.0)
        self.assertEqual(state.quiz.latest_completed.quiz_id, normal["quiz"]["quiz_id"])
        self.assertEqual(state.quiz.latest_quiz_id, normal["quiz"]["quiz_id"])
        self.assertEqual((state.quiz.quiz_count, state.quiz.completed_attempt_count), (1, 1))
        self.assertEqual(state.state_reason.code, "low_quiz_score")
        self.assertEqual(state.learning_state, "needs_review")

        # get_document_quiz_activity / progress / dashboard performance.
        activity = quiz_store.get_document_quiz_activity(DOC, self.alice)
        self.assertEqual([row["quiz_id"] for row in activity["quizzes"]], [normal["quiz"]["quiz_id"]])
        self.assertEqual([row["attempt_id"] for row in activity["completed_attempts"]], [normal["attempt"]["attempt_id"]])
        self.assertEqual(study_progress.latest_quiz_percentage(self.alice, DOC), 40.0)
        dashboard = self.alice_client.get("/api/dashboard").json()
        self.assertEqual(dashboard["metrics"]["current_quiz_performance"]["average_percentage"], 40.0)
        self.assertEqual(dashboard["latest_attempt"]["attempt_id"], normal["attempt"]["attempt_id"])
        self.assertEqual(dashboard["metrics"]["answered_questions"], 5)

        # Mastery / knowledge gaps / recommendations inputs are untouched by the practice attempt.
        self.assertEqual(len(quiz_store.list_completed_answer_snapshots(self.alice, DOC, "sched")), 5)
        self.assertEqual(quiz_store.list_completed_answer_snapshots(self.alice, DOC, "sync"), [])
        identities = {(row["document_id"], row["topic_id"]) for row in quiz_store.list_mastery_identities(self.alice)}
        self.assertEqual(identities, {(DOC, "sched")})
        self.assertEqual(self.alice_client.get(f"/api/knowledge-gaps/{DOC}").status_code, 200)

        # Written practice stays visible in its own history/results.
        history = self.alice_client.get("/api/quiz-history", params={"document_id": DOC}).json()
        by_attempt = {row["attempt_id"]: row for row in history}
        self.assertTrue(by_attempt[practice["attempt"]["attempt_id"]]["practice"])
        self.assertEqual(by_attempt[practice["attempt"]["attempt_id"]]["percentage"], 100)
        self.assertFalse(by_attempt[normal["attempt"]["attempt_id"]]["practice"])
        detail = self.alice_client.get(f"/api/quiz-history/{practice['attempt']['attempt_id']}").json()
        self.assertEqual((detail["score"], detail["total"], detail["quiz"]["source"]), (10, 10, "flashcards"))

    def test_practice_never_marks_a_normal_slot_saved_or_in_progress(self):
        normal = self.normal_quiz_at_40_percent()
        practice = self.create(mode="short_answer", question_count=3).json()
        self.alice_client.patch(f"/api/quiz/{DOC}/progress", json={
            "difficulty": "easy", "topic_id": "document", "quiz_id": practice["quiz_id"], "answers": {"1": "typed"},
        })
        variants = {v["quiz_id"]: v for status in list_quiz_statuses(self.alice) if status["document_id"] == DOC
                    for v in status["variants"]}
        self.assertTrue(variants[practice["quiz_id"]]["practice"])
        self.assertEqual(variants[practice["quiz_id"]]["progress_status"], "in_progress")   # still resumable
        self.assertFalse(variants[normal["quiz"]["quiz_id"]]["practice"])
        # The normal slot lookup and the Planner's quiz state ignore the practice quiz.
        self.assertEqual(quiz_store.get_quiz(DOC, "easy", "document", self.alice)["quiz_id"], normal["quiz"]["quiz_id"])
        state = get_document_study_state(self.alice, DOC)
        self.assertEqual(state.quiz.in_progress_count, 0)
        self.assertEqual(state.quiz.latest_quiz_id, normal["quiz"]["quiz_id"])
        resumed = self.alice_client.get(f"/api/quiz/{DOC}", params={
            "topic_id": "document", "difficulty": "easy", "quiz_id": practice["quiz_id"]}).json()
        self.assertEqual(resumed["latest_attempt"]["answers"], {"1": "typed"})

    def test_practice_alone_leaves_the_document_unassessed(self):
        self.perfect_practice()
        state = get_document_study_state(self.alice, DOC)
        self.assertIsNone(state.quiz.latest_completed)
        self.assertEqual(state.quiz.quiz_count, 0)
        self.assertNotIn(state.state_reason.code, ("low_quiz_score", "moderate_quiz_score", "strong_quiz_score"))
        self.assertIsNone(study_progress.latest_quiz_percentage(self.alice, DOC))
        self.assertIsNone(self.alice_client.get("/api/dashboard").json()["latest_attempt"])


class GradingModeTests(unittest.TestCase):
    def test_concise_answers_auto_graded_long_answers_self_check(self):
        self.assertTrue(flashcard_quiz_service.short_answer_is_auto_gradable("The scheduler"))
        self.assertTrue(flashcard_quiz_service.short_answer_is_auto_gradable("Round robin time slicing policy"))
        self.assertFalse(flashcard_quiz_service.short_answer_is_auto_gradable(CARDS[5][1]))

    def test_ambiguous_term_written_twice_in_a_sentence_is_not_blanked(self):
        sentences = [{"text": "The cache stores data and the cache is fast for every read request.", "chunk_id": "c9"}]
        card = {"front": "What is fast?", "back": "cache", "source_chunk_ids": []}
        self.assertIsNone(flashcard_quiz_service.ground_fill_blank(card, sentences))


if __name__ == "__main__":
    unittest.main()
