"""Google Calendar <-> Study Planner (MVP, one-way in each direction).

- Connection lifecycle: finish the OAuth callback (tokens + the dedicated calendar), status,
  settings, disconnect.
- Busy time -> Planner: the primary calendar's busy intervals become "busy" availability rows that
  study_time.free_minutes_by_date subtracts from the learner's availability BEFORE the unchanged
  deterministic scheduler runs. Titles/descriptions are never read. Any Google failure degrades to
  manual availability plus a non-blocking warning -- the Planner never fails because of Google.
- Planner -> Google: reconcile() mirrors the owner's confirmed sessions into the "AI Tutor Study
  Plan" calendar, idempotently (deterministic event ids + a content hash per link: unchanged
  sessions cause no write, a repeat never duplicates). It only ever touches events it created and
  linked; Google-side edits never change the Planner.

Planner actions call after_planner_change() after they have committed: a sync failure is recorded
on the event link (sync_status="error") and never undoes the planner action.
"""

import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone

from backend import google_calendar_service as google
from backend import google_calendar_store, study_planner_store
from backend.indexed_document_store import list_indexed_documents
from backend.study_scheduler import DEFAULT_CONFIG
from backend.study_scheduler_contracts import REASON_LABELS

logger = logging.getLogger(__name__)

ACTIVITY_LABELS = {
    "summary": "Summary", "flashcards": "Flashcards", "quiz": "Quiz", "quiz_retry": "Quiz retry",
    "review": "Review", "written_quiz": "Written Quiz",
}
EVENT_SOURCE = "ai_tutor"
# Google busy time is read for the scheduler's whole horizon (study_scheduler max_horizon_days).
BUSY_WINDOW_DAYS = DEFAULT_CONFIG.max_horizon_days + 1
# Sessions that ended longer ago than this are not newly created in Google (existing links are kept).
BACKFILL_DAYS = 14

WARNING_UNAVAILABLE = "google_calendar_unavailable"
WARNING_RECONNECT = "google_calendar_reconnect"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -- connection lifecycle ---------------------------------------------------------

def status(owner_id: str) -> dict:
    connection = google_calendar_store.get_connection(owner_id)
    connected = bool(connection and connection["status"] == "connected")
    links = google_calendar_store.link_summary(owner_id) if connection else {"errors": 0, "last_synced_at": None}
    return {
        "configured": google.is_configured(),
        "connected": connected,
        "reconnect_required": bool(connection and connection["status"] == "reconnect_required"),
        "calendar_id": connection["study_calendar_id"] if connected else None,
        "calendar_name": connection["study_calendar_name"] if connected else None,
        "connected_at": connection["connected_at"] if connected else None,
        "avoid_conflicts": bool(connection["avoid_conflicts"]) if connection else True,
        "sync_sessions": bool(connection["sync_sessions"]) if connection else True,
        "sync_errors": links["errors"],
        "last_synced_at": links["last_synced_at"],
    }


def remember_utc_offset(owner_id: str, utc_offset_minutes: int | None) -> None:
    """Sessions are naive learner-local times; their Google events need the learner's offset."""
    if utc_offset_minutes is None:
        return
    connection = google_calendar_store.get_connection(owner_id)
    if connection and connection["utc_offset_minutes"] != int(utc_offset_minutes):
        google_calendar_store.update_connection(owner_id, {"utc_offset_minutes": int(utc_offset_minutes)})


def complete_connection(owner_id: str, code: str) -> dict:
    """The OAuth callback after its state was validated: exchange the code, store the ENCRYPTED
    refresh token, create or reuse the dedicated calendar, then sync existing confirmed sessions."""
    tokens = google.exchange_code(code)
    refresh_token = tokens.get("refresh_token")
    granted = set(str(tokens.get("scope", "")).split())
    if not refresh_token:
        raise google.GoogleCalendarError("no_refresh_token", "Google did not grant offline access.")
    if not set(google.SCOPES) <= granted:
        google.revoke_token(refresh_token)
        raise google.GoogleCalendarError("scope_denied", "Both Google Calendar permissions are needed.")
    previous = google_calendar_store.get_connection(owner_id)
    google_calendar_store.save_connection(owner_id, google.encrypt_token(refresh_token), " ".join(sorted(granted)))
    google.remember_access_token(owner_id, tokens.get("access_token", ""), tokens.get("expires_in"))
    try:
        calendar = _ensure_study_calendar(owner_id, previous)
    except Exception:
        google_calendar_store.delete_connection(owner_id)
        google.forget_access_token(owner_id)
        raise
    reconcile_safely(owner_id)
    return calendar


def _ensure_study_calendar(owner_id: str, previous: dict | None = None) -> dict:
    """Reuse the owner's "AI Tutor Study Plan" calendar when it still exists (the connection's, or
    the one earlier events were synced into); create it only when there is none."""
    candidates = [previous["study_calendar_id"]] if previous and previous.get("study_calendar_id") else []
    candidates += [cid for cid in google_calendar_store.known_calendar_ids(owner_id) if cid not in candidates]
    for calendar_id in candidates:
        if google.calendar_exists(owner_id, calendar_id):
            name = (previous or {}).get("study_calendar_name") or google.STUDY_CALENDAR_NAME
            google_calendar_store.update_connection(owner_id, {"study_calendar_id": calendar_id, "study_calendar_name": name})
            return {"id": calendar_id, "summary": name}
    calendar = google.create_study_calendar(owner_id)
    google_calendar_store.update_connection(owner_id, {"study_calendar_id": calendar["id"],
                                                       "study_calendar_name": calendar["summary"]})
    return calendar


def disconnect(owner_id: str) -> dict:
    """Revoke Google access when possible and forget the tokens. Planner sessions, the Google events
    already created and their links (history, so a reconnect updates instead of duplicating) stay."""
    connection = google_calendar_store.get_connection(owner_id)
    revoked = False
    if connection:
        try:
            revoked = google.revoke_token(google.decrypt_token(connection["encrypted_refresh_token"]))
        except (google.GoogleCalendarError, google.GoogleCalendarConfigError):
            revoked = False
    google_calendar_store.delete_connection(owner_id)
    google_calendar_store.delete_oauth_states(owner_id)
    google.forget_access_token(owner_id)
    return {"connected": False, "revoked": revoked}


def update_settings(owner_id: str, changes: dict) -> dict:
    connection = google_calendar_store.get_connection(owner_id)
    if not connection:
        raise LookupError("Google Calendar is not connected.")
    google_calendar_store.update_connection(owner_id, changes)
    if changes.get("sync_sessions"):
        reconcile_safely(owner_id)
    return status(owner_id)


# -- busy time -> Planner ----------------------------------------------------------

def _parse_google_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local(value: datetime, offset: timedelta) -> datetime:
    return (value.astimezone(timezone.utc) + offset).replace(tzinfo=None)


def busy_intervals(owner_id: str, local_start: datetime, local_end: datetime, offset: timedelta) -> dict:
    """The owner's Google busy time in [local_start, local_end) on the learner's naive-local clock.

    {"intervals": [(start_iso, end_iso)], "warning": None | code}. Nothing (and no warning) when
    Google Calendar is not connected or "Avoid conflicts" is off -- the Planner then behaves
    exactly as without the integration."""
    connection = google_calendar_store.get_connection(owner_id)
    if connection and connection["status"] == "reconnect_required":
        return {"intervals": [], "warning": WARNING_RECONNECT}
    if not connection or not connection["avoid_conflicts"]:
        return {"intervals": [], "warning": None}
    def to_utc(local: datetime) -> str:
        return (local - offset).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        raw = google.query_busy(owner_id, to_utc(local_start), to_utc(local_end))
    except google.GoogleReconnectRequired:
        return {"intervals": [], "warning": WARNING_RECONNECT}
    except (google.GoogleCalendarError, google.GoogleCalendarConfigError, KeyError, ValueError) as error:
        logger.warning("Google busy time unavailable for planner: %s", error)
        return {"intervals": [], "warning": WARNING_UNAVAILABLE}
    intervals = []
    for start, end in raw:
        begin = max(_local(_parse_google_time(start), offset), local_start)
        finish = min(_local(_parse_google_time(end), offset), local_end)
        if finish > begin:
            intervals.append((begin.isoformat(), finish.isoformat()))
    return {"intervals": sorted(intervals), "warning": None}


def busy_availability_rows(intervals: list[tuple[str, str]]) -> list[dict]:
    """Busy intervals as availability rows with busy=True, one per local day (a meeting across
    midnight is split). study_time.free_minutes_by_date subtracts them from availability; they are
    never available time themselves. Minutes round outwards so a partial minute still blocks."""
    rows = []
    for start_iso, end_iso in intervals:
        start, end = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
        day = start.date()
        while datetime.combine(day, time()) < end:
            day_start = datetime.combine(day, time())
            begin, finish = max(start, day_start), min(end, day_start + timedelta(days=1))
            first = begin.hour * 60 + begin.minute
            seconds_to_end = (finish - day_start).total_seconds()
            last = min(24 * 60, int(-(-seconds_to_end // 60)))
            if last > first:
                rows.append({"busy": True, "is_recurring": False, "date": day.isoformat(),
                             "start_at": f"{first // 60:02d}:{first % 60:02d}",
                             "end_at": f"{last // 60:02d}:{last % 60:02d}"})
            day += timedelta(days=1)
    return rows


def planner_availability(owner_id: str, availability: list[dict], now: datetime, offset: timedelta) -> tuple[list[dict], list[dict]]:
    """(effective availability, warnings) for a scheduling run starting at the naive-local `now`:
    the learner's availability plus Google busy rows over the scheduler's horizon."""
    start = datetime.combine(now.date(), time())
    found = busy_intervals(owner_id, start, start + timedelta(days=BUSY_WINDOW_DAYS), offset)
    warnings = [{"code": found["warning"]}] if found["warning"] else []
    return list(availability) + busy_availability_rows(found["intervals"]), warnings


# -- Planner -> Google ------------------------------------------------------------

def event_id_for(owner_id: str, session_id: str) -> str:
    """Deterministic Google event id (base32hex alphabet: 0-9a-v) -- the idempotency key."""
    return "aitutor" + hashlib.sha256(f"{owner_id}:{session_id}".encode("utf-8")).hexdigest()[:40]


def event_title(document_title: str, activity_type: str) -> str:
    return f"{document_title} · {ACTIVITY_LABELS.get(activity_type, activity_type)}"


def _with_offset(naive_iso: str, offset: timedelta) -> str:
    return datetime.fromisoformat(naive_iso).replace(tzinfo=timezone(offset)).isoformat()


def event_body(session: dict, document_title: str, offset: timedelta) -> dict:
    """The Google event for one session. The description holds only the planner's reason label --
    never quiz answers or scores."""
    reason = REASON_LABELS.get(session.get("reason") or "", "")
    description = "\n\n".join(part for part in (reason, "Created by AI Tutor Study Planner") if part)
    return {
        "summary": event_title(document_title, session["activity_type"]),
        "description": description,
        "start": {"dateTime": _with_offset(session["scheduled_start"], offset)},
        "end": {"dateTime": _with_offset(session["scheduled_end"], offset)},
        "extendedProperties": {"private": {"source": EVENT_SOURCE, "session_id": session["session_id"],
                                           "activity_type": session["activity_type"]}},
    }


def _hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _wanted(session: dict, plan_status: str | None) -> bool:
    """Confirmed sessions that belong on the calendar: work under way or done (completed events are
    kept), and still-planned sessions of an ACTIVE plan. Skipped / cancelled / rescheduled (moved)
    sessions and the plans' preview proposals (never persisted) are not."""
    if session["status"] in ("in_progress", "completed"):
        return True
    return session["status"] in ("scheduled", "missed") and plan_status == "active"


def reconcile(owner_id: str, *, _retry_calendar: bool = True) -> dict:
    """Make the study calendar mirror the owner's confirmed sessions: create missing events,
    update changed ones, delete the linked events of sessions that are gone/skipped/cancelled.
    Unchanged sessions cause no Google write. Returns counts and an overall status."""
    result = {"status": "ok", "created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "errors": 0}
    connection = google_calendar_store.get_connection(owner_id)
    if not connection:
        return {**result, "status": "disconnected"}
    if connection["status"] != "connected":
        return {**result, "status": "reconnect_required"}
    if not connection["sync_sessions"]:
        return {**result, "status": "disabled"}
    if connection["utc_offset_minutes"] is None:
        return {**result, "status": "pending"}   # the learner's timezone is not known yet
    offset = timedelta(minutes=connection["utc_offset_minutes"])
    try:
        calendar_id = connection["study_calendar_id"] or _ensure_study_calendar(owner_id, connection)["id"]
    except google.GoogleReconnectRequired:
        return {**result, "status": "reconnect_required"}

    sessions = study_planner_store.list_sessions(owner_id)
    by_id = {session["session_id"]: session for session in sessions}
    plan_status = {plan["plan_id"]: plan["status"] for plan in study_planner_store.list_plans(owner_id)}
    titles = {doc["document_id"]: doc["display_name"] or doc["document_id"] for doc in list_indexed_documents(owner_id)}
    links = {}
    for link in google_calendar_store.list_event_links(owner_id):
        if link["calendar_id"] == calendar_id:
            links[link["session_id"]] = link
        else:   # a calendar of an earlier connection: never touched again
            google_calendar_store.delete_event_link(owner_id, link["session_id"])
    wanted = [s for s in sessions if _wanted(s, plan_status.get(s["plan_id"]))]
    wanted_ids = {s["session_id"] for s in wanted}

    # A rescheduled session inherits the event of the session it replaces: update, never duplicate.
    for session in wanted:
        if session["session_id"] in links:
            continue
        origin, seen = session.get("rescheduled_from"), set()
        while origin and origin not in links and origin in by_id and origin not in seen:
            seen.add(origin)
            origin = by_id[origin].get("rescheduled_from")
        if origin in links and origin not in wanted_ids:
            google_calendar_store.move_event_link(owner_id, origin, session["session_id"])
            links[session["session_id"]] = {**links.pop(origin), "session_id": session["session_id"], "synced_hash": None}

    backfill_from = (_utc_now() + offset).replace(tzinfo=None) - timedelta(days=BACKFILL_DAYS)
    try:
        for session in wanted:
            session_id = session["session_id"]
            link = links.get(session_id)
            if not link and datetime.fromisoformat(session["scheduled_end"]) < backfill_from:
                continue
            body = event_body(session, titles.get(session["document_id"], session["document_id"]), offset)
            digest = _hash(body)
            if link and link["synced_hash"] == digest and link["sync_status"] == "synced":
                result["unchanged"] += 1
                continue
            event_id = link["google_event_id"] if link else event_id_for(owner_id, session_id)
            try:
                if link:
                    google.update_event(owner_id, calendar_id, event_id, body)
                    result["updated"] += 1
                else:
                    google.insert_event(owner_id, calendar_id, event_id, body)
                    result["created"] += 1
                google_calendar_store.save_event_link(owner_id, session_id, calendar_id, event_id,
                                                      synced_hash=digest, sync_status="synced")
            except google.GoogleReconnectRequired:
                raise
            except google.GoogleCalendarError as error:
                if error.status == 404 and _retry_calendar and not google.calendar_exists(owner_id, calendar_id):
                    return _recreate_calendar_and_retry(owner_id)
                result["errors"] += 1
                google_calendar_store.save_event_link(owner_id, session_id, calendar_id, event_id,
                                                      synced_hash=None, sync_status="error", last_error=str(error))
        for session_id, link in links.items():
            if session_id in wanted_ids:
                continue
            try:
                google.delete_event(owner_id, calendar_id, link["google_event_id"])
                google_calendar_store.delete_event_link(owner_id, session_id)
                result["deleted"] += 1
            except google.GoogleReconnectRequired:
                raise
            except google.GoogleCalendarError as error:
                result["errors"] += 1
                google_calendar_store.mark_event_link_error(owner_id, session_id, str(error))
    except google.GoogleReconnectRequired:
        return {**result, "status": "reconnect_required"}
    if result["errors"]:
        result["status"] = "error"
    return result


def _recreate_calendar_and_retry(owner_id: str) -> dict:
    """The study calendar was deleted in Google: create it again and resync everything into it."""
    google_calendar_store.update_connection(owner_id, {"study_calendar_id": None})
    _ensure_study_calendar(owner_id, None)
    return reconcile(owner_id, _retry_calendar=False)


def reconcile_safely(owner_id: str) -> dict:
    try:
        return reconcile(owner_id)
    except (google.GoogleCalendarError, google.GoogleCalendarConfigError) as error:
        logger.warning("Google Calendar sync failed: %s", error)
        return {"status": "error", "errors": 1, "message": str(error)}
    except Exception:   # never let the integration break a planner action
        logger.exception("Google Calendar sync crashed")
        return {"status": "error", "errors": 1}


def after_planner_change(owner_id: str, utc_offset_minutes: int | None = None) -> dict | None:
    """Hook for planner actions, called AFTER they committed. None when the owner has no Google
    connection (the response then stays exactly as before the integration); otherwise the sync
    outcome, which never raises and never undoes the planner action."""
    try:
        connection = google_calendar_store.get_connection(owner_id)
    except Exception:
        logger.exception("Google Calendar connection lookup failed")
        return None
    if not connection:
        return None
    remember_utc_offset(owner_id, utc_offset_minutes)
    outcome = reconcile_safely(owner_id)
    return {"status": outcome["status"], "errors": outcome.get("errors", 0)}
