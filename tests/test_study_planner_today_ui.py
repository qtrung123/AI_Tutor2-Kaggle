"""Real-browser tests for the read-only Study Planner daily experience (headless Chrome, mocked
v2 API): Home "Today's Study Plan" (Next up / Later today / next upcoming / empty), the
confirmed plan's week (desktop: the calendar workspace; phone: the week list) with previous/next navigation, Start/Resume (Phase 4B): one start
request per click burst, then the session's document opens on the mapped tool, and Complete /
"Session not completed" -> Reschedule / Skip (Phase 4C) with Home + week refreshed afterwards. "Now" is pinned to Thu 2026-09-24 12:00
in the browser's local time. Runs at desktop (1280px) and phone (390px). Skipped without Chrome.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_v2_ui import MOCK as PLANNER_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

# Sessions are served per plan so an archived plan's leftovers can be told apart. Start mirrors
# the backend: scheduled -> in_progress (resume is a no-op), terminal -> 409, tool per activity.
MOCK = PLANNER_MOCK + r"""
window.__planner.sessionsByPlan = {};
window.__planner.startCalls = [];
window.__planner.actionCalls = [];
const plannerV2Fetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const m = p.match(/^\/api\/planner\/plans\/([^/]+)\/sessions$/);
  if (m) { P.calls.push("GET " + p); return json(P.sessionsByPlan[m[1]] || []); }
  const start = p.match(/^\/api\/planner\/sessions\/([^/]+)\/start$/);
  if (start && (init.method || "GET").toUpperCase() === "POST") {
    P.startCalls.push(start[1]);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const session = Object.values(P.sessionsByPlan).flat().find((s) => s.session_id === start[1]);
    if (!session) return json({detail: "Study session not found."}, 404);
    if (!["scheduled", "in_progress"].includes(session.status)) {
      return json({detail: {code: "session_not_startable", message: "A " + session.status + " session cannot be started.", status: session.status}}, 409);
    }
    const started = session.status === "scheduled";
    session.status = "in_progress";
    const tool = {summary: "summary", flashcards: "flashcards", quiz: "quiz", quiz_retry: "quiz", review: "flashcards"}[session.activity_type];
    return json({session: {...session}, started, tool, artifact_available: false});
  }
  const action = p.match(/^\/api\/planner\/sessions\/([^/]+)\/(complete|skip|reschedule)$/);
  if (action) {
    const body = init.body ? JSON.parse(init.body) : null;
    P.actionCalls.push([action[2], action[1], body]);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const lists = Object.values(P.sessionsByPlan);
    const session = lists.flat().find((s) => s.session_id === action[1]);
    if (!session) return json({detail: "Study session not found."}, 404);
    const conflict = (code, message) => json({detail: {code, message, status: session.status}}, 409);
    if (action[2] === "complete") {
      if (session.status === "completed") return json({session: {...session}, changed: false});
      if (session.status !== "in_progress") return conflict("session_not_started", "Start this session before completing it.");
      Object.assign(session, {status: "completed", completed_at: "2026-09-24T12:00:00+00:00"});
      return json({session: {...session}, changed: true});
    }
    if (action[2] === "skip") {
      if (!["scheduled", "in_progress", "skipped"].includes(session.status)) return conflict("session_not_skippable", "Cannot skip.");
      const changed = session.status !== "skipped";
      session.status = "skipped";
      return json({session: {...session}, changed});
    }
    if (P.noSlot) return conflict("no_available_slot", "No free study time is left before the deadline. Add availability, then try again.");
    session.status = "rescheduled";
    const moved = {...session, session_id: session.session_id + "-moved", status: "scheduled", rescheduled_from: session.session_id,
      scheduled_start: "2026-09-25T18:00:00", scheduled_end: "2026-09-25T18:" + String(session.duration_minutes).padStart(2, "0") + ":00"};
    lists.find((list) => list.includes(session)).push(moved);
    return json({session: moved, previous: {...session}, after_deadline: false});
  }
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
// Desktop (>=1024px) shows the plan on the calendar workspace; phone keeps the step flow's week list.
const desktop = window.innerWidth >= 1024;
const calendarEvents = () => [...document.querySelectorAll("#pcal-body .pcal-event")]
  .sort((a, b) => a.dataset.start.localeCompare(b.dataset.start));
const week = () => desktop ? ({
  label: document.getElementById("pcal-range").textContent,
  days: [...new Set(calendarEvents().map((e) => e.closest(".pcal-col").dataset.date))],
  sessions: calendarEvents().map((e) => e.querySelector(".pcal-event-title").textContent),
  kinds: calendarEvents().map((e) => e.dataset.kind),
  status: document.getElementById("pcal-status").textContent,
  queue: [...document.querySelectorAll("#pcal-queue .planner-session")].map((li) => li.querySelector("strong").textContent
    + " " + li.querySelector(".planner-session-time").textContent),
  wizardHidden: document.querySelector(".planner-shell").hidden,
}) : ({
  step: visibleStep(),
  label: document.getElementById("planner-plan-week-label").textContent,
  days: [...document.querySelectorAll("#planner-plan-sessions .planner-day h4")].map((h) => h.childNodes[0].textContent),
  sessions: [...document.querySelectorAll("#planner-plan-sessions .planner-session")].map((li) => li.querySelector("strong").textContent),
  empty: document.querySelector("#planner-plan-sessions .empty-state")?.textContent || null,
  inputs: view().querySelectorAll('[data-planner-step="plan"] input').length,
  actions: [...document.querySelectorAll("#planner-plan-sessions .planner-session button")].map((b) => b.textContent),
});
const opened = () => ({page: document.body.dataset.page, tab: document.body.dataset.sessionTab, document: activeDocumentId,
  summaryGenerateShown: !document.getElementById("summary-generate").hidden,
  flashcardsGenerateShown: !document.getElementById("flashcards-generate").hidden});
const startButton = (title, activity) => [...document.querySelectorAll(".planner-session")]
  .find((el) => el.offsetParent !== null && el.querySelector("strong").textContent === title
    && el.querySelector(".planner-activity-chip").textContent === activity)?.querySelector(".planner-session-start");
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
  P.sessionsByPlan["plan-1"] = [session("mkt.pdf", "summary", "2026-09-22T18:00:00", "2026-09-22T18:45:00", 45, "completed")];
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
  const nextWeek = () => document.getElementById(desktop ? "pcal-next" : "planner-plan-next-week").click();
  const prevWeek = () => document.getElementById(desktop ? "pcal-prev" : "planner-plan-prev-week").click();
  nextWeek(); await sleep(100);
  out.weekNext = week();
  nextWeek(); await sleep(100);
  out.weekEmpty = week();
  out.overflow.plannerEmptyWeek = noOverflow();
  prevWeek(); prevWeek(); await sleep(100);
  out.weekBack = week();

  // Phase 4B: Start from the week view (quiz_retry -> Quiz tab).
  const retry = session("stats.pdf", "quiz_retry", "2026-09-25T20:00:00", "2026-09-25T20:30:00", 30);
  P.sessionsByPlan["plan-1"].push(retry);
  await loadPlannerData(); await sleep(200);
  startButton("Statistics", "Quiz retry").click(); await sleep(1500);
  out.weekStart = {...opened(), calls: [...P.startCalls]};
  out.overflow.sessionAfterWeekStart = noOverflow();

  // Start from Home: double click -> ONE request, then the Summary tool (missing -> generate state).
  const summary = session("mkt.pdf", "summary", "2026-09-24T13:00:00", "2026-09-24T13:45:00", 45);
  const review = session("stats.pdf", "review", "2026-09-24T15:00:00", "2026-09-24T15:15:00", 15);
  const done = session("pbi.pdf", "quiz", "2026-09-24T17:00:00", "2026-09-24T17:30:00", 30);
  P.sessionsByPlan["plan-1"] = [summary, review, done];
  setPage("overview"); await sleep(400);
  out.homeButtons = today().buttons;
  P.startCalls = [];
  const homeStart = startButton("Marketing", "Summary");
  homeStart.click(); homeStart.click(); await sleep(50);
  out.starting = {disabled: homeStart.disabled, text: homeStart.textContent};
  homeStart.click(); await sleep(1500);
  out.homeStart = {...opened(), calls: [...P.startCalls]};

  // Back home: the started session now offers Resume; resuming opens it again (no new state).
  setPage("overview"); await sleep(400);
  out.resumeLabel = startButton("Marketing", "Summary").textContent;
  startButton("Marketing", "Summary").click(); await sleep(1500);
  out.resume = {...opened(), calls: [...P.startCalls], status: summary.status};

  // review -> the tool the backend picked (Flashcards here, with its create state).
  setPage("overview"); await sleep(400);
  startButton("Statistics", "Review").click(); await sleep(1500);
  out.review = opened();

  // A session that turned terminal meanwhile: 409 -> stay on Home, list refreshed, no crash.
  setPage("overview"); await sleep(400);
  const doneButton = startButton("PowerBI", "Quiz");
  done.status = "completed";
  doneButton.click(); await sleep(1200);
  out.rejected = {page: document.body.dataset.page, buttons: today().buttons, status: done.status};
  out.overflow.homeWithActions = noOverflow();

  // Phase 4C: overdue scheduled -> "Session not completed" + Reschedule / Skip; in_progress -> Complete.
  const item = (title, activity) => [...document.querySelectorAll(".planner-session")]
    .find((el) => el.offsetParent !== null && el.querySelector("strong").textContent === title
      && el.querySelector(".planner-activity-chip").textContent === activity);
  const describe = (el) => el && ({note: el.querySelector(".planner-session-note")?.textContent || null,
    status: el.querySelector(".planner-session-status")?.textContent || null,
    buttons: [...el.querySelectorAll("button")].map((b) => b.textContent)});
  const press = (title, activity, label) => [...item(title, activity).querySelectorAll("button")].find((b) => b.textContent === label);
  const overdue = session("mkt.pdf", "summary", "2026-09-24T09:00:00", "2026-09-24T09:30:00", 30);
  const running = session("stats.pdf", "quiz", "2026-09-24T11:30:00", "2026-09-24T12:15:00", 45, "in_progress");
  const later = session("pbi.pdf", "flashcards", "2026-09-24T15:00:00", "2026-09-24T15:20:00", 20);
  const lastWeek = session("stats.pdf", "review", "2026-09-22T19:00:00", "2026-09-22T19:15:00", 15);
  P.sessionsByPlan["plan-1"] = [overdue, running, later, lastWeek];
  // The border between "still ahead" and "not completed" is the browser-local clock.
  plannerNow = () => new Date(2026, 8, 24, 9, 29, 0);
  out.overdueBoundary = {before: plannerIsOverdue(overdue), at: (plannerNow = () => new Date(2026, 8, 24, 9, 30, 0), plannerIsOverdue(overdue))};
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  setPage("overview"); await sleep(400);
  out.lifecycleHome = {sections: today().sections, overdue: describe(item("Marketing", "Summary")),
    running: describe(item("Statistics", "Quiz")), later: describe(item("PowerBI", "Flashcards")),
    lastWeek: describe(item("Statistics", "Review"))};
  out.overflow.homeLifecycle = noOverflow();

  // Week view shows the same states.
  setPage("planner"); await sleep(500);
  out.lifecycleWeek = {overdue: describe(item("Marketing", "Summary")), running: describe(item("Statistics", "Quiz")),
    later: describe(item("PowerBI", "Flashcards"))};
  out.overflow.weekLifecycle = noOverflow();

  // Complete (double click -> one request), from Home.
  setPage("overview"); await sleep(400);
  P.actionCalls = [];
  const complete = press("Statistics", "Quiz", "Complete");
  complete.click(); complete.click(); await sleep(50);
  out.completing = {text: complete.textContent, disabled: [...complete.parentNode.querySelectorAll("button")].map((b) => b.disabled)};
  await sleep(1200);
  out.completed = {calls: P.actionCalls.map((c) => c[0]), toast: toast.textContent, status: running.status,
    stillListed: !!item("Statistics", "Quiz")};

  // Reschedule the overdue session: the learner's offset is sent; Home refreshes.
  press("Marketing", "Summary", "Reschedule").click(); await sleep(1300);
  out.rescheduled = {call: P.actionCalls[P.actionCalls.length - 1], toast: toast.textContent, oldStatus: overdue.status,
    sections: today().sections, overdueListed: !!item("Marketing", "Summary")};

  // No slot -> clear message, nothing changes; then Skip it.
  P.noSlot = true;
  press("Statistics", "Review", "Reschedule").click(); await sleep(1300);
  out.noSlot = {toast: toast.textContent, status: lastWeek.status, listed: !!item("Statistics", "Review")};
  press("Statistics", "Review", "Skip").click(); await sleep(1300);
  out.skipped = {toast: toast.textContent, status: lastWeek.status, sections: today().sections};

  // The week keeps history (Completed / Skipped) and shows the moved session as current work.
  setPage("planner"); await sleep(500);
  out.weekAfter = desktop
    ? {calendar: calendarEvents().map((e) => [e.querySelector(".pcal-event-title").textContent, e.querySelector(".pcal-event-meta").textContent, e.dataset.kind]),
       moved: describe(item("Marketing", "Summary")), queue: week().queue}
    : {running: describe(item("Statistics", "Quiz")), skipped: describe(item("Statistics", "Review")),
       moved: describe(item("Marketing", "Summary")), days: week().days.length};
  out.overflow.weekHistory = noOverflow();
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
        self.assertEqual(multiple["sections"], ["Next up", "Later today", "Not completed"])
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
        self.assertEqual(len(upcoming["sections"]), 2)
        self.assertRegex(upcoming["sections"][0], r"^Next session · .*28")
        self.assertEqual(upcoming["sections"][1], "Not completed")   # this morning's 09:00 session
        self.assertEqual((upcoming["upcoming"]["time"], upcoming["upcoming"]["title"], upcoming["upcoming"]["activity"]),
                         ("18:00–18:30", "Statistics", "Quiz"))

    def test_no_future_sessions_shows_empty_state(self):
        none = self.out["none"]
        self.assertEqual(none["sections"], [])
        self.assertEqual(none["empty"], "No upcoming study sessions. Create a plan to see what to study next.")

    def test_actions_are_start_or_resume_only(self):
        self.assertEqual(self.out["multiple"]["buttons"],
                         ["View full schedule", "Complete", "Resume", "Start", "Start", "Reschedule", "Skip"])
        self.assertEqual(self.out["upcoming"]["buttons"], ["View full schedule", "Start", "Reschedule", "Skip"])
        self.assertEqual(self.out["none"]["buttons"], ["View full schedule"])

    def test_start_from_week_opens_the_mapped_tool(self):
        week_start = self.out["weekStart"]
        self.assertEqual((week_start["page"], week_start["tab"], week_start["document"]), ("session", "quiz", "stats.pdf"))
        self.assertEqual(len(week_start["calls"]), 1)

    def test_start_from_home_is_double_click_safe_and_opens_summary(self):
        self.assertEqual(self.out["homeButtons"], ["View full schedule", "Start", "Start", "Start"])
        self.assertEqual(self.out["starting"], {"disabled": True, "text": "Opening…"})
        home = self.out["homeStart"]
        self.assertEqual((home["page"], home["tab"], home["document"]), ("session", "summary", "mkt.pdf"))
        self.assertEqual(len(home["calls"]), 1)
        self.assertTrue(home["summaryGenerateShown"])   # missing summary -> its generate state

    def test_resume_reopens_the_same_session(self):
        self.assertEqual(self.out["resumeLabel"], "Resume")
        resume = self.out["resume"]
        self.assertEqual((resume["page"], resume["tab"], resume["status"]), ("session", "summary", "in_progress"))
        self.assertEqual(len(resume["calls"]), 2)
        self.assertEqual(len(set(resume["calls"])), 1)

    def test_review_opens_the_resolved_tool_not_overview(self):
        review = self.out["review"]
        self.assertEqual((review["page"], review["tab"], review["document"]), ("session", "flashcards", "stats.pdf"))
        self.assertTrue(review["flashcardsGenerateShown"])

    def test_rejected_start_stays_home_and_refreshes(self):
        rejected = self.out["rejected"]
        self.assertEqual(rejected["page"], "overview")
        self.assertEqual(rejected["status"], "completed")
        self.assertEqual(rejected["buttons"], ["View full schedule", "Complete", "Resume", "Complete", "Resume"])

    # -- Phase 4C -------------------------------------------------------------------

    def test_overdue_detection_uses_the_browser_local_clock(self):
        self.assertEqual(self.out["overdueBoundary"], {"before": False, "at": True})

    def test_lifecycle_states_on_home_and_week(self):
        not_completed = {"note": "Session not completed", "status": None, "buttons": ["Reschedule", "Skip"]}
        running = {"note": None, "status": None, "buttons": ["Complete", "Resume"]}
        ahead = {"note": None, "status": None, "buttons": ["Start"]}
        home = self.out["lifecycleHome"]
        self.assertEqual(home["sections"], ["Next up", "Later today", "Not completed"])
        self.assertEqual((home["overdue"], home["running"], home["later"], home["lastWeek"]),
                         (not_completed, running, ahead, not_completed))
        week = self.out["lifecycleWeek"]
        self.assertEqual((week["overdue"], week["running"], week["later"]), (not_completed, running, ahead))
        for state in list(home.values()) + list(week.values()):
            if isinstance(state, dict):
                self.assertNotRegex(str(state).lower(), r"fail")

    def test_complete_is_double_click_safe_with_short_feedback(self):
        self.assertEqual(self.out["completing"], {"text": "Saving…", "disabled": [True, True]})
        completed = self.out["completed"]
        self.assertEqual(completed["calls"], ["complete"])
        self.assertEqual((completed["toast"], completed["status"]), ("Session completed. Nice work!", "completed"))
        self.assertFalse(completed["stillListed"])   # Home refreshed: done work leaves Today

    def test_reschedule_sends_the_offset_and_refreshes(self):
        rescheduled = self.out["rescheduled"]
        self.assertEqual(rescheduled["call"][0], "reschedule")
        self.assertEqual(set(rescheduled["call"][2]), {"utc_offset_minutes"})
        self.assertRegex(rescheduled["toast"], r"^Moved to .*25.*, 18:00$")
        self.assertEqual(rescheduled["oldStatus"], "rescheduled")
        self.assertFalse(rescheduled["overdueListed"])

    def test_no_slot_message_then_skip(self):
        self.assertEqual(self.out["noSlot"], {"toast": "No free study time is left before the deadline. Add availability, then try again.",
                                              "status": "scheduled", "listed": True})
        skipped = self.out["skipped"]
        self.assertEqual((skipped["toast"], skipped["status"]), ("Session skipped", "skipped"))
        self.assertNotIn("Not completed", skipped["sections"])

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


class PhoneWeekListAssertions:
    """The step flow's saved-plan week list (phone and tablet widths)."""

    def test_week_list_actions(self):
        self.assertEqual((self.out["weekThis"]["inputs"], self.out["weekThis"]["actions"]), (0, ["Start"]))

    def test_week_keeps_history_and_shows_moved_session(self):
        after = self.out["weekAfter"]
        self.assertEqual(after["running"], {"note": None, "status": "Completed", "buttons": []})
        self.assertEqual(after["skipped"], {"note": None, "status": "Skipped", "buttons": []})
        self.assertEqual(after["moved"], {"note": None, "status": None, "buttons": ["Start"]})

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


class DesktopCalendarWeekAssertions:
    """Desktop: the confirmed plan opens straight into the week calendar (no step flow)."""

    def test_week_view_and_navigation(self):
        self.assertEqual(self.out["page"], "planner")
        this_week = self.out["weekThis"]
        self.assertTrue(this_week["wizardHidden"])
        self.assertRegex(this_week["label"], r"21.*27")
        self.assertEqual((len(this_week["days"]), this_week["sessions"], this_week["kinds"]), (1, ["Marketing"], ["confirmed"]))
        self.assertEqual(this_week["status"], "1 planned session this week · 45m")
        next_week = self.out["weekNext"]
        self.assertRegex(next_week["label"], r"28.*4")
        self.assertEqual((len(next_week["days"]), next_week["sessions"]), (2, ["Statistics", "Marketing"]))
        empty = self.out["weekEmpty"]
        self.assertEqual((empty["sessions"], empty["status"]), ([], "No sessions this week"))
        self.assertRegex(empty["queue"][-1], r"^Marketing .*12 · 19:00–19:30$")   # the queue still shows what is next
        self.assertEqual(self.out["weekBack"], this_week)

    def test_queue_lists_upcoming_work_with_actions(self):
        self.assertEqual(len(self.out["weekThis"]["queue"]), 4)
        self.assertRegex(self.out["weekThis"]["queue"][0], r"^Marketing .*25 · 18:00–18:45$")

    def test_week_keeps_history_and_shows_moved_session(self):
        after = self.out["weekAfter"]
        calendar = {(title, meta.split(" · ")[0]): kind for title, meta, kind in after["calendar"]}
        self.assertEqual(calendar[("Statistics", "Quiz")], "completed")
        self.assertEqual(calendar[("Statistics", "Review")], "skipped")
        self.assertEqual(calendar[("PowerBI", "Flashcards")], "confirmed")   # later today
        self.assertEqual(calendar[("Marketing", "Summary")], "confirmed")   # the moved session, on its new day
        self.assertEqual(after["moved"], {"note": None, "status": None, "buttons": ["Start"]})
        self.assertFalse(any(entry.startswith("Statistics") for entry in after["queue"]))   # history is not queued work


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TodayAndWeekDesktopTests(TodayAndWeekAssertions, DesktopCalendarWeekAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TodayAndWeekPhoneTests(TodayAndWeekAssertions, PhoneWeekListAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_runs_at_phone_width(self):
        self.assertEqual(self.out["width"], 390)


if __name__ == "__main__":
    unittest.main()
