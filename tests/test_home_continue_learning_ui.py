"""Real-browser tests for Home's "Next up" and "Continue learning" (headless Chrome, mocked API reused
from the Today's Study Plan tests). A planned session is shown once, under Today's Study Plan "Next up"
(the Continue card is hidden then, so the session never appears twice), and goes through the Planner's
own Start/Resume lifecycle -- one start request even on a double click, the full session row (plan,
artifact), the tool the backend names -- and then reads Resume. Without a planned session the Continue
card opens the document: no planner request, nothing generated. Runs at desktop (1280px) and phone (390px).
Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_today_ui import MOCK as TODAY_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = TODAY_MOCK + r"""
window.__home = {progress: {}};
const todayFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/dashboard") return json({mastery: [], summary: {}, metrics: {documents: 2}, materials: [
    {document_id: "mkt.pdf", document_name: "Marketing", topic_count: 1}, {document_id: "stats.pdf", document_name: "Statistics", topic_count: 1}]});
  const progress = p.match(/^\/api\/progress\/documents\/(.+)$/);
  if (progress) {
    const id = decodeURIComponent(progress[1]);
    return json(window.__home.progress[id] || {document_id: id, learning: {state: "new", label: "Not started"}, quiz: {latest: null},
      study_pack: null, plan: {next_session: null, deadline: null}});
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
const H = window.__home;
// The progress API's next_session is a reduced row: no plan_id, artifact_id or reason.
const row = (s) => ({session_id: s.session_id, document_id: s.document_id, activity_type: s.activity_type,
  scheduled_start: s.scheduled_start, scheduled_end: s.scheduled_end, duration_minutes: s.duration_minutes, status: s.status});
const learning = (id, next) => ({document_id: id, learning: {state: "learning", label: "Learning"}, quiz: {latest: null}, study_pack: null,
  plan: {next_session: next ? row(next) : null, deadline: null}});
const button = () => document.querySelector("#home-continue .home-continue-open");
const state = () => ({hidden: document.getElementById("home-continue").hidden, label: button()?.textContent || null,
  title: document.querySelector("#home-continue .home-continue-title")?.childNodes[0]?.textContent || null,
  detail: document.querySelector("#home-continue .home-continue-detail")?.textContent || null});
const nextUp = () => document.querySelector("#today-plan .today-plan-next .planner-session");
const nextUpButton = () => nextUp()?.querySelector(".planner-session-actions button, button");
const nextUpState = () => ({continueHidden: document.getElementById("home-continue").hidden, label: nextUpButton()?.textContent || null,
  buttons: [...(nextUp()?.querySelectorAll("button") || [])].map((b) => b.textContent),
  title: nextUp()?.querySelector(".planner-session-body strong")?.textContent || null,
  activity: nextUp()?.querySelector(".planner-activity-chip")?.textContent || null,
  occurrences: [...document.querySelectorAll("#home-continue, #today-plan")].map((el) => el.textContent.split("Marketing").length - 1)
    .reduce((a, b) => a + b, 0)});
// Visual order of Home's blocks (top to bottom, then left to right), by their rendered position; only
// blocks actually rendered count (the library's document list lives on another page).
const homeOrder = () => [["continue", "#home-continue"], ["plan", "#today-plan"], ["upcoming", "#home-upcoming"],
  ["progress", "#home-progress"], ["materials", ".session-library"]]
  .map(([name, selector]) => [name, document.querySelector(selector)])
  .filter(([, el]) => el && el.getClientRects().length)
  .sort((a, b) => (a[1].getBoundingClientRect().top - b[1].getBoundingClientRect().top) || (a[1].getBoundingClientRect().left - b[1].getBoundingClientRect().left))
  .map(([name]) => name);
const opened = () => ({page: document.body.dataset.page, tab: document.body.dataset.sessionTab, document: activeDocumentId});
const home = async () => { setPage("overview"); await loadDashboard(); await sleep(400); await loadTodayPlan(); await sleep(200); };
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);   // Thu 24 Sep 2026, 12:00 browser-local
  const flash = {session_id: "s-flash", plan_id: "plan-1", document_id: "mkt.pdf", document_title: "Marketing", activity_type: "flashcards",
    scheduled_start: "2026-09-24T14:00:00", scheduled_end: "2026-09-24T14:30:00", duration_minutes: 30, status: "scheduled",
    reason: {code: "new_material", message: "New material to learn"}, artifact_id: null};
  P.plans = [{plan_id: "plan-1", title: "Plan", status: "active"}];
  P.sessionsByPlan = {"plan-1": [flash]};
  H.progress = {"mkt.pdf": learning("mkt.pdf", flash), "stats.pdf": learning("stats.pdf", null)};

  // 1) a scheduled session: shown once (Next up), Start through the Planner lifecycle, once, on the tool it names
  await home();
  out.scheduled = {...nextUpState(), order: homeOrder()};
  const startButton = nextUpButton();
  startButton.click(); startButton.click();    // a double click acts once (shared busy guard)
  await sleep(1200);
  out.afterStart = {startCalls: [...P.startCalls], opened: opened(),
    todayRow: homePlanSessions.find((s) => s.session_id === "s-flash")?.status, generateCalls: window.__calls.filter((c) => /generate/.test(c))};

  // 2) back on Home the same session is in progress: Resume, still one start per click
  P.startCalls.length = 0;
  H.progress["mkt.pdf"] = learning("mkt.pdf", {...flash, status: "in_progress"});
  P.sessionsByPlan = {"plan-1": [{...flash, status: "in_progress"}]};
  await home();
  out.inProgress = nextUpState();
  [...nextUp().querySelectorAll("button")].find((b) => b.textContent === "Resume").click(); await sleep(1200);
  out.afterResume = {startCalls: [...P.startCalls], opened: opened()};

  // 3) no planned session anywhere: the document opens, nothing touches the Planner
  P.startCalls.length = 0;
  P.sessionsByPlan = {"plan-1": []};
  H.progress = {"mkt.pdf": learning("mkt.pdf", null), "stats.pdf": learning("stats.pdf", null)};
  await home();
  out.noSession = {...state(), order: homeOrder(), planIdle: document.getElementById("today-plan").classList.contains("today-plan--idle"),
    planDate: document.querySelector("#today-plan .today-plan-heading .eyebrow")?.textContent, browserLanguage: navigator.language};
  const before = P.calls.length;
  button().click(); await sleep(800);
  out.afterOpen = {startCalls: [...P.startCalls], opened: opened(),
    plannerWrites: P.calls.slice(before).filter((c) => !c.startsWith("GET"))};
  out.calls = P.calls;
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


class ContinueLearningAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_a_scheduled_session_is_shown_once_and_starts_through_the_planner_lifecycle_once(self):
        scheduled = self.out["scheduled"]
        self.assertTrue(scheduled["continueHidden"])   # the Continue card does not repeat the planned session
        self.assertEqual((scheduled["label"], scheduled["title"], scheduled["activity"]), ("Start", "Marketing", "Flashcards"))
        self.assertEqual(scheduled["occurrences"], 1)
        self.assertEqual(scheduled["order"], ["plan", "progress"])   # Next up leads Home; no document list
        after = self.out["afterStart"]
        self.assertEqual(after["startCalls"], ["s-flash"])                       # one request for a double click
        self.assertEqual(after["opened"], {"page": "session", "tab": "flashcards", "document": "mkt.pdf"})
        self.assertEqual(after["todayRow"], "in_progress")   # the Today list's full row was the one started
        self.assertEqual(after["generateCalls"], [])                             # nothing generated on the way

    def test_an_in_progress_session_reads_resume_and_resumes(self):
        self.assertTrue(self.out["inProgress"]["continueHidden"])
        self.assertEqual(self.out["inProgress"]["buttons"], ["Complete", "Resume"])
        self.assertEqual(self.out["inProgress"]["occurrences"], 1)
        self.assertEqual(self.out["afterResume"]["startCalls"], ["s-flash"])
        self.assertEqual(self.out["afterResume"]["opened"]["tab"], "flashcards")

    def test_without_a_planned_session_only_the_document_opens(self):
        self.assertFalse(self.out["noSession"]["hidden"])   # nothing planned: the Continue card fills the gap
        self.assertEqual(self.out["noSession"]["label"], "Start studying")
        # Nothing planned: Continue learning leads; the plan prompt is compact; no document list on Home.
        self.assertEqual(self.out["noSession"]["order"], ["continue", "plan", "progress"])
        self.assertTrue(self.out["noSession"]["planIdle"])

    def test_dates_are_english_whatever_the_browser_language(self):
        # Thu 24 Sep 2026 (plannerNow): English like the rest of the UI, not the browser's language.
        self.assertEqual(self.out["noSession"]["planDate"], "Thursday, Sep 24")
        after = self.out["afterOpen"]
        self.assertEqual(after["startCalls"], [])
        self.assertEqual(after["plannerWrites"], [])
        self.assertEqual(after["opened"], {"page": "session", "tab": "overview", "document": "mkt.pdf"})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ContinueLearningDesktopTests(ContinueLearningAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ContinueLearningPhoneTests(ContinueLearningAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)


if __name__ == "__main__":
    unittest.main()
