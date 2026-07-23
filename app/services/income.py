from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import IncomeRule
from app.services.balance import get_or_create_settings


@dataclass
class IncomeEvent:
    date: date
    name: str
    amount: Decimal
    rule_id: int | None = None


def _last_friday(year: int, month: int) -> date:
    last_day = monthrange(year, month)[1]
    d = date(year, month, last_day)
    # weekday: Mon=0 ... Fri=4
    offset = (d.weekday() - 4) % 7
    return d - timedelta(days=offset)


def _add_months(d: date, months: int) -> date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def next_occurrence_for_rule(rule: IncomeRule, today: date) -> date | None:
    if not rule.active:
        return None

    if rule.rule_type == "last_friday":
        candidate = _last_friday(today.year, today.month)
        if candidate <= today:
            nxt = _add_months(today.replace(day=1), 1)
            candidate = _last_friday(nxt.year, nxt.month)
        return candidate

    if rule.rule_type == "day_of_month":
        day = max(1, min(28, rule.rule_value or 1))
        year, month = today.year, today.month
        last = monthrange(year, month)[1]
        candidate = date(year, month, min(day, last))
        if candidate <= today:
            nxt = _add_months(date(year, month, 1), 1)
            last = monthrange(nxt.year, nxt.month)[1]
            candidate = date(nxt.year, nxt.month, min(day, last))
        return candidate

    if rule.rule_type == "weekday":
        target = max(0, min(6, rule.rule_value or 0))
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # next occurrence, not today
        return today + timedelta(days=days_ahead)

    return None


def previous_occurrence_for_rule(rule: IncomeRule, today: date) -> date | None:
    """Most recent pay date on or before today."""
    if not rule.active:
        return None

    if rule.rule_type == "last_friday":
        candidate = _last_friday(today.year, today.month)
        if candidate > today:
            prev_month = _add_months(today.replace(day=1), -1)
            candidate = _last_friday(prev_month.year, prev_month.month)
        return candidate

    if rule.rule_type == "day_of_month":
        day = max(1, min(28, rule.rule_value or 1))
        year, month = today.year, today.month
        last = monthrange(year, month)[1]
        candidate = date(year, month, min(day, last))
        if candidate > today:
            prev_month = _add_months(date(year, month, 1), -1)
            last = monthrange(prev_month.year, prev_month.month)[1]
            candidate = date(prev_month.year, prev_month.month, min(day, last))
        return candidate

    if rule.rule_type == "weekday":
        target = max(0, min(6, rule.rule_value or 0))
        days_back = (today.weekday() - target) % 7
        return today - timedelta(days=days_back)

    return None


def list_active_rules(db: Session) -> list[IncomeRule]:
    return (
        db.query(IncomeRule)
        .filter(IncomeRule.active.is_(True))
        .order_by(IncomeRule.id)
        .all()
    )


def _fallback_payday(today: date, payday_day: int) -> date:
    day = max(1, min(28, payday_day))
    year, month = today.year, today.month
    last = monthrange(year, month)[1]
    candidate = date(year, month, min(day, last))
    if candidate <= today:
        nxt = _add_months(date(year, month, 1), 1)
        last = monthrange(nxt.year, nxt.month)[1]
        candidate = date(nxt.year, nxt.month, min(day, last))
    return candidate


def next_pay_date(db: Session, today: date | None = None) -> date:
    today = today or date.today()
    rules = list_active_rules(db)
    dates = [d for r in rules if (d := next_occurrence_for_rule(r, today))]
    if dates:
        return min(dates)
    settings = get_or_create_settings(db)
    return _fallback_payday(today, settings.payday_day)


def _fallback_previous_payday(today: date, payday_day: int) -> date:
    day = max(1, min(28, payday_day))
    year, month = today.year, today.month
    last = monthrange(year, month)[1]
    candidate = date(year, month, min(day, last))
    if candidate > today:
        prev_month = _add_months(date(year, month, 1), -1)
        last = monthrange(prev_month.year, prev_month.month)[1]
        candidate = date(prev_month.year, prev_month.month, min(day, last))
    return candidate


def previous_pay_date(db: Session, today: date | None = None) -> date:
    today = today or date.today()
    rules = list_active_rules(db)
    dates = [d for r in rules if (d := previous_occurrence_for_rule(r, today))]
    if dates:
        return max(dates)
    settings = get_or_create_settings(db)
    return _fallback_previous_payday(today, settings.payday_day)


@dataclass
class PayPeriod:
    """Current pay cycle: last payday → next payday (stats usually through today)."""

    start: date
    today: date
    next_payday: date

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} → {self.next_payday.isoformat()}"


def current_pay_period(db: Session, today: date | None = None) -> PayPeriod:
    today = today or date.today()
    return PayPeriod(
        start=previous_pay_date(db, today),
        today=today,
        next_payday=next_pay_date(db, today),
    )


@dataclass
class PayPeriodBounds:
    """Inclusive calendar span for a pay cycle (payday → day before next payday)."""

    start: date
    end: date
    next_payday: date
    is_current: bool
    days_elapsed: int
    days_full: int


def iter_pay_periods(
    db: Session, *, count: int = 6, today: date | None = None
) -> list[PayPeriodBounds]:
    """Walk payday→payday backwards. Index 0 is the current (possibly open) period."""
    today = today or date.today()
    count = max(1, min(int(count), 24))
    periods: list[PayPeriodBounds] = []

    current = current_pay_period(db, today)
    full_end = current.next_payday - timedelta(days=1)
    days_full = max((current.next_payday - current.start).days, 1)
    days_elapsed = max((today - current.start).days + 1, 1)
    periods.append(
        PayPeriodBounds(
            start=current.start,
            end=min(today, full_end),
            next_payday=current.next_payday,
            is_current=True,
            days_elapsed=days_elapsed,
            days_full=days_full,
        )
    )

    cursor_end = current.start - timedelta(days=1)
    while len(periods) < count:
        prior_start = previous_pay_date(db, cursor_end)
        if prior_start > cursor_end:
            break
        # Avoid infinite loops if payday logic collapses
        if periods and prior_start == periods[-1].start:
            break
        days_full = max((cursor_end - prior_start).days + 1, 1)
        periods.append(
            PayPeriodBounds(
                start=prior_start,
                end=cursor_end,
                next_payday=cursor_end + timedelta(days=1),
                is_current=False,
                days_elapsed=days_full,
                days_full=days_full,
            )
        )
        cursor_end = prior_start - timedelta(days=1)

    return periods


def upcoming_income(
    db: Session,
    start: date,
    end: date,
) -> list[IncomeEvent]:
    """Income events with date in (start, end] — exclusive of start for payday math consistency."""
    events: list[IncomeEvent] = []
    rules = list_active_rules(db)
    if not rules:
        return events

    # Walk each rule forward from start
    for rule in rules:
        cursor = start
        # Find first occurrence strictly after start (paydays after today)
        occ = next_occurrence_for_rule(rule, cursor)
        safety = 0
        while occ and occ <= end and safety < 48:
            if occ > start:
                events.append(
                    IncomeEvent(
                        date=occ, name=rule.name, amount=rule.amount, rule_id=rule.id
                    )
                )
            # Advance past this occurrence
            occ = next_occurrence_for_rule(rule, occ)
            safety += 1

    events.sort(key=lambda e: (e.date, e.name))
    return events


def monthly_income_total(db: Session) -> Decimal:
    rules = list_active_rules(db)
    if not rules:
        settings = get_or_create_settings(db)
        return settings.monthly_income_estimate or Decimal("0.00")

    total = Decimal("0.00")
    for rule in rules:
        if rule.frequency == "weekly" or rule.rule_type == "weekday":
            total += (rule.amount * Decimal("52") / Decimal("12")).quantize(
                Decimal("0.01")
            )
        else:
            total += rule.amount
    return total


def create_income_rule(
    db: Session,
    *,
    name: str,
    amount: Decimal,
    frequency: str,
    rule_type: str,
    rule_value: int | None,
    active: bool = True,
) -> IncomeRule:
    row = IncomeRule(
        name=name.strip(),
        amount=amount,
        frequency=frequency,
        rule_type=rule_type,
        rule_value=rule_value,
        active=active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_income_rule(db: Session, rule_id: int) -> bool:
    row = db.get(IncomeRule, rule_id)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def list_all_rules(db: Session) -> list[IncomeRule]:
    return db.query(IncomeRule).order_by(IncomeRule.active.desc(), IncomeRule.id).all()
