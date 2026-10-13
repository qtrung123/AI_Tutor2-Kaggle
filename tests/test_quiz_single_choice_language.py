"""Normal Quiz contract (engine v9): single_choice ONLY and ONE language per quiz.

* every generated Normal Quiz (document and topic scope) is single_choice: 4 options, exactly 1
  correct answer, no fill_blank and no multi_select, and no fill_blank step runs;
* the quiz language is the document's PRIMARY/SOURCE language, detected deterministically BEFORE
  the first call (structure -> bilingual order -> body fallback), stated in every prompt (first call
  and follow-ups), and enforced per candidate: a wrong-language candidate is rejected and replaced
  through the existing bounded follow-up calls;
* technical identifiers (eCos, TinyOS, HAL, FIFO) never decide or break the language.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, fact_sentence, make_chunks

from backend import quiz_service, quiz_units
from backend.quiz_units import (
    QUIZ_ENGINE_VERSION, build_generation_prompt, build_study_units, detect_quiz_language, language_mismatch,
    text_language, vietnamese_share,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
TOPIC = {"topic_id": "t1", "name": "Scheduling"}
# Structure (title + topic/subtopic names) of a Vietnamese-primary and an English-primary document.
VI_STRUCTURE = {"title": "Bài giảng hệ điều hành nhúng.pdf",
                "topics": [{"topic_id": "t1", "name": "Lập lịch tiến trình", "subtopics": [{"name": "Đồng bộ hóa luồng"}]}]}
EN_STRUCTURE = {"title": "Embedded Operating Systems.pdf",
                "topics": [{"topic_id": "t1", "name": "Section 1: Process Scheduling", "subtopics": [{"name": "Thread Synchronization"}]}]}
REAL_PDF = Path(__file__).resolve().parents[1] / "data" / "Embedded Systems.pdf"
ENGLISH_DIRECTIVE = "Write ALL question text, answer options and explanations in English only."
VIETNAMESE_DIRECTIVE = "Write ALL question text, answer options and explanations in Vietnamese only."

# Vietnamese facts: a technical identifier (kept as written) and a distinct Vietnamese behaviour.
VI_NOUNS = [
    "eCos", "TinyOS", "HAL", "FIFO", "mutex", "semaphore", "watchdog", "bootloader",
    "firmware", "kernel", "cache", "router", "socket", "compiler", "allocator", "hypervisor",
]
VI_BEHAVIOURS = [
    "quản lý luồng thời gian thực", "điều phối cảm biến năng lượng thấp", "trừu tượng hóa phần cứng vi điều khiển",
    "xếp hàng dữ liệu theo thứ tự đến", "khóa tài nguyên dùng chung", "đếm tín hiệu đồng bộ",
    "khởi động lại hệ thống bị treo", "nạp ảnh chương trình ban đầu", "điều khiển thiết bị ngoại vi",
    "lập lịch tiến trình ưu tiên", "lưu bản sao dữ liệu gần", "chuyển tiếp gói tin mạng",
    "kết nối điểm cuối giao tiếp", "biên dịch mã nguồn sang máy", "phân chia khối bộ nhớ heap",
    "cô lập các máy ảo riêng",
]


def vi_sentence(index: int) -> str:
    return f"Thành phần {VI_NOUNS[index]} {VI_BEHAVIOURS[index]} trong quá trình xử lý."


def vi_chunks(count: int = 8) -> list[dict]:
    chunks = []
    for chunk_index in range(count):
        facts = [vi_sentence((chunk_index * 2 + offset) % len(VI_NOUNS)) for offset in range(2)]
        text = " ".join(facts) + f" Ghi chú nền số {chunk_index + 1}: " + "nội dung bổ sung xung quanh chủ đề " * 12
        chunks.append({"content": text, "metadata": {"chunk_id": f"vi_{chunk_index + 1}", "document_id": "lecture.pdf"}})
    return chunks


def vi_candidate(fact: int, answer_index: int = 0) -> dict:
    correct = VI_BEHAVIOURS[fact]
    options = [VI_BEHAVIOURS[(fact + step) % len(VI_BEHAVIOURS)] for step in (5, 9, 13)]
    options.insert(answer_index, correct)
    return {
        "evidence_quote": vi_sentence(fact),
        "question": f"{VI_NOUNS[fact]} có chức năng gì?",
        "options": options,
        "answer_index": answer_index,
        "explanation": f"Tài liệu nêu rằng {VI_NOUNS[fact]} {correct}.",
    }


def vietnamese_version_of_english(fact: int) -> dict:
    """An English-source candidate (grounded quote) whose learner-facing text is Vietnamese."""
    raw = candidates([fact])[0]
    raw.update({
        "question": "Thành phần này có chức năng gì trong quá trình xử lý?",
        "options": ["Đặt trước tài nguyên hệ thống", "Sắp xếp các tiến trình", "Ánh xạ địa chỉ bộ nhớ", "Kết nối các điểm cuối"],
        "explanation": "Tài liệu nêu rõ chức năng của thành phần này.",
    })
    return raw


def bilingual_chunks(source: str, headings: list[str] | None = None, facts: int = 16) -> list[dict]:
    """Translated teaching material, one sentence per line: every source sentence is followed by
    its translation. `source` "english": EN line then VI line; "vietnamese": VI line then EN line.
    `headings` (one per chunk) are heading lines inside the text; None means no heading lines."""
    chunks = []
    for chunk_index in range(facts // 2):
        lines = [headings[chunk_index % len(headings)]] if headings else []
        for fact in (chunk_index * 2, chunk_index * 2 + 1):
            pair = [fact_sentence(fact), vi_sentence(fact)]
            lines.extend(pair if source == "english" else pair[::-1])
        chunks.append({"content": "\n".join(lines), "metadata": {"chunk_id": f"bi_{chunk_index + 1}", "document_id": "lecture.pdf"}})
    return chunks


def texts(chunks: list[dict]) -> list[str]:
    return [chunk["content"] for chunk in chunks]


def run_generate(payloads, chunks, scope="document", structure=None):
    document = {**DOCUMENT, "topics": [TOPIC], **(structure or {})}
    FakeModel.reset(payloads)
    with (
        patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: document}),
        patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"),
        patch.object(quiz_service, "get_quiz", return_value=None),
        patch.object(quiz_service, "get_document_chunks", return_value=chunks),
        patch.object(quiz_service, "get_schema_topic_evidence", return_value=chunks),
        patch.object(quiz_service, "ChatOllama", FakeModel),
        patch.object(quiz_service, "save_quiz_validation_event"),
        patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        patch.object(quiz_units, "select_flashcard_fill_blanks") as cloze_step,
    ):
        quiz = quiz_service.generate_quiz(
            DOCUMENT["id"], "easy", scope, topic_id=document["topics"][0]["topic_id"] if scope == "topic" else None,
            question_count=12, owner_id="owner",
        )
    cloze_step.assert_not_called()
    return quiz


def quiz_text(question: dict) -> str:
    return " ".join([question["question"], *question["options"], question["explanation"]])


class SingleChoiceContractTests(unittest.TestCase):
    def assert_single_choice_quiz(self, quiz):
        questions = quiz["questions"]
        self.assertEqual(len(questions), 12)
        types = [question["question_type"] for question in questions]
        self.assertEqual(types.count("single_choice"), 12)
        self.assertEqual(types.count("fill_blank"), 0)
        self.assertEqual(types.count("multi_select"), 0)
        self.assertEqual(quiz["assessment_plan"]["type_distribution"], {"single_choice": 12})
        self.assertNotIn("fill_blank", quiz["assessment_plan"])
        for question in questions:
            self.assertEqual([option[:3] for option in question["options"]], ["A. ", "B. ", "C. ", "D. "])
            self.assertEqual(len(question["correct_answers"]), 1)
            self.assertIn(question["correct_answer"], "ABCD")
            self.assertEqual(question["correct_answers"], [question["correct_answer"]])

    def test_document_quiz_of_12_is_single_choice_only(self):
        quiz = run_generate([{"questions": candidates(range(15))}], make_chunks(12, 2))
        self.assert_single_choice_quiz(quiz)
        self.assertEqual(quiz["assessment_plan"]["planner_version"], QUIZ_ENGINE_VERSION)
        self.assertEqual(quiz["assessment_plan"]["llm_calls"], 1)   # clean run: the candidate call only
        self.assertEqual(len(FakeModel.prompts), 1)

    def test_topic_quiz_is_single_choice_only(self):
        quiz = run_generate([{"questions": candidates(range(15))}], make_chunks(12, 2), scope="topic")
        self.assertEqual(quiz["assessment_scope"], "topic")
        self.assert_single_choice_quiz(quiz)

    def test_engine_version_is_bumped_so_old_mixed_quizzes_are_not_reused(self):
        self.assertEqual(QUIZ_ENGINE_VERSION, "simple_context_v10")


class LanguageDetectionTests(unittest.TestCase):
    """Primary/source language: structure -> bilingual order -> body fallback."""

    EN_LABELS = ["Embedded Operating Systems", "Section 1: Process Scheduling", "Thread Synchronization"]
    VI_LABELS = ["Bài giảng hệ điều hành nhúng", "Lập lịch tiến trình", "Đồng bộ hóa luồng"]

    def detect(self, chunks, labels=()):
        return detect_quiz_language(texts(chunks), labels)

    def test_a_english_headings_with_en_to_vi_pairs_is_english(self):
        result = self.detect(bilingual_chunks("english", ["Section 1: Process Scheduling"]), self.EN_LABELS)
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("english", "structural"))
        self.assertEqual((result["structural_en_votes"], result["structural_vi_votes"]), (3, 0))
        self.assertGreater(result["en_to_vi_transitions"], result["vi_to_en_transitions"])

    def test_b_vietnamese_headings_with_vi_to_en_pairs_is_vietnamese(self):
        result = self.detect(bilingual_chunks("vietnamese", ["Phần 1: Lập lịch tiến trình"]), self.VI_LABELS)
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("vietnamese", "structural"))
        self.assertEqual((result["structural_en_votes"], result["structural_vi_votes"]), (0, 3))
        self.assertGreater(result["vi_to_en_transitions"], result["en_to_vi_transitions"])

    def test_c_no_headings_en_to_vi_pairs_is_english_by_order(self):
        result = self.detect(bilingual_chunks("english"))
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("english", "bilingual_order"))
        self.assertEqual((result["en_to_vi_transitions"], result["vi_to_en_transitions"]), (16, 0))
        # Vietnamese has the accented letters, yet the translation never wins
        self.assertEqual(text_language(vi_sentence(0)), "vietnamese")

    def test_d_no_headings_vi_to_en_pairs_is_vietnamese_by_order(self):
        result = self.detect(bilingual_chunks("vietnamese"))
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("vietnamese", "bilingual_order"))
        self.assertEqual((result["en_to_vi_transitions"], result["vi_to_en_transitions"]), (0, 16))

    def test_order_survives_glued_pdf_lines_and_an_untranslated_sentence(self):
        # "English.Vietnamese." on one line without spaces (as PDF extraction produces it), and one
        # line holding only a translation: the source language is still English.
        lines = [f"{fact_sentence(fact)}{vi_sentence(fact)}".replace(" ", "") for fact in range(8)]
        lines.insert(4, vi_sentence(9).replace(" ", ""))
        result = detect_quiz_language(["\n".join(lines)])
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("english", "bilingual_order"))
        self.assertEqual(result["vi_to_en_transitions"], 0)

    def test_e_english_only_is_english(self):
        result = self.detect(make_chunks(12, 2))
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("english", "body_fallback"))
        self.assertEqual(result["body_vi_votes"], 0)

    def test_f_vietnamese_only_is_vietnamese(self):
        result = self.detect(vi_chunks())
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("vietnamese", "body_fallback"))
        self.assertEqual(result["body_en_votes"], 0)

    def test_body_fallback_is_balanced_and_ties_go_to_the_first_segment(self):
        long_vietnamese = " ".join(vi_sentence(fact).rstrip(".") for fact in range(12)) + "."
        english = [fact_sentence(fact) for fact in range(3)]
        result = detect_quiz_language(["\n\n".join(english + [long_vietnamese])])   # one long paragraph = one vote
        self.assertEqual((result["detected_language"], result["detection_basis"]), ("english", "body_fallback"))
        tie = detect_quiz_language(["\n\n".join([vi_sentence(0), fact_sentence(0)])])
        self.assertEqual(tie["detected_language"], "vietnamese")                       # the first confident segment

    def test_g_technical_identifiers_do_not_affect_detection(self):
        for identifiers in ("eCos", "TinyOS HAL", "FIFO", "eCos TinyOS HAL FIFO RTOS API"):
            self.assertIsNone(text_language(identifiers, 1))
        noisy = ["eCos TinyOS HAL FIFO.\n" * 10]
        for chunks in (bilingual_chunks("vietnamese"), bilingual_chunks("english"), vi_chunks()):
            plain = self.detect(chunks)
            with_identifiers = detect_quiz_language(noisy + texts(chunks), ["eCos", "TinyOS HAL", "FIFO"])
            self.assertEqual(plain, with_identifiers)
        self.assertEqual(vietnamese_share("eCos TinyOS HAL FIFO RTOS API"), (0, 0))

    @unittest.skipUnless(REAL_PDF.exists(), "data/Embedded Systems.pdf is gitignored; optional local check")
    def test_h_real_embedded_systems_pdf_is_english(self):
        from pypdf import PdfReader
        pages = [page.extract_text() or "" for page in PdfReader(str(REAL_PDF)).pages]
        structural = detect_quiz_language(pages, ["Embedded Systems", "Section 1: Embedded Systems",
                                                  "Section 2: Characteristics of Embedded Operating Systems"])
        self.assertEqual((structural["detected_language"], structural["detection_basis"]), ("english", "structural"))
        by_order = detect_quiz_language(pages)
        self.assertEqual((by_order["detected_language"], by_order["detection_basis"]), ("english", "bilingual_order"))

    def test_mismatch_rules(self):
        options = ["Option one here", "Option two here", "Option three here", "Option four here"]
        self.assertFalse(language_mismatch("What does the eCos HAL provide?", options, "The excerpt says so.", "english"))
        self.assertTrue(language_mismatch("Thành phần này có chức năng gì?", options, "Tài liệu nêu rõ điều này.", "english"))
        vi_options = ["Quản lý luồng", "Điều phối cảm biến", "Khóa tài nguyên", "Đếm tín hiệu"]
        self.assertFalse(language_mismatch("TinyOS có chức năng gì?", vi_options, "Tài liệu nêu rằng TinyOS điều phối cảm biến.", "vietnamese"))
        self.assertTrue(language_mismatch("What does the scheduler do in TinyOS?", options, "The document states it.", "vietnamese"))


class PerOptionLanguageTests(unittest.TestCase):
    VI_STEM = "TinyOS có chức năng gì trong hệ thống?"
    VI_EXPLANATION = "Tài liệu nêu rằng TinyOS điều phối cảm biến năng lượng thấp."
    VI_OPTIONS = ["Quản lý luồng thời gian thực", "Điều phối cảm biến năng lượng thấp", "Khóa tài nguyên dùng chung", "Đếm tín hiệu đồng bộ"]
    EN_STEM = "What does the eCos HAL provide to the kernel?"
    EN_EXPLANATION = "The document states that the HAL abstracts the hardware."
    EN_OPTIONS = ["Abstracts the microcontroller hardware", "Schedules the sensor tasks", "Locks the shared resources", "Counts the sync signals"]

    def test_vietnamese_quiz_rejects_one_clearly_english_option(self):
        options = self.VI_OPTIONS[:3] + ["Counts the synchronisation signals of tasks"]
        self.assertTrue(language_mismatch(self.VI_STEM, options, self.VI_EXPLANATION, "vietnamese"))
        self.assertFalse(language_mismatch(self.VI_STEM, self.VI_OPTIONS, self.VI_EXPLANATION, "vietnamese"))

    def test_english_quiz_rejects_one_clearly_vietnamese_option(self):
        options = self.EN_OPTIONS[:3] + ["Đếm các tín hiệu đồng bộ của tác vụ"]
        self.assertTrue(language_mismatch(self.EN_STEM, options, self.EN_EXPLANATION, "english"))
        self.assertFalse(language_mismatch(self.EN_STEM, self.EN_OPTIONS, self.EN_EXPLANATION, "english"))

    def test_identifier_and_short_term_options_stay_neutral(self):
        for terms in (["eCos", "TinyOS", "HAL", "FIFO"], ["FIFO queue", "LIFO stack", "eCos HAL", "TinyOS mote"]):
            self.assertFalse(language_mismatch(self.VI_STEM, terms, self.VI_EXPLANATION, "vietnamese"), terms)
            self.assertFalse(language_mismatch(self.EN_STEM, terms, self.EN_EXPLANATION, "english"), terms)

    def test_the_validator_never_accepts_a_mixed_option_candidate(self):
        units = build_study_units(vi_chunks())
        raw = vi_candidate(1)
        raw["options"][3] = "Counts the synchronisation signals of tasks"
        scope = {"topic_id": "document", "name": "Doc"}
        # the wrong-language distractor is replaced locally; the quiz stays in ONE language
        question, warnings = quiz_units.validate_candidate(raw, units, [], "easy", 1, scope, 1, language="vietnamese")
        self.assertIn("options_repaired", warnings)
        self.assertNotIn("D. Counts the synchronisation signals of tasks", question["options"])
        self.assertFalse(language_mismatch(question["question"], question["options"], question["explanation"], "vietnamese"))
        # with nothing to replace it from, the candidate is still rejected as language_mismatch
        raw["options"] = [raw["options"][0], "Counts the synchronisation signals of tasks", "Locks the shared memory pages", "Boots the device firmware"]
        raw["answer_index"] = 0
        with patch.object(quiz_units, "_distractor_alternatives", return_value=[]):
            with self.assertRaises(quiz_units.CandidateRejected) as caught:
                quiz_units.validate_candidate(raw, units, [], "easy", 1, scope, 1, language="vietnamese")
        self.assertEqual(caught.exception.code, "language_mismatch")


def saved_quiz(planner_version: str) -> dict:
    return {
        "quiz_id": f"saved-{planner_version}", "document_id": DOCUMENT["id"], "difficulty": "easy", "topic_id": "document",
        "title": "Lecture", "assessment_scope": "document",
        "assessment_plan": {"planner_version": planner_version, "requested_count": 12},
        "questions": [{"id": index + 1, "question_type": "single_choice"} for index in range(12)],
    }


class CacheVersionTests(unittest.TestCase):
    """A saved v8 quiz (possibly mixed types/languages) or v9 quiz (before local repair and the
    3-call budget) never satisfies a new Normal Quiz request; a saved v10 quiz does. Old rows are not deleted or rewritten: regeneration saves a new artifact."""

    def request(self, saved):
        document = {**DOCUMENT, "topics": []}
        FakeModel.reset([{"questions": candidates(range(15))}])
        with (
            patch.object(quiz_service, "_document_lookup", return_value={DOCUMENT["id"]: document}),
            patch.object(quiz_service, "invalidate_document_quizzes_for_topic_schema"),
            patch.object(quiz_service, "get_quiz", return_value=saved),
            patch.object(quiz_service, "get_document_chunks", return_value=make_chunks(12, 2)),
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz) as save,
            patch.object(quiz_service, "_delete_quiz_row") as delete,
        ):
            quiz = quiz_service.generate_quiz(DOCUMENT["id"], "easy", "document", question_count=12, owner_id="owner")
        delete.assert_not_called()
        return quiz, save

    def test_saved_v8_and_v9_quizzes_are_not_current(self):
        for version in ("simple_context_v8", "simple_context_v9"):
            with self.subTest(version=version):
                FakeModel.reset([{"questions": candidates(range(15))}])
                old = saved_quiz(version)
                quiz, save = self.request(old)
                self.assertEqual(len(FakeModel.prompts), 1)                   # regenerated, not served from cache
                save.assert_called_once()
                self.assertNotEqual(quiz["quiz_id"], old["quiz_id"])          # a new artifact; the old row is untouched
                self.assertEqual(quiz["assessment_plan"]["planner_version"], "simple_context_v10")
                self.assertEqual(old["assessment_plan"]["planner_version"], version)

    def test_saved_v10_quiz_is_cache_compatible(self):
        current = saved_quiz("simple_context_v10")
        quiz, save = self.request(current)
        self.assertIs(quiz, current)
        self.assertEqual(FakeModel.prompts, [])                           # no model call
        save.assert_not_called()


class LanguageEnforcementTests(unittest.TestCase):
    def test_english_source_rejects_vietnamese_candidates_and_top_up_keeps_english(self):
        chunks = make_chunks(12, 2)
        first = candidates(range(9)) + [vietnamese_version_of_english(fact) for fact in (9, 10, 11, 12)]
        quiz = run_generate([{"questions": first}, {"questions": candidates(range(9, 15))}], chunks)
        plan = quiz["assessment_plan"]
        self.assertEqual(plan["language"], "english")
        self.assertEqual(plan["validation_results"]["rejection_codes"].get("language_mismatch"), 4)
        self.assertEqual(plan["llm_calls"], 2)                          # one bounded follow-up, nothing more
        self.assertEqual(len(FakeModel.prompts), 2)
        for prompt in FakeModel.prompts:                                 # first call AND follow-up
            self.assertIn(ENGLISH_DIRECTIVE, prompt)
        self.assertEqual(len(quiz["questions"]), 12)
        for question in quiz["questions"]:
            self.assertEqual(vietnamese_share(quiz_text(question))[1], 0, question["question"])

    def test_vietnamese_source_gives_a_vietnamese_quiz_with_identifiers_unchanged(self):
        quiz = run_generate([{"questions": [vi_candidate(fact, fact % 4) for fact in range(15)]}], vi_chunks(), structure=VI_STRUCTURE)
        plan = quiz["assessment_plan"]
        self.assertEqual(plan["language"], "vietnamese")
        self.assertIn(VIETNAMESE_DIRECTIVE, FakeModel.prompts[0])
        self.assertIn("Preserve technical identifiers such as eCos, TinyOS, HAL, FIFO exactly.", FakeModel.prompts[0])
        self.assertEqual(len(quiz["questions"]), 12)
        for question in quiz["questions"]:
            self.assertFalse(language_mismatch(question["question"], question["options"], question["explanation"], "vietnamese"))
        stems = " ".join(question["question"] for question in quiz["questions"])
        self.assertIn("eCos", stems)
        self.assertIn("TinyOS", stems)

    def test_vietnamese_source_rejects_english_candidates(self):
        english = [
            {**vi_candidate(fact), "question": f"What does {VI_NOUNS[fact]} do during processing?",
             "explanation": f"The document states what {VI_NOUNS[fact]} does."}
            for fact in (12, 13, 14)
        ]
        first = [vi_candidate(fact, fact % 4) for fact in range(12)] + english
        quiz = run_generate([{"questions": first}], vi_chunks(), structure=VI_STRUCTURE)
        self.assertEqual(quiz["assessment_plan"]["validation_results"]["rejection_codes"].get("language_mismatch"), 3)
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertTrue(all(not question["question"].startswith("What") for question in quiz["questions"]))

    def test_one_quiz_never_persists_a_mixture_of_languages(self):
        # Half the candidates in each language on an English source: only English ones survive.
        mixed = []
        for fact in range(15):
            mixed.append(vietnamese_version_of_english(fact) if fact % 2 else candidates([fact])[0])
        follow = candidates(range(15, 24))
        quiz = run_generate([{"questions": mixed}, {"questions": follow}, {"questions": []}, {"questions": []}], make_chunks(12, 2))
        languages = {
            "vietnamese" if vietnamese_share(quiz_text(question))[1] else "english" for question in quiz["questions"]
        }
        self.assertEqual(languages, {"english"})
        self.assertLessEqual(quiz["assessment_plan"]["llm_calls"], quiz["assessment_plan"]["max_llm_calls"])

    def test_language_reaches_every_prompt(self):
        units = build_study_units(make_chunks(4, 2))
        self.assertIn(ENGLISH_DIRECTIVE, build_generation_prompt("L", "easy", units, 5))
        followup = build_generation_prompt("L", "easy", units, 5, ["Q?"], ["quote"], language="vietnamese")
        self.assertIn(VIETNAMESE_DIRECTIVE, followup)
        self.assertNotIn(ENGLISH_DIRECTIVE, followup)


class BilingualGenerationTests(unittest.TestCase):
    """I / J: a generated quiz over translated material uses ONLY the source language."""

    def test_i_english_primary_bilingual_quiz_is_all_english_single_choice(self):
        chunks = bilingual_chunks("english", ["Section 1: Process Scheduling"])
        mixed = [vietnamese_version_of_english(fact) if fact in (2, 7) else candidates([fact])[0] for fact in range(15)]
        quiz = run_generate([{"questions": mixed}, {"questions": candidates(range(15, 16))}], chunks, structure=EN_STRUCTURE)
        plan = quiz["assessment_plan"]
        self.assertEqual(plan["language"], "english")
        self.assertEqual(plan["language_detection"]["detection_basis"], "structural")
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertEqual(plan["type_distribution"], {"single_choice": 12})
        for question in quiz["questions"]:
            self.assertEqual(vietnamese_share(quiz_text(question))[1], 0, question["question"])
            self.assertEqual(len(question["options"]), 4)
        for prompt in FakeModel.prompts:
            self.assertIn(ENGLISH_DIRECTIVE, prompt)

    def test_j_vietnamese_primary_bilingual_quiz_is_all_vietnamese_single_choice(self):
        chunks = bilingual_chunks("vietnamese", ["Phần 1: Lập lịch tiến trình"])
        english = [candidates([fact])[0] for fact in (0, 1, 2)]                      # grounded, but English
        quiz = run_generate([{"questions": [vi_candidate(fact, fact % 4) for fact in range(15)] + english}],
                            chunks, structure=VI_STRUCTURE)
        plan = quiz["assessment_plan"]
        self.assertEqual(plan["language"], "vietnamese")
        self.assertEqual(plan["language_detection"]["detection_basis"], "structural")
        self.assertEqual(plan["validation_results"]["rejection_codes"].get("language_mismatch"), 3)
        self.assertEqual(len(quiz["questions"]), 12)
        self.assertEqual(plan["type_distribution"], {"single_choice": 12})
        for question in quiz["questions"]:
            self.assertFalse(language_mismatch(question["question"], question["options"], question["explanation"], "vietnamese"))
        self.assertIn(VIETNAMESE_DIRECTIVE, FakeModel.prompts[0])

    def test_detection_diagnostics_are_persisted(self):
        quiz = run_generate([{"questions": candidates(range(15))}], bilingual_chunks("english"),
                            structure={"title": "x.pdf", "topics": [{"topic_id": "t1", "name": "", "subtopics": []}]})
        detection = quiz["assessment_plan"]["language_detection"]
        self.assertEqual(set(detection), {
            "detected_language", "detection_basis", "structural_en_votes", "structural_vi_votes",
            "en_to_vi_transitions", "vi_to_en_transitions", "body_en_votes", "body_vi_votes",
        })
        self.assertEqual((detection["detected_language"], detection["detection_basis"]), ("english", "bilingual_order"))


if __name__ == "__main__":
    unittest.main()
