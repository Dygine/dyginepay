"""
Settings, and the startup guard that refuses to run an unsafe production process.

Every value here can come from the environment. The ones marked PRODUCTION are
checked by assert_production_safe(), which raises at import time rather than
letting a service boot with a development secret and take real money.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- application ---
    PROJECT_NAME: str = "Dygine Pay"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    BASE_URL: str = "http://localhost:8000"

    # --- database ---
    DATABASE_URL: str = "postgresql+psycopg://dygine:dygine@localhost:5432/dygine"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 280          # Neon drops idle connections; recycle first
    DB_POOL_PRE_PING: bool = True       # and verify before handing one out
    DB_ECHO: bool = False

    # --- security ---
    # PRODUCTION: generate a real value.
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    SECRET_KEY: str = "CHANGE-ME-generate-a-real-secret-before-any-deployment"
    # Separate key for encrypting Razorpay secrets at rest. Rotating SECRET_KEY
    # must not make stored gateway credentials unreadable, so it is its own value.
    ENCRYPTION_KEY: str = "CHANGE-ME-generate-a-real-encryption-key-32-chars-min"

    SESSION_COOKIE_NAME: str = "dygine_admin"
    SESSION_HOURS: int = 12
    SESSION_COOKIE_SECURE: bool = False   # PRODUCTION: true

    # --- first admin, created by bootstrap.py ---
    ADMIN_EMAIL: str = "admin@dygine.com"
    ADMIN_PASSWORD: str = "admin1234"     # PRODUCTION: refused, must be changed

    # --- razorpay ---
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_MODE: str = "test"           # "test" | "live"

    # --- your business identity, printed on every invoice ---
    BUSINESS_NAME: str = "Dygine Software Solution"
    BUSINESS_ADDRESS: str = "Bengaluru, Karnataka, India"
    BUSINESS_EMAIL: str = "billing@dygine.com"
    BUSINESS_PHONE: str = ""
    BUSINESS_STATE_CODE: str = "29"       # Karnataka. Decides CGST/SGST vs IGST.
    # Empty means not GST registered: bills of supply, no tax charged.
    # Fill it in the day you register and tax invoices start immediately.
    BUSINESS_GSTIN: str = ""
    BUSINESS_PAN: str = ""
    DEFAULT_GST_RATE: int = 18            # percent
    DEFAULT_SAC: str = "997331"
    INVOICE_PREFIX: str = "DGN"

    # --- workers ---
    DISPATCHER_ENABLED: bool = True       # in-process outbound webhook loop
    DISPATCHER_INTERVAL_SECONDS: int = 10
    # Shared secret for /internal/tasks/run, called by GitHub Actions cron.
    INTERNAL_TASK_TOKEN: str = "CHANGE-ME-internal-task-token"

    # --- limits ---
    CHECKOUT_SESSION_MINUTES: int = 30
    IDEMPOTENCY_RETENTION_HOURS: int = 24
    RATE_LIMIT_PER_MINUTE: int = 120

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def gst_registered(self) -> bool:
        return bool(self.BUSINESS_GSTIN.strip())


def assert_production_safe(s: Settings) -> None:
    """Refuse to start a production process that is still carrying dev defaults."""
    if not s.is_production:
        return

    problems: list[str] = []

    def weak(value: str, name: str, minimum: int = 32) -> None:
        if not value or "CHANGE-ME" in value or len(value) < minimum:
            problems.append(f"{name} is unset, too short, or still the default")

    weak(s.SECRET_KEY, "SECRET_KEY")
    weak(s.ENCRYPTION_KEY, "ENCRYPTION_KEY")
    weak(s.INTERNAL_TASK_TOKEN, "INTERNAL_TASK_TOKEN", 16)

    if s.DEBUG:
        problems.append("DEBUG must be false in production")
    if not s.SESSION_COOKIE_SECURE:
        problems.append("SESSION_COOKIE_SECURE must be true in production")
    if s.ADMIN_PASSWORD == "admin1234":
        problems.append("ADMIN_PASSWORD is still the default")
    if not s.BASE_URL.startswith("https://"):
        problems.append("BASE_URL must be https in production")
    if not (s.RAZORPAY_KEY_ID and s.RAZORPAY_KEY_SECRET):
        problems.append("Razorpay credentials are not configured")
    if not s.RAZORPAY_WEBHOOK_SECRET:
        # Without this the webhook cannot be verified, and an unverified webhook
        # is an open endpoint that lets anyone mark a payment captured.
        problems.append("RAZORPAY_WEBHOOK_SECRET is not set")
    if s.RAZORPAY_MODE == "live" and s.RAZORPAY_KEY_ID.startswith("rzp_test_"):
        problems.append("RAZORPAY_MODE is live but the key id is a test key")

    if problems:
        raise RuntimeError(
            "Refusing to start in production:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    assert_production_safe(s)
    return s


settings = get_settings()
