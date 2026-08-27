import unittest

from backend.assessment_planner import build_structural_seeds, resolve_concept_evidence


def location(offset):
    return {"page": 1, "page_index": 0, "char_offset": offset, "document_offset": offset}


def boundary(start, end):
    return {"start": location(start), "end": location(end), "interval": "half-open"}


def chunk(chunk_id, start, content, topic_id="topic_a", subtopic_id=""):
    return {"content": content, "metadata": {
        "chunk_id": chunk_id, "page": 0, "start_index": start,
        "topic_id": topic_id, "subtopic_id": subtopic_id,
    }}


class TopicV2Phase3Tests(unittest.TestCase):
    def test_short_subtopic_and_larger_neighbor_share_canonical_chunk(self):
        topic = {"topic_id": "topic_a", "name": "Topic A", "boundary": boundary(0, 200), "subtopics": [
            {"subtopic_id": "short", "name": "Short", "boundary": boundary(0, 40)},
            {"subtopic_id": "large", "name": "Large", "boundary": boundary(40, 200)},
        ]}
        chunks = [chunk("shared", 0, "S" * 40 + "L" * 160, subtopic_id="large")]
        seeds = build_structural_seeds(topic, chunks)
        self.assertEqual([seed["subtopic_id"] for seed in seeds], ["short", "large"])
        self.assertEqual([seed["source_chunk_ids"] for seed in seeds], [["shared"], ["shared"]])
        self.assertEqual(seeds[0]["chunks"][0]["content"], "S" * 40)
        self.assertEqual(seeds[1]["chunks"][0]["content"], "L" * 160)

    def test_cross_topic_primary_chunk_supplies_clipped_topic_a_evidence(self):
        topic = {"topic_id": "topic_a", "name": "Topic A", "boundary": boundary(0, 80), "subtopics": [
            {"subtopic_id": "tail", "name": "Tail", "boundary": boundary(20, 80)},
        ]}
        chunks = [chunk("cross", 20, "A" * 60 + "B" * 40, topic_id="topic_b")]
        seed = build_structural_seeds(topic, chunks)[0]
        self.assertEqual(seed["source_chunk_ids"], ["cross"])
        self.assertEqual(seed["chunks"][0]["content"], "A" * 60)
        self.assertNotIn("B", seed["chunks"][0]["content"])

    def test_heading_only_trivial_overlap_is_rejected(self):
        topic = {"topic_id": "topic_a", "name": "Topic A", "boundary": boundary(0, 100), "subtopics": [
            {"subtopic_id": "tiny", "name": "Tiny", "boundary": boundary(0, 10)},
        ]}
        seeds = build_structural_seeds(topic, [chunk("one", 0, "Tiny" + "x" * 95)])
        self.assertNotIn("tiny", {seed["subtopic_id"] for seed in seeds})

    def test_meaningful_ratio_overlap_is_accepted_below_absolute_threshold(self):
        topic = {"topic_id": "topic_a", "name": "Topic A", "boundary": boundary(0, 200), "subtopics": [
            {"subtopic_id": "short", "name": "Short", "boundary": boundary(20, 60)},
        ]}
        seeds = build_structural_seeds(topic, [chunk("one", 0, "x" * 200)])
        self.assertEqual(seeds[0]["source_chunk_ids"], ["one"])
        self.assertEqual(len(seeds[0]["chunks"][0]["content"]), 40)

    def test_resolved_evidence_is_clipped_and_not_duplicated(self):
        topic = {"topic_id": "topic_a", "name": "Topic A", "boundary": boundary(0, 160), "subtopics": [
            {"subtopic_id": "left", "name": "Left", "boundary": boundary(0, 80)},
            {"subtopic_id": "right", "name": "Right", "boundary": boundary(80, 160)},
        ]}
        chunks = [chunk("shared", 0, "L" * 80 + "R" * 80, subtopic_id="left")]
        concept = {"source_subtopic_ids": ["right"], "source_chunk_ids": ["shared"]}
        evidence = resolve_concept_evidence(topic, chunks, concept)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["metadata"]["chunk_id"], "shared")
        self.assertEqual(evidence[0]["content"], "R" * 80)


if __name__ == "__main__":
    unittest.main()
