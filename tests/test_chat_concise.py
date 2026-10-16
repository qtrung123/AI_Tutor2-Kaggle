"""Concise, focused AI Tutor answers (CHAT-PERF-3): the RAG prompt asks for a direct answer of a
fitting length (depth only on request, the student's language), the conversation history is short
and bounded, an Explain More request never bloats later prompts or retrieval queries, and the output
cap is only a safety limit -- a capped answer is saved ending on a full sentence, streamed or not."""

import unittest
from unittest.mock import patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, AIMessageChunk

from backend import llm_backend, rag_service
from config import CHAT_MAX_TOKENS

TEMPLATE = rag_service.load_prompt_template()
EXPLAIN_MORE = "\n".join([
    'Explain more about this quiz question from the document "Operating Systems" (os.pdf).',
    "Question type: Single choice",
    "Question: Which component translates virtual addresses to physical addresses?",
    "Options:", "  A. The MMU", "  B. The scheduler",
    "My answer: The scheduler", "Correct answer: The MMU", "Result: I answered it incorrectly.",
    "Please explain clearly, using only this document:", "1. Why the correct answer is correct.",
    "2. Why my answer is wrong or incomplete.", "3. The underlying concept.", "4. Optionally, a short simple example.",
])
LONG_ANSWER = ("The MMU looks up the page table entry for the page (source: os.pdf, page 12). "
               "It joins the frame number with the offset. ") * 12
DOC = Document(page_content="The MMU maps pages to frames.", metadata={"source": "os.pdf", "page": 12,
                                                                       "owner_id": "u", "document_id": "os.pdf"})


def turn(question, answer=LONG_ANSWER):
    return [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]


class FakeModel:
    calls = []
    reply = "The MMU translates addresses (source: os.pdf, page 12)."
    metadata = {"done_reason": "stop"}

    def __init__(self, **options):
        self.options = options

    def invoke(self, prompt):
        FakeModel.calls.append((self.options, prompt))
        return AIMessage(content=FakeModel.reply, response_metadata=FakeModel.metadata)

    def stream(self, prompt):
        FakeModel.calls.append((self.options, prompt))
        words = FakeModel.reply.split(" ")
        for index, word in enumerate(words):
            yield AIMessageChunk(content=word + (" " if index < len(words) - 1 else ""))
        yield AIMessageChunk(content="", response_metadata=FakeModel.metadata)


def ask(question, history=(), streaming=False):
    """One answer through rag_service with storage and retrieval faked: (result, options, prompt, query)."""
    FakeModel.calls = []
    conversation = {"title": "Chat", "document_id": "os.pdf", "messages": list(history)}
    with patch.object(rag_service, "ChatOllama", FakeModel), \
            patch.object(rag_service, "resolve_generation_model", return_value="qwen"), \
            patch.object(rag_service, "get_conversation", return_value=conversation), \
            patch.object(rag_service, "add_message", side_effect=lambda *a: {"role": a[2], "content": a[3]}), \
            patch.object(rag_service, "add_exchange", side_effect=lambda owner, conv, user, answer, *a, **k: (
                {"role": "user", "content": user}, {"role": "assistant", "content": answer})), \
            patch.object(rag_service, "_retrieve_conversation_docs", return_value=[DOC]) as retrieve:
        if streaming:
            events = list(rag_service.stream_conversation_message("u", "c", question, "qwen-2.5-7b"))
            result = {**events[-1], "deltas": "".join(e["text"] for e in events if e["type"] == "delta")}
        else:
            result = rag_service.answer_conversation_message("u", "c", question, "qwen-2.5-7b")
    options, prompt = FakeModel.calls[0]
    return result, options, prompt, retrieve.call_args.args[1]


def history_of(prompt):
    return prompt.split("Recent conversation (for references only, not evidence):\n", 1)[1].split("\n\nContext:\n", 1)[0]


class PromptInstructionTests(unittest.TestCase):
    def test_default_is_a_direct_answer_of_fitting_length(self):
        for phrase in ("Start with the answer itself", "No introduction", "no closing summary", "say each thing once",
                       "2-4 sentences", "about 80-150 words", "Answer in the language of the student's question"):
            self.assertIn(phrase, TEMPLATE)
        # Formatting is used when it helps, never as a fixed template.
        self.assertIn('a "### " heading only when the answer has several distinct parts', TEMPLATE)
        self.assertNotIn("FORMAT", TEMPLATE)

    def test_grounding_and_citation_rules_are_kept(self):
        self.assertIn("Use ONLY the provided context", TEMPLATE)
        self.assertIn('"I don\'t know based on the provided documents."', TEMPLATE)   # grounding_status relies on it
        self.assertIn("(source: page)", TEMPLATE)                                  # rendered as citation chips
        self.assertIn("It is never evidence", TEMPLATE)
        self.assertEqual(TEMPLATE.count("{context}") + TEMPLATE.count("{conversation_history}") + TEMPLATE.count("{question}"), 3)
        self.assertLess(TEMPLATE.index("{conversation_history}"), TEMPLATE.index("{context}"))
        self.assertTrue(TEMPLATE.rstrip().endswith("Answer:"))

    def test_explicit_requests_for_depth_are_honoured_by_the_prompt_not_cut_by_the_cap(self):
        self.assertIn('Go into depth only when the student asks for it (details, steps, examples, "explain more"', TEMPLATE)
        for question in ("What is paging?", "Explain paging in detail, step by step, with an example."):
            with self.subTest(question=question):
                _, options, prompt, _ = ask(question)
                self.assertEqual(options["num_predict"], CHAT_MAX_TOKENS)        # one safety cap, no shortening trick
                self.assertEqual(options["temperature"], 0)
                self.assertTrue(prompt.rstrip().endswith(f"Question:\n{question}\n\nAnswer:"))
        self.assertGreaterEqual(CHAT_MAX_TOKENS, 1024)


class HistoryTests(unittest.TestCase):
    def test_long_history_is_bounded_and_keeps_the_latest_exchange(self):
        history = [message for index in range(20) for message in turn(f"Question number {index} about paging?")]
        text = rag_service._history_for_prompt(history)
        self.assertLessEqual(len(text), rag_service.HISTORY_TOTAL_CHARS)
        self.assertLessEqual(len(text.splitlines()), rag_service.HISTORY_MESSAGES)
        self.assertIn("Question number 19 about paging?", text)
        self.assertNotIn("Question number 0 ", text)
        self.assertNotIn("(source:", text)                      # past answers are not citable evidence
        for line in text.splitlines():
            if line.startswith("Tutor: "):
                self.assertLessEqual(len(line), len("Tutor: ") + rag_service.HISTORY_TUTOR_CHARS + 2)
                self.assertTrue(line.endswith("…"))
        # The prompt does not grow with the conversation.
        _, _, short, _ = ask("Why?", history[-6:])
        _, _, long, _ = ask("Why?", history)
        self.assertEqual(len(short), len(long))

    def test_short_history_is_kept_verbatim(self):
        history = turn("What is a TLB?", "A cache of page table entries (source: os.pdf, page 14).")
        self.assertEqual(rag_service._history_for_prompt(history),
                         "Student: What is a TLB?\nTutor: A cache of page table entries.")
        self.assertEqual(rag_service._history_for_prompt([]), "No previous messages.")


class ExplainMoreFollowUpTests(unittest.TestCase):
    def test_explain_more_is_remembered_as_its_question_only(self):
        history = turn(EXPLAIN_MORE)
        _, _, prompt, _ = ask("Why is the scheduler wrong?", history)
        past = history_of(prompt)
        self.assertIn("Student: Explain more about the quiz question: Which component translates virtual addresses "
                      "to physical addresses?", past)
        for leftover in ("Options:", "My answer", "Please explain clearly", "1. Why the correct answer"):
            self.assertNotIn(leftover, past)

    def test_follow_up_retrieval_uses_the_compact_previous_question(self):
        history = turn(EXPLAIN_MORE)
        _, _, _, query = ask("Why is that faster?", history)
        self.assertEqual(query, "Explain more about the quiz question: Which component translates virtual addresses "
                                "to physical addresses?\nWhy is that faster?")
        # A self-contained question after Explain More is searched on its own.
        own = "What are the main differences between paging and segmentation in memory management systems?"
        self.assertEqual(ask(own, history)[3], own)
        # The Explain More request itself is still searched in full (its options and answer matter).
        self.assertEqual(ask(EXPLAIN_MORE)[3], EXPLAIN_MORE.strip())

    def test_before_this_change_explain_more_flooded_the_next_query(self):
        old_query = " ".join(m["content"] for m in turn(EXPLAIN_MORE)[-4:] if m["role"] == "user") + "\nWhy?"
        new_query = ask("Why?", turn(EXPLAIN_MORE))[3]
        self.assertLess(len(new_query), len(old_query) / 3)


class CompatibilityTests(unittest.TestCase):
    def tearDown(self):
        FakeModel.reply, FakeModel.metadata = "The MMU translates addresses (source: os.pdf, page 12).", {"done_reason": "stop"}

    def test_streaming_and_json_build_the_same_prompt_options_and_answer(self):
        history = turn(EXPLAIN_MORE) + turn("How does the TLB help?")
        plain, plain_options, plain_prompt, plain_query = ask("Why?", history)
        streamed, stream_options, stream_prompt, stream_query = ask("Why?", history, streaming=True)
        self.assertEqual((plain_options, plain_prompt, plain_query), (stream_options, stream_prompt, stream_query))
        self.assertEqual(plain["answer"], streamed["answer"])
        self.assertEqual(streamed["deltas"], FakeModel.reply)
        self.assertEqual((plain["grounding_status"], len(plain["citations"])), ("supported", 1))

    def test_a_capped_answer_is_saved_ending_on_a_full_sentence(self):
        FakeModel.reply = "Paging splits memory into pages. The MMU maps each page to a frame. When the cap hits mid"
        FakeModel.metadata = {"done_reason": "length"}
        expected = "Paging splits memory into pages. The MMU maps each page to a frame."
        plain = ask("Explain paging in detail")[0]
        streamed = ask("Explain paging in detail", streaming=True)[0]
        self.assertEqual(plain["answer"], expected)
        self.assertEqual(streamed["answer"], expected)                 # what is saved and rendered last
        self.assertEqual(streamed["assistant_message"]["content"], expected)

    def test_an_uncapped_answer_is_never_trimmed(self):
        FakeModel.reply = "Paging maps pages to frames, and the MMU does it"
        self.assertEqual(ask("What is paging?")[0]["answer"], FakeModel.reply)

    def test_vllm_receives_the_cap_as_max_tokens(self):
        with patch.object(llm_backend, "LLM_BACKEND", "vllm"):
            model = llm_backend.ChatOllama(model=llm_backend.CHAT_MODEL, temperature=0, num_ctx=16384,
                                           num_predict=CHAT_MAX_TOKENS, keep_alive="30m")
        self.assertEqual(model.options, {"temperature": 0, "max_tokens": CHAT_MAX_TOKENS})


if __name__ == "__main__":
    unittest.main()
