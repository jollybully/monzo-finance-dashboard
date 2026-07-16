from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.schemas import SafeSpendOut
from app.services.balance import get_or_create_settings
from app.services.bills import bills_due_by, bills_reserved_total
from app.services.income import next_pay_date


def next_payday(today: date, payday_day: int) -> date:
    """Legacy day-of-month helper (fallback when no income rules)."""
    day = max(1, min(28, payday_day))
    year, month = today.year, today.month
    candidate = date(year, month, day)
    if candidate <= today:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        last = monthrange(year, month)[1]
        candidate = date(year, month, min(day, last))
    return candidate


def days_until_payday(today: date, payday_day: int) -> tuple[int, date]:
    payday = next_payday(today, payday_day)
    return (payday - today).days, payday


def calculate_safe_spend(db: Session, today: date | None = None) -> SafeSpendOut:
    today = today or date.today()
    settings = get_or_create_settings(db)
    payday = next_pay_date(db, today)
    days = (payday - today).days
    balance = settings.current_balance or Decimal("0.00")
    buffer = settings.reserved_buffer or Decimal("0.00")
    bills_total = bills_reserved_total(db, payday, today=today)
    bills = bills_due_by(db, payday, today=today)
    available = balance - buffer - bills_total
    divisor = Decimal(max(days, 1))
    safe = (available / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return SafeSpendOut(
        current_balance=balance,
        reserved_buffer=buffer,
        bills_reserved=bills_total,
        available=available,
        days_until_payday=days,
        next_payday=payday,
        safe_daily_spend=safe,
        upcoming_bills=[
            {"id": b.id, "name": b.name, "amount": b.amount, "next_due_date": b.next_due_date}
            for b in bills
        ],
    )
