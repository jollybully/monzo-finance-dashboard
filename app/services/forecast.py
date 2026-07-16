from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from calendar import monthrange

from app.services.balance import get_or_create_settings
from app.services.bills import list_active_bills
from app.services.income import next_pay_date, upcoming_income


def _next_monthly(due: date, due_day: int | None) -> date:
    day = max(1, min(28, due_day if due_day is not None else due.day))
    year = due.year + (1 if due.month == 12 else 0)
    month = 1 if due.month == 12 else due.month + 1
    last = monthrange(year, month)[1]
    return date(year, month, min(day, last))


@dataclass
class ForecastEvent:
    date: date
    kind: str  # income | bill
    name: str
    amount: Decimal  # signed: income +, bill -


@dataclass
class ForecastPoint:
    date: date
    balance: Decimal
    events: list[ForecastEvent]


@dataclass
class ForecastResult:
    start_balance: Decimal
    end_balance: Decimal
    next_payday: date
    events: list[ForecastEvent]
    timeline: list[ForecastPoint]


def build_forecast(
    db: Session,
    *,
    today: date | None = None,
    days: int = 30,
) -> ForecastResult:
    today = today or date.today()
    end = today + timedelta(days=days)
    settings = get_or_create_settings(db)
    balance = settings.current_balance or Decimal("0.00")
    payday = next_pay_date(db, today)

    events: list[ForecastEvent] = []

    for inc in upcoming_income(db, today, end):
        events.append(
            ForecastEvent(
                date=inc.date, kind="income", name=inc.name, amount=inc.amount
            )
        )

    for bill in list_active_bills(db):
        # Project bill occurrences within window
        due = bill.next_due_date
        safety = 0
        while due <= end and safety < 24:
            if due > today:
                events.append(
                    ForecastEvent(
                        date=due,
                        kind="bill",
                        name=bill.name,
                        amount=-abs(bill.amount),
                    )
                )
            if bill.frequency == "once":
                break
            if bill.frequency == "weekly":
                due = due + timedelta(days=7)
            else:
                due = _next_monthly(due, bill.due_day)
            safety += 1

    events.sort(key=lambda e: (e.date, 0 if e.kind == "income" else 1, e.name))

    running = balance
    timeline: list[ForecastPoint] = [
        ForecastPoint(date=today, balance=running, events=[])
    ]
    by_day: dict[date, list[ForecastEvent]] = {}
    for ev in events:
        by_day.setdefault(ev.date, []).append(ev)

    for d in sorted(by_day):
        day_events = by_day[d]
        for ev in day_events:
            running += ev.amount
        timeline.append(ForecastPoint(date=d, balance=running, events=day_events))

    return ForecastResult(
        start_balance=balance,
        end_balance=running,
        next_payday=payday,
        events=events,
        timeline=timeline,
    )
