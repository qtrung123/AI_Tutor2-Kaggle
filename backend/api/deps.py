"""Shared FastAPI auth dependencies for the API routers."""
from fastapi import Depends, HTTPException, Request

from backend.auth_store import get_user_for_session, is_admin_email
from config import AUTH_COOKIE_NAME


def require_current_user(request: Request) -> dict:
    user = get_user_for_session(request.cookies.get(AUTH_COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin_user(current_user: dict = Depends(require_current_user)) -> dict:
    if not is_admin_email(current_user["email"]):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user
