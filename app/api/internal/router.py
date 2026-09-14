"""
Scheduled work, triggered by GitHub Actions cron.

The in-process dispatcher does the same jobs every few seconds. This endpoint
exists because that loop dies with its process, and a webhook stuck in the queue
because a worker died at 3am is not something you want to find out about from a
customer. Both paths are idempotent, so running both is harmless.
"""
from __future__ import annotations

from fastapi import APIRouter, Header
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import tokens_equal
from app.core.database import get_db, session as new_session
from app.core.exceptions import AuthError
from app.services import subscription_service, wallet_service, webhook_service

router = APIRouter(prefix="/internal", tags=["internal"])


def _authorise(token: str | None) -> None:
    if not token or not tokens_equal(token, settings.INTERNAL_TASK_TOKEN):
        raise AuthError("Invalid internal task token")


@router.post("/tasks/run")
def run_tasks(x_internal_token: str | None = Header(default=None),
              tasks: str = "webhooks,subscriptions,reconcile"):
    _authorise(x_internal_token)
    wanted = {t.strip() for t in tasks.split(",") if t.strip()}
    result: dict = {}

    if "webhooks" in wanted:
        db: Session = new_session()
        try:
            result["webhooks"] = webhook_service.drain(db, limit=50)
        finally:
            db.close()

    if "subscriptions" in wanted:
        db = new_session()
        try:
            result["subscriptions"] = subscription_service.run_lifecycle(db)
        finally:
            db.close()

    if "reconcile" in wanted:
        db = new_session()
        try:
            drift = wallet_service.reconcile(db)
            result["reconcile"] = {"wallets_checked": "all", "drift": drift}
        finally:
            db.close()

    return {"ok": True, "result": result}
