"""Real-browser tests: Study Planner Start/Resume of a quiz or quiz_retry session opens the same
focused Quiz Player as the Quiz library's Start/Resume -- never the older inline quiz view -- for the
exact quiz_id, with its saved answers and position. Headless Chrome against the Quiz Player mock
(tests/test_quiz_player_ui.py) plus the planner's session start endpoint.

Like the real backend, a quiz-detail request WITHOUT quiz_id answers with the document's default
quiz: that is what used to render the legacy inline view on the planner path.
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
window.__startCalls = [];
window.__detailRequests = [];
const quizFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const u = new URL(url, "http://x");
  const p = u.pathname;
  const start = p.match(/^\/api\/planner\/sessions\/([^/]+)\/start$/);
  if (start) {
    const session = window.__plannerSessions[start[1]];
    window.__startCalls.push(start[1]);
    const started = session.status === "scheduled";
    session.status = "in_progress";
    return json({session: {...session}, started, tool: "quiz", artifact_available: Boolean(session.artifact_id)});
  }
  const detail = p.match(/^\/api\/quiz\/([^/]+)$/);
  if (detail && (init.method || "GET").toUpperCase() === "GET") {
    window.__detailRequests.push(u.searchParams.get("quiz_id"));
    if (!u.searchParams.get("quiz_id")) {   // the document's default quiz, as the backend does
      return json({document_id: detail[1], quiz: window.__quizzes["quiz-a"], latest_attempt: window.__attempts["quiz-a"] || null, attempt_summary: null});
    }
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
  playerOpen: pane().classList.contains("quiz-player-open"), playerVisible: !$("quiz-player").hidden,
  // the older inline view: its question cards in the Quiz pane (the player's own view is separate)
  legacyInline: $("quiz-list").querySelectorAll(".quiz-question-card").length > 0,
  quizId: currentQuiz?.quiz_id || null, title: $("quiz-player-title").textContent,
  position: $("quiz-player-position").textContent, answered: $("quiz-player-answered-count").textContent,
  answers: {...quizAnswers},
  nav: [...$("quiz-player-nav").querySelectorAll(".quiz-player-nav-item")].map((b) => (b.classList.contains("is-current") ? "C" : "") + (b.classList.contains("is-answered") ? "A" : "") || "-"),
  resultsVisible: !$("quiz-results-view").hidden, questionVisible: !$("quiz-player-question-view").hidden,
});
const plannerOpen = async (session) => {
  // The planner's own session card and its Start/Resume button (the same element Home and the
  // calendar use), clicked like a learner would.
  window.__plannerSessions[session.session_id] = session;
  const card = plannerSessionItem({...session, plan_id: "plan-1"}, "div", {action: true});
  document.body.appendChild(card);
  const button = [...card.querySelectorAll("button")].find((b) => b.textContent === "Resume" || b.textContent === "Start");
  const label = button.textContent;
  button.click();
  await sleep(700);
  card.remove();
  return {label, ...state()};
};
const exitPlayer = async () => {
  // Exit asks for confirmation when there is progress (the player's own behavior).
  $("quiz-player-exit").click(); await sleep(100);
  if (!$("quiz-exit-confirm").hidden) { $("quiz-exit-confirm-exit").click(); await sleep(300); }
};
const session = (id, activity, artifactId, status) => ({session_id: id, document_id: "lecture.pdf", document_title: "Lecture",
  activity_type: activity, artifact_id: artifactId, status, scheduled_start: "2026-01-02T10:00:00", scheduled_end: "2026-01-02T10:30:00",
  duration_minutes: 30, reason: {code: "new_material", message: "New material to learn"}});
window.__plannerSessions = {};
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 0, 2, 9, 0, 0);
  window.__detailRequests = [];   // from here on: only what the planner path requests

  // Quiz B is half done: two answers saved, on question 3.
  window.__attempts["quiz-b"] = {attempt_id: "att-quiz-b", quiz_id: "quiz-b", answers: {"1": "A", "2": "C"}, current_question_index: 2,
    completed: false, answered: 2, total: 3, updated_at: "2026-01-01T00:05:00Z"};
  // Quiz A was completed earlier (a quiz_retry session points at it).
  window.__attempts["quiz-a"] = {attempt_id: "att-quiz-a", quiz_id: "quiz-a", answers: {"1": "A", "2": "A", "3": "A"}, completed: true,
    completed_at: "2026-01-01T00:02:00Z", score: 1, total: 3, percentage: 33, current_question_index: 0, updated_at: "2026-01-01T00:02:00Z",
    question_results: []};

  // 1. Resume an in-progress planned quiz -> the player on quiz-b, answers and position kept.
  out.resume = await plannerOpen(session("s-resume", "quiz", "quiz-b", "in_progress"));
  await exitPlayer();
  out.afterExit = state();
  window.__attempts["quiz-b"].answers = {"1": "A", "2": "C"};   // Exit saves the same snapshot

  // 2. Start a fresh planned quiz (not started) -> the player on quiz-c at question 1.
  setPage("overview"); await sleep(100);
  out.start = await plannerOpen(session("s-start", "quiz", "quiz-c", "scheduled"));
  await exitPlayer();

  // 3. quiz_retry on the completed quiz -> the same player (its Results, like the library's Start).
  setPage("overview"); await sleep(100);
  out.retry = await plannerOpen(session("s-retry", "quiz_retry", "quiz-a", "scheduled"));
  await exitPlayer();

  // 4. No artifact on the session: the document's quiz in progress (quiz-b) is resumed.
  setPage("overview"); await sleep(100);
  out.noArtifact = await plannerOpen(session("s-none", "quiz", null, "scheduled"));
  await exitPlayer();

  // 5. No artifact and nothing in progress: the Quiz library -- not the legacy inline quiz.
  delete window.__attempts["quiz-b"];
  setPage("overview"); await sleep(100);
  out.library = await plannerOpen(session("s-lib", "quiz", null, "scheduled"));

  // 6. The normal Library entry still opens the player.
  const card = [...document.querySelectorAll(".quiz-saved-card")].find((c) => c.querySelector("strong")?.textContent === "Quiz C");
  card.querySelector(".quiz-history-actions button").click(); await sleep(400);
  out.libraryEntry = state();

  out.startCalls = window.__startCalls;
  out.detailRequests = window.__detailRequests;
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})().catch((error) => { out.fatal = String(error && error.stack || error);
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerQuizEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="planner_quiz_"))
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

    def assertInPlayer(self, state, quiz_id):
        self.assertEqual((state["page"], state["tab"], state["document"]), ("session", "quiz", "lecture.pdf"))
        self.assertTrue(state["playerOpen"])
        self.assertTrue(state["playerVisible"])
        self.assertFalse(state["legacyInline"])
        self.assertEqual(state["quizId"], quiz_id)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_resume_in_progress_quiz_keeps_quiz_answers_and_position(self):
        resume = self.out["resume"]
        self.assertEqual(resume["label"], "Resume")
        self.assertInPlayer(resume, "quiz-b")
        self.assertEqual(resume["title"], "Quiz B")
        self.assertEqual(resume["answers"], {"1": "A", "2": "C"})
        self.assertRegex(resume["position"], r"3\D+3")          # on question 3 of 3
        self.assertRegex(resume["answered"], r"^2\D")            # 2 answered
        self.assertTrue(resume["questionVisible"])
        self.assertEqual(resume["nav"], ["A", "A", "C"])   # the same question navigator as the Library entry

    def test_exit_returns_to_the_quiz_library(self):
        after = self.out["afterExit"]
        self.assertEqual((after["page"], after["tab"]), ("session", "quiz"))
        self.assertFalse(after["playerOpen"])
        self.assertFalse(after["legacyInline"])

    def test_start_fresh_planned_quiz_opens_the_player_at_question_one(self):
        start = self.out["start"]
        self.assertEqual(start["label"], "Start")
        self.assertInPlayer(start, "quiz-c")
        self.assertEqual(start["answers"], {})
        self.assertRegex(start["position"], r"^\D*1\D+3")

    def test_quiz_retry_uses_the_same_player(self):
        retry = self.out["retry"]
        self.assertInPlayer(retry, "quiz-a")
        self.assertTrue(retry["resultsVisible"])   # a completed quiz opens its Results, as from the library

    def test_without_artifact_the_in_progress_quiz_is_resumed(self):
        self.assertInPlayer(self.out["noArtifact"], "quiz-b")

    def test_without_any_quiz_to_resume_the_library_is_shown_not_the_legacy_view(self):
        library = self.out["library"]
        self.assertEqual((library["page"], library["tab"]), ("session", "quiz"))
        self.assertFalse(library["playerOpen"])
        self.assertFalse(library["legacyInline"])
        self.assertIsNone(library["quizId"])

    def test_normal_library_entry_still_opens_the_player(self):
        self.assertInPlayer(self.out["libraryEntry"], "quiz-c")
        self.assertEqual(len(self.out["libraryEntry"]["nav"]), 3)   # the same question navigator
        self.assertEqual(sum("C" in item for item in self.out["libraryEntry"]["nav"]), 1)

    def test_planner_path_never_loads_the_default_quiz(self):
        self.assertEqual(self.out["startCalls"], ["s-resume", "s-start", "s-retry", "s-none", "s-lib"])
        self.assertNotIn(None, self.out["detailRequests"])   # every detail request names its exact quiz_id


if __name__ == "__main__":
    unittest.main()
