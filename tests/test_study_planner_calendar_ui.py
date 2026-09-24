"""Real-browser tests for the desktop calendar-first Study Planner (Phase 7A; headless Chrome, mocked
v2 API from tests/test_study_planner_v2_ui.py). At >=1024px the planner is one week calendar:
add materials (optional inline deadline) -> drag availability -> the preview endpoint runs by itself
(debounced) and its sessions appear as dashed ghost events -> Accept plan confirms once and the same
events turn solid in place -> reload opens straight into the confirmed week. "Now" is pinned to
Mon 2026-09-21 08:00 browser-local (the week's Monday evening is still ahead, so it can be marked
available); the mocked schedule lands on Mon 28 / Tue 29 September.
Runs at 1280px (three columns) and 1100px (calendar + one side column); 900px keeps the step flow.
Skipped when Chrome is not installed.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_quiz_player_ui import FRONTEND, find_chrome
from tests.test_study_planner_v2_ui import MOCK

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
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const column = (date) => document.querySelector(`#pcal-body .pcal-col[data-date="${date}"]`);
const events = () => [...document.querySelectorAll("#pcal-body .pcal-event")].sort((a, b) => a.dataset.start.localeCompare(b.dataset.start));
const describeEvent = (e) => ({date: e.closest(".pcal-col").dataset.date, kind: e.dataset.kind, top: e.style.top, height: e.style.height,
  title: e.querySelector(".pcal-event-title").textContent, meta: e.querySelector(".pcal-event-meta").textContent,
  border: getComputedStyle(e).borderTopStyle, background: getComputedStyle(e).backgroundColor, z: Number(getComputedStyle(e).zIndex)});
const availability = () => [...document.querySelectorAll("#pcal-body .pcal-avail")].map((a) => ({date: a.closest(".pcal-col").dataset.date,
  text: a.textContent, top: a.style.top, height: a.style.height, recurring: a.dataset.recurring,
  background: getComputedStyle(a).backgroundColor, z: Number(getComputedStyle(a).zIndex)}));
// The Study Queue: only work with no calendar slot (title, activity · duration, reason, "Needs a time slot").
const queue = () => ({items: [...document.querySelectorAll("#pcal-queue .pcal-queue-item")].map((li) => ({
  title: li.querySelector(".pcal-queue-title").textContent, meta: li.querySelector(".pcal-queue-meta").textContent,
  reason: li.querySelector(".pcal-queue-reason")?.textContent || null, need: li.querySelector(".pcal-queue-need").textContent,
  buttons: [...li.querySelectorAll("button")].map((b) => b.textContent)})),
  empty: document.querySelector("#pcal-queue .pcal-queue-empty")?.textContent || null,
  sessionCards: document.querySelectorAll("#pcal-queue .planner-session").length,
  buttonCount: $("pcal-queue").closest(".pcal-queue").querySelectorAll("button").length,
  text: $("pcal-queue").closest(".pcal-queue").textContent});
const toolbar = () => ({range: $("pcal-range").textContent, status: $("pcal-status").textContent,
  acceptShown: visible($("pcal-accept")), acceptDisabled: $("pcal-accept").disabled, acceptText: $("pcal-accept").textContent,
  autoPlanShown: visible($("pcal-auto-plan")), notice: $("pcal-notice").hidden ? null : $("pcal-notice").textContent});
const markers = () => [...document.querySelectorAll(".pcal-allday")].filter((c) => c.children.length)
  .map((c) => [c.dataset.date, [...c.children].map((m) => m.textContent)]);
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("planner-workspace").getBoundingClientRect().bottom <= window.innerHeight + 1
  && [...document.querySelectorAll(".pcal-rail, .pcal-main")].every((el) => el.getBoundingClientRect().right <= window.innerWidth + 1);
// A drag on a day column, in real pointer events (down on the column, move/up on the document).
const drag = async (date, fromMinute, toMinute) => {
  const col = column(date);
  const y = (minute) => col.getBoundingClientRect().top + (minute + 10) * 0.8;
  col.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientY: y(fromMinute)}));
  if (toMinute !== undefined) document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, clientY: y(toMinute)}));
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  await sleep(150);
};
const clickBlock = async (block) => {
  block.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientY: block.getBoundingClientRect().top + 4}));
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
  await sleep(100);
};
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 21, 8, 0, 0);   // Mon 21 Sep 2026, 08:00
  out.desktop = innerWidth >= 1024;
  setPage("planner"); await sleep(500);
  if (!out.desktop) {
    out.tablet = {wizardShown: visible(document.querySelector(".planner-shell")), workspaceHidden: $("planner-workspace").hidden,
      steps: document.querySelectorAll("[data-step-indicator]").length};
    publish();
    return;
  }

  // 1. First visit: straight into this week's calendar; no step flow; ask for materials only.
  out.first = {...toolbar(), wizardHidden: document.querySelector(".planner-shell").hidden, workspace: visible($("planner-workspace")),
    stepsVisible: [...document.querySelectorAll("[data-step-indicator]")].some(visible),
    columns: [...document.querySelectorAll("#pcal-body .pcal-col")].map((c) => c.dataset.date),
    dayHeads: [...document.querySelectorAll(".pcal-dayhead")].map((h) => h.textContent),
    today: document.querySelector(".pcal-dayhead.is-today")?.textContent, nowLine: !!column("2026-09-21").querySelector(".pcal-now"),
    nowTop: column("2026-09-21").querySelector(".pcal-now")?.style.top,
    hours: document.querySelectorAll(".pcal-hour").length, stickyHead: getComputedStyle($("pcal-head")).position,
    innerScroll: $("pcal-scroll").scrollHeight > $("pcal-scroll").clientHeight, scrolledTo: $("pcal-scroll").scrollTop,
    queueEmpty: document.querySelector(".pcal-queue-empty")?.textContent, plans: P.plans.length,
    topbarHidden: getComputedStyle(document.querySelector(".topbar")).display === "none"};
  out.overflow.first = noOverflow();

  // 2. Add materials from the side sheet, one with an inline deadline.
  document.querySelector(".pcal-notice-action").click(); await sleep(100);
  out.sheet = {open: visible($("pcal-sheet")), rows: [...document.querySelectorAll(".pcal-sheet-row strong")].map((s) => s.textContent)};
  const row = (id) => document.querySelector(`.pcal-sheet-row[data-document-id="${id}"]`);
  row("mkt.pdf").querySelector('input[type="date"]').value = "2026-10-01";
  row("mkt.pdf").querySelector(".pcal-sheet-add").click(); await sleep(300);
  row("stats.pdf").querySelector(".pcal-sheet-add").click(); await sleep(300);
  out.sheet.rowsAfter = [...document.querySelectorAll(".pcal-sheet-row strong")].map((s) => s.textContent);
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);
  out.sheet.closed = $("pcal-sheet").hidden;
  out.materials = {saved: P.materials.map((m) => [m.document_id, m.deadline]), plans: P.plans.length,
    rail: [...document.querySelectorAll(".pcal-material")].map((m) => ({title: m.querySelector(".pcal-material-title").textContent,
      pill: m.querySelector(".pcal-pill").textContent, deadline: m.querySelector('input[type="date"]').value})),
    ...toolbar(), previews: P.previewBodies.length, queueEmpty: document.querySelector(".pcal-queue-empty")?.textContent};

  // 3. One click on Monday 18:00 = 30 minutes of weekly availability -> preview runs by itself (at risk).
  await drag("2026-09-21", 18 * 60);
  out.firstSlot = {saved: P.availability.map((s) => [s.day_of_week, s.start_at, s.end_at, s.is_recurring]), toolbar: toolbar()};
  await sleep(900);
  out.atRisk = {previews: P.previewBodies.length, ...toolbar(), queue: queue()};

  // 4. Drag Monday 18:30 -> 21:30: availability is now 4h; one debounced preview for the change.
  await drag("2026-09-21", 18 * 60 + 30, 21 * 60 + 30);
  out.dragged = P.availability.map((s) => [s.day_of_week, s.start_at, s.end_at, s.is_recurring]);
  await sleep(900);
  out.onTrack = {previews: P.previewBodies.length, ...toolbar(), queue: queue(), thisWeekEvents: events().length,
    availability: availability().filter((a) => a.date === "2026-09-21")};
  out.overflow.preview = noOverflow();

  // 5. Next week: the suggested sessions are ghost events on the right day/time; deadline markers on top.
  $("pcal-next").click(); await sleep(100);
  out.nextWeek = {range: $("pcal-range").textContent, events: events().map(describeEvent), markers: markers(),
    availability: availability().filter((a) => a.date === "2026-09-28")};

  // 6. Inline deadline edit + two quick edits -> exactly one more preview (debounced).
  const before = P.previewBodies.length;
  const statsDeadline = document.querySelector('.pcal-material[data-document-id="stats.pdf"] input[type="date"]');
  statsDeadline.value = "2026-10-03"; statsDeadline.dispatchEvent(new Event("change", {bubbles: true})); await sleep(60);
  const statsDeadline2 = document.querySelector('.pcal-material[data-document-id="stats.pdf"] input[type="date"]');
  statsDeadline2.value = "2026-10-02"; statsDeadline2.dispatchEvent(new Event("change", {bubbles: true})); await sleep(900);
  out.deadlineEdit = {previews: P.previewBodies.length - before, saved: P.materials.map((m) => [m.document_id, m.deadline]), markers: markers()};

  // 7. Availability popover: click the block -> time, Repeat weekly, Delete (for a separate Wednesday slot).
  $("pcal-today").click(); await sleep(100);
  out.todayButton = $("pcal-range").textContent;
  await drag("2026-09-23", 9 * 60, 9 * 60 + 30); await sleep(900);
  const wed = () => [...column("2026-09-23").querySelectorAll(".pcal-avail")][0];
  await clickBlock(wed());
  out.popover = {open: visible($("pcal-popover")), text: $("pcal-popover").textContent, repeat: $("pcal-repeat")?.checked,
    hasDelete: !!$("pcal-delete-availability")};
  const previewsBeforeDelete = P.previewBodies.length;
  $("pcal-delete-availability").click(); await sleep(900);
  out.deleted = {availability: P.availability.map((s) => [s.day_of_week, s.start_at, s.end_at]), popoverClosed: $("pcal-popover").hidden,
    wednesdayBlocks: column("2026-09-23").querySelectorAll(".pcal-avail").length, previews: P.previewBodies.length - previewsBeforeDelete};

  // 8. Manual rerun is optional: Auto Plan re-runs the same preview.
  const beforeAuto = P.previewBodies.length;
  $("pcal-auto-plan").click(); await sleep(400);
  out.autoPlan = P.previewBodies.length - beforeAuto;

  // 9. Ghost event popover (read-only), then Accept plan: double click -> one confirm; ghosts turn solid in place.
  $("pcal-next").click(); await sleep(100);
  events()[0].click(); await sleep(50);
  out.ghostPopover = {open: visible($("pcal-popover")), text: $("pcal-popover").textContent, buttons: [...$("pcal-popover").querySelectorAll("button")].map((b) => b.textContent)};
  const ghostPositions = events().map((e) => [e.closest(".pcal-col").dataset.date, e.style.top, e.style.height]);
  $("pcal-accept").click(); $("pcal-accept").click(); await sleep(60);
  out.accepting = {disabled: $("pcal-accept").disabled, text: $("pcal-accept").textContent};
  await sleep(900);
  out.accepted = {confirms: P.confirmBodies.length, ...toolbar(), events: events().map(describeEvent),
    samePositions: JSON.stringify(events().map((e) => [e.closest(".pcal-col").dataset.date, e.style.top, e.style.height])) === JSON.stringify(ghostPositions),
    workspace: visible($("planner-workspace")), queue: queue()};
  out.overflow.accepted = noOverflow();

  // 10. A confirmed session's popover offers the existing lifecycle action.
  events()[0].click(); await sleep(50);
  out.confirmedPopover = [...$("pcal-popover").querySelectorAll("button")].map((b) => b.textContent);
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(50);

  // 11. Reload: opens into the live calendar (this week), no preview is requested, next week is solid.
  const previewsBeforeReload = P.previewBodies.length;
  await loadPlannerData(); await sleep(400);
  out.reloaded = {range: $("pcal-range").textContent, wizardHidden: document.querySelector(".planner-shell").hidden, ...toolbar()};
  $("pcal-next").click(); await sleep(100);
  out.reloaded.events = events().map((e) => e.dataset.kind);
  out.reloaded.previews = P.previewBodies.length - previewsBeforeReload;
  out.reloaded.newPlanShown = visible($("pcal-new-plan"));

  out.text = $("planner-workspace").textContent;
  out.calls = P.calls;
  out.previewBodies = P.previewBodies;
  out.confirmBodies = P.confirmBodies;
  out.expectedOffset = -new Date().getTimezoneOffset();
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

FRAME_HOST = """<!doctype html><html><body style="margin:0">
<iframe src="index.html" style="width:{width}px;height:{height}px;border:0"></iframe>
<script>window.addEventListener("message", (event) => {{ const pre = document.createElement("pre"); pre.id = "harness-out";
pre.textContent = event.data; document.body.appendChild(pre); }});</script></body></html>"""


def run_at_width(width, height, mock=MOCK, driver=DRIVER):
    """1280px: the page itself (the headless window size). Other widths: the page inside an iframe."""
    work = Path(tempfile.mkdtemp(prefix=f"calendar_{width}_"))
    try:
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copy(FRONTEND / name, work / name)
        (work / "app-config.js").write_text(mock, encoding="utf-8")
        (work / "driver.js").write_text(driver, encoding="utf-8")
        page = (work / "index.html").read_text(encoding="utf-8").replace(
            '<script src="app.js"></script>', '<script src="app.js"></script><script src="driver.js"></script>')
        (work / "index.html").write_text(page, encoding="utf-8")
        entry = work / "index.html"
        if width != 1280:
            entry = work / "frame.html"
            entry.write_text(FRAME_HOST.format(width=width, height=height), encoding="utf-8")
        result = subprocess.run(
            [find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=60000",
             "--window-size=1280,900", "--dump-dom", f"--user-data-dir={work / 'profile'}", entry.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        match = re.search(r'<pre id="harness-out">(.*?)</pre>', result.stdout, re.S)
        if not match:
            raise AssertionError("the page produced no result: " + result.stderr[-1500:])
        return json.loads(html.unescape(match.group(1)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


class CalendarPlannerAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertTrue(self.out["desktop"])

    def test_first_visit_opens_the_current_week_without_a_wizard(self):
        first = self.out["first"]
        self.assertTrue(first["wizardHidden"])
        self.assertTrue(first["workspace"])
        self.assertFalse(first["stepsVisible"])
        self.assertTrue(first["topbarHidden"])
        self.assertEqual(first["columns"], [f"2026-09-{day}" for day in range(21, 28)])
        self.assertEqual(first["dayHeads"], ["Mon21", "Tue22", "Wed23", "Thu24", "Fri25", "Sat26", "Sun27"])
        self.assertEqual(first["today"], "Mon21")
        self.assertRegex(first["range"], r"^21\D.*27, 2026$")
        self.assertEqual((first["nowLine"], first["nowTop"]), (True, "384px"))   # 08:00 on a 48px/hour grid
        self.assertEqual(first["hours"], 23)
        self.assertEqual(first["stickyHead"], "sticky")
        self.assertTrue(first["innerScroll"])
        self.assertEqual(first["scrolledTo"], 7 * 48)   # opens around the morning
        self.assertEqual(first["notice"], "Add the materials you want to study.Add materials")
        self.assertEqual((first["status"], first["acceptShown"], first["autoPlanShown"]), ("", False, False))
        self.assertEqual(first["plans"], 0)   # nothing is created just by opening the planner

    def test_add_materials_with_inline_deadline_without_leaving(self):
        sheet = self.out["sheet"]
        self.assertTrue(sheet["open"])
        self.assertEqual(sheet["rows"], ["Marketing", "Statistics", "PowerBI"])
        self.assertEqual(sheet["rowsAfter"], ["PowerBI"])
        self.assertTrue(sheet["closed"])
        materials = self.out["materials"]
        self.assertEqual(materials["saved"], [["mkt.pdf", "2026-10-01"], ["stats.pdf", None]])
        self.assertEqual(materials["plans"], 1)
        self.assertEqual(materials["rail"], [{"title": "Marketing", "pill": "New", "deadline": "2026-10-01"},
                                             {"title": "Statistics", "pill": "New", "deadline": ""}])
        # Materials but no availability: prompt to drag, no preview yet.
        self.assertEqual(materials["notice"], "Drag on the calendar to mark when you could study.")
        self.assertEqual(materials["previews"], 0)
        self.assertEqual(materials["queueEmpty"], "Mark some free time to see what fits.")

    def test_drag_creates_weekly_availability_with_the_existing_api(self):
        self.assertEqual(self.out["firstSlot"]["saved"], [[0, "18:00", "18:30", True]])
        self.assertEqual(self.out["dragged"], [[0, "18:00", "18:30", True], [0, "18:30", "22:00", True]])

    def test_auto_preview_after_enough_input(self):
        at_risk = self.out["atRisk"]
        self.assertEqual(at_risk["previews"], 1)
        self.assertEqual(at_risk["status"], "1 suggested session · 45m · 1h 15m doesn’t fit")
        self.assertIsNone(at_risk["notice"])
        # the one suggestion that fits is on the calendar; only what could not be placed is queued
        self.assertEqual([(q["title"], q["meta"]) for q in at_risk["queue"]["items"]],
                         [("Statistics", "Summary · 45m"), ("Marketing", "Quiz · 30m")])
        on_track = self.out["onTrack"]
        self.assertEqual(on_track["previews"], 2)   # one debounced preview per change
        self.assertEqual(on_track["status"], "3 suggested sessions · 2h")
        self.assertTrue(on_track["acceptShown"])
        self.assertTrue(on_track["autoPlanShown"])
        self.assertEqual(on_track["thisWeekEvents"], 0)   # the sessions land next week

    def test_study_queue_holds_only_unscheduled_work(self):
        at_risk = self.out["atRisk"]["queue"]
        self.assertEqual(at_risk["items"][0], {"title": "Statistics", "meta": "Summary · 45m",
                                               "reason": 'Start "Statistics": read the summary.', "need": "Needs a time slot",
                                               "buttons": []})
        self.assertEqual(at_risk["sessionCards"], 0)
        # everything fits: the suggestions are on the calendar and the queue is quietly empty
        on_track = self.out["onTrack"]["queue"]
        self.assertEqual(on_track["items"], [])
        self.assertEqual(on_track["empty"], "All caught upEverything that needs attention is already on your calendar.")
        for state in (at_risk, on_track, self.out["accepted"]["queue"]):
            self.assertEqual(state["buttonCount"], 0)   # no Start / Resume / Complete / Skip in the queue
            self.assertNotRegex(state["text"], r"sessions done|studied|still planned|quiz performance")

    def test_ghost_sessions_render_on_the_right_day_and_time(self):
        next_week = self.out["nextWeek"]
        self.assertRegex(next_week["range"], r"^28\D.*\b4\b.*2026$")
        ghosts = next_week["events"]
        self.assertEqual([(e["date"], e["kind"], e["title"], e["meta"]) for e in ghosts], [
            ("2026-09-28", "suggested", "Marketing", "Summary · 18:00–18:45"),
            ("2026-09-28", "suggested", "Statistics", "Summary · 18:55–19:40"),
            ("2026-09-29", "suggested", "Marketing", "Quiz · 20:00–20:30")])
        self.assertEqual((ghosts[0]["top"], ghosts[0]["height"]), ("864px", "34px"))   # 18:00, 45 minutes
        self.assertEqual(ghosts[1]["top"], "908px")
        self.assertEqual(ghosts[2]["top"], "960px")
        self.assertTrue(all(e["border"] == "dashed" for e in ghosts))

    def test_availability_is_visually_distinct_and_larger_than_the_study(self):
        blocks = self.out["nextWeek"]["availability"]
        self.assertEqual(len(blocks), 2)   # the recurring Monday slots repeat next week
        window = blocks[1]
        self.assertEqual((window["top"], window["height"]), ("888px", "166px"))   # 18:30-22:00
        self.assertIn("Available", window["text"])
        self.assertIn("3h 30m", window["text"])
        ghosts = self.out["nextWeek"]["events"]
        self.assertNotEqual(window["background"], ghosts[0]["background"])
        self.assertLess(window["z"], ghosts[0]["z"])   # sessions sit on top of the availability they use

    def test_deadline_markers_and_inline_deadline_edit(self):
        self.assertEqual(self.out["nextWeek"]["markers"], [["2026-10-01", ["Marketing"]]])
        edit = self.out["deadlineEdit"]
        self.assertEqual(edit["saved"], [["mkt.pdf", "2026-10-01"], ["stats.pdf", "2026-10-02"]])
        self.assertEqual(edit["previews"], 1)   # two quick edits -> one refreshed preview
        self.assertEqual(edit["markers"], [["2026-10-01", ["Marketing"]], ["2026-10-02", ["Statistics"]]])

    def test_availability_popover_repeat_weekly_and_delete(self):
        self.assertRegex(self.out["todayButton"], r"^21\D.*27, 2026$")
        popover = self.out["popover"]
        self.assertTrue(popover["open"])
        self.assertIn("09:00–10:00", popover["text"])
        self.assertIn("Repeat weekly", popover["text"])
        self.assertTrue(popover["repeat"])
        self.assertTrue(popover["hasDelete"])
        deleted = self.out["deleted"]
        self.assertEqual(deleted["availability"], [[0, "18:00", "18:30"], [0, "18:30", "22:00"]])
        self.assertTrue(deleted["popoverClosed"])
        self.assertEqual(deleted["wednesdayBlocks"], 0)
        self.assertEqual(deleted["previews"], 1)
        self.assertEqual(self.out["autoPlan"], 1)

    def test_ghost_popover_is_read_only(self):
        ghost = self.out["ghostPopover"]
        self.assertTrue(ghost["open"])
        self.assertIn("Suggested", ghost["text"])
        self.assertIn("Why this session? The deadline is coming up.", ghost["text"])
        self.assertEqual(ghost["buttons"], ["×"])

    def test_accept_confirms_once_and_ghosts_become_solid_in_place(self):
        self.assertEqual(self.out["accepting"], {"disabled": True, "text": "Saving…"})
        accepted = self.out["accepted"]
        self.assertEqual(accepted["confirms"], 1)
        self.assertTrue(accepted["workspace"])
        self.assertRegex(accepted["range"], r"^28\D.*\b4\b.*2026$")   # same screen, same week
        self.assertEqual([e["kind"] for e in accepted["events"]], ["confirmed"] * 3)
        self.assertTrue(all(e["border"] != "dashed" for e in accepted["events"]))
        self.assertTrue(accepted["samePositions"])
        self.assertEqual(accepted["status"], "3 planned sessions this week · 2h")
        self.assertFalse(accepted["acceptShown"])
        self.assertFalse(accepted["autoPlanShown"])
        self.assertEqual((accepted["queue"]["items"], accepted["queue"]["sessionCards"]), ([], 0))   # confirmed sessions live on the calendar
        self.assertEqual(self.out["confirmedPopover"], ["×", "Start", "Reschedule", "Skip"])   # Start primary; no Complete before starting

    def test_reload_opens_the_confirmed_calendar(self):
        reloaded = self.out["reloaded"]
        self.assertTrue(reloaded["wizardHidden"])
        self.assertRegex(reloaded["range"], r"^21\D.*27, 2026$")
        self.assertEqual(reloaded["status"], "No sessions this week")
        self.assertEqual(reloaded["events"], ["confirmed"] * 3)
        self.assertEqual(reloaded["previews"], 0)
        self.assertFalse(reloaded["acceptShown"])
        self.assertTrue(reloaded["newPlanShown"])

    def test_sends_only_the_learner_utc_offset(self):
        self.assertGreaterEqual(len(self.out["previewBodies"]), 5)
        for body in self.out["previewBodies"] + self.out["confirmBodies"]:
            self.assertEqual(body, {"utc_offset_minutes": self.out["expectedOffset"]})

    def test_no_internal_codes_or_legacy_endpoints(self):
        self.assertNotRegex(self.out["text"], r"deadline_approaching|new_material|no_availability|priority|score")
        for call in self.out["calls"]:
            self.assertNotRegex(call, r"/api/planner/(tasks|blocks|topic-progress)")

    def test_no_page_overflow(self):
        self.assertTrue(self.out["overflow"])
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarPlannerDesktopTests(CalendarPlannerAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarPlannerNarrowDesktopTests(CalendarPlannerAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800)

    def test_runs_at_narrow_desktop_width(self):
        self.assertEqual(self.out["width"], 1100)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class TabletKeepsStepFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(900, 800)

    def test_tablet_keeps_the_step_flow(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertEqual(self.out["tablet"], {"wizardShown": True, "workspaceHidden": True, "steps": 4})


if __name__ == "__main__":
    unittest.main()
