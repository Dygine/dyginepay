# Dygine Pay

Central payments for Dygine products. One Razorpay account, many tools, one place
that knows what every customer paid, what invoice was raised, and which product
actually makes money.

```
pgdesk ─┐
hrlens ─┼──► Dygine Pay ──► Razorpay ──► your bank
others ─┘        │
                 ├── invoices (GST, sequential, PDF)
                 ├── wallets (closed loop, row-locked)
                 ├── subscriptions
                 └── margin per product (real gateway fees)
```

---

## The boundary

Two money flows that must never touch:

| Flow | Payer | Receiver | Razorpay account |
|---|---|---|---|
| Rent, orders, whatever your tool's users pay each other | end user | your customer | **their own keys** |
| Subscriptions and wallet top-ups | your customer | **you** | **Dygine's keys** |

Dygine Pay only ever handles the second. Routing other people's money through
your merchant account is payment aggregation and needs an RBI licence with a
₹15 crore net worth requirement. This is not a design preference.

---

## How a tool integrates

One key pair lives in your tool's **master admin**, not in each customer's
settings. Each customer becomes a record in Dygine keyed by whatever id your tool
already uses.

Four things to implement, and nothing else:

**1. Create a session, server-side**

```python
from dygine_pay import DyginePay

pay = DyginePay(key_id=KEY_ID, key_secret=KEY_SECRET)
session = pay.create_checkout(
    customer={"external_id": str(org.id), "name": org.name,
              "email": org.email, "state_code": "29"},
    purpose="subscription", plan_code="pro",
    success_url="https://app.pgdesk.in/billing/done",
    idempotency_key=f"sub-{org.id}-{period}")
```

**2. Redirect** to `session["checkout_url"]`.

**3. Receive one webhook**

```python
from dygine_pay import verify_webhook

@app.post("/api/dygine/webhook")
async def hook(request: Request):
    raw = await request.body()          # RAW bytes, never a re-dumped copy
    if not verify_webhook(SECRET, raw, request.headers.get("X-Dygine-Signature", "")):
        raise HTTPException(403)

    event = json.loads(raw)
    if event["event"] == "payment.captured":
        activate(event["data"])         # must be idempotent
    return {"ok": True}
```

**4. Poll on boot** for anything you missed:

```python
payment = pay.get_payment(reference)    # always safe
```

That fourth step is what saves you when your server was down during a webhook.

PHP tools use `clients/php/DyginePay.php` — same four steps, no dependencies.

---

## What it handles that you would otherwise get wrong

**The callback/webhook race.** Razorpay tells you a payment succeeded twice: once
through the browser, once server to server. Both verify a signature, both call
the same capture function, and the second finds it already captured and does
nothing. That is why a customer whose phone died after paying still gets their
subscription.

**Invoice numbering.** GST requires a consecutive series per financial year with
no gaps. The number is taken under a row lock inside the same transaction that
writes the invoice. `count(*) + 1` hands the same number to concurrent captures
and the unique constraint then discards a real payment's invoice. There is a test
that issues twelve invoices simultaneously and asserts they are numbered 1 to 12.

**Wallet overdraw.** `if balance >= amount: balance -= amount` lets ten
concurrent debits all pass the check. Every debit takes `SELECT … FOR UPDATE`
first, with a CHECK constraint as backstop. Tested with real threads: ten
concurrent ₹20 debits against a ₹100 balance, exactly five succeed.

**Real gateway fees.** The fee is read off the Razorpay payment object at
capture, never estimated at 2%. RuPay credit via UPI is 2.15%, international
cards are 3%, and during your zero-platform-fee window it is 0 — an estimate
shows a loss that is not there.

**Money as integers.** Paise, everywhere. No floats, no Decimal.

---

## GST

Driven entirely by `BUSINESS_GSTIN`:

- **empty** → bills of supply, no tax charged. Correct below the ₹20 lakh
  services threshold.
- **set** → tax invoices. CGST+SGST when the customer is in Karnataka, IGST
  otherwise, decided per customer from their state code.

Set it the day you register. No code change, no migration, no redeploy of
anything but the env var.

---

## Wallet compliance

The balance is a **closed system prepaid instrument**, which RBI exempts from PPI
authorisation. Three things must stay impossible:

- withdrawing balance as cash
- transferring balance between customers
- refunding anywhere except back through the original payment

There is no API that does any of them. Keep it that way — the moment one becomes
possible this is a semi-closed PPI and needs an RBI licence.

**Tax on top-ups needs your CA.** Advances received for *services* are taxable at
receipt; the advance exemption covers goods only. The code issues a tax invoice
at top-up and treats consumption as a pure ledger debit, which is the simpler
correct reading — but confirm it before you take real top-ups.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill DATABASE_URL at minimum
alembic upgrade head
python bootstrap.py --demo    # admin + a demo product with test keys
uvicorn app.main:app --reload
```

- admin — http://localhost:8000/admin
- API docs — http://localhost:8000/v1/docs (debug only)
- health — http://localhost:8000/health

```bash
pytest -q                     # 63 tests, needs a real Postgres
```

The concurrency tests use real threads against real Postgres deliberately. What
they test *is* the database's locking behaviour, so a mock would prove nothing.

---

## Layout

```
app/
  core/        config, money, crypto, database, errors
  models/      21 tables
  services/    gst, razorpay, checkout, invoice, wallet,
               subscription, refund, webhook, reporting, pdf
  api/
    v1/        the API your tools call
    admin/     server-rendered dashboard
    internal/  cron-triggered tasks
    webhooks   inbound from Razorpay
  templates/   admin, checkout, legal
  workers/     outbound webhook dispatcher
clients/
  python/      pip-installable client
  php/         single-file client
tests/         63 tests
```

Deployment: **DEPLOY.md**.
