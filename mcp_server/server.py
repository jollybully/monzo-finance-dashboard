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
    compare_two_weeks as compare_two_weeks_svc,
    compare_weeks as compare_weeks_svc,
    data_range,
    day_stats,
    merchant_detail,
    merchant_totals,
    month_to_date_stats,
    pay_period_to_date_stats,
    search_merchants as search_merchants_svc,
    week_detail as week_detail_svc,
    weeks_benchmark,
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
  Positive Card payment amounts (refunds) net against spend; other credits
  (salary, Faster payments, pots) count as income — not refunds.
- When comparing pay periods of different lengths (e.g. 4 vs 5 weeks), prefer
  avg_daily and normalised_28d from compare_pay_periods over raw totals.
- Budgets net category card-refunds against outflows (including bills categories).
- Forecast is income + bills only (not everyday discretionary spend).
- This is not mortgage or regulated financial advice.
- Prefer get_spending_snapshot or compare_pay_periods for top-level questions,
  then drill with get_category_detail / get_merchant_detail.
- For week-to-week discretionary spend, use compare_weeks or compare_two_weeks;
  prefer avg_daily when the current week is still open.
- Optional receipt tools (get_receipt_list / get_receipt_detail /
  get_receipt_item_spend) expose Lidl (and later other) line items. Receipt
  totals enrich Monzo merchant spend — do not double-count them as extra spend.
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
def compare_weeks(count: int = 12) -> dict[str, Any]:
    """Recent Mon–Sun discretionary weeks with avg_daily, vs previous, and vs completed-week average."""
    db = _db()
    try:
        rows = compare_weeks_svc(db, count=count)
        return {
            "count": len(rows),
            "benchmark": to_jsonable(weeks_benchmark(rows)),
            "note": (
                "Index 0 is the current (possibly open) week. "
                "Use avg_daily / projected_full for the open week; raw spent is to-date."
            ),
            "weeks": to_jsonable(rows),
        }
    finally:
        db.close()


@mcp.tool()
def compare_two_weeks(
    week_a: str,
    week_b: str,
    top_n: int = 10,
) -> dict[str, Any]:
    """Side-by-side discretionary spend for two weeks (pass any date in each Mon–Sun week)."""
    db = _db()
    try:
        return to_jsonable(
            compare_two_weeks_svc(
                db,
                _parse_date(week_a),
                _parse_date(week_b),
                top_n=top_n,
            )
        )
    finally:
        db.close()


@mcp.tool()
def get_week_detail(week_start: str, top_n: int = 10, vs: str | None = None) -> dict[str, Any]:
    """Deep dive one Mon–Sun week; optional vs=YYYY-MM-DD for category/merchant deltas."""
    db = _db()
    try:
        return to_jsonable(
            week_detail_svc(
                db,
                _parse_date(week_start),
                top_n=top_n,
                vs=_parse_date(vs) if vs else None,
            )
        )
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
    top_n: int = 10,
    compare_previous: bool = False,
) -> dict[str, Any]:
    """Deep dive one merchant: total, count, average, avg_daily, normalised_28d pace, categories, monthly series, largest txs, share of spend. Omit dates to use full history in the DB. Set compare_previous for vs prior pay period (includes previous pace)."""
    db = _db()
    try:
        span = data_range(db)
        default_start = span["earliest"] or date.today()
        default_end = span["latest"] or date.today()
        start_d = _parse_date(start, default=default_start)  # type: ignore[arg-type]
        end_d = _parse_date(end, default=default_end)  # type: ignore[arg-type]
        detail = merchant_detail(
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
    """Deep dive one Monzo category: merchants within it, monthly series, largest txs, share of spend, averages, avg_daily and normalised_28d pace. Omit dates to use full history in the DB. Set compare_previous for vs prior pay period (includes previous pace)."""
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
def get_cashflow_forecast(
    days: int = 30,
    include_daily_spend: bool = False,
) -> dict[str, Any]:
    """Income + bills forecast. Set include_daily_spend to also subtract current pay-period avg daily discretionary pace. Default 30 days."""
    db = _db()
    try:
        days = max(1, min(int(days), 90))
        daily = None
        if include_daily_spend:
            from app.services.analytics import pay_period_to_date_stats
            from app.services.income import current_pay_period

            period = current_pay_period(db)
            stats = pay_period_to_date_stats(db, top_n=1)
            elapsed = max((period.today - period.start).days + 1, 1)
            daily = (stats.spent / Decimal(elapsed)).quantize(Decimal("0.01"))
        result = build_forecast(
            db,
            days=days,
            include_daily_spend=include_daily_spend,
            daily_spend=daily,
        )
        return {
            "days": days,
            "include_daily_spend": result.include_daily_spend,
            "daily_spend": money(result.daily_spend) if result.daily_spend is not None else None,
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
                if p.events or include_daily_spend
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


@mcp.tool()
def get_receipt_list(
    source: str = "lidl",
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List synced retailer receipts (line-item enrichment). Empty if the addon has not run."""
    from app.services.receipts import list_receipts, receipts_available

    db = _db()
    try:
        if not receipts_available(db):
            return {"available": False, "receipts": []}
        s, e = _default_range(db)
        start_d = _parse_date(start, default=s)
        end_d = _parse_date(end, default=e)
        rows = list_receipts(
            db,
            source.strip().lower() or None,
            start=start_d,
            end=end_d,
            limit=max(1, min(int(limit), 100)),
        )
        return {
            "available": True,
            "source": source,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "receipts": [
                {
                    "external_id": r.external_id,
                    "purchased_at": r.purchased_at.isoformat() if r.purchased_at else None,
                    "purchase_date": r.purchase_date.isoformat(),
                    "total_amount": money(r.total_amount),
                    "currency": r.currency,
                    "store_name": r.store_name,
                    "store_locality": r.store_locality,
                    "transaction_id": r.transaction_id,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_receipt_detail(source: str, external_id: str) -> dict[str, Any]:
    """Full receipt with line items for a retailer source (e.g. lidl)."""
    from app.services.receipts import get_receipt, receipts_available

    db = _db()
    try:
        if not receipts_available(db):
            return {"available": False, "found": False}
        receipt = get_receipt(db, source.strip().lower(), external_id.strip())
        if receipt is None:
            return {"available": True, "found": False}
        return {
            "available": True,
            "found": True,
            "source": receipt.source,
            "external_id": receipt.external_id,
            "purchased_at": receipt.purchased_at.isoformat() if receipt.purchased_at else None,
            "total_amount": money(receipt.total_amount),
            "currency": receipt.currency,
            "store_name": receipt.store_name,
            "store_locality": receipt.store_locality,
            "store_postcode": receipt.store_postcode,
            "transaction_id": receipt.transaction_id,
            "items": [
                {
                    "product_id": i.product_id,
                    "description": i.description,
                    "quantity": str(i.quantity),
                    "unit_price": money(i.unit_price),
                    "net_total": money(i.net_total),
                    "is_weight": i.is_weight,
                }
                for i in (receipt.items or [])
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_receipt_item_spend(
    source: str = "lidl",
    product_id: str | None = None,
    description: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Spend on one receipt product over a date range. Prefer product_id when known."""
    from app.services.receipts import item_spend, receipts_available

    db = _db()
    try:
        if not receipts_available(db):
            return {"available": False}
        s, e = _default_range(db)
        start_d = _parse_date(start, default=s)
        end_d = _parse_date(end, default=e)
        detail = item_spend(
            db,
            source.strip().lower(),
            product_id=product_id,
            description=description,
            start=start_d,
            end=end_d,
        )
        if detail is None:
            return {"available": True, "found": False}
        return {
            "available": True,
            "found": True,
            "source": detail["source"],
            "product_id": detail.get("product_id"),
            "description": detail.get("description"),
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "total": money(detail["total"]),
            "quantity": str(detail["quantity"]),
            "count": detail["count"],
            "by_month": [
                {"month": row["month"], "total": money(row["total"])}
                for row in detail.get("by_month") or []
            ],
            "lines": [
                {
                    "date": row["date"].isoformat(),
                    "quantity": str(row["quantity"]),
                    "net_total": money(row["net_total"]),
                    "store_name": row.get("store_name"),
                    "receipt_external_id": row.get("receipt_external_id"),
                }
                for row in detail.get("lines") or []
            ],
        }
    finally:
        db.close()
