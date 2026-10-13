"""Normal Quiz reliability + speed (two real runs: Go Language and TCP Socket, 12 requested, 8 returned,
~30 of ~37 candidates rejected, 4 LLM calls, 100-120 s).

1. One fact / one statement may be asked about several times for DIFFERENT aspects; only a true
   near-duplicate question is rejected.
2. Bad distractors are repaired locally (no LLM call); synthetic distractors are fine; a second
   option that the evidence also supports is repaired, or the candidate is rejected.
3. A too-short quote is widened to its exact source statement; an ungrounded answer is still rejected.
4. answer_in_evidence is True for "0" when the quote is "Array •Fixed size •Index starts at 0".
5. At most 3 LLM calls: 12 -> ask 14, then missing + 2, then missing + 1, never a 4th call.
"""

import re
import unittest
from unittest.mock import patch

from quiz_fixtures import FakeModel, candidates, make_chunks, raw_candidate

from backend import quiz_service, quiz_units
from backend.quiz_service import _generate_quiz_from_units
from backend.quiz_units import CandidateRejected, build_study_units, repair_evidence, squash, validate_candidate

SCOPE = {"topic_id": "document", "name": "Entire document"}
DOCUMENT = {"id": "go.pdf", "title": "Go Networking", "hash": "hash", "topic_schema_version": 2}

GO_TEXT = (
    'TCP Server in Go. The server calls net.Listen("tcp", ":8080") to listen for incoming connections. '
    'The client calls net.Dial("tcp", "localhost:8080") to connect to the server. '
    "The fmt.Println function prints a line to standard output. "
    "A bufio.NewReader wraps the connection for buffered reads. "
    "Go was first released to the public in 2009 after work began in 2007. "
    "Array •Fixed size •Index starts at 0 "
    "Slice •Dynamic size •Backed by an array"
)
LISTEN = 'The server calls net.Listen("tcp", ":8080") to listen for incoming connections.'


def go_units():
    return build_study_units([{"content": GO_TEXT, "metadata": {"chunk_id": "go1"}}])


def candidate(quote, question, correct, wrong, answer_index=0, **overrides):
    options = list(wrong)
    options.insert(answer_index, correct)
    result = {"evidence_quote": quote, "question": question, "options": options, "answer_index": answer_index,
              "explanation": "The document states this directly."}
    result.update(overrides)
    return result


def validate(raw, units, accepted=None, bank=None):
    accepted = accepted if accepted is not None else []
    return validate_candidate(raw, units, accepted, "easy", len(accepted) + 1, SCOPE, len(accepted) + 1,
                              language="english", distractor_bank=bank)


def bodies(question):
    return [option[3:] for option in question["options"]]


class SameFactDifferentAspectTests(unittest.TestCase):
    def setUp(self):
        self.units = go_units()

    def test_one_code_statement_yields_questions_about_different_aspects(self):
        accepted = []
        for raw in (
            candidate(LISTEN, "Which function does the server call to listen for connections?", "net.Listen",
                      ["net.Dial", "fmt.Println", "bufio.NewReader"]),
            candidate(LISTEN, 'Which port does net.Listen("tcp", ":8080") listen on?', "8080", ["8000", "8081", "443"]),
            candidate(LISTEN, 'Which protocol does net.Listen("tcp", ":8080") listen on?', "tcp", ["udp", "http", "icmp"]),
            candidate(LISTEN, "What is the purpose of the server's net.Listen call?", "To listen for incoming connections",
                      ["To print a line of output", "To buffer reads from a file", "To close idle connections"]),
        ):
            question, _ = validate(raw, self.units, accepted)
            accepted.append(question)
        self.assertEqual(len(accepted), 4)
        self.assertEqual({question["concept_id"] for question in accepted}, {self.units[0]["unit_id"]})

    def test_the_same_evidence_and_the_same_answer_are_allowed_for_different_aspects(self):
        quote = ('The server calls net.Listen("tcp", ":8080") to listen for incoming connections. '
                 'The client calls net.Dial("tcp", "localhost:8080") to connect to the server.')
        accepted = []
        for raw in (
            candidate(quote, "Which protocol does the server pass to net.Listen?", "tcp", ["udp", "http", "icmp"]),
            candidate(quote, "Which protocol does the client pass to net.Dial?", "tcp", ["udp", "http", "icmp"]),
        ):
            question, _ = validate(raw, self.units, accepted)
            accepted.append(question)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0]["_meta"]["spans"], accepted[1]["_meta"]["spans"])           # same evidence
        self.assertEqual(accepted[0]["_meta"]["answer_key"], accepted[1]["_meta"]["answer_key"])   # same answer
        # ... while the same aspect asked again in other words is the same question
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate(quote, "When the server calls net.Listen, which protocol name does it use?", "tcp",
                               ["udp", "http", "icmp"]), self.units, accepted)
        self.assertEqual(caught.exception.category, "duplicate")

    def test_a_true_near_duplicate_is_still_rejected(self):
        accepted = []
        first, _ = validate(candidate(LISTEN, 'Which port does net.Listen("tcp", ":8080") listen on?', "8080",
                                      ["8000", "8081", "443"]), self.units, accepted)
        accepted.append(first)
        for raw in (
            candidate(LISTEN, 'Which port does net.Listen("tcp", ":8080") listen on?', "8080", ["21", "22", "25"]),
            candidate(LISTEN, 'What port does net.Listen("tcp", ":8080") listen on?', "Port 8080", ["21", "22", "25"]),
            candidate(LISTEN, "On which port number does the server listen for incoming connections?", "8080",
                      ["21", "22", "25"]),
        ):
            with self.subTest(question=raw["question"]):
                with self.assertRaises(CandidateRejected) as caught:
                    validate(raw, self.units, accepted)
                self.assertEqual(caught.exception.category, "duplicate")

    def test_inflected_rewording_of_the_same_question_is_a_duplicate(self):
        accepted = []
        quote = "Array •Fixed size •Index starts at 0 Slice •Dynamic size"
        first, _ = validate(candidate(quote, "Which size does an Array have?", "Fixed size",
                                      ["Dynamic size", "Unlimited size", "Random size"]), self.units, accepted)
        accepted.append(first)
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate(quote, "Which size do Arrays have?", "A fixed size",
                               ["Growing size", "Unlimited size", "Random size"]), self.units, accepted)
        self.assertEqual(caught.exception.category, "duplicate")


class DistractorRepairTests(unittest.TestCase):
    def setUp(self):
        self.units = go_units()

    def test_nearby_numbers_replace_bad_numeric_distractors(self):
        question, warnings = validate(candidate(
            "Array •Fixed size •Index starts at 0", "At which index does an Array start?", "0", ["0", "None of the above", ""]),
            self.units)
        self.assertEqual(bodies(question), ["0", "-1", "1", "2"])
        self.assertIn("options_repaired", warnings)
        question, _ = validate(candidate(
            "Go was first released to the public in 2009 after work began in 2007.", "Work on Go began in which year?",
            "2007", ["2007", "2007.", "all of the above"]), self.units)
        self.assertEqual(bodies(question), ["2007", "2005", "2008", "2010"])

    def test_other_identifiers_of_the_material_replace_bad_identifier_distractors(self):
        question, warnings = validate(candidate(
            'The client calls net.Dial("tcp", "localhost:8080") to connect to the server.',
            "Which function does the client call to connect to the server?", "net.Dial", ["net.Dial", "net.dial", "none of the above"]),
            self.units)
        self.assertEqual(bodies(question), ["net.Dial", "net.Listen", "fmt.Println", "bufio.NewReader"])
        self.assertIn("options_repaired", warnings)

    def test_only_the_wrong_options_change_and_the_correct_answer_keeps_its_position(self):
        raw = candidate(LISTEN, "What is the purpose of the server's net.Listen call?", "To listen for incoming connections",
                        ["To print a line of output", "To print a line of output", "To buffer reads"], answer_index=2)
        question, _ = validate(raw, self.units, bank=["To close the connection", "To format strings"])
        self.assertEqual(question["correct_answer"], "C")
        self.assertEqual(bodies(question)[2], "To listen for incoming connections")
        self.assertEqual(bodies(question)[0], "To print a line of output")
        self.assertEqual(question["question"], raw["question"])
        self.assertEqual(question["_meta"]["quote"], LISTEN)
        self.assertEqual(len(set(bodies(question))), 4)

    def test_opposite_actions_and_options_from_the_same_call_are_used(self):
        question, _ = validate(candidate(
            'The client calls net.Dial("tcp", "localhost:8080") to connect to the server.',
            "What does the client do with net.Dial?", "Connects to the server", ["Connects to the server", "", ""]),
            self.units, bank=["Listens for connections", "Prints a line"])
        self.assertEqual(bodies(question), ["Connects to the server", "Connects to the client",   # opposite role
                                            "Listens for connections", "Prints a line"])          # same-call options

    def test_a_question_with_nothing_to_repair_from_is_rejected(self):
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate(LISTEN, "What is the purpose of the server's net.Listen call?", "To listen for incoming connections",
                               ["To listen for incoming connections", "None of the above", ""]), self.units)
        self.assertEqual((caught.exception.category, caught.exception.code), ("structure", "options"))

    def test_synthetic_distractors_absent_from_the_source_are_accepted_unchanged(self):
        raw = candidate(LISTEN, "Which function does the server call to listen for connections?", "net.Listen",
                        ["net.Serve", "http.ListenAndServe", "os.Open"])
        question, warnings = validate(raw, self.units)
        self.assertEqual(bodies(question), ["net.Listen", "net.Serve", "http.ListenAndServe", "os.Open"])
        self.assertNotIn("options_repaired", warnings)
        self.assertEqual(question["validation_outcome"], "accepted")


class MultipleCorrectOptionTests(unittest.TestCase):
    def setUp(self):
        self.units = go_units()

    def test_a_second_supported_list_item_is_replaced(self):
        raw = candidate("Array •Fixed size •Index starts at 0", "Which is a feature of an Array?", "Fixed size",
                        ["Index starts at 0", "Dynamic size", "Unlimited length"])
        question, warnings = validate(raw, self.units, bank=["Backed by a map", "Grows on demand"])
        self.assertNotIn("Index starts at 0", bodies(question))
        self.assertEqual(bodies(question)[0], "Fixed size")
        self.assertIn("options_repaired", warnings)

    def test_rejected_when_the_supported_option_cannot_be_replaced(self):
        raw = candidate("Array •Fixed size •Index starts at 0", "Which is a feature of an Array?", "Fixed size",
                        ["Index starts at 0", "Dynamic size", "Unlimited length"])
        with patch.object(quiz_units, "_distractor_alternatives", return_value=[]):
            with self.assertRaises(CandidateRejected) as caught:
                validate(raw, self.units)
        self.assertEqual(caught.exception.code, "multiple_correct")

    def test_a_specific_question_keeps_a_distractor_named_in_the_quote(self):
        raw = candidate("Array •Fixed size •Index starts at 0", "At which index does an Array start?", "0",
                        ["1", "-1", "2"])
        question, warnings = validate(raw, self.units)
        self.assertNotIn("options_repaired", warnings)


class EvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.units = go_units()

    def test_a_too_short_quote_is_widened_to_its_exact_source_text(self):
        question, warnings = validate(candidate('net.Listen("tcp", ":8080")', "Which port does the server listen on?", "8080",
                                                ["8000", "8081", "443"]), self.units)
        quote = question["_meta"]["quote"]
        self.assertIn("evidence_repaired", warnings)
        self.assertIn(quote, self.units[0]["evidence_excerpt"])                  # exact source text, never invented
        self.assertIn('net.Listen("tcp", ":8080")', quote)
        self.assertGreaterEqual(len(squash(quote)), quiz_units.QUIZ_MIN_QUOTE_CHARS)

    def test_a_short_list_item_takes_its_neighbouring_item_from_the_source(self):
        repaired = repair_evidence("Index starts at 0", self.units, "0")
        self.assertIsNotNone(repaired)
        text, _located = repaired
        self.assertTrue(text.startswith("Fixed size •Index starts at 0"))   # the previous bullet joins it
        self.assertIn(text, self.units[0]["evidence_excerpt"])

    def test_a_slightly_changed_quote_is_replaced_by_the_real_sentence(self):
        question, warnings = validate(candidate(
            'The client calls net.Dial("tcp", "localhost:8080") to connect to the server in the example program.',
            "Which function does the client call to connect?", "net.Dial", ["net.Listen", "fmt.Println", "os.Open"]), self.units)
        self.assertIn("evidence_repaired", warnings)
        self.assertEqual(question["_meta"]["quote"], 'The client calls net.Dial("tcp", "localhost:8080") to connect to the server.')

    def test_an_invented_quote_is_still_rejected(self):
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate("Goroutines are scheduled by the operating system kernel threads only.",
                               "How are goroutines scheduled?", "By kernel threads", ["By the GC", "By channels", "By maps"]), self.units)
        self.assertEqual(caught.exception.code, "quote_not_found")

    def test_an_ungrounded_answer_is_never_rescued_by_a_repair(self):
        # the widened statement does not state the answer, so no repair happens at all
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate('net.Listen("tcp", ":8080")', "What does the server use to accept connections?",
                               "A goroutine pool scheduler", ["A mutex", "A channel", "A map"]), self.units)
        self.assertEqual(caught.exception.code, "quote_too_short")
        # ... and with a real quote an invented answer is still the hard answer_not_in_context rejection
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate(LISTEN, "What does the server use to accept connections?",
                               "A goroutine pool scheduler", ["A mutex", "A channel", "A map"]), self.units)
        self.assertEqual(caught.exception.code, "answer_not_in_context")


class AnswerInEvidenceTests(unittest.TestCase):
    def test_a_numeric_answer_written_in_the_quote_is_in_evidence(self):
        units = go_units()
        question, warnings = validate(candidate("Array •Fixed size •Index starts at 0 Slice", "At which index does an Array start?",
                                                "0", ["1", "-1", "2"]), units)
        self.assertTrue(question["_meta"]["answer_in_evidence"])
        self.assertNotIn("unverified_answer", warnings)
        _questions, evidence = quiz_units.finalize_questions([question])
        self.assertTrue(evidence[0]["answer_in_evidence"])

    def test_a_number_not_in_the_quote_is_still_unverified(self):
        question, warnings = validate(candidate("Array •Fixed size •Index starts at 0 Slice", "At which index does an Array start?",
                                                "7", ["1", "-1", "2"]), go_units())
        self.assertFalse(question["_meta"]["answer_in_evidence"])
        self.assertIn("unverified_answer", warnings)


class CallBudgetTests(unittest.TestCase):
    def run_engine(self, payloads, question_count=12):
        FakeModel.reset(payloads)
        with (
            patch.object(quiz_service, "ChatOllama", FakeModel),
            patch.object(quiz_service, "save_quiz_validation_event"),
            patch.object(quiz_service, "save_quiz", side_effect=lambda _d, _x, quiz, _o: quiz),
        ):
            return _generate_quiz_from_units(
                document=DOCUMENT, scope="document", scope_topic_id="document", scope_topic_name="Entire document",
                chunks=make_chunks(12, 2), difficulty="easy", owner_id="owner", model_id="qwen-test", regenerate=False,
                question_count=question_count,
            )

    @staticmethod
    def asks():
        return [int(re.search(r"Write exactly (\d+)", prompt).group(1)) for prompt in FakeModel.prompts]

    def test_a_clean_12_question_run_is_one_call_asking_for_14(self):
        result = self.run_engine([{"questions": candidates(range(14))}, {"questions": candidates(range(14, 20))}])
        plan = result["assessment_plan"]
        self.assertEqual((len(result["questions"]), plan["status"], plan["llm_calls"]), (12, "complete", 1))
        self.assertEqual(self.asks(), [14])
        self.assertEqual({question["question_type"] for question in result["questions"]}, {"single_choice"})
        for question in result["questions"]:
            self.assertEqual((len(question["options"]), len(question["correct_answers"])), (4, 1))

    def test_bad_distractors_do_not_cost_an_extra_call(self):
        broken = [raw_candidate(fact, options=[raw_candidate(fact)["options"][0]] * 2 + ["None of the above", ""])
                  for fact in range(4)]
        result = self.run_engine([{"questions": broken + candidates(range(4, 12))}])
        plan = result["assessment_plan"]
        self.assertEqual((len(result["questions"]), plan["llm_calls"]), (12, 1))
        self.assertEqual(plan["validation_results"]["options_repaired"], 4)

    def test_a_hard_run_never_makes_more_than_3_calls(self):
        payloads = [{"questions": candidates(range(4))}, {"questions": candidates(range(4, 7))},
                    {"questions": candidates(range(7, 9))}, {"questions": candidates(range(9, 20))}]
        result = self.run_engine(payloads)
        plan = result["assessment_plan"]
        self.assertEqual((plan["llm_calls"], plan["max_llm_calls"], len(FakeModel.prompts)), (3, 3, 3))
        self.assertEqual(self.asks(), [14, 10, 6])                 # 14, then 8 missing + 2, then 5 missing + 1
        self.assertEqual((len(result["questions"]), plan["status"]), (9, "partial"))
        self.assertEqual(len(FakeModel.payloads), 1)              # a 4th call is never made

    def test_every_count_is_capped_at_3_calls(self):
        for count in (12, 15, 18, 20):
            with self.subTest(count=count):
                result = self.run_engine([{"questions": candidates([call])} for call in range(6)], question_count=count)
                self.assertEqual(result["assessment_plan"]["llm_calls"], 3)
                self.assertEqual(self.asks()[0], quiz_units.candidate_target(count))


CODE_STATEMENT = 'ln, err := net.Listen("tcp", ":8080")'


def code_units():
    return build_study_units([{"content": (
        f"Server setup code: {CODE_STATEMENT} returns a listener and an error result for the server. "
        "if err != nil { log.Fatal(err) }"), "metadata": {"chunk_id": "code1"}}])


class SingleStatementReuseTests(unittest.TestCase):
    """The evidence is ONE code statement; questions on it are kept unless they mean the same."""

    def setUp(self):
        self.units = code_units()

    def accept_all(self, raws):
        accepted = []
        for raw in raws:
            question, _ = validate(raw, self.units, accepted)
            accepted.append(question)
        return accepted

    def test_different_aspects_of_one_statement_coexist(self):
        accepted = self.accept_all([
            candidate(CODE_STATEMENT, "Which function creates the listener?", "net.Listen", ["net.Dial", "net.Accept", "os.Open"]),
            candidate(CODE_STATEMENT, "Which port does the server listen on?", "8080", ["8000", "8081", "443"]),
            candidate(CODE_STATEMENT, "Which protocol does net.Listen use here?", "tcp", ["udp", "http", "icmp"]),
            candidate(CODE_STATEMENT, "Which variable receives the listener?", "ln", ["err", "conn", "addr"]),
        ])
        self.assertEqual(len(accepted), 4)

    def test_the_same_answer_on_one_statement_is_kept_for_a_different_aspect(self):
        accepted = self.accept_all([
            candidate(CODE_STATEMENT, "Which function creates the listener?", "net.Listen", ["net.Dial", "net.Accept", "os.Open"]),
            candidate(CODE_STATEMENT, "Which call's error result is stored in err?", "net.Listen", ["net.Dial", "net.Accept", "os.Open"]),
        ])
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0]["_meta"]["quote"], accepted[1]["_meta"]["quote"])            # one statement
        self.assertEqual(accepted[0]["_meta"]["answer_key"], accepted[1]["_meta"]["answer_key"])  # one answer

    def test_a_true_rewording_on_the_same_statement_is_rejected(self):
        accepted = self.accept_all([
            candidate(CODE_STATEMENT, "Which port does the server listen on?", "8080", ["8000", "8081", "443"])])
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate(CODE_STATEMENT, "What is the server's listening port?", "8080", ["21", "22", "25"]),
                     self.units, accepted)
        self.assertEqual(caught.exception.category, "duplicate")


GO_HISTORY = "Robert Griesemer, Ken Thompson, and Rob Pike started the project."


class MultipleSupportedOptionTests(unittest.TestCase):
    def setUp(self):
        self.units = build_study_units([{"content": f"History of Go. {GO_HISTORY} " + "Go history notes " * 20,
                                         "metadata": {"chunk_id": "h1"}}])
        self.raw = candidate(GO_HISTORY, "Who started the project?", "Ken Thompson",
                             ["Rob Pike", "Dennis Ritchie", "Linus Torvalds"])

    def test_a_second_founder_among_the_options_is_repaired(self):
        question, warnings = validate(self.raw, self.units, bank=["Bjarne Stroustrup", "Guido van Rossum"])
        names = bodies(question)
        self.assertEqual(question["correct_answer"], "A")
        self.assertEqual(names[0], "Ken Thompson")
        self.assertFalse({"Rob Pike", "Robert Griesemer"} & set(names[1:]))     # never two supported answers
        self.assertIn("options_repaired", warnings)

    def test_rejected_as_multiple_correct_when_it_cannot_be_repaired(self):
        with patch.object(quiz_units, "_distractor_alternatives", return_value=["Robert Griesemer"]):
            with self.assertRaises(CandidateRejected) as caught:
                validate(self.raw, self.units)
        self.assertEqual(caught.exception.code, "multiple_correct")


class ListSiblingIsNotAutomaticallyCorrectTests(unittest.TestCase):
    def test_founder_list_second_valid_member_is_detected(self):
        units = build_study_units([{"content": f"History of Go. {GO_HISTORY} " + "Go history notes " * 20,
                                    "metadata": {"chunk_id": "h1"}}])
        self.assertTrue(quiz_units._also_correct("Rob Pike", "Ken Thompson", "Who started the project?", GO_HISTORY))
        with patch.object(quiz_units, "_distractor_alternatives", return_value=[]):
            with self.assertRaises(CandidateRejected) as caught:
                validate(candidate(GO_HISTORY, "Who started the project?", "Ken Thompson",
                                   ["Rob Pike", "Dennis Ritchie", "Linus Torvalds"]), units)
        self.assertEqual(caught.exception.code, "multiple_correct")

    def test_list_siblings_that_do_not_satisfy_the_question_stay_valid_distractors(self):
        source = "Go supports arrays, slices, maps. Arrays have fixed size."
        units = build_study_units([{"content": f"Go types. {source} " + "Go type notes " * 20, "metadata": {"chunk_id": "t1"}}])
        self.assertFalse(quiz_units._also_correct("Slices", "Arrays", "Which type has fixed size?", source))
        self.assertFalse(quiz_units._also_correct("Maps", "Arrays", "Which type has fixed size?", source))
        question, warnings = validate(candidate(source, "Which type has fixed size?", "Arrays",
                                                ["Slices", "Maps", "Channels"]), units)
        self.assertEqual(bodies(question), ["Arrays", "Slices", "Maps", "Channels"])     # nothing replaced
        self.assertNotIn("options_repaired", warnings)


class EvidenceRepairSafetyTests(unittest.TestCase):
    def setUp(self):
        self.units = go_units()

    def test_a_misleading_partial_quote_is_not_repaired(self):
        # most of the quote is a real sentence, but the claim the answer rests on is invented
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate('The client calls net.Dial("tcp", "localhost:8080") and retries three times on failure.',
                               "How many times does the client retry?", "Three times", ["Once", "Twice", "Five times"]),
                     self.units)
        self.assertEqual(caught.exception.code, "quote_not_found")
        self.assertIsNone(repair_evidence("to connect to the server", self.units, "net.Listen"))   # generic phrase elsewhere

    def test_a_generic_short_quote_is_not_attached_to_unrelated_text(self):
        with self.assertRaises(CandidateRejected) as caught:
            validate(candidate("to the server", "Which function does the server call to listen?", "net.Listen",
                               ["net.Dial", "fmt.Println", "os.Open"]), self.units)
        self.assertEqual(caught.exception.code, "quote_too_short")


class AnswerInEvidenceExactCaseTests(unittest.TestCase):
    def test_array_index_starts_at_0_answer_is_in_evidence(self):
        question, warnings = validate(candidate("Array •Fixed size •Index starts at 0", "At which index does an Array start?",
                                                "0", ["1", "-1", "2"]), go_units())
        self.assertEqual(question["_meta"]["quote"], "Array •Fixed size •Index starts at 0")
        self.assertTrue(question["_meta"]["answer_in_evidence"])
        self.assertNotIn("unverified_answer", warnings)


class CallPolicyValueTests(unittest.TestCase):
    def test_the_actual_call_policy_values(self):
        self.assertEqual(quiz_units.candidate_target(12), 14)
        self.assertEqual(quiz_units.max_llm_calls(12), 3)
        self.assertEqual(quiz_units.followup_request(4, 2), 6)
        self.assertEqual(quiz_units.followup_request(2, 3), 3)


if __name__ == "__main__":
    unittest.main()
