"""Frontend side of the post-upload Study Planner auto-include (frontend/js/planner.js, tutor.js).

Runs the real loadPlannerData / plannerAfterUpload / plannerRetryInclude source in a Node `vm`
sandbox with a stubbed API, so the ordering rules are checked without a browser: an older planner
reload that resolves last never overwrites a newer one, a failed auto-include shows a retry that
re-sends only those documents, and "unchanged" (duplicate upload) triggers no reload at all.
Skipped when Node.js is not installed.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
NODE = shutil.which("node")


def function_source(text: str, name: str) -> str:
    match = re.search(rf"^(async )?function {name}\(.*?^}}\n", text, re.S | re.M)
    assert match, name
    return match.group(0)


HARNESS = r"""
const vm = require("vm");
const sources = JSON.parse(process.argv[process.argv.length - 1]);
const log = [];
const pending = [];
const sandbox = {
  log, pending, console, Map, Promise, JSON, encodeURIComponent,
  PLANNER_PLANS_API_URL: "/plans", PLANNER_AVAILABILITY_API_URL: "/availability",
  PLANNER_INCLUDE_DOCUMENTS_API_URL: "/include", PLANNER_HISTORY_SESSION_STATUSES: ["completed"],
  document: { body: { dataset: { page: "planner" } } },
  plannerLoadSeq: 0, plannerIncludeFailure: null, plannerAvailability: [], plannerPlan: null, plannerMaterials: [],
  plannerSessions: [], plannerHistorySessions: [], plannerSessionsById: new Map(), plannerPlanWeekStart: null,
  plannerStep: "materials", plannerCalWeekStart: null, plannerPlacements: [], plannerPlacementsPlanId: null,
  plannerIsActiveSession: () => true, loadPlannerPlanProgress() {}, renderPlanner() { log.push("render"); },
  plannerIsDesktop: () => false, plannerLoadGoogleCalendar() {}, showToast: (text) => log.push(`toast:${text}`),
  plannerNewMaterialSeq: 0, plannerNewMaterialFailure: null, plannerUtcOffsetMinutes: () => 0, renderPlannerWorkspace() {},
  plannerStartCalendarReview: (planId, trigger, proposal) => log.push(`review:${planId}:${trigger.kind}:${proposal.change_count}`),
  plannerShowAdaptationReview: (planId, trigger, proposal) => log.push(`review:${planId}:${trigger.kind}:${proposal.change_count}`),
  // Every request waits until the test resolves it, so responses can arrive in any order.
  plannerRequest: (url, options) => new Promise((resolve, reject) => pending.push({ url, options, resolve, reject })),
};
vm.createContext(sandbox);
vm.runInContext(sources.join("\n") + "\nthis.api = { loadPlannerData, plannerAfterUpload, plannerRetryInclude, plannerProposeNewMaterials };", sandbox);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const answer = async (url, body) => {
  const index = pending.findIndex((item) => item.url === url);
  pending.splice(index, 1)[0].resolve(body);
  await tick();
};
const plan = (id, docs) => ({ plans: [{ plan_id: id, status: "active" }], detail: { materials: docs.map((d) => ({ document_id: d })) } });

(async () => {
  const out = {};
  // 1) Two uploads finish close together: the older reload resolves LAST and must be ignored.
  sandbox.api.plannerAfterUpload({ status: "updated", plan_id: "p", added_document_ids: ["a.pdf"] });   // reload #1
  sandbox.api.plannerAfterUpload({ status: "updated", plan_id: "p", added_document_ids: ["b.pdf"] });   // reload #2
  const take = (url) => pending.filter((i) => i.url === url).map((i) => pending.splice(pending.indexOf(i), 1)[0]);
  const [plans, availability] = [take("/plans"), take("/availability")];   // [reload #1, reload #2] each
  const ab = plan("p", ["a.pdf", "b.pdf"]), a = plan("p", ["a.pdf"]);
  plans[1].resolve(ab.plans); availability[1].resolve([]); await tick();          // newer reload first
  await answer("/plans/p", ab.detail); await answer("/plans/p/sessions", []);
  plans[0].resolve(a.plans); availability[0].resolve([]); await tick();           // the older one lands last
  out.afterRace = sandbox.plannerMaterials.map((m) => m.document_id);
  out.leftover = pending.map((i) => i.url);

  // 2) A duplicate upload ("unchanged") does not reload anything.
  sandbox.api.plannerAfterUpload({ status: "unchanged", plan_id: null, added_document_ids: [] });
  out.unchangedRequests = pending.length;

  // 3) A failed auto-include keeps the ids for a retry, which re-sends exactly those documents.
  sandbox.api.plannerAfterUpload({ status: "failed", document_ids: ["c.pdf"], message: "Plan not updated." });
  out.failure = sandbox.plannerIncludeFailure;
  pending.splice(0);   // that failure's reload is irrelevant here
  const retry = sandbox.api.plannerRetryInclude(null);
  await tick();
  out.retryBody = pending[0].options.body;
  await answer("/include", { status: "updated", plan_id: "p", added_document_ids: ["c.pdf"] });
  out.failureAfterRetry = sandbox.plannerIncludeFailure;
  pending.splice(0);   // the retry's own reload is irrelevant here

  // 4) Accepted plan + new PDF: reload, then a "materials_added" proposal goes to the calendar review.
  const live = { plans: [{ plan_id: "live", status: "active" }], detail: { materials: [{ document_id: "a.pdf" }, { document_id: "b.pdf" }] } };
  const accepted = [{ session_id: "s1", status: "scheduled" }];
  const reviewsBefore = log.filter((x) => x.startsWith("review:")).length;
  sandbox.api.plannerAfterUpload({ status: "updated", plan_id: "live", added_document_ids: ["b.pdf"] });
  await answer("/plans", live.plans); await answer("/availability", []);
  await answer("/plans/live", live.detail); await answer("/plans/live/sessions", accepted);
  const previewCall = pending.find((i) => i.url === "/plans/live/adaptation/preview");
  out.proposalTrigger = previewCall && previewCall.options.body.trigger;
  await answer("/plans/live/adaptation/preview", { change_count: 3, added: [], moved: [], cancelled: [] });
  out.reviews = log.filter((x) => x.startsWith("review:")).slice(reviewsBefore);

  // 5) The proposal request fails: the accepted plan is untouched, a retry is offered, no review.
  const failed = sandbox.api.plannerProposeNewMaterials("live");
  await tick();
  pending.splice(pending.findIndex((i) => i.url === "/plans/live/adaptation/preview"), 1)[0].reject(new Error("boom"));
  await failed;
  out.newMaterialFailure = sandbox.plannerNewMaterialFailure;
  out.reviewsAfterFailure = log.filter((x) => x.startsWith("review:")).length - reviewsBefore;

  // 6) Two proposals in flight: the older answer, arriving last, is ignored; success clears the failure.
  const one = sandbox.api.plannerProposeNewMaterials("live");
  const two = sandbox.api.plannerProposeNewMaterials("live");
  await tick();
  const [first, second] = take("/plans/live/adaptation/preview");
  second.resolve({ change_count: 2 }); await two;
  first.resolve({ change_count: 1 }); await one;
  out.raceReviews = log.filter((x) => x.startsWith("review:")).slice(reviewsBefore + 1);
  out.failureCleared = sandbox.plannerNewMaterialFailure;

  // 7) A plan that was never accepted: the reload alone re-runs the preview; no proposal request.
  sandbox.api.plannerAfterUpload({ status: "updated", plan_id: "draft", added_document_ids: ["c.pdf"] });
  await answer("/plans", [{ plan_id: "draft", status: "active" }]); await answer("/availability", []);
  await answer("/plans/draft", { materials: [{ document_id: "c.pdf" }] }); await answer("/plans/draft/sessions", []);
  out.draftRequests = pending.map((i) => i.url);
  out.log = log;
  console.log(JSON.stringify(out));
})().catch((error) => { console.log(JSON.stringify({ fatal: String(error && error.stack || error) })); });
"""


@unittest.skipUnless(NODE, "Node.js is not installed")
class PlannerAutoIncludeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        planner = (FRONTEND / "js" / "planner.js").read_text(encoding="utf-8")
        sources = [function_source(planner, name) for name in (
            "loadPlannerData", "plannerAfterUpload", "plannerRetryInclude", "plannerAfterMaterialsAdded",
            "plannerProposeNewMaterials", "plannerHasLivePlan")]
        result = subprocess.run([NODE, "-e", HARNESS, json.dumps(sources)], capture_output=True, text=True,
                                encoding="utf-8", timeout=60)
        cls.out = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {"fatal": result.stderr}

    def test_no_errors(self):
        self.assertNotIn("fatal", self.out)

    def test_an_older_reload_never_overwrites_newer_suggestions(self):
        self.assertEqual(self.out["afterRace"], ["a.pdf", "b.pdf"])
        self.assertEqual(self.out["leftover"], [])   # the stale reload stopped before fetching its plan detail

    def test_duplicate_upload_triggers_no_reload(self):
        self.assertEqual(self.out["unchangedRequests"], 0)

    def test_failure_offers_a_retry_of_exactly_those_documents(self):
        self.assertEqual(self.out["failure"], {"documentIds": ["c.pdf"], "message": "Plan not updated."})
        self.assertEqual(self.out["retryBody"], {"document_ids": ["c.pdf"]})
        self.assertIsNone(self.out["failureAfterRetry"])
        self.assertIn("toast:Study plan updated", self.out["log"])

    def test_accepted_plan_gets_a_reviewable_proposal_for_the_new_material(self):
        self.assertEqual(self.out["proposalTrigger"], {"kind": "materials_added"})
        self.assertEqual(self.out["reviews"], ["review:live:materials_added:3"])

    def test_failed_proposal_keeps_the_plan_and_offers_a_retry(self):
        self.assertEqual(self.out["newMaterialFailure"], {"planId": "live"})
        self.assertEqual(self.out["reviewsAfterFailure"], 1)   # only the earlier review; none for the failure
        self.assertIn("toast:Could not suggest sessions for your new material. Try again in Study Planner.", self.out["log"])

    def test_an_older_proposal_never_replaces_a_newer_one(self):
        self.assertEqual(self.out["raceReviews"], ["review:live:materials_added:2"])
        self.assertIsNone(self.out["failureCleared"])

    def test_unaccepted_plan_gets_no_proposal_request(self):
        self.assertEqual(self.out["draftRequests"], [])

    def test_upload_hands_the_planner_result_to_the_planner(self):
        tutor = (FRONTEND / "js" / "tutor.js").read_text(encoding="utf-8")
        self.assertIn("plannerAfterUpload(data.planner)", tutor)
        self.assertLess(tutor.index("plannerAfterUpload(data.planner)"), tutor.index("} catch (error) {\n    const detail = error.message"))


if __name__ == "__main__":
    unittest.main()
