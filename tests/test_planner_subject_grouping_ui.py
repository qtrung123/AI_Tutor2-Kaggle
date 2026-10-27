"""PLANNER-SUBJECT-GROUPING in a real browser (headless Chrome, mocked API).

Phone (390px, step flow): the Materials step lists documents under their subject with Unassigned
last; each group expands/collapses, shows "x of y selected" and has Select all; single documents
can still be picked; documents from several subjects go into one plan; the step shows the total
selected; every plan material is added as one document (POST {document_id}) and Next still works.
Desktop (1280px): the Add materials sheet is grouped the same way and still adds one document at a
time. No horizontal overflow. Skipped without Chrome."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_subject_management_ui import MOCK as SUBJECT_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = SUBJECT_MOCK + r"""
window.__subjects = [
  {subject_id: "bio", name: "Biology", color: "#10b981", created_at: "t", updated_at: "t", document_ids: ["mkt.pdf", "stats.pdf"]},
  {subject_id: "empty", name: "Empty", color: "#ef4444", created_at: "t", updated_at: "t", document_ids: []}];
window.__materialBodies = [];
const groupingFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  if (/\/api\/planner\/plans\/[^/]+\/materials$/.test(new URL(url, "http://x").pathname) && (init.method || "GET").toUpperCase() === "POST") {
    window.__materialBodies.push(JSON.parse(init.body));
  }
  return groupingFetch(input, init);
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
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1;
const groups = (root) => [...root.querySelectorAll(".planner-subject-group")].map((group) => {
  const all = group.querySelector(".planner-subject-group-all input");
  return {key: group.dataset.subjectGroup, name: group.querySelector(".planner-subject-group-name").textContent,
    count: group.querySelector(".planner-subject-group-count").textContent,
    expanded: group.querySelector(".planner-subject-group-toggle").getAttribute("aria-expanded"),
    bodyHidden: group.querySelector(".planner-subject-group-body").hidden,
    all: all ? {checked: all.checked, indeterminate: all.indeterminate} : null,
    docs: [...group.querySelectorAll("[data-document-id]")].map((el) => el.dataset.documentId)};
});
const step = () => document.getElementById("planner-material-list");
const group = (root, key) => root.querySelector(`.planner-subject-group[data-subject-group="${key}"]`);
const change = async (box, checked) => { box.checked = checked; box.dispatchEvent(new Event("change")); await sleep(250); };
const count = () => document.getElementById("planner-material-count").textContent;
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  setPage("overview"); await sleep(600);   // loads the dashboard (and the user's subjects)
  setPage("planner"); await sleep(800);
  if (innerWidth < 1024) {
    out.start = {groups: groups(step()), count: count(), nextDisabled: document.getElementById("planner-to-availability").disabled};
    await change(group(step(), "bio").querySelector(".planner-subject-group-all input"), true);
    out.afterSelectAll = {groups: groups(step()), count: count()};
    await change(step().querySelector('input[data-document-id="pbi.pdf"]'), true);   // a second subject's document
    out.afterSingle = {groups: groups(step()), count: count()};
    group(step(), "bio").querySelector(".planner-subject-group-toggle").click(); await sleep(100);
    out.collapsed = groups(step()).find((g) => g.key === "bio");
    group(step(), "bio").querySelector(".planner-subject-group-toggle").click(); await sleep(100);
    out.expanded = groups(step()).find((g) => g.key === "bio");
    await change(step().querySelector('input[data-document-id="stats.pdf"]'), false);
    out.partial = {bio: groups(step()).find((g) => g.key === "bio"), count: count()};
    window.confirm = () => true;
    await change(group(step(), "bio").querySelector(".planner-subject-group-all input"), false);
    out.cleared = {groups: groups(step()), count: count()};
    out.planMaterials = window.__planner.materials.map((m) => m.document_id);
    out.overflow.step = noOverflow();
    document.getElementById("planner-to-availability").click(); await sleep(200);
    out.afterNext = [...document.querySelectorAll("[data-planner-step]")].filter((s) => !s.hidden).map((s) => s.dataset.plannerStep);
  } else {
    document.getElementById("pcal-add-materials").click(); await sleep(300);
    const sheet = document.getElementById("pcal-sheet-list");
    out.sheetStart = groups(sheet);
    sheet.querySelector('.pcal-sheet-row[data-document-id="stats.pdf"] .pcal-sheet-add').click(); await sleep(500);
    out.sheetAfterAdd = groups(document.getElementById("pcal-sheet-list"));
    group(document.getElementById("pcal-sheet-list"), "unassigned").querySelector(".planner-subject-group-toggle").click(); await sleep(100);
    out.sheetCollapsed = groups(document.getElementById("pcal-sheet-list")).find((g) => g.key === "unassigned");
    out.planMaterials = window.__planner.materials.map((m) => m.document_id);
    out.overflow.sheet = noOverflow();
  }
  out.materialBodies = window.__materialBodies;
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


class GroupingAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_every_material_is_one_document(self):
        self.assertTrue(self.out["materialBodies"])
        for body in self.out["materialBodies"]:
            self.assertEqual(set(body) - {"goal_type", "deadline", "target_date"}, {"document_id"})
            self.assertTrue(body["document_id"].endswith(".pdf"))

    def test_no_horizontal_overflow(self):
        self.assertTrue(all(self.out["overflow"].values()))


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class PhoneMaterialsStepTests(GroupingAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)

    def test_groups_by_subject_with_unassigned_last(self):
        start = self.out["start"]["groups"]
        self.assertEqual([(g["name"], g["docs"], g["count"]) for g in start],
                         [("Biology", ["mkt.pdf", "stats.pdf"], "0 of 2 selected"), ("Unassigned", ["pbi.pdf"], "0 of 1 selected")])
        self.assertTrue(all(g["expanded"] == "true" and not g["bodyHidden"] for g in start))
        self.assertEqual(self.out["start"]["count"], "No documents selected yet")
        self.assertTrue(self.out["start"]["nextDisabled"])

    def test_select_all_and_single_documents_across_subjects(self):
        bio = self.out["afterSelectAll"]["groups"][0]
        self.assertEqual((bio["count"], bio["all"]), ("2 of 2 selected", {"checked": True, "indeterminate": False}))
        self.assertEqual(self.out["afterSelectAll"]["count"], "2 documents selected")
        self.assertEqual(self.out["afterSingle"]["groups"][1]["count"], "1 of 1 selected")
        self.assertEqual(self.out["afterSingle"]["count"], "3 documents selected")
        self.assertEqual([body["document_id"] for body in self.out["materialBodies"]], ["mkt.pdf", "stats.pdf", "pbi.pdf"])

    def test_expand_collapse(self):
        self.assertEqual((self.out["collapsed"]["expanded"], self.out["collapsed"]["bodyHidden"]), ("false", True))
        self.assertEqual((self.out["expanded"]["expanded"], self.out["expanded"]["bodyHidden"]), ("true", False))

    def test_partial_and_clearing_a_group(self):
        self.assertEqual((self.out["partial"]["bio"]["count"], self.out["partial"]["bio"]["all"]),
                         ("1 of 2 selected", {"checked": False, "indeterminate": True}))
        self.assertEqual(self.out["partial"]["count"], "2 documents selected")
        self.assertEqual(self.out["cleared"]["groups"][0]["count"], "0 of 2 selected")
        self.assertEqual(self.out["cleared"]["count"], "1 document selected")
        self.assertEqual(self.out["planMaterials"], ["pbi.pdf"])

    def test_next_still_goes_to_availability(self):
        self.assertEqual(self.out["afterNext"], ["availability"])


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class DesktopAddSheetTests(GroupingAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)

    def test_sheet_is_grouped_and_adds_one_document(self):
        self.assertEqual([(g["name"], g["docs"], g["count"]) for g in self.out["sheetStart"]],
                         [("Biology", ["mkt.pdf", "stats.pdf"], "0 of 2 in plan"), ("Unassigned", ["pbi.pdf"], "0 of 1 in plan")])
        self.assertIsNone(self.out["sheetStart"][0]["all"])   # no bulk add on the sheet: one goal per document
        after = self.out["sheetAfterAdd"][0]
        self.assertEqual((after["docs"], after["count"]), (["mkt.pdf"], "1 of 2 in plan"))
        self.assertEqual(self.out["planMaterials"], ["stats.pdf"])
        self.assertEqual((self.out["sheetCollapsed"]["expanded"], self.out["sheetCollapsed"]["bodyHidden"]), ("false", True))


if __name__ == "__main__":
    unittest.main()
