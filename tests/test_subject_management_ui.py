"""SUBJECT-MANAGEMENT in a real browser (headless Chrome, mocked API): the sidebar's Subjects list
(counts, Unassigned, Add subject), All Documents and each subject's view (its documents, header
actions and learning activity), search by subject name, moving one document, bulk move, rename,
delete, the optional subject of an upload, and the Planner's display-only subject filter (sessions
of other subjects are faded in place; no planner request is made). Desktop (1280px) and phone
(390px), with no horizontal overflow. Skipped without Chrome."""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_progress_ui import MOCK as PROGRESS_MOCK
import tests.test_sidebar_navigation_ui as sidebar_harness

MOCK = PROGRESS_MOCK + r"""
window.__subjects = [
  {subject_id: "bio", name: "Biology", color: "#10b981", created_at: "t", updated_at: "t", document_ids: ["mkt.pdf", "stats.pdf"], document_count: 2},
  {subject_id: "phy", name: "Physics", color: "#ef4444", created_at: "t", updated_at: "t", document_ids: [], document_count: 0}];
window.__subjectCalls = [];
window.__uploadForms = [];
const subjectList = () => window.__subjects.map((s) => ({...s, document_count: s.document_ids.length}));
const subjectFetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || "GET").toUpperCase();
  const p = new URL(url, "http://x").pathname;
  if (p === "/api/dashboard") {
    const data = await (await subjectFetch(input, init)).json();
    data.user_subjects = subjectList();
    return json(data);
  }
  if (p.startsWith("/api/subjects")) {
    const body = init.body ? JSON.parse(init.body) : null;
    window.__subjectCalls.push([method, p, body]);
    if (p === "/api/subjects" && method === "GET") return json(subjectList());
    if (p === "/api/subjects" && method === "POST") {
      const subject = {subject_id: "s" + window.__subjects.length, name: body.name, color: body.color, created_at: "t", updated_at: "t", document_ids: []};
      window.__subjects.push(subject); return json({...subject, document_count: 0}, 201);
    }
    if (p === "/api/subjects/assign") {
      window.__subjects.forEach((s) => { s.document_ids = s.document_ids.filter((id) => !body.document_ids.includes(id)); });
      if (body.subject_id) window.__subjects.find((s) => s.subject_id === body.subject_id).document_ids.push(...body.document_ids);
      return json({subject_id: body.subject_id, document_ids: body.document_ids, subjects: subjectList()});
    }
    const id = decodeURIComponent(p.split("/").pop());
    if (method === "PATCH") { Object.assign(window.__subjects.find((s) => s.subject_id === id), body); return json(subjectList().find((s) => s.subject_id === id)); }
    if (method === "DELETE") {
      const gone = window.__subjects.find((s) => s.subject_id === id);
      window.__subjects = window.__subjects.filter((s) => s.subject_id !== id);
      return json({deleted: id, unassigned_document_ids: gone.document_ids, subjects: subjectList()});
    }
  }
  if (p === "/api/sources/upload") {
    window.__uploadForms.push([...init.body.entries()].map(([key, value]) => [key, typeof value === "string" ? value : value.name]));
    return json({new_files: 1, new_chunks: 3, skipped_files: [], total_indexed_files: 4, sources: []});
  }
  return subjectFetch(input, init);
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
const shown = (el) => Boolean(el && el.getClientRects().length);
// The sidebar's Subjects list: name, document count (from its accessible name) and active state.
const cards = () => [...document.querySelectorAll("#sidebar-subject-list .sidebar-subject-item")].map((item) => ({
  key: item.dataset.subjectFilter, name: item.querySelector(".sidebar-subject-name").textContent,
  count: item.getAttribute("aria-label").split(": ").pop(), active: item.classList.contains("active")}));
const rows = () => [...document.querySelectorAll("#overview-materials-list .home-doc-card")].filter((row) => !row.hidden)
  .map((row) => ({id: row.dataset.documentId, subject: row.querySelector(".home-doc-subject").selectedOptions[0].textContent}));
const heading = () => document.getElementById("home-materials-title").textContent;
const activeNav = () => [...document.querySelectorAll(".nav-item.active")].map((b) => b.dataset.page);
// "all" = the All Documents navigation item; anything else = that entry under Subjects.
const clickCard = async (key) => {
  const target = key === "all" ? document.querySelector('.nav-item[data-page="documents"]')
    : document.querySelector(`.sidebar-subject-item[data-subject-filter="${key}"]`);
  target.click(); await sleep(150);
};
const header = () => ({title: heading(), count: document.getElementById("library-count").textContent,
  eyebrow: document.getElementById("library-eyebrow").textContent,
  actions: ["library-subject-plan", "library-subject-edit", "library-subject-delete", "library-upload"].filter((id) => shown(document.getElementById(id))),
  activity: shown(document.getElementById("library-activity"))
    ? [...document.querySelectorAll("#library-activity .library-activity-card h3")].map((h) => h.textContent) : null});
const submitDialog = async (fill) => {
  const dialog = document.querySelector("dialog.subject-dialog");
  if (!dialog) return null;
  const info = {open: dialog.open, title: dialog.querySelector(".subject-dialog-title").textContent};
  fill?.(dialog);
  dialog.querySelector(".subject-dialog-confirm").click(); await sleep(150);
  return info;
};
(async () => {
  await sleep(1500);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  setPage("overview"); await sleep(700);
  // Home is a dashboard: no document rows are rendered on it.
  out.home = {docRowsOnHome: [...document.querySelectorAll("#overview-view .home-doc-card")].length,
    libraryShown: shown(document.querySelector(".session-library")), subjectsInSidebar: cards().length};
  out.overflow.home = noOverflow();

  await clickCard("all");
  out.initial = {cards: cards(), rows: rows(), heading: heading(), page: document.body.dataset.page, activeNav: activeNav(),
    header: header(), searchVisible: !document.getElementById("home-material-search").hidden};
  out.overflow.library = noOverflow();

  await clickCard("bio");
  out.biology = {rows: rows(), heading: heading(), cards: cards(), activeNav: activeNav(), header: header()};
  out.overflow.subject = noOverflow();
  await clickCard("unassigned");
  out.unassigned = {rows: rows(), heading: heading(), header: header()};
  // The library's own Subject filter opens the same views.
  const filter = document.querySelector("#library-subject-filter select");
  filter.value = "bio"; filter.dispatchEvent(new Event("change")); await sleep(150);
  out.filterSelect = {heading: heading(), rows: rows().map((row) => row.id).sort(), active: cards().filter((c) => c.active).map((c) => c.key)};
  await clickCard("all");

  // Search matches a subject's name as well as titles.
  const search = document.getElementById("session-search-input");
  search.value = "biology"; search.dispatchEvent(new Event("input")); await sleep(50);
  out.searchBiology = rows().map((row) => row.id);
  search.value = ""; search.dispatchEvent(new Event("input")); await sleep(50);

  // Move one document with its Subject menu.
  const select = document.querySelector('.home-doc-card[data-document-id="pbi.pdf"] .home-doc-subject');
  select.value = "phy"; select.dispatchEvent(new Event("change")); await sleep(300);
  out.afterMove = {cards: cards(), rows: rows()};

  // Bulk: tick two documents and move them to Unassigned.
  for (const id of ["mkt.pdf", "pbi.pdf"]) {
    const box = document.querySelector(`.home-doc-card[data-document-id="${id}"] .home-doc-select`);
    box.checked = true; box.dispatchEvent(new Event("change"));
  }
  await sleep(50);
  out.bulkBar = {hidden: document.getElementById("home-bulk-bar").hidden, text: document.querySelector(".home-bulk-count").textContent};
  document.querySelector(".home-bulk-move").click(); await sleep(300);
  out.afterBulk = {cards: cards(), rows: rows(), barHidden: document.getElementById("home-bulk-bar").hidden};
  out.overflow.afterBulk = noOverflow();

  // Rename / recolor through the dialog, from the subject's own view.
  await clickCard("phy");
  document.getElementById("library-subject-edit").click(); await sleep(100);
  out.renameDialog = await submitDialog((dialog) => {
    dialog.querySelector('input[type="text"]').value = "Physics II";
    dialog.querySelector('input[value="#8b5cf6"]').checked = true;
  });
  out.afterRename = {cards: cards(), heading: heading()};

  // Delete from the subject's view: confirm says the documents are kept; the view falls back to All Documents.
  new MutationObserver(() => { const d = document.querySelector("dialog.confirm-dialog[open]:not([data-answered])"); if (!d) return; d.dataset.answered = "1"; (window.__confirmTexts = window.__confirmTexts || []).push(d.textContent); d.querySelector(".confirm-dialog-confirm").click(); }).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["open"]});
  await clickCard("bio");
  document.getElementById("library-subject-delete").click(); await sleep(300);
  out.afterDelete = {confirmText: (window.__confirmTexts || []).join(" "), cards: cards(), rows: rows(), heading: heading(),
    page: document.body.dataset.page, activeNav: activeNav(), deleteShown: shown(document.getElementById("library-subject-delete"))};

  // Add subject from the sidebar: it is created and its (empty) view opens.
  document.getElementById("sidebar-subject-add").click(); await sleep(150);
  out.addDialog = await submitDialog((dialog) => { dialog.querySelector('input[type="text"]').value = "History"; });
  await sleep(200);
  out.afterAdd = {heading: heading(), active: cards().filter((c) => c.active).map((c) => c.name), rows: rows(),
    empty: document.querySelector("#overview-materials-list .empty-state")?.textContent || null};

  // Upload with a new subject chosen in the dialog.
  setPage("overview"); await sleep(200);
  const upload = uploadWithSubjectChoice([new File(["%PDF-1.4"], "week3.pdf", {type: "application/pdf"})]);
  await sleep(150);
  out.uploadPreset = document.querySelector("dialog.subject-dialog select")?.value;   // nothing preselected on Home
  out.uploadDialog = await submitDialog((dialog) => {
    const choice = dialog.querySelector("select");
    choice.value = "__new__"; choice.dispatchEvent(new Event("change"));
    dialog.querySelector('input[type="text"]').value = "Chemistry";
  });
  await upload; await sleep(300);
  // Cancelled upload: nothing is sent.
  const cancelled = uploadWithSubjectChoice([new File(["%PDF-1.4"], "nope.pdf", {type: "application/pdf"})]);
  await sleep(150);
  document.querySelector("dialog.subject-dialog .subject-dialog-cancel").click();
  await cancelled; await sleep(100);
  out.uploads = window.__uploadForms;

  // Planner: a confirmed plan with sessions of two subjects, filtered to one.
  window.__subjects = [{subject_id: "bio", name: "Biology", color: "#10b981", created_at: "t", updated_at: "t", document_ids: ["mkt.pdf"]}];
  await refreshSubjects();
  window.__planner.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  window.__planner.materials = [{material_id: "m1", plan_id: "plan-1", document_id: "mkt.pdf", deadline: null, goal_type: "flexible", learning_state: "new"},
                                {material_id: "m2", plan_id: "plan-1", document_id: "stats.pdf", deadline: null, goal_type: "flexible", learning_state: "new"}];
  const session = (id, doc, title, start, end) => ({session_id: id, document_id: doc, document_title: title, activity_type: "summary",
    scheduled_start: start, scheduled_end: end, duration_minutes: 30, status: "scheduled", reason: {code: "new_material", message: "New material to learn"}, artifact_id: null});
  window.__planner.sessionsByPlan = {"plan-1": [session("s1", "mkt.pdf", "Marketing", "2026-09-25T18:00:00", "2026-09-25T18:30:00"),
                                                session("s2", "stats.pdf", "Statistics", "2026-09-25T19:00:00", "2026-09-25T19:30:00")]};
  window.__planner.sessions = window.__planner.sessionsByPlan["plan-1"];
  // A subject's view lists only that subject's planned sessions.
  await clickCard("bio"); await sleep(600);
  out.subjectSessions = [...document.querySelectorAll("#library-activity .planner-session strong")].map((el) => el.textContent);
  setPage("planner"); await sleep(900);
  const desktop = innerWidth >= 1024;
  const plannerFilter = document.querySelector(desktop ? "#pcal-subject-filter select" : "#planner-plan-subject-filter select");
  out.planner_caption = plannerFilter?.parentElement.querySelector(".planner-subject-caption")?.textContent || null;
  out.planner = {desktop, filterShown: Boolean(plannerFilter)};
  if (plannerFilter) {
    out.planner.options = [...plannerFilter.options].map((option) => option.textContent);
    const callsBefore = window.__planner.calls.length;
    plannerFilter.value = "bio"; plannerFilter.dispatchEvent(new Event("change")); await sleep(200);
    out.planner.newCalls = window.__planner.calls.slice(callsBefore);
    if (desktop) {
      out.planner.events = [...document.querySelectorAll(".pcal-event[data-session-id]")].map((block) => [block.dataset.sessionId, block.classList.contains("is-subject-muted")]);
      out.planner.materials = [...document.querySelectorAll("#pcal-material-list .pcal-material")].map((item) => [item.dataset.documentId, item.hidden]);
    } else {
      out.planner.sessions = [...document.querySelectorAll("#planner-plan-sessions .planner-session")].map((item) => [item.textContent.includes("Marketing") ? "s1" : "s2", item.classList.contains("is-subject-muted")]);
    }
    plannerFilter.value = ""; plannerFilter.dispatchEvent(new Event("change")); await sleep(200);
    if (desktop) out.planner.eventsAfterReset = document.querySelectorAll(".pcal-event[data-session-id]").length;
  }
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


class SubjectUiAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_home_has_no_document_list(self):
        self.assertEqual(self.out["home"]["docRowsOnHome"], 0)
        self.assertFalse(self.out["home"]["libraryShown"])
        self.assertEqual(self.out["home"]["subjectsInSidebar"], 3)

    def test_sidebar_subjects_and_all_documents(self):
        initial = self.out["initial"]
        self.assertEqual([(card["name"], card["count"]) for card in initial["cards"]],
                         [("Biology", "2 documents"), ("Physics", "0 documents"), ("Unassigned", "1 document")])
        self.assertFalse(any(card["active"] for card in initial["cards"]))
        self.assertEqual((initial["page"], initial["activeNav"], initial["heading"]), ("documents", ["documents"], "All Documents"))
        self.assertEqual(len(initial["rows"]), 3)
        self.assertTrue(initial["searchVisible"])   # a library organized in subjects gets search
        self.assertEqual(initial["header"]["actions"], ["library-upload"])   # no subject actions on All Documents
        self.assertIsNone(initial["header"]["activity"])

    def test_subject_view_shows_its_documents_and_activity(self):
        biology = self.out["biology"]
        self.assertEqual(biology["heading"], "Biology")
        self.assertEqual(biology["activeNav"], [])   # the subject is highlighted instead of All Documents
        self.assertEqual([card["key"] for card in biology["cards"] if card["active"]], ["bio"])
        self.assertEqual(sorted(row["id"] for row in biology["rows"]), ["mkt.pdf", "stats.pdf"])
        self.assertEqual({row["subject"] for row in biology["rows"]}, {"Biology"})
        self.assertEqual((biology["header"]["eyebrow"], biology["header"]["count"]), ("Subject", "2 documents"))
        self.assertEqual(biology["header"]["actions"], ["library-subject-plan", "library-subject-edit", "library-subject-delete", "library-upload"])
        self.assertEqual(biology["header"]["activity"], ["Progress", "Upcoming sessions"])
        self.assertEqual(self.out["subjectSessions"], ["Marketing"])   # only this subject's planned sessions

    def test_unassigned_view_and_library_filter(self):
        unassigned = self.out["unassigned"]
        self.assertEqual((unassigned["rows"], unassigned["heading"]), ([{"id": "pbi.pdf", "subject": "Unassigned"}], "Unassigned"))
        self.assertIsNone(unassigned["header"]["activity"])
        self.assertEqual(self.out["filterSelect"], {"heading": "Biology", "rows": ["mkt.pdf", "stats.pdf"], "active": ["bio"]})

    def test_search_matches_subject_names(self):
        self.assertEqual(sorted(self.out["searchBiology"]), ["mkt.pdf", "stats.pdf"])

    def test_move_one_and_bulk_move(self):
        counts = {card["name"]: card["count"] for card in self.out["afterMove"]["cards"]}
        self.assertEqual(counts["Physics"], "1 document")
        self.assertNotIn("Unassigned", counts)   # nothing left outside a subject: no Unassigned entry
        self.assertEqual(self.out["bulkBar"], {"hidden": False, "text": "2 selected"})
        after = {card["name"]: card["count"] for card in self.out["afterBulk"]["cards"]}
        self.assertEqual((after["Biology"], after["Physics"], after["Unassigned"]), ("1 document", "0 documents", "2 documents"))
        self.assertTrue(self.out["afterBulk"]["barHidden"])

    def test_rename_and_recolor(self):
        self.assertEqual(self.out["renameDialog"], {"open": True, "title": "Edit subject"})
        self.assertIn("Physics II", [card["name"] for card in self.out["afterRename"]["cards"]])
        self.assertEqual(self.out["afterRename"]["heading"], "Physics II")

    def test_delete_keeps_documents_as_unassigned(self):
        deleted = self.out["afterDelete"]
        self.assertIn("Its documents will be kept and moved to Unassigned", deleted["confirmText"])
        self.assertNotIn("Biology", [card["name"] for card in deleted["cards"]])
        self.assertEqual(len(deleted["rows"]), 3)   # every document is still listed
        self.assertEqual({row["subject"] for row in deleted["rows"]}, {"Unassigned"})
        self.assertEqual((deleted["page"], deleted["heading"], deleted["activeNav"]), ("documents", "All Documents", ["documents"]))
        self.assertFalse(deleted["deleteShown"])

    def test_add_subject_from_sidebar_opens_its_view(self):
        self.assertEqual(self.out["addDialog"], {"open": True, "title": "New subject"})
        added = self.out["afterAdd"]
        self.assertEqual((added["heading"], added["active"], added["rows"]), ("History", ["History"], []))
        self.assertIn("No documents in this subject yet", added["empty"])

    def test_upload_sends_the_chosen_subject_and_cancel_uploads_nothing(self):
        self.assertEqual(self.out["uploadPreset"], "")
        self.assertEqual(self.out["uploadDialog"], {"open": True, "title": "Add 1 document"})
        self.assertEqual(self.out["uploads"], [[["files", "week3.pdf"], ["new_subject_name", "Chemistry"]]])

    def test_planner_subject_filter_is_display_only(self):
        planner = self.out["planner"]
        self.assertTrue(planner["filterShown"])
        self.assertEqual(planner["options"], ["All subjects", "Biology", "Unassigned"])
        self.assertEqual(planner["newCalls"], [])   # filtering never asks the planner for anything
        if planner["desktop"]:
            self.assertEqual(planner["events"], [["s1", False]])   # desktop: only the chosen subject's sessions
            self.assertEqual(sorted(planner["materials"]), [["mkt.pdf", False], ["stats.pdf", True]])
            self.assertEqual(planner["eventsAfterReset"], 2)
        else:
            self.assertEqual(sorted(planner["sessions"]), [["s1", False], ["s2", True]])   # Your plan: faded in place
        self.assertEqual(self.out["planner_caption"], "Subject")   # a visible label, not only an aria-label

    def test_no_horizontal_overflow(self):
        self.assertEqual(self.out["overflow"], {"home": True, "library": True, "subject": True, "afterBulk": True, "planner": True})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SubjectDesktopTests(SubjectUiAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class SubjectPhoneTests(SubjectUiAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844)


if __name__ == "__main__":
    unittest.main()
