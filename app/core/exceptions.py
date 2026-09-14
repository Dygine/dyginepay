"""Application errors, each carrying the HTTP status it should become."""
from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_failed"


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class InsufficientBalanceError(AppError):
    status_code = 409
    code = "insufficient_balance"


class UpstreamError(AppError):
    """Razorpay said no, or could not be reached."""
    status_code = 502
    code = "gateway_error"


class SignatureError(AuthError):
    code = "invalid_signature"
