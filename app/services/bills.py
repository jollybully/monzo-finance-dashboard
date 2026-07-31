from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models import Transaction, UpcomingBill

logger = logging.getLogger(__name__)


def _add_months(d: date, months: int = 1) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def compute_next_due(
    frequency: str,
    due_day: int | None,
    *,
    today: date | None = None,
    explicit: date | None = None,
) -> date:
    today = today or date.today()
    if frequency == "once":
        return explicit or today

    if frequency == "weekly":
        weekday = max(0, min(6, due_day if due_day is not None else today.weekday()))
        days_ahead = (weekday - today.weekday()) % 7
        if days_ahead == 0:
            # If due today, keep today as next due (still unpaid today)
            return today
        return today + timedelta(days=days_ahead)

    # monthly
    day = max(1, min(28, due_day if due_day is not None else today.day))
    year, month = today.year, today.month
    last = monthrange(year, month)[1]
    candidate = date(year, month, min(day, last))
    if candidate < today:
        nxt = _add_months(date(year, month, 1), 1)
        last = monthrange(nxt.year, nxt.month)[1]
        candidate = date(nxt.year, nxt.month, min(day, last))
    return candidate


def advance_due_date(bill: UpcomingBill, today: date | None = None) -> date:
    today = today or date.today()
    due = bill.next_due_date
    while due < today:
        if bill.frequency == "once":
            break
        if bill.frequency == "weekly":
            due = due + timedelta(days=7)
        else:
            day = max(1, min(28, bill.due_day if bill.due_day is not None else due.day))
            nxt = _add_months(due.replace(day=1), 1)
            last = monthrange(nxt.year, nxt.month)[1]
            due = date(nxt.year, nxt.month, min(day, last))
    return due


def advance_overdue_bills(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    updated = 0
    bills = (
        db.query(UpcomingBill)
        .filter(UpcomingBill.active.is_(True), UpcomingBill.next_due_date < today)
        .all()
    )
    for bill in bills:
        if bill.frequency == "once":
            # One-off past due: deactivate so it stops reserving forever
            bill.active = False
            updated += 1
            continue
        new_due = advance_due_date(bill, today)
        if new_due != bill.next_due_date:
            bill.next_due_date = new_due
            updated += 1
    if updated:
        db.commit()
    return updated


def _normalize_bill_key(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def _merchant_matches_bill(bill_name: str, merchant: str | None) -> bool:
    bill_key = _normalize_bill_key(bill_name)
    merch_key = _normalize_bill_key(merchant)
    if not bill_key or not merch_key:
        return False
    # Short brands (O2): require token/prefix match, not loose substring.
    if len(bill_key) < 3:
        return merch_key == bill_key or merch_key.startswith(bill_key + " ")
    return bill_key in merch_key or merch_key in bill_key


def _amount_close(tx_amount: Decimal, bill_amount: Decimal) -> bool:
    """Exact match, or within 1% for tiny card rounding (min 1p)."""
    paid = abs(tx_amount or Decimal("0"))
    expected = abs(bill_amount or Decimal("0"))
    if expected <= 0:
        return False
    if paid == expected:
        return True
    tol = max(Decimal("0.01"), (expected * Decimal("0.01")).quantize(Decimal("0.01")))
    return abs(paid - expected) <= tol


def _advance_bill_after_payment(bill: UpcomingBill, paid_on: date) -> bool:
    """Move next_due past this payment. Returns True if the bill row changed."""
    if bill.frequency == "once":
        if bill.active:
            bill.active = False
            return True
        return False

    due = bill.next_due_date
    nxt = _step_due(bill, due)
    if nxt is None or nxt <= due:
        bill.active = False
        return True
    # Keep stepping while still on/before the payment date (covers early pays).
    while nxt <= paid_on:
        stepped = _step_due(bill, nxt)
        if stepped is None or stepped <= nxt:
            break
        nxt = stepped
    if nxt != bill.next_due_date:
        bill.next_due_date = nxt
        return True
    return False


def reconcile_paid_bills(db: Session, today: date | None = None) -> int:
    """Advance Upcoming Bills when a matching Monzo outflow is found near the due date.

    Requires amount match within a date window around next_due. Merchant name
    (substring either way) only boosts score when amounts collide — bill labels
    often differ from Monzo (e.g. Rent → JETTA Dorling).
    """
    today = today or date.today()
    advance_overdue_bills(db, today)

    bills = (
        db.query(UpcomingBill)
        .filter(
            UpcomingBill.active.is_(True),
            UpcomingBill.next_due_date <= today + timedelta(days=3),
        )
        .order_by(UpcomingBill.amount.desc(), UpcomingBill.id)
        .all()
    )
    if not bills:
        return 0

    window_start = today - timedelta(days=14)
    txs = (
        db.query(Transaction)
        .filter(
            and_(
                Transaction.date >= window_start,
                Transaction.date <= today,
                Transaction.amount < 0,
            )
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )
    if not txs:
        return 0

    used_tx_ids: set[int] = set()
    updated = 0

    for bill in bills:
        due = bill.next_due_date
        match_start = due - timedelta(days=5)
        match_end = due + timedelta(days=3)
        best: tuple[int, Transaction] | None = None  # score, tx

        for tx in txs:
            if tx.id in used_tx_ids:
                continue
            if tx.date < match_start or tx.date > match_end:
                continue
            # Amount near due date is required (Rent ≠ JETTA Dorling on Monzo).
            # Merchant match only boosts score when several bills share an amount.
            if not _amount_close(tx.amount, bill.amount):
                continue
            score = 50
            if _merchant_matches_bill(bill.name, tx.merchant):
                score = 100
            score -= abs((tx.date - due).days)
            if best is None or score > best[0]:
                best = (score, tx)

        if best is None:
            continue

        _, tx = best
        if _advance_bill_after_payment(bill, tx.date):
            used_tx_ids.add(tx.id)
            updated += 1
            logger.info(
                "Advanced bill %s (%s) after %s %s on %s → next due %s",
                bill.id,
                bill.name,
                tx.merchant,
                tx.amount,
                tx.date,
                bill.next_due_date,
            )

    if updated:
        db.commit()
    return updated


def list_active_bills(db: Session) -> list[UpcomingBill]:
    reconcile_paid_bills(db)
    return (
        db.query(UpcomingBill)
        .filter(UpcomingBill.active.is_(True))
        .order_by(UpcomingBill.next_due_date, UpcomingBill.name)
        .all()
    )


def list_all_bills(db: Session) -> list[UpcomingBill]:
    reconcile_paid_bills(db)
    return (
        db.query(UpcomingBill)
        .order_by(UpcomingBill.active.desc(), UpcomingBill.next_due_date, UpcomingBill.name)
        .all()
    )


def _step_due(bill: UpcomingBill, due: date) -> date | None:
    if bill.frequency == "once":
        return None
    if bill.frequency == "weekly":
        return due + timedelta(days=7)
    day = max(1, min(28, bill.due_day if bill.due_day is not None else due.day))
    nxt = _add_months(due.replace(day=1), 1)
    last = monthrange(nxt.year, nxt.month)[1]
    return date(nxt.year, nxt.month, min(day, last))


@dataclass(frozen=True)
class BillOccurrence:
    """A single bill charge within a date window (recurring bills may appear more than once)."""

    id: int
    name: str
    amount: Decimal
    next_due_date: date


def bill_occurrences(
    bill: UpcomingBill,
    start: date,
    end: date,
) -> list[BillOccurrence]:
    """Expand a bill into every charge from start through end (inclusive)."""
    if start > end:
        return []

    due = bill.next_due_date
    safety = 0
    while due < start and bill.frequency != "once" and safety < 52:
        nxt = _step_due(bill, due)
        if nxt is None or nxt <= due:
            break
        due = nxt
        safety += 1

    out: list[BillOccurrence] = []
    while due <= end and safety < 64:
        if due >= start:
            out.append(
                BillOccurrence(
                    id=bill.id,
                    name=bill.name,
                    amount=bill.amount or Decimal("0.00"),
                    next_due_date=due,
                )
            )
        nxt = _step_due(bill, due)
        if nxt is None or nxt <= due:
            break
        due = nxt
        safety += 1
    return out


def bills_due_by(db: Session, end: date, *, today: date | None = None) -> list[BillOccurrence]:
    """All bill charges from today through end (inclusive), expanding weekly/monthly recurrence."""
    today = today or date.today()
    occurrences: list[BillOccurrence] = []
    for bill in list_active_bills(db):
        occurrences.extend(bill_occurrences(bill, today, end))
    occurrences.sort(key=lambda o: (o.next_due_date, o.name))
    return occurrences


def bills_reserved_total(db: Session, end: date, *, today: date | None = None) -> Decimal:
    total = Decimal("0.00")
    for occ in bills_due_by(db, end, today=today):
        total += occ.amount
    return total


def create_bill(
    db: Session,
    *,
    name: str,
    amount: Decimal,
    frequency: str,
    due_day: int | None,
    next_due_date: date | None = None,
    category: str | None = None,
    notes: str | None = None,
    active: bool = True,
) -> UpcomingBill:
    due = next_due_date or compute_next_due(
        frequency, due_day, explicit=next_due_date
    )
    # Persist weekday for weekly bills so recurrence stays on the intended day
    if frequency == "weekly" and due_day is None:
        due_day = due.weekday()
    row = UpcomingBill(
        name=name.strip(),
        amount=amount,
        frequency=frequency,
        due_day=due_day,
        next_due_date=due,
        category=(category.strip() if category else None) or None,
        notes=(notes.strip() if notes else None) or None,
        active=active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_bill(
    db: Session,
    bill_id: int,
    *,
    name: str,
    amount: Decimal,
    frequency: str,
    due_day: int | None,
    next_due_date: date,
    category: str | None,
    notes: str | None,
    active: bool,
) -> UpcomingBill | None:
    row = db.get(UpcomingBill, bill_id)
    if not row:
        return None
    row.name = name.strip()
    row.amount = amount
    row.frequency = frequency
    if frequency == "weekly" and due_day is None:
        due_day = next_due_date.weekday()
    row.due_day = due_day
    row.next_due_date = next_due_date
    row.category = (category.strip() if category else None) or None
    row.notes = (notes.strip() if notes else None) or None
    row.active = active
    db.commit()
    db.refresh(row)
    return row


def deactivate_bill(db: Session, bill_id: int) -> bool:
    row = db.get(UpcomingBill, bill_id)
    if not row:
        return False
    row.active = False
    db.commit()
    return True


def delete_bill(db: Session, bill_id: int) -> bool:
    row = db.get(UpcomingBill, bill_id)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True
