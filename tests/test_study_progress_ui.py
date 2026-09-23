"""Real-browser tests for Phase 6A progress (headless Chrome, mocked API): the Progress tab's
Where you are / Quiz results / Study plan panels, the Quiz coverage fix (concept_coverage_ratio),
the Home "Quiz performance" stat and the Planner's plan-progress line. Desktop + 390px."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_today_ui import MOCK as TODAY_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = TODAY_MOCK + r"""
const progressFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  if (p === "/api/dashboard") return json({summary: {}, latest_attempt: null,
    metrics: {documents: 3, total_topics: 2, topics_assessed: 1, topics_mastered: 0, answered_questions: 12,
              current_quiz_performance: {average_percentage: 72.5, assessed_documents: 2, documents: 3}},
    mastery: [{document_id: "mkt.pdf", document_name: "Marketing", topic_id: "t1", topic_name: "Branding",
               mastery_level: "Developing", mastery_score: 0.55, has_evidence: true, has_sufficient_evidence: true,
               concept_coverage_ratio: 0.6, answered_questions: 12},
              {document_id: "mkt.pdf", document_name: "Marketing", topic_id: "t2", topic_name: "Pricing",
               mastery_level: "Insufficient evidence", mastery_score: 0.3, has_evidence: true, has_sufficient_evidence: false,
               concept_coverage_ratio: 0.25, answered_questions: 3}],
    materials: [{document_id: "mkt.pdf", document_name: "Marketing", topic_count: 2, assessed_topic_count: 2}]});
  if (p === "/api/progress/documents/mkt.pdf") return json({document_id: "mkt.pdf", title: "Marketing",
    learning: {state: "learning", label: "Learning", explanation: "Latest quiz score 7/10 (70%); keep practicing."},
    quiz: {latest: {score: 7, total: 10, percentage: 70, completed_at: "2026-09-23T10:00:00+00:00"},
           attempts: [{percentage: 40, score: 4, total: 10, completed_at: "2026-09-20T10:00:00+00:00"},
                      {percentage: 55, score: 5.5, total: 10, completed_at: "2026-09-21T10:00:00+00:00"},
                      {percentage: 70, score: 7, total: 10, completed_at: "2026-09-23T10:00:00+00:00"}],
           attempt_count: 3, trend: "improving"},
    flashcards: {card_count: 12},
    plan: {plan_id: "plan-1", plan_title: "Now", deadline: "2026-10-01", completed_sessions: 2, planned_sessions: 5,
           completed_minutes: 75, remaining_minutes: 90, next_session: {session_id: "s9", document_id: "mkt.pdf",
           activity_type: "quiz", scheduled_start: "2026-09-25T18:00:00", scheduled_end: "2026-09-25T18:30:00",
           duration_minutes: 30, status: "scheduled"}}});
  if (p === "/api/progress/documents/stats.pdf") return json({document_id: "stats.pdf", title: "Statistics",
    learning: {state: "new", label: "Not started", explanation: "Not studied yet."},
    quiz: {latest: null, attempts: [], attempt_count: 0, trend: null}, flashcards: {card_count: 0}, plan: null});
  if (p === "/api/progress/documents/pbi.pdf") return json({document_id: "pbi.pdf", title: "PowerBI",
    learning: {state: "learning", label: "Learning", explanation: "Latest quiz score 5/10 (50%)."},
    quiz: {latest: {score: 5, total: 10, percentage: 50, completed_at: "2026-09-22T10:00:00+00:00"},
           attempts: [{percentage: 50, score: 5, total: 10, completed_at: "2026-09-22T10:00:00+00:00"}], attempt_count: 1, trend: null},
    flashcards: {card_count: 0}, plan: null});
  if (/^\/api\/planner\/plans\/[^/]+\/progress$/.test(p)) return json({plan_id: "plan-1", completed_sessions: 3,
    planned_sessions: 8, completed_minutes: 90, remaining_minutes: 150, next_session: null, documents: [],
    current_quiz_performance: {average_percentage: 72.5, assessed_documents: 2, documents: 3}});
  return progressFetch(input, init);
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
const lines = (id) => [...document.getElementById(id).children].map((el) => el.textContent);
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1;
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  setPage("overview"); await sleep(300);
  out.kpis = [...document.querySelectorAll("#overview-kpis .stat-card")].map((card) => [...card.children].map((el) => el.textContent));

  await openStudySession("mkt.pdf", "progress"); await sleep(600);
  out.mkt = {state: lines("session-progress-state"), quiz: lines("session-progress-quiz"), plan: lines("session-progress-plan"),
    coverage: [...document.querySelectorAll("#session-coverage-list .mastery-card span")].map((el) => el.textContent),
    headings: [...document.querySelectorAll('[data-session-pane="progress"] h2')].map((h) => h.textContent)};
  out.overflow.progress = noOverflow();

  await openStudySession("stats.pdf", "progress"); await sleep(600);
  out.stats = {state: lines("session-progress-state"), quiz: lines("session-progress-quiz"), plan: lines("session-progress-plan")};
  await openStudySession("pbi.pdf", "progress"); await sleep(600);
  out.pbi = {quiz: lines("session-progress-quiz")};
  out.overflow.empty = noOverflow();

  window.__planner.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  window.__planner.sessionsByPlan = {"plan-1": [{session_id: "s1", document_id: "mkt.pdf", document_title: "Marketing",
    activity_type: "quiz", scheduled_start: "2026-09-25T18:00:00", scheduled_end: "2026-09-25T18:30:00", duration_minutes: 30,
    status: "scheduled", reason: {code: "new_material", message: "New material to learn"}, artifact_id: null}]};
  setPage("planner"); await sleep(700);
  out.planProgress = document.getElementById("planner-plan-progress").hidden ? null : document.getElementById("planner-plan-progress").textContent;
  out.overflow.planner = noOverflow();
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


class ProgressUiAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_home_shows_current_quiz_performance_not_overall_accuracy(self):
        labels = {card[0]: card[1:] for card in self.out["kpis"]}
        self.assertNotIn("Overall accuracy", labels)
        self.assertEqual(labels["Quiz performance"], ["73%", "Latest quiz across 2 documents"])

    def test_progress_panels_show_real_values(self):
        mkt = self.out["mkt"]
        self.assertEqual(mkt["headings"][:3], ["Learning state", "Latest score", "Sessions"])
        self.assertEqual(mkt["state"], ["Learning", "Latest quiz score 7/10 (70%); keep practicing.", "12 flashcards ready"])
        self.assertEqual(mkt["quiz"][0], "70%")
        self.assertRegex(mkt["quiz"][1], r"^7/10 correct · ")
        self.assertEqual(mkt["quiz"][2], "Up 15 points from your previous attempt")
        self.assertEqual(mkt["quiz"][3], "Earlier attempts")
        self.assertRegex(mkt["quiz"][4], r"^55% · .+40% · ")    # newest first
        self.assertEqual(mkt["plan"][0], "2 of 5 sessions done")
        self.assertEqual(mkt["plan"][1], "1h 15m studied · 1h 30m still planned")
        self.assertRegex(mkt["plan"][2], r"^Next: Quiz · .*25.*, 18:00$")
        self.assertRegex(mkt["plan"][3], r"^Deadline: .*1")
        for text in mkt["state"] + mkt["quiz"] + mkt["plan"]:
            self.assertNotRegex(text.lower(), r"mastery \d|priority|flashcards? (mastered|progress)")

    def test_quiz_coverage_uses_the_real_ratio(self):
        self.assertIn("Quiz coverage", self.out["mkt"]["headings"])
        self.assertEqual(self.out["mkt"]["coverage"], [
            "60% of this topic's key concepts covered by your quizzes",
            "25% of this topic's key concepts covered by your quizzes · a few more questions give a reliable picture"])
        self.assertNotIn("Coverage is pending", " ".join(self.out["mkt"]["coverage"]))

    def test_empty_states_without_quiz_or_plan(self):
        stats = self.out["stats"]
        self.assertEqual(stats["state"], ["Not started", "Not studied yet."])
        self.assertEqual(stats["quiz"], ["No completed quiz yet. Your results will appear here after your first quiz."])
        self.assertEqual(stats["plan"], ["This document isn't part of an active study plan."])

    def test_one_attempt_is_not_interpreted_as_a_trend(self):
        quiz = self.out["pbi"]["quiz"]
        self.assertEqual(quiz[0], "50%")
        self.assertEqual(quiz[2], "One completed attempt so far. A later attempt will show how your score changes.")
        self.assertEqual(len(quiz), 3)

    def test_planner_shows_plan_progress(self):
        self.assertEqual(self.out["planProgress"],
                         "3 of 8 sessions done · 1h 30m studied, 2h 30m still planned · quiz performance 73% across 2 of 3 documents")

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()), self.out["overflow"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ProgressUiDesktopTests(ProgressUiAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ProgressUiPhoneTests(ProgressUiAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_runs_at_phone_width(self):
        self.assertEqual(self.out["width"], 390)


if __name__ == "__main__":
    unittest.main()
