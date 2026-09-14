"""
The in-process loop that delivers queued webhooks and runs daily jobs.

Deliberately simple: no Redis, no Celery, no BullMQ. Those need infrastructure
that free tiers do not give you, and the work here is a table poll.

This loop dies with its process. That is exactly why /internal/tasks/run exists
and why GitHub Actions hits it on a schedule - a queue stuck because a worker
died at 3am should not be discovered by a customer.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from app.core.config import settings
from app.core.database import session as new_session
from app.services import subscription_service, wallet_service, webhook_service

log = logging.getLogger("dygine.dispatcher")

_last_daily: date | None = None


async def loop() -> None:
    global _last_daily
    log.info("dispatcher started, interval=%ss", settings.DISPATCHER_INTERVAL_SECONDS)

    while True:
        try:
            db = new_session()
            try:
                result = webhook_service.drain(db, limit=20)
                if result["delivered"] or result["failed"]:
                    log.info("webhooks delivered=%s failed=%s",
                             result["delivered"], result["failed"])
            finally:
                db.close()

            today = date.today()
            if _last_daily != today:
                db = new_session()
                try:
                    moved = subscription_service.run_lifecycle(db)
                    drift = wallet_service.reconcile(db)
                    if drift:
                        # Loud on purpose. A wallet whose stored balance does not
                        # match its ledger means money is being tracked wrong.
                        log.error("WALLET DRIFT on %s wallet(s): %s",
                                  len(drift), drift)
                    log.info("daily jobs: %s", moved)
                    _last_daily = today
                finally:
                    db.close()

        except Exception:                        # noqa: BLE001
            # Never let one bad cycle kill the loop - the next one may succeed,
            # and a dead dispatcher is a silent outage.
            log.exception("dispatcher cycle failed")

        await asyncio.sleep(settings.DISPATCHER_INTERVAL_SECONDS)
