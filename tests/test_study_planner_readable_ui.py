"""Real-browser tests for the readable Study Planner (headless Chrome; the lifecycle mock from
tests/test_study_planner_today_ui.py). "Now" is pinned to Thu 2026-09-24 12:00.

Week view: sessions name their subject first (a readable label, never only a raw file name), keep
their real start/end at every zoom level, sit side by side when they overlap, and differ visibly by
status; a day header expands that day. Agenda view: the same week as readable cards grouped by day,
with the same actions. Narrow screens: the saved plan is an agenda list without horizontal overflow.
Runs at 1280px, 1100px and 390px. Skipped when Chrome is not installed.
"""

import unittest

from tests.test_quiz_player_ui import find_chrome
from tests.test_study_planner_calendar_ui import run_at_width
from tests.test_study_planner_today_ui import MOCK

DRIVER = r"""
{
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {errors: [], width: window.innerWidth};
const publish = () => {
  if (window.parent !== window) { window.parent.postMessage(JSON.stringify(out), "*"); return; }
  const pre = document.createElement("pre"); pre.id = "harness-out"; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
};
window.addEventListener("error", (e) => out.errors.push(String(e.message) + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => out.errors.push("rejection: " + String(e.reason && e.reason.message || e.reason)));
const $ = (id) => document.getElementById(id);
const P = window.__planner;
const titles = {"mkt.pdf": "Marketing", "stats.pdf": "Statistics", "emb.pdf": "Lecture_03-Embedded_Systems.pdf"};
const session = (id, doc, activity, start, end, minutes, status = "scheduled", extra = {}) => ({session_id: id, document_id: doc,
  document_title: titles[doc], activity_type: activity, scheduled_start: start, scheduled_end: end, duration_minutes: minutes,
  status, reason: {code: "new_material", message: "New material to learn"}, artifact_id: null, rescheduled_from: null, ...extra});
const noOverflow = () => document.documentElement.scrollWidth <= window.innerWidth + 1;
const shown = (el) => el && el.getClientRects().length && getComputedStyle(el).display !== "none" ? el.textContent : null;
const ev = (id) => document.querySelector(`.pcal-event[data-session-id="${id}"]`);
const box = (id) => { const r = ev(id).getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
const card = (id) => document.querySelector(`.pcal-agenda-card[data-session-id="${id}"]`);
(async () => {
  await sleep(1500);
  const noMotion = document.createElement("style"); noMotion.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}"; document.head.appendChild(noMotion);
  plannerNow = () => new Date(2026, 8, 24, 12, 0, 0);
  P.plans = [{plan_id: "plan-1", title: "Now", status: "active"}];
  P.sessionsByPlan = {"plan-1": [
    session("s-ahead", "mkt.pdf", "summary", "2026-09-25T18:00:00", "2026-09-25T18:45:00", 45),
    session("s-long", "emb.pdf", "quiz", "2026-09-25T10:00:00", "2026-09-25T10:30:00", 30),
    session("s-start", "stats.pdf", "flashcards", "2026-09-26T10:00:00", "2026-09-26T10:20:00", 20),
    session("s-running", "stats.pdf", "quiz", "2026-09-24T11:30:00", "2026-09-24T12:15:00", 45, "in_progress"),
    session("s-done", "mkt.pdf", "flashcards", "2026-09-22T18:00:00", "2026-09-22T18:40:00", 40, "completed",
            {completed_at: "2026-09-22T18:45:00", started_at: "2026-09-22T18:01:00"}),
    session("s-over", "stats.pdf", "review", "2026-09-22T18:20:00", "2026-09-22T19:00:00", 40),
  ]};
  out.desktop = innerWidth >= 1024;
  setPage("planner"); await sleep(800);

  // Shared rules, the same on every width.
  out.lanes = pcalLayoutLanes([{start: 0, end: 30}, {start: 15, end: 45}, {start: 45, end: 60}, {start: 50, end: 70}, {start: 100, end: 120}]);
  out.validation = [10, 19, 20, 30, 20.5].map(plannerValidateNewSessionMinutes);
  out.rules = {presets: PLANNER_SESSION_DURATION_PRESETS, def: PLANNER_DEFAULT_SESSION_MINUTES, min: PLANNER_MIN_SESSION_MINUTES};
  out.display = ["Lecture_03-Embedded_Systems.pdf", "Biology - Chapter 1.pdf", "Marketing", "covid-19 notes.txt"].map(plannerDisplayTitle);

  if (!out.desktop) {
    const cards = [...document.querySelectorAll("#planner-plan-sessions .planner-session")];
    out.narrow = {workspaceHidden: $("planner-workspace").hidden, overflow: noOverflow(),
      days: [...document.querySelectorAll("#planner-plan-sessions .planner-day h4")].length,
      cards: cards.map((c) => ({title: c.querySelector("strong").textContent, full: c.querySelector("strong").title,
        time: c.querySelector(".planner-session-time").textContent, live: c.querySelector(".planner-session-live")?.textContent || null,
        fits: c.getBoundingClientRect().right <= window.innerWidth + 1,
        fontSize: parseFloat(getComputedStyle(c.querySelector("strong")).fontSize),
        actionsClear: (() => { const a = c.querySelector(".planner-session-actions"); if (!a || !a.getClientRects().length) return true;
          const ar = a.getBoundingClientRect(), br = c.querySelector(".planner-session-body").getBoundingClientRect();
          return ar.top >= br.bottom - 1 || ar.left >= br.right - 1; })()}))};
    publish();
    return;
  }

  // Week view at the default (comfortable) zoom.
  $("pcal-scroll").scrollTop = 9 * PCAL_HOUR_PX;
  out.week = {hourPx: PCAL_HOUR_PX, weekPressed: $("pcal-view-week").getAttribute("aria-pressed"),
    long: {title: ev("s-long").querySelector(".pcal-event-title").textContent, tooltip: ev("s-long").title,
      aria: ev("s-long").getAttribute("aria-label"), time: shown(ev("s-long").querySelector(".pcal-event-time")),
      duration: shown(ev("s-long").querySelector(".pcal-event-duration"))},
    laneEnd: shown(ev("s-over").querySelector(".pcal-event-end")),
    overlap: {done: box("s-done"), over: box("s-over"), lanes: [ev("s-done").classList.contains("is-lane"), ev("s-over").classList.contains("is-lane")],
      tops: [ev("s-done").style.top, ev("s-over").style.top], heights: [ev("s-done").style.height, ev("s-over").style.height]},
    styles: Object.fromEntries(["s-ahead", "s-running", "s-done", "s-over"].map((id) => [id, {bg: getComputedStyle(ev(id)).backgroundColor,
      border: getComputedStyle(ev(id)).borderTopStyle, status: shown(ev(id).querySelector(".pcal-event-status"))}])),
    titleFont: parseFloat(getComputedStyle(ev("s-ahead").querySelector(".pcal-event-title")).fontSize),
    overflow: noOverflow()};

  // Expanded day: the day header widens that column; the others stay visible; again = equal widths.
  const equal = box("s-ahead").width;
  document.querySelector('.pcal-dayhead[data-date="2026-09-25"]').click(); await sleep(50);
  out.focus = {before: equal, focused: box("s-ahead").width, other: box("s-start").width,
    pressed: document.querySelector('.pcal-dayhead[data-date="2026-09-25"]').getAttribute("aria-pressed"), overflow: noOverflow()};
  document.querySelector('.pcal-dayhead[data-date="2026-09-25"]').click(); await sleep(50);
  out.focus.after = box("s-ahead").width;

  // Zoom: taller hours, same times.
  const before = {top: parseFloat(ev("s-ahead").style.top), height: parseFloat(ev("s-ahead").style.height)};
  $("pcal-zoom-in").click(); await sleep(50);
  out.zoom = {before, after: {top: parseFloat(ev("s-ahead").style.top), height: parseFloat(ev("s-ahead").style.height)}, px: PCAL_HOUR_PX};
  $("pcal-zoom-out").click(); await sleep(50);
  out.zoom.back = PCAL_HOUR_PX;

  // Agenda view: the same week as cards grouped by day, with the same actions.
  $("pcal-view-agenda").click(); await sleep(80);
  const agendaCards = [...document.querySelectorAll(".pcal-agenda-card")];
  out.agenda = {shown: !$("pcal-agenda").hidden, gridHidden: $("pcal-scroll").hidden, zoomHidden: $("pcal-zoom-in").parentElement.hidden,
    pressed: $("pcal-view-agenda").getAttribute("aria-pressed"),
    days: [...document.querySelectorAll(".pcal-agenda-day")].map((d) => d.dataset.date),
    today: document.querySelector(".pcal-agenda-day.is-today .pcal-agenda-today")?.textContent || null,
    cards: agendaCards.map((c) => ({id: c.dataset.sessionId, title: c.querySelector(".pcal-agenda-title").textContent,
      full: c.querySelector(".pcal-agenda-title").title || null, start: c.querySelector(".pcal-agenda-start").textContent,
      end: c.querySelector(".pcal-agenda-end").textContent, duration: c.querySelector(".pcal-agenda-duration").textContent,
      status: c.querySelector(".pcal-agenda-status").textContent, activity: c.querySelector(".pcal-agenda-activity").textContent,
      actions: [...c.querySelectorAll(".pcal-agenda-actions button")].map((b) => b.textContent),
      fits: c.getBoundingClientRect().right <= $("pcal-agenda").getBoundingClientRect().right + 1})),
    overflow: noOverflow()};
  card("s-start").querySelector(".pcal-agenda-details").click(); await sleep(50);
  out.details = {open: !$("pcal-popover").hidden, title: $("pcal-popover").querySelector(".pcal-popover-title")?.textContent || null};
  document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
  P.actionCalls = [];
  const skip = [...card("s-ahead").querySelectorAll("button")].find((b) => b.textContent === "Skip");
  skip.click(); skip.click();
  await sleep(1200);
  out.agendaCalls = P.actionCalls.map((c) => [c[0], c[1]]);
  out.agendaAfter = {stillAgenda: !$("pcal-agenda").hidden, cards: document.querySelectorAll(".pcal-agenda-card").length};
  $("pcal-view-week").click(); await sleep(80);
  out.backToWeek = {gridShown: !$("pcal-scroll").hidden, events: document.querySelectorAll("#pcal-body .pcal-event").length};
  publish();
})().catch((error) => { out.fatal = String(error && error.stack || error); publish(); });
}
"""


class SharedRuleAssertions:
    def test_no_script_errors(self):
        self.assertNotIn("fatal", self.out)
        self.assertEqual(self.out["errors"], [])

    def test_overlapping_events_get_side_by_side_lanes(self):
        self.assertEqual(self.out["lanes"], [{"lane": 0, "lanes": 2}, {"lane": 1, "lanes": 2}, {"lane": 0, "lanes": 2},
                                             {"lane": 1, "lanes": 2}, {"lane": 0, "lanes": 1}])

    def test_new_session_duration_rules(self):
        self.assertEqual(self.out["rules"], {"presets": [20, 30, 40, 50], "def": 30, "min": 20})
        valid = [message == "" for message in self.out["validation"]]
        self.assertEqual(valid, [False, False, True, True, False])

    def test_readable_labels_from_stored_names(self):
        self.assertEqual(self.out["display"], ["Lecture 03 Embedded Systems", "Biology - Chapter 1", "Marketing", "covid-19 notes"])


class WeekAndAgendaAssertions(SharedRuleAssertions):
    def test_week_blocks_name_the_subject_with_time_and_duration(self):
        week = self.out["week"]
        self.assertEqual((week["hourPx"], week["weekPressed"]), (112, "true"))
        long = week["long"]
        self.assertEqual(long["title"], "Lecture 03 Embedded Systems")      # never the raw file name only
        self.assertIn("Lecture_03-Embedded_Systems.pdf", long["tooltip"])   # the stored name stays reachable
        self.assertIn("Lecture_03-Embedded_Systems.pdf", long["aria"])
        self.assertEqual((long["time"], long["duration"]), ("10:00–10:30", "30m"))
        self.assertGreaterEqual(week["titleFont"], 13)
        self.assertTrue(week["overflow"])

    def test_overlapping_sessions_never_cover_each_other_and_keep_real_times(self):
        overlap = self.out["week"]["overlap"]
        self.assertEqual(overlap["lanes"], [True, True])
        self.assertIsNone(self.out["week"]["laneEnd"])   # a narrow lane shows its start; the range is in the tooltip
        self.assertLessEqual(overlap["done"]["right"], overlap["over"]["left"] + 1)
        # 18:00-18:40 and 18:20-19:00 at 112px/hour
        self.assertEqual(overlap["tops"], [f"{18 * 112}px", f"{round((18 * 60 + 20) * 112 / 60, 2):g}px"])
        self.assertEqual([round(float(h[:-2])) for h in overlap["heights"]], [round(40 * 112 / 60 - 2)] * 2)

    def test_status_is_visually_distinct(self):
        styles = self.out["week"]["styles"]
        backgrounds = {styles[key]["bg"] for key in ("s-ahead", "s-running", "s-done", "s-over")}
        self.assertEqual(len(backgrounds), 4)
        self.assertEqual(styles["s-running"]["status"], "In progress")
        self.assertEqual(styles["s-over"]["border"], "dashed")
        self.assertIsNone(styles["s-ahead"]["status"])

    def test_day_header_expands_that_day_without_hiding_the_others(self):
        focus = self.out["focus"]
        self.assertEqual(focus["pressed"], "true")
        self.assertGreater(focus["focused"], focus["before"] * 2)
        self.assertGreater(focus["other"], 40)
        self.assertAlmostEqual(focus["after"], focus["before"], delta=1)
        self.assertTrue(focus["overflow"])

    def test_zoom_changes_scale_but_not_times(self):
        zoom = self.out["zoom"]
        self.assertEqual((zoom["px"], zoom["back"]), (144, 112))
        self.assertAlmostEqual(zoom["after"]["top"] / 144, zoom["before"]["top"] / 112, places=3)   # still 18:00
        self.assertAlmostEqual((zoom["after"]["height"] + 2) / 144, (zoom["before"]["height"] + 2) / 112, places=3)

    def test_agenda_lists_the_week_by_day_with_readable_cards(self):
        agenda = self.out["agenda"]
        self.assertEqual((agenda["shown"], agenda["gridHidden"], agenda["zoomHidden"], agenda["pressed"]), (True, True, True, "true"))
        self.assertEqual(agenda["days"], ["2026-09-22", "2026-09-24", "2026-09-25", "2026-09-26"])
        self.assertEqual(agenda["today"], "Today")
        cards = {card["id"]: card for card in agenda["cards"]}
        self.assertEqual([card["id"] for card in agenda["cards"]], ["s-done", "s-over", "s-running", "s-long", "s-ahead", "s-start"])
        self.assertEqual({key: cards[key]["status"] for key in cards},
                         {"s-done": "Completed", "s-over": "Not completed", "s-running": "In progress", "s-long": "Planned",
                          "s-ahead": "Planned", "s-start": "Planned"})
        self.assertEqual((cards["s-long"]["title"], cards["s-long"]["full"]), ("Lecture 03 Embedded Systems", "Lecture_03-Embedded_Systems.pdf"))
        self.assertEqual((cards["s-ahead"]["start"], cards["s-ahead"]["end"], cards["s-ahead"]["duration"], cards["s-ahead"]["activity"]),
                         ("18:00", "to 18:45", "45m", "Summary"))
        self.assertEqual(cards["s-ahead"]["actions"], ["Start Session", "Edit", "Reschedule", "Skip", "Details"])
        self.assertEqual(cards["s-running"]["actions"], ["Resume", "Complete session", "Skip", "Details"])
        self.assertEqual(cards["s-done"]["actions"], ["Details"])
        self.assertTrue(all(card["fits"] for card in agenda["cards"]))
        self.assertTrue(agenda["overflow"])

    def test_agenda_actions_use_the_existing_lifecycle_once(self):
        self.assertEqual(self.out["details"], {"open": True, "title": "Statistics"})
        self.assertEqual(self.out["agendaCalls"], [["skip", "s-ahead"]])
        self.assertTrue(self.out["agendaAfter"]["stillAgenda"])
        self.assertEqual(self.out["backToWeek"], {"gridShown": True, "events": 6})


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ReadableDesktopTests(WeekAndAgendaAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1280, 900, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ReadableNarrowDesktopTests(WeekAndAgendaAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(1100, 800, MOCK, DRIVER)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ReadablePhoneTests(SharedRuleAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(390, 844, MOCK, DRIVER)

    def test_phone_lists_sessions_as_an_agenda_without_overflow(self):
        narrow = self.out["narrow"]
        self.assertTrue(narrow["workspaceHidden"])
        self.assertTrue(narrow["overflow"])
        self.assertGreaterEqual(narrow["days"], 3)
        cards = {card["time"]: card for card in narrow["cards"]}
        self.assertEqual((cards["10:00–10:30"]["title"], cards["10:00–10:30"]["full"]),
                         ("Lecture 03 Embedded Systems", "Lecture_03-Embedded_Systems.pdf"))
        self.assertEqual(cards["11:30–12:15"]["live"], "In progress")
        for card in narrow["cards"]:
            self.assertTrue(card["fits"], card)
            self.assertGreaterEqual(card["fontSize"], 14)
            self.assertTrue(card["actionsClear"], card)


@unittest.skipUnless(find_chrome(), "Chrome is not installed")
class ReadableTabletTests(ReadablePhoneTests):
    @classmethod
    def setUpClass(cls):
        cls.out = run_at_width(768, 1000, MOCK, DRIVER)


if __name__ == "__main__":
    unittest.main()
