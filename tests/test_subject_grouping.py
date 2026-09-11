import unittest
from pathlib import Path
from unittest.mock import patch

from backend import quiz_service
from backend.subject_grouping import derive_subject_name, group_documents_into_subjects


class DeriveSubjectNameTests(unittest.TestCase):
    def test_strips_extension_and_dash_separated_chapter_suffix(self):
        self.assertEqual(derive_subject_name("Biology - Chapter 1.pdf"), "Biology")
        self.assertEqual(derive_subject_name("Biology - Chapter 2.pdf"), "Biology")

    def test_strips_trailing_chapter_or_week_suffix_without_separator(self):
        self.assertEqual(derive_subject_name("Calculus Chapter 3.pdf"), "Calculus")
        self.assertEqual(derive_subject_name("Physics Week 2.pdf"), "Physics")

    def test_name_with_no_recognizable_suffix_is_returned_as_its_own_subject(self):
        self.assertEqual(derive_subject_name("Embedded Systems.pdf"), "Embedded Systems")

    def test_empty_or_missing_name_falls_back_to_untitled(self):
        self.assertEqual(derive_subject_name(""), "Untitled")
        self.assertEqual(derive_subject_name(None), "Untitled")


class GroupDocumentsIntoSubjectsTests(unittest.TestCase):
    def test_multiple_documents_can_appear_under_one_subject(self):
        materials = [
            {"document_id": "bio1.pdf", "document_name": "Biology - Chapter 1.pdf",
             "topic_count": 3, "assessed_topic_count": 1},
            {"document_id": "bio2.pdf", "document_name": "Biology - Chapter 2.pdf",
             "topic_count": 2, "assessed_topic_count": 2},
        ]
        subjects = group_documents_into_subjects(materials)
        self.assertEqual(len(subjects), 1)
        subject = subjects[0]
        self.assertEqual(subject["subject_name"], "Biology")
        self.assertEqual(subject["document_count"], 2)
        self.assertEqual(set(subject["document_ids"]), {"bio1.pdf", "bio2.pdf"})
        self.assertEqual(subject["primary_document_id"], "bio1.pdf")

    def test_overview_aggregation_is_correct(self):
        materials = [
            {"document_id": "bio1.pdf", "document_name": "Biology - Chapter 1.pdf",
             "topic_count": 3, "assessed_topic_count": 1},
            {"document_id": "bio2.pdf", "document_name": "Biology - Chapter 2.pdf",
             "topic_count": 2, "assessed_topic_count": 2},
            {"document_id": "es.pdf", "document_name": "Embedded Systems.pdf",
             "topic_count": 4, "assessed_topic_count": 0},
        ]
        subjects = group_documents_into_subjects(materials)
        by_name = {subject["subject_name"]: subject for subject in subjects}
        self.assertEqual(set(by_name), {"Biology", "Embedded Systems"})

        biology = by_name["Biology"]
        self.assertEqual(biology["document_count"], 2)
        self.assertEqual(biology["topic_count"], 5)
        self.assertEqual(biology["assessed_topic_count"], 3)
        self.assertEqual(biology["progress_percent"], 60.0)

        embedded = by_name["Embedded Systems"]
        self.assertEqual(embedded["document_count"], 1)
        self.assertEqual(embedded["topic_count"], 4)
        self.assertEqual(embedded["progress_percent"], 0.0)

    def test_subject_with_no_topics_has_null_progress(self):
        materials = [{"document_id": "empty.pdf", "document_name": "Empty.pdf",
                      "topic_count": 0, "assessed_topic_count": 0}]
        subjects = group_documents_into_subjects(materials)
        self.assertIsNone(subjects[0]["progress_percent"])

    def test_empty_materials_returns_no_subjects(self):
        self.assertEqual(group_documents_into_subjects([]), [])

    def test_subjects_are_sorted_by_name(self):
        materials = [
            {"document_id": "z.pdf", "document_name": "Zoology.pdf", "topic_count": 1, "assessed_topic_count": 0},
            {"document_id": "a.pdf", "document_name": "Astronomy.pdf", "topic_count": 1, "assessed_topic_count": 0},
        ]
        subjects = group_documents_into_subjects(materials)
        self.assertEqual([subject["subject_name"] for subject in subjects], ["Astronomy", "Zoology"])


class SubjectOverviewIntegrationTests(unittest.TestCase):
    """Exercises the same aggregation the real /api/dashboard endpoint performs
    (build_learning_dashboard's existing "materials" rows, grouped by
    group_documents_into_subjects) without touching the quiz pipeline itself --
    list_indexed_documents is stubbed exactly like the existing dashboard tests already do."""

    def test_dashboard_materials_group_into_subjects_without_changing_materials(self):
        documents = [
            {"id": "bio1.pdf", "title": "Biology - Chapter 1.pdf", "chunks": 2,
             "topics": [{"topic_id": "t1", "name": "Cells"}]},
            {"id": "bio2.pdf", "title": "Biology - Chapter 2.pdf", "chunks": 2,
             "topics": [{"topic_id": "t2", "name": "Genetics"}]},
            {"id": "es.pdf", "title": "Embedded Systems.pdf", "chunks": 3,
             "topics": [{"topic_id": "t3", "name": "Interrupts"}]},
        ]
        with patch.object(quiz_service, "list_indexed_documents", return_value=documents):
            dashboard = quiz_service.build_learning_dashboard("subject-overview-student")

        # Existing document flow is not broken: "materials" is still the flat, unchanged,
        # per-document list every other document-detail flow already relies on.
        self.assertEqual(len(dashboard["materials"]), 3)
        self.assertEqual(
            {material["document_id"] for material in dashboard["materials"]},
            {"bio1.pdf", "bio2.pdf", "es.pdf"},
        )

        subjects = group_documents_into_subjects(dashboard["materials"])
        by_name = {subject["subject_name"]: subject for subject in subjects}
        self.assertEqual(set(by_name), {"Biology", "Embedded Systems"})
        self.assertEqual(by_name["Biology"]["document_count"], 2)
        self.assertEqual(by_name["Embedded Systems"]["document_count"], 1)


class SubjectOverviewFrontendTests(unittest.TestCase):
    def test_overview_renders_subject_cards_not_bare_document_cards(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("function renderSubjectCards", script)
        self.assertIn("dashboardData.subjects", script)
        self.assertIn("subject-card", script)
        self.assertIn("subject-quick-actions", script)

    def test_quick_actions_reuse_existing_document_session_flow(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        # Existing document flow (openStudySession/setPage) is reused, never replaced.
        self.assertIn('openStudySession(primaryDocumentId, "quiz")', script)
        self.assertIn('openStudySession(primaryDocumentId, "flashcards")', script)
        self.assertIn('openStudySession(primaryDocumentId, "material")', script)
        self.assertIn('setPage("planner")', script)

    def test_each_document_within_a_subject_can_still_be_opened_individually(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn("subject-document-row", script)
        self.assertIn("openStudySession(documentId)", script)

    def test_quick_actions_are_labeled_as_document_scoped_not_subject_wide(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        # Quiz/Flashcards/AI Tutor tooltips must name the specific (primary) document they act
        # on, never imply a subject-wide generation across every document in the group.
        self.assertIn("`Opens Quiz for ${primaryDocumentLabel}`", script)
        self.assertIn("`Opens Flashcards for ${primaryDocumentLabel}`", script)
        self.assertIn("`Opens AI Tutor for ${primaryDocumentLabel}`", script)
        self.assertNotIn("all documents in this subject", script.lower())
        self.assertNotIn("every document in this subject", script.lower())


if __name__ == "__main__":
    unittest.main()
