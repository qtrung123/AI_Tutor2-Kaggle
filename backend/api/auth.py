"""Auth routes: signup, login, logout and the current user."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.api.deps import require_current_user
from backend.auth_store import authenticate_user, create_session, create_user, is_admin_email, revoke_session
from config import AUTH_COOKIE_NAME, AUTH_COOKIE_SECURE, AUTH_SESSION_DAYS

router = APIRouter()


class SignupRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def _with_admin_flag(user: dict) -> dict:
    """Attach the (env-configured, never stored) admin flag to a public user payload."""
    return {**user, "is_admin": is_admin_email(user["email"])}


def _validate_email(email: str) -> str:
    import re
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return normalized


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


@router.post("/api/auth/signup", status_code=201)
def auth_signup(request: SignupRequest, response: Response) -> dict:
    display_name = request.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Display name cannot be blank.")
    try:
        user = create_user(display_name, _validate_email(request.email), request.password)
        token, _session = create_session(user["id"])
        _set_session_cookie(response, token)
        return _with_admin_flag(user)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/auth/login")
def auth_login(request: LoginRequest, response: Response) -> dict:
    user = authenticate_user(_validate_email(request.email), request.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token, _session = create_session(user["id"])
    _set_session_cookie(response, token)
    return _with_admin_flag(user)


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    revoke_session(request.cookies.get(AUTH_COOKIE_NAME))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=AUTH_COOKIE_SECURE, samesite="lax")
    return {"signed_out": True}


@router.get("/api/auth/me")
def auth_me(current_user: dict = Depends(require_current_user)) -> dict:
    return _with_admin_flag(current_user)
