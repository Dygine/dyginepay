"""
The admin dashboard. Server-rendered, because it is tables and forms and
shipping a Node toolchain to render a table is a cost with no return.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.admin.auth import clear_cookie, current_user, issue_cookie
from app.core.config import settings
from app.core.crypto import (
    encrypt, generate_secret, hash_password, hash_token, verify_password,
)
from app.core.database import get_db
from app.core.exceptions import NotFoundError, ValidationError
from app.core.money import paise_to_rupees, rupee_display, rupees_to_paise
from app.models import (
    AdminUser, ApiKey, AuditLog, Cost, Customer, InboundEvent, Invoice,
    OutboundDelivery, Payment, Plan, Product, Subscription, Wallet,
    WalletTransaction,
)
from app.models.enums import (
    AuditAction, DeliveryStatus, KeyMode, PaymentStatus, ProductStatus,
    WalletTxnSource,
)
from app.services import (
    audit, pdf_service, refund_service, reference, reporting_service,
    wallet_service, webhook_service,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["rupees"] = rupee_display
templates.env.filters["plain_rupees"] = paise_to_rupees


FLASH_COOKIE = "dygine_issued_key"


def _flash_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="dygine-key-flash")


def _read_flash(request: Request) -> dict:
    """Read the one-shot issued-key cookie. Signed, so it cannot be forged."""
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return {}
    try:
        return _flash_serializer().loads(raw, max_age=120)
    except (BadSignature, SignatureExpired):
        return {}


def ctx(request: Request, user: AdminUser, **extra) -> dict:
    base = {"request": request, "user": user, "settings": settings,
            "business": settings.BUSINESS_NAME,
            "gst_registered": settings.gst_registered}
    base.update(extra)
    return base


# ----------------------------------------------------------------- auth --
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("admin/login.html",
                                      {"request": request, "error": None,
                                       "business": settings.BUSINESS_NAME})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.scalars(select(AdminUser).where(
        AdminUser.email == email.strip().lower())).first()

    if user is None or not verify_password(password, user.password_hash):
        audit.record(db, actor=email, action=AuditAction.LOGIN_FAILED,
                     entity_type="admin_user", summary="Failed sign-in")
        db.commit()
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Wrong email or password",
             "business": settings.BUSINESS_NAME}, status_code=401)

    user.last_login_at = datetime.now(timezone.utc)
    audit.record(db, actor=user.email, action=AuditAction.LOGIN,
                 entity_type="admin_user", entity_id=str(user.id),
                 summary="Signed in")
    db.commit()

    response = RedirectResponse("/admin", status_code=303)
    issue_cookie(response, user)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    clear_cookie(response)
    return response


# ------------------------------------------------------------ dashboard --
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 30,
              user: AdminUser = Depends(current_user),
              db: Session = Depends(get_db)):
    until = date.today()
    since = until - timedelta(days=days - 1)
    return templates.TemplateResponse("admin/dashboard.html", ctx(
        request, user,
        totals=reporting_service.totals(db, since, until),
        margins=reporting_service.profitability(db, since, until),
        daily=reporting_service.daily_revenue(db, days),
        recent=db.scalars(select(Payment)
                          .order_by(Payment.created_at.desc()).limit(8)).all(),
        products={str(p.id): p for p in db.scalars(select(Product))},
        customers={str(c.id): c for c in db.scalars(select(Customer))},
        days=days, since=since, until=until))


# ------------------------------------------------------------- products --
@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.name)).all()
    keys = {}
    for p in products:
        keys[str(p.id)] = db.scalars(select(ApiKey).where(
            ApiKey.product_id == p.id).order_by(ApiKey.created_at.desc())).all()

    # Read the one-shot secret, then delete the cookie so a refresh does not
    # show it again and a shared screen does not leak it later.
    flash = _read_flash(request)
    response = templates.TemplateResponse("admin/products.html", ctx(
        request, user, products=products, keys=keys,
        new_secret=flash.get("secret"), new_key_id=flash.get("key_id")))
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/admin")
    return response


@router.post("/products")
def create_product(slug: str = Form(...), name: str = Form(...),
                   webhook_url: str = Form(default=""),
                   user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    slug = slug.strip().lower().replace(" ", "-")
    if db.scalars(select(Product).where(Product.slug == slug)).first():
        raise ValidationError(f"A product with slug {slug!r} already exists")

    product = Product(slug=slug, name=name.strip(),
                      webhook_url=webhook_url.strip() or None,
                      webhook_secret_encrypted=encrypt(generate_secret(24)))
    db.add(product)
    audit.record(db, actor=user.email, action=AuditAction.CREATED,
                 entity_type="product", summary=f"Created product {slug}")
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@router.post("/products/{product_id}/webhook")
def update_webhook(product_id: uuid.UUID, webhook_url: str = Form(default=""),
                   ip_allowlist: str = Form(default=""),
                   user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError("Product not found")
    product.webhook_url = webhook_url.strip() or None
    product.ip_allowlist = ip_allowlist.strip()
    if not product.webhook_secret_encrypted:
        product.webhook_secret_encrypted = encrypt(generate_secret(24))
    audit.record(db, actor=user.email, action=AuditAction.UPDATED,
                 entity_type="product", entity_id=str(product.id),
                 summary="Updated webhook settings")
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@router.post("/products/{product_id}/keys")
def issue_key(product_id: uuid.UUID, mode: str = Form(default=KeyMode.TEST),
              label: str = Form(default=""),
              user: AdminUser = Depends(current_user),
              db: Session = Depends(get_db)):
    """
    The secret is returned once, in the redirect, and never again. It is stored
    as a SHA-256 hash - a key that is lost is rotated, not recovered.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError("Product not found")

    secret = generate_secret(32)
    key = ApiKey(product_id=product.id, mode=mode,
                 key_id=reference.api_key_id(mode),
                 secret_hash=hash_token(secret), secret_hint=secret[:6],
                 label=label.strip())
    db.add(key)
    audit.record(db, actor=user.email, action=AuditAction.KEY_ISSUED,
                 entity_type="api_key", entity_id=key.key_id,
                 summary=f"Issued {mode} key for {product.slug}")
    db.commit()

    # The secret goes back in a one-shot signed cookie, never in the URL.
    #
    # A query string lands in browser history, in Render's access logs, and in
    # the Referer header of any outbound link on the page. None of those are
    # places an API secret that can move money should ever be written. The
    # cookie is read once by the next page render and deleted immediately.
    response = RedirectResponse("/admin/products?issued=1", status_code=303)
    response.set_cookie(
        FLASH_COOKIE,
        _flash_serializer().dumps({"key_id": key.key_id, "secret": secret}),
        max_age=120, httponly=True, secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax", path="/admin")
    return response


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: uuid.UUID, user: AdminUser = Depends(current_user),
               db: Session = Depends(get_db)):
    key = db.get(ApiKey, key_id)
    if key is None:
        raise NotFoundError("Key not found")
    key.is_active = False
    key.revoked_at = datetime.now(timezone.utc)
    audit.record(db, actor=user.email, action=AuditAction.KEY_REVOKED,
                 entity_type="api_key", entity_id=key.key_id, summary="Revoked")
    db.commit()
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/products/{product_id}/secret", response_class=HTMLResponse)
def reveal_webhook_secret(product_id: uuid.UUID, request: Request,
                          user: AdminUser = Depends(current_user),
                          db: Session = Depends(get_db)):
    from app.core.crypto import decrypt
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError("Product not found")
    return templates.TemplateResponse("admin/secret.html", ctx(
        request, user, product=product,
        secret=decrypt(product.webhook_secret_encrypted) or "(not set)"))


# ------------------------------------------------------------- payments --
@router.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, status: str = "", q: str = "",
                  user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    query = select(Payment).order_by(Payment.created_at.desc()).limit(200)
    if status:
        query = query.where(Payment.status == status)
    payments = db.scalars(query).all()

    if q:
        needle = q.lower()
        customers = {c.id: c for c in db.scalars(select(Customer))}
        payments = [p for p in payments
                    if needle in p.reference.lower()
                    or (p.razorpay_payment_id or "").lower().find(needle) >= 0
                    or needle in customers.get(p.customer_id,
                                               Customer(name="")).name.lower()]

    return templates.TemplateResponse("admin/payments.html", ctx(
        request, user, payments=payments, status=status, q=q,
        products={str(p.id): p for p in db.scalars(select(Product))},
        customers={str(c.id): c for c in db.scalars(select(Customer))},
        statuses=list(PaymentStatus)))


@router.get("/payments/{payment_id}", response_class=HTMLResponse)
def payment_detail(payment_id: uuid.UUID, request: Request,
                   user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    from app.models import Order, Refund
    from app.services import invoice_service
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError("Payment not found")
    return templates.TemplateResponse("admin/payment_detail.html", ctx(
        request, user, payment=payment,
        order=db.get(Order, payment.order_id),
        customer=db.get(Customer, payment.customer_id),
        product=db.get(Product, payment.product_id),
        invoice=invoice_service.for_payment(db, payment.id),
        refunds=db.scalars(select(Refund).where(
            Refund.payment_id == payment.id)).all()))


@router.post("/payments/{payment_id}/refund")
def admin_refund(payment_id: uuid.UUID, amount: str = Form(default=""),
                 reason: str = Form(default=""),
                 user: AdminUser = Depends(current_user),
                 db: Session = Depends(get_db)):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError("Payment not found")
    amount_paise = rupees_to_paise(amount) if amount.strip() else None
    refund_service.create(db, payment, amount_paise=amount_paise,
                          reason=reason, actor=user.email)
    db.commit()
    return RedirectResponse(f"/admin/payments/{payment_id}", status_code=303)


# ------------------------------------------------------------- invoices --
@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    invoices = db.scalars(select(Invoice)
                          .order_by(Invoice.issue_date.desc(),
                                    Invoice.number.desc()).limit(200)).all()
    return templates.TemplateResponse("admin/invoices.html", ctx(
        request, user, invoices=invoices,
        customers={str(c.id): c for c in db.scalars(select(Customer))},
        products={str(p.id): p for p in db.scalars(select(Product))}))


@router.get("/invoices/{invoice_id}/pdf")
def admin_invoice_pdf(invoice_id: uuid.UUID,
                      user: AdminUser = Depends(current_user),
                      db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFoundError("Invoice not found")
    filename = invoice.number.replace("/", "-") + ".pdf"
    return Response(content=pdf_service.render(invoice),
                    media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


# -------------------------------------------------------------- wallets --
@router.get("/wallets", response_class=HTMLResponse)
def wallets_page(request: Request, user: AdminUser = Depends(current_user),
                 db: Session = Depends(get_db)):
    wallets = db.scalars(select(Wallet)
                         .order_by(Wallet.balance_paise.desc())).all()
    return templates.TemplateResponse("admin/wallets.html", ctx(
        request, user, wallets=wallets,
        customers={str(c.id): c for c in db.scalars(select(Customer))},
        drift=wallet_service.reconcile(db),
        total=sum(w.balance_paise for w in wallets)))


@router.get("/wallets/{customer_id}", response_class=HTMLResponse)
def wallet_detail(customer_id: uuid.UUID, request: Request,
                  user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("Customer not found")
    return templates.TemplateResponse("admin/wallet_detail.html", ctx(
        request, user, customer=customer,
        balance=wallet_service.balance(db, customer.id),
        transactions=wallet_service.transactions(db, customer.id, limit=200)))


@router.post("/wallets/{customer_id}/adjust")
def adjust_wallet(customer_id: uuid.UUID, direction: str = Form(...),
                  amount: str = Form(...), reason: str = Form(default=""),
                  user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    """
    A manual correction, which is the only way balance appears without a payment.
    Deliberately audited loudly: this is the one path that creates spendable
    value out of nothing, so it should be easy to find later.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("Customer not found")
    paise = rupees_to_paise(amount)

    if direction == "credit":
        wallet_service.credit(db, customer.id, paise,
                              source=WalletTxnSource.ADJUSTMENT,
                              description=reason or f"Adjustment by {user.email}")
    else:
        wallet_service.debit(db, customer.id, paise,
                             source=WalletTxnSource.ADJUSTMENT,
                             description=reason or f"Adjustment by {user.email}")

    audit.record(db, actor=user.email, action=AuditAction.WALLET_ADJUSTED,
                 entity_type="wallet", entity_id=str(customer.id),
                 summary=f"{direction} {paise} paise: {reason}")
    db.commit()
    return RedirectResponse(f"/admin/wallets/{customer_id}", status_code=303)


# ------------------------------------------------------------ customers --
@router.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    balances = {str(w.customer_id): w.balance_paise
                for w in db.scalars(select(Wallet))}
    spend = {str(r[0]): int(r[1]) for r in db.execute(
        select(Payment.customer_id, func.sum(Payment.amount_paise))
        .where(Payment.status == PaymentStatus.CAPTURED)
        .group_by(Payment.customer_id)).all()}
    return templates.TemplateResponse("admin/customers.html", ctx(
        request, user, customers=customers, balances=balances, spend=spend,
        products={str(p.id): p for p in db.scalars(select(Product))}))


# ----------------------------------------------------------------- plans --
@router.get("/plans", response_class=HTMLResponse)
def plans_page(request: Request, user: AdminUser = Depends(current_user),
               db: Session = Depends(get_db)):
    return templates.TemplateResponse("admin/plans.html", ctx(
        request, user,
        plans=db.scalars(select(Plan).order_by(Plan.created_at.desc())).all(),
        products=db.scalars(select(Product).order_by(Product.name)).all(),
        product_map={str(p.id): p for p in db.scalars(select(Product))},
        subscriptions=db.scalars(select(Subscription)
                                 .order_by(Subscription.created_at.desc())
                                 .limit(100)).all(),
        customers={str(c.id): c for c in db.scalars(select(Customer))},
        plan_map={str(p.id): p for p in db.scalars(select(Plan))}))


@router.post("/plans")
def create_plan(product_id: uuid.UUID = Form(...), code: str = Form(...),
                name: str = Form(...), amount: str = Form(...),
                interval: str = Form(default="monthly"),
                trial_days: int = Form(default=0),
                user: AdminUser = Depends(current_user),
                db: Session = Depends(get_db)):
    db.add(Plan(product_id=product_id, code=code.strip().lower(),
                name=name.strip(), amount_paise=rupees_to_paise(amount),
                interval=interval, trial_days=trial_days,
                sac=settings.DEFAULT_SAC))
    audit.record(db, actor=user.email, action=AuditAction.CREATED,
                 entity_type="plan", summary=f"Created plan {code}")
    db.commit()
    return RedirectResponse("/admin/plans", status_code=303)


# -------------------------------------------------------------- webhooks --
@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_page(request: Request, user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    return templates.TemplateResponse("admin/webhooks.html", ctx(
        request, user,
        outbound=db.scalars(select(OutboundDelivery)
                            .order_by(OutboundDelivery.created_at.desc())
                            .limit(100)).all(),
        inbound=db.scalars(select(InboundEvent)
                           .order_by(InboundEvent.created_at.desc())
                           .limit(100)).all(),
        products={str(p.id): p for p in db.scalars(select(Product))}))


@router.post("/webhooks/{delivery_id}/replay")
def replay_webhook(delivery_id: uuid.UUID,
                   user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    delivery = db.get(OutboundDelivery, delivery_id)
    if delivery is None:
        raise NotFoundError("Delivery not found")
    webhook_service.replay(db, delivery)
    audit.record(db, actor=user.email, action=AuditAction.WEBHOOK_REPLAYED,
                 entity_type="outbound_delivery", entity_id=str(delivery.id),
                 summary=f"Replayed {delivery.event}")
    db.commit()
    return RedirectResponse("/admin/webhooks", status_code=303)


@router.post("/webhooks/drain")
def drain_webhooks(user: AdminUser = Depends(current_user),
                   db: Session = Depends(get_db)):
    webhook_service.drain(db, limit=50)
    return RedirectResponse("/admin/webhooks", status_code=303)


# ----------------------------------------------------------------- costs --
@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, user: AdminUser = Depends(current_user),
               db: Session = Depends(get_db)):
    return templates.TemplateResponse("admin/costs.html", ctx(
        request, user,
        costs=db.scalars(select(Cost).order_by(Cost.incurred_on.desc())
                         .limit(200)).all(),
        products=db.scalars(select(Product).order_by(Product.name)).all(),
        product_map={str(p.id): p for p in db.scalars(select(Product))}))


@router.post("/costs")
def create_cost(product_id: str = Form(default=""), description: str = Form(...),
                amount: str = Form(...), category: str = Form(default="hosting"),
                incurred_on: str = Form(default=""),
                recurring_monthly: bool = Form(default=False),
                user: AdminUser = Depends(current_user),
                db: Session = Depends(get_db)):
    db.add(Cost(product_id=uuid.UUID(product_id) if product_id else None,
                description=description.strip(),
                amount_paise=rupees_to_paise(amount), category=category,
                incurred_on=date.fromisoformat(incurred_on) if incurred_on
                else date.today(),
                recurring_monthly=recurring_monthly))
    db.commit()
    return RedirectResponse("/admin/costs", status_code=303)


# ----------------------------------------------------------------- audit --
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, user: AdminUser = Depends(current_user),
               db: Session = Depends(get_db)):
    return templates.TemplateResponse("admin/audit.html", ctx(
        request, user,
        logs=db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc())
                        .limit(200)).all()))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: AdminUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    from app.services import razorpay_client
    return templates.TemplateResponse("admin/settings.html", ctx(
        request, user, razorpay_configured=razorpay_client.configured(),
        webhook_secret_set=bool(settings.RAZORPAY_WEBHOOK_SECRET)))
