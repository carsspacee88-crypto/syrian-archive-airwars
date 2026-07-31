from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash

from .config import Settings

password_hash = PasswordHash.recommended()


def verify_admin(settings: Settings, username: str, password: str) -> bool:
    if not hmac.compare_digest(username.strip(), settings.admin_username):
        return False
    try:
        return password_hash.verify(password, settings.resolved_admin_password_hash)
    except (ValueError, TypeError):
        return False


def require_admin(request: Request) -> None:
    if request.session.get("admin") is not True:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"}
        )


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


def require_csrf(request: Request, supplied: str) -> None:
    expected = str(request.session.get("csrf") or "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="رمز الحماية غير صالح")
