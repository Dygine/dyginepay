# Going live

You asked me to "convert everything to production mode". I have made the code
production-ready. I have **not** flipped any switch that moves real money —
that depends on your Razorpay KYC and your decision, not on the code, and it is
not something an assistant should do on your behalf.

Work down this list. Nothing before step 4 costs anything.

---

## 1. Move both services off the free plan

Render → each service → Settings → Instance Type → **Starter, $7/mo**.

Not optional once a real PG owner touches this. Free sleeps after 15 minutes and
takes ~50 seconds to wake. You have already hit it: the 502 on Test connection
was Dygine asleep. In production it means an owner clicking Pay gets a timeout,
and a Razorpay webhook arriving at a sleeping service times out too.

Both retry, so no money is lost. But the payment sits unconfirmed and the
customer contacts you.

**Set a spend limit** under Billing at the same time. $20/month is plenty and
caps the worst case.

## 2. Add the custom domain

`pay.dygine.com` → Dygine Pay. $0.25/mo since you have used your two free ones.

Then update, in this order:
1. Render → Dygine Pay → `BASE_URL` = `https://pay.dygine.com`
2. Razorpay → Webhooks → URL = `https://pay.dygine.com/webhooks/razorpay`
3. PGuru → master admin → Settings → Dygine Pay URL

Skip this if you would rather stay on `onrender.com`. Nothing breaks either way.

## 3. Turn the scheduled jobs on

The daily sweep now does five things: auto-debit renewals, warn on low wallet
balance, release expired coupon holds, **email invoices**, and expire lapsed
subscriptions. None of it runs unless something calls it.

Dygine already has the GitHub Actions cron. PGuru needs the same — point a
workflow at its sweep endpoint, or run `python expire_subscriptions.py` daily.

**Until you do this, no invoice emails go out and no renewal is ever taken
automatically.**

## 4. Complete Razorpay KYC

Dashboard → Account & Settings. PAN, Aadhaar, bank proof, website.

The name on your PAN, your bank account and your Razorpay profile must match
exactly. Roughly 40% of activation delays are a name mismatch.

Your website needs the policy pages live — Dygine Pay already serves them at
`/legal/terms`, `/privacy`, `/refund`, `/delivery`, `/pricing`, `/contact`.
**Read them.** They describe what you actually do, and the refund window in
particular is a commitment you are making.

## 5. Switch to live keys

Only after step 4 is approved.

1. Razorpay → Settings → API Keys → **Generate Live Key**
2. Render → Dygine Pay:
   - `RAZORPAY_KEY_ID` = `rzp_live_…`
   - `RAZORPAY_KEY_SECRET` = the new secret
   - `RAZORPAY_MODE` = `live`
3. **Add a live-mode webhook in Razorpay.** Test and live webhooks are separate.
   Forgetting this is the single most common go-live mistake: payments succeed
   and nothing is ever confirmed.
4. Dygine admin → Products & keys → issue a **live** key for PGuru
5. PGuru → master admin → Settings → paste the new key id and secret → **Test
   connection**

## 6. Prove it with your own money

Create a plan at ₹1. Pay it from a real PG owner account. Then:

- Dygine → Payments shows `captured` with a real fee
- Dygine → Invoices has the invoice, PDF opens
- Dygine → Webhooks → Outbound shows `delivered`
- PGuru → My subscription shows the renewal moved
- Ten minutes later, the invoice email arrives with the PDF attached
- Refund it from Dygine admin and check the credit note appears

If any step fails, stop. Do not tell a customer to pay until all six pass.

---

## Before any of this

**Rotate the Neon password.** You pasted it into a chat earlier in this session.
Neon console → Reset password → update `DATABASE_URL` in Render.

**Revoke the old Dygine test keys.** `dgn_test_8MOGbiL2dvAGWcOM` and
`dgn_test_BAEavLU1y84sxjYR` are both dead but still active. One of their secrets
went through a URL query string before that bug was fixed.

---

## What "production ready" means here, honestly

The code is. It has row locks where money moves, idempotency on every create,
HMAC verification on both webhook directions, secrets encrypted at rest with no
read path, tenant isolation tested across organisations, and a startup guard
that refuses to boot with development secrets.

What it has not had is **traffic**. One test payment is not load. Watch the
first ten real ones individually — Dygine → Payments, Webhooks → Outbound, and
the owner's history screen — before you trust it unattended.
