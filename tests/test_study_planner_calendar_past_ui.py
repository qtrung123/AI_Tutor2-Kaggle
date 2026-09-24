"""Real-browser tests: the past is read-only history on the desktop Study Planner calendar.

"Now" is pinned to Thu 2026-09-24 14:00 browser-local. Past days and the part of today before
now take no new availability and no drops (muted, no create cursor); later today and future days
work as before. A missed session stays clickable, offers Reschedule/Skip (never Start) and moves
only to the future; a completed one stays inspectable; earlier weeks render read-only.
Runs at 1280px and 1100px. Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_drag_ui import MOCK
from tests.test_study_planner_calendar_ui import run_at_width

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
const column = (date) => document.querySelector(`#pcal-body .pcal-col[data-date="${date}"]`);
const addCalls = () => P.calls.filter((c) => c === "POST /api/planner/availability").length;
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("planner-workspace").getBoundingClientRect().bottom <= window.innerHeight + 1;
const show = (minute) => { $("pcal-scroll").scrollTop = Math.max(0, (minute - 60) * 0.8); };
const y = (date, minute) => column(date).getBoundingClientRect().top + (minute + 10) * 0.8;
// Drag on an empty stretch of a day column (creates availability when allowed).
const paint = async (date, from, to) => {
  const col = column(date);
  col.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientY: y(date, from)}));
  document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, clientY: y(date, to)}));
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  await sleep(250);
};
// A real pointer drag of a calendar event to a day/time.
const move = async (element, date, minute) => {
  const box = element.getBoundingClientRect();
  const from = {x: box.left + 10, y: box.top + 4};
  element.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientX: from.x, clientY: from.y}));
  const col = column(date).getBoundingClientRect();
  const to = {x: col.left + col.width / 2, y: col.top + (minute + 5) * 0.8 + 1};
  for (let step = 1; step <= 6; step += 1) {
    document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, clientX: from.x + (to.x - from.x) * step / 6, clientY: from.y + (to.y - from.y) * step / 6}));
  }
  const invalid = $("pcal-drop")?.classList.contains("is-invalid") || false;
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true, clientX: to.x, clientY: to.y}));
  element.dispatchEvent(new MouseEvent("click", {bubbles: true}));
  await sleep(400);
  return invalid;
};
const event = (sessionId) => document.querySelector(`.pcal-event[data-session-id="${sessionId}"]`);
const popoverButtons = () => [...$("pcal-popover").querySelectorAll("button")].map((b) => b.textContent);
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 14, 0, 0);   // Thu 24 Sep 2026, 14:00
  const session = (id, activity, start, end, minutes, status) => ({session_id: id, plan_id: "plan-1", document_id: "mkt.pdf",
    document_title: "Marketing", activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
    status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, rescheduled_from: null});
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null, learning_state: "learning"}];
  P.availability = [
    {availability_id: "a-fri", start_at: "18:00", end_at: "22:00", is_recurring: true, day_of_week: 4, date: null},
    {availability_id: "a-tue", start_at: "18:00", end_at: "22:00", is_recurring: false, day_of_week: null, date: "2026-09-22"},
    {availability_id: "a-thu", start_at: "09:00", end_at: "12:00", is_recurring: false, day_of_week: null, date: "2026-09-24"}];
  P.sessions = [
    session("s-missed", "summary", "2026-09-23T10:00:00", "2026-09-23T10:45:00", 45, "scheduled"),
    session("s-done", "flashcards", "2026-09-22T18:00:00", "2026-09-22T18:20:00", 20, "completed"),
    session("s-next", "quiz", "2026-09-25T20:00:00", "2026-09-25T20:30:00", 30, "scheduled")];
  setPage("planner"); await sleep(700);
  show(9 * 60);

  out.visual = {
    past: [...document.querySelectorAll("#pcal-body .pcal-col")].map((c) => [c.dataset.date, c.classList.contains("is-past")]),
    shade: column("2026-09-24").querySelector(".pcal-past-shade")?.style.height || null,
    pastCursor: getComputedStyle(column("2026-09-22")).cursor, futureCursor: getComputedStyle(column("2026-09-25")).cursor,
    shadeCursor: getComputedStyle(column("2026-09-24").querySelector(".pcal-past-shade")).cursor,
    pastAvailability: [...document.querySelectorAll(".pcal-avail")].map((a) => [a.closest(".pcal-col").dataset.date, a.classList.contains("is-past")]),
    nowLine: column("2026-09-24").querySelector(".pcal-now")?.style.top || null,
    kinds: Object.fromEntries([...document.querySelectorAll(".pcal-event")].map((e) => [e.dataset.sessionId, e.dataset.kind])),
  };

  // 1. Creating availability: past day and today-before-now refused on the page; later today and future days work.
  const before = addCalls();
  await paint("2026-09-22", 10 * 60, 11 * 60);
  out.previousDay = addCalls() - before;
  await paint("2026-09-24", 10 * 60 + 30, 11 * 60 + 30);   // an empty stretch of today, before now
  out.todayBeforeNow = addCalls() - before;
  show(13 * 60);
  await paint("2026-09-24", 15 * 60, 12 * 60);              // started after now, dragged up past it: clamped at 14:00
  await paint("2026-09-24", 16 * 60, 17 * 60);
  show(9 * 60);
  await paint("2026-09-26", 9 * 60, 10 * 60);
  out.created = P.availability.filter((s) => !["a-fri", "a-tue", "a-thu"].includes(s.availability_id))
    .map((s) => [s.day_of_week, s.start_at, s.end_at, s.utc_offset_minutes]);
  out.expectedOffset = -new Date().getTimezoneOffset();

  // 2. Past availability: its details, but no Repeat weekly / Delete.
  show(18 * 60);
  const tue = [...column("2026-09-22").querySelectorAll(".pcal-avail")][0];
  tue.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientY: tue.getBoundingClientRect().bottom - 6}));
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  await sleep(100);
  out.pastPopover = {open: !$("pcal-popover").hidden, text: $("pcal-popover").textContent, repeat: !!$("pcal-repeat"), remove: !!$("pcal-delete-availability")};
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);

  // 3. The missed session: clickable, Reschedule/Skip (never Start), moves only to the future.
  show(9 * 60);
  event("s-missed").click(); await sleep(100);
  out.missedPopover = {text: $("pcal-popover").textContent, buttons: popoverButtons()};
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);
  out.dropPastDay = {invalid: await move(event("s-missed"), "2026-09-22", 11 * 60), calls: P.rescheduleCalls.length, toast: toast.textContent};
  out.dropTodayPast = {invalid: await move(event("s-missed"), "2026-09-24", 13 * 60), calls: P.rescheduleCalls.length};
  show(18 * 60);
  out.dropFuture = {invalid: await move(event("s-missed"), "2026-09-25", 18 * 60 + 30), calls: P.rescheduleCalls.map((c) => c.body.target_start)};
  out.afterMove = Object.fromEntries([...document.querySelectorAll(".pcal-event")].map((e) => [e.dataset.sessionId, [e.closest(".pcal-col").dataset.date, e.dataset.kind]]));

  // 4. A completed session stays inspectable.
  event("s-done").click(); await sleep(100);
  out.donePopover = {text: $("pcal-popover").textContent, buttons: popoverButtons()};
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);
  out.overflow.thisWeek = noOverflow();

  // 5. An earlier week: renders normally, all read-only.
  $("pcal-prev").click(); await sleep(150);
  const beforePrev = addCalls();
  out.previousWeek = {range: $("pcal-range").textContent, allPast: [...document.querySelectorAll("#pcal-body .pcal-col")].every((c) => c.classList.contains("is-past"))};
  await paint("2026-09-16", 10 * 60, 11 * 60);
  out.previousWeek.created = addCalls() - beforePrev;
  out.previousWeek.recurringShown = [...document.querySelectorAll(".pcal-avail")].map((a) => [a.closest(".pcal-col").dataset.date, a.classList.contains("is-past")]);
  out.overflow.previousWeek = noOverflow();
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class PastReadOnlyAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_past_is_muted_with_no_create_cursor(self):
        visual = self.out["visual"]
        self.assertEqual(visual["past"], [[f"2026-09-{day}", day < 24] for day in range(21, 28)])
        self.assertEqual(visual["shade"], "672px")           # 14:00 on a 48px/hour grid
        self.assertEqual(visual["nowLine"], "672px")
        self.assertEqual((visual["pastCursor"], visual["shadeCursor"], visual["futureCursor"]), ("default", "default", "crosshair"))
        self.assertIn(["2026-09-22", True], visual["pastAvailability"])    # a past date
        self.assertIn(["2026-09-24", True], visual["pastAvailability"])    # today, over before now
        self.assertIn(["2026-09-25", False], visual["pastAvailability"])
        self.assertEqual(visual["kinds"], {"s-missed": "overdue", "s-done": "completed", "s-next": "confirmed"})

    def test_availability_cannot_be_created_in_the_past(self):
        self.assertEqual(self.out["previousDay"], 0)
        self.assertEqual(self.out["todayBeforeNow"], 0)

    def test_later_today_and_future_days_work_and_send_the_offset(self):
        offset = self.out["expectedOffset"]
        self.assertEqual(self.out["created"], [[3, "14:00", "15:30", offset], [3, "16:00", "17:30", offset], [5, "09:00", "10:30", offset]])

    def test_past_availability_is_read_only(self):
        popover = self.out["pastPopover"]
        self.assertTrue(popover["open"])
        self.assertIn("18:00–22:00", popover["text"])
        self.assertEqual((popover["repeat"], popover["remove"]), (False, False))

    def test_missed_session_offers_reschedule_and_moves_only_to_the_future(self):
        self.assertIn("Not completed", self.out["missedPopover"]["text"])
        self.assertEqual(self.out["missedPopover"]["buttons"], ["×", "Reschedule", "Skip"])
        self.assertEqual(self.out["dropPastDay"], {"invalid": True, "calls": 0, "toast": "That time has already passed."})
        self.assertEqual(self.out["dropTodayPast"], {"invalid": True, "calls": 0})
        self.assertEqual(self.out["dropFuture"], {"invalid": False, "calls": ["2026-09-25T18:30:00"]})
        self.assertEqual(self.out["afterMove"]["s-missed-m"], ["2026-09-25", "confirmed"])
        self.assertNotIn("s-missed", self.out["afterMove"])

    def test_completed_session_stays_inspectable(self):
        done = self.out["donePopover"]
        self.assertIn("Completed", done["text"])
        self.assertEqual(done["buttons"], ["×"])

    def test_previous_week_renders_read_only(self):
        previous = self.out["previousWeek"]
        self.assertRegex(previous["range"], r"^14\D.*20, 2026$")
        self.assertTrue(previous["allPast"])
        self.assertEqual(previous["created"], 0)
        # the weekly slots (Fri, plus the Thu/Sat ones created above) show as read-only history
        self.assertEqual(sorted({date for date, _ in previous["recurringShown"]}), ["2026-09-17", "2026-09-18", "2026-09-19"])
        self.assertTrue(all(past for _, past in previous["recurringShown"]))

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PastReadOnlyDesktopTests(PastReadOnlyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PastReadOnlyNarrowDesktopTests(PastReadOnlyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
