from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import UpcomingBill


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


def list_active_bills(db: Session) -> list[UpcomingBill]:
    advance_overdue_bills(db)
    return (
        db.query(UpcomingBill)
        .filter(UpcomingBill.active.is_(True))
        .order_by(UpcomingBill.next_due_date, UpcomingBill.name)
        .all()
    )


def list_all_bills(db: Session) -> list[UpcomingBill]:
    advance_overdue_bills(db)
    return (
        db.query(UpcomingBill)
        .order_by(UpcomingBill.active.desc(), UpcomingBill.next_due_date, UpcomingBill.name)
        .all()
    )


def bills_due_by(db: Session, end: date, *, today: date | None = None) -> list[UpcomingBill]:
    today = today or date.today()
    advance_overdue_bills(db, today)
    return [
        b
        for b in list_active_bills(db)
        if today <= b.next_due_date <= end
    ]


def bills_reserved_total(db: Session, end: date, *, today: date | None = None) -> Decimal:
    total = Decimal("0.00")
    for bill in bills_due_by(db, end, today=today):
        total += bill.amount or Decimal("0.00")
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
