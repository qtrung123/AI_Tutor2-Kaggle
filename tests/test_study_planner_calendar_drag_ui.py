"""Real-browser tests for desktop direct manipulation on the Study Planner calendar (Phase 7B;
headless Chrome, real pointer events, mocked API that mirrors the server rules):

- a suggested (ghost) session dragged to a new time becomes a placement the preview applies and
  Accept sends; an invalid target sends nothing;
- an unscheduled queue item dragged onto an available slot is placed the same way;
- a stale placement makes Accept save nothing, then the refreshed plan confirms;
- a confirmed session dragged to a new time is rescheduled exactly once (rescheduled_from kept);
  an invalid drop -- caught on the page or refused by the server -- writes nothing;
- a confirmed plan's still-wanted activity is dragged from the queue into a free slot;
- reload shows the resulting schedule.

"Now" is pinned to Thu 2026-09-24 12:00; the planned week is Mon 28 Sep - Sun 4 Oct.
Runs at 1280px and 1100px. Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width
from tests.test_study_planner_v2_ui import MOCK as PLANNER_MOCK

# Placement-aware preview/confirm, exact-target reschedule and confirmed-plan candidates. The same
# rules as the server: 15-minute starts, inside availability, no overlap; unknown keys are stale.
MOCK = PLANNER_MOCK + r"""
Object.assign(window.__planner, {extraUnscheduled: [], staleKeys: [], rescheduleCalls: [], placeCalls: [], liveCandidates: [],
  serverRefuses: null});
const TITLES = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"};
const pad = (n) => String(n).padStart(2, "0");
const minutesOf = (iso) => Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
const isoAt = (day, minute) => `${day}T${pad(Math.floor(minute / 60))}:${pad(minute % 60)}:00`;
const weekdayOf = (day) => (new Date(day + "T12:00:00").getDay() + 6) % 7;
function slotProblem(start, minutes, others) {
  if (!/T\d\d:(00|15|30|45):00$/.test(start)) return "invalid_time";
  const day = start.slice(0, 10), from = minutesOf(start), to = from + minutes;
  const inside = window.__planner.availability.some((slot) => (slot.is_recurring ? slot.day_of_week === weekdayOf(day) : slot.date === day)
    && minutesOf("xxxxxxxxxxT" + slot.start_at) <= from && to <= minutesOf("xxxxxxxxxxT" + slot.end_at));
  if (!inside) return "outside_availability";
  const end = isoAt(day, to);
  if (others.some((s) => s.scheduled_start < end && s.scheduled_end > start)) return "slot_taken";
  return null;
}
const MESSAGES = {invalid_time: "Pick a time on the calendar grid.", outside_availability: "Pick a time inside your available hours.",
  slot_taken: "That time is already taken by another session.", candidate_stale: "This suggestion changed. The calendar has been refreshed."};
function placedSchedule(placements) {
  const P = window.__planner;
  const base = plannerSchedule();
  const counts = {};
  const key = (item) => { const k = item.document_id + "|" + item.activity_type; counts[k] = (counts[k] || 0) + 1; return k + "|" + (counts[k] - 1); };
  const entries = base.sessions.map((s) => ({...s, candidate_key: key(s), placed: false}));
  const waiting = [...base.capacity.unscheduled, ...P.extraUnscheduled].map((c) => ({...c, candidate_key: key(c)}));
  const rejected = [];
  (placements || []).forEach((placement) => {
    const k = placement.candidate_key;
    const entry = entries.find((e) => e.candidate_key === k);
    const wait = waiting.find((c) => c.candidate_key === k);
    if ((!entry && !wait) || P.staleKeys.includes(k)) { rejected.push({candidate_key: k, code: "candidate_stale", message: MESSAGES.candidate_stale}); return; }
    const minutes = entry ? entry.duration_minutes : wait.estimated_minutes;
    const problem = slotProblem(placement.scheduled_start, minutes, entries.filter((e) => e !== entry));
    if (problem) { rejected.push({candidate_key: k, code: problem, message: MESSAGES[problem]}); return; }
    const end = isoAt(placement.scheduled_start.slice(0, 10), minutesOf(placement.scheduled_start) + minutes);
    if (entry) Object.assign(entry, {scheduled_start: placement.scheduled_start, scheduled_end: end, placed: true});
    else {
      waiting.splice(waiting.indexOf(wait), 1);
      entries.push({document_id: wait.document_id, document_title: TITLES[wait.document_id], activity_type: wait.activity_type,
        scheduled_start: placement.scheduled_start, scheduled_end: end, duration_minutes: minutes, reason: wait.reason,
        artifact_id: null, candidate_key: k, placed: true});
    }
  });
  entries.sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
  const scheduled = entries.reduce((sum, e) => sum + e.duration_minutes, 0);
  const shortfall = waiting.reduce((sum, c) => sum + c.estimated_minutes, 0);
  return {sessions: entries, capacity: {...base.capacity, scheduled_minutes: scheduled, shortfall_minutes: shortfall,
    required_minutes: scheduled + shortfall, status: shortfall ? "at_risk" : "on_track", unscheduled: waiting},
    placements: {applied: entries.filter((e) => e.placed).map((e) => e.candidate_key), rejected}, warnings: []};
}
const v2Fetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const url = typeof input === "string" ? input : input.url;
  const p = new URL(url, "http://x").pathname;
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  let m = p.match(/^\/api\/planner\/plans\/([^/]+)\/(preview|confirm)$/);
  if (m) {
    P.calls.push(method + " " + p);
    (m[2] === "preview" ? P.previewBodies : P.confirmBodies).push(body);
    const result = placedSchedule(body.placements);
    if (m[2] === "preview") return json({plan_id: m[1], persisted: false, ...result});
    if (P.sessions.some((s) => s.status === "scheduled")) return json({detail: "This plan is already confirmed."}, 409);
    if (result.placements.rejected.length) {
      return json({detail: {code: "placement_rejected", message: result.placements.rejected[0].message, placements: result.placements.rejected}}, 409);
    }
    P.sessions = result.sessions.map((s, i) => ({...s, session_id: "s" + i, status: "scheduled", rescheduled_from: null}));
    return json({plan_id: m[1], persisted: true, sessions: P.sessions, capacity: result.capacity, warnings: []}, 201);
  }
  m = p.match(/^\/api\/planner\/sessions\/([^/]+)\/reschedule$/);
  if (m) {
    P.calls.push(method + " " + p);
    P.rescheduleCalls.push({id: m[1], body});
    const session = P.sessions.find((s) => s.session_id === m[1]);
    const problem = P.serverRefuses || slotProblem(body.target_start, session.duration_minutes,
      P.sessions.filter((s) => s !== session && ["scheduled", "in_progress", "completed"].includes(s.status)));
    if (problem) return json({detail: {code: problem, message: MESSAGES[problem], status: session.status}}, 409);
    session.status = "rescheduled";
    const moved = {...session, session_id: session.session_id + "-m", status: "scheduled", rescheduled_from: session.session_id,
      scheduled_start: body.target_start, scheduled_end: isoAt(body.target_start.slice(0, 10), minutesOf(body.target_start) + session.duration_minutes)};
    P.sessions.push(moved);
    return json({session: moved, previous: {...session}, after_deadline: false});
  }
  m = p.match(/^\/api\/planner\/plans\/([^/]+)\/candidates(\/place)?$/);
  if (m && !m[2]) { P.calls.push(method + " " + p); return json(P.liveCandidates); }
  if (m && m[2]) {
    P.calls.push(method + " " + p);
    P.placeCalls.push(body);
    const candidate = P.liveCandidates.find((c) => c.candidate_key === body.candidate_key);
    if (!candidate) return json({detail: {code: "candidate_stale", message: MESSAGES.candidate_stale, status: ""}}, 409);
    const problem = slotProblem(body.scheduled_start, candidate.estimated_minutes, P.sessions.filter((s) => s.status === "scheduled"));
    if (problem) return json({detail: {code: problem, message: MESSAGES[problem], status: ""}}, 409);
    const session = {session_id: "placed-" + P.placeCalls.length, document_id: candidate.document_id, document_title: candidate.document_title,
      activity_type: candidate.activity_type, scheduled_start: body.scheduled_start,
      scheduled_end: isoAt(body.scheduled_start.slice(0, 10), minutesOf(body.scheduled_start) + candidate.estimated_minutes),
      duration_minutes: candidate.estimated_minutes, status: "scheduled", reason: {code: "new_material", message: "New material to learn"},
      artifact_id: null, rescheduled_from: null};
    P.sessions.push(session);
    P.liveCandidates = P.liveCandidates.filter((c) => c !== candidate);
    return json({session}, 201);
  }
  return v2Fetch(input, init);
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
const $ = (id) => document.getElementById(id);
const P = window.__planner;
const column = (date) => document.querySelector(`#pcal-body .pcal-col[data-date="${date}"]`);
const events = () => [...document.querySelectorAll("#pcal-body .pcal-event")].sort((a, b) => a.dataset.start.localeCompare(b.dataset.start));
const describe = () => events().map((e) => [e.closest(".pcal-col").dataset.date, e.dataset.kind, e.querySelector(".pcal-event-title").textContent,
  e.querySelector(".pcal-event-meta").textContent]);
const eventOf = (title, activity) => events().find((e) => e.querySelector(".pcal-event-title").textContent === title
  && e.querySelector(".pcal-event-meta").textContent.startsWith(activity));
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("planner-workspace").getBoundingClientRect().bottom <= window.innerHeight + 1;
const show = (minute) => { $("pcal-scroll").scrollTop = Math.max(0, (minute - 60) * 0.8); };
// A real pointer drag: press 4px below the element's top (5 grid minutes), move in steps, release.
const drag = async (element, date, minute, {inspect} = {}) => {
  const box = element.getBoundingClientRect();
  const from = {x: box.left + Math.min(20, box.width / 2), y: box.top + 4};
  element.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, cancelable: true, button: 0, clientX: from.x, clientY: from.y}));
  const col = column(date).getBoundingClientRect();
  const to = {x: col.left + col.width / 2, y: col.top + (minute + (element.classList.contains("pcal-event") ? 5 : 0)) * 0.8 + 1};
  for (let step = 1; step <= 6; step += 1) {
    document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, clientX: from.x + (to.x - from.x) * step / 6, clientY: from.y + (to.y - from.y) * step / 6}));
  }
  const during = inspect ? {moving: $("planner-workspace").classList.contains("is-moving"), preview: !!$("pcal-drop") && !$("pcal-drop").hidden,
    invalid: $("pcal-drop")?.classList.contains("is-invalid") || false, time: $("pcal-drop")?.querySelector(".pcal-drop-time").textContent || null,
    top: $("pcal-drop") ? parseFloat($("pcal-drop").style.top) - $("pcal-body").offsetTop : null, cursor: getComputedStyle(column(date)).cursor} : null;
  document.dispatchEvent(new PointerEvent("pointerup", {bubbles: true, clientX: to.x, clientY: to.y}));
  element.dispatchEvent(new MouseEvent("click", {bubbles: true}));   // the click that follows a drag must not open a popover
  await sleep(50);
  return during;
};
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: "2026-10-01", learning_state: "new"},
                 {material_id: "m-stats", plan_id: "plan-1", document_id: "stats.pdf", deadline: null, learning_state: "new"}];
  P.availability = [0, 1, 2].map((day) => ({availability_id: "a" + day, start_at: "18:00", end_at: "22:00", is_recurring: true, day_of_week: day, date: null}));
  P.extraUnscheduled = [{document_id: "mkt.pdf", activity_type: "review", estimated_minutes: 15, deadline: "2026-10-01",
    reason: {code: "final_review", message: "Final review of \"Marketing\" before 2026-10-01."}, artifact_id: null}];
  setPage("planner"); await sleep(900);
  $("pcal-next").click(); await sleep(100);
  show(18 * 60);
  out.ghosts = describe();

  // 1. Ghost: drag Marketing summary (Mon 18:00) to Wed 30 20:00 -> preview re-runs with that placement.
  const previews = P.previewBodies.length;
  out.ghostDuring = await drag(eventOf("Marketing", "Summary"), "2026-09-30", 20 * 60, {inspect: true});
  await sleep(300);
  out.ghostMoved = {previews: P.previewBodies.length - previews, placements: P.previewBodies[P.previewBodies.length - 1].placements,
    events: describe(), popoverOpen: !$("pcal-popover").hidden, moving: $("planner-workspace").classList.contains("is-moving"),
    dropLeft: !!$("pcal-drop")};

  // 2. Invalid ghost target (Thu 1 Oct has no availability): muted preview, no request, calm message.
  const beforeInvalid = P.previewBodies.length;
  out.ghostInvalidDuring = await drag(eventOf("Statistics", "Summary"), "2026-10-01", 19 * 60, {inspect: true});
  await sleep(300);
  out.ghostInvalid = {previews: P.previewBodies.length - beforeInvalid, toast: toast.textContent, events: describe()};

  // 3. Queue -> calendar before Accept: the unscheduled final review onto Tue 29 18:00.
  const queued = () => [...document.querySelectorAll("#pcal-queue .pcal-queue-item--unscheduled")];
  out.queueBefore = queued().map((li) => [li.textContent.includes("Review"), li.classList.contains("is-draggable"), li.dataset.candidateKey]);
  out.queueDuring = await drag(queued()[0], "2026-09-29", 18 * 60, {inspect: true});
  await sleep(300);
  out.queuePlaced = {placements: P.previewBodies[P.previewBodies.length - 1].placements, events: describe(), queue: queued().length,
    status: $("pcal-status").textContent};

  // 4. Accept with a placement that went stale meanwhile: nothing is saved; the rest confirms next.
  P.staleKeys = ["mkt.pdf|review|0"];
  $("pcal-accept").click(); await sleep(500);
  out.staleAccept = {confirms: P.confirmBodies.length, saved: P.sessions.length, toast: toast.textContent,
    placementsSent: P.confirmBodies[0].placements, placementsLeft: plannerPlacements.map((p) => p.candidate_key)};
  P.staleKeys = [];
  await sleep(300);
  $("pcal-accept").click(); await sleep(600);
  out.accepted = {confirms: P.confirmBodies.length, body: P.confirmBodies[P.confirmBodies.length - 1], events: describe(),
    saved: P.sessions.map((s) => [s.document_id, s.activity_type, s.scheduled_start])};
  out.overflow.accepted = noOverflow();

  // 5. Confirmed session: drag Marketing summary (Wed 30 20:00) to Mon 28 20:00 -> one reschedule, lineage kept.
  show(18 * 60);
  const moving = eventOf("Marketing", "Summary");
  const originalId = moving.dataset.sessionId;
  out.confirmedDuring = await drag(moving, "2026-09-28", 20 * 60, {inspect: true});
  await sleep(300);
  const moved = P.sessions.find((s) => s.rescheduled_from === originalId);
  out.rescheduled = {calls: P.rescheduleCalls.length, body: P.rescheduleCalls[0]?.body, originalStatus: P.sessions.find((s) => s.session_id === originalId).status,
    moved: moved && [moved.scheduled_start, moved.status], events: describe(), range: $("pcal-range").textContent};

  // 6. Invalid confirmed drops: onto another session (caught on the page) and a server refusal. Zero writes.
  const snapshot = JSON.stringify(P.sessions);
  const quiz = eventOf("Marketing", "Quiz");
  await drag(eventOf("Statistics", "Summary"), quiz.closest(".pcal-col").dataset.date, 20 * 60);
  await sleep(300);
  out.pageRefused = {calls: P.rescheduleCalls.length, toast: toast.textContent, unchanged: JSON.stringify(P.sessions) === snapshot};
  P.serverRefuses = "slot_taken";
  await drag(eventOf("Statistics", "Summary"), "2026-09-30", 21 * 60);
  await sleep(400);
  P.serverRefuses = null;
  out.serverRefused = {calls: P.rescheduleCalls.length, toast: toast.textContent, unchanged: JSON.stringify(P.sessions) === snapshot,
    stillThere: describe().some((e) => e[0] === "2026-09-28" && e[2] === "Statistics")};

  // 7. Confirmed plan: a still-wanted activity from the queue onto Wed 30 18:00.
  P.liveCandidates = [{candidate_key: "stats.pdf|quiz|0", document_id: "stats.pdf", document_title: "Statistics", activity_type: "quiz",
    estimated_minutes: 30, deadline: null, reason: {code: "new_material", message: "Start \"Statistics\": take a quiz."}, artifact_id: null}];
  await loadPlannerData({keepWeek: true}); await sleep(400);
  show(18 * 60);
  out.liveQueue = queued().map((li) => [li.dataset.candidateKey, li.classList.contains("is-draggable")]);
  await drag(queued()[0], "2026-09-30", 18 * 60);
  await sleep(500);
  out.livePlaced = {calls: P.placeCalls, events: describe(), queue: queued().length};

  // 8. Reload: the resulting schedule, solid, with the moved session in its new place.
  await loadPlannerData(); await sleep(400);
  $("pcal-next").click(); await sleep(100);
  out.reloaded = describe();
  out.overflow.reloaded = noOverflow();
  out.popoverStillWorks = (() => { events()[0].click(); return !$("pcal-popover").hidden; })();
  out.calls = P.calls;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class CalendarDragAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_ghost_drag_shows_a_snapped_preview_and_becomes_a_placement(self):
        self.assertIn(["2026-09-28", "suggested", "Marketing", "Summary · 18:00–18:45"], self.out["ghosts"])
        during = self.out["ghostDuring"]
        self.assertEqual((during["moving"], during["preview"], during["invalid"], during["time"], during["top"]),
                         (True, True, False, "20:00–20:45", 960))   # 20:00, relative to the grid
        self.assertEqual(during["cursor"], "grabbing")
        moved = self.out["ghostMoved"]
        self.assertEqual(moved["previews"], 1)
        self.assertEqual(moved["placements"], [{"candidate_key": "mkt.pdf|summary|0", "scheduled_start": "2026-09-30T20:00:00"}])
        self.assertIn(["2026-09-30", "suggested", "Marketing", "Summary · 20:00–20:45"], moved["events"])
        self.assertNotIn(["2026-09-28", "suggested", "Marketing", "Summary · 18:00–18:45"], moved["events"])
        self.assertFalse(moved["popoverOpen"])
        self.assertEqual((moved["moving"], moved["dropLeft"]), (False, False))

    def test_invalid_ghost_target_sends_nothing(self):
        self.assertTrue(self.out["ghostInvalidDuring"]["invalid"])
        invalid = self.out["ghostInvalid"]
        self.assertEqual(invalid["previews"], 0)
        self.assertEqual(invalid["toast"], "Pick a time inside your available hours.")
        self.assertEqual(invalid["events"], self.out["ghostMoved"]["events"])

    def test_queue_item_is_placed_into_a_valid_slot(self):
        self.assertEqual(self.out["queueBefore"], [[True, True, "mkt.pdf|review|0"]])
        self.assertFalse(self.out["queueDuring"]["invalid"])
        placed = self.out["queuePlaced"]
        self.assertEqual(placed["placements"], [
            {"candidate_key": "mkt.pdf|summary|0", "scheduled_start": "2026-09-30T20:00:00"},
            {"candidate_key": "mkt.pdf|review|0", "scheduled_start": "2026-09-29T18:00:00"}])
        self.assertIn(["2026-09-29", "suggested", "Marketing", "Review · 18:00–18:15"], placed["events"])
        self.assertEqual(placed["queue"], 0)

    def test_stale_placement_saves_nothing_then_the_move_survives_accept(self):
        stale = self.out["staleAccept"]
        self.assertEqual((stale["confirms"], stale["saved"]), (1, 0))
        self.assertEqual(stale["toast"], "This suggestion changed. The calendar has been refreshed.")
        self.assertEqual(len(stale["placementsSent"]), 2)
        self.assertEqual(stale["placementsLeft"], ["mkt.pdf|summary|0"])
        accepted = self.out["accepted"]
        self.assertEqual(accepted["confirms"], 2)
        self.assertEqual(accepted["body"]["placements"], [{"candidate_key": "mkt.pdf|summary|0", "scheduled_start": "2026-09-30T20:00:00"}])
        self.assertIn(["mkt.pdf", "summary", "2026-09-30T20:00:00"], accepted["saved"])
        self.assertIn(["2026-09-30", "confirmed", "Marketing", "Summary · 20:00–20:45"], accepted["events"])

    def test_confirmed_drag_reschedules_exactly_once_with_lineage(self):
        self.assertFalse(self.out["confirmedDuring"]["invalid"])
        rescheduled = self.out["rescheduled"]
        self.assertEqual(rescheduled["calls"], 1)
        self.assertEqual(rescheduled["body"], {"utc_offset_minutes": rescheduled["body"]["utc_offset_minutes"],
                                               "target_start": "2026-09-28T20:00:00"})
        self.assertEqual(rescheduled["originalStatus"], "rescheduled")
        self.assertEqual(rescheduled["moved"], ["2026-09-28T20:00:00", "scheduled"])
        self.assertIn(["2026-09-28", "confirmed", "Marketing", "Summary · 20:00–20:45"], rescheduled["events"])
        self.assertFalse(any(e[0] == "2026-09-30" and e[2] == "Marketing" and e[3].startswith("Summary") for e in rescheduled["events"]))
        self.assertRegex(rescheduled["range"], r"^28\D")   # updated in place, same week

    def test_invalid_confirmed_drops_write_nothing(self):
        page = self.out["pageRefused"]
        self.assertEqual((page["calls"], page["unchanged"]), (1, True))   # still only the one valid reschedule
        self.assertEqual(page["toast"], "That time is already taken by another session.")
        server = self.out["serverRefused"]
        self.assertEqual((server["calls"], server["unchanged"], server["stillThere"]), (2, True, True))
        self.assertEqual(server["toast"], "That time is already taken by another session.")

    def test_confirmed_plan_candidate_from_queue(self):
        self.assertEqual(self.out["liveQueue"], [["stats.pdf|quiz|0", True]])
        placed = self.out["livePlaced"]
        self.assertEqual(len(placed["calls"]), 1)
        self.assertEqual({k: v for k, v in placed["calls"][0].items() if k != "utc_offset_minutes"},
                         {"candidate_key": "stats.pdf|quiz|0", "scheduled_start": "2026-09-30T18:00:00"})
        self.assertIn(["2026-09-30", "confirmed", "Statistics", "Quiz · 18:00–18:30"], placed["events"])
        self.assertEqual(placed["queue"], 0)

    def test_reload_preserves_the_resulting_schedule(self):
        reloaded = self.out["reloaded"]
        self.assertIn(["2026-09-28", "confirmed", "Marketing", "Summary · 20:00–20:45"], reloaded)
        self.assertIn(["2026-09-30", "confirmed", "Statistics", "Quiz · 18:00–18:30"], reloaded)
        self.assertIn(["2026-09-29", "confirmed", "Marketing", "Quiz · 20:00–20:30"], reloaded)
        self.assertFalse(any(e[0] == "2026-09-30" and e[3].startswith("Summary") for e in reloaded))
        self.assertTrue(all(e[1] == "confirmed" for e in reloaded))
        self.assertTrue(self.out["popoverStillWorks"])

    def test_no_page_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarDragDesktopTests(CalendarDragAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarDragNarrowDesktopTests(CalendarDragAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
