"""Admin session handling. Signed cookie, no JWT, no refresh dance."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import AuthError
from app.models import AdminUser


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="dygine-admin")


def issue_cookie(response, user: AdminUser) -> None:
    token = _serializer().dumps({"uid": str(user.id), "email": user.email})
    response.set_cookie(
        settings.SESSION_COOKIE_NAME, token,
        max_age=settings.SESSION_HOURS * 3600,
        httponly=True,                       # script cannot read it
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",                      # survives the Razorpay redirect back
        path="/")


def clear_cookie(response) -> None:
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")


def current_user(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    raw = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not raw:
        raise AuthError("Sign in to continue")
    try:
        data = _serializer().loads(raw, max_age=settings.SESSION_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        raise AuthError("Your session has expired") from None

    user = db.get(AdminUser, uuid.UUID(data["uid"]))
    if user is None or not user.is_active:
        raise AuthError("This account is no longer active")
    return user
