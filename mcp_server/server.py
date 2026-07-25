from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.analytics import (
    category_breakdown,
    category_detail,
    compare_pay_periods as compare_pay_periods_svc,
    data_range,
    day_stats,
    merchant_detail,
    merchant_totals,
    month_to_date_stats,
    pay_period_to_date_stats,
    search_merchants as search_merchants_svc,
)
from app.services.bills import bills_due_by, list_active_bills
from app.services.budgets import budget_progress
from app.services.forecast import build_forecast
from app.services.income import current_pay_period
from app.services.insight_facts import build_insight_facts
from app.services.insights import get_latest_insight
from app.services.safe_spend import calculate_safe_spend
from mcp_server.serialize import money, to_jsonable

INSTRUCTIONS = """
You are querying a personal UK Monzo finance dashboard (read-only).

Rules:
- Cite numbers returned by tools only; do not invent balances or merchants.
- Default spend is discretionary: Monzo categories Bills/Savings/Transfers and
  merchants matching active Upcoming Bills are excluded from spend rankings.
- When comparing pay periods of different lengths (e.g. 4 vs 5 weeks), prefer
  avg_daily and normalised_28d from compare_pay_periods over raw totals.
- Budgets still count all outflows in a category (including bills).
- Forecast is income + bills only (not everyday discretionary spend).
- This is not mortgage or regulated financial advice.
- Prefer get_spending_snapshot or compare_pay_periods for top-level questions,
  then drill with get_category_detail / get_merchant_detail.
""".strip()

mcp = FastMCP(
    name="finance-dashboard",
    instructions=INSTRUCTIONS,
)


def _db() -> Session:
    return SessionLocal()


def _parse_date(value: str | None, *, default: date | None = None) -> date:
    if value is None or value == "":
        if default is None:
            raise ValueError("date is required (YYYY-MM-DD)")
        return default
    return date.fromisoformat(value)


def _default_range(db: Session) -> tuple[date, date]:
    period = current_pay_period(db)
    return period.start, period.today


def _period_stats_dict(stats: Any) -> dict[str, Any]:
    return {
        "start": stats.start.isoformat(),
        "end": stats.end.isoformat(),
        "income": money(stats.income),
        "spent": money(stats.spent),
        "net": money(stats.net),
        "by_category": to_jsonable(stats.by_category),
        "by_merchant": to_jsonable(stats.by_merchant),
        "largest": [
            {
                "date": tx.date.isoformat(),
                "merchant": tx.merchant or "Unknown",
                "category": tx.category or "Uncategorised",
                "amount": money(abs(tx.amount or Decimal("0"))),
            }
            for tx in stats.largest
        ],
    }


@mcp.tool()
def get_data_range() -> dict[str, Any]:
    """Earliest/latest transaction dates and row count in the database."""
    db = _db()
    try:
        return to_jsonable(data_range(db))
    finally:
        db.close()


@mcp.tool()
def get_spending_snapshot() -> dict[str, Any]:
    """Top-level insight facts: pace, periods, category/merchant deltas, habits, outliers."""
    db = _db()
    try:
        return build_insight_facts(db).to_dict()
    finally:
        db.close()


@mcp.tool()
def get_safe_spend() -> dict[str, Any]:
    """Balance, buffer, bills reserved, available, safe daily spend, days until payday."""
    db = _db()
    try:
        return to_jsonable(calculate_safe_spend(db))
    finally:
        db.close()


@mcp.tool()
def get_pay_period_stats(top_n: int = 8) -> dict[str, Any]:
    """Discretionary spend/income for the current pay period (last payday through today)."""
    db = _db()
    try:
        period = current_pay_period(db)
        stats = pay_period_to_date_stats(db, top_n=top_n)
        return {
            "pay_period": {
                "start": period.start.isoformat(),
                "today": period.today.isoformat(),
                "next_payday": period.next_payday.isoformat(),
                "days_elapsed": (period.today - period.start).days + 1,
                "days_full": max((period.next_payday - period.start).days, 1),
            },
            "stats": _period_stats_dict(stats),
        }
    finally:
        db.close()


@mcp.tool()
def compare_pay_periods(count: int = 6, top_n: int = 5) -> dict[str, Any]:
    """Compare recent pay cycles with avg_daily and 28-day normalised spend (fair for 4 vs 5 week months)."""
    db = _db()
    try:
        rows = compare_pay_periods_svc(db, count=count, top_n=top_n)
        return {
            "count": len(rows),
            "note": (
                "Use avg_daily / normalised_28d when period lengths differ. "
                "Current period spent is to-date; projected_full extrapolates pace."
            ),
            "periods": to_jsonable(rows),
        }
    finally:
        db.close()


@mcp.tool()
def get_top_merchants(
    start: str | None = None,
    end: str | None = None,
    top_n: int = 10,
    discretionary: bool = True,
) -> dict[str, Any]:
    """Top merchants by outflow total. Defaults to current pay period. discretionary=True excludes bills/savings/transfers and Upcoming Bill merchants."""
    db = _db()
    try:
        default_start, default_end = _default_range(db)
        start_d = _parse_date(start, default=default_start)
        end_d = _parse_date(end, default=default_end)
        if end_d < start_d:
            raise ValueError("end must be on or after start")
        rows = merchant_totals(
            db, start_d, end_d, discretionary=discretionary, top_n=top_n
        )
        return {
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "discretionary": discretionary,
            "merchants": to_jsonable(rows),
        }
    finally:
        db.close()


@mcp.tool()
def search_merchants(
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 15,
    discretionary: bool = True,
) -> dict[str, Any]:
    """Substring search over merchant names with totals in a date range (defaults to current pay period)."""
    db = _db()
    try:
        default_start, default_end = _default_range(db)
        start_d = _parse_date(start, default=default_start)
        end_d = _parse_date(end, default=default_end)
        rows = search_merchants_svc(
            db,
            query,
            start_d,
            end_d,
            discretionary=discretionary,
            limit=limit,
        )
        return {
            "query": query,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "discretionary": discretionary,
            "matches": to_jsonable(rows),
        }
    finally:
        db.close()


@mcp.tool()
def get_merchant_detail(
    name: str,
    start: str | None = None,
    end: str | None = None,
    discretionary: bool = True,
) -> dict[str, Any]:
    """Deep dive one merchant: total, count, average, first/last seen, monthly series, share of spend. Omit dates to use full history in the DB."""
    db = _db()
    try:
        span = data_range(db)
        default_start = span["earliest"] or date.today()
        default_end = span["latest"] or date.today()
        start_d = _parse_date(start, default=default_start)  # type: ignore[arg-type]
        end_d = _parse_date(end, default=default_end)  # type: ignore[arg-type]
        detail = merchant_detail(
            db, name, start_d, end_d, discretionary=discretionary
        )
        if not detail:
            return {
                "found": False,
                "name": name,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
            }
        return {"found": True, **to_jsonable(detail)}
    finally:
        db.close()


@mcp.tool()
def get_category_breakdown(
    start: str | None = None,
    end: str | None = None,
    top_n: int = 10,
    discretionary: bool = True,
    compare_previous: bool = False,
) -> dict[str, Any]:
    """Category totals for a range (default current pay period). Optionally compare to the previous equal-length window."""
    db = _db()
    try:
        default_start, default_end = _default_range(db)
        start_d = _parse_date(start, default=default_start)
        end_d = _parse_date(end, default=default_end)
        result = category_breakdown(
            db,
            start_d,
            end_d,
            discretionary=discretionary,
            top_n=top_n,
            compare_previous=compare_previous,
        )
        return to_jsonable(result)
    finally:
        db.close()


@mcp.tool()
def get_category_detail(
    name: str,
    start: str | None = None,
    end: str | None = None,
    discretionary: bool = True,
    top_n: int = 10,
    compare_previous: bool = False,
) -> dict[str, Any]:
    """Deep dive one Monzo category: merchants within it, monthly series, largest txs, share of spend, averages. Omit dates to use full history in the DB. Set compare_previous for vs prior pay period."""
    db = _db()
    try:
        span = data_range(db)
        default_start = span["earliest"] or date.today()
        default_end = span["latest"] or date.today()
        start_d = _parse_date(start, default=default_start)  # type: ignore[arg-type]
        end_d = _parse_date(end, default=default_end)  # type: ignore[arg-type]
        detail = category_detail(
            db,
            name,
            start_d,
            end_d,
            discretionary=discretionary,
            top_n=top_n,
            compare_previous=compare_previous,
        )
        if not detail:
            return {
                "found": False,
                "name": name,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
            }
        return {"found": True, **to_jsonable(detail)}
    finally:
        db.close()


@mcp.tool()
def get_budget_status() -> dict[str, Any]:
    """Calendar-month budget progress (all outflows in each category, including bills)."""
    db = _db()
    try:
        rows = budget_progress(db)
        return {"budgets": to_jsonable(rows)}
    finally:
        db.close()


@mcp.tool()
def list_upcoming_bills() -> dict[str, Any]:
    """Active Upcoming Bills plus occurrences reserved through next payday."""
    db = _db()
    try:
        period = current_pay_period(db)
        active = list_active_bills(db)
        due = bills_due_by(db, period.next_payday, today=period.today)
        return {
            "next_payday": period.next_payday.isoformat(),
            "active_bills": [
                {
                    "id": b.id,
                    "name": b.name,
                    "amount": money(b.amount),
                    "frequency": b.frequency,
                    "next_due_date": b.next_due_date.isoformat(),
                    "category": b.category,
                }
                for b in active
            ],
            "due_by_payday": [
                {
                    "id": o.id,
                    "name": o.name,
                    "amount": money(o.amount),
                    "next_due_date": o.next_due_date.isoformat(),
                }
                for o in due
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_cashflow_forecast(days: int = 30) -> dict[str, Any]:
    """Income + bills forecast (not everyday discretionary spend). Default 30 days."""
    db = _db()
    try:
        days = max(1, min(int(days), 90))
        result = build_forecast(db, days=days)
        return {
            "days": days,
            "start_balance": money(result.start_balance),
            "end_balance": money(result.end_balance),
            "next_payday": result.next_payday.isoformat(),
            "events": to_jsonable(result.events),
            "timeline": [
                {
                    "date": p.date.isoformat(),
                    "balance": money(p.balance),
                    "events": to_jsonable(p.events),
                }
                for p in result.timeline
                if p.events
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_day_or_mtd_stats(mode: str = "mtd", day: str | None = None) -> dict[str, Any]:
    """Quick stats: mode='mtd' for calendar month-to-date, mode='day' for a single day (default yesterday)."""
    db = _db()
    try:
        today = date.today()
        if mode == "day":
            target = _parse_date(day, default=today - timedelta(days=1))
            stats = day_stats(db, target, top_n=8)
            return {"mode": "day", "stats": _period_stats_dict(stats)}
        stats = month_to_date_stats(db, today)
        return {"mode": "mtd", "stats": _period_stats_dict(stats)}
    finally:
        db.close()


@mcp.tool()
def get_latest_coach_insight() -> dict[str, Any]:
    """Cached Gemini coach insight from the dashboard (does not call Gemini)."""
    db = _db()
    try:
        insight = get_latest_insight(db, ok_only=True)
        if not insight:
            return {"found": False}
        return {
            "found": True,
            "status": insight.status,
            "coach_status": insight.coach_status,
            "headline": insight.headline,
            "actions": insight.actions,
            "leaks": insight.leaks,
            "habits": insight.habits,
            "created_at": insight.created_at.isoformat() if insight.created_at else None,
            "model": insight.model,
        }
    finally:
        db.close()
