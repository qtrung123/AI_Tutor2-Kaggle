"""Real-browser tests for the desktop Planner: effective availability and the Materials / Study queue panels
(headless Chrome, mocked API from tests/test_google_calendar_planner_ui.py).

1. "Available" blocks are the learner's marked time minus Google busy time -- exactly the pieces the
   scheduler uses (the shared CASES of tests/test_planner_effective_availability.py) -- never one block
   drawn through a "Busy" block; with "Avoid conflicts" off the marked time is shown whole again.
2. Materials and Study queue open from two icon buttons at the top right of the calendar header, as
   popovers anchored under them; the calendar keeps its full width (no grid column is ever added). One
   panel at a time; the same button, a click outside or Escape closes it. Old stored collapse flags are
   ignored. "Now" is pinned to Mon 2026-09-21 08:00. Skipped without Chrome.
"""

import json
import unittest

from tests.test_google_calendar_planner_ui import MOCK
from tests.test_planner_effective_availability import AVAILABLE, CASES, DAY
from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }   // 1440px runs in an iframe
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const CASES = __CASES__;
const DAY = "__DAY__";
const blocks = (selector) => [...document.querySelectorAll(`#pcal-body .pcal-col[data-date="${DAY}"] ${selector}`)].map((b) => ({
  start: b.dataset.start, end: b.dataset.end, time: b.querySelector(".pcal-avail-time")?.textContent || null,
  label: b.getAttribute("aria-label"), top: parseFloat(b.style.top), bottom: parseFloat(b.style.top) + parseFloat(b.style.height)}));
const box = (el) => { const r = el.getBoundingClientRect(); return {left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top), bottom: Math.round(r.bottom), width: Math.round(r.width)}; };
const panelState = () => ({classes: $("planner-workspace").className, workspace: box($("planner-workspace")), main: box(document.querySelector(".pcal-main")),
  columns: getComputedStyle($("planner-workspace")).gridTemplateColumns.split(" ").length, scrollTop: box($("pcal-scroll")).top,
  materialsList: visible($("pcal-material-list")), gcal: visible($("pcal-gcal")), queueList: visible($("pcal-queue")),
  expanded: ["materials", "queue"].map((panel) => $(`pcal-${panel}-toggle`).getAttribute("aria-expanded")),
  toggles: box(document.querySelector(".pcal-panel-toggles")), materials: box($("pcal-materials-rail")), queue: box($("pcal-queue-rail")),
  gcalButtons: [...$("pcal-gcal").querySelectorAll("button, input")].map((b) => b.id), focus: document.activeElement?.id,
  weekRange: $("pcal-range").textContent});
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 21, 8, 0, 0);   // Mon 21 Sep 2026, 08:00
  const G = window.__google;
  const P = window.__planner;
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null, familiarity: null, learning_state: "new"}];
  P.availability = [{availability_id: "a0", is_recurring: false, date: DAY, start_at: "__FROM__", end_at: "__TO__"}];
  G.connected = true;
  out.initial = {classes: $("planner-workspace").className};
  setPage("planner"); await sleep(800);
  await plannerLoadGoogleCalendar(); await sleep(400);
  out.durations = Object.fromEntries([30, 60, 90, 120, 150, 180].map((m) => [m, plannerFormatDuration(m)]));

  // 1. Each busy case: the rendered "Available" pieces, the busy blocks and placement inside busy time.
  const render = async (busy) => {
    G.busy = busy.map(([from, to]) => ({start: `${DAY}T${from}:00`, end: `${DAY}T${to}:00`}));
    plannerGoogleBusyWeek = null;   // refetch this week's busy time, as after a settings change
    pcalRenderGrid(); await sleep(400);
    return {available: blocks(".pcal-avail"), busy: blocks(".pcal-busy")};
  };
  out.cases = {};
  for (const [name, [busy]] of Object.entries(CASES)) {
    const state = await render(busy);
    state.busyPlacement = busy.map(([from]) => pcalCheckTarget(DAY, 1, plannerToMinutes(from) + (plannerToMinutes(from) % 30 ? 30 - plannerToMinutes(from) % 30 : 0), {duration: 15, documentId: "mkt.pdf"}));
    out.cases[name] = state;
  }

  // The popover of a piece says it belongs to a larger marked slot (its actions edit the whole slot).
  await render(CASES.busy_inside[0]);
  document.querySelector(`#pcal-body .pcal-col[data-date="${DAY}"] .pcal-avail`).dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true}));
  await sleep(100);
  out.popover = $("pcal-popover").hidden ? null : $("pcal-popover").textContent;
  pcalClosePopover();

  // "Avoid conflicts" off: no busy blocks, the marked time is whole and schedulable again.
  $("pcal-gcal-avoid").click(); await sleep(500);
  out.avoidOff = {available: blocks(".pcal-avail"), busy: blocks(".pcal-busy"),
    placement: pcalCheckTarget(DAY, 1, 10 * 60, {duration: 30, documentId: "mkt.pdf"})};
  $("pcal-gcal-avoid").click(); await sleep(500);
  out.avoidOn = {available: blocks(".pcal-avail"), busy: blocks(".pcal-busy")};

  // 2. Materials / Study queue panels.
  out.panels = {closed: panelState()};
  $("pcal-materials-toggle").click(); await sleep(100);
  out.panels.materials = panelState();
  $("pcal-queue-toggle").click(); await sleep(100);
  out.panels.queue = panelState();                       // switching: the other one closes
  $("pcal-queue-toggle").click(); await sleep(100);
  out.panels.toggledOff = panelState();                  // the same button closes it
  $("pcal-materials-toggle").click(); await sleep(100);
  $("pcal-material-list").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.panels.insideClick = panelState();
  $("pcal-range").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
  out.panels.outsideClick = panelState();
  // A calendar block stops its pointerdown from bubbling; the panel still closes.
  $("pcal-queue-toggle").click(); await sleep(100);
  const avail = document.querySelector("#pcal-body .pcal-avail");
  avail.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientY: avail.getBoundingClientRect().top + 4}));
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  out.panels.blockClick = panelState();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  $("pcal-queue-toggle").click(); await sleep(100);
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  out.panels.escape = panelState();
  // The calendar still works with a panel open: week navigation and the grid render.
  $("pcal-materials-toggle").click(); await sleep(100);
  $("pcal-next").click(); await sleep(300); $("pcal-today").click(); await sleep(300);
  out.panels.navigated = {...panelState(), columns7: document.querySelectorAll("#pcal-body .pcal-col").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

# A narrower desktop with the old stored "collapsed" flags: they no longer affect the layout.
PERSISTED_MOCK = MOCK + r"""
localStorage.setItem("planner_materials_collapsed", "true");
localStorage.setItem("planner_queue_collapsed", "true");
"""
PERSISTED_DRIVER = r"""
{
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message)));
const box = (el) => { const r = el.getBoundingClientRect(); return {left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top), bottom: Math.round(r.bottom), width: Math.round(r.width)}; };
setTimeout(() => {
  setPage("planner");
  setTimeout(() => {
    const workspace = document.getElementById("planner-workspace");
    out.classes = workspace.className;
    out.workspace = box(workspace);
    out.main = box(document.querySelector(".pcal-main"));
    document.getElementById("pcal-queue-toggle").click();
    setTimeout(() => {
      out.openMain = box(document.querySelector(".pcal-main"));
      out.queue = box(document.getElementById("pcal-queue-rail"));
      out.toggles = box(document.querySelector(".pcal-panel-toggles"));
      out.viewport = window.innerWidth;
      window.parent.postMessage(JSON.stringify(out), "*");
    }, 150);
  }, 800);
}, 1500);
}
"""


def _driver():
    return (DRIVER.replace("__CASES__", json.dumps(CASES)).replace("__DAY__", DAY)
            .replace("__FROM__", AVAILABLE[0]).replace("__TO__", AVAILABLE[1]))


def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class EffectiveAvailabilityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1440, 900, mock=MOCK, driver=_driver())

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out, self.out.get("fatal"))
        self.assertEqual(self.out["errors"], [])

    def assert_pieces(self, rendered, expected):
        self.assertEqual([(b["start"], b["end"]) for b in rendered], expected)
        for block, (start, end) in zip(rendered, expected):
            duration = self.out["durations"][str(_minutes(end) - _minutes(start))]
            self.assertEqual(block["time"], f"{start}–{end} · {duration}")   # the piece's own length
            self.assertEqual(block["label"], f"Available {start}–{end}")

    def test_available_blocks_are_marked_time_minus_busy(self):
        for name, (busy, expected) in CASES.items():
            with self.subTest(name):
                state = self.out["cases"][name]
                self.assert_pieces(state["available"], expected)   # same pieces as the scheduler
                self.assertEqual([(b["start"], b["end"]) for b in state["busy"]], [tuple(b) for b in busy])

    def test_no_available_block_is_drawn_through_busy_time(self):
        for name, state in self.out["cases"].items():
            with self.subTest(name):
                for available in state["available"]:
                    for busy in state["busy"]:
                        self.assertTrue(available["bottom"] <= busy["top"] or available["top"] >= busy["bottom"], (available, busy))

    def test_nothing_can_be_placed_inside_busy_time(self):
        for name, state in self.out["cases"].items():
            with self.subTest(name):
                for reason in state["busyPlacement"]:
                    self.assertIn(reason, ("That time is busy in your Google Calendar.", "Pick a time inside your available hours."))

    def test_piece_popover_edits_the_whole_marked_slot(self):
        self.assertIn("08:30–09:30", self.out["popover"])
        self.assertIn("Part of 08:30–11:30", self.out["popover"])
        self.assertIn("Delete", self.out["popover"])

    def test_avoid_conflicts_off_shows_the_marked_time_whole(self):
        off = self.out["avoidOff"]
        self.assert_pieces(off["available"], [AVAILABLE])
        self.assertEqual(off["busy"], [])
        self.assertIsNone(off["placement"])
        self.assert_pieces(self.out["avoidOn"]["available"], CASES["busy_inside"][1])

    def test_panels_start_closed_and_the_calendar_has_full_width(self):
        closed = self.out["panels"]["closed"]
        self.assertNotIn("-open", closed["classes"])
        self.assertNotIn("collapsed", closed["classes"])
        self.assertEqual(closed["expanded"], ["false", "false"])
        self.assertFalse(closed["materialsList"] or closed["gcal"] or closed["queueList"])
        self.assertEqual(closed["columns"], 1)
        self.assertEqual((closed["main"]["left"], closed["main"]["right"]), (closed["workspace"]["left"], closed["workspace"]["right"]))
        # Both buttons sit side by side at the top right of the calendar header.
        toggles = closed["toggles"]
        self.assertLessEqual(closed["main"]["right"] - toggles["right"], 24)
        self.assertLess(toggles["bottom"], closed["scrollTop"])
        self.assertLessEqual(toggles["width"], 90)

    def test_materials_opens_as_an_anchored_popover_without_shrinking_the_calendar(self):
        panels = self.out["panels"]
        closed, opened = panels["closed"], panels["materials"]
        self.assertIn("is-materials-open", opened["classes"])
        self.assertEqual(opened["expanded"], ["true", "false"])
        self.assertTrue(opened["materialsList"] and opened["gcal"])
        self.assertIn("pcal-gcal-avoid", opened["gcalButtons"])
        self.assertFalse(opened["queueList"])
        self.assertEqual((opened["main"], opened["columns"]), (closed["main"], 1))
        panel = opened["materials"]
        self.assertGreaterEqual(panel["top"], opened["toggles"]["bottom"])
        self.assertLessEqual(panel["top"] - opened["toggles"]["bottom"], 12)
        self.assertEqual(panel["right"], opened["toggles"]["right"])
        self.assertGreaterEqual(panel["left"], opened["workspace"]["left"])
        self.assertLessEqual(panel["bottom"], opened["workspace"]["bottom"])

    def test_only_one_panel_and_the_same_button_closes_it(self):
        panels = self.out["panels"]
        queue = panels["queue"]
        self.assertIn("is-queue-open", queue["classes"])
        self.assertNotIn("is-materials-open", queue["classes"])
        self.assertEqual(queue["expanded"], ["false", "true"])
        self.assertTrue(queue["queueList"])
        self.assertFalse(queue["materialsList"] or queue["gcal"])
        self.assertEqual(queue["main"], panels["closed"]["main"])
        self.assertEqual(queue["queue"]["right"], queue["toggles"]["right"])
        off = panels["toggledOff"]
        self.assertEqual(off["expanded"], ["false", "false"])
        self.assertFalse(off["materialsList"] or off["queueList"])

    def test_click_outside_and_escape_close_the_panel(self):
        panels = self.out["panels"]
        self.assertTrue(panels["insideClick"]["materialsList"])        # a click inside keeps it open
        self.assertFalse(panels["outsideClick"]["materialsList"])
        self.assertEqual(panels["outsideClick"]["expanded"], ["false", "false"])
        self.assertFalse(panels["blockClick"]["queueList"])            # also from a calendar block
        escape = panels["escape"]
        self.assertFalse(escape["queueList"])
        self.assertEqual(escape["expanded"], ["false", "false"])
        self.assertEqual(escape["focus"], "pcal-queue-toggle")

    def test_week_navigation_with_a_panel_open(self):
        navigated = self.out["panels"]["navigated"]
        self.assertEqual(navigated["columns7"], 7)
        self.assertEqual(navigated["weekRange"], self.out["panels"]["closed"]["weekRange"])
        self.assertTrue(navigated["materialsList"])
        self.assertEqual(navigated["main"], self.out["panels"]["closed"]["main"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class NarrowDesktopPanelUiTests(unittest.TestCase):
    def test_old_stored_flags_are_ignored_and_the_panel_fits(self):
        out = run_at_width(1100, 800, mock=PERSISTED_MOCK, driver=PERSISTED_DRIVER)
        self.assertEqual(out["errors"], [])
        self.assertNotIn("collapsed", out["classes"])
        self.assertEqual((out["main"]["left"], out["main"]["right"]), (out["workspace"]["left"], out["workspace"]["right"]))
        self.assertEqual(out["openMain"], out["main"])
        self.assertEqual(out["queue"]["right"], out["toggles"]["right"])
        self.assertGreaterEqual(out["queue"]["left"], out["workspace"]["left"])
        self.assertLessEqual(out["queue"]["right"], out["viewport"])


if __name__ == "__main__":
    unittest.main()
