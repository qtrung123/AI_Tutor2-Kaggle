"""Real-browser tests for the read-only Study Planner daily experience (headless Chrome, mocked
v2 API): Home "Today's Study Plan" (Next up / Later today / next upcoming / empty) and the
confirmed plan's week view with previous/next navigation. "Now" is pinned to Thu 2026-09-24 12:00
in the browser's local time. Runs at desktop (1280px) and phone (390px). Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_v2_ui import MOCK as PLANNER_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

# Sessions are served per plan so an archived plan's leftovers can be told apart.
MOCK = PLANNER_MOCK + r"""
window.__planner.sessionsByPlan = {};
const plannerV2Fetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const m = p.match(/^\/api\/planner\/plans\/([^/]+)\/sessions$/);
  if (m) { window.__planner.calls.push("GET " + p); return json(window.__planner.sessionsByPlan[m[1]] || []); }
  return plannerV2Fetch(input, init);
};
"""

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
const P = window.__planner;
const titles = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"};
let seq = 0;
const session = (doc, activity, start, end, minutes, status = "scheduled") => ({session_id: "s" + (seq++), document_id: doc,
  document_title: titles[doc], activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
  status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null});
const panel = () => document.getElementById("today-plan");
const card = (el) => el && ({time: el.querySelector(".planner-session-time").textContent, title: el.querySelector("strong").textContent,
  activity: el.querySelector(".planner-activity-chip").textContent, duration: el.querySelector(".planner-session-meta span:last-child").textContent,
  reason: el.querySelector(".planner-session-reason").textContent});
const today = () => ({
  hidden: panel().hidden,
  title: panel().querySelector("h2").textContent,
  sections: [...panel().querySelectorAll(".today-plan-section h3")].map((h) => h.textContent),
  next: card(panel().querySelector(".today-plan-next .planner-session")),
  later: [...panel().querySelectorAll(".today-plan-later .planner-session")].map(card),
  upcoming: card(panel().querySelector(".today-plan-upcoming .planner-session")),
  empty: panel().querySelector(".today-plan-empty")?.textContent || null,
  buttons: [...panel().querySelectorAll("button")].map((b) => b.textContent),
});
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1;
const view = () => document.getElementById("planner-view");
const visibleStep = () => [...view().querySelectorAll("[data-planner-step]")].find((s) => !s.hidden)?.dataset.plannerStep;
const week = () => ({
  step: visibleStep(),
  label: document.getElementById("planner-plan-week-label").textContent,
  days: [...document.querySelectorAll("#planner-plan-sessions .planner-day h4")].map((h) => h.childNodes[0].textContent),
  sessions: [...document.querySelectorAll("#planner-plan-sessions .planner-session")].map((li) => li.querySelector("strong").textContent),
  empty: document.querySelector("#planner-plan-sessions .empty-state")?.textContent || null,
  editable: view().querySelectorAll('[data-planner-step="plan"] input, [data-planner-step="plan"] .planner-session button').length,
});
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);   // Thu 24 Sep 2026, 12:00 browser-local
  setPage("overview"); await sleep(300);

  // An archived plan still holding a scheduled session today, plus the active plan.
  P.plans = [{plan_id: "plan-old", title: "Old", status: "archived"}, {plan_id: "plan-1", title: "Now", status: "active"}];
  P.sessionsByPlan = {
    "plan-old": [session("pbi.pdf", "summary", "2026-09-24T12:30:00", "2026-09-24T13:00:00", 30)],
    "plan-1": [
      session("stats.pdf", "flashcards", "2026-09-24T20:00:00", "2026-09-24T20:20:00", 20),
      session("mkt.pdf", "summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45),        // already over
      session("mkt.pdf", "quiz", "2026-09-24T14:00:00", "2026-09-24T14:30:00", 30),
      session("stats.pdf", "summary", "2026-09-24T11:30:00", "2026-09-24T12:15:00", 45, "in_progress"),
      session("mkt.pdf", "review", "2026-09-24T16:00:00", "2026-09-24T16:15:00", 15, "skipped"),
      session("stats.pdf", "quiz", "2026-09-28T18:00:00", "2026-09-28T18:30:00", 30),
      session("mkt.pdf", "flashcards", "2026-09-29T19:00:00", "2026-09-29T19:20:00", 20),
    ],
  };
  await loadTodayPlan(); await sleep(100);
  out.multiple = today();
  out.overflow.homeMultiple = noOverflow();

  // Nothing (left) today -> the next upcoming session with its date.
  P.sessionsByPlan["plan-1"] = P.sessionsByPlan["plan-1"].filter((s) => !s.scheduled_start.startsWith("2026-09-24") || s.scheduled_end < "2026-09-24T12:00:00");
  await loadTodayPlan(); await sleep(100);
  out.upcoming = today();

  // No future sessions at all (the archived plan's leftovers do not count).
  P.sessionsByPlan["plan-1"] = [session("mkt.pdf", "summary", "2026-09-22T18:00:00", "2026-09-22T18:45:00", 45)];
  P.sessionsByPlan["plan-old"].push(session("pbi.pdf", "quiz", "2026-09-30T18:00:00", "2026-09-30T18:30:00", 30));
  await loadTodayPlan(); await sleep(100);
  out.none = today();
  out.overflow.homeEmpty = noOverflow();

  // View full schedule -> Planner's week view of the confirmed (active) plan.
  P.sessionsByPlan["plan-1"] = [
    session("mkt.pdf", "summary", "2026-09-25T18:00:00", "2026-09-25T18:45:00", 45),
    session("stats.pdf", "quiz", "2026-09-28T18:00:00", "2026-09-28T18:30:00", 30),
    session("mkt.pdf", "flashcards", "2026-09-29T19:00:00", "2026-09-29T19:20:00", 20),
    session("mkt.pdf", "quiz", "2026-10-12T19:00:00", "2026-10-12T19:30:00", 30),
  ];
  [...panel().querySelectorAll("button")].find((b) => b.textContent === "View full schedule").click(); await sleep(400);
  out.page = document.body.dataset.page;
  out.weekThis = week();
  out.overflow.plannerWeek = noOverflow();
  document.getElementById("planner-plan-next-week").click(); await sleep(100);
  out.weekNext = week();
  document.getElementById("planner-plan-next-week").click(); await sleep(100);
  out.weekEmpty = week();
  out.overflow.plannerEmptyWeek = noOverflow();
  document.getElementById("planner-plan-prev-week").click(); document.getElementById("planner-plan-prev-week").click(); await sleep(100);
  out.weekBack = week();
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


class TodayAndWeekAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_today_with_multiple_sessions(self):
        multiple = self.out["multiple"]
        self.assertFalse(multiple["hidden"])
        self.assertEqual(multiple["title"], "Today’s Study Plan")
        self.assertEqual(multiple["sections"], ["Next up", "Later today"])
        # Earliest remaining: the in-progress session (the 09:00 one is over, the skipped one is not work).
        self.assertEqual(multiple["next"], {"time": "11:30–12:15", "title": "Statistics", "activity": "Summary",
                                            "duration": "45m", "reason": "New material to learn"})
        self.assertEqual([(s["time"], s["title"], s["activity"]) for s in multiple["later"]],
                         [("14:00–14:30", "Marketing", "Quiz"), ("20:00–20:20", "Statistics", "Flashcards")])
        self.assertIsNone(multiple["upcoming"])

    def test_archived_plan_sessions_are_not_current_work(self):
        self.assertNotIn("PowerBI", [s["title"] for s in self.out["multiple"]["later"]])
        self.assertNotEqual(self.out["multiple"]["next"]["title"], "PowerBI")
        self.assertNotIn("GET /api/planner/plans/plan-old/sessions", self.out["calls"])

    def test_no_session_today_shows_next_upcoming_with_its_date(self):
        upcoming = self.out["upcoming"]
        self.assertIsNone(upcoming["next"])
        self.assertEqual(upcoming["empty"], "Nothing left for today.")
        self.assertEqual(len(upcoming["sections"]), 1)
        self.assertRegex(upcoming["sections"][0], r"^Next session · .*28")
        self.assertEqual((upcoming["upcoming"]["time"], upcoming["upcoming"]["title"], upcoming["upcoming"]["activity"]),
                         ("18:00–18:30", "Statistics", "Quiz"))

    def test_no_future_sessions_shows_empty_state(self):
        none = self.out["none"]
        self.assertEqual(none["sections"], [])
        self.assertEqual(none["empty"], "No upcoming study sessions. Create a plan to see what to study next.")

    def test_read_only_with_only_a_view_schedule_action(self):
        for key in ("multiple", "upcoming", "none"):
            self.assertEqual(self.out[key]["buttons"], ["View full schedule"])
        self.assertEqual(self.out["weekThis"]["editable"], 0)

    def test_week_view_and_navigation(self):
        self.assertEqual(self.out["page"], "planner")
        this_week = self.out["weekThis"]
        self.assertEqual(this_week["step"], "plan")
        self.assertRegex(this_week["label"], r"21.*27")
        self.assertEqual((len(this_week["days"]), this_week["sessions"]), (1, ["Marketing"]))
        next_week = self.out["weekNext"]
        self.assertRegex(next_week["label"], r"28.*4")
        self.assertEqual((len(next_week["days"]), next_week["sessions"]), (2, ["Statistics", "Marketing"]))
        empty = self.out["weekEmpty"]
        self.assertEqual(empty["sessions"], [])
        self.assertRegex(empty["empty"], r"^No study sessions this week\. Next session: .*12")
        self.assertEqual(self.out["weekBack"], this_week)

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TodayAndWeekDesktopTests(TodayAndWeekAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TodayAndWeekPhoneTests(TodayAndWeekAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_runs_at_phone_width(self):
        self.assertEqual(self.out["width"], 390)


if __name__ == "__main__":
    unittest.main()
