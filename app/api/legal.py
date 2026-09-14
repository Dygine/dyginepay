"""
The policy pages Razorpay checks before approving your website.

Served from the app rather than written by hand somewhere else, because they
have to exist at a real URL on pay.dygine.com before the review passes, and a
page that lives in the codebase cannot be forgotten during a redeploy.

Read them. They describe what you actually do - change anything that does not
match, especially the refund window.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/legal", tags=["legal"])
templates = Jinja2Templates(directory="app/templates")

B = settings.BUSINESS_NAME

PAGES = {
    "terms": ("Terms and Conditions", f"""
<p>These terms govern your use of software products supplied by {B} and payments
made through this platform.</p>
<h2>Who you are contracting with</h2>
<p>{B}, {settings.BUSINESS_ADDRESS}. Contact {settings.BUSINESS_EMAIL}.</p>
<h2>What we supply</h2>
<p>Access to software on a subscription or prepaid-credit basis. Access begins
immediately on successful payment and continues for the period paid for.</p>
<h2>Payments</h2>
<ul>
<li>All prices are in Indian Rupees. Applicable taxes are shown at checkout.</li>
<li>Payments are processed by Razorpay. We never receive or store your card,
UPI or banking credentials.</li>
<li>A tax invoice or bill of supply is issued for every successful payment.</li>
</ul>
<h2>Prepaid credit</h2>
<p>Credit purchased on this platform can be used only for products supplied by
{B}. It cannot be withdrawn as cash, transferred to another account or another
customer, or redeemed anywhere else.</p>
<h2>Your responsibilities</h2>
<p>You are responsible for the accuracy of the billing details you give us,
including your GSTIN and state, which determine how tax is applied. You must not
use the service unlawfully or attempt to disrupt it.</p>
<h2>Suspension</h2>
<p>Access may be suspended where a subscription lapses beyond its grace period,
or where payment is reversed or disputed. Suspension does not delete your data.</p>
<h2>Liability</h2>
<p>To the extent permitted by law, our liability for any claim is limited to the
amount you paid us in the three months before the claim arose.</p>
<h2>Governing law</h2>
<p>These terms are governed by the laws of India. Courts at Bengaluru, Karnataka
have exclusive jurisdiction.</p>"""),

    "privacy": ("Privacy Policy", f"""
<p>This policy explains what {B} collects when you pay us and what we do with it.</p>
<h2>What we collect</h2>
<ul>
<li>Your name, email address and phone number.</li>
<li>Your billing address, GSTIN and state, where you provide them.</li>
<li>A record of each payment: amount, date, method and status.</li>
</ul>
<h2>What we do not collect</h2>
<p>We never receive your card number, CVV, UPI PIN or net banking credentials.
Those go directly to Razorpay, a payment gateway authorised by the Reserve Bank
of India. We only receive a token and the outcome.</p>
<h2>Why we hold it</h2>
<p>To process your payment, issue a legally compliant invoice, provide support,
and meet our tax and accounting obligations under Indian law.</p>
<h2>Who we share it with</h2>
<p>Razorpay, to process payments. Our accountant and the tax authorities, as
required by law. Nobody else. We do not sell your data and we do not use it for
advertising.</p>
<h2>How long we keep it</h2>
<p>Invoice and payment records are retained for at least eight years, as Indian
tax law requires. Other personal data is deleted on request where no legal
obligation requires us to keep it.</p>
<h2>Your rights</h2>
<p>Write to {settings.BUSINESS_EMAIL} to access, correct or delete the personal
data we hold about you.</p>"""),

    "refund": ("Refund and Cancellation Policy", f"""
<h2>Cancelling a subscription</h2>
<p>You may cancel at any time. Cancellation takes effect at the end of the period
you have already paid for; access is not withdrawn early and the unused part of
that period is not refunded.</p>
<h2>Refunds</h2>
<p>We refund in full where:</p>
<ul>
<li>you were charged twice for the same thing;</li>
<li>you were charged after cancelling;</li>
<li>the service was unavailable for a prolonged period through our fault.</li>
</ul>
<p>Requests must reach {settings.BUSINESS_EMAIL} within 7 days of the charge.</p>
<h2>How a refund is paid</h2>
<p>Refunds are returned through the original payment method only. We cannot pay a
refund to a different card, account or person. Once approved, the refund is
initiated within 3 business days and typically reaches your account within 5 to
7 business days depending on your bank.</p>
<h2>Prepaid credit</h2>
<p>Unused credit is refundable within 30 days of purchase, back through the
original payment, less any credit already consumed. Consumed credit is not
refundable.</p>
<h2>Credit notes</h2>
<p>Every refund is accompanied by a credit note against the original invoice.</p>"""),

    "delivery": ("Delivery Policy", f"""
<h2>What is delivered</h2>
<p>{B} supplies software services. There is nothing physical to ship and no
shipping charge applies.</p>
<h2>When</h2>
<p>Access is granted immediately on successful payment, normally within seconds.
Prepaid credit appears in your account balance at the same moment.</p>
<h2>How you are notified</h2>
<p>Your invoice is raised automatically on payment and is available in the
product you paid through. Where you have given an email address, confirmation is
sent there.</p>
<h2>If access does not appear</h2>
<p>If money has left your account and access has not been granted within 30
minutes, contact {settings.BUSINESS_EMAIL} with the payment reference. Do not
pay again — duplicate charges are refunded in full but take days to return.</p>"""),

    "pricing": ("Pricing", f"""
<h2>How pricing works</h2>
<p>Each product supplied by {B} is priced separately. The exact price applying to
you is shown in your product before you pay and again on the payment page, and
that figure is what is charged.</p>
<h2>Taxes</h2>
<p>{'Prices are exclusive of GST. GST at ' + str(settings.DEFAULT_GST_RATE) + '% is added at checkout and shown separately on your tax invoice.' if settings.gst_registered else B + ' is not currently registered under GST. No tax is charged and a bill of supply is issued instead of a tax invoice.'}</p>
<h2>Currency</h2>
<p>All prices are in Indian Rupees (INR).</p>
<h2>Changes</h2>
<p>Prices may change. An existing subscription keeps its price for the period
already paid for; a change applies from the next renewal and you will be told
before it takes effect.</p>"""),

    "contact": ("Contact Us", f"""
<h2>{B}</h2>
<p>{settings.BUSINESS_ADDRESS}</p>
<p>Email: {settings.BUSINESS_EMAIL}<br>
{'Phone: ' + settings.BUSINESS_PHONE if settings.BUSINESS_PHONE else ''}</p>
{'<p>GSTIN: ' + settings.BUSINESS_GSTIN + '</p>' if settings.BUSINESS_GSTIN else ''}
{'<p>PAN: ' + settings.BUSINESS_PAN + '</p>' if settings.BUSINESS_PAN else ''}
<h2>Support</h2>
<p>For anything about a payment, quote the payment reference (it starts with
<code>dgn_pay_</code>) and we will reply within one business day.</p>"""),
}


@router.get("/{slug}", response_class=HTMLResponse)
def legal_page(slug: str, request: Request):
    if slug not in PAGES:
        raise NotFoundError("Page not found")
    title, content = PAGES[slug]
    return templates.TemplateResponse("legal/page.html", {
        "request": request, "title": title, "content": content,
        "business": B, "address": settings.BUSINESS_ADDRESS,
        "email": settings.BUSINESS_EMAIL, "phone": settings.BUSINESS_PHONE,
        "gstin": settings.BUSINESS_GSTIN,
        "updated": date.today().strftime("%d %B %Y")})
