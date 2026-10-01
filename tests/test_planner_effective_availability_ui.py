"""Real-browser tests for the desktop Planner: effective availability and the collapsible side rails
(headless Chrome, mocked API from tests/test_google_calendar_planner_ui.py).

1. "Available" blocks are the learner's marked time minus Google busy time -- exactly the pieces the
   scheduler uses (the shared CASES of tests/test_planner_effective_availability.py) -- never one block
   drawn through a "Busy" block; with "Avoid conflicts" off the marked time is shown whole again.
2. Materials and Study queue fold independently to zero width (only a floating edge button stays),
   the calendar takes the freed width, and the state is remembered in localStorage (planner_materials_collapsed /
   planner_queue_collapsed). "Now" is pinned to Mon 2026-09-21 08:00. Skipped without Chrome.
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
const widths = () => Object.fromEntries(["materials", "main", "queue"].map((name) => [name, Math.round(document.querySelector(`.pcal-${name}`).getBoundingClientRect().width)]));
const railState = () => ({classes: $("planner-workspace").className, widths: widths(),
  stored: [localStorage.getItem("planner_materials_collapsed"), localStorage.getItem("planner_queue_collapsed")],
  materialsList: visible($("pcal-material-list")), gcal: visible($("pcal-gcal")), queueList: visible($("pcal-queue")),
  materialsExpand: visible($("pcal-materials-expand")), queueExpand: visible($("pcal-queue-expand")),
  materialsCollapse: [visible($("pcal-materials-collapse")), $("pcal-materials-collapse").getAttribute("aria-expanded")],
  queueCollapse: [visible($("pcal-queue-collapse")), $("pcal-queue-collapse").getAttribute("aria-expanded")],
  gcalButtons: [...$("pcal-gcal").querySelectorAll("button, input")].map((b) => b.id), weekRange: $("pcal-range").textContent,
  expandButtonSizes: ["materials", "queue"].map((rail) => { const r = $(`pcal-${rail}-expand`).getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })});
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

  // 2. Collapsible rails.
  out.rails = {expanded: railState()};
  $("pcal-materials-collapse").click(); await sleep(100);
  out.rails.materials = {...railState(), focus: document.activeElement?.id};
  $("pcal-queue-collapse").click(); await sleep(100);
  out.rails.both = railState();
  $("pcal-materials-expand").click(); await sleep(100);
  out.rails.queue = {...railState(), focus: document.activeElement?.id};
  $("pcal-queue-expand").click(); await sleep(100);
  out.rails.restored = railState();
  // The calendar still works with folded rails: week navigation and the grid render.
  $("pcal-materials-collapse").click(); $("pcal-queue-collapse").click(); await sleep(100);
  $("pcal-next").click(); await sleep(300); $("pcal-today").click(); await sleep(300);
  out.rails.navigated = {...railState(), columns: document.querySelectorAll("#pcal-body .pcal-col").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

# A second page load with both rails stored as collapsed: they start folded.
PERSISTED_MOCK = MOCK + r"""
localStorage.setItem("planner_materials_collapsed", "true");
localStorage.setItem("planner_queue_collapsed", "true");
"""
PERSISTED_DRIVER = r"""
{
const out = {errors: []};
window.addEventListener("error", (e) => out.errors.push(String(e.message)));
setTimeout(() => {
  setPage("planner");
  setTimeout(() => {
    const w = (name) => Math.round(document.querySelector(`.pcal-${name}`).getBoundingClientRect().width);
    out.classes = document.getElementById("planner-workspace").className;
    out.widths = {materials: w("materials"), main: w("main"), queue: w("queue")};
    window.parent.postMessage(JSON.stringify(out), "*");
  }, 800);
}, 1500);
}
"""


GAP = 10   # .pcal column gap, removed together with a folded rail's column


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

    def test_rails_start_expanded(self):
        expanded = self.out["rails"]["expanded"]
        self.assertNotIn("collapsed", self.out["initial"]["classes"])
        self.assertEqual((expanded["widths"]["materials"], expanded["widths"]["queue"]), (196, 236))
        self.assertTrue(expanded["materialsList"] and expanded["gcal"] and expanded["queueList"])
        self.assertFalse(expanded["materialsExpand"] or expanded["queueExpand"])
        self.assertEqual((expanded["materialsCollapse"], expanded["queueCollapse"]), ([True, "true"], [True, "true"]))
        self.assertEqual(expanded["stored"], [None, None])

    def test_materials_collapse_gives_the_calendar_its_width(self):
        rails = self.out["rails"]
        expanded, folded = rails["expanded"], rails["materials"]
        self.assertIn("is-materials-collapsed", folded["classes"])
        self.assertEqual(folded["widths"]["materials"], 0)
        self.assertEqual(folded["widths"]["main"] - expanded["widths"]["main"], 196 + GAP)   # column and its gap are gone
        self.assertEqual(folded["widths"]["queue"], 236)
        self.assertTrue(folded["materialsExpand"])
        self.assertFalse(folded["materialsList"] or folded["gcal"])
        self.assertTrue(folded["queueList"])
        self.assertEqual(folded["stored"], ["true", None])
        self.assertEqual(folded["focus"], "pcal-materials-expand")

    def test_both_collapsed_then_each_expands_independently(self):
        rails = self.out["rails"]
        both, queue = rails["both"], rails["queue"]
        self.assertEqual((both["widths"]["materials"], both["widths"]["queue"]), (0, 0))
        self.assertEqual(both["widths"]["main"] - rails["expanded"]["widths"]["main"], 196 + GAP + 236 + GAP)
        self.assertEqual(both["expandButtonSizes"], [[40, 40], [40, 40]])
        self.assertEqual(both["stored"], ["true", "true"])
        self.assertTrue(both["materialsExpand"] and both["queueExpand"])
        # Materials reopened, queue still folded: Google Calendar controls are back.
        self.assertEqual((queue["widths"]["materials"], queue["widths"]["queue"]), (196, 0))
        self.assertTrue(queue["gcal"] and queue["materialsList"])
        self.assertIn("pcal-gcal-avoid", queue["gcalButtons"])
        self.assertEqual(queue["stored"], ["false", "true"])
        self.assertEqual(queue["focus"], "pcal-materials-collapse")
        restored = rails["restored"]
        self.assertEqual(restored["widths"], rails["expanded"]["widths"])
        self.assertTrue(restored["queueList"] and restored["gcal"])
        self.assertEqual(restored["stored"], ["false", "false"])

    def test_week_navigation_with_folded_rails(self):
        navigated = self.out["rails"]["navigated"]
        self.assertEqual(navigated["columns"], 7)
        self.assertEqual(navigated["weekRange"], self.out["rails"]["expanded"]["weekRange"])
        self.assertIn("is-materials-collapsed", navigated["classes"])
        self.assertIn("is-queue-collapsed", navigated["classes"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PersistedRailStateUiTests(unittest.TestCase):
    def test_stored_collapsed_rails_start_folded(self):
        out = run_at_width(1440, 900, mock=PERSISTED_MOCK, driver=PERSISTED_DRIVER)
        self.assertEqual(out["errors"], [])
        self.assertIn("is-materials-collapsed", out["classes"])
        self.assertIn("is-queue-collapsed", out["classes"])
        self.assertEqual((out["widths"]["materials"], out["widths"]["queue"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
