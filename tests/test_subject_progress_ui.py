"""SUBJECT-STUDY-PROGRESS on Home (desktop, 1280px, headless Chrome, mocked API).

Each subject shows completed/total saved Planner sessions and a progress bar, summed from its
documents' own progress (/api/progress/documents/{id} -> plan); a subject expands to per-PDF rows;
Unassigned appears once its documents have planned sessions. The figures follow a completed
session, a quiz submission (dashboard reload), a document moved to another subject and a deleted
subject. Skipped without Chrome."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_subject_management_ui import MOCK as SUBJECT_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = SUBJECT_MOCK + r"""
// Per-document plan figures, editable by the driver (null = not in an active plan).
window.__plans = {"mkt.pdf": [2, 5], "stats.pdf": null, "pbi.pdf": [1, 3]};
window.__progressCalls = 0;
window.__failing = new Set();   // documents whose progress request fails
const subjectProgressFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const p = new URL(typeof input === "string" ? input : input.url, "http://x").pathname;
  const match = p.match(/^\/api\/progress\/documents\/(.+)$/);
  if (!match) return subjectProgressFetch(input, init);
  window.__progressCalls += 1;
  if (window.__failing.has(decodeURIComponent(match[1]))) return json({detail: "Progress is unavailable"}, 500);
  const data = await (await subjectProgressFetch(input, init)).json();
  const figures = window.__plans[decodeURIComponent(match[1])];
  data.plan = figures ? {...(data.plan || {plan_id: "plan-1", plan_title: "Now", deadline: null, completed_minutes: 0, remaining_minutes: 0, next_session: null}),
                         completed_sessions: figures[0], planned_sessions: figures[1]} : null;
  return json(data);
};
"""

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: []};
const publish = () => { const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre); };
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const rows = () => [...document.querySelectorAll("#home-subject-progress .subject-progress-item")].map((item) => ({
  key: item.dataset.subjectProgress, name: item.querySelector(".subject-progress-name").textContent,
  count: item.querySelector(".subject-progress-head .subject-progress-count").textContent,
  bar: item.querySelector(".subject-progress-head .subject-progress-bar").getAttribute("aria-valuenow") + "/" +
       item.querySelector(".subject-progress-head .subject-progress-bar").getAttribute("aria-valuemax"),
  expanded: item.querySelector(".subject-progress-toggle").getAttribute("aria-expanded"),
  docs: item.querySelector(".subject-progress-docs").hidden ? null
    : [...item.querySelectorAll(".subject-progress-doc")].map((doc) => [doc.dataset.documentId, doc.querySelector(".subject-progress-count").textContent])}));
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  setPage("overview"); await sleep(900);
  out.initial = {rows: rows(), title: document.getElementById("home-subject-progress-title")?.textContent};
  document.querySelector('.subject-progress-item[data-subject-progress="bio"] .subject-progress-toggle').click(); await sleep(100);
  out.expanded = rows();

  window.__plans["mkt.pdf"] = [3, 5];   // a session was completed (Home's or the Planner's action)
  await plannerRefreshSchedules(); await sleep(500);
  out.afterSession = rows();

  window.__plans["stats.pdf"] = [0, 2];  // a quiz submission reloads the dashboard (and re-planned)
  await loadDashboard(); await sleep(500);
  out.afterQuiz = rows();

  await moveDocumentsToSubject(["pbi.pdf"], "bio"); await sleep(300);
  out.afterMove = rows();
  window.confirm = () => true;
  await deleteSubject(subjectById("bio")); await sleep(300);
  out.afterDelete = rows();

  // A failed progress request: never counted as "not planned"; marked, explained and retryable.
  window.__failing.add("stats.pdf");
  refreshHomeDocumentStates(); await sleep(500);
  document.querySelector('.subject-progress-item[data-subject-progress="unassigned"] .subject-progress-toggle').click(); await sleep(100);
  out.failed = {rows: rows(), warning: document.querySelector(".subject-progress-warning")?.textContent || null};
  window.__failing.clear();
  document.querySelector(".subject-progress-retry").click(); await sleep(500);
  out.retried = {rows: rows(), warning: document.querySelector(".subject-progress-warning")?.textContent || null};
  out.overflow = document.documentElement.scrollWidth <= window.innerWidth + 1;
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SubjectProgressDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        original = (sidebar_harness.MOCK, sidebar_harness.DRIVER)
        sidebar_harness.MOCK, sidebar_harness.DRIVER = MOCK, DRIVER
        try:
            cls.out = sidebar_harness.run_at_width(1280, 900)
        finally:
            sidebar_harness.MOCK, sidebar_harness.DRIVER = original

    def summary(self, key):
        return [(row["key"], row["count"], row["bar"]) for row in self.out[key]]

    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])
        self.assertTrue(self.out["overflow"])

    def test_completed_and_total_per_subject_with_unassigned(self):
        self.assertEqual(self.out["initial"]["title"], "Current plan progress by subject")
        # Biology = Marketing 2/5 + Statistics (not planned); Physics has no documents; Unassigned = PowerBI 1/3.
        self.assertEqual([(row["key"], row["count"], row["bar"]) for row in self.out["initial"]["rows"]],
                         [("bio", "2 of 5 sessions done", "2/5"), ("unassigned", "1 of 3 sessions done", "1/3")])

    def test_expanding_a_subject_lists_each_pdf(self):
        bio = next(row for row in self.out["expanded"] if row["key"] == "bio")
        self.assertEqual(bio["expanded"], "true")
        self.assertEqual(sorted(bio["docs"]), [["mkt.pdf", "2 of 5 sessions done"], ["stats.pdf", "Not in your study plan"]])

    def test_updates_after_session_completion_and_quiz_submission(self):
        self.assertEqual(self.summary("afterSession")[0], ("bio", "3 of 5 sessions done", "3/5"))
        self.assertEqual(self.summary("afterQuiz")[0], ("bio", "3 of 7 sessions done", "3/7"))
        self.assertEqual(sorted(self.out["afterQuiz"][0]["docs"]), [["mkt.pdf", "3 of 5 sessions done"], ["stats.pdf", "0 of 2 sessions done"]])

    def test_failed_request_marks_totals_incomplete_instead_of_zero(self):
        failed = self.out["failed"]
        row = failed["rows"][0]
        self.assertEqual(row["key"], "unassigned")   # (after the subject was deleted) every document is here
        self.assertEqual(row["count"], "4 of 8 sessions done · incomplete")
        self.assertIn(["stats.pdf", "Progress unavailable"], row["docs"])
        self.assertIn("Progress for 1 document could not be loaded", failed["warning"])

    def test_try_again_restores_complete_totals(self):
        self.assertIsNone(self.out["retried"]["warning"])
        self.assertEqual(self.out["retried"]["rows"][0]["count"], "4 of 10 sessions done")

    def test_updates_after_reassignment_and_subject_deletion(self):
        self.assertEqual(self.summary("afterMove"), [("bio", "4 of 10 sessions done", "4/10")])   # Unassigned now empty
        self.assertEqual(self.summary("afterDelete"), [("unassigned", "4 of 10 sessions done", "4/10")])


if __name__ == "__main__":
    unittest.main()
