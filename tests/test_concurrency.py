"""
The two places where concurrency would silently corrupt money.

These use real threads against real Postgres. A mocked test here would prove
nothing, because what is being tested IS the database's locking behaviour.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import InsufficientBalanceError
from app.models import Customer, Invoice, Product, Wallet, WalletTransaction
from app.services import gst, invoice_service, wallet_service


@pytest.fixture
def customer(db, product):
    c = Customer(product_id=product.id, external_id="org_1", name="Sunrise PG",
                 state_code="29")
    db.add(c)
    db.commit()
    return c


class TestWalletLocking:
    def test_concurrent_debits_cannot_overdraw(self, db, customer):
        """
        The classic bug. Balance 100, ten threads each try to debit 20.

        Without SELECT ... FOR UPDATE every thread reads 100, every thread
        passes the check, and the balance ends up at -100. With the lock, five
        succeed and five are refused.
        """
        wallet_service.credit(db, customer.id, 10000)      # 100.00
        db.commit()

        ok, refused = [], []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()                    # maximise the overlap
            s = SessionLocal()
            try:
                wallet_service.debit(s, customer.id, 2000)
                s.commit()
                ok.append(1)
            except InsufficientBalanceError:
                s.rollback()
                refused.append(1)
            finally:
                s.close()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert len(ok) == 5, f"expected 5 successes, got {len(ok)}"
        assert len(refused) == 5

        db.expire_all()
        wallet = db.scalars(select(Wallet).where(
            Wallet.customer_id == customer.id)).first()
        assert wallet.balance_paise == 0
        assert wallet.balance_paise >= 0          # never negative

    def test_concurrent_credits_all_land(self, db, customer):
        barrier = threading.Barrier(8)

        def worker(n):
            barrier.wait()
            s = SessionLocal()
            try:
                wallet_service.credit(s, customer.id, 1000)
                s.commit()
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()

        db.expire_all()
        assert wallet_service.balance(db, customer.id) == 8000
        assert not wallet_service.reconcile(db)       # ledger agrees

    def test_idempotent_debit_charges_once(self, db, customer):
        wallet_service.credit(db, customer.id, 10000)
        db.commit()
        for _ in range(4):
            wallet_service.debit(db, customer.id, 2500, idempotency_key="charge-99")
            db.commit()
        assert wallet_service.balance(db, customer.id) == 7500

    def test_reconcile_detects_drift(self, db, customer):
        wallet_service.credit(db, customer.id, 5000)
        db.commit()
        assert not wallet_service.reconcile(db)

        # Simulate the bug this check exists to catch: a balance written
        # without a matching ledger row.
        wallet = db.scalars(select(Wallet).where(
            Wallet.customer_id == customer.id)).first()
        wallet.balance_paise = 9999
        db.commit()

        drift = wallet_service.reconcile(db)
        assert len(drift) == 1
        assert drift[0]["stored_paise"] == 9999
        assert drift[0]["computed_paise"] == 5000

    def test_database_refuses_negative_balance(self, db, customer):
        """Backstop: even if a future code path skips the service layer."""
        wallet = wallet_service.get_or_create(db, customer.id)
        db.commit()
        wallet.balance_paise = -1
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestInvoiceSequence:
    def test_concurrent_issues_have_no_gaps_or_duplicates(self, db, product,
                                                          customer):
        """
        Twelve invoices issued at once must be numbered 1 to 12 exactly.

        `count(*) + 1` would hand the same number to several threads; the unique
        constraint would then throw away a real payment's invoice.
        """
        numbers, errors = [], []
        barrier = threading.Barrier(12)

        def worker():
            barrier.wait()
            s = SessionLocal()
            try:
                p = s.get(Product, product.id)
                c = s.get(Customer, customer.id)
                inv = invoice_service.issue(
                    s, product=p, customer=c,
                    line_items=[{"description": "Pro", "amount": 149900}])
                s.commit()
                numbers.append(inv.number)
            except Exception as exc:               # noqa: BLE001
                s.rollback()
                errors.append(exc)
            finally:
                s.close()

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"issuing failed: {errors[:2]}"
        assert len(numbers) == 12
        assert len(set(numbers)) == 12, "duplicate invoice numbers issued"

        tails = sorted(int(n.rsplit("/", 1)[-1]) for n in numbers)
        assert tails == list(range(1, 13)), f"gaps in the series: {tails}"

    def test_series_restarts_per_financial_year(self, db, product, customer):
        from datetime import date
        a = invoice_service.issue(db, product=product, customer=customer,
                                  line_items=[{"description": "x", "amount": 10000}],
                                  issue_date=date(2026, 3, 31))
        b = invoice_service.issue(db, product=product, customer=customer,
                                  line_items=[{"description": "x", "amount": 10000}],
                                  issue_date=date(2026, 4, 1))
        db.commit()
        assert "25-26" in a.number
        assert "26-27" in b.number
        assert b.number.endswith("00001")     # new year, fresh series

    def test_credit_note_has_its_own_series(self, db, product, customer):
        inv = invoice_service.issue(db, product=product, customer=customer,
                                    line_items=[{"description": "Pro",
                                                 "amount": 149900}])
        db.commit()
        note = invoice_service.credit_note(db, inv, reason="Duplicate charge")
        db.commit()
        assert "/CN/" in note.number
        assert note.reverses_invoice_id == inv.id
        assert note.total_paise == inv.total_paise
        # The original is untouched - that is the whole point
        assert inv.status == "issued"
