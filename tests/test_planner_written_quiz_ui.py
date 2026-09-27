"""Real-browser tests: Study Planner written_quiz sessions (flashcard retrieval practice). Starting one
opens the exact flashcard practice quiz the backend linked to the session (artifact_id) in the
focused Quiz Player -- never a normal assessment quiz -- and Resume/reload restores its typed answers.
Normal quiz sessions keep opening normal quizzes and never fall back to a practice quiz. Also: the
Written Quiz label/reason and its distinct style, the Flashcards fallback when there are no cards,
and that submitting practice never triggers planner adaptation. Headless Chrome against the Quiz
Player mock (tests/test_quiz_player_ui.py) plus the planner's session start endpoint.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, MOCK as QUIZ_MOCK, find_chrome

MOCK = QUIZ_MOCK + r"""
const written = (id, text, answer) => ({id, question: text, options: [], correct_answer: answer, correct_answers: [answer],
  question_type: "short_answer", topic_id: "t1", topic_name: "T1", difficulty: "easy", explanation: "Answer from your flashcard.", source_chunk_ids: []});
window.__quizzes["fc-quiz"] = {quiz_id: "fc-quiz", document_id: "lecture.pdf", title: "Lecture · Flashcard practice (Mixed)",
  difficulty: "easy", topic_id: "document", topic_name: "Entire document", assessment_scope: "document", generation_model: null,
  created_at: "2026-01-01T00:00:00Z", assessment_plan: {planner_version: "flashcard_written_v1", source: "flashcards", self_check_question_ids: []},
  questions: [written(1, "What orders processes?", "scheduler"), written(2, "What guards counters?", "semaphore")]};
window.__library = () => Object.values(window.__quizzes).map((quiz) => {
  const attempt = window.__attempts[quiz.quiz_id];
  const completed = Boolean(attempt && attempt.completed);
  const answered = attempt ? Object.keys(attempt.answers || {}).length : 0;
  const model = quiz.generation_model || {};
  return {quiz_id: quiz.quiz_id, title: quiz.title, topic_id: "document", topic_name: "", assessment_scope: "document",
    difficulty: quiz.difficulty, question_count: quiz.questions.length, requested_count: quiz.questions.length, status: "complete",
    model_id: model.model_id || null, model_name: model.name || null, created_at: quiz.created_at,
    updated_at: (attempt && attempt.updated_at) || quiz.created_at,
    progress_status: completed ? "completed" : (answered > 0 ? "in_progress" : "not_started"),
    answered, total: quiz.questions.length, score: completed ? attempt.score : null, percentage: completed ? attempt.percentage : null,
    practice: quiz.assessment_plan?.planner_version === "flashcard_written_v1"};
});
window.__startCalls = [];
window.__cardsAvailable = true;
const quizFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const p = new URL(url, "http://x").pathname;
  const start = p.match(/^\/api\/planner\/sessions\/([^/]+)\/start$/);
  if (start) {
    const session = window.__plannerSessions[start[1]];
    window.__startCalls.push(start[1]);
    const started = session.status === "scheduled";
    session.status = "in_progress";
    if (session.activity_type === "written_quiz") {
      // Like the backend: the first start links the flashcard practice quiz; no cards -> Flashcards.
      if (!window.__cardsAvailable) return json({session: {...session}, started, tool: "flashcards", artifact_available: false});
      session.artifact_id = session.artifact_id || "fc-quiz";
    }
    return json({session: {...session}, started, tool: "quiz", artifact_available: Boolean(session.artifact_id)});
  }
  return quizFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const pane = () => document.querySelector('[data-session-pane="quiz"]');
const $ = (id) => document.getElementById(id);
const state = () => ({
  page: document.body.dataset.page, tab: document.body.dataset.sessionTab, document: activeDocumentId,
  playerOpen: pane().classList.contains("quiz-player-open"), quizId: currentQuiz?.quiz_id || null,
  title: $("quiz-player-title").textContent, answers: {...quizAnswers},
  inputValue: $("quiz-player-fill-input")?.value ?? null,
});
const plannerOpen = async (session) => {
  window.__plannerSessions[session.session_id] = session;
  const card = plannerSessionItem({...session, plan_id: "plan-1"}, "div", {action: true});
  document.body.appendChild(card);
  const info = {chip: card.querySelector(".planner-activity-chip").textContent, reason: card.querySelector(".planner-session-reason").textContent,
    className: card.className, borderLeft: getComputedStyle(card).borderLeftWidth};
  const button = [...card.querySelectorAll("button")].find((b) => b.textContent === "Resume" || b.textContent === "Start");
  const label = button.textContent;
  button.click();
  await sleep(700);
  card.remove();
  return {label, card: info, ...state()};
};
const exitPlayer = async () => {
  $("quiz-player-exit").click(); await sleep(100);
  if (!$("quiz-exit-confirm").hidden) { $("quiz-exit-confirm-exit").click(); await sleep(300); }
};
const session = (id, activity, artifactId, status, reason) => ({session_id: id, document_id: "lecture.pdf", document_title: "Lecture",
  activity_type: activity, artifact_id: artifactId, status, scheduled_start: "2026-01-02T10:00:00", scheduled_end: "2026-01-02T10:30:00",
  duration_minutes: 15, reason});
const PRACTICE_REASON = {code: "retrieval_practice", message: "Scheduled to reinforce recall before your next assessment"};
window.__plannerSessions = {};
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 0, 2, 9, 0, 0);
  // A normal quiz (quiz-b) and the practice quiz are both in progress; the practice one is newer.
  window.__attempts["quiz-b"] = {attempt_id: "att-quiz-b", quiz_id: "quiz-b", answers: {"1": "A"}, current_question_index: 1,
    completed: false, answered: 1, total: 3, updated_at: "2026-01-01T00:05:00Z"};

  // 1. A fresh written_quiz session (no artifact yet): the backend links fc-quiz, the player opens exactly it.
  out.writtenStart = await plannerOpen(session("s-written", "written_quiz", null, "scheduled", PRACTICE_REASON));
  const typed = $("quiz-player-fill-input");
  typed.value = "the Scheduler"; typed.dispatchEvent(new Event("input", {bubbles: true}));
  await sleep(700);
  out.autosave = window.__lastProgressBody;
  await exitPlayer();
  out.linkedArtifact = window.__plannerSessions["s-written"].artifact_id;

  // 2. Resume the same session (as after a reload: it now carries artifact_id) -> same quiz, answer restored.
  setPage("overview"); await sleep(100);
  out.writtenResume = await plannerOpen({...window.__plannerSessions["s-written"]});
  await exitPlayer();

  // 3. A normal quiz session without artifact resumes the NORMAL in-progress quiz, never the practice one.
  setPage("overview"); await sleep(100);
  out.normalNoArtifact = await plannerOpen(session("s-normal", "quiz", null, "scheduled", {code: "new_material", message: "New material to learn"}));
  await exitPlayer();

  // 4. A normal quiz session with its artifact opens exactly that quiz.
  setPage("overview"); await sleep(100);
  out.normalExact = await plannerOpen(session("s-normal-c", "quiz", "quiz-c", "scheduled", {code: "new_material", message: "New material to learn"}));
  await exitPlayer();

  // 5. No flashcards: the written_quiz session opens Flashcards, not a quiz.
  window.__cardsAvailable = false;
  setPage("overview"); await sleep(100);
  out.noCards = await plannerOpen(session("s-written-2", "written_quiz", null, "scheduled", PRACTICE_REASON));

  // 6. Submitting practice never triggers planner adaptation; a normal quiz submission does.
  const planCalls = () => window.__calls.filter((call) => call.includes("/api/planner/plans")).length;
  await openQuizPlayer({document_id: "lecture.pdf", topic_id: "document", difficulty: "easy", quiz_id: "fc-quiz"}); await sleep(300);
  const before = planCalls();
  const submitsBefore = window.__submitCalls;
  await submitQuizPlayer(); await sleep(500);
  out.practiceSubmitted = window.__submitCalls - submitsBefore === 1 && window.__lastSubmitBody.quiz_id === "fc-quiz";
  out.practicePlanCalls = planCalls() - before;
  await closeQuizPlayerToLibrary(); await sleep(200);
  await openQuizPlayer({document_id: "lecture.pdf", topic_id: "document", difficulty: "easy", quiz_id: "quiz-c"}); await sleep(300);
  const beforeNormal = planCalls();
  await submitQuizPlayer(); await sleep(500);
  out.normalPlanCalls = planCalls() - beforeNormal;

  out.reasonText = pcalReasonText({code: "retrieval_practice", message: ""});
  out.activityLabel = pcalActivity("written_quiz");
  out.startCalls = window.__startCalls;
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error);
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerWrittenQuizUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="planner_written_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        shutil.copytree(FRONTEND / "js", work / "js")
        (work / "app-config.js").write_text(MOCK, encoding="utf-8")
        (work / "driver.js").write_text(DRIVER, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=45000", "--dump-dom",
             f"--user-data-dir={work / 'profile'}", (work / "index.html").as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        cls.out = json.loads(html.unescape(match.group(1)))

    def assertInPlayer(self, state, quiz_id):
        self.assertEqual((state["page"], state["tab"], state["document"]), ("session", "quiz", "lecture.pdf"))
        self.assertTrue(state["playerOpen"])
        self.assertEqual(state["quizId"], quiz_id)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_written_quiz_session_is_labelled_and_styled_as_retrieval_practice(self):
        card = self.out["writtenStart"]["card"]
        self.assertEqual(card["chip"], "Written Quiz · Retrieval practice")
        self.assertEqual(card["reason"], "Scheduled to reinforce recall before your next assessment")
        self.assertIn("planner-session--written_quiz", card["className"])
        self.assertEqual(card["borderLeft"], "4px")                                  # distinct from a normal Quiz
        self.assertEqual(self.out["normalExact"]["card"]["chip"], "Quiz")
        self.assertNotEqual(self.out["normalExact"]["card"]["borderLeft"], "4px")
        self.assertEqual(self.out["activityLabel"], "Written Quiz")
        self.assertEqual(self.out["reasonText"], "Retrieval practice: scheduled to reinforce recall before your next assessment.")

    def test_start_opens_the_exact_written_practice_quiz(self):
        start = self.out["writtenStart"]
        self.assertEqual(start["label"], "Start")
        self.assertInPlayer(start, "fc-quiz")                        # never the in-progress normal quiz-b
        self.assertEqual(start["title"], "Lecture · Flashcard practice (Mixed)")
        self.assertEqual(self.out["linkedArtifact"], "fc-quiz")
        self.assertEqual(self.out["autosave"]["quiz_id"], "fc-quiz")
        self.assertEqual(self.out["autosave"]["answers"], {"1": "the Scheduler"})

    def test_resume_reopens_the_same_quiz_with_the_typed_answer(self):
        resume = self.out["writtenResume"]
        self.assertEqual(resume["label"], "Resume")
        self.assertInPlayer(resume, "fc-quiz")
        self.assertEqual(resume["answers"], {"1": "the Scheduler"})
        self.assertEqual(resume["inputValue"], "the Scheduler")

    def test_normal_quiz_sessions_still_open_normal_quizzes(self):
        self.assertInPlayer(self.out["normalNoArtifact"], "quiz-b")   # not the newer practice quiz
        self.assertInPlayer(self.out["normalExact"], "quiz-c")

    def test_without_flashcards_the_session_opens_flashcards(self):
        no_cards = self.out["noCards"]
        self.assertEqual((no_cards["page"], no_cards["tab"]), ("session", "flashcards"))
        self.assertFalse(no_cards["playerOpen"])

    def test_practice_submission_never_adapts_the_plan(self):
        self.assertTrue(self.out["practiceSubmitted"])
        self.assertEqual(self.out["practicePlanCalls"], 0)
        self.assertGreater(self.out["normalPlanCalls"], 0)

    def test_start_calls(self):
        self.assertEqual(self.out["startCalls"], ["s-written", "s-written", "s-normal", "s-normal-c", "s-written-2"])


if __name__ == "__main__":
    unittest.main()
