"""PLANNER-STUDY-QUEUE: what the Study Queue lists right after Accept Plan.

The queue (GET /candidates) must list only what the accepted schedule really left out. A deadline's
final review that a quiz inside its window already covers is not "missing": the scheduler drops it
on purpose (final_satisfied_by_retrieval), so the queue must not ask for a time slot for it --
including when an empty final window was widened to an earlier usable day. Whatever is listed says
why it has no session: held back by the plan's pacing although free time is left (drag it in), or
no free slot before its deadline at all.

Clock: Mon 2026-09-28 08:00, UTC offset 0.
"""

import unittest
from datetime import datetime

from fastapi.testclient import TestClient
from planner_fixtures import PlannerDatabaseMixin

from backend import study_placement
from backend.main import app

NOW = "2026-09-28T08:00:00"
BODY = {"utc_offset_minutes": 0, "local_now": NOW}


class StudyQueueAfterAcceptTests(PlannerDatabaseMixin, unittest.TestCase):
    def setUp(self):
        self.start_planner_database()
        self.alice = self.user("Alice")
        self.add_document(self.alice, "mkt", "Marketing")
        self.add_document(self.alice, "stats", "Statistics")
        self.client = TestClient(app)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "email": "alice-planner@example.com", "password": "long-password-x"}).status_code, 200)
        self.plan = self.client.post("/api/planner/plans", json={"title": "Exams"}).json()
        self.base = f"/api/planner/plans/{self.plan['plan_id']}"

    def setup_plan(self, deadlines: dict, evenings: dict):
        for document_id, deadline in deadlines.items():
            self.client.post(f"{self.base}/materials", json={"document_id": document_id, **({"deadline": deadline} if deadline else {})})
        for day, (start, end) in evenings.items():
            self.client.post("/api/planner/availability", json={"start_at": start, "end_at": end, "is_recurring": True, "day_of_week": day})
        confirm = self.client.post(f"{self.base}/confirm", json=BODY)
        self.assertEqual(confirm.status_code, 201, confirm.text)
        queue = self.client.get(f"{self.base}/candidates", params={"utc_offset_minutes": 0, "local_now": NOW})
        self.assertEqual(queue.status_code, 200, queue.text)
        return confirm.json(), queue.json()

    @staticmethod
    def pairs(items):
        return sorted((item["document_id"], item["activity_type"]) for item in items)

    def test_final_review_covered_by_a_quiz_in_its_window_is_not_queued(self):
        confirmed, queue = self.setup_plan({"mkt": "2026-09-30", "stats": None},
                                           {day: ("18:00", "22:00") for day in range(4)})
        self.assertEqual(confirmed["capacity"]["unscheduled"], [])   # the accepted plan misses nothing
        mkt = [(s["activity_type"], s["scheduled_start"][:10]) for s in confirmed["sessions"] if s["document_id"] == "mkt"]
        self.assertEqual(mkt[-1][0], "quiz")                            # its last session is the quiz in the window
        self.assertEqual(queue, [])                                     # ...so nothing "needs a time slot"

    def test_widened_final_window_is_followed_too(self):
        # Only Monday evenings: the final window (Oct 6-7) has no usable day, so it starts on Mon Oct 5.
        confirmed, queue = self.setup_plan({"mkt": "2026-10-08"}, {0: ("18:00", "22:00")})
        self.assertEqual(confirmed["capacity"]["unscheduled"], [])
        self.assertEqual(queue, [])

    def test_queue_matches_what_the_accepted_plan_left_out_and_says_why(self):
        confirmed, queue = self.setup_plan({"mkt": "2026-10-08", "stats": "2026-10-02"}, {0: ("18:00", "22:00")})
        self.assertEqual(self.pairs(queue), self.pairs(confirmed["capacity"]["unscheduled"]))
        self.assertTrue(queue)
        for item in queue:
            self.assertIn(item["queue_reason"]["code"], ("held_by_pacing", "no_room_before_deadline", "no_room"))
            self.assertTrue(item["queue_reason"]["message"])
        # Statistics is due Fri Oct 2 and only Monday evenings are available: Monday's free time is
        # left over (the daily study target is full), so the reason is pacing, and a drag is allowed.
        stats = [item for item in queue if item["document_id"] == "stats"]
        self.assertEqual({item["queue_reason"]["code"] for item in stats}, {"held_by_pacing"})


class QueueReasonRuleTests(unittest.TestCase):
    """queue_reason checks the same rules as a drag onto the calendar."""
    NOW = datetime(2026, 9, 28, 8, 0)
    MONDAY_EVENING = [{"start_at": "18:00", "end_at": "19:00", "is_recurring": True, "day_of_week": 0}]

    def reason(self, duration, deadline, busy=(), availability=None):
        return study_placement.queue_reason(duration, deadline, now=self.NOW, availability=availability or self.MONDAY_EVENING,
                                            busy=list(busy), horizon_days=14)["code"]

    def test_free_window_means_pacing_held_it_back(self):
        self.assertEqual(self.reason(45, "2026-10-01"), "held_by_pacing")

    def test_taken_or_too_short_windows_mean_no_room_before_the_deadline(self):
        busy = [{"scheduled_start": "2026-09-28T18:00:00", "scheduled_end": "2026-09-28T18:30:00"}]
        self.assertEqual(self.reason(45, "2026-10-01", busy), "no_room_before_deadline")   # 30 free minutes left
        self.assertEqual(self.reason(75, "2026-10-01"), "no_room_before_deadline")           # longer than the evening
        self.assertEqual(self.reason(45, "2026-10-04", busy), "no_room_before_deadline")    # next Monday is after it

    def test_google_busy_time_counts_as_taken_and_flexible_material_uses_the_horizon(self):
        google = [*self.MONDAY_EVENING, {"busy": True, "date": "2026-09-28", "start_at": "18:00", "end_at": "19:00"},
                  {"busy": True, "date": "2026-10-05", "start_at": "18:00", "end_at": "19:00"},
                  {"busy": True, "date": "2026-10-12", "start_at": "18:00", "end_at": "19:00"}]
        self.assertEqual(self.reason(30, None, availability=google), "no_room")
        self.assertEqual(self.reason(30, None), "held_by_pacing")


if __name__ == "__main__":
    unittest.main()
