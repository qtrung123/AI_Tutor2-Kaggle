"""PLANNER-SUBJECT-CALENDAR (desktop, 1280px, headless Chrome, mocked API).

Live plan: every calendar session (a combined one too) and Study Queue item shows its subject
(name + color; Unassigned included); the Subject filter shows only matching sessions and queue
items, says how many are shown and offers "Show all subjects"; filtering sends no planner or
Google Calendar request; moving a document to another subject and deleting a subject are reflected
at once (a deleted subject's filter falls back to all); the Add materials sheet offers no bulk add
while a plan is active. Draft plan: "Add all" adds every document of a subject, each as its own
material with its own goal, and only re-runs the preview. Skipped without Chrome."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_subject_management_ui import MOCK as SUBJECT_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

COMMON = SUBJECT_MOCK + r"""
window.__materialBodies = [];
window.__otherCalls = [];
const calendarFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (p.startsWith("/api/integrations")) window.__otherCalls.push(method + " " + p);
  if (/\/materials$/.test(p) && method === "POST") window.__materialBodies.push(JSON.parse(init.body));
  if (/^\/api\/planner\/plans\/[^/]+\/candidates$/.test(p)) return json(window.__candidates || []);
  return calendarFetch(input, init);
};
"""

LIVE_DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const events = () => [...document.querySelectorAll(".pcal-event[data-session-id]")].map((block) => ({
  id: block.dataset.sessionId, subject: block.querySelector(".pcal-event-subject")?.textContent || null,
  aria: block.getAttribute("aria-label"), combined: block.classList.contains("pcal-event--combined")})).sort((a, b) => a.id.localeCompare(b.id));
const queue = () => [...document.querySelectorAll("#pcal-queue li")].map((item) => [item.className.split(" ")[0],
  item.querySelector(".pcal-queue-title")?.textContent || item.textContent, item.querySelector(".pcal-queue-subject")?.textContent || null]);
const filter = () => document.querySelector("#pcal-subject-filter select");
const choose = async (value) => { filter().value = value; filter().dispatchEvent(new Event("change")); await sleep(200); };
const note = () => document.querySelector(".pcal-subject-filter-note")?.textContent || null;
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 9, 0, 0);
  window.__planner.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  window.__planner.materials = ["mkt.pdf", "stats.pdf"].map((doc, i) => ({material_id: "m" + i, plan_id: "plan-1", document_id: doc, deadline: null, goal_type: "flexible", learning_state: "new"}));
  const session = (id, doc, title, start, end, extra = {}) => ({session_id: id, document_id: doc, document_title: title, activity_type: "summary",
    scheduled_start: start, scheduled_end: end, duration_minutes: 60, status: "scheduled", reason: {code: "new_material", message: "New"}, artifact_id: null, ...extra});
  window.__planner.sessionsByPlan = {"plan-1": [
    session("s1", "mkt.pdf", "Marketing", "2026-09-25T10:00:00", "2026-09-25T11:00:00"),
    session("s2", "stats.pdf", "Statistics", "2026-09-25T10:30:00", "2026-09-25T11:30:00"),
    session("s3", "pbi.pdf", "PowerBI", "2026-09-26T10:00:00", "2026-09-26T11:00:00",
      {steps: [{activity_type: "summary", minutes: 30, status: "pending"}, {activity_type: "flashcards", minutes: 30, status: "pending"}]})]};
  window.__planner.sessions = window.__planner.sessionsByPlan["plan-1"];
  window.__candidates = [{candidate_key: "c1", document_id: "mkt.pdf", document_title: "Marketing", activity_type: "quiz", estimated_minutes: 30, reason: {message: "Check"}},
                         {candidate_key: "c2", document_id: "pbi.pdf", document_title: "PowerBI", activity_type: "quiz", estimated_minutes: 30, reason: {message: "Check"}}];
  setPage("overview"); await sleep(600);
  setPage("planner"); await sleep(900);
  out.all = {events: events(), options: [...filter().options].map((o) => o.textContent), note: note()};
  document.getElementById("pcal-queue-toggle").click(); await sleep(200);
  out.queueAll = queue();
  const plannerCallsBefore = window.__planner.calls.length, otherBefore = window.__otherCalls.length;
  await choose("phy");
  out.physics = {events: events(), note: note(), queue: queue(), queueCount: document.getElementById("pcal-queue-count").textContent};
  await choose("unassigned");
  out.unassigned = {events: events(), note: note()};
  out.resetColor = getComputedStyle(document.querySelector(".pcal-subject-filter-reset")).color;
  document.querySelector(".pcal-subject-filter-reset").click(); await sleep(200);
  out.reset = {events: events().map((e) => e.id), value: filter().value, note: note()};
  out.requestsWhileFiltering = {planner: window.__planner.calls.slice(plannerCallsBefore), other: window.__otherCalls.slice(otherBefore)};

  // Reassignment: Statistics moves to Biology (Home's Subject menu uses the same call).
  await choose("bio");
  await moveDocumentsToSubject(["stats.pdf"], "bio"); await sleep(300);
  out.afterMove = {events: events(), note: note()};
  // Deleting the filtered subject: its documents become Unassigned and the filter falls back to all.
  window.confirm = () => true;
  await deleteSubject(subjectById("bio")); await sleep(300);
  out.afterDelete = {events: events(), value: filter().value, options: [...filter().options].map((o) => o.textContent)};

  document.getElementById("pcal-add-materials").click(); await sleep(300);
  out.liveSheet = {note: document.querySelector(".pcal-sheet-note")?.textContent || null,
    addAll: document.querySelectorAll(".planner-subject-group-action").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""

DRAFT_DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 9, 0, 0);
  setPage("overview"); await sleep(600);
  setPage("planner"); await sleep(800);
  document.getElementById("pcal-add-materials").click(); await sleep(300);
  const bio = document.querySelector('#pcal-sheet-list .planner-subject-group[data-subject-group="bio"]');
  out.buttons = [...document.querySelectorAll(".planner-subject-group-action")].map((b) => [b.closest(".planner-subject-group").dataset.subjectGroup, b.textContent]);
  out.docs = indexedDocuments.map((doc) => doc.id);
  // The second document's own goal choice is kept: a deadline goal with a date.
  const row = bio.querySelector('.pcal-sheet-row[data-document-id="stats.pdf"]');
  row.querySelector('input[value="deadline"]').click(); await sleep(50);
  const date = row.querySelector('.planner-goal-deadline input, input[type="date"]');
  date.value = "2026-10-20"; date.dispatchEvent(new Event("input")); date.dispatchEvent(new Event("change")); await sleep(50);
  bio.querySelector(".planner-subject-group-action").click(); await sleep(1500);
  out.bodies = window.__materialBodies;
  out.materials = window.__planner.materials.map((m) => m.document_id);
  out.previews = window.__planner.previewBodies.length;
  out.adaptCalls = window.__planner.calls.filter((call) => /adapt|propos/.test(call));
  out.sheetAfter = [...document.querySelectorAll('#pcal-sheet-list .planner-subject-group')].map((g) => [g.dataset.subjectGroup, g.querySelector(".planner-subject-group-count").textContent]);
  out.addAllToast = document.getElementById("toast").textContent;

  // Preview -> Accept plan: the saved sessions are the server's, one document each.
  document.getElementById("pcal-sheet-close").click(); await sleep(100);
  out.previewSessions = (plannerPreview?.sessions || []).map((s) => s.document_id);
  document.getElementById("pcal-accept").click(); await sleep(1200);
  out.confirmBody = window.__planner.confirmBodies[0] || null;
  out.saved = window.__planner.sessions.map((s) => ({id: s.session_id, doc: s.document_id}));
  out.calendar = [...document.querySelectorAll(".pcal-event[data-session-id]")].map((b) => [b.dataset.sessionId, b.querySelector(".pcal-event-subject")?.textContent || null]);
  // Study Session: starting a planned session opens its own document.
  window.__planner.sessionsByPlan = {[plannerPlan.plan_id]: window.__planner.sessions};   // the mock's start looks here
  const first = plannerSessions.find((s) => s.document_id === "stats.pdf") || plannerSessions[0];
  await plannerSessionAction(first, "start", document.createElement("button"), document.createElement("div")); await sleep(600);
  out.started = {session: first.document_id, opened: activeDocumentId, page: document.body.dataset.page};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


LIVE_SUBJECTS = r"""
window.__subjects = [
  {subject_id: "bio", name: "Biology", color: "#10b981", created_at: "t", updated_at: "t", document_ids: ["mkt.pdf"]},
  {subject_id: "phy", name: "Physics", color: "#ef4444", created_at: "t", updated_at: "t", document_ids: ["stats.pdf"]}];
"""


def run(driver, prelude=""):
    original = (sidebar_harness.MOCK, sidebar_harness.DRIVER)
    sidebar_harness.MOCK, sidebar_harness.DRIVER = COMMON + prelude, driver
    try:
        return sidebar_harness.run_at_width(1280, 900)
    finally:
        sidebar_harness.MOCK, sidebar_harness.DRIVER = original


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class LivePlanSubjectCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(LIVE_DRIVER, LIVE_SUBJECTS)

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_every_session_shows_its_subject(self):
        events = self.out["all"]["events"]
        self.assertEqual([(e["id"], e["subject"]) for e in events], [("s1", "Biology"), ("s2", "Physics"), ("s3", "Unassigned")])
        self.assertTrue(events[2]["combined"])
        self.assertIn("· Marketing · Biology,", events[0]["aria"])
        self.assertEqual(self.out["all"]["options"], ["All subjects", "Biology", "Physics", "Unassigned"])
        self.assertIsNone(self.out["all"]["note"])

    def test_queue_items_show_their_subject(self):
        self.assertEqual([(cls, subject) for cls, _title, subject in self.out["queueAll"]],
                         [("pcal-queue-item", "Biology"), ("pcal-queue-item", "Unassigned")])

    def test_filter_shows_only_matching_sessions_and_queue_items(self):
        physics = self.out["physics"]
        self.assertEqual([e["id"] for e in physics["events"]], ["s2"])
        self.assertEqual(physics["note"], "Showing 1 of 3 sessions this week")
        self.assertEqual(physics["queue"], [["pcal-queue-hidden", "2 more in other subjects", None]])
        self.assertEqual(physics["queueCount"], "")
        self.assertEqual([e["id"] for e in self.out["unassigned"]["events"]], ["s3"])
        self.assertEqual(self.out["reset"], {"events": ["s1", "s2", "s3"], "value": "", "note": None})

    def test_show_all_subjects_uses_the_accent_text_color(self):
        self.assertEqual(self.out["resetColor"], "rgb(65, 70, 192)")   # --ds-accent-text, 7:1 on white

    def test_filtering_sends_no_planner_or_calendar_requests(self):
        self.assertEqual(self.out["requestsWhileFiltering"], {"planner": [], "other": []})

    def test_reassignment_and_deleted_subject(self):
        moved = self.out["afterMove"]
        self.assertEqual([(e["id"], e["subject"]) for e in moved["events"]], [("s1", "Biology"), ("s2", "Biology")])
        self.assertEqual(moved["note"], "Showing 2 of 3 sessions this week")
        deleted = self.out["afterDelete"]
        self.assertEqual(deleted["value"], "")   # back to All subjects
        self.assertEqual([(e["id"], e["subject"]) for e in deleted["events"]], [("s1", "Unassigned"), ("s2", "Unassigned"), ("s3", "Unassigned")])
        self.assertNotIn("Biology", deleted["options"])

    def test_live_plan_sheet_has_no_bulk_add(self):
        self.assertEqual(self.out["liveSheet"]["addAll"], 0)
        self.assertIn("add documents one at a time", self.out["liveSheet"]["note"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class DraftPlanAddAllTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(DRAFT_DRIVER, r"""
window.__planner.availability = [{availability_id: "a0", is_recurring: true, day_of_week: 0, date: null, start_at: "18:00", end_at: "21:00"}];
""")

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_add_all_adds_each_document_with_its_own_goal(self):
        self.assertIn(["bio", "Add all 2"], self.out["buttons"])   # only groups with more than one document
        self.assertTrue(all(int(text.split()[-1]) > 1 for _key, text in self.out["buttons"]))
        bodies = {body["document_id"]: body for body in self.out["bodies"]}
        self.assertEqual(sorted(bodies), ["mkt.pdf", "stats.pdf"])
        self.assertEqual(bodies["mkt.pdf"].get("goal_type"), "flexible")
        self.assertEqual((bodies["stats.pdf"].get("goal_type"), bodies["stats.pdf"].get("deadline")), ("deadline", "2026-10-20"))
        self.assertEqual(sorted(self.out["materials"]), ["mkt.pdf", "stats.pdf"])
        self.assertEqual(dict(self.out["sheetAfter"])["bio"], "2 of 2 in plan")

    def test_accepted_plan_is_document_based_and_opens_the_document(self):
        self.assertTrue(self.out["previewSessions"])
        self.assertTrue(all(doc.endswith(".pdf") for doc in self.out["previewSessions"]))
        self.assertNotIn("subject_id", self.out["confirmBody"] or {})
        self.assertTrue(self.out["saved"])
        self.assertTrue(all(row["doc"] in ("mkt.pdf", "stats.pdf") for row in self.out["saved"]))
        self.assertTrue(all(subject == "Biology" for _id, subject in self.out["calendar"]))
        self.assertEqual(self.out["started"]["opened"], self.out["started"]["session"])
        self.assertEqual(self.out["started"]["page"], "session")

    def test_add_all_summarizes_its_result(self):
        self.assertEqual(self.out["addAllToast"], "Added 2 documents to your plan")

    def test_only_the_preview_reruns(self):
        self.assertGreaterEqual(self.out["previews"], 1)
        self.assertEqual(self.out["adaptCalls"], [])


PARTIAL_DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
const failingFetch = window.fetch;
window.fetch = async (input, init = {}) => {   // the second document cannot be added
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (/\/materials$/.test(p) && (init.method || "GET").toUpperCase() === "POST" && JSON.parse(init.body).document_id === "stats.pdf") {
    return new Response(JSON.stringify({detail: "Server busy"}), {status: 503, headers: {"Content-Type": "application/json"}});
  }
  return failingFetch(input, init);
};
(async () => {
  await sleep(1500);
  setPage("overview"); await sleep(600);
  setPage("planner"); await sleep(800);
  document.getElementById("pcal-add-materials").click(); await sleep(300);
  document.querySelector('#pcal-sheet-list .planner-subject-group[data-subject-group="bio"] .planner-subject-group-action').click(); await sleep(1500);
  out.toast = document.getElementById("toast").textContent;
  out.materials = window.__planner.materials.map((m) => m.document_id);
  out.stillListed = [...document.querySelectorAll('#pcal-sheet-list .pcal-sheet-row')].map((row) => row.dataset.documentId);
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class AddAllPartialFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(PARTIAL_DRIVER)

    def test_partial_failure_is_reported_and_the_rest_is_kept(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertEqual(self.out["materials"], ["mkt.pdf"])
        self.assertEqual(self.out["toast"], "Added 1 of 2 documents. Not added: Statistics. Add it again from this list.")
        self.assertIn("stats.pdf", self.out["stillListed"])


if __name__ == "__main__":
    unittest.main()
