"""Effective availability = the learner's marked availability minus Google Calendar busy time.

CASES is shared with tests/test_planner_effective_availability_ui.py: the scheduler's free time
(study_time.free_minutes_by_date over google_calendar_sync's busy rows) and the Planner's rendered
"Available" blocks must be exactly the same pieces. No real Google call is made.

Every case marks Tue 2026-09-22 08:30-11:30 (the reported example) and lists busy intervals on
that day, learner-local.
"""

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from backend import google_calendar_sync
from backend.study_time import free_minutes_by_date

DAY = "2026-09-22"
AVAILABLE = ("08:30", "11:30")
CASES = {
    "busy_inside": ([("09:30", "11:00")], [("08:30", "09:30"), ("11:00", "11:30")]),
    "busy_at_start": ([("08:00", "09:00")], [("09:00", "11:30")]),
    "busy_at_end": ([("11:00", "12:00")], [("08:30", "11:00")]),
    "multiple_busy": ([("09:00", "09:30"), ("10:00", "10:30")], [("08:30", "09:00"), ("09:30", "10:00"), ("10:30", "11:30")]),
    "overlapping_busy": ([("09:30", "10:30"), ("09:00", "10:00")], [("08:30", "09:00"), ("10:30", "11:30")]),
    "fully_consumed": ([("08:00", "12:00")], []),
}


def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _label(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _availability():
    return [{"is_recurring": False, "date": DAY, "start_at": AVAILABLE[0], "end_at": AVAILABLE[1]}]


def _intervals(busy):
    return [(f"{DAY}T{start}:00", f"{DAY}T{end}:00") for start, end in busy]


def _free(availability):
    day = date.fromisoformat(DAY)
    return [(_label(start), _label(end)) for start, end in free_minutes_by_date(day, day, availability, [])[day]]


class SchedulerEffectiveAvailabilityTests(unittest.TestCase):
    def test_busy_time_splits_availability(self):
        for name, (busy, expected) in CASES.items():
            with self.subTest(name):
                rows = google_calendar_sync.busy_availability_rows(_intervals(busy))
                self.assertEqual(_free(_availability() + rows), expected)

    def test_planner_availability_subtracts_busy_only_when_avoiding_conflicts(self):
        busy, expected = CASES["busy_inside"]
        google_busy = [(f"{DAY}T{start}:00Z", f"{DAY}T{end}:00Z") for start, end in busy]   # UTC == local here
        now = datetime(2026, 9, 21, 8, 0)
        for avoid, want in ((True, expected), (False, [AVAILABLE])):
            with self.subTest(avoid_conflicts=avoid), \
                    patch.object(google_calendar_sync.google_calendar_store, "get_connection",
                                 return_value={"status": "connected", "avoid_conflicts": avoid}), \
                    patch.object(google_calendar_sync.google, "query_busy", return_value=google_busy) as query:
                effective, warnings = google_calendar_sync.planner_availability("alice", _availability(), now, timedelta(0))
                self.assertEqual((_free(effective), warnings), (want, []))
                self.assertEqual(query.called, avoid)   # "Avoid conflicts" off never asks Google


if __name__ == "__main__":
    unittest.main()
