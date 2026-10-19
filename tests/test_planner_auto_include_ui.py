"""Frontend side of explicit Study Planner material selection (frontend/js/planner.js, tutor.js).

An upload never touches the planner: tutor.js no longer hands anything to it. Add Material on an
accepted plan asks for a reviewable "materials_added" proposal (a deadline goal keeps its
"deadline_changed" proposal); on a plan not accepted yet it only re-runs the read-only preview.
Runs the real planner.js functions in a Node `vm` sandbox with a stubbed API. Skipped when Node.js
is not installed.
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
  log, pending, console, Map, Promise, JSON, Object, encodeURIComponent,
  PLANNER_PLANS_API_URL: "/plans", PLANNER_AVAILABILITY_API_URL: "/availability", PLANNER_HISTORY_SESSION_STATUSES: ["completed"],
  document: { body: { dataset: { page: "planner" } } },
  plannerLoadSeq: 0, plannerAvailability: [], plannerPlan: null, plannerMaterials: [],
  plannerSessions: [], plannerHistorySessions: [], plannerSessionsById: new Map(), plannerPlanWeekStart: null,
  plannerStep: "materials", plannerCalWeekStart: null, plannerPlacements: [], plannerPlacementsPlanId: null,
  plannerIsActiveSession: () => true, loadPlannerPlanProgress() {}, renderPlanner() { log.push("render"); },
  plannerIsDesktop: () => false, plannerLoadGoogleCalendar() {}, showToast: (text) => log.push(`toast:${text}`),
  plannerNewMaterialSeq: 0, plannerNewMaterialFailure: null, plannerUtcOffsetMinutes: () => 0, renderPlannerWorkspace() {},
  pcalSheetGoals: new Map(), plannerQueueAutoPreview: () => log.push("preview"),
  plannerStartCalendarReview: (planId, trigger, proposal) => log.push(`review:${planId}:${trigger.kind}:${proposal.change_count}`),
  plannerShowAdaptationReview: (planId, trigger, proposal) => log.push(`review:${planId}:${trigger.kind}:${proposal.change_count}`),
  // Every request waits until the test resolves it, so responses can arrive in any order.
  plannerRequest: (url, options) => new Promise((resolve, reject) => pending.push({ url, options, resolve, reject })),
};
vm.createContext(sandbox);
vm.runInContext(sources.join("\n") + "\nthis.api = { loadPlannerData, pcalAddMaterial, plannerProposeNewMaterials };", sandbox);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const answer = async (url, body) => {
  const index = pending.findIndex((item) => item.url === url);
  pending.splice(index, 1)[0].resolve(body);
  await tick();
};
const take = (url) => pending.filter((i) => i.url === url).map((i) => pending.splice(pending.indexOf(i), 1)[0]);
const plan = (id, docs) => ({ plans: [{ plan_id: id, status: "active" }], detail: { materials: docs.map((d) => ({ document_id: d })) } });

(async () => {
  const out = {};
  // 1) Two reloads close together: the older one resolves LAST and must be ignored.
  sandbox.api.loadPlannerData({ keepWeek: true });
  sandbox.api.loadPlannerData({ keepWeek: true });
  const [plans, availability] = [take("/plans"), take("/availability")];
  const ab = plan("p", ["a.pdf", "b.pdf"]), a = plan("p", ["a.pdf"]);
  plans[1].resolve(ab.plans); availability[1].resolve([]); await tick();
  await answer("/plans/p", ab.detail); await answer("/plans/p/sessions", []);
  plans[0].resolve(a.plans); availability[0].resolve([]); await tick();
  out.afterRace = sandbox.plannerMaterials.map((m) => m.document_id);
  out.leftover = pending.map((i) => i.url);

  // 2) Add Material on a plan not accepted yet: only the read-only preview runs again.
  sandbox.plannerPlan = { plan_id: "draft" }; sandbox.plannerMaterials = []; sandbox.plannerSessions = [];
  const logBefore = log.length;
  const draft = sandbox.api.pcalAddMaterial("c.pdf", {}, null);
  await tick();
  await answer("/plans/draft/materials", { document_id: "c.pdf" });
  await draft;
  out.draftRequests = pending.map((i) => i.url);
  out.draftLog = log.slice(logBefore);

  // 3) Add Material on an accepted plan: a "materials_added" proposal goes to the calendar review.
  sandbox.plannerPlan = { plan_id: "live" }; sandbox.plannerSessions = [{ session_id: "s1", status: "scheduled" }];
  const added = sandbox.api.pcalAddMaterial("b.pdf", {}, null);
  await tick();
  out.addBody = pending.find((i) => i.url === "/plans/live/materials").options.body;
  await answer("/plans/live/materials", { document_id: "b.pdf" });
  const previewCall = pending.find((i) => i.url === "/plans/live/adaptation/preview");
  out.proposalTrigger = previewCall && previewCall.options.body.trigger;
  await answer("/plans/live/adaptation/preview", { change_count: 3, added: [], moved: [], cancelled: [] });
  await added;
  out.reviews = log.filter((x) => x.startsWith("review:"));

  // 4) A deadline goal on an accepted plan keeps its deadline proposal.
  const dated = sandbox.api.pcalAddMaterial("d.pdf", { deadline: "2026-10-20" }, null);
  await tick();
  await answer("/plans/live/materials", { document_id: "d.pdf", deadline: "2026-10-20" });
  const deadlineCall = pending.find((i) => i.url === "/plans/live/adaptation/preview");
  out.deadlineTrigger = deadlineCall && deadlineCall.options.body.trigger;
  await answer("/plans/live/adaptation/preview", { change_count: 0 });
  await dated;

  // 5) The proposal request fails: the accepted plan is untouched, a retry is offered, no review.
  const reviewsBefore = log.filter((x) => x.startsWith("review:")).length;
  const failed = sandbox.api.plannerProposeNewMaterials("live");
  await tick();
  take("/plans/live/adaptation/preview")[0].reject(new Error("boom"));
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
  out.raceReviews = log.filter((x) => x.startsWith("review:")).slice(reviewsBefore);
  out.failureCleared = sandbox.plannerNewMaterialFailure;
  out.log = log;
  console.log(JSON.stringify(out));
})().catch((error) => { console.log(JSON.stringify({ fatal: String(error && error.stack || error) })); });
"""


@unittest.skipUnless(NODE, "Node.js is not installed")
class PlannerMaterialSelectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        planner = (FRONTEND / "js" / "planner.js").read_text(encoding="utf-8")
        sources = [function_source(planner, name) for name in (
            "loadPlannerData", "plannerPlanUrl", "plannerEnsurePlan", "pcalAddMaterial", "plannerSuggestAdaptation",
            "plannerProposeNewMaterials", "plannerHasLivePlan")]
        result = subprocess.run([NODE, "-e", HARNESS, json.dumps(sources)], capture_output=True, text=True,
                                encoding="utf-8", timeout=60)
        cls.out = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {"fatal": result.stderr}

    def test_no_errors(self):
        self.assertNotIn("fatal", self.out)

    def test_an_older_reload_never_overwrites_a_newer_one(self):
        self.assertEqual(self.out["afterRace"], ["a.pdf", "b.pdf"])
        self.assertEqual(self.out["leftover"], [])   # the stale reload stopped before fetching its plan detail

    def test_adding_to_an_unaccepted_plan_only_refreshes_the_preview(self):
        self.assertEqual(self.out["draftRequests"], [])
        self.assertEqual(self.out["draftLog"], ["preview"])

    def test_adding_to_an_accepted_plan_gets_a_reviewable_proposal(self):
        self.assertEqual(self.out["addBody"], {"document_id": "b.pdf"})
        self.assertEqual(self.out["proposalTrigger"], {"kind": "materials_added"})
        self.assertEqual(self.out["reviews"], ["review:live:materials_added:3"])

    def test_a_deadline_goal_keeps_the_deadline_proposal(self):
        self.assertEqual(self.out["deadlineTrigger"], {"kind": "deadline_changed", "document_id": "d.pdf"})

    def test_failed_proposal_keeps_the_plan_and_offers_a_retry(self):
        self.assertEqual(self.out["newMaterialFailure"], {"planId": "live"})
        self.assertEqual(self.out["reviewsAfterFailure"], 0)
        self.assertIn("toast:Could not suggest sessions for your new material. Try again in Study Planner.", self.out["log"])

    def test_an_older_proposal_never_replaces_a_newer_one(self):
        self.assertEqual(self.out["raceReviews"], ["review:live:materials_added:2"])
        self.assertIsNone(self.out["failureCleared"])

    def test_upload_never_hands_anything_to_the_planner(self):
        tutor = (FRONTEND / "js" / "tutor.js").read_text(encoding="utf-8")
        planner = (FRONTEND / "js" / "planner.js").read_text(encoding="utf-8")
        api = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
        self.assertNotIn("plannerAfterUpload", tutor + planner)
        self.assertNotIn("data.planner", tutor)
        self.assertNotIn("include-documents", api + planner)


if __name__ == "__main__":
    unittest.main()
