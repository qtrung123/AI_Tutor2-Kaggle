import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import auth_store, flashcard_service, flashcard_store, indexed_document_store


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLlm:
    responses: list = []
    calls: list = []

    def __init__(self, **_kwargs):
        pass

    def invoke(self, prompt):
        self.__class__.calls.append(prompt)
        return FakeResponse(self.__class__.responses.pop(0))


def _cards_response(cards, topic_id="alpha"):
    return json.dumps({"topics": [{"topic_id": topic_id, "cards": cards}]})


def _multi_topic_response(topic_cards: list[tuple[str, list[dict]]]) -> str:
    return json.dumps({
        "topics": [{"topic_id": topic_id, "cards": cards} for topic_id, cards in topic_cards]
    })


EN_QA = {
    "front": "When is eCos bitmap scheduling most efficient?",
    "back": "When only a small number of threads are active.",
    "source_chunk_ids": ["a1"],
}
VI_QA = {
    "front": "Bộ lập lịch bitmap của eCos hiệu quả nhất khi nào?",
    "back": "Khi chỉ có một số lượng nhỏ luồng đang hoạt động.",
    "source_chunk_ids": ["a1"],
}
VI_QA_2 = {
    "front": "Khi nào bộ nhớ đệm được sử dụng lại?",
    "back": "Khi dữ liệu được truy cập lặp đi lặp lại nhiều lần.",
    "source_chunk_ids": ["a1"],
}
# The reported bug: an English source sentence copied to Front, its Vietnamese translation
# copied to Back -- a translation, not a real question/answer relationship.
BAD_TRANSLATION = {
    "front": "eCos scheduler supports bitmap scheduling for small numbers of active threads.",
    "back": "Bộ lập lịch bitmap: Hiệu quả đối với một số lượng nhỏ luồng đang hoạt động.",
    "source_chunk_ids": ["a1"],
}
# The reported corruption bug: identical content with all inter-word whitespace lost.
CORRUPTED_BACK = "Bộlậplịchbitmap:Hiệuquảđốivớimộtsốlượngnhỏluồngđanghoạtđộng."


class FlashcardBilingualQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cards.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(flashcard_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
        ]
        for item in self.patches:
            item.start()
        self.owner = auth_store.create_user("Alice", "alice-lang@example.com", "long-password-a")["id"]
        topics = [{"topic_id": "alpha", "name": "Scheduling", "subtopics": []}]
        indexed_document_store.upsert_indexed_document(self.owner, "notes.pdf", {
            "hash": "hash-1", "chunks": 1, "path": "notes.pdf", "topic_schema_version": 2, "topics": topics,
        })

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(_document_id, _topic_id, owner_id):
        return [{"content": "Evidence content.", "metadata": {
            "owner_id": owner_id, "document_id": "notes.pdf", "topic_id": "alpha",
            "chunk_id": "a1", "chunk": 0,
        }}]

    def generate(self, responses, language=None):
        FakeLlm.responses = list(responses)
        FakeLlm.calls = []
        with patch.object(flashcard_service, "ChatOllama", FakeLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(flashcard_service, "resolve_generation_model", return_value="runtime-model"):
            return flashcard_service.generate_flashcards(
                self.owner, "notes.pdf", ["alpha"], "model-a", language=language,
            )

    def test_bilingual_source_does_not_create_english_front_vietnamese_back(self):
        result = self.generate([_cards_response([BAD_TRANSLATION, EN_QA])])
        pairs = [(card["front"], card["back"]) for card in result["cards"]]
        self.assertNotIn((BAD_TRANSLATION["front"], BAD_TRANSLATION["back"]), pairs)
        self.assertIn((EN_QA["front"], EN_QA["back"]), pairs)

    def test_english_mode_produces_english_front_and_back(self):
        result = self.generate([_cards_response([EN_QA, VI_QA])], language="english")
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])
        self.assertEqual(result["flashcard_language"], "english")

    def test_vietnamese_mode_produces_vietnamese_front_and_back(self):
        result = self.generate([_cards_response([EN_QA, VI_QA])], language="vietnamese")
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], VI_QA["front"])
        self.assertEqual(result["flashcard_language"], "vietnamese")

    def test_auto_mode_selects_one_consistent_language_for_the_set(self):
        result = self.generate([_cards_response([VI_QA, VI_QA_2, EN_QA])])
        fronts = {card["front"] for card in result["cards"]}
        self.assertEqual(fronts, {VI_QA["front"], VI_QA_2["front"]})
        self.assertNotIn(EN_QA["front"], fronts)
        self.assertEqual(result["flashcard_language"], "auto")

    def test_translation_pair_is_rejected_not_persisted_as_a_qa_pair(self):
        reversed_pair = {"front": VI_QA["back"], "back": EN_QA["back"], "source_chunk_ids": ["a1"]}
        result = self.generate([_cards_response([BAD_TRANSLATION, reversed_pair, EN_QA])])
        fronts = {card["front"] for card in result["cards"]}
        self.assertNotIn(BAD_TRANSLATION["front"], fronts)
        self.assertNotIn(reversed_pair["front"], fronts)
        self.assertIn(EN_QA["front"], fronts)

    def test_whitespace_between_vietnamese_words_is_preserved(self):
        spaced_back = "Khi  chỉ có\nmột số lượng nhỏ luồng đang hoạt động."
        card = {"front": VI_QA["front"], "back": spaced_back, "source_chunk_ids": ["a1"]}
        result = self.generate([_cards_response([card])], language="vietnamese")
        self.assertEqual(len(result["cards"]), 1)
        back = result["cards"][0]["back"]
        self.assertEqual(back, "Khi chỉ có một số lượng nhỏ luồng đang hoạt động.")
        self.assertEqual(len(back.split()), len(spaced_back.split()))

    def test_corrupted_no_whitespace_card_is_rejected_and_set_recovers_via_retry(self):
        corrupted = {
            "front": "What does the bitmap scheduler optimize for?",
            "back": CORRUPTED_BACK, "source_chunk_ids": ["a1"],
        }
        result = self.generate([_cards_response([corrupted]), _cards_response([EN_QA])])
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])

    def test_corrupted_card_mixed_with_valid_card_only_valid_one_kept(self):
        corrupted = {
            "front": "What does the bitmap scheduler optimize for?",
            "back": CORRUPTED_BACK, "source_chunk_ids": ["a1"],
        }
        result = self.generate([_cards_response([corrupted, EN_QA])])
        self.assertEqual(len(result["cards"]), 1)
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])

    def test_old_v1_cache_is_not_reused(self):
        legacy_identity = {
            "owner_id": self.owner, "document_id": "notes.pdf", "document_hash": "hash-1",
            "topic_schema_version": 2, "flashcard_version": "grounded_flashcards_v1",
            "model_id": "model-a", "runtime_model": "runtime-model", "topic_ids": ["alpha"],
            "flashcard_language": "auto",
        }
        flashcard_store.save_flashcards(legacy_identity, [{
            "topic_id": "alpha", "topic_name": "Scheduling", "subtopic_id": None, "subtopic_name": None,
            "front": "Legacy front", "back": CORRUPTED_BACK, "source_chunk_ids": ["a1"],
        }])
        self.assertNotEqual(flashcard_service.FLASHCARD_VERSION, "grounded_flashcards_v1")
        result = self.generate([_cards_response([EN_QA])])
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["cards"][0]["front"], EN_QA["front"])

    def test_cache_distinguishes_requested_language(self):
        english_result = self.generate([_cards_response([EN_QA])], language="english")
        self.assertFalse(english_result["cache_hit"])
        vietnamese_result = self.generate([_cards_response([VI_QA])], language="vietnamese")
        self.assertFalse(vietnamese_result["cache_hit"])
        self.assertNotEqual(english_result["set_id"], vietnamese_result["set_id"])
        repeat_english = self.generate([], language="english")
        self.assertTrue(repeat_english["cache_hit"])
        self.assertEqual(repeat_english["cards"][0]["front"], EN_QA["front"])


class FlashcardTopicCoverageTests(unittest.TestCase):
    """Every selected topic must have at least one valid card before a set may persist -- a
    generated 13-card set collapsing to 1-3 cards (or losing a whole topic) after language/
    quality filtering must never be saved as-is."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cards.db"
        self.patches = [
            patch.object(auth_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "DATABASE_PATH", self.db),
            patch.object(flashcard_store, "DATABASE_PATH", self.db),
            patch.object(indexed_document_store, "INDEXED_FILES_PATH", Path(self.temp.name) / "missing.json"),
        ]
        for item in self.patches:
            item.start()
        self.owner = auth_store.create_user("Carol", "carol-coverage@example.com", "long-password-c")["id"]
        topics = [
            {"topic_id": "alpha", "name": "Alpha", "subtopics": []},
            {"topic_id": "beta", "name": "Beta", "subtopics": []},
        ]
        indexed_document_store.upsert_indexed_document(self.owner, "notes.pdf", {
            "hash": "hash-1", "chunks": 2, "path": "notes.pdf", "topic_schema_version": 2, "topics": topics,
        })

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def chunks(_document_id, topic_id, owner_id):
        chunk_id = "a1" if topic_id == "alpha" else "b1"
        return [{"content": f"{topic_id} evidence.", "metadata": {
            "owner_id": owner_id, "document_id": "notes.pdf", "topic_id": topic_id,
            "chunk_id": chunk_id, "chunk": 0,
        }}]

    def generate(self, responses, language=None):
        FakeLlm.responses = list(responses)
        FakeLlm.calls = []
        with patch.object(flashcard_service, "ChatOllama", FakeLlm), \
             patch.object(flashcard_service, "get_topic_chunks", side_effect=self.chunks), \
             patch.object(flashcard_service, "resolve_generation_model", return_value="runtime-model"):
            return flashcard_service.generate_flashcards(
                self.owner, "notes.pdf", ["alpha", "beta"], "model-a", language=language,
            )

    def _alpha_card(self):
        return {"front": "What is Alpha?", "back": "Alpha is the first concept.", "source_chunk_ids": ["a1"]}

    def _beta_card(self):
        return {"front": "What is Beta?", "back": "Beta is the second concept.", "source_chunk_ids": ["b1"]}

    def _incomplete_response(self):
        # Beta's only candidate cites alpha's chunk_id, so it is discarded for bad provenance --
        # beta ends up with zero surviving cards while alpha has one.
        return _multi_topic_response([
            ("alpha", [self._alpha_card()]),
            ("beta", [{"front": "What is Beta?", "back": "Beta is the second concept.", "source_chunk_ids": ["a1"]}]),
        ])

    def test_one_selected_topic_filtered_to_zero_cards_is_not_persisted(self):
        with self.assertRaises(ValueError):
            self.generate([self._incomplete_response(), self._incomplete_response()])
        self.assertEqual(flashcard_store.list_flashcards(self.owner, "notes.pdf"), [])

    def test_retry_restores_full_topic_coverage_and_persists(self):
        complete_response = _multi_topic_response([
            ("alpha", [self._alpha_card()]), ("beta", [self._beta_card()]),
        ])
        result = self.generate([self._incomplete_response(), complete_response])
        self.assertEqual(result["llm_calls"], 2)
        self.assertFalse(result["cache_hit"])
        self.assertEqual({card["topic_id"] for card in result["cards"]}, {"alpha", "beta"})
        persisted = flashcard_store.list_flashcards(self.owner, "notes.pdf", result["set_id"])
        self.assertEqual({card["topic_id"] for card in persisted}, {"alpha", "beta"})

    def test_retry_still_missing_a_topic_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "beta"):
            self.generate([self._incomplete_response(), self._incomplete_response()])
        self.assertEqual(len(FakeLlm.calls), 2)
        self.assertEqual(flashcard_store.list_flashcards(self.owner, "notes.pdf"), [])


class FlashcardLayoutCssTests(unittest.TestCase):
    def test_flashcard_copy_and_card_have_defensive_wrap_css(self):
        css = Path("frontend/styles.css").read_text(encoding="utf-8")
        copy_start = css.index(".flashcard-copy{")
        copy_rule = css[copy_start:copy_start + 400]
        self.assertIn("overflow-wrap:anywhere", copy_rule)
        self.assertIn("word-break:break-word", copy_rule)
        self.assertIn("min-width:0", copy_rule)
        card_start = css.index(".flashcard{")
        card_rule = css[card_start:card_start + 400]
        self.assertIn("max-width:100%", card_rule)
        self.assertIn("min-width:0", card_rule)


class FlashcardFrontendWiringTests(unittest.TestCase):
    def test_shuffle_manage_cards_and_language_select_remain_wired(self):
        script = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('document.getElementById("shuffle-flashcards")?.addEventListener("click"', script)
        self.assertIn(
            'document.getElementById("manage-flashcards")?.addEventListener("click", openFlashcardManager)',
            script,
        )
        self.assertIn("flashcard-language-select", script)
        self.assertIn('flashcardLanguageSelect?.addEventListener("change"', script)


if __name__ == "__main__":
    unittest.main()
