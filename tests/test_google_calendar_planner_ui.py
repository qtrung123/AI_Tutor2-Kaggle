"""Real-browser tests for the Planner's Google Calendar section (headless Chrome, mocked API from
tests/test_study_planner_v2_ui.py plus an in-page model of /api/integrations/google-calendar/*).

Desktop calendar (1280px), "now" pinned to Mon 2026-09-21 08:00 browser-local: Connect when
disconnected (a full-page redirect through the backend), the connected controls, read-only "Busy"
blocks that never show an event name and block placement, the "Calendar sync failed" indicator
and its retry, a written_quiz session that still starts from the calendar, and Disconnect.
Planner flows without Google are covered by the existing planner browser tests (the section stays
hidden when the status endpoint does not answer). Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width
from tests.test_study_planner_v2_ui import MOCK as PLANNER_MOCK

MOCK = PLANNER_MOCK + r"""
window.__google = {connected: false, avoid: true, sync: true, syncErrors: 0, syncOutcome: {status: "ok", errors: 0},
  busy: [], calls: [], bodies: [], startCalls: [], skipCalls: []};
const googleStatus = () => { const G = window.__google; return {configured: true, connected: G.connected, reconnect_required: false,
  calendar_id: G.connected ? "study-1@group.calendar.google.com" : null, calendar_name: G.connected ? "AI Tutor Study Plan" : null,
  connected_at: G.connected ? "2026-09-20T10:00:00+00:00" : null, avoid_conflicts: G.avoid, sync_sessions: G.sync,
  sync_errors: G.syncErrors, last_synced_at: null}; };
const plannerFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const G = window.__google;
  const url = new URL(typeof input === "string" ? input : input.url, "http://x");
  const method = (init.method || "GET").toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;
  const p = url.pathname;
  if (p.startsWith("/api/integrations/google-calendar")) {
    G.calls.push(method + " " + p + url.search);
    if (body) G.bodies.push([p, body]);
    if (p.endsWith("/status")) return json(googleStatus());
    if (p.endsWith("/busy")) return json({busy: G.avoid && G.connected ? G.busy : [], warning: null});
    if (p.endsWith("/settings")) { if ("avoid_conflicts" in body) G.avoid = body.avoid_conflicts; if ("sync_sessions" in body) G.sync = body.sync_sessions; return json(googleStatus()); }
    if (p.endsWith("/sync")) { G.syncErrors = 0; return json({sync: {status: "ok", created: 1, updated: 0, deleted: 1, unchanged: 0, errors: 0}, ...googleStatus()}); }
    if (p.endsWith("/disconnect")) { G.connected = false; return json({...googleStatus(), revoked: true}); }
  }
  let m = p.match(/^\/api\/planner\/sessions\/([^/]+)\/(start|skip)$/);
  if (m) {
    const session = window.__planner.sessions.find((s) => s.session_id === m[1]);
    (m[2] === "start" ? G.startCalls : G.skipCalls).push([m[1], body]);
    session.status = m[2] === "start" ? "in_progress" : "skipped";
    if (m[2] === "skip") { G.syncErrors = G.syncOutcome.status === "error" ? 1 : 0; return json({session: {...session}, changed: true, calendar_sync: G.syncOutcome}); }
    if (session.activity_type === "written_quiz") session.artifact_id = "fc-quiz";
    return json({session: {...session}, started: true, tool: "quiz", artifact_available: true, calendar_sync: G.syncOutcome});
  }
  if (/\/api\/planner\/plans\/[^/]+\/(candidates|progress)$/.test(p) || p.includes("/adaptation/")) return json([]);
  return plannerFetch(input, init);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const visible = (el) => !!el && el.offsetParent !== null && !el.closest("[hidden]");
const column = (date) => document.querySelector(`#pcal-body .pcal-col[data-date="${date}"]`);
const section = () => ({shown: visible($("pcal-gcal")), text: $("pcal-gcal").textContent,
  buttons: [...$("pcal-gcal").querySelectorAll("button")].map((b) => [b.id, b.textContent, b.disabled]),
  toggles: [...$("pcal-gcal").querySelectorAll("input[type=checkbox]")].map((i) => [i.id, i.checked, i.closest("label").textContent]),
  syncError: visible($("pcal-gcal-sync-error")) ? $("pcal-gcal-sync-error").textContent : null});
const busyBlocks = () => [...document.querySelectorAll("#pcal-body .pcal-busy")].map((b) => ({date: b.closest(".pcal-col").dataset.date,
  text: b.textContent, start: b.dataset.start, end: b.dataset.end, top: b.style.top, height: b.style.height, tag: b.tagName,
  role: b.getAttribute("role"), tabIndex: b.tabIndex, draggable: b.draggable, pointerEvents: getComputedStyle(b).pointerEvents,
  background: getComputedStyle(b).backgroundImage, z: Number(getComputedStyle(b).zIndex), label: b.getAttribute("aria-label")}));
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 21, 8, 0, 0);   // Mon 21 Sep 2026, 08:00
  const G = window.__google;
  const P = window.__planner;
  let navigatedTo = null;
  plannerGoogleNavigate = (url) => { navigatedTo = url; };

  // A confirmed plan: a written_quiz session this morning and a quiz on Wednesday; Monday 12:00-16:00 available.
  P.plans = [{plan_id: "plan-1", title: "Exams", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null, familiarity: null, learning_state: "new"}];
  P.availability = [{availability_id: "a0", is_recurring: true, day_of_week: 0, start_at: "12:00", end_at: "16:00"}];
  P.sessions = [
    {session_id: "s-wq", document_id: "mkt.pdf", document_title: "Marketing", activity_type: "written_quiz", scheduled_start: "2026-09-21T09:00:00",
     scheduled_end: "2026-09-21T09:15:00", duration_minutes: 15, status: "scheduled", artifact_id: null,
     reason: {code: "retrieval_practice", message: "Scheduled to reinforce recall before your next assessment"}},
    {session_id: "s-quiz", document_id: "mkt.pdf", document_title: "Marketing", activity_type: "quiz", scheduled_start: "2026-09-23T10:00:00",
     scheduled_end: "2026-09-23T10:30:00", duration_minutes: 30, status: "scheduled", artifact_id: null,
     reason: {code: "new_material", message: "New material to learn"}}];
  G.busy = [{start: "2026-09-21T13:00:00", end: "2026-09-21T15:00:00"}, {start: "2026-09-23T23:00:00", end: "2026-09-24T01:00:00"}];

  // 1. Disconnected: a Connect button; nothing busy is drawn or fetched.
  setPage("planner"); await sleep(800);
  out.disconnected = {...section(), busy: busyBlocks(), busyCalls: G.calls.filter((c) => c.includes("/busy")).length,
    statusCall: G.calls.find((c) => c.includes("/status"))};
  $("pcal-gcal-connect").click();
  out.connectUrl = navigatedTo;

  // 2. Connected (as after the OAuth redirect back): the controls and this week's busy blocks.
  G.connected = true;
  await plannerLoadGoogleCalendar(); await sleep(500);
  out.connected = {...section(), busy: busyBlocks(), busyCall: G.calls.filter((c) => c.includes("/busy")).pop(),
    availabilityBackground: getComputedStyle(document.querySelector(".pcal-avail")).backgroundColor,
    eventBackground: getComputedStyle(document.querySelector(".pcal-event")).backgroundColor};
  const block = document.querySelector("#pcal-body .pcal-busy");
  const box = block.getBoundingClientRect();
  out.connected.hitTarget = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)?.className || null;
  out.placement = {inBusy: pcalCheckTarget("2026-09-21", 0, 13 * 60 + 30, {duration: 30, documentId: "mkt.pdf"}),
    overlapsBusyEdge: pcalCheckTarget("2026-09-21", 0, 12 * 60 + 45, {duration: 30, documentId: "mkt.pdf"}),
    free: pcalCheckTarget("2026-09-21", 0, 15 * 60, {duration: 30, documentId: "mkt.pdf"})};
  out.popoverAfterBusyClick = (() => { block.click(); return !$("pcal-popover").hidden; })();

  // Next week: its own busy request; the midnight-crossing block from Wednesday is not on it.
  $("pcal-next").click(); await sleep(400);
  out.nextWeek = {busy: busyBlocks(), busyCall: G.calls.filter((c) => c.includes("/busy")).pop()};
  $("pcal-today").click(); await sleep(400);

  // 3. A session action whose Google sync fails: the action still succeeds, a non-blocking indicator shows.
  G.syncOutcome = {status: "error", errors: 1};
  [...document.querySelectorAll("#pcal-body .pcal-event")].find((e) => e.dataset.sessionId === "s-quiz").click(); await sleep(100);
  [...$("pcal-popover").querySelectorAll("button")].find((b) => b.textContent === "Skip").click(); await sleep(900);
  out.syncFailed = {...section(), skipped: G.skipCalls.map((c) => c[0]), sessionStatus: P.sessions.find((s) => s.session_id === "s-quiz").status};
  // Retry: Sync now fixes it.
  G.syncOutcome = {status: "ok", errors: 0};
  $("pcal-gcal-sync-now").click(); await sleep(500);
  out.retried = {...section(), syncBody: G.bodies.filter(([p]) => p.endsWith("/sync")).pop()?.[1]};

  // 4. "Avoid conflicts" off: busy blocks disappear (and the setting is saved server-side).
  $("pcal-gcal-avoid").click(); await sleep(500);
  out.avoidOff = {busy: busyBlocks().length, body: G.bodies.filter(([p]) => p.endsWith("/settings")).pop()?.[1],
    free: pcalCheckTarget("2026-09-21", 0, 13 * 60 + 30, {duration: 30, documentId: "mkt.pdf"})};
  $("pcal-gcal-avoid").click(); await sleep(500);
  out.avoidOn = busyBlocks().length;

  // 5. The written_quiz session is still a normal, startable planner session.
  const wq = [...document.querySelectorAll("#pcal-body .pcal-event")].find((e) => e.dataset.sessionId === "s-wq");
  out.writtenQuiz = {classes: wq.className, meta: wq.querySelector(".pcal-event-meta").textContent};
  wq.click(); await sleep(100);
  out.writtenQuiz.buttons = [...$("pcal-popover").querySelectorAll("button")].map((b) => b.textContent);
  [...$("pcal-popover").querySelectorAll("button")].find((b) => b.textContent === "Start Session").click(); await sleep(1500);
  out.writtenQuiz.startCalls = G.startCalls;
  out.writtenQuiz.page = document.body.dataset.page;
  out.writtenQuiz.tab = document.body.dataset.sessionTab;
  out.writtenQuiz.document = typeof activeDocumentId === "undefined" ? null : activeDocumentId;

  // 6. Disconnect: back to Connect, no busy blocks.
  setPage("planner"); await sleep(800);
  $("pcal-gcal-disconnect").click(); await sleep(600);
  out.afterDisconnect = {...section(), busy: busyBlocks().length};
  out.calls = G.calls;
  out.pageText = document.body.textContent;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class GoogleCalendarPlannerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, mock=MOCK, driver=DRIVER)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out, self.out.get("fatal"))
        self.assertEqual(self.out["errors"], [])

    def test_connect_button_when_disconnected(self):
        state = self.out["disconnected"]
        self.assertTrue(state["shown"])
        self.assertIn("Google Calendar", state["text"])
        self.assertEqual(state["buttons"], [["pcal-gcal-connect", "Connect", False]])
        self.assertEqual((state["busy"], state["busyCalls"]), ([], 0))
        self.assertRegex(state["statusCall"], r"/status\?utc_offset_minutes=-?\d+$")
        # Connect is a full-page navigation to the backend, which redirects to Google.
        self.assertEqual(self.out["connectUrl"], "/api/integrations/google-calendar/connect")

    def test_connected_state(self):
        state = self.out["connected"]
        self.assertIn("✓ Connected", state["text"])
        self.assertEqual(state["toggles"], [["pcal-gcal-avoid", True, "Avoid conflicts"],
                                            ["pcal-gcal-sync", True, "Sync confirmed study sessions"]])
        self.assertEqual([b[1] for b in state["buttons"]], ["Sync now", "Disconnect"])
        self.assertIsNone(state["syncError"])
        self.assertNotRegex(state["text"], r"token|secret|study-1@")

    def test_busy_blocks_are_read_only_and_anonymous(self):
        busy = self.out["connected"]["busy"]
        self.assertEqual([(b["date"], b["start"], b["end"]) for b in busy],
                         [("2026-09-21", "13:00", "15:00"), ("2026-09-23", "23:00", "24:00"), ("2026-09-24", "00:00", "01:00")])
        for block in busy:
            self.assertEqual(block["text"], "Busy")
            self.assertEqual((block["tag"], block["role"], block["tabIndex"], block["draggable"], block["pointerEvents"]),
                             ("DIV", None, -1, False, "none"))
            self.assertIn("repeating-linear-gradient", block["background"])   # hatched, unlike availability/sessions
            self.assertTrue(block["label"].startswith("Busy in Google Calendar"))
        self.assertEqual((busy[0]["top"], busy[0]["height"]), ("624px", "94px"))
        self.assertNotEqual(self.out["connected"]["hitTarget"], "pcal-busy")
        self.assertFalse(self.out["popoverAfterBusyClick"])
        self.assertRegex(self.out["connected"]["busyCall"], r"/busy\?start=2026-09-21&days=7&utc_offset_minutes=-?\d+$")
        self.assertRegex(self.out["nextWeek"]["busyCall"], r"start=2026-09-28")

    def test_busy_time_blocks_placement(self):
        placement = self.out["placement"]
        self.assertEqual(placement["inBusy"], "That time is busy in your Google Calendar.")
        self.assertEqual(placement["overlapsBusyEdge"], "That time is busy in your Google Calendar.")
        self.assertIsNone(placement["free"])
        self.assertIsNone(self.out["avoidOff"]["free"])
        self.assertEqual(self.out["avoidOff"]["busy"], 0)
        self.assertEqual(self.out["avoidOff"]["body"], {"avoid_conflicts": False})
        self.assertEqual(self.out["avoidOn"], 3)

    def test_sync_error_indicator_and_retry(self):
        failed = self.out["syncFailed"]
        self.assertEqual((failed["skipped"], failed["sessionStatus"]), (["s-quiz"], "skipped"))   # the action itself succeeded
        self.assertEqual(failed["syncError"], "Calendar sync failed")
        retried = self.out["retried"]
        self.assertIsNone(retried["syncError"])
        self.assertIn("utc_offset_minutes", retried["syncBody"])

    def test_written_quiz_session_still_starts_from_the_calendar(self):
        written = self.out["writtenQuiz"]
        self.assertIn("pcal-activity--written_quiz", written["classes"])
        self.assertTrue(written["meta"].startswith("Written Quiz"))
        self.assertIn("Start Session", written["buttons"])
        self.assertEqual([call[0] for call in written["startCalls"]], ["s-wq"])
        self.assertEqual((written["page"], written["tab"], written["document"]), ("session", "quiz", "mkt.pdf"))

    def test_disconnect(self):
        state = self.out["afterDisconnect"]
        self.assertEqual(state["buttons"], [["pcal-gcal-connect", "Connect", False]])
        self.assertEqual(state["busy"], 0)
        self.assertIn("POST /api/integrations/google-calendar/disconnect", self.out["calls"])

    def test_no_google_internals_in_the_page(self):
        self.assertNotRegex(self.out["pageText"], r"google_calendar_|access_token|refresh_token|client_secret")


if __name__ == "__main__":
    unittest.main()
