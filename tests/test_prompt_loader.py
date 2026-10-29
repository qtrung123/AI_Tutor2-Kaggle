"""File-based LLM prompts (prompts/<feature>/*.txt): the shared loader only reads files inside
prompts/, independent of the working directory, every prompt file renders with the fields its
service passes, and the builders still produce the exact text they did when it lived in Python."""

import os
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend import flashcard_service, quiz_legacy_v2, quiz_units, quiz_validation, rag_service, summary_service
from backend.prompt_loader import load_prompt, render_prompt
from config import BASE_DIR, PROMPTS_DIR

# The two prompts that were already files keep their trailing newline; every prompt extracted from
# Python code ends exactly where its string literal ended.
TRAILING_NEWLINE = {"chat/rag_answer.txt", "quiz/batch_generation.txt"}
# Filled with str.replace (its JSON example uses single braces), not str.format.
REPLACE_TEMPLATES = {"quiz/batch_generation.txt"}


def prompt_files() -> list[str]:
    return sorted(path.relative_to(PROMPTS_DIR).as_posix() for path in Path(PROMPTS_DIR).rglob("*.txt"))


def fields(name: str) -> set[str]:
    return {field for _text, field, _spec, _conv in string.Formatter().parse(load_prompt(name)) if field}


class PromptLoaderTest(unittest.TestCase):
    def test_prompts_live_in_feature_folders(self):
        names = prompt_files()
        self.assertIn("chat/rag_answer.txt", names)
        self.assertTrue(all("/" in name for name in names), names)
        for feature in ("chat", "flashcards", "quiz", "summary", "topics"):
            self.assertTrue(any(name.startswith(f"{feature}/") for name in names), feature)

    def test_loads_exact_text(self):
        expected = (Path(PROMPTS_DIR) / "summary" / "document_synthesis.txt").read_text(encoding="utf-8")
        self.assertEqual(expected, load_prompt("summary/document_synthesis.txt"))
        self.assertTrue(load_prompt("chat/rag_answer.txt").endswith("Answer:\n"))

    def test_rejects_paths_outside_prompts(self):
        for name in ("../config.py", "../README.md", "chat/../../config.py", str(BASE_DIR / "config.py"),
                     "chat/rag_answer", "chat/rag_answer.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_prompt(name)
        with self.assertRaises(FileNotFoundError):
            load_prompt("chat/missing_prompt.txt")

    def test_render_fills_fields_and_keeps_literal_braces(self):
        rendered = render_prompt("summary/document_synthesis.txt", document_id="doc.pdf", topic_summaries="[{}]")
        self.assertIn('Return JSON only: {"overview":"one concise paragraph"', rendered)
        self.assertTrue(rendered.endswith("DOCUMENT: doc.pdf\nORDERED TOPIC SUMMARIES:\n[{}]"))
        with self.assertRaises(KeyError):
            render_prompt("summary/document_synthesis.txt", document_id="doc.pdf")

    def test_every_format_template_parses_and_ends_as_extracted(self):
        for name in prompt_files():
            with self.subTest(name=name):
                text = load_prompt(name)
                self.assertNotIn("\r", text)
                self.assertEqual(name in TRAILING_NEWLINE, text.endswith("\n"))
                if name not in REPLACE_TEMPLATES:
                    values = {field: "<v>" for field in fields(name)}
                    self.assertNotIn("{<v>}", render_prompt(name, **values))

    def test_loads_from_any_working_directory(self):
        code = ("from backend.prompt_loader import load_prompt; "
                "print(len(load_prompt('chat/rag_answer.txt')))")
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True,
                                    env={**os.environ, "PYTHONPATH": str(BASE_DIR)}, check=True)
        self.assertEqual(str(len(load_prompt("chat/rag_answer.txt"))), result.stdout.strip())


class PromptRenderingTest(unittest.TestCase):
    """Representative renders, compared with the text the builders produced before extraction."""

    def test_quiz_explanation_prompt(self):
        prompt = render_prompt("quiz/answer_explanation.txt", question="Q?", options="A. a\nB. b",
                               correct_option="A. a", context="CTX").strip()
        self.assertEqual(
            "Use only the course material context below.\n\nQuestion: Q?\nOptions:\nA. a\nB. b\n"
            "Correct answer: A. a\n\nExplain in 2 to 3 concise sentences why the correct answer is correct.\n"
            "Only explain the correct answer. Do not discuss any learner selection or wrong option.\n"
            "Do not mention retrieval, chunks, or these instructions. Do not use outside knowledge.\n\n"
            "COURSE MATERIAL CONTEXT:\nCTX",
            prompt,
        )

    def test_rag_prompt_fields(self):
        self.assertEqual({"conversation_history", "context", "question"}, fields("chat/rag_answer.txt"))
        prompt = rag_service.load_prompt_template().format(context="C", conversation_history="H", question="Q")
        self.assertTrue(prompt.endswith("Recent conversation (for references only, not evidence):\nH\n\n"
                                        "Context:\nC\n\nQuestion:\nQ\n\nAnswer:\n"))

    def test_summary_topic_prompt_switches_derived_sections(self):
        topic = {"topic_id": "t1", "name": "Locks", "subtopics": []}
        chunks = [{"content": "Mutex", "metadata": {"chunk_id": "c1", "page": 1}}]
        derived = summary_service._topic_prompt("doc.pdf", [(topic, chunks)])
        self.assertIn("empty subsections list.\nA topic block marked SUBSECTIONS: DERIVE_FROM_EVIDENCE", derived)
        self.assertIn("too short to divide.\n\nNever create placeholder", derived)
        self.assertIn('{"heading":"short evidence-based heading","content_type"', derived)
        required = summary_service._topic_prompt("doc.pdf", [(topic, chunks)], False)
        self.assertIn("empty subsections list.\nNever create placeholder", required)
        self.assertIn('{"subtopic_id":"exact id","content_type"', required)
        self.assertTrue(required.endswith("DOCUMENT: doc.pdf\n\nTOPIC_ID: t1\nTOPIC_NAME: Locks\n"
                                          "REQUIRED_SUBTOPICS_IN_ORDER: []\nEVIDENCE:\n"
                                          "[chunk_id=c1 page=1 subtopic_id=TOPIC_LEVEL]\nMutex\nEND_TOPIC"))

    def test_flashcard_prompt_languages(self):
        groups = [({"topic_id": "t1", "name": "Locks"}, [{"content": "Mutex", "metadata": {"chunk_id": "c1"}}])]
        for language in ("auto", "english", "vietnamese"):
            prompt = flashcard_service._prompt("doc.pdf", groups, language)
            self.assertIn("just restate or translate the front.\nSome evidence may repeat", prompt)
            self.assertIn(f"\n{load_prompt(f'flashcards/language_{language}.txt')}\nPreserve precise", prompt)
            self.assertIn(f"Generate at most {flashcard_service.FLASHCARD_MAX_CARDS_PER_TOPIC} cards per topic block",
                          prompt)
        with self.assertRaises(KeyError):
            flashcard_service._prompt("doc.pdf", groups, "klingon")

    def test_single_choice_prompt_follow_up_block(self):
        units = [{"unit_id": "U1", "evidence_excerpt": "E"}]
        first = quiz_units.build_generation_prompt("L", "easy", units, 5)
        self.assertIn('(e.g. for net.Listen("tcp", ":8080"): the function', first)
        self.assertIn("Difficulty EASY: test one explicitly stated fact, definition, purpose or step.\nEXCERPTS:\n[U1]\nE",
                      first)
        follow_up = quiz_units.build_generation_prompt("L", "easy", units, 5, ["Q?"], ["quote"])
        self.assertIn('Questions already written:\n- Q?  [source: "quote"]\nEXCERPTS:', follow_up)
        self.assertIn('Return JSON only: {"questions":[{"evidence_quote":"..."', follow_up)

    def test_slot_prompts_keep_spacing_between_fragments(self):
        group = {"slot_id": "S1", "evidence_excerpt": "E"}
        prompt = quiz_legacy_v2._build_v2_prompt("doc.pdf", {"topic_id": "t1"}, "medium", [group], 1, [], False)
        self.assertIn("at least one incorrect option. Choose each question_type", prompt)
        self.assertIn("before fallback. Question <=18 words", prompt)
        self.assertIn("do not ask direct recall alone.\nEVIDENCE:\nS1|E|cognitive_intent=relationship_application",
                      prompt)
        repair = quiz_legacy_v2._build_document_single_choice_prompt(
            "doc.pdf", {"name": "Locks"}, "easy", [group], 1, ["Old?"], True, {"S1": ["dup"]}, {"S1": 2})
        self.assertIn("grammar.\nTARGETED MISSING-SLOT FILL:", repair)
        self.assertIn("S1 previous rejection: dup\nACCEPTED STEMS / FORBIDDEN QUESTIONS (all accepted stems; "
                      "do not repeat or closely paraphrase):\n- Old?\nEVIDENCE:", repair)

    def test_legacy_batch_retry_escapes(self):
        prompt = quiz_legacy_v2._build_retry_prompt("doc.pdf", 2, "easy", [], ValueError("bad"))
        self.assertIn('- escape double quotes inside JSON strings as \\"\n', prompt)
        self.assertIn("- encode line breaks inside strings as \\n and close", prompt)
        self.assertIn('{\n  "questions": [\n    {\n      "question": "Specific concept question"', prompt)

    def test_semantic_validation_prompt(self):
        question = {"question": "Q?", "options": ["A", "B"], "correct_answer": "A", "explanation": "E"}
        prompt = quiz_validation._prompt(question, [{"content": "C", "metadata": {"chunk_id": "c1"}}], "easy", "T")
        self.assertIn("Assessment concept: legacy\nStructural lineage: []\nConcept origin: legacy\n", prompt)
        self.assertIn('Cited context: [{"chunk_id": "c1", "content": "C"}]', prompt)
        self.assertTrue(prompt.endswith("Judge semantic support from the cited context."))


if __name__ == "__main__":
    unittest.main()
