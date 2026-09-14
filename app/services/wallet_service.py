"""
The closed-loop wallet.

The only interesting thing here is the locking. This is wrong:

    if wallet.balance_paise >= amount:        # two requests both pass
        wallet.balance_paise -= amount        # and the balance goes negative

Two concurrent debits read the same balance, both pass the check, and both
subtract. Under `SELECT ... FOR UPDATE` the second request blocks until the
first commits and then reads the real balance. The CHECK constraint on the table
is the backstop for the day someone writes a new code path and forgets.

Balance is stored *and* derivable. The column exists so a balance lookup is one
row; the transaction rows exist so it can be proved. reconcile() asserts they
still agree and is run nightly.
"""
from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InsufficientBalanceError, NotFoundError
from app.models import Customer, Wallet, WalletTransaction
from app.models.enums import WalletTxnSource, WalletTxnType


def _ensure_row(db: Session, customer_id: uuid.UUID) -> None:
    """
    Create the wallet row if it is missing, safely under concurrency.

    A plain "SELECT then INSERT if absent" loses this race: two requests for a
    customer who has never had a wallet both see nothing and both INSERT, and
    the second one dies on the unique constraint. Worse, the caller sees a
    500 on a perfectly valid top-up.

    ON CONFLICT DO UPDATE rather than DO NOTHING is deliberate. DO NOTHING
    returns without waiting, so if a concurrent transaction has inserted the row
    but not yet committed, the following SELECT finds nothing and raises. DO
    UPDATE blocks until that transaction commits, and then the row is visible.
    The update itself is a no-op; it exists only to force that wait.
    """
    db.execute(
        pg_insert(Wallet)
        .values(id=uuid.uuid4(), customer_id=customer_id,
                balance_paise=0, currency="INR")
        .on_conflict_do_update(index_elements=["customer_id"],
                               set_={"currency": "INR"}))
    db.flush()


def get_or_create(db: Session, customer_id: uuid.UUID) -> Wallet:
    _ensure_row(db, customer_id)
    return db.execute(select(Wallet).where(
        Wallet.customer_id == customer_id)).scalar_one()


def _locked(db: Session, customer_id: uuid.UUID) -> Wallet:
    """Fetch the wallet with a row lock held until the transaction commits."""
    _ensure_row(db, customer_id)
    return db.execute(
        select(Wallet).where(Wallet.customer_id == customer_id).with_for_update()
    ).scalar_one()


def _existing(db: Session, wallet_id: uuid.UUID, key: str | None) -> WalletTransaction | None:
    if not key:
        return None
    return db.scalars(select(WalletTransaction).where(
        WalletTransaction.wallet_id == wallet_id,
        WalletTransaction.idempotency_key == key)).first()


def credit(db: Session, customer_id: uuid.UUID, amount_paise: int, *,
           source: str = WalletTxnSource.TOPUP, description: str = "",
           reference_type: str | None = None, reference_id: str | None = None,
           idempotency_key: str | None = None) -> WalletTransaction:
    if amount_paise <= 0:
        raise ConflictError("Credit amount must be positive")

    wallet = _locked(db, customer_id)
    seen = _existing(db, wallet.id, idempotency_key)
    if seen:
        return seen

    wallet.balance_paise += amount_paise
    txn = WalletTransaction(
        wallet_id=wallet.id, type=WalletTxnType.CREDIT, source=source,
        amount_paise=amount_paise, balance_after=wallet.balance_paise,
        description=description, reference_type=reference_type,
        reference_id=str(reference_id) if reference_id else None,
        idempotency_key=idempotency_key)
    db.add(txn)
    db.flush()
    return txn


def debit(db: Session, customer_id: uuid.UUID, amount_paise: int, *,
          source: str = WalletTxnSource.CONSUMPTION, description: str = "",
          reference_type: str | None = None, reference_id: str | None = None,
          idempotency_key: str | None = None) -> WalletTransaction:
    if amount_paise <= 0:
        raise ConflictError("Debit amount must be positive")

    wallet = _locked(db, customer_id)
    seen = _existing(db, wallet.id, idempotency_key)
    if seen:
        return seen

    if wallet.balance_paise < amount_paise:
        raise InsufficientBalanceError(
            "Wallet balance is not enough for this charge",
            detail={"balance_paise": wallet.balance_paise,
                    "required_paise": amount_paise,
                    "shortfall_paise": amount_paise - wallet.balance_paise})

    wallet.balance_paise -= amount_paise
    txn = WalletTransaction(
        wallet_id=wallet.id, type=WalletTxnType.DEBIT, source=source,
        amount_paise=amount_paise, balance_after=wallet.balance_paise,
        description=description, reference_type=reference_type,
        reference_id=str(reference_id) if reference_id else None,
        idempotency_key=idempotency_key)
    db.add(txn)
    db.flush()
    return txn


def balance(db: Session, customer_id: uuid.UUID) -> int:
    wallet = db.scalars(select(Wallet).where(Wallet.customer_id == customer_id)).first()
    return wallet.balance_paise if wallet else 0


def transactions(db: Session, customer_id: uuid.UUID, limit: int = 50) -> list[WalletTransaction]:
    wallet = db.scalars(select(Wallet).where(Wallet.customer_id == customer_id)).first()
    if not wallet:
        return []
    return list(db.scalars(
        select(WalletTransaction)
        .where(WalletTransaction.wallet_id == wallet.id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)))


def reconcile(db: Session) -> list[dict]:
    """
    Assert that every wallet's stored balance equals the sum of its ledger.
    Returns the wallets that drifted. Run nightly; an empty list is the only
    acceptable result, and anything else means a code path bypassed the lock.
    """
    signed = func.sum(
        case((WalletTransaction.type == WalletTxnType.CREDIT,
              WalletTransaction.amount_paise),
             else_=-WalletTransaction.amount_paise)
    ).label("computed")

    rows = db.execute(
        select(Wallet.id, Wallet.customer_id, Wallet.balance_paise,
               func.coalesce(signed, 0))
        .outerjoin(WalletTransaction, WalletTransaction.wallet_id == Wallet.id)
        .group_by(Wallet.id, Wallet.customer_id, Wallet.balance_paise)
    ).all()

    drift = []
    for wallet_id, customer_id, stored, computed in rows:
        if stored != int(computed or 0):
            drift.append({"wallet_id": str(wallet_id),
                          "customer_id": str(customer_id),
                          "stored_paise": stored,
                          "computed_paise": int(computed or 0)})
    return drift
