"""Real-browser tests: the desktop calendar's session popover shows one primary action per state and
keeps terminal/history actions secondary (headless Chrome; the lifecycle mock from
tests/test_study_planner_today_ui.py). "Now" is pinned to Thu 2026-09-24 12:00.

scheduled -> Start (Reschedule, Skip); in progress -> Resume (Complete session, Skip); missed ->
Reschedule (Skip); completed / skipped -> read-only history. Each click fires its lifecycle call
exactly once. Runs at 1280px and 1100px. Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width
from tests.test_study_planner_today_ui import MOCK

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: [], width: window.innerWidth, overflow: {}};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const P = window.__planner;
const titles = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"};
const session = (id, doc, activity, start, end, minutes, status = "scheduled", extra = {}) => ({session_id: id, document_id: doc,
  document_title: titles[doc], activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
  status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, rescheduled_from: null, ...extra});
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("planner-workspace").getBoundingClientRect().bottom <= window.innerHeight + 1
  && (!$("pcal-popover").hidden ? $("pcal-popover").getBoundingClientRect().right <= window.innerWidth + 1 : true);
// Open a session's popover and read it the way a learner sees it.
const open = (id) => {
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  document.querySelector(`.pcal-event[data-session-id="${id}"]`).click();
  const pop = $("pcal-popover");
  return {
    open: !pop.hidden, pill: pop.querySelector(".pcal-kind")?.textContent || null,
    title: pop.querySelector(".pcal-popover-title")?.textContent || null,
    activity: pop.querySelector(".pcal-popover-activity")?.textContent || null,
    when: pop.querySelector(".pcal-popover-when")?.textContent || null,
    reason: pop.querySelector(".pcal-popover-reason")?.textContent || null,
    notes: [...pop.querySelectorAll(".pcal-popover-note")].map((n) => n.textContent),
    primary: [...pop.querySelectorAll(".pcal-session-actions .primary-button")].map((b) => b.textContent),
    secondary: [...pop.querySelectorAll(".pcal-session-actions .pcal-text-action")].map((b) => b.textContent),
    focused: document.activeElement?.textContent || null,
    primaryWeight: pop.querySelector(".pcal-session-actions .primary-button") ? getComputedStyle(pop.querySelector(".pcal-session-actions .primary-button")).backgroundColor : null,
    secondaryBackground: pop.querySelector(".pcal-text-action") ? getComputedStyle(pop.querySelector(".pcal-text-action")).backgroundColor : null,
    text: pop.textContent,
  };
};
const press = (label) => [...$("pcal-popover").querySelectorAll(".pcal-session-actions button")].find((b) => b.textContent === label);
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  P.sessionsByPlan = {"plan-1": [
    session("s-ahead", "mkt.pdf", "summary", "2026-09-25T18:00:00", "2026-09-25T18:45:00", 45),
    session("s-start", "stats.pdf", "flashcards", "2026-09-26T10:00:00", "2026-09-26T10:20:00", 20),
    session("s-running", "stats.pdf", "quiz", "2026-09-24T11:30:00", "2026-09-24T12:15:00", 45, "in_progress"),
    session("s-missed", "pbi.pdf", "review", "2026-09-24T09:00:00", "2026-09-24T09:15:00", 15),
    session("s-done", "mkt.pdf", "flashcards", "2026-09-22T18:00:00", "2026-09-22T18:20:00", 20, "completed",
            {completed_at: "2026-09-22T18:25:00", started_at: "2026-09-22T18:01:00"}),
    session("s-skipped", "pbi.pdf", "summary", "2026-09-23T18:00:00", "2026-09-23T18:40:00", 40, "skipped"),
    session("s-orig", "stats.pdf", "summary", "2026-09-23T19:00:00", "2026-09-23T19:45:00", 45, "rescheduled"),
    session("s-moved", "stats.pdf", "summary", "2026-09-26T11:00:00", "2026-09-26T11:45:00", 45, "scheduled", {rescheduled_from: "s-orig"}),
    session("s-cancelled", "mkt.pdf", "review", "2026-09-23T20:00:00", "2026-09-23T20:15:00", 15, "cancelled"),
  ]};
  setPage("planner"); await sleep(600);
  $("pcal-scroll").scrollTop = 8 * 48;
  out.onCalendar = [...document.querySelectorAll(".pcal-event")].map((e) => e.dataset.sessionId).sort();

  out.scheduled = open("s-ahead");
  out.running = open("s-running");
  out.missed = open("s-missed");
  out.completed = open("s-done");
  out.skipped = open("s-skipped");
  out.moved = open("s-moved");
  out.overflow.popover = noOverflow();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));

  // Each action fires its existing lifecycle call exactly once (double clicks included).
  P.actionCalls = []; P.startCalls = [];
  open("s-running");
  const complete = press("Complete session"); complete.click(); complete.click();
  await sleep(1200);
  open("s-ahead");
  const skip = press("Skip"); skip.click(); skip.click();
  await sleep(1200);
  open("s-missed");
  const reschedule = press("Reschedule"); reschedule.click(); reschedule.click();
  await sleep(1500);
  out.calls = P.actionCalls.map((c) => [c[0], c[1]]);
  out.rescheduleBody = P.actionCalls.find((c) => c[0] === "reschedule")?.[2] || null;
  out.expectedOffset = -new Date().getTimezoneOffset();
  out.overflow.afterActions = noOverflow();
  // Start opens the study tool (last: it leaves the planner).
  open("s-start");
  const start = press("Start"); start.click(); start.click();
  await sleep(1500);
  out.started = {calls: [...P.startCalls], page: document.body.dataset.page, tab: document.body.dataset.sessionTab};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class PopoverAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_only_live_and_history_sessions_are_on_the_calendar(self):
        # rescheduled originals and cancelled sessions are not calendar events (nothing to act on)
        self.assertEqual(self.out["onCalendar"], ["s-ahead", "s-done", "s-missed", "s-moved", "s-running", "s-skipped", "s-start"])

    def test_scheduled_session_start_is_primary_and_complete_is_absent(self):
        scheduled = self.out["scheduled"]
        self.assertEqual((scheduled["pill"], scheduled["primary"], scheduled["secondary"]), ("Planned", ["Start"], ["Reschedule", "Skip"]))
        self.assertEqual((scheduled["title"], scheduled["activity"]), ("Marketing", "Summary"))
        self.assertRegex(scheduled["when"], r"25.* · 18:00–18:45 · 45m$")
        self.assertEqual(scheduled["focused"], "Start")   # keyboard lands on the primary action
        self.assertNotIn("Complete", scheduled["text"])

    def test_in_progress_resume_is_primary_and_complete_is_secondary(self):
        running = self.out["running"]
        self.assertEqual((running["pill"], running["primary"], running["secondary"]),
                         ("In progress", ["Resume"], ["Complete session", "Skip"]))
        self.assertNotEqual(running["primaryWeight"], running["secondaryBackground"])   # filled button vs quiet text action
        self.assertEqual(running["secondaryBackground"], "rgba(0, 0, 0, 0)")

    def test_missed_session_reschedule_is_primary(self):
        missed = self.out["missed"]
        self.assertEqual((missed["pill"], missed["primary"], missed["secondary"]), ("Not completed", ["Reschedule"], ["Skip"]))
        self.assertNotRegex(missed["text"], r"Start|Resume|Complete")

    def test_history_is_read_only(self):
        completed = self.out["completed"]
        self.assertEqual((completed["pill"], completed["primary"], completed["secondary"]), ("Completed", [], []))
        self.assertEqual(len(completed["notes"]), 1)
        self.assertRegex(completed["notes"][0], r"^Completed .*22")
        skipped = self.out["skipped"]
        self.assertEqual((skipped["pill"], skipped["primary"], skipped["secondary"]), ("Skipped", [], []))
        for state in (completed, skipped):
            self.assertNotRegex(state["text"], r"Start|Resume|Reschedule|Complete session")

    def test_moved_session_shows_where_it_came_from(self):
        moved = self.out["moved"]
        self.assertEqual(moved["primary"], ["Start"])
        self.assertEqual(len(moved["notes"]), 1)
        self.assertRegex(moved["notes"][0], r"^Moved from .*23.* · 19:00$")

    def test_reason_is_shown_without_internal_codes(self):
        for state in ("scheduled", "running", "missed", "completed", "skipped", "moved"):
            popover = self.out[state]
            self.assertEqual(popover["reason"], "Why this session? Build understanding of new material.")
            self.assertNotRegex(popover["text"], r"new_material|priority|score")

    def test_each_action_fires_once(self):
        self.assertEqual(self.out["calls"], [["complete", "s-running"], ["skip", "s-ahead"], ["reschedule", "s-missed"]])
        self.assertEqual(self.out["rescheduleBody"], {"utc_offset_minutes": self.out["expectedOffset"]})
        self.assertEqual(self.out["started"], {"calls": ["s-start"], "page": "session", "tab": "flashcards"})

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PopoverDesktopTests(PopoverAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PopoverNarrowDesktopTests(PopoverAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
