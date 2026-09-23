"""Real-browser tests for the focused Quiz Player (frontend/app.js in headless Chrome against a
mocked API). Covers Start/Resume opening the player, answering/autosave, free Previous/Next
navigation, Exit with/without progress, Finish with unanswered questions, duplicate-Finish
blocking, autosave failure resilience, progress isolation between two sibling quizzes sharing the
same document/scope/difficulty, and that the Library/Create Quiz sheet are hidden while the player
is open. Does not touch Create Quiz sheet generation itself (see test_create_quiz_sheet_ui.py) or
Flashcards/Summary/Overview/Sidebar.

Skipped when Chrome is not installed (set CHROME_PATH to point at it). No backend, no Ollama, no
network.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def find_chrome():
    candidates = [os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    return next((path for path in candidates if path and Path(path).exists()), None)


MOCK = r"""
window.APP_CONFIG = { API_BASE_URL: "" };
window.__calls = [];
window.__failNextProgress = false;
// Holding a PATCH keeps it "in flight" until the driver calls __releaseProgress().
window.__holdProgress = false;
window.__heldProgress = [];
window.__progressBodies = [];
window.__releaseProgress = () => { window.__holdProgress = false; window.__heldProgress.splice(0).forEach((release) => release()); };
const QUESTIONS = [
  {id: 1, question: "Q1?", options: ["A. a", "B. b", "C. c", "D. d"], correct_answer: "A", question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: []},
  {id: 2, question: "Q2?", options: ["A. a", "B. b", "C. c", "D. d"], correct_answer: "B", question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: []},
  {id: 3, question: "Q3?", options: ["A. a", "B. b", "C. c", "D. d"], correct_answer: "C", question_type: "single_choice", topic_id: "document", topic_name: "", difficulty: "easy", explanation: "e", source_chunk_ids: []},
];
function makeQuiz(quizId, title, modelId) {
  return {quiz_id: quizId, document_id: "lecture.pdf", title, difficulty: "easy", topic_id: "document", topic_name: "",
    assessment_scope: "document", generation_model: {model_id: modelId, name: modelId}, questions: QUESTIONS,
    created_at: "2026-01-01T00:00:00Z"};
}
window.__quizzes = {
  "quiz-a": makeQuiz("quiz-a", "Quiz A", "qwen-2.5-7b"),
  "quiz-b": makeQuiz("quiz-b", "Quiz B", "gemma3-12b"),
  "quiz-c": makeQuiz("quiz-c", "Quiz C", "qwen-2.5-7b"),
};
window.__attempts = {};
window.__submitCalls = 0;
window.__library = () => Object.values(window.__quizzes).map((quiz) => {
  const attempt = window.__attempts[quiz.quiz_id];
  const completed = Boolean(attempt && attempt.completed);
  const answered = attempt ? Object.keys(attempt.answers || {}).length : 0;
  return {
    quiz_id: quiz.quiz_id, title: quiz.title, topic_id: "document", topic_name: "", assessment_scope: "document",
    difficulty: quiz.difficulty, question_count: QUESTIONS.length, requested_count: QUESTIONS.length, status: "complete",
    model_id: quiz.generation_model.model_id, model_name: quiz.generation_model.name,
    created_at: quiz.created_at, updated_at: (attempt && attempt.updated_at) || quiz.created_at,
    progress_status: completed ? "completed" : (answered > 0 ? "in_progress" : "not_started"),
    answered, total: QUESTIONS.length, score: completed ? attempt.score : null, percentage: completed ? attempt.percentage : null,
  };
});
const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  window.__calls.push(method + " " + url);
  const u = new URL(url, "http://x");
  const p = u.pathname;
  if (p === "/api/auth/me") return json({id: "u1", display_name: "Tester", email: "t@example.com", role: "user"});
  if (p === "/api/models") return json({models: [
    {id: "qwen-2.5-7b", label: "Qwen 2.5 7B", default: true, ready: true},
    {id: "gemma3-12b", label: "Gemma 3 12B", default: false, ready: true}]});
  if (p.startsWith("/api/models/") && p.endsWith("/prepare")) return json({status: "ready", ready: true});
  if (p === "/api/documents") return json([
    {id: "lecture.pdf", title: "Lecture", chunks: 12, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2},
    {id: "notes.pdf", title: "Notes", chunks: 4, topics: [{topic_id: "t1", name: "Topic 1"}], topic_schema_version: 2}]);
  if (p === "/api/quizzes") return json([{document_id: "lecture.pdf", title: "Lecture", chunks: 12, has_quiz: true, variants: window.__library()}]);
  if (p === "/api/quiz-history") return json([]);
  const progressMatch = p.match(/^\/api\/quiz\/([^/]+)\/progress$/);
  if (progressMatch && method === "PATCH") {
    const body = JSON.parse(init.body);
    window.__lastProgressBody = body;
    window.__progressBodies.push({document: progressMatch[1], ...body});
    if (window.__holdProgress) await new Promise((release) => window.__heldProgress.push(release));
    if (window.__failNextProgress) { window.__failNextProgress = false; return json({detail: "simulated failure"}, 500); }
    const quizId = body.quiz_id;
    const previous = window.__attempts[quizId] || {attempt_id: "att-" + quizId, quiz_id: quizId, answers: {}, current_question_index: 0, completed: false};
    let answers = {...previous.answers};
    if (body.answers) answers = {...body.answers};   // the Quiz Player's full snapshot replaces
    if (body.question_id) answers[String(body.question_id)] = body.selected_answer;
    const updated = {
      ...previous, answers, answered: Object.keys(answers).length, total: QUESTIONS.length, completed: false, completed_at: null,
      current_question_index: body.current_question_index != null ? body.current_question_index : previous.current_question_index,
      updated_at: "2026-01-01T00:01:00Z",
    };
    window.__attempts[quizId] = updated;
    return json(updated);
  }
  const submitMatch = p.match(/^\/api\/quiz\/([^/]+)\/submit$/);
  if (submitMatch && method === "POST") {
    window.__submitCalls += 1;
    const body = JSON.parse(init.body);
    window.__lastSubmitBody = body;
    const quiz = window.__quizzes[body.quiz_id];
    let score = 0;
    quiz.questions.forEach((question) => { if ((body.answers[String(question.id)] || "") === question.correct_answer) score += 1; });
    const total = quiz.questions.length;
    const completedAttempt = {
      attempt_id: "att-" + body.quiz_id + "-" + window.__submitCalls, quiz_id: body.quiz_id, answers: body.answers,
      completed: true, completed_at: "2026-01-01T00:02:00Z", score, total, percentage: Math.round((100 * score) / total),
      current_question_index: 0, updated_at: "2026-01-01T00:02:00Z", attempt_number: 1,
      attempt_summary: {attempts: 1, latest_score: Math.round((100 * score) / total), best_score: Math.round((100 * score) / total), average_score: Math.round((100 * score) / total)},
    };
    window.__attempts[body.quiz_id] = completedAttempt;
    return json(completedAttempt);
  }
  const quizMatch = p.match(/^\/api\/quiz\/([^/]+)$/);
  if (quizMatch && method === "GET") {
    const quizId = u.searchParams.get("quiz_id");
    const quiz = quizId ? window.__quizzes[quizId] : null;
    const attempt = quizId ? (window.__attempts[quizId] || null) : null;
    return json({document_id: quizMatch[1], difficulty: u.searchParams.get("difficulty"), topic_id: u.searchParams.get("topic_id"), quiz: quiz || null, latest_attempt: attempt, attempt_summary: null});
  }
  if (p.startsWith("/api/summary/")) return json({status: "not_generated"});
  if (p.startsWith("/api/flashcards/")) return json({status: "not_generated", cards: []});
  if (p === "/api/conversations" && method === "POST") return json({id: "c1", title: "New conversation", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p.startsWith("/api/conversations/")) return json({id: "c1", title: "t", document_id: "lecture.pdf", document_ids: ["lecture.pdf"], messages: []});
  if (p === "/api/conversations") return json([]);
  if (p === "/api/dashboard") return json({mastery: [], materials: [], summary: {}});
  if (p === "/api/sources") return json([]);
  return json([]);
};
"""

DRIVER = r"""
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno + ":" + e.colno + (e.error && e.error.stack ? " | " + e.error.stack : "")));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const pane = () => document.querySelector('[data-session-pane="quiz"]');
const playerOpen = () => pane().classList.contains("quiz-player-open");
const cardFor = (title) => [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === title);
const startButtonFor = (title) => cardFor(title)?.querySelector(".quiz-history-actions button");
const answerCards = () => [...document.querySelectorAll(".quiz-player-answer-card")];
const clickAnswer = (letter) => answerCards().find((b) => b.querySelector(".quiz-player-answer-label").textContent.trim().startsWith(letter + "."))?.click();
(async () => {
  await sleep(1500);
  await openStudySession("lecture.pdf", "quiz");
  await sleep(300);

  // 1) Start opens the focused player for the exact quiz_id and hides the Library/Create UI.
  startButtonFor("Quiz A").click();
  await sleep(300);
  out.opened = {
    playerOpen: playerOpen(),
    playerVisible: !document.getElementById("quiz-player").hidden,
    libraryHidden: getComputedStyle(document.querySelector(".assessment-shell")).display === "none",
    createHeaderHidden: getComputedStyle(document.querySelector(".quiz-landing-header")).display === "none",
    title: document.getElementById("quiz-player-title").textContent,
    difficulty: document.getElementById("quiz-player-difficulty").textContent,
    model: document.getElementById("quiz-player-model").textContent,
    position: document.getElementById("quiz-player-position").textContent,
    answeredCount: document.getElementById("quiz-player-answered-count").textContent,
    previousDisabled: document.getElementById("quiz-player-previous").disabled,
    nextLabel: document.getElementById("quiz-player-next").textContent,
    answerCount: answerCards().length,
  };

  // 2) Selecting an answer updates the UI immediately and autosaves (debounced) for this exact quiz_id.
  clickAnswer("A");
  out.immediatelyAfterClick = { selectedClass: answerCards()[0].classList.contains("selected") };
  await sleep(700);
  out.afterAnswerAutosave = {
    body: window.__lastProgressBody, saveStatus: document.getElementById("quiz-player-save-status").textContent,
  };

  // 3) Next never requires an answer; position autosaves too.
  document.getElementById("quiz-player-next").click();
  await sleep(700);
  out.afterNext = {
    position: document.getElementById("quiz-player-position").textContent,
    previousDisabled: document.getElementById("quiz-player-previous").disabled,
    body: window.__lastProgressBody,
  };

  // Previous returns to Q1 with the answer still shown selected (local state, not re-fetched).
  document.getElementById("quiz-player-previous").click();
  await sleep(100);
  out.afterPrevious = { position: document.getElementById("quiz-player-position").textContent, stillSelected: answerCards()[0].classList.contains("selected") };

  // 4) Exit with progress shows a confirmation; Continue Quiz keeps the player open.
  document.getElementById("quiz-player-exit").click();
  await sleep(100);
  out.exitConfirm = { visible: !document.getElementById("quiz-exit-confirm").hidden, detail: document.getElementById("quiz-exit-confirm-detail").textContent };
  document.getElementById("quiz-exit-confirm-continue").click();
  await sleep(100);
  out.afterContinue = { playerOpen: playerOpen(), confirmHidden: document.getElementById("quiz-exit-confirm").hidden };

  // Exit for real returns to the Library; the card now shows In Progress / Resume.
  document.getElementById("quiz-player-exit").click();
  await sleep(100);
  document.getElementById("quiz-exit-confirm-exit").click();
  await sleep(300);
  const quizACard = cardFor("Quiz A");
  out.afterExit = {
    playerOpen: playerOpen(), isLanding: pane().classList.contains("quiz-landing"),
    cardStatus: quizACard.querySelector(".quiz-history-scope").textContent,
    cardDetail: quizACard.querySelector("small:last-child").textContent,
    buttonLabel: startButtonFor("Quiz A").textContent,
  };

  // 4b) A transient autosave failure keeps the locally-selected answer and local navigation working
  // -- it never reverts the UI or blocks Next/Previous. Uses a separate quiz (Quiz C) so this does
  // not disturb Quiz A's/Quiz B's own answer/position state used by the steps around it.
  startButtonFor("Quiz C").click();
  await sleep(300);
  clickAnswer("A");
  await sleep(700);   // establishes a normal, successful baseline save
  window.__failNextProgress = true;
  clickAnswer("B");   // changes the answer -- this autosave will fail
  out.beforeAutosaveFailure = { selectedLabel: answerCards().find((b) => b.classList.contains("selected"))?.querySelector(".quiz-player-answer-label").textContent };
  await sleep(700);
  out.afterAutosaveFailure = {
    saveStatus: document.getElementById("quiz-player-save-status").textContent,
    stillSelectedB: answerCards()[1].classList.contains("selected"),
  };
  document.getElementById("quiz-player-next").click();
  await sleep(100);
  out.afterFailureNext = { position: document.getElementById("quiz-player-position").textContent };
  document.getElementById("quiz-player-exit").click(); await sleep(50);
  document.getElementById("quiz-exit-confirm-exit").click(); await sleep(300);
  const attemptSnapshot = (quizId) => ({ answers: {...window.__attempts[quizId].answers}, index: window.__attempts[quizId].current_question_index });
  // Exit flushed the failed answer + new position: the server now has C's local state.
  out.quizCAfterExit = attemptSnapshot("quiz-c");

  // 4c) Resume restores the exact saved position (Q2), not just the first question.
  startButtonFor("Quiz C").click();
  await sleep(300);
  out.resumeC = { position: document.getElementById("quiz-player-position").textContent, q1Answered: document.getElementById("quiz-player-answered-count").textContent };
  // Answer then navigate inside the debounce window, and Exit while that save fails: the dialog
  // must not claim the progress is saved, and Continue Quiz keeps the local answers.
  clickAnswer("D");
  document.getElementById("quiz-player-next").click();
  window.__failNextProgress = true;
  document.getElementById("quiz-player-exit").click();
  await sleep(150);
  out.exitAfterFailedSave = { visible: !document.getElementById("quiz-exit-confirm").hidden, title: document.getElementById("quiz-exit-confirm-title").textContent };
  document.getElementById("quiz-exit-confirm-continue").click(); await sleep(50);
  out.afterFailedSaveContinue = { position: document.getElementById("quiz-player-position").textContent, answered: document.getElementById("quiz-player-answered-count").textContent };
  document.getElementById("quiz-player-exit").click();
  await sleep(150);
  out.exitAfterRetry = { title: document.getElementById("quiz-exit-confirm-title").textContent };
  document.getElementById("quiz-exit-confirm-exit").click(); await sleep(300);
  out.quizCFinal = attemptSnapshot("quiz-c");

  // 5) Progress is isolated: open Quiz B, answer differently, exit -- must never affect Quiz A.
  startButtonFor("Quiz B").click();
  await sleep(300);
  out.quizBOpened = { title: document.getElementById("quiz-player-title").textContent, position: document.getElementById("quiz-player-position").textContent };
  clickAnswer("B");
  await sleep(700);
  document.getElementById("quiz-player-exit").click();
  await sleep(100);
  document.getElementById("quiz-exit-confirm-exit").click();
  await sleep(300);

  // 6) Resume A restores only A's answer/position; Resume B restores only B's.
  const quizAButtonLabelBeforeResume = startButtonFor("Quiz A").textContent;
  startButtonFor("Quiz A").click();
  await sleep(300);
  out.resumeA = {
    buttonWas: quizAButtonLabelBeforeResume, position: document.getElementById("quiz-player-position").textContent,
    answeredCount: document.getElementById("quiz-player-answered-count").textContent,
    q1Selected: answerCards()[0].classList.contains("selected"),
  };
  document.getElementById("quiz-player-exit").click(); await sleep(50);
  document.getElementById("quiz-exit-confirm-continue").click(); await sleep(50);

  // 7) Finish with unanswered questions shows a confirmation; Review Unanswered jumps to the first one.
  document.getElementById("quiz-player-next").click(); await sleep(50);   // Q2
  document.getElementById("quiz-player-next").click(); await sleep(50);   // Q3 (last) -- Next is now "Finish Quiz"
  out.lastQuestion = { nextLabel: document.getElementById("quiz-player-next").textContent };
  document.getElementById("quiz-player-next").click();
  await sleep(100);
  out.finishConfirm = { visible: !document.getElementById("quiz-finish-confirm").hidden, title: document.getElementById("quiz-finish-confirm-title").textContent };
  document.getElementById("quiz-finish-review").click();
  await sleep(100);
  out.afterReviewUnanswered = { position: document.getElementById("quiz-player-position").textContent, submitCallsSoFar: window.__submitCalls };

  // 8) Answer the rest, then Finish with everything answered submits immediately (no confirmation)
  // and shows only the minimal completion state.
  clickAnswer("B"); await sleep(700);   // Q2
  document.getElementById("quiz-player-next").click(); await sleep(50);   // Q3
  clickAnswer("C"); await sleep(700);   // Q3
  document.getElementById("quiz-player-next").click();   // Finish Quiz, 3/3 answered -- no confirm
  await sleep(400);
  out.completed = {
    confirmVisible: !document.getElementById("quiz-finish-confirm").hidden,
    completionVisible: !document.getElementById("quiz-player-completion-view").hidden,
    questionViewHidden: document.getElementById("quiz-player-question-view").hidden,
    score: document.getElementById("quiz-player-completion-score").textContent,
    submitCalls: window.__submitCalls,
  };
  document.getElementById("quiz-player-completion-back").click();
  await sleep(300);
  out.afterCompletionBack = { playerOpen: playerOpen(), isLanding: pane().classList.contains("quiz-landing") };

  // 9) Resume B still shows only B's own answer (isolation held across A's whole finish flow).
  startButtonFor("Quiz B").click();
  await sleep(300);
  out.resumeB = {
    answeredCount: document.getElementById("quiz-player-answered-count").textContent,
    q1SelectedB: answerCards()[1].classList.contains("selected"),   // B's own "B" answer
    q1SelectedA: answerCards()[0].classList.contains("selected"),   // never A's "A"
  };

  // 10) Duplicate Finish (rapid double-fire) sends only one submit request. (These answers are
  // deliberately not all correct -- only the request count matters here, not the score.)
  clickAnswer("C"); await sleep(700);   // overwrite Q1's answer
  document.getElementById("quiz-player-next").click(); await sleep(50);   // Q2
  clickAnswer("C"); await sleep(700);
  const submitsBefore = window.__submitCalls;
  submitQuizPlayer(); submitQuizPlayer();
  await sleep(400);
  out.duplicateFinish = { newSubmitCalls: window.__submitCalls - submitsBefore };
  document.getElementById("quiz-player-completion-back").click();
  await sleep(300);

  // 11) Opening an already-completed quiz (Quiz A, finished in step 8) shows only the minimal
  // completion state directly -- never the question-taking chrome.
  startButtonFor("Quiz A").click();
  await sleep(300);
  out.reopenCompletedQuizA = { completionVisible: !document.getElementById("quiz-player-completion-view").hidden };
  document.getElementById("quiz-player-completion-back").click();
  await sleep(300);

  // 12) Session/document switch race: a save is in flight, newer local changes are pending (inside
  // the debounce window), then the learner switches document. The newest snapshot must still land
  // on the OLD quiz, after the in-flight one, and nothing may leak into the new session.
  startButtonFor("Quiz C").click();
  await sleep(300);
  window.__holdProgress = true;
  clickAnswer("A");                  // Q3 = A
  await sleep(700);                  // this save is now in flight (held)
  const heldBody = window.__lastProgressBody;
  document.getElementById("quiz-player-previous").click();   // position -> Q2
  clickAnswer("A");                  // Q2: D -> A (pending, not yet sent)
  const bodiesBeforeSwitch = window.__progressBodies.length;
  await openStudySession("notes.pdf", "quiz");
  await sleep(100);
  const newSessionBeforeRelease = { active: activeDocumentId, playerOpen: playerOpen(), quiz: currentQuiz && currentQuiz.quiz_id, attempt: currentAttempt };
  window.__releaseProgress();
  await quizDetachedSave;
  await sleep(200);
  const oldQuizBodies = window.__progressBodies.slice(bodiesBeforeSwitch);
  out.switchRace = {
    heldBody: { answers: heldBody.answers, index: heldBody.current_question_index },
    sentAfterSwitch: oldQuizBodies.map((body) => ({ document: body.document, quiz_id: body.quiz_id })),
    persisted: attemptSnapshot("quiz-c"),
    newSessionBeforeRelease,
    newSessionAfterRelease: { active: activeDocumentId, playerOpen: playerOpen(), quiz: currentQuiz && currentQuiz.quiz_id, attempt: currentAttempt,
      saveStatusHidden: document.getElementById("quiz-player-save-status").hidden },
  };

  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error); const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class QuizPlayerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="quiz_player_"))
        cls.addClassCleanup(shutil.rmtree, work, ignore_errors=True)
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
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

    def test_the_app_runs_without_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_start_opens_the_focused_player_for_the_exact_quiz_hiding_the_library(self):
        opened = self.out["opened"]
        self.assertTrue(opened["playerOpen"])
        self.assertTrue(opened["playerVisible"])
        self.assertTrue(opened["libraryHidden"])
        self.assertTrue(opened["createHeaderHidden"])   # "+ New Quiz" is hidden while taking a quiz
        self.assertEqual(opened["title"], "Quiz A")
        self.assertEqual(opened["difficulty"], "easy")
        self.assertIn("Qwen 2.5 7B", opened["model"])
        self.assertEqual(opened["position"], "Question 1 of 3")
        self.assertEqual(opened["answeredCount"], "0 answered")
        self.assertTrue(opened["previousDisabled"])
        self.assertEqual(opened["nextLabel"], "Next")
        self.assertEqual(opened["answerCount"], 4)

    def test_selecting_an_answer_updates_ui_immediately_and_autosaves_by_quiz_id(self):
        self.assertTrue(self.out["immediatelyAfterClick"]["selectedClass"])
        saved = self.out["afterAnswerAutosave"]
        self.assertEqual(saved["body"]["quiz_id"], "quiz-a")
        self.assertEqual(saved["body"]["answers"], {"1": "A"})
        self.assertEqual(saved["saveStatus"], "Saved")

    def test_next_never_requires_an_answer_and_persists_position(self):
        after_next = self.out["afterNext"]
        self.assertEqual(after_next["position"], "Question 2 of 3")
        self.assertFalse(after_next["previousDisabled"])
        self.assertEqual(after_next["body"]["current_question_index"], 1)

    def test_previous_returns_to_the_prior_question_with_its_answer_still_selected(self):
        after_previous = self.out["afterPrevious"]
        self.assertEqual(after_previous["position"], "Question 1 of 3")
        self.assertTrue(after_previous["stillSelected"])

    def test_exit_with_progress_shows_a_confirmation_and_continue_keeps_the_player_open(self):
        confirm = self.out["exitConfirm"]
        self.assertTrue(confirm["visible"])
        self.assertIn("1 of 3 answered", confirm["detail"])
        after_continue = self.out["afterContinue"]
        self.assertTrue(after_continue["playerOpen"])
        self.assertTrue(after_continue["confirmHidden"])

    def test_exit_returns_to_the_library_which_shows_in_progress_and_resume(self):
        after_exit = self.out["afterExit"]
        self.assertFalse(after_exit["playerOpen"])
        self.assertTrue(after_exit["isLanding"])
        self.assertEqual(after_exit["cardStatus"], "In Progress")
        self.assertIn("1 / 3 answered", after_exit["cardDetail"])
        self.assertEqual(after_exit["buttonLabel"], "Resume")

    def test_autosave_failure_keeps_the_locally_selected_answer_and_local_navigation_working(self):
        self.assertEqual(self.out["beforeAutosaveFailure"]["selectedLabel"], "B. b")
        after_failure = self.out["afterAutosaveFailure"]
        self.assertIn("save", after_failure["saveStatus"].lower())
        self.assertTrue(after_failure["stillSelectedB"])   # local state was never reverted
        self.assertEqual(self.out["afterFailureNext"]["position"], "Question 2 of 3")   # Next still works

    def test_exit_flushes_pending_and_failed_saves_before_leaving(self):
        self.assertEqual(self.out["quizCAfterExit"], {"answers": {"1": "B"}, "index": 1})

    def test_resume_restores_the_exact_saved_question_position(self):
        self.assertEqual(self.out["resumeC"]["position"], "Question 2 of 3")
        self.assertEqual(self.out["resumeC"]["q1Answered"], "1 answered")

    def test_exit_after_a_failed_save_says_so_and_continue_keeps_local_answers(self):
        failed = self.out["exitAfterFailedSave"]
        self.assertTrue(failed["visible"])
        self.assertIn("not saved", failed["title"])
        kept = self.out["afterFailedSaveContinue"]
        self.assertEqual(kept["position"], "Question 3 of 3")
        self.assertEqual(kept["answered"], "2 answered")
        self.assertEqual(self.out["exitAfterRetry"]["title"], "Your progress is saved")

    def test_an_answer_made_just_before_navigating_is_never_dropped_by_the_debounce(self):
        self.assertEqual(self.out["quizCFinal"], {"answers": {"1": "B", "2": "D"}, "index": 2})

    def test_switching_document_mid_save_persists_the_newest_snapshot_to_the_old_quiz_only(self):
        race = self.out["switchRace"]
        # The in-flight save carried the older snapshot (Q3 answered, position Q3).
        self.assertEqual(race["heldBody"], {"answers": {"1": "B", "2": "D", "3": "A"}, "index": 2})
        # After the switch, exactly one newer save was sent -- to the old quiz, queued behind it.
        self.assertEqual(race["sentAfterSwitch"], [{"document": "lecture.pdf", "quiz_id": "quiz-c"}])
        self.assertEqual(race["persisted"], {"answers": {"1": "B", "2": "A", "3": "A"}, "index": 1})
        # The new session never sees the old quiz or its (stale) save responses.
        for state in (race["newSessionBeforeRelease"], race["newSessionAfterRelease"]):
            self.assertEqual(state["active"], "notes.pdf")
            self.assertFalse(state["playerOpen"])
            self.assertIsNone(state["quiz"])
            self.assertIsNone(state["attempt"])
        self.assertTrue(race["newSessionAfterRelease"]["saveStatusHidden"])

    def test_progress_is_isolated_between_sibling_quizzes(self):
        self.assertEqual(self.out["quizBOpened"]["title"], "Quiz B")
        self.assertEqual(self.out["quizBOpened"]["position"], "Question 1 of 3")

    def test_resume_restores_each_quizs_own_answers_and_position_only(self):
        resume_a = self.out["resumeA"]
        self.assertEqual(resume_a["buttonWas"], "Resume")
        self.assertEqual(resume_a["position"], "Question 1 of 3")
        self.assertEqual(resume_a["answeredCount"], "1 answered")
        self.assertTrue(resume_a["q1Selected"])   # A's own "A" answer, not B's "B"

    def test_finish_with_unanswered_questions_shows_a_confirmation_and_review_unanswered_jumps_to_it(self):
        self.assertEqual(self.out["lastQuestion"]["nextLabel"], "Finish Quiz")
        confirm = self.out["finishConfirm"]
        self.assertTrue(confirm["visible"])
        self.assertIn("2 questions unanswered", confirm["title"])
        after_review = self.out["afterReviewUnanswered"]
        self.assertEqual(after_review["position"], "Question 2 of 3")
        self.assertEqual(after_review["submitCallsSoFar"], 0)   # Review Unanswered never submits

    def test_finishing_fully_answered_submits_immediately_and_shows_only_the_minimal_completion_state(self):
        completed = self.out["completed"]
        self.assertTrue(completed["confirmVisible"] is False)
        self.assertTrue(completed["completionVisible"])
        self.assertTrue(completed["questionViewHidden"])
        self.assertEqual(completed["score"], "Score 3 / 3")   # Q1=A, Q2=B, Q3=C -- all three correct
        self.assertEqual(completed["submitCalls"], 1)

    def test_completion_back_returns_to_the_library(self):
        after_back = self.out["afterCompletionBack"]
        self.assertFalse(after_back["playerOpen"])
        self.assertTrue(after_back["isLanding"])

    def test_quiz_b_progress_survived_quiz_as_entire_finish_flow_untouched(self):
        resume_b = self.out["resumeB"]
        self.assertEqual(resume_b["answeredCount"], "1 answered")
        self.assertTrue(resume_b["q1SelectedB"])
        self.assertFalse(resume_b["q1SelectedA"])

    def test_duplicate_finish_is_blocked(self):
        self.assertEqual(self.out["duplicateFinish"]["newSubmitCalls"], 1)

    def test_opening_an_already_completed_quiz_shows_only_the_completion_state(self):
        self.assertTrue(self.out["reopenCompletedQuizA"]["completionVisible"])


if __name__ == "__main__":
    unittest.main()
