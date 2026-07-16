from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models import Transaction


@dataclass
class NamedTotal:
    name: str
    total: Decimal


@dataclass
class PeriodStats:
    start: date
    end: date
    income: Decimal
    spent: Decimal
    net: Decimal
    by_category: list[NamedTotal]
    by_merchant: list[NamedTotal]
    largest: list[Transaction]


def _month_bounds(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    return start, today


def query_transactions(
    db: Session, start: date, end: date
) -> list[Transaction]:
    return (
        db.query(Transaction)
        .filter(and_(Transaction.date >= start, Transaction.date <= end))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )


def summarize_period(
    db: Session,
    start: date,
    end: date,
    *,
    top_n: int = 5,
) -> PeriodStats:
    txs = query_transactions(db, start, end)
    income = Decimal("0.00")
    spent = Decimal("0.00")
    cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    merch: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount > 0:
            income += amount
        else:
            spent += abs(amount)
            category = tx.category or "Uncategorised"
            merchant = tx.merchant or "Unknown"
            cat[category] += abs(amount)
            merch[merchant] += abs(amount)

    by_category = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(cat.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]
    by_merchant = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(merch.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]
    largest = sorted(
        [t for t in txs if (t.amount or 0) < 0],
        key=lambda t: abs(t.amount),
        reverse=True,
    )[:top_n]

    return PeriodStats(
        start=start,
        end=end,
        income=income,
        spent=spent,
        net=income - spent,
        by_category=by_category,
        by_merchant=by_merchant,
        largest=largest,
    )


def month_to_date_stats(db: Session, today: date | None = None) -> PeriodStats:
    today = today or date.today()
    start, end = _month_bounds(today)
    return summarize_period(db, start, end)


def day_stats(db: Session, day: date, *, top_n: int = 3) -> PeriodStats:
    """Stats for a single calendar day (e.g. yesterday)."""
    return summarize_period(db, day, day, top_n=top_n)


def pay_period_to_date_stats(
    db: Session, today: date | None = None, *, top_n: int = 5
) -> PeriodStats:
    """Spending/income since last payday through today."""
    from app.services.income import current_pay_period

    today = today or date.today()
    period = current_pay_period(db, today)
    return summarize_period(db, period.start, today, top_n=top_n)


def previous_pay_period_stats(
    db: Session, today: date | None = None, *, top_n: int = 5
) -> PeriodStats:
    """Full previous pay cycle (last payday back to the one before)."""
    from app.services.income import current_pay_period, previous_pay_date

    today = today or date.today()
    current = current_pay_period(db, today)
    # Day before current period start is the end of the prior cycle
    prior_end = current.start - timedelta(days=1)
    prior_start = previous_pay_date(db, prior_end)
    return summarize_period(db, prior_start, prior_end, top_n=top_n)


def savings_rate(income: Decimal, spent: Decimal) -> Decimal | None:
    if income <= 0:
        return None
    return ((income - spent) / income * Decimal("100")).quantize(Decimal("0.1"))
