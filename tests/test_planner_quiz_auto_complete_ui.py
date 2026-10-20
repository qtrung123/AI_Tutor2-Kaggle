"""Real-browser tests for the frontend side of quiz-driven Planner completion (headless Chrome,
mocked API from the Today's Study Plan tests). Starting a session with a quiz activity remembers it
for this tab; the quiz submission for that document carries it as planner_session_id (never for
another document's quiz); the server's planner_completion refreshes Home and the Planner without a
manual reload; a refresh keeps the link and starting another session replaces it. Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_today_ui import MOCK as TODAY_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = TODAY_MOCK + r"""
window.__submits = [];
window.__completion = null;
const todayFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const submit = p.match(/^\/api\/quiz\/([^/]+)\/submit$/);
  if (submit) {
    const body = JSON.parse(init.body);
    window.__submits.push({document: decodeURIComponent(submit[1]), body});
    return json({attempt_id: "att-" + window.__submits.length, quiz_id: body.quiz_id, score: 2, total: 2, attempt_number: window.__submits.length,
      completed: true, percentage: 100, question_results: [], attempt_summary: {attempts: window.__submits.length},
      planner_completion: window.__completion});
  }
  return todayFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const P = window.__planner;
const stored = () => JSON.parse(sessionStorage.getItem("aiTutor.plannerQuizSession") || "null");
const quizFor = (document_id) => ({quiz_id: "q-" + document_id, document_id, difficulty: "easy", topic_id: "document", questions: []});
const session = (id, document_id, activity, extra = {}) => ({session_id: id, plan_id: "plan-1", document_id, document_title: document_id,
  activity_type: activity, scheduled_start: "2026-09-24T14:00:00", scheduled_end: "2026-09-24T14:35:00", duration_minutes: 35,
  status: "scheduled", reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, ...extra});
const group = () => { const g = document.createElement("div"); const b = document.createElement("button"); g.appendChild(b); document.body.appendChild(g); return [b, g]; };
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  sessionStorage.clear();
  const combinedQuiz = session("s-combined", "mkt.pdf", "flashcards", {steps: [
    {activity_type: "flashcards", duration_minutes: 15, artifact_id: null, status: "pending", completed_at: null},
    {activity_type: "quiz", duration_minutes: 20, artifact_id: null, status: "pending", completed_at: null}]});
  const summary = session("s-summary", "stats.pdf", "summary");
  P.plans = [{plan_id: "plan-1", title: "Plan", status: "active"}];
  P.sessionsByPlan = {"plan-1": [combinedQuiz, summary]};

  // 1) Starting a session with a quiz step remembers it (even while its first step is current).
  let [button, g] = group();
  await plannerSessionAction(combinedQuiz, "start", button, g); await sleep(300);
  out.afterStart = stored();

  // 2) Its document's quiz carries the session; another document's quiz never does.
  currentQuiz = quizFor("stats.pdf"); quizAnswers = {};
  await requestQuizSubmission();
  currentQuiz = quizFor("mkt.pdf");
  await requestQuizSubmission();
  out.submitted = window.__submits.map((s) => ({document: s.document, session: s.body.planner_session_id || null}));

  // 3) A refresh keeps the link (sessionStorage), so the next submission still names the session.
  out.afterReload = plannerQuizSessionFor("mkt.pdf");

  // 4) The server completed only the quiz step: toast, Home + Planner refresh, the link is kept.
  const before = P.calls.length;
  await plannerAfterQuizSubmission({status: "completed", session_id: "s-combined", changed: true, session_completed: false, step_index: 1,
    session: {...combinedQuiz, status: "in_progress", steps: [{...combinedQuiz.steps[0]}, {...combinedQuiz.steps[1], status: "completed"}]}});
  out.stepToast = document.getElementById("toast")?.textContent || null;
  out.stepRefresh = P.calls.slice(before).filter((c) => c.startsWith("GET"));
  out.afterStep = stored();

  // 5) The whole session completed: the link is forgotten.
  await plannerAfterQuizSubmission({status: "completed", session_id: "s-combined", changed: true, session_completed: true, step_index: 1,
    session: {...combinedQuiz, status: "completed", steps: combinedQuiz.steps.map((s) => ({...s, status: "completed"}))}});
  out.doneToast = document.getElementById("toast")?.textContent || null;
  out.afterDone = stored();

  // 6) An unchanged / refused / missing result neither toasts nor refreshes.
  const calls = P.calls.length;
  for (const result of [null, {status: "unchanged", session_id: "x", changed: false}, {status: "not_applied", code: "quiz_session_mismatch"},
                        {status: "error"}]) await plannerAfterQuizSubmission(result);
  out.quietCalls = P.calls.length - calls;

  // 7) Starting a session without a quiz replaces the link: no session is sent any more.
  [button, g] = group();
  await plannerSessionAction(combinedQuiz, "start", button, g); await sleep(300);
  [button, g] = group();
  await plannerSessionAction(summary, "start", button, g); await sleep(300);
  out.afterOtherStart = stored();
  currentQuiz = quizFor("mkt.pdf");
  await requestQuizSubmission();
  out.lastSubmitSession = window.__submits[window.__submits.length - 1].body.planner_session_id || null;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


def run_at_width(width, height):
    original = (sidebar_harness.MOCK, sidebar_harness.DRIVER)
    sidebar_harness.MOCK, sidebar_harness.DRIVER = MOCK, DRIVER
    try:
        return sidebar_harness.run_at_width(width, height)
    finally:
        sidebar_harness.MOCK, sidebar_harness.DRIVER = original


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PlannerQuizCompletionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_start_remembers_a_session_with_a_quiz_step(self):
        self.assertEqual(self.out["afterStart"], {"session_id": "s-combined", "document_id": "mkt.pdf"})

    def test_only_the_same_documents_quiz_carries_the_session(self):
        self.assertEqual(self.out["submitted"], [{"document": "stats.pdf", "session": None},
                                                 {"document": "mkt.pdf", "session": "s-combined"}])
        self.assertEqual(self.out["afterReload"], "s-combined")

    def test_a_completed_quiz_step_refreshes_home_and_planner_and_keeps_the_link(self):
        self.assertEqual(self.out["stepToast"], "Quiz step done. Finish Flashcards to complete the session")
        self.assertIn("GET /api/planner/plans", self.out["stepRefresh"])
        self.assertEqual(self.out["afterStep"]["session_id"], "s-combined")

    def test_a_completed_session_forgets_the_link(self):
        self.assertEqual(self.out["doneToast"], "Study session completed. Nice work!")
        self.assertIsNone(self.out["afterDone"])

    def test_nothing_happens_without_a_completion(self):
        self.assertEqual(self.out["quietCalls"], 0)

    def test_starting_another_session_replaces_the_link(self):
        self.assertIsNone(self.out["afterOtherStart"])
        self.assertIsNone(self.out["lastSubmitSession"])


if __name__ == "__main__":
    unittest.main()
