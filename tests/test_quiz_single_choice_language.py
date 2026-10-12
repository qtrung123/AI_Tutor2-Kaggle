"""Normal Quiz contract (engine v9): single_choice ONLY and ONE language per quiz.

* every generated Normal Quiz (document and topic scope) is single_choice: 4 options, exactly 1
  correct answer, no fill_blank and no multi_select, and no fill_blank step runs;
* the quiz language is detected deterministically from the source excerpts BEFORE the first call,
  stated in every prompt (first call and follow-ups), and enforced per candidate: a wrong-language
  candidate is rejected and replaced through the existing bounded follow-up calls;
* technical identifiers (eCos, TinyOS, HAL, FIFO) never decide or break the language.
"""

import unittest
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, make_chunks

from backend import quiz_service, quiz_units
from backend.quiz_units import (
    QUIZ_ENGINE_VERSION, build_generation_prompt, build_study_units, detect_quiz_language, language_mismatch,
    vietnamese_share,
)

DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}
TOPIC = {"topic_id": "t1", "name": "Scheduling"}
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


def run_generate(payloads, chunks, scope="document"):
    document = {**DOCUMENT, "topics": [TOPIC]}
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
            DOCUMENT["id"], "easy", scope, topic_id=TOPIC["topic_id"] if scope == "topic" else None,
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
        self.assertEqual(QUIZ_ENGINE_VERSION, "simple_context_v9")


class LanguageDetectionTests(unittest.TestCase):
    def test_english_source_is_english(self):
        units = build_study_units(make_chunks(12, 2))
        self.assertEqual(detect_quiz_language([unit["evidence_excerpt"] for unit in units]), "english")

    def test_vietnamese_source_is_vietnamese(self):
        units = build_study_units(vi_chunks())
        self.assertEqual(detect_quiz_language([unit["evidence_excerpt"] for unit in units]), "vietnamese")

    def test_majority_wins_and_technical_terms_do_not_vote(self):
        english = [chunk["content"] for chunk in make_chunks(5, 2)]
        vietnamese = [chunk["content"] for chunk in vi_chunks(2)]
        self.assertEqual(detect_quiz_language(english + vietnamese), "english")
        self.assertEqual(detect_quiz_language(vietnamese * 3 + english[:2]), "vietnamese")
        # identifiers carry no language signal
        self.assertEqual(vietnamese_share("eCos TinyOS HAL FIFO RTOS API"), (0, 0))
        self.assertEqual(detect_quiz_language(["eCos TinyOS HAL FIFO " * 20]), "english")

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

    def test_the_validator_rejects_a_mixed_option_candidate(self):
        units = build_study_units(vi_chunks())
        raw = vi_candidate(1)
        raw["options"][3] = "Counts the synchronisation signals of tasks"
        with self.assertRaises(quiz_units.CandidateRejected) as caught:
            quiz_units.validate_candidate(raw, units, [], "easy", 1, {"topic_id": "document", "name": "Doc"}, 1,
                                          language="vietnamese")
        self.assertEqual(caught.exception.code, "language_mismatch")


def saved_quiz(planner_version: str) -> dict:
    return {
        "quiz_id": f"saved-{planner_version}", "document_id": DOCUMENT["id"], "difficulty": "easy", "topic_id": "document",
        "title": "Lecture", "assessment_scope": "document",
        "assessment_plan": {"planner_version": planner_version, "requested_count": 12},
        "questions": [{"id": index + 1, "question_type": "single_choice"} for index in range(12)],
    }


class CacheVersionTests(unittest.TestCase):
    """A saved v8 quiz (possibly mixed types/languages) never satisfies a new Normal Quiz request;
    a saved v9 quiz does. Old rows are not deleted or rewritten: regeneration saves a new artifact."""

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

    def test_saved_v8_quiz_is_not_current(self):
        old = saved_quiz("simple_context_v8")
        quiz, save = self.request(old)
        self.assertEqual(len(FakeModel.prompts), 1)                       # regenerated, not served from cache
        save.assert_called_once()
        self.assertNotEqual(quiz["quiz_id"], old["quiz_id"])              # a new artifact; the v8 row is untouched
        self.assertEqual(quiz["assessment_plan"]["planner_version"], "simple_context_v9")
        self.assertEqual(old["assessment_plan"]["planner_version"], "simple_context_v8")

    def test_saved_v9_quiz_is_cache_compatible(self):
        current = saved_quiz("simple_context_v9")
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
        quiz = run_generate([{"questions": [vi_candidate(fact, fact % 4) for fact in range(15)]}], vi_chunks())
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
        quiz = run_generate([{"questions": first}], vi_chunks())
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


if __name__ == "__main__":
    unittest.main()
