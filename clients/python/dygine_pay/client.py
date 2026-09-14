"""
Dygine Pay client.

Everything a consuming tool needs, in one file with one dependency (httpx).

    pay = DyginePay(key_id=..., key_secret=..., base_url="https://pay.dygine.com")

    session = pay.create_checkout(
        customer={"external_id": str(org.id), "name": org.name,
                  "email": org.email, "state_code": "29"},
        purpose="subscription", plan_code="pro",
        success_url="https://app.pgdesk.in/billing/done",
        idempotency_key=f"sub-{org.id}-{period}")

    return redirect(session["checkout_url"])
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import httpx


class DyginePayError(Exception):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class DyginePay:
    def __init__(self, key_id: str, key_secret: str,
                 base_url: str = "https://pay.dygine.com", timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()

    # ------------------------------------------------------------ plumbing --
    def _call(self, method: str, path: str, body: dict | None = None,
              idempotency_key: str | None = None) -> dict:
        headers = {"Authorization": f"Basic {self._auth}",
                   "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.request(method, f"{self.base_url}{path}",
                                     json=body, headers=headers)
        except httpx.RequestError as exc:
            raise DyginePayError(f"Could not reach Dygine Pay: {exc}") from None

        if res.status_code >= 400:
            try:
                err = res.json().get("error", {})
            except ValueError:
                err = {}
            raise DyginePayError(err.get("message") or res.text,
                                 status=res.status_code,
                                 code=err.get("code", ""))
        return res.json() if res.content else {}

    # ------------------------------------------------------------ checkout --
    def create_checkout(self, *, customer: dict, line_items: list[dict] | None = None,
                        purpose: str = "one_off", plan_code: str | None = None,
                        success_url: str | None = None, cancel_url: str | None = None,
                        notes: dict | None = None,
                        idempotency_key: str | None = None) -> dict:
        """
        Create a payment and get a URL to redirect the customer to.

        Always pass an idempotency_key that is stable for the attempt. Without
        one, a double-clicked button creates two orders and can charge twice.
        One is generated if you omit it, which protects against a network retry
        but not against a user clicking twice.
        """
        body: dict = {"customer": customer, "purpose": purpose}
        if line_items:
            body["line_items"] = line_items
        if plan_code:
            body["plan_code"] = plan_code
        if success_url:
            body["success_url"] = success_url
        if cancel_url:
            body["cancel_url"] = cancel_url
        if notes:
            body["notes"] = notes
        return self._call("POST", "/v1/checkout/sessions", body,
                          idempotency_key or f"auto-{uuid.uuid4()}")

    # ------------------------------------------------------------ payments --
    def get_payment(self, reference: str) -> dict:
        """
        Safe to call any time. This is your self-heal path: poll it for anything
        you did not receive a webhook for.
        """
        return self._call("GET", f"/v1/payments/{reference}")

    def list_payments(self, customer_external_id: str | None = None,
                      limit: int = 50) -> list[dict]:
        q = f"?limit={limit}"
        if customer_external_id:
            q += f"&customer_external_id={customer_external_id}"
        return self._call("GET", f"/v1/payments{q}")["data"]

    def refund(self, reference: str, amount_paise: int | None = None,
               reason: str = "") -> dict:
        body: dict = {"reason": reason}
        if amount_paise is not None:
            body["amount"] = amount_paise
        return self._call("POST", f"/v1/payments/{reference}/refund", body)

    # ----------------------------------------------------------- customers --
    def upsert_customer(self, **fields) -> dict:
        return self._call("POST", "/v1/customers", fields)

    def get_customer(self, external_id: str) -> dict:
        return self._call("GET", f"/v1/customers/{external_id}")

    # -------------------------------------------------------------- wallet --
    def get_wallet(self, customer_external_id: str) -> dict:
        return self._call("GET", f"/v1/customers/{customer_external_id}/wallet")

    def debit_wallet(self, customer_external_id: str, amount_paise: int, *,
                     description: str = "", reference_type: str | None = None,
                     reference_id: str | None = None,
                     idempotency_key: str | None = None) -> dict:
        """
        Consume balance. Raises DyginePayError with code 'insufficient_balance'
        when there is not enough - catch that specifically and tell the customer
        to top up rather than treating it as an outage.

        The idempotency_key should identify the thing being charged for, not the
        attempt: "sms-batch-4417", not a fresh uuid. A retry must not double charge.
        """
        return self._call("POST", "/v1/wallet/debit", {
            "customer_external_id": customer_external_id,
            "amount": amount_paise, "description": description,
            "reference_type": reference_type, "reference_id": reference_id,
        }, idempotency_key)

    # ------------------------------------------------------------ invoices --
    def get_invoice(self, invoice_id: str) -> dict:
        return self._call("GET", f"/v1/invoices/{invoice_id}")

    def invoice_pdf_url(self, invoice_id: str) -> str:
        return f"{self.base_url}/v1/invoices/{invoice_id}/pdf"

    def list_invoices(self, customer_external_id: str) -> list[dict]:
        return self._call(
            "GET", f"/v1/customers/{customer_external_id}/invoices")["data"]

    # ------------------------------------------------------- subscriptions --
    def get_subscription(self, reference: str) -> dict:
        return self._call("GET", f"/v1/subscriptions/{reference}")

    def list_subscriptions(self, customer_external_id: str) -> list[dict]:
        return self._call(
            "GET", f"/v1/customers/{customer_external_id}/subscriptions")["data"]

    def cancel_subscription(self, reference: str) -> dict:
        return self._call("POST", f"/v1/subscriptions/{reference}/cancel")

    def list_plans(self) -> list[dict]:
        return self._call("GET", "/v1/plans")["data"]


def verify_webhook(secret: str, raw_body: bytes, signature_header: str) -> bool:
    """
    Verify an inbound Dygine Pay webhook.

    Pass the RAW request body, not a parsed-and-re-dumped copy. Re-serialising
    changes the bytes and the signature will never match - and the tempting fix
    for that is to stop verifying, which leaves an endpoint anyone can forge.

        # Django
        if not verify_webhook(SECRET, request.body,
                              request.headers.get("X-Dygine-Signature", "")):
            return HttpResponseForbidden()

        # Flask / FastAPI
        raw = request.get_data()        # await request.body()
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}",
                               (signature_header or "").strip())
