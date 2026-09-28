"""Google Calendar integration routes (MVP one-way planning integration).

OAuth is handled entirely by the backend: /connect redirects to Google with a one-time state bound
to the signed-in user, /callback validates it, stores the ENCRYPTED refresh token and sends the
browser back to the Planner with ?google_calendar=connected|error. No token or client secret ever
reaches the frontend or any response body.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from backend import google_calendar_service, google_calendar_store, google_calendar_sync
from backend.api.deps import require_current_user
from backend.auth_store import get_user_for_session
from backend.study_plan_api_service import PlanValidationError, _validate_utc_offset
from config import AUTH_COOKIE_NAME, google_calendar_settings

router = APIRouter(prefix="/api/integrations/google-calendar")
logger = logging.getLogger(__name__)


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    utc_offset_minutes: Optional[int] = None


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    avoid_conflicts: Optional[bool] = None
    sync_sessions: Optional[bool] = None
    utc_offset_minutes: Optional[int] = None


def _offset_minutes(utc_offset_minutes: Optional[int]) -> Optional[int]:
    if utc_offset_minutes is None:
        return None
    try:
        return int(_validate_utc_offset(utc_offset_minutes).total_seconds() // 60)
    except PlanValidationError as error:
        raise HTTPException(status_code=400, detail=error.payload) from error


def _planner_redirect(outcome: str, reason: str | None = None) -> RedirectResponse:
    query = {"google_calendar": outcome, **({"reason": reason} if reason else {})}
    return RedirectResponse(f"{google_calendar_settings()['AI_TUTOR_FRONTEND_URL']}/?{urlencode(query)}", status_code=303)


@router.get("/status")
def google_calendar_status(utc_offset_minutes: Optional[int] = None,
                           current_user: dict = Depends(require_current_user)) -> dict:
    google_calendar_sync.remember_utc_offset(current_user["id"], _offset_minutes(utc_offset_minutes))
    return google_calendar_sync.status(current_user["id"])


@router.get("/connect")
def google_calendar_connect(current_user: dict = Depends(require_current_user)) -> RedirectResponse:
    try:
        url = google_calendar_service.build_authorization_url(current_user["id"])
    except google_calendar_service.GoogleCalendarConfigError as error:
        raise HTTPException(status_code=503, detail={"code": "not_configured", "message": str(error)}) from error
    return RedirectResponse(url, status_code=303)


@router.get("/callback")
def google_calendar_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    """Google's redirect target. The state must be unexpired, unused and belong to the user whose
    session cookie made this request; anything else is refused before the code is used."""
    user = get_user_for_session(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        return _planner_redirect("error", "signed_out")
    if not google_calendar_store.consume_oauth_state(state, user["id"]):
        return _planner_redirect("error", "invalid_state")
    if error:   # e.g. access_denied: the user declined on Google's consent screen
        return _planner_redirect("error", "access_denied" if error == "access_denied" else "google_error")
    if not code:
        return _planner_redirect("error", "google_error")
    try:
        google_calendar_sync.complete_connection(user["id"], code)
    except google_calendar_service.GoogleCalendarConfigError:
        return _planner_redirect("error", "not_configured")
    except google_calendar_service.GoogleCalendarError as failure:
        logger.warning("Google Calendar connection failed: %s", failure)
        return _planner_redirect("error", failure.code if failure.code in ("scope_denied", "no_refresh_token") else "google_error")
    return _planner_redirect("connected")


@router.post("/disconnect")
def google_calendar_disconnect(current_user: dict = Depends(require_current_user)) -> dict:
    """Revoke (best effort) and forget the connection. Planner sessions are never touched."""
    result = google_calendar_sync.disconnect(current_user["id"])
    return {**google_calendar_sync.status(current_user["id"]), "revoked": result["revoked"]}


@router.patch("/settings")
def google_calendar_update_settings(request: SettingsRequest, current_user: dict = Depends(require_current_user)) -> dict:
    changes = request.model_dump(exclude_none=True)
    if "utc_offset_minutes" in changes:
        changes["utc_offset_minutes"] = _offset_minutes(changes["utc_offset_minutes"])
    try:
        return google_calendar_sync.update_settings(current_user["id"], changes)
    except LookupError as error:
        raise HTTPException(status_code=409, detail={"code": "not_connected", "message": str(error)}) from error


@router.post("/sync")
def google_calendar_sync_now(request: Optional[SyncRequest] = None, current_user: dict = Depends(require_current_user)) -> dict:
    """Retry reconciliation of the current user's confirmed sessions with their study calendar."""
    owner_id = current_user["id"]
    if not google_calendar_sync.status(owner_id)["connected"]:
        raise HTTPException(status_code=409, detail={"code": "not_connected", "message": "Google Calendar is not connected."})
    google_calendar_sync.remember_utc_offset(owner_id, _offset_minutes(request.utc_offset_minutes if request else None))
    result = google_calendar_sync.reconcile_safely(owner_id)
    return {"sync": {key: result.get(key, 0) for key in ("created", "updated", "deleted", "unchanged", "errors")}
            | {"status": result["status"]}, **google_calendar_sync.status(owner_id)}


@router.get("/busy")
def google_calendar_busy(start: str, utc_offset_minutes: int, days: int = 7,
                         current_user: dict = Depends(require_current_user)) -> dict:
    """Read-only busy blocks for the Planner calendar ("Busy" only: never event names)."""
    offset = timedelta(minutes=_offset_minutes(utc_offset_minutes))
    try:
        first = date.fromisoformat(start)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="start must be an ISO date (YYYY-MM-DD).") from error
    if not 1 <= days <= 42:
        raise HTTPException(status_code=400, detail="days must be between 1 and 42.")
    local_start = datetime.combine(first, time())
    found = google_calendar_sync.busy_intervals(current_user["id"], local_start, local_start + timedelta(days=days), offset)
    return {"busy": [{"start": s, "end": e} for s, e in found["intervals"]], "warning": found["warning"]}
