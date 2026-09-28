"""Google Calendar API access for the Study Planner integration (server side only).

OAuth 2.0 authorization-code flow (offline access, narrow scopes), refresh-token encryption at
rest, access-token refresh (memory only), token revocation, and the few Calendar REST calls the
integration needs: the dedicated "AI Tutor Study Plan" calendar, freeBusy on the primary calendar,
and create/update/delete of the events this app created. Plain httpx with explicit token handling;
tests swap `_transport` for an httpx.MockTransport, so no real Google call is ever made.

Tokens are never logged, returned to a client or written unencrypted.
"""

import base64
import hashlib
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from backend import google_calendar_store
from config import google_calendar_settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

SCOPE_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
SCOPE_APP_CREATED = "https://www.googleapis.com/auth/calendar.app.created"
SCOPES = (SCOPE_FREEBUSY, SCOPE_APP_CREATED)

STUDY_CALENDAR_NAME = "AI Tutor Study Plan"
STATE_TTL = timedelta(minutes=10)
HTTP_TIMEOUT_SECONDS = 8.0

# Test seam: an httpx transport (e.g. httpx.MockTransport). None = the real network.
_transport: httpx.BaseTransport | None = None

# owner_id -> (access_token, expires_at monotonic seconds). Memory only, never persisted.
_access_tokens: dict[str, tuple[str, float]] = {}
_access_lock = threading.Lock()


class GoogleCalendarConfigError(RuntimeError):
    """The server is missing Google OAuth configuration (names the missing variables)."""


class GoogleCalendarError(RuntimeError):
    """A Google call failed. `code` is machine-readable; the message never contains a token."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class GoogleReconnectRequired(GoogleCalendarError):
    """The refresh token was revoked/expired (invalid_grant): the user must connect again."""

    def __init__(self):
        super().__init__("reconnect_required", "Google Calendar access was revoked. Reconnect to continue.")


# -- configuration and encryption -----------------------------------------------

def settings(require: bool = True) -> dict:
    values = google_calendar_settings()
    if require and values["missing"]:
        raise GoogleCalendarConfigError(
            "Google Calendar is not configured on this server. Missing: " + ", ".join(values["missing"]) + "."
        )
    return values


def is_configured() -> bool:
    return not google_calendar_settings()["missing"]


def _fernet() -> Fernet:
    """GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY: a Fernet key (recommended) or any secret passphrase,
    which is stretched to a key with SHA-256."""
    key = settings()["GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY"]
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest()))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        # Wrong/rotated key: the stored token is unusable, the user has to connect again.
        raise GoogleReconnectRequired() from error


# -- HTTP -----------------------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=HTTP_TIMEOUT_SECONDS)


def _send(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        with _client() as client:
            return client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        # The exception text can include the URL but never a token (tokens go in headers/bodies).
        raise GoogleCalendarError("unavailable", f"Google Calendar could not be reached ({type(error).__name__}).") from error


def _error_from(response: httpx.Response, action: str) -> GoogleCalendarError:
    return GoogleCalendarError("google_error", f"Google Calendar {action} failed (HTTP {response.status_code}).",
                               response.status_code)


# -- OAuth ----------------------------------------------------------------------

def build_authorization_url(owner_id: str) -> str:
    """A one-time, 10-minute `state` bound to this app user, and the Google consent URL."""
    config = settings()
    state = secrets.token_urlsafe(32)
    google_calendar_store.create_oauth_state(owner_id, state, STATE_TTL)
    query = {
        "client_id": config["GOOGLE_CLIENT_ID"],
        "redirect_uri": config["GOOGLE_CALENDAR_REDIRECT_URI"],
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # Always show consent so Google returns a refresh token on a reconnect too.
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(query)}"


def exchange_code(code: str) -> dict:
    """{refresh_token, access_token, expires_in, scope} for an authorization code."""
    config = settings()
    response = _send("POST", TOKEN_ENDPOINT, data={
        "grant_type": "authorization_code", "code": code,
        "client_id": config["GOOGLE_CLIENT_ID"], "client_secret": config["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": config["GOOGLE_CALENDAR_REDIRECT_URI"],
    })
    if response.status_code != 200:
        raise _error_from(response, "authorization")
    return response.json()


def remember_access_token(owner_id: str, access_token: str, expires_in) -> None:
    with _access_lock:
        _access_tokens[owner_id] = (access_token, time.monotonic() + max(0, int(expires_in or 0) - 60))


def forget_access_token(owner_id: str) -> None:
    with _access_lock:
        _access_tokens.pop(owner_id, None)


def access_token(owner_id: str) -> str:
    """A valid access token for the owner's connection: cached in memory, else refreshed from the
    stored (encrypted) refresh token. A revoked grant marks the connection reconnect_required."""
    with _access_lock:
        cached = _access_tokens.get(owner_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    connection = google_calendar_store.get_connection(owner_id)
    if not connection or connection["status"] != "connected":
        raise GoogleReconnectRequired()
    config = settings()
    try:
        refresh_token = decrypt_token(connection["encrypted_refresh_token"])
    except GoogleReconnectRequired:
        google_calendar_store.update_connection(owner_id, {"status": "reconnect_required"})
        raise
    response = _send("POST", TOKEN_ENDPOINT, data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": config["GOOGLE_CLIENT_ID"], "client_secret": config["GOOGLE_CLIENT_SECRET"],
    })
    if response.status_code in (400, 401) and _oauth_error(response) in ("invalid_grant", "unauthorized_client"):
        google_calendar_store.update_connection(owner_id, {"status": "reconnect_required"})
        forget_access_token(owner_id)
        raise GoogleReconnectRequired()
    if response.status_code != 200:
        raise _error_from(response, "token refresh")
    payload = response.json()
    remember_access_token(owner_id, payload["access_token"], payload.get("expires_in"))
    return payload["access_token"]


def _oauth_error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", ""))
    except ValueError:
        return ""


def revoke_token(token: str) -> bool:
    """Best effort: True when Google confirmed the revocation."""
    try:
        response = _send("POST", REVOKE_ENDPOINT, data={"token": token})
    except GoogleCalendarError:
        return False
    return response.status_code == 200


# -- Calendar API -----------------------------------------------------------------

def _api(owner_id: str, method: str, path: str, *, json: dict | None = None, retry_auth: bool = True) -> httpx.Response:
    token = access_token(owner_id)
    response = _send(method, f"{CALENDAR_API}{path}", json=json, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 401 and retry_auth:
        # The cached access token expired early or was revoked: refresh once, then give up.
        forget_access_token(owner_id)
        return _api(owner_id, method, path, json=json, retry_auth=False)
    return response


def _quote(value: str) -> str:
    return quote(value, safe="")


def calendar_exists(owner_id: str, calendar_id: str) -> bool:
    response = _api(owner_id, "GET", f"/calendars/{_quote(calendar_id)}")
    if response.status_code == 200:
        return True
    if response.status_code in (403, 404, 410):
        return False
    raise _error_from(response, "calendar lookup")


def create_study_calendar(owner_id: str) -> dict:
    response = _api(owner_id, "POST", "/calendars", json={
        "summary": STUDY_CALENDAR_NAME,
        "description": "Study sessions confirmed in the AI Tutor Study Planner.",
    })
    if response.status_code != 200:
        raise _error_from(response, "calendar creation")
    payload = response.json()
    return {"id": payload["id"], "summary": payload.get("summary") or STUDY_CALENDAR_NAME}


def query_busy(owner_id: str, time_min_utc: str, time_max_utc: str) -> list[tuple[str, str]]:
    """Busy (start, end) RFC 3339 strings on the user's PRIMARY calendar. Only times -- the freebusy
    scope never exposes event titles or descriptions."""
    response = _api(owner_id, "POST", "/freeBusy", json={
        "timeMin": time_min_utc, "timeMax": time_max_utc, "items": [{"id": "primary"}],
    })
    if response.status_code != 200:
        raise _error_from(response, "free/busy query")
    primary = response.json().get("calendars", {}).get("primary", {})
    if primary.get("errors"):
        raise GoogleCalendarError("google_error", "Google Calendar could not read your busy times.")
    return [(item["start"], item["end"]) for item in primary.get("busy", [])]


def insert_event(owner_id: str, calendar_id: str, event_id: str, body: dict) -> str:
    """Create the event with our deterministic id. If that id already exists (e.g. a previous
    attempt succeeded but its link was not saved, or it was deleted) it is updated/restored instead:
    a repeated sync can never create a duplicate."""
    response = _api(owner_id, "POST", f"/calendars/{_quote(calendar_id)}/events", json={**body, "id": event_id})
    if response.status_code in (200, 201):
        return response.json().get("id", event_id)
    if response.status_code == 409:
        return update_event(owner_id, calendar_id, event_id, body, create_missing=False)
    raise _error_from(response, "event creation")


def update_event(owner_id: str, calendar_id: str, event_id: str, body: dict, create_missing: bool = True) -> str:
    response = _api(owner_id, "PUT", f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}",
                    json={**body, "status": "confirmed"})
    if response.status_code == 200:
        return event_id
    if response.status_code in (404, 410) and create_missing:
        return insert_event(owner_id, calendar_id, event_id, body)
    raise _error_from(response, "event update")


def delete_event(owner_id: str, calendar_id: str, event_id: str) -> None:
    response = _api(owner_id, "DELETE", f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}")
    if response.status_code in (200, 204, 404, 410):   # already gone is fine
        return
    raise _error_from(response, "event deletion")
