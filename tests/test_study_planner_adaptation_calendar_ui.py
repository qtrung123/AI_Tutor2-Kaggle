"""Real-browser tests: on desktop (>=1024px) a large adaptation proposal is reviewed on the week
calendar itself -- no "Review plan changes" modal. New and moved-to sessions are dashed ghosts,
sessions being moved or no longer needed stay in place muted, the toolbar offers Keep current plan
(zero writes) / Accept changes (one apply with confirm=true), a 409 drops the stale proposal and
offers a fresh check, and changes outside the visible week are pointed to ("1 change next week").
Small adaptations still apply at once. Headless Chrome with the mocked adaptation API from
tests/test_study_planner_adaptation_ui.py; "now" is pinned to Thu 2026-09-24 12:00. Runs at
1440px and 1100px."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_adaptation_ui import MOCK
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
const titles = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "pbi.pdf": "PowerBI"};
const session = (id, doc, activity, start, end, minutes, status = "scheduled") => ({session_id: id, document_id: doc,
  document_title: titles[doc], activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
  status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, rescheduled_from: null});
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1
  && $("pcal-review").scrollWidth <= $("pcal-review").clientWidth + 1
  && (!$("pcal-popover").hidden ? $("pcal-popover").getBoundingClientRect().right <= window.innerWidth + 1 : true);
const events = () => [...document.querySelectorAll("#pcal-body .pcal-event")].map((e) => ({
  kind: e.dataset.kind, change: e.dataset.change || null, id: e.dataset.sessionId || null, start: e.dataset.start,
  title: e.querySelector(".pcal-event-title").textContent, tag: e.querySelector(".pcal-event-tag")?.textContent || null, label: e.title || null,
  dashed: getComputedStyle(e).borderTopStyle === "dashed", draggable: e.classList.contains("is-draggable"),
  struck: getComputedStyle(e.querySelector(".pcal-event-title")).textDecorationLine.includes("line-through")}));
const bar = () => ({shown: !$("pcal-review").hidden, text: $("pcal-review").hidden ? "" : $("pcal-review").textContent,
  statusShown: !$("pcal-status").parentElement.hidden,
  keep: $("pcal-review-keep")?.textContent || null, accept: $("pcal-review-accept")?.textContent || null});
const popover = (selector) => {
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  document.querySelector(selector).click();
  const pop = $("pcal-popover");
  return {open: !pop.hidden, pill: pop.querySelector(".pcal-kind")?.textContent || null,
    title: pop.querySelector(".pcal-popover-title")?.textContent || null,
    activity: pop.querySelector(".pcal-popover-activity")?.textContent || null,
    when: [...pop.querySelectorAll(".pcal-popover-when")].map((p) => p.textContent),
    reason: pop.querySelector(".pcal-popover-reason")?.textContent || null,
    actions: pop.querySelectorAll(".pcal-session-actions button").length, text: pop.textContent};
};
const reschedule = async (id) => {
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  document.querySelector(`.pcal-event[data-session-id="${id}"]`).click();
  [...$("pcal-popover").querySelectorAll(".pcal-session-actions button")].find((b) => b.textContent === "Reschedule").click();
  await sleep(1200);
};
const homeItem = (title, activity) => [...document.querySelectorAll("#today-plan .planner-session")]
  .find((el) => el.querySelector("strong").textContent === title && el.querySelector(".planner-activity-chip").textContent === activity);
const modal = () => !!$("adapt-review");
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  P.materials = [{material_id: "m-mkt", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null}];
  out.desktop = plannerIsDesktop();

  // 1. Small: skipping from the calendar adapts at once -- no review, a short note, the week refreshed.
  P.sessionsByPlan = {"plan-1": [
    session("stats-overdue", "stats.pdf", "quiz", "2026-09-24T09:00:00", "2026-09-24T09:30:00", 30),
    session("pbi-later", "pbi.pdf", "flashcards", "2026-09-24T15:00:00", "2026-09-24T15:20:00", 20)]};
  setPage("planner"); await sleep(700);
  document.querySelector('.pcal-event[data-session-id="stats-overdue"]').click();
  [...$("pcal-popover").querySelectorAll(".pcal-session-actions button")].find((b) => b.textContent === "Skip").click();
  await sleep(1600);
  out.small = {confirm: P.adaptCalls.map((c) => !!c.confirm), toast: toast.textContent, modal: modal(), bar: bar(),
    changes: events().filter((e) => e.change).length,
    replacement: events().some((e) => e.title === "Statistics" && e.start === "2026-09-25T18:00:00" && e.kind === "confirmed")};

  // 2. Large, from Home's Reschedule: the calendar opens with the proposal drawn on it.
  P.adaptMode = "large"; P.adaptCalls = []; P.actionCalls = [];
  P.sessionsByPlan = {"plan-1": [
    session("mkt-missed", "mkt.pdf", "summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45),
    session("stats-quiz", "stats.pdf", "quiz", "2026-09-25T18:00:00", "2026-09-25T18:30:00", 30),
    session("pbi-review", "pbi.pdf", "review", "2026-09-27T18:00:00", "2026-09-27T18:10:00", 10)]};
  const before = JSON.stringify(P.sessionsByPlan);
  setPage("overview"); await sleep(600);
  [...homeItem("Marketing", "Summary").querySelectorAll("button")].find((b) => b.textContent === "Reschedule").click();
  await sleep(1500);
  out.review = {page: state.page, modal: modal(), bar: bar(), events: events(), toast: toast.textContent,
    range: $("pcal-range").textContent, overflow: noOverflow()};
  out.review.added = popover('.pcal-event[data-change="added"]');
  out.review.moved = popover('.pcal-event[data-change="moved"]');
  out.review.moving = popover('.pcal-event[data-change="moving"]');
  out.review.cancelled = popover('.pcal-event[data-change="cancelled"]');
  out.overflow.review = noOverflow();
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));

  // 3. Keep current plan: zero writes, the proposal disappears.
  $("pcal-review-keep").click(); await sleep(300);
  out.kept = {bar: bar(), changes: events().filter((e) => e.change).length, calls: P.adaptCalls.length,
    confirms: P.adaptCalls.filter((c) => c.confirm).length, legacy: P.actionCalls.length,
    unchanged: JSON.stringify(P.sessionsByPlan) === before, toast: toast.textContent};

  // 4. Accept (from the calendar's own Reschedule): double click -> exactly one confirm=true apply.
  P.adaptCalls = [];
  await reschedule("mkt-missed");
  out.accepting = {bar: bar(), modal: modal()};
  $("pcal-review-accept").click(); $("pcal-review-accept").click(); await sleep(50);
  out.accepting.busy = {text: $("pcal-review-accept").textContent, disabled: $("pcal-review-accept").disabled,
    keepDisabled: $("pcal-review-keep").disabled};
  await sleep(1600);
  out.accepted = {confirms: P.adaptCalls.filter((c) => c.confirm).length, bar: bar(), toast: toast.textContent,
    events: events().filter((e) => e.kind !== "rescheduled")};
  $("pcal-next").click(); await sleep(150);
  out.accepted.nextWeek = events();
  $("pcal-today").click(); await sleep(150);

  // 5. Stale plan: Accept answers 409 -> proposal cleared, saved schedule intact, a fresh check offered.
  P.adaptMode = "conflict"; P.adaptCalls = [];
  P.sessionsByPlan["plan-1"].push(session("stats-missed", "stats.pdf", "quiz", "2026-09-24T10:00:00", "2026-09-24T10:30:00", 30));
  await loadPlannerData({keepWeek: true}); await sleep(400);
  const beforeConflict = JSON.stringify(P.sessionsByPlan);
  const eventsBefore = JSON.stringify(events().map((e) => [e.id, e.start, e.kind]));
  await reschedule("stats-missed");
  out.conflict = {barBefore: bar().shown};
  $("pcal-review-accept").click(); await sleep(1500);
  out.conflict = {...out.conflict, bar: bar(), modal: modal(), changes: events().filter((e) => e.change).length,
    unchanged: JSON.stringify(P.sessionsByPlan) === beforeConflict,
    sameEvents: JSON.stringify(events().map((e) => [e.id, e.start, e.kind])) === eventsBefore,
    notice: $("pcal-notice").hidden ? null : $("pcal-notice").textContent, recheck: !!$("pcal-review-recheck")};
  $("pcal-review-recheck").click(); await sleep(1200);
  out.conflict.recheck = {calls: P.adaptCalls.map((c) => !!c.confirm), bar: bar().shown, notice: !$("pcal-notice").hidden};
  $("pcal-review-keep").click(); await sleep(200);

  // 6. Changes outside the visible week: a pointer in the review bar; normal week controls get there.
  P.adaptMode = "large"; P.adaptCalls = [];
  P.sessionsByPlan = {"plan-1": [
    session("mkt-missed2", "mkt.pdf", "summary", "2026-09-24T09:00:00", "2026-09-24T09:45:00", 45),
    session("pbi-review", "pbi.pdf", "review", "2026-09-27T18:00:00", "2026-09-27T18:10:00", 10)]};
  P.largeProposal = {added: [{document_id: "mkt.pdf", activity_type: "review", scheduled_start: "2026-10-01T18:00:00",
      scheduled_end: "2026-10-01T18:20:00", duration_minutes: 20, reason_code: "low_quiz_score",
      message: "Low quiz score.", artifact_id: null, replaces_session_id: null}],
    moved: [], cancelled: [{session_id: "pbi-review", document_id: "pbi.pdf", activity_type: "review", scheduled_start: "2026-09-27T18:00:00",
      message: "Cancel the review session for \"PowerBI\": latest quiz scored 95%, it is no longer needed."}], significance: "large"};
  await loadPlannerData({keepWeek: true}); await sleep(400);
  await reschedule("mkt-missed2");
  out.nav = {range: $("pcal-range").textContent, bar: bar(), here: events().filter((e) => e.change).map((e) => e.change)};
  $("pcal-next").click(); await sleep(150);
  out.nav.next = {range: $("pcal-range").textContent, bar: bar(), here: events().filter((e) => e.change).map((e) => [e.change, e.tag, e.title])};
  out.nav.nextPopover = popover('.pcal-event[data-change="added"]');
  out.overflow.nav = noOverflow();
  $("pcal-review-keep").click(); await sleep(200);
  $("pcal-today").click(); await sleep(150);

  // 7. Everything in a later week: the review opens on that week.
  P.largeProposal = {...P.largeProposal, cancelled: []};
  await reschedule("mkt-missed2");
  out.jump = {monday: plannerDateKey(pcalWeekDates()[0]), bar: bar(), here: events().filter((e) => e.change).map((e) => e.change)};
  $("pcal-review-keep").click(); await sleep(200);

  // 8. A finished quiz with a large proposal: no navigation, no modal; the review waits in the Planner.
  P.largeProposal = null; P.adaptCalls = [];
  setPage("overview"); await sleep(400);
  await plannerAdaptAfterQuiz("mkt.pdf"); await sleep(300);
  out.quiz = {page: state.page, modal: modal(), toast: toast.textContent};
  setPage("planner"); await sleep(900);
  out.quiz.bar = bar();
  $("pcal-review-keep").click(); await sleep(200);
  out.quiz.writes = P.adaptCalls.filter((c) => c.confirm).length;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class CalendarReviewAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertTrue(self.out["desktop"])

    def test_small_adaptation_still_applies_automatically(self):
        small = self.out["small"]
        self.assertEqual(small["confirm"], [False])
        self.assertFalse(small["modal"])
        self.assertFalse(small["bar"]["shown"])
        self.assertEqual(small["changes"], 0)
        self.assertRegex(small["toast"], r"^Session skipped\. Plan adjusted: quiz added .+? 18:00\.$")
        self.assertTrue(small["replacement"])

    def test_large_adaptation_is_reviewed_on_the_calendar_without_a_modal(self):
        review = self.out["review"]
        self.assertEqual(review["page"], "planner")
        self.assertFalse(review["modal"])
        self.assertTrue(review["bar"]["shown"])
        self.assertFalse(review["bar"]["statusShown"])
        self.assertIn("3 schedule changes suggested", review["bar"]["text"])
        self.assertNotIn("change next week", review["bar"]["text"])
        self.assertEqual((review["bar"]["keep"], review["bar"]["accept"]), ("Keep current plan", "Accept changes"))
        self.assertTrue(review["overflow"])

    def test_added_session_is_a_ghost(self):
        added = [e for e in self.out["review"]["events"] if e["change"] == "added"]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]["kind"], added[0]["title"], added[0]["tag"]), ("proposed", "Marketing", "+ New"))
        self.assertEqual(added[0]["start"], "2026-09-24T18:00:00")
        self.assertTrue(added[0]["dashed"])
        self.assertFalse(added[0]["draggable"])

    def test_moved_session_shows_origin_and_destination(self):
        events = self.out["review"]["events"]
        moving = [e for e in events if e["change"] == "moving"]
        moved = [e for e in events if e["change"] == "moved"]
        self.assertEqual([(e["id"], e["start"], e["label"]) for e in moving], [("stats-quiz", "2026-09-25T18:00:00", "Moving")])
        self.assertFalse(moving[0]["draggable"])
        self.assertEqual([(e["title"], e["start"], e["label"], e["tag"], e["dashed"]) for e in moved],
                         [("Statistics", "2026-09-26T18:00:00", "Moved here", "→", True)])   # short block: glyph tag

    def test_cancelled_session_stays_visible_and_muted(self):
        cancelled = [e for e in self.out["review"]["events"] if e["change"] == "cancelled"]
        self.assertEqual([(e["id"], e["label"], e["struck"]) for e in cancelled], [("pbi-review", "No longer needed", True)])

    def test_popovers_explain_each_change_without_internal_codes(self):
        review = self.out["review"]
        added, moved, moving, cancelled = review["added"], review["moved"], review["moving"], review["cancelled"]
        self.assertEqual((added["title"], added["activity"]), ("Marketing", "Summary · 45m"))
        self.assertRegex(added["when"][0], r"^Proposed time.*24.*18:00–18:45$")
        self.assertEqual(added["reason"], "Why this change? Replaces the missed summary session.")
        self.assertEqual(added["actions"], 0)
        self.assertRegex(moved["when"][0], r"^Proposed time.*26.*18:00–18:30$")
        self.assertRegex(moved["when"][1], r"^Currently.*25.*18:00–18:30$")
        self.assertEqual(moved["reason"], "Why this change? Your latest quiz scored 40%.")
        self.assertEqual(moving["reason"], moved["reason"])
        self.assertEqual(cancelled["reason"], "Why this change? Your latest quiz scored 95% — this session is no longer needed.")
        for pop in (added, moved, moving, cancelled):
            self.assertTrue(pop["open"])
            self.assertNotRegex(pop["text"], r"session_missed|rescheduled|low_quiz|reason_code|stale|Move the|Cancel the|__")

    def test_keep_current_plan_writes_nothing(self):
        kept = self.out["kept"]
        self.assertFalse(kept["bar"]["shown"])
        self.assertTrue(kept["bar"]["statusShown"])
        self.assertEqual((kept["changes"], kept["calls"], kept["confirms"], kept["legacy"]), (0, 1, 0, 0))
        self.assertTrue(kept["unchanged"])
        self.assertEqual(kept["toast"], "Kept your current plan")

    def test_accept_applies_exactly_once_and_refreshes(self):
        accepting = self.out["accepting"]
        self.assertTrue(accepting["bar"]["shown"])
        self.assertFalse(accepting["modal"])
        self.assertEqual(accepting["busy"], {"text": "Applying…", "disabled": True, "keepDisabled": True})
        accepted = self.out["accepted"]
        self.assertEqual(accepted["confirms"], 1)
        self.assertFalse(accepted["bar"]["shown"])
        self.assertRegex(accepted["toast"], r"^Plan adjusted: ")
        self.assertFalse(any(e["change"] for e in accepted["events"]))
        starts = {(e["title"], e["start"]) for e in accepted["events"] if e["kind"] == "confirmed"}
        self.assertIn(("Marketing", "2026-09-24T18:00:00"), starts)
        self.assertIn(("Statistics", "2026-09-26T18:00:00"), starts)
        self.assertFalse(any(e["title"] == "PowerBI" for e in accepted["events"]))

    def test_conflict_clears_the_proposal_without_partial_writes(self):
        conflict = self.out["conflict"]
        self.assertTrue(conflict["barBefore"])
        self.assertFalse(conflict["bar"]["shown"])
        self.assertFalse(conflict["modal"])
        self.assertEqual(conflict["changes"], 0)
        self.assertTrue(conflict["unchanged"])
        self.assertTrue(conflict["sameEvents"])
        self.assertEqual(conflict["notice"], "Your plan changed. Review the latest schedule.Check for new suggestions")
        recheck = conflict["recheck"]
        self.assertEqual(recheck["calls"], [False, True, False])   # preview, the failed apply, a fresh preview
        self.assertTrue(recheck["bar"])
        self.assertFalse(recheck["notice"])

    def test_changes_in_another_week_are_pointed_to(self):
        nav = self.out["nav"]
        self.assertIn("2 schedule changes suggested", nav["bar"]["text"])
        self.assertIn("1 change next week", nav["bar"]["text"])
        self.assertEqual(nav["here"], ["cancelled"])
        self.assertIn("1 change last week", nav["next"]["bar"]["text"])
        self.assertEqual(nav["next"]["here"], [["added", "+", "Marketing"]])
        self.assertEqual(nav["nextPopover"]["reason"], "Why this change? Your latest quiz shows this needs more practice.")
        jump = self.out["jump"]
        self.assertEqual(jump["monday"], "2026-09-28")
        self.assertEqual(jump["here"], ["added"])
        self.assertNotIn("next week", jump["bar"]["text"])

    def test_quiz_proposal_waits_in_the_planner(self):
        quiz = self.out["quiz"]
        self.assertEqual(quiz["page"], "overview")
        self.assertFalse(quiz["modal"])
        self.assertEqual(quiz["toast"], "3 schedule changes suggested. Review them in Study Planner.")
        self.assertTrue(quiz["bar"]["shown"])
        self.assertEqual(quiz["writes"], 0)

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarReviewWideTests(CalendarReviewAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1440, 900, MOCK, DRIVER)

    def test_runs_at_1440(self):
        self.assertEqual(self.out["width"], 1440)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class CalendarReviewNarrowTests(CalendarReviewAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)

    def test_runs_at_1100(self):
        self.assertEqual(self.out["width"], 1100)


if __name__ == "__main__":
    unittest.main()
