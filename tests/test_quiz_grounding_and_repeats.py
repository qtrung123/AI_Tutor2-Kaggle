"""Regression tests for the generic Quiz fixes found in a real run (36 candidates, 9 accepted).

1. Grounding survives PDF extraction artifacts (glued words, dropped glyphs, a sentence split
   between two chunks) and still refuses unsupported / hallucinated evidence.
2. A follow-up call PREFERS material no accepted question was built on but may be shown used
   passages again; a duplicate is the same FACT (same evidence and same answer), not merely a
   second question about the same sentence.
3. Negative-polarity questions (NOT / EXCEPT / incorrect / false) are refused.
4. `answer_in_evidence` means one thing: the answer was found in the material.
5. Partial quizzes, 12/15/18/20, and "no extra LLM call once enough valid questions exist" still hold.

Everything here is deterministic (a fake model, no Ollama); the real-model validation is done on Kaggle.
"""

import unittest
from unittest.mock import patch

from quiz_fixtures import FACT_COUNT, FakeModel, candidates, fact_sentence, make_chunks, raw_candidate

from backend import quiz_service, quiz_units
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import (
    CandidateRejected, build_generation_prompt, build_study_units, locate_evidence, mask_used_evidence,
    squash, validate_candidate,
)

SCOPE = {"topic_id": "document", "name": "Entire document"}
DOCUMENT = {"id": "lecture.pdf", "title": "Lecture", "hash": "hash", "topic_schema_version": 2}


def validate(raw, units, accepted=None):
    accepted = accepted or []
    return validate_candidate(raw, units, accepted, "easy", len(accepted) + 1, SCOPE, len(accepted) + 1)


def accept(raw, units, accepted):
    question, warnings = validate(raw, units, accepted)
    accepted.append(question)
    return question, warnings


def one_unit(text, chunk_id="c1"):
    return build_study_units([{"content": text, "metadata": {"chunk_id": chunk_id}}])


def candidate(quote, question, correct, wrong, **overrides):
    result = {"evidence_quote": quote, "question": question, "options": [correct, *wrong], "answer_index": 0,
              "explanation": "The document states this directly."}
    result.update(overrides)
    return result


# A bilingual, fully glued extraction: what a slide deck exported to PDF really looks like.
GLUED = (
    "Section2: SchedulingAlgorithms 1. First-Come-First-Served(FCFS)○ EachprocessjoinstheReadyqueue.→Mỗitiếntrìnhsẽthamgiavàohàngđợisẵnsàng."
    "○ Whenthecurrentprocessceasestoexecute,theprocessatthefrontofthequeueisselected.→Khitiếntrìnhhiệntạingừngthựcthi,tiếntrìnhởđầuhàngđợisẽđượcchọn."
    "○ Ashortprocessmayhavetowaitalongtimebeforeitcanexecute.→Mộttiếntrìnhngắncóthểphảichờrấtlâutrướckhinócóthểđượcthựcthi."
    " 2. RoundRobin(RR)○ Usespreemptionbasedonaclock.→Sửdụngviệcgiànhquyềnđiềuphốidựatrênđồnghồ."
    "○ Eachprocessisgivenatimeslice(quantum)beforebeingpreempted.→Mỗitiếntrìnhđượccấpmộtkhoảngthờigiantrướckhibịgiànhquyền."
)
FRONT = candidate(
    "When the current process ceases to execute, the process at the front of the queue is selected.",
    "Which process is selected when the current process ceases to execute?",
    "The process at the front of the queue",
    ["The process that arrived last", "The process holding the most memory", "The process chosen at random"],
)


class PdfExtractionArtifactTests(unittest.TestCase):
    def test_a_respaced_quote_of_glued_bilingual_text_is_grounded(self):
        units = one_unit(GLUED)
        self.assertNotIn(" the process at the front", units[0]["evidence_excerpt"])        # the source really is glued
        question, warnings = validate(FRONT, units)
        self.assertEqual(question["correct_answer"], "A")
        self.assertEqual(warnings, [])

    def test_the_quote_may_be_glued_spaced_accented_or_recased_in_any_combination(self):
        units = one_unit(GLUED)
        for quote in (
            "Whenthecurrentprocessceasestoexecute,theprocessatthefrontofthequeueisselected.",
            "WHEN THE CURRENT PROCESS CEASES TO EXECUTE, THE PROCESS AT THE FRONT OF THE QUEUE IS SELECTED",
            "when the current process ceases to execute -- the process at the front of the queue is selected!",
            "When the current process ceases to exe- cute, the process at the front of the queue is selected.",
        ):
            with self.subTest(quote=quote):
                self.assertIsNotNone(locate_evidence(quote, units))
                validate({**FRONT, "evidence_quote": quote}, units)

    def test_a_quote_written_in_the_other_language_of_a_bilingual_page_is_grounded_too(self):
        units = one_unit(GLUED)
        vietnamese = "Khi tiến trình hiện tại ngừng thực thi, tiến trình ở đầu hàng đợi sẽ được chọn."
        located = locate_evidence(vietnamese, units)
        self.assertIsNotNone(located)
        self.assertEqual(located[1], 1.0)
        validate({**FRONT, "evidence_quote": vietnamese}, units)

    def test_dropped_glyphs_cost_only_their_own_characters_even_when_there_are_many(self):
        sentence = ("Thesystemachieveshighefficiencybyoverlappingtheexecutionofseveralstagesinparallelwhilethecontrollerkeepstrackofeveryinstructionthatis"
                    "currentlyinflightandreleasesitsresourcesassoonasitretires")
        # an extractor that loses one character every ~27 letters (ligatures, private-use glyphs): 7 damaged
        # places, i.e. 8 matching runs -- more than the 5 runs the old check tolerated
        damaged = "".join(ch for index, ch in enumerate(sentence) if index % 27 != 26)
        self.assertEqual(len(sentence) - len(damaged), 7)
        units = one_unit(f"Intro text about pipelines. {damaged} Closing remark about hazards.")
        located = locate_evidence(sentence, units)
        self.assertIsNotNone(located)
        self.assertGreaterEqual(located[1], quiz_units.QUIZ_QUOTE_MIN_ALIGNMENT)

    def test_a_scattered_quote_stitched_from_pieces_of_the_page_is_still_refused(self):
        units = one_unit(GLUED)
        stitched = "Each process joins the Ready queue. Uses preemption based on a clock. A short process may have to wait a long time."
        self.assertIsNone(locate_evidence(stitched, units))

    def test_a_sentence_split_between_two_chunks_is_grounded_and_cites_both_chunks(self):
        padding = " Background about the machine: " + "surrounding notes on hardware timers and queues " * 12
        chunks = [
            {"content": "Intro." + padding + " The dispatcher selects the next", "metadata": {"chunk_id": "a"}},
            {"content": "process to run after every clock interrupt occurs in the system. Outro." + padding, "metadata": {"chunk_id": "b"}},
        ]
        units = build_study_units(chunks)
        self.assertEqual(len(units), 2)                                            # two excerpts: the sentence is cut between them
        quote = "The dispatcher selects the next process to run after every clock interrupt occurs in the system."
        self.assertFalse(any(squash(quote) in unit["_squashed"] for unit in units))
        located = locate_evidence(quote, units)
        self.assertIsNotNone(located)
        question, _ = validate(candidate(
            quote, "When does the dispatcher select the next process to run?", "After every clock interrupt",
            ["When memory is freed", "When a file is closed", "When the disk is idle"]), units)
        self.assertEqual(question["source_chunk_ids"], ["a", "b"])
        self.assertEqual(question["concept_id"], units[0]["unit_id"])

    def test_excerpts_that_are_not_neighbours_are_never_joined(self):
        padding = " Background about the machine: " + "surrounding notes on hardware timers and queues " * 12
        chunks = [{"content": f"Part {i}." + padding + tail, "metadata": {"chunk_id": f"c{i}"}} for i, tail in enumerate(
            [" The dispatcher selects the next", " Unrelated middle section about disks.", " process to run after every clock interrupt occurs."])]
        units = build_study_units(chunks)
        quote = "The dispatcher selects the next process to run after every clock interrupt occurs."
        self.assertIsNone(locate_evidence(quote, [units[0], units[2]]))            # the middle excerpt was not shown
        self.assertIsNone(locate_evidence(quote, units))                           # ... and even shown, the halves are not neighbours

    def test_inflected_words_of_the_material_are_recognised(self):
        units = one_unit("Thescheduleruseswhichevercriterionisconfiguredandpreemptiveschedulinginterruptsthecurrentlyrunningprocessatanytime.")
        question, _ = validate(candidate(
            "The scheduler uses whichever criterion is configured and preemptive scheduling interrupts the currently running process at any time.",
            "What does preemption of a running process do?", "Interrupts the process that is running",
            ["Cancels the process forever", "Copies the process to disk", "Renames the process quietly"]), units)
        self.assertEqual(question["correct_answer"], "A")


class UnsupportedEvidenceIsStillRejectedTests(unittest.TestCase):
    def setUp(self):
        self.units = one_unit(GLUED)

    def rejected(self, raw, code, category):
        with self.assertRaises(CandidateRejected) as context:
            validate(raw, self.units)
        self.assertEqual((context.exception.category, context.exception.code), (category, code), str(context.exception))

    def test_an_invented_quote_is_rejected(self):
        self.rejected({**FRONT, "evidence_quote": "The banker's algorithm avoids deadlock by refusing unsafe resource requests."},
                      "quote_not_found", "grounding")

    def test_a_too_short_quote_proves_nothing(self):
        self.rejected({**FRONT, "evidence_quote": "The queue."}, "quote_too_short", "grounding")

    def test_a_real_quote_with_an_answer_from_outside_knowledge_is_rejected(self):
        self.rejected(candidate(
            FRONT["evidence_quote"], "Which process is selected when the current process ceases to execute?",
            "The banker's safety sequence", ["The process that arrived last", "A random process", "The largest process"]),
            "answer_not_in_context", "grounding")

    def test_one_shared_long_word_does_not_ground_an_invented_answer(self):
        """The unsafe old rule: any 8+ letter word found in the excerpt was enough ("algorithm" in the heading)."""
        for answer in ("The banker's algorithm", "A hashing algorithm", "The banker's algorithm safety sequence"):
            with self.subTest(answer=answer):
                self.rejected(candidate(
                    FRONT["evidence_quote"], "Which process is selected when the current process ceases to execute?", answer,
                    ["The process that arrived last", "A random process", "The largest process"]), "answer_not_in_context", "grounding")

    def test_reasonable_paraphrases_of_the_material_are_still_grounded(self):
        for answer in ("It picks the front process of the queue", "The first process waiting in the queue", "The process at the queue front"):
            with self.subTest(answer=answer):
                question, _ = validate(candidate(
                    FRONT["evidence_quote"], "Which process is selected when the current process ceases to execute?", answer,
                    ["The process that arrived last", "A random process", "The largest process"]), self.units)
                self.assertEqual(question["correct_answer"], "A")

    def test_a_real_quote_with_a_question_about_something_else_is_rejected(self):
        self.rejected(candidate(
            FRONT["evidence_quote"], "How does virtual memory translate addresses through hierarchical page tables?",
            "Through hierarchical page tables", ["By hashing", "By copying", "By guessing"]),
            "question_not_supported", "grounding")

    def test_short_words_must_still_match_exactly(self):
        # only words of 7+ letters get the inflection allowance; shorter ones must occur whole
        units = one_unit("Thecontrollerkeepsthequeueofpendingrequestsorderedbyarrivaltimeandscheduler.")
        haystack = units[0]["_squashed"]
        self.assertTrue(quiz_units._token_supported("scheduled", haystack))       # inflection of "scheduler"
        self.assertTrue(quiz_units._token_supported("queue", haystack))
        self.assertFalse(quiz_units._token_supported("quotes", haystack))         # 6 letters, not present
        self.assertFalse(quiz_units._token_supported("queuing", haystack))        # prefix "queuin" is not in the text
        self.assertFalse(quiz_units._token_supported("compiler", haystack))


class SamePassageIsADuplicateTests(unittest.TestCase):
    def setUp(self):
        self.units = one_unit(GLUED)
        self.english = FRONT
        self.vietnamese = candidate(
            "Khi tiến trình hiện tại ngừng thực thi, tiến trình ở đầu hàng đợi sẽ được chọn.",
            "What happens to the queue when the current process stops?", "The process at the front is chosen",
            ["Nothing is chosen at all", "Every process is removed", "The queue is emptied"])

    def test_two_different_facts_of_the_same_sentence_are_both_valid_questions(self):
        units = one_unit("Theschedulerordersprocessesbyprioritywhilethedispatcherstoreseachdecisioninthelogduringprocessing.")
        quote = "The scheduler orders processes by priority while the dispatcher stores each decision in the log during processing."
        accepted = []
        accept(candidate(quote, "How does the scheduler order processes?", "By priority",
                         ["By arrival time only", "By memory size", "At random"]), units, accepted)
        second, _ = accept(candidate(quote, "Where does the dispatcher store each decision?", "In the log",
                                     ["In the cache", "In the register file", "In the page table"]), units, accepted)
        self.assertEqual(len(accepted), 2)                      # one sentence, two independent facts: capacity is not lowered
        self.assertEqual(second["_meta"]["spans"], accepted[0]["_meta"]["spans"])

    def test_the_same_fact_of_the_same_sentence_with_another_wording_is_rejected(self):
        units = one_unit("Theschedulerordersprocessesbyprioritywhilethedispatcherstoreseachdecisioninthelogduringprocessing.")
        quote = "The scheduler orders processes by priority while the dispatcher stores each decision in the log during processing."
        accepted = []
        accept(candidate(quote, "How does the scheduler order processes?", "By priority",
                         ["By arrival time only", "By memory size", "At random"]), units, accepted)
        with self.assertRaises(CandidateRejected) as context:
            validate(candidate(quote, "According to the material, which criterion decides the order of processes?", "Priority",
                               ["Arrival time only", "Memory size", "Chance"]), units, accepted)
        self.assertEqual((context.exception.category, context.exception.code), ("duplicate", "duplicate_evidence"))

    def test_the_same_sentence_asked_again_in_other_words_is_rejected(self):
        accepted = []
        accept(self.english, self.units, accepted)
        again = candidate(FRONT["evidence_quote"], "What comes first in the ready queue when execution stops?", "The process at the front of the queue",
                          ["The process that arrived last", "The largest process", "A process chosen randomly"])
        with self.assertRaises(CandidateRejected) as context:
            validate(again, self.units, accepted)
        self.assertEqual(context.exception.category, "duplicate")

    def test_a_different_bullet_of_the_same_excerpt_is_a_new_question(self):
        accepted = []
        accept(self.english, self.units, accepted)
        other = candidate("Each process is given a time slice (quantum) before being preempted.",
                          "What is each process given in Round Robin before it is preempted?", "A time slice called a quantum",
                          ["A private memory partition", "An unlimited processor share", "A fixed priority level"])
        question, _ = validate(other, self.units, accepted)
        self.assertEqual(question["correct_answer"], "A")

    def test_overlap_is_measured_on_the_evidence_not_on_the_wording(self):
        units = one_unit("Thecacheholdscopiesoffrequentlyuseddataforfasteraccess. Thebufferqueuesoutgoingpacketsuntilthelinkisfree. Theregisterstoresthecurrentinstructionaddress.")
        accepted = []
        accept(candidate("The cache holds copies of frequently used data for faster access.", "What does the cache hold?",
                         "Copies of frequently used data", ["Outgoing packets", "Instruction addresses", "Free memory blocks"]), units, accepted)
        accept(candidate("The buffer queues outgoing packets until the link is free.", "What does the buffer queue?",
                         "Outgoing packets until the link is free", ["Copies of used data", "Instruction addresses", "Free blocks"]), units, accepted)
        self.assertEqual(len(accepted), 2)


class FollowUpsPreferUnusedEvidenceTests(unittest.TestCase):
    def setUp(self):
        FakeModel.reset()

    @staticmethod
    def bilingual_chunks(count=12):
        bullets = []
        for fact in range(count):
            english = fact_sentence(fact).replace(" ", "")
            vietnamese = (f"Thànhphần{fact_sentence(fact).split()[1]}xửlýdữliệutheocáchriêngbiệtsố{fact}trongquátrìnhvậnhành"
                          + "đồngthờiđảmbảotínhnhấtquánvàantoàncholuồngxửlýchínhcủatoànbộhệthốngtrongmọitrườnghợp." * 2)
            bullets.append(f"○ {english}→{vietnamese}")
        text = " ".join(bullets)
        half = len(text) // 3
        return [{"content": text[i * half:(i + 1) * half + (3 if i < 2 else 0)], "metadata": {"chunk_id": f"b{i + 1}"}} for i in range(3)]

    def run_engine(self, payloads, chunks, question_count=12):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=chunks, difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )

    def test_the_prompt_of_a_follow_up_no_longer_contains_the_used_passages(self):
        chunks = make_chunks(12, 2)
        result = self.run_engine([{"questions": candidates(range(5))}, {"questions": candidates(range(5, 12))}], chunks)
        self.assertEqual(len(result["questions"]), 12)
        second = FakeModel.prompts[1][FakeModel.prompts[1].index("EXCERPTS:"):]
        for used in range(5):
            self.assertNotIn(fact_sentence(used), second)
        self.assertTrue(all(fact_sentence(fact) in second for fact in range(5, 12)))

    def test_masking_hides_a_used_bullet_together_with_its_translation(self):
        units = build_study_units(self.bilingual_chunks())
        accepted = []
        for fact in (0, 1):
            quote = fact_sentence(fact)
            question, _ = validate(candidate(quote, f"What does the {quote.split()[1]} do?", fact_sentence(fact).split(" ", 2)[2].replace(" during processing.", ""),
                                             ["Something unrelated here", "Another invented option", "A third made up choice"]), units, accepted)
            accepted.append(question)
        shown = "\n".join(unit["evidence_excerpt"] for unit in mask_used_evidence(units, accepted))
        for used in (0, 1):
            self.assertNotIn(fact_sentence(used).replace(" ", ""), shown)                                  # the English bullet ...
            self.assertNotIn(f"số{used}trong", shown)                                                       # ... and its Vietnamese translation
        for unused in (2, 3, 11):
            self.assertIn(fact_sentence(unused).replace(" ", ""), shown)

    def test_the_retry_is_not_shown_what_call_1_already_turned_into_questions_while_unused_text_remains(self):
        chunks = self.bilingual_chunks()
        english = [candidate(fact_sentence(f), f"What does the {fact_sentence(f).split()[1]} do?", raw_candidate(f)["options"][0],
                             raw_candidate(f)["options"][1:]) for f in range(6)]
        second = [candidate(fact_sentence(f), f"What does the {fact_sentence(f).split()[1]} do?", raw_candidate(f)["options"][0],
                            raw_candidate(f)["options"][1:]) for f in range(6, 12)]
        result = self.run_engine([{"questions": english}, {"questions": second}], chunks)
        plan = result["assessment_plan"]
        self.assertEqual((len(result["questions"]), plan["llm_calls"], plan["status"]), (12, 2, "complete"))
        excerpts = FakeModel.prompts[1][FakeModel.prompts[1].index("EXCERPTS:"):]
        for used in range(6):
            self.assertNotIn(fact_sentence(used).replace(" ", ""), excerpts)       # the English bullet ...
            self.assertNotIn(f"số{used}trong", excerpts)                            # ... and its Vietnamese translation are cut out
        for unused in range(6, 12):
            self.assertIn(fact_sentence(unused).replace(" ", ""), excerpts)

    def test_used_passages_are_offered_again_when_too_little_unused_text_is_left(self):
        chunks = make_chunks(6, 2, pad=False)                           # 12 facts and nothing else
        result = self.run_engine([{"questions": candidates(range(9))}, {"questions": candidates(range(9, 12))}], chunks, question_count=12)
        plan = result["assessment_plan"]
        self.assertEqual((len(result["questions"]), plan["status"]), (12, "complete"))
        excerpts = FakeModel.prompts[1][FakeModel.prompts[1].index("EXCERPTS:"):]
        for fact in range(12):
            self.assertIn(fact_sentence(fact), excerpts)                           # nothing was hidden: 3 unused facts cannot carry 3 questions' worth
        self.assertIn("A sentence they used may still hold a different fact", FakeModel.prompts[1])
        self.assertIn("Questions already written:", FakeModel.prompts[1])          # ... the existing questions still steer the model

    def test_a_used_sentence_can_still_yield_a_different_fact_and_repeats_are_stopped(self):
        two_facts = [f"The {noun} {do} and the {other_noun} {other_do} during processing." for noun, do, other_noun, other_do in (
            ("mutex", "reserves resources", "scheduler", "orders processes"), ("paging", "maps addresses", "socket", "connects endpoints"),
            ("router", "forwards packets", "compiler", "translates sources"))]
        chunks = [{"content": " ".join(two_facts), "metadata": {"chunk_id": "c1"}}]

        def question(sentence, noun, do):
            return candidate(sentence, f"What does the {noun} do?", do, ["stores instructions", "loads images", "signals events"])

        first = [question(two_facts[0], "mutex", "reserves resources"), question(two_facts[1], "paging", "maps addresses"),
                 question(two_facts[2], "router", "forwards packets")]
        again = [question(two_facts[0], "mutex", "reserves resources"),            # the same fact again: a duplicate
                 candidate(two_facts[0], "What does the scheduler do?", "orders processes", ["guards counters", "assigns blocks", "verifies bytes"]),
                 candidate(two_facts[1], "What does the socket do?", "connects endpoints", ["guards counters", "assigns blocks", "verifies bytes"]),
                 candidate(two_facts[2], "What does the compiler do?", "translates sources", ["guards counters", "assigns blocks", "verifies bytes"])]
        result = self.run_engine([{"questions": first}, {"questions": again}, {"questions": []}, {"questions": []}], chunks, question_count=12)
        plan = result["assessment_plan"]
        self.assertEqual(len(result["questions"]), 6)                               # 3 sentences, 6 facts: capacity is the facts, not the sentences
        self.assertEqual(plan["status"], "partial")
        self.assertEqual(plan["validation_results"]["duplicate_rejections"], 1)      # only the genuine repeat
        self.assertEqual(plan["validation_results"]["grounding_rejections"], 0)
        self.assertLessEqual(plan["llm_calls"], 4)
        self.assertIn(two_facts[0], FakeModel.prompts[1])                           # the used sentence was offered again

    def test_a_follow_up_still_writes_only_what_is_missing(self):
        result = self.run_engine([{"questions": candidates(range(8))}, {"questions": candidates(range(8, 14))}], make_chunks(12, 2))
        self.assertEqual(len(result["questions"]), 12)
        self.assertEqual(result["assessment_plan"]["llm_calls"], 2)
        self.assertIn(f"Write exactly {quiz_units.followup_request(4)} easy", FakeModel.prompts[1])


class FollowUpEvidenceIsAPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(12, 2))
        self.accepted = []
        for fact in range(6):
            accept(raw_candidate(fact), self.units, self.accepted)

    def test_with_enough_unused_text_only_unused_text_is_offered(self):
        shown = quiz_units.followup_units(self.units, self.accepted, 7000, wanted_chars=1000)
        text = "\n".join(unit["evidence_excerpt"] for unit in shown)
        self.assertFalse(any(fact_sentence(fact) in text for fact in range(6)))
        self.assertTrue(all(fact_sentence(fact) in text for fact in range(6, 12)))

    def test_with_too_little_unused_text_the_whole_excerpts_come_back(self):
        shown = quiz_units.followup_units(self.units, self.accepted, 7000, wanted_chars=10 ** 6)
        text = "\n".join(unit["evidence_excerpt"] for unit in shown)
        self.assertTrue(all(fact_sentence(fact) in text for fact in range(12)))

    def test_less_used_excerpts_win_when_the_budget_cannot_hold_everything(self):
        usage_units = self.units[:4]
        accepted = [{"concept_id": "U1"}, {"concept_id": "U1"}, {"concept_id": "U2"}]
        chosen = quiz_units._least_used_first(usage_units, quiz_units.Counter(question["concept_id"] for question in accepted),
                                              budget_chars=usage_units[0]["char_count"] + usage_units[1]["char_count"] + 5)
        self.assertEqual([unit["unit_id"] for unit in chosen], ["U3", "U4"])       # U1 (2 questions) and U2 (1) wait

    def test_the_retry_prompt_explains_the_preference(self):
        prompt = build_generation_prompt("Lecture", "easy", self.units[:2], 4, ["What does the mutex do?"], [fact_sentence(0)])
        self.assertIn("prefer sentences and parts of the excerpts that they did not use", prompt)
        self.assertIn("A sentence they used may still hold a different fact", prompt)


class NegativePolarityTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units(make_chunks(16, facts_per_chunk=1))

    def rejected_code(self, stem):
        with self.assertRaises(CandidateRejected) as context:
            validate({**raw_candidate(3), "question": stem}, self.units)
        return context.exception.category, context.exception.code

    def test_not_except_incorrect_and_false_questions_are_rejected(self):
        for stem in (
            "Which of the following is NOT a task of the scheduler?",
            "All of the following describe the socket EXCEPT which one?",
            "All of the following describe the socket except one. Which?",
            "Which statement about the mutex is incorrect?",
            "Which statement about the mutex is false?",
            "Which of the following is not true about the cache?",
            "Which of the following does not describe the kernel?",
            "Which option is least likely to describe the router?",
            "Which is INCORRECT about the compiler?",
            "Which of the following is not a scheduling policy?",
            "Điều nào sau đây KHÔNG đúng về bộ định thời?",
            "Tất cả các phát biểu sau đều đúng ngoại trừ phát biểu nào?",
            "Phát biểu nào sai về bộ nhớ đệm?",
            "Câu nào sau đây không đúng?",
        ):
            with self.subTest(stem=stem):
                self.assertEqual(self.rejected_code(stem), ("structure", "negative_polarity"))

    def test_ordinary_questions_that_merely_contain_negations_are_kept(self):
        for stem in (
            "Which scheduling policy is non-preemptive?",
            "What does the mutex do when the notification arrives?",
            "Why is a notation needed for the scheduler?",
            "What happens when a process does not finish within its time slice?",
            "Which policy favours short processes?",
            "Why is the cache faster than main memory?",
            "What is the primary purpose of the mutex?",
        ):
            with self.subTest(stem=stem):
                question, _ = validate({**raw_candidate(3), "question": stem}, self.units)
                self.assertEqual(question["question"], stem)

    def test_the_prompt_tells_the_model_not_to_write_negative_questions(self):
        prompt = build_generation_prompt("Lecture", "easy", self.units, 5)
        self.assertIn("No negative questions", prompt)
        self.assertIn("must not contain NOT, EXCEPT, incorrect or false", prompt)
        self.assertIn("never ask which option is NOT true", prompt)

    def test_a_negative_question_whose_every_option_is_supported_never_reaches_the_quiz(self):
        """The reported failure: 'Which is NOT ...?' where all four options are stated in the material."""
        facts = [fact_sentence(f) for f in range(4)]
        units = one_unit(" ".join(facts))
        question = candidate(
            facts[0], "Which of the following is NOT stated about the system?", "The mutex reserves resources during processing",
            ["The scheduler orders processes during processing", "The paging maps addresses during processing", "The socket connects endpoints during processing"])
        with self.assertRaises(CandidateRejected) as context:
            validate(question, units)
        self.assertEqual(context.exception.code, "negative_polarity")


class AnswerInEvidenceIsConsistentTests(unittest.TestCase):
    def test_a_supported_answer_written_in_another_order_is_recorded_as_in_evidence(self):
        units = one_unit("Thescheduleruses First-Come-First-Served(FCFS) tochoosetheprocessatthefrontofthequeuewheneverthecpuisfree.")
        question, warnings = validate(candidate(
            "The scheduler uses First-Come-First-Served (FCFS) to choose the process at the front of the queue whenever the CPU is free.",
            "Which policy chooses the process at the front of the queue?", "FCFS (First-Come-First-Served)",
            ["Round Robin with a time slice", "Shortest remaining time first", "Highest response ratio next"]), units)
        self.assertNotIn("unverified_answer", warnings)
        self.assertTrue(question["_meta"]["answer_in_evidence"])
        selected, evidence = quiz_units.finalize_questions([question])
        self.assertTrue(evidence[0]["answer_in_evidence"])

    def test_an_answer_found_in_a_neighbouring_excerpt_is_in_evidence_too(self):
        padding = " Background about the machine: " + "surrounding notes on hardware timers and queues " * 12
        units = build_study_units([
            {"content": "Header section." + padding + " The dispatcher is the component that hands the processor to the chosen process.", "metadata": {"chunk_id": "a"}},
            {"content": "Second section." + padding + " Nothing else of interest is stated here about anything at all.", "metadata": {"chunk_id": "b"}},
        ])
        question, warnings = validate(candidate(
            "The dispatcher is the component that hands the processor to the chosen process.",
            "Which component hands the processor to the chosen process?", "The dispatcher",
            ["The compiler", "The linker", "The assembler"]), units)
        self.assertTrue(question["_meta"]["answer_in_evidence"])
        self.assertNotIn("unverified_answer", warnings)

    def test_an_answer_with_no_word_to_anchor_is_unverified_and_recorded_as_not_in_evidence(self):
        units = one_unit("Thequantumofeachprocessis 42 milliseconds inthisconfigurationofthesystemundertest.")
        question, warnings = validate(candidate(
            "The quantum of each process is 42 milliseconds in this configuration of the system under test.",
            "How long is the time quantum of a process here?", "17 ms", ["9 ms", "31 ms", "64 ms"]), units)
        self.assertIn("unverified_answer", warnings)
        self.assertFalse(question["_meta"]["answer_in_evidence"])

    def test_the_flag_and_the_warning_always_agree_across_a_whole_run(self):
        units = build_study_units(make_chunks(12, 2))
        accepted, mismatches = [], []
        for raw in candidates(range(24)):
            try:
                question, warnings = validate(raw, units, accepted)
            except CandidateRejected:
                continue
            accepted.append(question)
            if question["_meta"]["answer_in_evidence"] == ("unverified_answer" in warnings):
                mismatches.append(question["question"])
        self.assertEqual(mismatches, [])
        self.assertEqual(len(accepted), 24)


class CountsPartialsAndStoppingStillHoldTests(unittest.TestCase):
    def setUp(self):
        FakeModel.reset()

    def run_engine(self, payloads, question_count, chunks=None):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=chunks or make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )

    def test_12_15_18_and_20_complete_in_one_call_and_never_call_again(self):
        for count in (12, 15, 18, 20):
            with self.subTest(count=count):
                result = self.run_engine([{"questions": candidates(range(quiz_units.candidate_target(count)))}, {"questions": candidates(range(6))}], count)
                plan = result["assessment_plan"]
                self.assertEqual((len(result["questions"]), plan["status"], plan["llm_calls"]), (count, "complete", 1))
                self.assertEqual(len(FakeModel.prompts), 1)

    def test_a_short_first_call_is_completed_by_one_follow_up_and_then_the_loop_stops(self):
        for count in (12, 15, 18, 20):
            with self.subTest(count=count):
                short = count - 5
                result = self.run_engine([{"questions": candidates(range(short))},
                                          {"questions": candidates(range(short, FACT_COUNT))},
                                          {"questions": candidates(range(6))}], count)
                plan = result["assessment_plan"]
                self.assertEqual((len(result["questions"]), plan["status"], plan["llm_calls"]), (count, "complete", 2))
                self.assertEqual(len(FakeModel.prompts), 2)

    def test_partial_quizzes_still_return_every_valid_question(self):
        result = self.run_engine([{"questions": candidates(range(7))}, {"questions": []}, {"questions": []}], 18)
        plan = result["assessment_plan"]
        self.assertEqual((len(result["questions"]), plan["status"], plan["requested_count"], plan["actual_count"]), (7, "partial", 18, 7))
        self.assertLessEqual(plan["llm_calls"], quiz_units.max_llm_calls(18))

    def test_the_call_ceiling_is_unchanged(self):
        self.assertEqual({n: quiz_units.max_llm_calls(n) for n in (12, 15, 18, 20)}, {12: 4, 15: 4, 18: 5, 20: 5})


if __name__ == "__main__":
    unittest.main()
