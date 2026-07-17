from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.models import BillSuggestion, Transaction
from app.services.analytics import (
    active_bill_merchant_keys,
    is_non_discretionary,
    pay_period_to_date_stats,
    previous_pay_period_stats,
    query_transactions,
)
from app.services.budgets import over_budget
from app.services.income import current_pay_period, previous_pay_date
from app.services.safe_spend import calculate_safe_spend


def _money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def _named_totals(items: list) -> list[dict[str, str]]:
    return [{"name": row.name, "total": _money(row.total)} for row in items]


@dataclass
class InsightFacts:
    """Compact aggregate payload for Gemini (no full transaction dump)."""

    today: str
    currency: str
    pay_period: dict[str, Any]
    pace: dict[str, Any]
    current_period: dict[str, Any]
    previous_period: dict[str, Any]
    category_deltas: list[dict[str, Any]]
    merchant_deltas: list[dict[str, Any]]
    over_budget: list[dict[str, str]]
    bill_suggestions: list[dict[str, str]]
    habits: dict[str, Any]
    outliers: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    def facts_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:32]


def _period_maps(
    db: Session, start: date, end: date
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    txs = query_transactions(db, start, end)
    bill_keys = active_bill_merchant_keys(db)
    cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    merch: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in txs:
        if (tx.amount or 0) >= 0:
            continue
        if is_non_discretionary(tx, bill_keys):
            continue
        amount = abs(tx.amount)
        cat[tx.category or "Uncategorised"] += amount
        merch[tx.merchant or "Unknown"] += amount
    return dict(cat), dict(merch)


def _deltas(
    current: dict[str, Decimal], previous: dict[str, Decimal], *, top_n: int = 5
) -> list[dict[str, Any]]:
    names = set(current) | set(previous)
    rows: list[dict[str, Any]] = []
    for name in names:
        cur = current.get(name, Decimal("0.00"))
        prev = previous.get(name, Decimal("0.00"))
        delta = cur - prev
        if cur == 0 and prev == 0:
            continue
        rows.append(
            {
                "name": name,
                "current": _money(cur),
                "previous": _money(prev),
                "delta": _money(delta),
            }
        )
    rows.sort(key=lambda r: abs(Decimal(r["delta"])), reverse=True)
    return rows[:top_n]


def _habit_slice(db: Session, start: date, end: date, spent: Decimal) -> dict[str, Any]:
    bill_keys = active_bill_merchant_keys(db)
    txs = (
        db.query(Transaction)
        .filter(
            Transaction.date >= start,
            Transaction.date <= end,
            Transaction.amount < 0,
        )
        .all()
    )
    weekday = Decimal("0.00")
    weekend = Decimal("0.00")
    by_merchant: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in txs:
        if is_non_discretionary(tx, bill_keys):
            continue
        amount = abs(tx.amount or Decimal("0.00"))
        if tx.date.weekday() >= 5:
            weekend += amount
        else:
            weekday += amount
        by_merchant[tx.merchant or "Unknown"] += amount

    top_merchant = None
    top_share = Decimal("0.0")
    if by_merchant and spent > 0:
        name, total = max(by_merchant.items(), key=lambda item: item[1])
        top_merchant = name
        top_share = (total / spent * Decimal("100")).quantize(Decimal("0.1"))

    return {
        "weekday_spend": _money(weekday),
        "weekend_spend": _money(weekend),
        "top_merchant": top_merchant,
        "top_merchant_share_pct": float(top_share) if top_merchant else None,
    }


def _outliers(db: Session, start: date, end: date, *, top_n: int = 3) -> list[dict[str, Any]]:
    bill_keys = active_bill_merchant_keys(db)
    txs = [
        tx
        for tx in query_transactions(db, start, end)
        if (tx.amount or 0) < 0 and not is_non_discretionary(tx, bill_keys)
    ]
    if not txs:
        return []

    by_cat: dict[str, list[Decimal]] = defaultdict(list)
    for tx in txs:
        by_cat[tx.category or "Uncategorised"].append(abs(tx.amount))

    medians = {
        cat: Decimal(str(median(vals))).quantize(Decimal("0.01"))
        for cat, vals in by_cat.items()
        if vals
    }

    largest = sorted(txs, key=lambda t: abs(t.amount), reverse=True)[:top_n]
    rows: list[dict[str, Any]] = []
    for tx in largest:
        cat = tx.category or "Uncategorised"
        amount = abs(tx.amount)
        cat_med = medians.get(cat)
        unusual = bool(cat_med and cat_med > 0 and amount > cat_med * 2)
        rows.append(
            {
                "date": tx.date.isoformat(),
                "merchant": tx.merchant or "Unknown",
                "category": cat,
                "amount": _money(amount),
                "unusual_vs_category_median": unusual,
            }
        )
    return rows


def build_insight_facts(db: Session, today: date | None = None) -> InsightFacts:
    today = today or date.today()
    safe = calculate_safe_spend(db, today)
    period = current_pay_period(db, today)
    current = pay_period_to_date_stats(db, today, top_n=5)
    previous = previous_pay_period_stats(db, today, top_n=5)

    days_elapsed = max((today - period.start).days + 1, 1)
    days_in_period = max((period.next_payday - period.start).days, 1)
    avg_daily = (current.spent / Decimal(days_elapsed)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    projected = (avg_daily * Decimal(days_in_period)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    safe_daily = safe.safe_daily_spend
    pace_delta = avg_daily - safe_daily
    if safe_daily <= 0:
        pace_status = "unknown"
    elif avg_daily <= safe_daily * Decimal("1.05"):
        pace_status = "on_track"
    elif avg_daily <= safe_daily * Decimal("1.25"):
        pace_status = "at_risk"
    else:
        pace_status = "overspending"

    cur_cats, cur_merch = _period_maps(db, period.start, today)
    prior_end = period.start - timedelta(days=1)
    prior_start = previous_pay_date(db, prior_end)
    prev_cats, prev_merch = _period_maps(db, prior_start, prior_end)

    overs = over_budget(db, today)
    suggestions = (
        db.query(BillSuggestion)
        .filter(BillSuggestion.status == "suggested")
        .order_by(BillSuggestion.confidence.desc())
        .limit(5)
        .all()
    )

    return InsightFacts(
        today=today.isoformat(),
        currency="GBP",
        pay_period={
            "start": period.start.isoformat(),
            "next_payday": period.next_payday.isoformat(),
            "days_elapsed": days_elapsed,
            "days_until_payday": safe.days_until_payday,
            "days_in_period": days_in_period,
        },
        pace={
            "status": pace_status,
            "safe_daily_spend": _money(safe_daily),
            "avg_daily_spend": _money(avg_daily),
            "pace_delta_vs_safe": _money(pace_delta),
            "spent_to_date": _money(current.spent),
            "projected_period_spend": _money(projected),
            "available": _money(safe.available),
            "balance": _money(safe.current_balance),
            "bills_reserved": _money(safe.bills_reserved),
        },
        current_period={
            "spent": _money(current.spent),
            "income": _money(current.income),
            "top_categories": _named_totals(current.by_category),
            "top_merchants": _named_totals(current.by_merchant),
        },
        previous_period={
            "start": previous.start.isoformat(),
            "end": previous.end.isoformat(),
            "spent": _money(previous.spent),
            "income": _money(previous.income),
            "top_categories": _named_totals(previous.by_category),
            "top_merchants": _named_totals(previous.by_merchant),
        },
        category_deltas=_deltas(cur_cats, prev_cats),
        merchant_deltas=_deltas(cur_merch, prev_merch),
        over_budget=[
            {
                "category": b.category,
                "spent": _money(b.spent),
                "monthly_limit": _money(b.monthly_limit),
            }
            for b in overs
        ],
        bill_suggestions=[
            {
                "merchant": s.merchant,
                "typical_amount": _money(s.typical_amount),
                "frequency_guess": s.frequency_guess,
                "confidence": _money(s.confidence),
            }
            for s in suggestions
        ],
        habits=_habit_slice(db, period.start, today, current.spent),
        outliers=_outliers(db, period.start, today),
    )
