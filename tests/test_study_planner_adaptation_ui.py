"""Real-browser tests for adaptive replanning UX (Phase 5B2; headless Chrome, mocked API):
small proposals are applied automatically with a short note; large ones open a review panel
(Accept -> apply with confirm=true, Keep current plan -> nothing written). "Now" is pinned to
Thu 2026-09-24 12:00 browser-local. Runs at phone width (390px): desktop (>=1024px) reviews large
proposals on the calendar instead (tests/test_study_planner_adaptation_calendar_ui.py)."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_today_ui import MOCK as TODAY_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

# The adaptation endpoint mirrors the backend contract. P.adaptMode picks the proposal: small
# (applied at once), large (needs confirm=true) or conflict (confirm fails with 409). Applying
# really changes the mocked sessions, so Home and the week must show the new state afterwards.
MOCK = TODAY_MOCK + r"""
window.__planner.adaptCalls = [];
window.__planner.adaptMode = "small";
const todayFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const P = window.__planner;
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const m = p.match(/^\/api\/planner\/plans\/([^/]+)\/adaptation\/apply$/);
  if (!m) return todayFetch(input, init);
  const body = JSON.parse(init.body);
  P.adaptCalls.push(body);
  await new Promise((resolve) => setTimeout(resolve, 300));
  const sessions = P.sessionsByPlan[m[1]] || [];
  const trigger = body.trigger;
  const target = sessions.find((s) => s.session_id === trigger.session_id);
  const proposal = P.adaptMode === "small"
    ? {added: [{document_id: target ? target.document_id : trigger.document_id, activity_type: target ? target.activity_type : "review",
                scheduled_start: "2026-09-25T18:00:00", scheduled_end: "2026-09-25T18:30:00", duration_minutes: 30,
                reason_code: "rescheduled", message: "Replace the skipped quiz session for \"Statistics\".", artifact_id: null,
                replaces_session_id: trigger.session_id || null}],
       moved: [], cancelled: [], significance: "small"}
    : P.largeProposal ? JSON.parse(JSON.stringify(P.largeProposal))
    : {added: [{document_id: "mkt.pdf", activity_type: "summary", scheduled_start: "2026-09-24T18:00:00",
                scheduled_end: "2026-09-24T18:45:00", duration_minutes: 45, reason_code: "rescheduled",
                message: "Replace the missed summary session for \"Marketing\".", artifact_id: null, replaces_session_id: trigger.session_id}],
       moved: [{session_id: "stats-quiz", document_id: "stats.pdf", activity_type: "quiz", from_start: "2026-09-25T18:00:00",
                from_end: "2026-09-25T18:30:00", to_start: "2026-09-26T18:00:00", to_end: "2026-09-26T18:30:00",
                message: "Move the quiz session for \"Statistics\" to 2026-09-26 18:00: latest quiz scored 40%."}],
       cancelled: [{session_id: "pbi-review", document_id: "pbi.pdf", activity_type: "review", scheduled_start: "2026-09-27T18:00:00",
                    message: "Cancel the review session for \"PowerBI\": latest quiz scored 95%, it is no longer needed."}],
       significance: "large"};
  const result = {plan_id: m[1], trigger, unchanged_count: 1, change_count: proposal.added.length + proposal.moved.length + proposal.cancelled.length,
    warnings: [], reasons: ["x"], ...proposal};
  if (P.adaptMode === "large" && !body.confirm) return json({...result, applied: false, requires_confirmation: true, sessions: []});
  if (P.adaptMode === "conflict" && body.confirm) return json({detail: {code: "stale_plan", message: "The plan changed.", status: ""}}, 409);
  if (P.adaptMode === "conflict") return json({...result, applied: false, requires_confirmation: true, sessions: []});
  // Apply: the mocked store changes like the real one would.
  if (target && target.status === "scheduled") target.status = "rescheduled";
  proposal.added.forEach((a, i) => sessions.push({session_id: "added-" + P.adaptCalls.length + "-" + i, document_id: a.document_id,
    document_title: {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"}[a.document_id], activity_type: a.activity_type,
    scheduled_start: a.scheduled_start, scheduled_end: a.scheduled_end, duration_minutes: a.duration_minutes, status: "scheduled",
    reason: {code: "rescheduled", message: "Rescheduled"}, artifact_id: null}));
  proposal.moved.forEach((mv) => { const s = sessions.find((x) => x.session_id === mv.session_id); if (s) Object.assign(s, {scheduled_start: mv.to_start, scheduled_end: mv.to_end}); });
  proposal.cancelled.forEach((c) => { const s = sessions.find((x) => x.session_id === c.session_id); if (s) s.status = "cancelled"; });
  return json({...result, applied: true, requires_confirmation: false, sessions: []});
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
const session = (id, doc, activity, start, end, minutes, status = "scheduled") => ({session_id: id, document_id: doc,
  document_title: titles[doc], activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
  status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null});
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1;
const item = (title, activity) => [...document.querySelectorAll(".planner-session")]
  .find((el) => el.offsetParent !== null && el.querySelector("strong").textContent === title
    && el.querySelector(".planner-activity-chip").textContent === activity);
const press = (title, activity, label) => [...item(title, activity).querySelectorAll("button")].find((b) => b.textContent === label);
const panel = () => document.getElementById("adapt-review");
const readPanel = () => panel() && ({
  title: panel().querySelector("h2").textContent,
  intro: panel().querySelector(".adapt-intro").textContent,
  groups: [...panel().querySelectorAll(".adapt-group")].map((g) => ({heading: g.querySelector("h3").textContent,
    items: [...g.querySelectorAll(".adapt-item")].map((li) => ({name: li.querySelector("strong").textContent,
      when: li.querySelector(".adapt-when").textContent, reason: li.querySelector("small").textContent}))})),
  buttons: [...panel().querySelectorAll("button")].map((b) => b.textContent),
  text: panel().textContent,
  fits: panel().querySelector(".adapt-panel").getBoundingClientRect().right <= window.innerWidth + 1
    && panel().querySelector(".adapt-panel").getBoundingClientRect().left >= -1,
});
const skipCalls = () => P.actionCalls.filter((c) => c[0] === "skip").length;
// The saved week: calendar events on desktop (>=1024px), the step flow's week list on phone.
const weekEntries = () => window.innerWidth >= 1024
  ? [...document.querySelectorAll("#pcal-body .pcal-event")].map((e) => e.querySelector(".pcal-event-title").textContent + " "
    + e.querySelector(".pcal-event-meta").textContent.split(" · ")[1])
  : [...document.querySelectorAll("#planner-plan-sessions .planner-session")].map((li) =>
    li.querySelector("strong").textContent + " " + li.querySelector(".planner-session-time").textContent);
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null}];

  // 1. Small: skip an overdue quiz -> adapted automatically, short note, Home + week refreshed.
  P.sessionsByPlan = {"plan-1": [
    session("stats-overdue", "stats.pdf", "quiz", "2026-09-24T09:00:00", "2026-09-24T09:30:00", 30),
    session("pbi-later", "pbi.pdf", "flashcards", "2026-09-24T15:00:00", "2026-09-24T15:20:00", 20)]};
  setPage("planner"); await sleep(500);
  setPage("overview"); await sleep(400);
  press("Statistics", "Quiz", "Skip").click(); await sleep(1600);
  out.small = {calls: P.adaptCalls.map((c) => c.trigger), confirm: P.adaptCalls.map((c) => !!c.confirm), toast: toast.textContent,
    panel: !!panel(), skips: skipCalls()};
  setPage("planner"); await sleep(500);
  out.small.week = weekEntries();
  out.overflow.small = noOverflow();

  // 2. Large via "Reschedule" on a session not completed: review panel, Keep current plan writes nothing.
  P.adaptMode = "large"; P.adaptCalls = []; P.actionCalls = [];
  P.sessionsByPlan = {"plan-1": [
    session("mkt-missed", "mkt.pdf", "summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45),
    session("stats-quiz", "stats.pdf", "quiz", "2026-09-25T18:00:00", "2026-09-25T18:30:00", 30),
    session("pbi-review", "pbi.pdf", "review", "2026-09-27T18:00:00", "2026-09-27T18:10:00", 10)]};
  const before = JSON.stringify(P.sessionsByPlan);
  setPage("overview"); await sleep(400);
  press("Marketing", "Summary", "Reschedule").click(); await sleep(900);
  out.review = readPanel();
  out.overflow.review = noOverflow();
  document.getElementById("adapt-keep").click(); await sleep(200);
  out.kept = {panel: !!panel(), calls: P.adaptCalls.length, confirms: P.adaptCalls.filter((c) => c.confirm).length,
    unchanged: JSON.stringify(P.sessionsByPlan) === before, legacyReschedule: P.actionCalls.length, toast: toast.textContent,
    stillNotCompleted: !!item("Marketing", "Summary")};

  // 3. Accept: double click -> one confirm=true apply; then Home + week show the persisted result.
  press("Marketing", "Summary", "Reschedule").click(); await sleep(900);
  const accept = document.getElementById("adapt-accept");
  accept.click(); accept.click(); await sleep(50);
  out.accepting = {text: accept.textContent, disabled: accept.disabled, keepDisabled: document.getElementById("adapt-keep").disabled};
  await sleep(1400);
  out.accepted = {panel: !!panel(), confirms: P.adaptCalls.filter((c) => c.confirm).length, toast: toast.textContent,
    notCompleted: !!item("Marketing", "Summary") && !!item("Marketing", "Summary").querySelector(".planner-session-note"),
    homeTitles: [...document.querySelectorAll("#today-plan .planner-session strong")].map((s) => s.textContent)};
  setPage("planner"); await sleep(500);
  out.accepted.week = weekEntries();

  // 4. Stale plan while reviewing: a clear, calm error; nothing closes; buttons usable again.
  P.adaptMode = "conflict"; P.adaptCalls = [];
  P.sessionsByPlan["plan-1"].push(session("stats-missed", "stats.pdf", "quiz", "2026-09-24T10:00:00", "2026-09-24T10:30:00", 30));
  setPage("overview"); await sleep(400);
  press("Statistics", "Quiz", "Reschedule").click(); await sleep(900);
  document.getElementById("adapt-accept").click(); await sleep(700);
  out.conflict = {panel: !!panel(), error: panel()?.querySelector(".adapt-error").textContent,
    errorShown: panel() ? !panel().querySelector(".adapt-error").hidden : false,
    acceptEnabled: !document.getElementById("adapt-accept").disabled};
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); await sleep(100);
  out.conflict.escapeClosed = !panel();

  // 5. Quiz completion adapts the plan that holds the document (and only that one).
  P.adaptMode = "small"; P.adaptCalls = [];
  await plannerAdaptAfterQuiz("mkt.pdf"); await sleep(400);
  out.quiz = {calls: P.adaptCalls.map((c) => c.trigger), toast: toast.textContent};
  P.adaptCalls = [];
  await plannerAdaptAfterQuiz("pbi.pdf"); await sleep(300);
  out.quiz.otherDocumentCalls = P.adaptCalls.length;
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


class AdaptationUiAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_small_adaptation_applies_automatically_with_a_short_note(self):
        small = self.out["small"]
        self.assertEqual(small["calls"], [{"kind": "session_skipped", "session_id": "stats-overdue"}])
        self.assertEqual(small["confirm"], [False])
        self.assertEqual(small["skips"], 1)
        self.assertFalse(small["panel"])
        self.assertRegex(small["toast"], r"^Session skipped\. Plan adjusted: quiz added .+? 18:00\.$")
        self.assertIn("Statistics 18:00–18:30", small["week"])   # the week shows the persisted replacement

    def test_large_adaptation_opens_a_readable_review(self):
        review = self.out["review"]
        self.assertEqual(review["title"], "Review plan changes")
        self.assertIn("Nothing changes unless you accept", review["intro"])
        self.assertEqual([g["heading"] for g in review["groups"]], ["New sessions", "Moved", "No longer needed"])
        added, moved, removed = (g["items"][0] for g in review["groups"])
        self.assertEqual(added["name"], "Summary · Marketing")
        self.assertRegex(added["when"], r"24.*, 18:00$")
        self.assertEqual(added["reason"], 'Replace the missed summary session for "Marketing".')
        self.assertEqual(moved["name"], "Quiz · Statistics")
        self.assertRegex(moved["when"], r"25.*18:00 → .*26.*18:00$")
        self.assertEqual(moved["reason"], "Latest quiz scored 40%.")
        self.assertEqual(removed["name"], "Review · PowerBI")
        self.assertRegex(removed["when"], r"^Removed from plan · was .*27")
        self.assertEqual(review["buttons"], ["Keep current plan", "Accept changes"])
        self.assertNotRegex(review["text"], r"session_missed|final_review|rescheduled|cancelled|priority|stale|fail|error")
        self.assertTrue(review["fits"])

    def test_keep_current_plan_writes_nothing(self):
        kept = self.out["kept"]
        self.assertEqual((kept["panel"], kept["calls"], kept["confirms"]), (False, 1, 0))
        self.assertTrue(kept["unchanged"])
        self.assertEqual(kept["legacyReschedule"], 0)
        self.assertEqual(kept["toast"], "Kept your current plan")
        self.assertTrue(kept["stillNotCompleted"])

    def test_accept_is_double_click_safe_and_refreshes(self):
        self.assertEqual(self.out["accepting"], {"text": "Applying…", "disabled": True, "keepDisabled": True})
        accepted = self.out["accepted"]
        self.assertEqual((accepted["panel"], accepted["confirms"]), (False, 1))
        self.assertRegex(accepted["toast"], r"^Plan adjusted: quiz moved to .+? 18:00, summary added .+? 18:00, review no longer needed\.$")
        self.assertFalse(accepted["notCompleted"])                # the missed session is replaced
        self.assertNotIn("PowerBI", accepted["homeTitles"])      # removed from the plan
        self.assertIn("Marketing 18:00–18:45", accepted["week"])
        self.assertIn("Statistics 18:00–18:30", accepted["week"])
        self.assertFalse(any(entry.startswith("PowerBI") for entry in accepted["week"]))

    def test_conflict_is_explained_calmly(self):
        conflict = self.out["conflict"]
        self.assertTrue(conflict["panel"])
        self.assertTrue(conflict["errorShown"])
        self.assertEqual(conflict["error"], "Your plan changed in the meantime. Try again to use the latest version.")
        self.assertTrue(conflict["acceptEnabled"])
        self.assertTrue(conflict["escapeClosed"])

    def test_quiz_completion_adapts_only_plans_holding_the_document(self):
        quiz = self.out["quiz"]
        self.assertEqual(quiz["calls"], [{"kind": "quiz_completed", "document_id": "mkt.pdf"}])
        self.assertRegex(quiz["toast"], r"^Plan adjusted: review added .+? 18:00\.$")
        self.assertEqual(quiz["otherDocumentCalls"], 0)

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class AdaptationUiPhoneTests(AdaptationUiAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_runs_at_phone_width(self):
        self.assertEqual(self.out["width"], 390)


if __name__ == "__main__":
    unittest.main()
