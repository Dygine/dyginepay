"""
Response headers, and the one place a CSP here actually earns its keep.

Dygine serves its own HTML - the admin, the legal pages and, most importantly,
the checkout page - so unlike a pure JSON API this app can and should say
exactly what a browser is allowed to load.

**The checkout page is the reason this file exists.** `/c/{token}` is where a
customer types a card number. Two attacks matter there and both are stopped by
headers rather than by anything in the request handler:

  framing        an invisible iframe over the Pay button, or the whole page
                 framed inside an attacker's site that looks like ours
  script origin  anything injected into that page that is not Razorpay's own
                 checkout bundle

So frame-ancestors is 'none' everywhere, and the checkout CSP names Razorpay's
hosts and nothing else.

The admin gets its own, slightly looser policy because its templates use inline
styles. `unsafe-inline` for styles is a real weakening and is stated here rather
than hidden: it permits CSS injection, not script injection, and the templates
render no user-controlled markup. Removing it means hashing every style block,
which is worth doing later and is not worth blocking a launch on.

Nothing here is applied to `/v1` responses beyond `default-src 'none'`: an API
response has no legitimate reason to load anything at all.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

BASE_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # A payment URL carries a session token in its path. Send no referrer at
    # all from these pages - "strict-origin" would still leak the host to
    # Razorpay, and there is nothing here that needs a referrer to work.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

HSTS = "max-age=31536000; includeSubDomains"

#: Razorpay Checkout loads its bundle from checkout.razorpay.com and talks to
#: api.razorpay.com and lumberjack.razorpay.com. Its own script injects an
#: iframe, which is why frame-src is present while frame-ancestors stays none -
#: we may embed Razorpay, nobody may embed us.
CHECKOUT_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://checkout.razorpay.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://*.razorpay.com",
    "connect-src 'self' https://*.razorpay.com",
    "frame-src https://*.razorpay.com https://api.razorpay.com",
    "form-action 'self' https://*.razorpay.com",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

ADMIN_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _policy_for(path: str) -> str:
    if path.startswith("/c/"):
        return CHECKOUT_CSP
    if path.startswith("/admin") or path.startswith("/legal"):
        return ADMIN_CSP
    return API_CSP


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the headers above, never overwriting one a route already set."""

    def __init__(self, app, *, hsts: bool = True):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        for name, value in BASE_HEADERS.items():
            response.headers.setdefault(name, value)

        # Behind Render the real scheme is in X-Forwarded-Proto; the request's
        # own scheme reads "http" there and would suppress HSTS in production.
        forwarded = request.headers.get("x-forwarded-proto", "")
        secure = forwarded.split(",")[0].strip() == "https" or request.url.scheme == "https"
        if self.hsts and secure:
            response.headers.setdefault("Strict-Transport-Security", HSTS)

        response.headers.setdefault(
            "Content-Security-Policy", _policy_for(request.url.path))
        return response
