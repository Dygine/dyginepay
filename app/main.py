"""
Dygine Pay.

One FastAPI app serving four things on one origin:

    /v1/*        the API your tools call
    /c/{token}   the page your customers pay on
    /webhooks/*  inbound from Razorpay
    /admin       your dashboard

Same origin on purpose. Splitting the admin onto a separate host buys nothing
here and costs CORS configuration and cross-site cookies, which is a class of
bug that only shows up in production.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.api import checkout_pages, legal, webhooks
from app.api.admin import router as admin_router
from app.api.internal import router as internal_router
from app.api.v1 import router as v1_router
from app.core.config import settings
from app.core.database import engine
from app.core.exceptions import AppError, AuthError
from app.workers.dispatcher import loop as dispatcher_loop

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
log = logging.getLogger("dygine")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("%s starting in %s mode", settings.PROJECT_NAME, settings.ENVIRONMENT)
    task = None
    if settings.DISPATCHER_ENABLED:
        task = asyncio.create_task(dispatcher_loop())
    yield
    if task:
        task.cancel()
    log.info("shutting down")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Central payments for Dygine products.",
    lifespan=lifespan,
    docs_url="/v1/docs" if settings.DEBUG else None,
    redoc_url=None,
)

app.include_router(v1_router.router)
app.include_router(admin_router.router)
app.include_router(internal_router.router)
app.include_router(webhooks.router)
app.include_router(checkout_pages.router)
app.include_router(legal.router)


# ------------------------------------------------------- error handling --
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    # An admin page hitting an auth error wants a login screen, not JSON.
    if isinstance(exc, AuthError) and request.url.path.startswith("/admin"):
        return RedirectResponse("/admin/login", status_code=303)
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": exc.code,
                                           "message": exc.message,
                                           **({"detail": exc.detail}
                                              if exc.detail else {})}})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    # Never leak an internal message to a caller in production. The detail is in
    # the log, where it belongs.
    message = str(exc) if settings.DEBUG else "Something went wrong on our side."
    return JSONResponse(status_code=500,
                        content={"error": {"code": "internal_error",
                                           "message": message}})


# --------------------------------------------------------------- health --
@app.get("/health", tags=["meta"])
def health():
    """Render pings this. It checks the database, because a process that is up
    but cannot reach Neon is not actually healthy."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:                            # noqa: BLE001
        db_ok = False
        log.exception("health check: database unreachable")

    status = 200 if db_ok else 503
    return JSONResponse(status_code=status, content={
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "environment": settings.ENVIRONMENT,
        "gst_registered": settings.gst_registered,
    })


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")
