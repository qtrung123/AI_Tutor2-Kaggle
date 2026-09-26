"""Retake resets the persisted quiz progress, not only the local UI.

Root cause this covers: frontend/js/quiz.js declared resetAssessmentQuiz twice. The later
(local-only) declaration always overrode the earlier one, which was the only caller of
requestQuizProgressReset(), so Retake cleared the screen but left the in-progress attempt on the
server -- a reload/Resume could bring the pre-retake answers back.

Frontend: the real functions are extracted from the frontend source and run with Node against
stubs (skipped without Node). Backend: the real DELETE /api/quiz/{document_id}/progress route on a
real SQLite database (nothing in the persistence layer is patched).
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import quiz_attempt_service, quiz_store
from backend.api.deps import require_current_user
from backend.main import app
from frontend_source import frontend_script_paths

DOCUMENT_ID = "lecture.pdf"

NODE_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const source = JSON.parse(process.argv[1]).map((path) => fs.readFileSync(path, "utf8")).join("\n");
function extract(name) {
  for (const prefix of [`async function ${name}(`, `function ${name}(`]) {
    const start = source.indexOf(prefix);
    if (start < 0) continue;
    let depth = 0;
    for (let i = source.indexOf("{", start); i < source.length; i++) {
      if (source[i] === "{") depth++;
      if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`missing function ${name}`);
}
const context = vm.createContext({ console, URLSearchParams, setTimeout, clearTimeout });
const names = ["resetAssessmentQuiz", "requestQuizProgressReset", "flushQuizAutosave", "resetQuizAutosave"];
vm.runInContext(names.map(extract).join("\n"), context);
vm.runInContext(process.argv[2], context);   // scenario state + stubs
Promise.resolve(vm.runInContext(`(async () => { ${process.argv[3]} })()`, context))
  .then((result) => process.stdout.write(JSON.stringify(result)))
  .catch((error) => { console.error(error); process.exit(1); });
"""

# The player state of a quiz whose previous attempt is still in progress (or completed) locally.
SETUP = """
var QUIZ_API_BASE_URL = "/api/quiz";
var currentQuiz = {quiz_id: "quiz-a", document_id: "lecture.pdf", difficulty: "easy", topic_id: "document",
                   questions: [{id: 1}, {id: 2}]};
var currentAttempt = {attempt_id: "old", answers: {"1": "A"}};
var quizAnswers = {"1": "A", "2": "B"};
var quizExplanations = {"1": "cached"};
var quizQuestionIndex = 1;
var quizAutosaveTimer = null, quizAutosaveDirty = false, quizAutosaveInFlight = null, quizAutosaveSeq = 0;
var quizDetachedSave = null, quizPlayerOpen = false;
var calls = [], toasts = [], renders = 0, resetStatus = 200;
var showToast = (message) => toasts.push(message);
var renderAssessmentQuiz = () => { renders += 1; };
var setQuizPlayerSaveStatus = () => {};
var requestQuizProgress = async (payload) => { calls.push({method: "PATCH", answers: payload.answers}); return {attempt_id: "saved"}; };
var fetch = async (url, init = {}) => {
  calls.push({method: init.method || "GET", url});
  return {ok: resetStatus < 400, status: resetStatus, json: async () => ({reset: true})};
};
"""

STATE = "({calls, toasts, renders, currentAttempt, quizAnswers, quizExplanations, quizQuestionIndex, dirty: quizAutosaveDirty})"


def run_node(setup, scenario):
    result = subprocess.run(
        ["node", "-e", NODE_DRIVER, json.dumps([str(path) for path in frontend_script_paths()]), setup, scenario],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RetakeFrontendTests(unittest.TestCase):
    def test_retake_resets_this_exact_quiz_on_the_server_then_locally(self):
        state = run_node(SETUP, f"await resetAssessmentQuiz(); return {STATE};")
        self.assertEqual(state["calls"], [{
            "method": "DELETE",
            "url": "/api/quiz/lecture.pdf/progress?difficulty=easy&topic_id=document&quiz_id=quiz-a",
        }])
        self.assertIsNone(state["currentAttempt"])
        self.assertEqual(state["quizAnswers"], {})
        self.assertEqual(state["quizExplanations"], {})
        self.assertEqual(state["quizQuestionIndex"], 0)
        self.assertEqual(state["renders"], 1)
        self.assertEqual(state["toasts"], ["Retake started with the same questions"])   # unchanged visible UX

    def test_a_failed_server_reset_keeps_the_local_attempt_and_says_so(self):
        state = run_node(SETUP + "resetStatus = 500;", f"await resetAssessmentQuiz(); return {STATE};")
        self.assertEqual([call["method"] for call in state["calls"]], ["DELETE"])
        self.assertEqual(state["quizAnswers"], {"1": "A", "2": "B"})       # never pretends the reset happened
        self.assertEqual(state["quizQuestionIndex"], 1)
        self.assertEqual(state["currentAttempt"], {"attempt_id": "old", "answers": {"1": "A"}})
        self.assertEqual(state["renders"], 0)
        self.assertEqual(state["toasts"], ["Reset progress API returned 500"])

    def test_a_pending_autosave_of_the_old_answers_lands_before_the_reset_never_after(self):
        scenario = f"""
            quizAutosaveDirty = true;
            quizAutosaveTimer = setTimeout(() => {{ calls.push({{method: "LATE-TIMER"}}); }}, 50);
            await resetAssessmentQuiz();
            await new Promise((resolve) => setTimeout(resolve, 120));   // the old timer must not fire later
            return {STATE};
        """
        state = run_node(SETUP, scenario)
        self.assertEqual([call["method"] for call in state["calls"]], ["PATCH", "DELETE"])
        self.assertFalse(state["dirty"])
        self.assertEqual(state["quizAnswers"], {})

    def test_there_is_exactly_one_retake_implementation(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in frontend_script_paths())
        self.assertEqual(source.count("function resetAssessmentQuiz("), 1)


def quiz(quiz_id: str) -> dict:
    return {
        "quiz_id": quiz_id, "document_id": DOCUMENT_ID, "title": "Quiz", "difficulty": "easy",
        "topic_id": "document", "topic_name": "Entire document",
        "questions": [
            {"id": 1, "question": "Question one?", "options": ["A. One", "B. Two", "C. Three", "D. Four"],
             "correct_answer": "A", "topic_id": "document", "difficulty": "easy", "explanation": "Supported.",
             "source_chunk_ids": ["hash_1"], "validation_outcome": "accepted"},
            {"id": 2, "question": "Question two?", "options": ["A. One", "B. Two", "C. Three", "D. Four"],
             "correct_answer": "B", "topic_id": "document", "difficulty": "easy", "explanation": "Supported.",
             "source_chunk_ids": ["hash_2"], "validation_outcome": "accepted"},
        ],
    }


class RetakeServerResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(quiz_store, "DATABASE_PATH", Path(self.temp.name) / "quiz.db"),
            patch.object(quiz_attempt_service, "_document_lookup", return_value={DOCUMENT_ID: {"id": DOCUMENT_ID}}),
            patch.object(quiz_attempt_service, "recompute_topic_mastery", return_value={}),
        ]
        for item in self.patches:
            item.start()
        self.owner = "owner-1"
        quiz_store.save_quiz(DOCUMENT_ID, "easy", quiz("quiz-a"), self.owner)
        quiz_store.save_quiz(DOCUMENT_ID, "easy", quiz("quiz-b"), self.owner)
        app.dependency_overrides[require_current_user] = lambda: {"id": self.owner, "email": "o@example.com"}
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def autosave(self, quiz_id, answers, index=0):
        return quiz_attempt_service.update_quiz_progress(
            DOCUMENT_ID, "easy", "document", self.owner, quiz_id=quiz_id, answers=answers, current_question_index=index,
        )

    def retake(self, quiz_id):
        # Exactly the request the frontend's requestQuizProgressReset() sends.
        return self.client.delete(f"/api/quiz/{DOCUMENT_ID}/progress",
                                  params={"difficulty": "easy", "topic_id": "document", "quiz_id": quiz_id})

    def resume(self, quiz_id):
        return quiz_attempt_service.load_quiz_with_attempt(DOCUMENT_ID, "easy", "document", self.owner, quiz_id=quiz_id)

    def test_retake_keeps_completed_history_and_resume_never_restores_the_old_answers(self):
        completed = quiz_attempt_service.submit_quiz_attempt(
            DOCUMENT_ID, "easy", "document", {"1": "A", "2": "A"}, self.owner, quiz_id="quiz-a")
        history_before = quiz_attempt_service.list_completed_quiz_attempts(DOCUMENT_ID, "easy", self.owner)
        result_before = quiz_attempt_service.load_completed_quiz_attempt(completed["attempt_id"], self.owner)

        response = self.retake("quiz-a")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reset"])
        # 3. completed history/results are untouched
        self.assertEqual(quiz_attempt_service.list_completed_quiz_attempts(DOCUMENT_ID, "easy", self.owner), history_before)
        self.assertEqual(quiz_attempt_service.load_completed_quiz_attempt(completed["attempt_id"], self.owner), result_before)
        # the quiz itself is kept (not deleted/regenerated) and Resume starts clean
        resumed = self.resume("quiz-a")
        self.assertEqual(resumed["quiz"]["quiz_id"], "quiz-a")
        self.assertIsNone(resumed["latest_attempt"])

    def test_retake_clears_in_progress_answers_so_a_reload_cannot_restore_them(self):
        self.autosave("quiz-a", {"1": "B", "2": "C"}, index=1)
        self.autosave("quiz-b", {"1": "D"})
        self.assertEqual(self.resume("quiz-a")["latest_attempt"]["answers"], {"1": "B", "2": "C"})

        self.assertEqual(self.retake("quiz-a").status_code, 200)

        # 4. reload/Resume of the retaken quiz no longer finds the pre-retake answers
        self.assertIsNone(self.resume("quiz-a")["latest_attempt"])
        # the next autosave starts a fresh attempt instead of merging into the old one
        fresh = self.autosave("quiz-a", {"2": "B"})
        self.assertEqual(fresh["answers"], {"2": "B"})
        self.assertEqual(self.resume("quiz-a")["latest_attempt"]["answers"], {"2": "B"})
        # quiz_id targeting: the sibling quiz in the same slot keeps its own progress
        self.assertEqual(self.resume("quiz-b")["latest_attempt"]["answers"], {"1": "D"})


if __name__ == "__main__":
    unittest.main()
