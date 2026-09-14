# Checkout now shows the product, not the company

## What changed

The Razorpay modal and the hosted page both showed **Dygine Software Solution**,
which is the company that operates the platform. A PG owner has never heard of
it. They know **PGuru**.

Both now use the product name from Dygine's own Products table, so the same code
shows "PGuru" to a PGuru customer and "HRLens" to an HRLens customer with no
change when you add the next tool.

## One thing I did not do, on purpose

I did not remove the company name entirely.

Whatever the modal says, the customer's **card or bank statement carries the
Razorpay account holder** — Dygine Software Solution. That descriptor comes from
your Razorpay account, not from anything the checkout page can set. A charge on
a statement that the customer does not recognise is a charge they dispute, and a
chargeback costs you the payment plus a fee plus the argument.

So the page now says, under the Pay button:

> Payments collected by Dygine Software Solution · this will appear on your
> statement

Big name they recognise at the top, small line telling them what they will see
later. That is the pairing that prevents the support ticket.

## Two files

```
app/api/checkout_pages.py              passes the product to the template
app/templates/checkout/pay.html        product name in the header and the modal
```

Copy over your **dygine-pay** repo (not pgdesk), then:

```powershell
git add .
git commit -m "checkout shows the product name"
git push origin main
```

No migration. 72 tests still pass.

## Worth considering later

Razorpay Checkout also takes an `image` option for a logo, which would replace
the grey "D" letter avatar in the modal. That needs a `logo_url` column on
Product and a migration, so I have left it out rather than bundle a schema
change into a display fix. Say the word if you want it.
