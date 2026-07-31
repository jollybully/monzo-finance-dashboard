from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models import Transaction, UpcomingBill

NON_DISCRETIONARY_CATEGORIES = frozenset({"bills", "savings", "transfers"})


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


def normalize_merchant(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def _normalize_category(category: str | None) -> str:
    if not category:
        return ""
    return category.strip().lower().replace("_", " ")


def active_bill_merchant_keys(db: Session) -> set[str]:
    return {
        normalize_merchant(b.name)
        for b in db.query(UpcomingBill).filter(UpcomingBill.active.is_(True)).all()
        if normalize_merchant(b.name)
    }


def is_non_discretionary(tx: Transaction, bill_keys: set[str]) -> bool:
    """True for fixed outflows: Bills/Savings/Transfers or active Upcoming Bill merchants."""
    if _normalize_category(tx.category) in NON_DISCRETIONARY_CATEGORIES:
        return True
    key = normalize_merchant(tx.merchant)
    return bool(key) and key in bill_keys


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
    bill_keys = active_bill_merchant_keys(db)
    income = Decimal("0.00")
    spent = Decimal("0.00")
    cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    merch: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    discretionary_outflows: list[Transaction] = []

    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount > 0:
            income += amount
            continue
        if is_non_discretionary(tx, bill_keys):
            continue
        spent += abs(amount)
        category = tx.category or "Uncategorised"
        merchant = tx.merchant or "Unknown"
        cat[category] += abs(amount)
        merch[merchant] += abs(amount)
        discretionary_outflows.append(tx)

    by_category = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(cat.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]
    by_merchant = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(merch.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]
    largest = sorted(
        discretionary_outflows,
        key=lambda t: abs(t.amount or Decimal("0.00")),
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


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def current_week_bounds(today: date | None = None) -> tuple[date, date]:
    """Current Mon–Sun week, clipped to today for the open week."""
    today = today or date.today()
    start = monday_of(today)
    sunday = start + timedelta(days=6)
    return start, min(sunday, today)


def previous_full_week_bounds(today: date | None = None) -> tuple[date, date]:
    """Completed Mon–Sun week before the current week (weekly report window)."""
    today = today or date.today()
    this_monday = monday_of(today)
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(days=1)
    return start, end


@dataclass
class WeekComparison:
    start: date
    end: date
    is_current: bool
    days_elapsed: int
    spent: Decimal
    avg_daily: Decimal
    projected_full: Decimal | None
    delta_vs_prev: Decimal | None
    delta_pct_vs_prev: Decimal | None
    delta_vs_avg: Decimal | None


def compare_weeks(
    db: Session,
    *,
    count: int = 8,
    today: date | None = None,
) -> list[WeekComparison]:
    """Recent Mon–Sun discretionary spend. Index 0 is the current (possibly open) week."""
    today = today or date.today()
    count = max(1, min(int(count), 26))
    raw: list[tuple[date, date, bool, int, Decimal]] = []

    cur_start, cur_end = current_week_bounds(today)
    cur_stats = summarize_period(db, cur_start, cur_end, top_n=1)
    raw.append(
        (
            cur_start,
            cur_end,
            True,
            max((cur_end - cur_start).days + 1, 1),
            cur_stats.spent,
        )
    )

    cursor_monday = monday_of(today) - timedelta(days=7)
    while len(raw) < count:
        start = cursor_monday
        end = cursor_monday + timedelta(days=6)
        stats = summarize_period(db, start, end, top_n=1)
        raw.append((start, end, False, 7, stats.spent))
        cursor_monday -= timedelta(days=7)

    completed = [spent for _, _, is_cur, _, spent in raw if not is_cur]
    avg_completed = (
        (sum(completed, Decimal("0.00")) / Decimal(len(completed))).quantize(
            Decimal("0.01")
        )
        if completed
        else None
    )

    rows: list[WeekComparison] = []
    for i, (start, end, is_current, days, spent) in enumerate(raw):
        avg_daily = (spent / Decimal(days)).quantize(Decimal("0.01"))
        projected = None
        if is_current:
            projected = (avg_daily * Decimal("7")).quantize(Decimal("0.01"))

        delta_vs_prev = None
        delta_pct_vs_prev = None
        if i + 1 < len(raw):
            prev_spent = raw[i + 1][4]
            delta_vs_prev = spent - prev_spent
            if prev_spent > 0:
                delta_pct_vs_prev = (
                    delta_vs_prev / prev_spent * Decimal("100")
                ).quantize(Decimal("0.1"))

        delta_vs_avg = None
        if avg_completed is not None and not is_current:
            delta_vs_avg = spent - avg_completed

        rows.append(
            WeekComparison(
                start=start,
                end=end,
                is_current=is_current,
                days_elapsed=days,
                spent=spent,
                avg_daily=avg_daily,
                projected_full=projected,
                delta_vs_prev=delta_vs_prev,
                delta_pct_vs_prev=delta_pct_vs_prev,
                delta_vs_avg=delta_vs_avg,
            )
        )
    return rows


def weeks_benchmark(weeks: list[WeekComparison]) -> dict[str, Decimal | None]:
    """Average / best / worst among completed weeks in a compare_weeks result."""
    completed = [w for w in weeks if not w.is_current]
    if not completed:
        return {
            "avg_spent": None,
            "best_spent": None,
            "worst_spent": None,
            "avg_daily": None,
        }
    spent_vals = [w.spent for w in completed]
    avg_spent = (sum(spent_vals, Decimal("0.00")) / Decimal(len(spent_vals))).quantize(
        Decimal("0.01")
    )
    avg_daily = (
        sum((w.avg_daily for w in completed), Decimal("0.00"))
        / Decimal(len(completed))
    ).quantize(Decimal("0.01"))
    return {
        "avg_spent": avg_spent,
        "best_spent": min(spent_vals),
        "worst_spent": max(spent_vals),
        "avg_daily": avg_daily,
    }


def _week_span(
    week_start: date, *, today: date | None = None
) -> tuple[date, date, bool]:
    today = today or date.today()
    monday = monday_of(week_start)
    sunday = monday + timedelta(days=6)
    is_current = monday <= today <= sunday
    end = min(sunday, today) if is_current else sunday
    return monday, end, is_current


def _diff_maps(
    left: dict[str, Decimal],
    right: dict[str, Decimal],
    *,
    top_n: int,
) -> list[dict[str, str | Decimal]]:
    keys = set(left) | set(right)
    rows: list[dict[str, str | Decimal]] = []
    for name in keys:
        a = left.get(name, Decimal("0.00"))
        b = right.get(name, Decimal("0.00"))
        rows.append({"name": name, "a": a, "b": b, "delta": a - b})
    rows.sort(key=lambda r: abs(Decimal(r["delta"])), reverse=True)
    return rows[:top_n]


def compare_two_weeks(
    db: Session,
    week_a: date,
    week_b: date,
    *,
    today: date | None = None,
    top_n: int = 10,
) -> dict:
    """Side-by-side discretionary spend for two Mon–Sun weeks (a vs b)."""
    today = today or date.today()
    top_n = _clamp_top_n(top_n)
    a_start, a_end, a_current = _week_span(week_a, today=today)
    b_start, b_end, b_current = _week_span(week_b, today=today)

    a_cat, a_merch, a_spent, _ = _outflow_maps(db, a_start, a_end, discretionary=True)
    b_cat, b_merch, b_spent, _ = _outflow_maps(db, b_start, b_end, discretionary=True)
    a_avg, _ = _pace(a_spent, a_start, a_end)
    b_avg, _ = _pace(b_spent, b_start, b_end)
    delta = a_spent - b_spent
    delta_pct = None
    if b_spent > 0:
        delta_pct = (delta / b_spent * Decimal("100")).quantize(Decimal("0.1"))

    return {
        "a": {
            "start": a_start,
            "end": a_end,
            "is_current": a_current,
            "spent": a_spent,
            "avg_daily": a_avg,
        },
        "b": {
            "start": b_start,
            "end": b_end,
            "is_current": b_current,
            "spent": b_spent,
            "avg_daily": b_avg,
        },
        "delta": delta,
        "delta_pct": delta_pct,
        "by_category": _diff_maps(a_cat, b_cat, top_n=top_n),
        "by_merchant": _diff_maps(a_merch, b_merch, top_n=top_n),
    }


def week_detail(
    db: Session,
    week_start: date,
    *,
    today: date | None = None,
    top_n: int = 10,
    vs: date | None = None,
) -> dict:
    """Deep dive one Mon–Sun week: categories, merchants, largest txs, vs previous week."""
    today = today or date.today()
    top_n = _clamp_top_n(top_n)
    monday, end, is_current = _week_span(week_start, today=today)

    stats = summarize_period(db, monday, end, top_n=top_n)
    avg_daily, _ = _pace(stats.spent, monday, end)
    projected_full = None
    if is_current:
        projected_full = (avg_daily * Decimal("7")).quantize(Decimal("0.01"))

    prev_start = monday - timedelta(days=7)
    prev_end = monday - timedelta(days=1)
    prev_stats = summarize_period(db, prev_start, prev_end, top_n=1)
    delta = stats.spent - prev_stats.spent
    delta_pct = None
    if prev_stats.spent > 0:
        delta_pct = (delta / prev_stats.spent * Decimal("100")).quantize(Decimal("0.1"))

    compare = None
    if vs is not None and monday_of(vs) != monday:
        compare = compare_two_weeks(db, monday, vs, today=today, top_n=top_n)

    return {
        "start": monday,
        "end": end,
        "is_current": is_current,
        "spent": stats.spent,
        "income": stats.income,
        "avg_daily": avg_daily,
        "projected_full": projected_full,
        "by_category": stats.by_category,
        "by_merchant": stats.by_merchant,
        "largest": stats.largest,
        "previous": {
            "start": prev_start,
            "end": prev_end,
            "spent": prev_stats.spent,
            "delta": delta,
            "delta_pct": delta_pct,
        },
        "compare": compare,
    }


def _clamp_top_n(top_n: int, *, default: int = 10, hard_max: int = 25) -> int:
    try:
        n = int(top_n)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hard_max))


def _pace(total: Decimal, start: date, end: date) -> tuple[Decimal, Decimal]:
    """Avg daily spend and 28-day normalised pace for a date span."""
    days = max((end - start).days + 1, 1)
    avg_daily = (total / Decimal(days)).quantize(Decimal("0.01"))
    normalised_28d = (avg_daily * Decimal("28")).quantize(Decimal("0.01"))
    return avg_daily, normalised_28d


def data_range(db: Session) -> dict[str, date | int | None]:
    row = db.query(
        func.min(Transaction.date),
        func.max(Transaction.date),
        func.count(Transaction.id),
    ).one()
    return {
        "earliest": row[0],
        "latest": row[1],
        "transaction_count": int(row[2] or 0),
    }


def _outflow_maps(
    db: Session,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal, int]:
    """Category map, merchant map, total spent, outflow count."""
    txs = query_transactions(db, start, end)
    bill_keys = active_bill_merchant_keys(db) if discretionary else set()
    cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    merch: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    spent = Decimal("0.00")
    count = 0
    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount >= 0:
            continue
        if discretionary and is_non_discretionary(tx, bill_keys):
            continue
        abs_amount = abs(amount)
        spent += abs_amount
        count += 1
        cat[tx.category or "Uncategorised"] += abs_amount
        merch[tx.merchant or "Unknown"] += abs_amount
    return dict(cat), dict(merch), spent, count


@dataclass
class PeriodComparison:
    start: date
    end: date
    next_payday: date
    is_current: bool
    days_elapsed: int
    days_full: int
    income: Decimal
    discretionary_spent: Decimal
    avg_daily: Decimal
    normalised_28d: Decimal
    projected_full: Decimal | None
    by_category: list[NamedTotal]
    by_merchant: list[NamedTotal]


def compare_pay_periods(
    db: Session,
    *,
    count: int = 6,
    today: date | None = None,
    top_n: int = 5,
) -> list[PeriodComparison]:
    """Compare recent pay cycles using daily pace (fair for 4- vs 5-week months)."""
    from app.services.income import iter_pay_periods

    today = today or date.today()
    top_n = _clamp_top_n(top_n, default=5)
    rows: list[PeriodComparison] = []
    for bounds in iter_pay_periods(db, count=count, today=today):
        stats = summarize_period(db, bounds.start, bounds.end, top_n=top_n)
        days = max(bounds.days_elapsed, 1)
        avg_daily = (stats.spent / Decimal(days)).quantize(Decimal("0.01"))
        normalised = (avg_daily * Decimal("28")).quantize(Decimal("0.01"))
        projected = None
        if bounds.is_current:
            projected = (avg_daily * Decimal(bounds.days_full)).quantize(Decimal("0.01"))
        rows.append(
            PeriodComparison(
                start=bounds.start,
                end=bounds.end,
                next_payday=bounds.next_payday,
                is_current=bounds.is_current,
                days_elapsed=bounds.days_elapsed,
                days_full=bounds.days_full,
                income=stats.income,
                discretionary_spent=stats.spent,
                avg_daily=avg_daily,
                normalised_28d=normalised,
                projected_full=projected,
                by_category=stats.by_category,
                by_merchant=stats.by_merchant,
            )
        )
    return rows


def merchant_totals(
    db: Session,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
    top_n: int = 10,
) -> list[NamedTotal]:
    top_n = _clamp_top_n(top_n)
    _, merch, _, _ = _outflow_maps(db, start, end, discretionary=discretionary)
    return [
        NamedTotal(name=k, total=v)
        for k, v in sorted(merch.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]


def category_breakdown(
    db: Session,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
    top_n: int = 10,
    compare_previous: bool = False,
) -> dict:
    top_n = _clamp_top_n(top_n)
    cat, _, spent, _ = _outflow_maps(db, start, end, discretionary=discretionary)
    current = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(cat.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]
    result: dict = {
        "start": start,
        "end": end,
        "discretionary": discretionary,
        "spent": spent,
        "by_category": current,
        "previous": None,
    }
    if compare_previous:
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        prev_cat, _, prev_spent, _ = _outflow_maps(
            db, prev_start, prev_end, discretionary=discretionary
        )
        result["previous"] = {
            "start": prev_start,
            "end": prev_end,
            "spent": prev_spent,
            "by_category": [
                NamedTotal(name=k, total=v)
                for k, v in sorted(
                    prev_cat.items(), key=lambda item: item[1], reverse=True
                )[:top_n]
            ],
        }
    return result


def search_merchants(
    db: Session,
    query: str,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
    limit: int = 15,
) -> list[dict]:
    needle = normalize_merchant(query)
    if not needle:
        return []
    limit = _clamp_top_n(limit, default=15)
    _, merch, spent, _ = _outflow_maps(db, start, end, discretionary=discretionary)
    matches: list[tuple[str, Decimal]] = []
    for name, total in merch.items():
        key = normalize_merchant(name)
        if needle in key:
            matches.append((name, total))
    matches.sort(key=lambda item: item[1], reverse=True)
    rows: list[dict] = []
    for name, total in matches[:limit]:
        share = None
        if spent > 0:
            share = (total / spent * Decimal("100")).quantize(Decimal("0.1"))
        rows.append(
            {
                "name": name,
                "total": total,
                "share_of_spent_pct": share,
            }
        )
    return rows


def _category_label(category: str | None) -> str:
    return category or "Uncategorised"


def _previous_pay_bounds(db: Session, end: date) -> tuple[date, date]:
    from app.services.income import current_pay_period, previous_pay_date

    current = current_pay_period(db, end)
    prior_end = current.start - timedelta(days=1)
    prior_start = previous_pay_date(db, prior_end)
    return prior_start, prior_end


def _previous_block(
    *,
    total: Decimal,
    prev_total: Decimal,
    prev_count: int,
    prior_start: date,
    prior_end: date,
) -> dict:
    delta = total - prev_total
    delta_pct = None
    if prev_total > 0:
        delta_pct = (delta / prev_total * Decimal("100")).quantize(Decimal("0.1"))
    prev_avg, prev_norm = _pace(prev_total, prior_start, prior_end)
    return {
        "start": prior_start,
        "end": prior_end,
        "total": prev_total,
        "count": prev_count,
        "delta": delta,
        "delta_pct": delta_pct,
        "avg_daily": prev_avg,
        "normalised_28d": prev_norm,
    }


def _largest_rows(
    matched: list[Transaction],
    *,
    top_n: int,
    default_category: str | None = None,
) -> list[dict]:
    largest_txs = sorted(
        matched,
        key=lambda t: abs(t.amount or Decimal("0.00")),
        reverse=True,
    )[:top_n]
    return [
        {
            "date": tx.date,
            "merchant": tx.merchant or "Unknown",
            "category": tx.category or default_category or "Uncategorised",
            "amount": abs(tx.amount or Decimal("0.00")),
        }
        for tx in largest_txs
    ]


def _by_month_rows(matched: list[Transaction]) -> list[dict]:
    by_month: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in matched:
        by_month[tx.date.strftime("%Y-%m")] += abs(tx.amount or Decimal("0.00"))
    return [
        {"month": k, "total": by_month[k]}
        for k in sorted(by_month.keys(), reverse=True)
    ]


def _merchant_outflows(
    db: Session,
    name: str,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
) -> tuple[str, list[Transaction], Decimal]:
    """Match merchant outflows; return display name, matched txs, discretionary spend total."""
    needle = normalize_merchant(name)
    if not needle:
        return name, [], Decimal("0.00")

    txs = query_transactions(db, start, end)
    bill_keys = active_bill_merchant_keys(db) if discretionary else set()
    matched: list[Transaction] = []
    display_name = name
    total_discretionary = Decimal("0.00")

    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount >= 0:
            continue
        if discretionary and is_non_discretionary(tx, bill_keys):
            continue
        abs_amount = abs(amount)
        total_discretionary += abs_amount
        key = normalize_merchant(tx.merchant)
        if key == needle or needle in key:
            matched.append(tx)
            if tx.merchant:
                display_name = tx.merchant

    return display_name, matched, total_discretionary


def merchant_detail(
    db: Session,
    name: str,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
    top_n: int = 10,
    compare_previous: bool = False,
    series_start: date | None = None,
) -> dict | None:
    """Deep dive one merchant: categories, monthly series, largest txs, pace, optional prior pay period."""
    needle = normalize_merchant(name)
    if not needle:
        return None

    top_n = _clamp_top_n(top_n)
    window_start = (
        series_start if series_start is not None and series_start < start else start
    )
    if window_start < start:
        display_name, window_matched, _ = _merchant_outflows(
            db, name, window_start, end, discretionary=discretionary
        )
        period_matched = [tx for tx in window_matched if tx.date >= start]
        _, _, total_discretionary = _merchant_outflows(
            db, name, start, end, discretionary=discretionary
        )
    else:
        display_name, period_matched, total_discretionary = _merchant_outflows(
            db, name, start, end, discretionary=discretionary
        )
        window_matched = period_matched

    if not period_matched and not window_matched:
        return None

    total = sum(
        (abs(t.amount or Decimal("0.00")) for t in period_matched), Decimal("0.00")
    )
    count = len(period_matched)
    avg = (
        (total / Decimal(count)).quantize(Decimal("0.01"))
        if count
        else Decimal("0.00")
    )
    dates = sorted(t.date for t in period_matched) if period_matched else []
    avg_daily, normalised_28d = _pace(total, start, end)

    cats: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in period_matched:
        cats[_category_label(tx.category)] += abs(tx.amount or Decimal("0.00"))
    by_category = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(cats.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]

    share = None
    if total_discretionary > 0 and total > 0:
        share = (total / total_discretionary * Decimal("100")).quantize(Decimal("0.1"))

    previous = None
    if compare_previous:
        prior_start, prior_end = _previous_pay_bounds(db, end)
        _, prev_matched, _ = _merchant_outflows(
            db, display_name, prior_start, prior_end, discretionary=discretionary
        )
        prev_total = sum(
            (abs(t.amount or Decimal("0.00")) for t in prev_matched),
            Decimal("0.00"),
        )
        previous = _previous_block(
            total=total,
            prev_total=prev_total,
            prev_count=len(prev_matched),
            prior_start=prior_start,
            prior_end=prior_end,
        )

    series_for_month = window_matched if window_start < start else period_matched
    first_seen = dates[0] if dates else (sorted(t.date for t in window_matched)[0] if window_matched else None)
    last_seen = dates[-1] if dates else (sorted(t.date for t in window_matched)[-1] if window_matched else None)

    return {
        "name": display_name,
        "query": name,
        "start": start,
        "end": end,
        "discretionary": discretionary,
        "total": total,
        "count": count,
        "average": avg,
        "avg_daily": avg_daily,
        "normalised_28d": normalised_28d,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "share_of_spent_pct": share,
        "by_category": by_category,
        "by_month": _by_month_rows(series_for_month),
        "largest": _largest_rows(period_matched, top_n=top_n),
        "previous": previous,
        "has_period_spend": bool(period_matched),
    }


def _category_outflows(
    db: Session,
    name: str,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
) -> tuple[str, list[Transaction], Decimal, Decimal]:
    """Match outflows in one category.

    Returns display name, matched txs, discretionary spend total, and all-outflow total
    (for share % when drilling into Bills/Savings/Transfers).
    """
    target = name.strip() or "Uncategorised"
    target_is_fixed = _normalize_category(target) in NON_DISCRETIONARY_CATEGORIES
    txs = query_transactions(db, start, end)
    bill_keys = active_bill_merchant_keys(db) if discretionary else set()
    matched: list[Transaction] = []
    display_name = target
    total_discretionary = Decimal("0.00")
    total_outflows = Decimal("0.00")

    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount >= 0:
            continue
        abs_amount = abs(amount)
        total_outflows += abs_amount
        if not (discretionary and is_non_discretionary(tx, bill_keys)):
            total_discretionary += abs_amount

        cat = _category_label(tx.category)
        if cat != target:
            continue

        # When drilling into a category, include that category's own outflows even if
        # it is Bills/Savings/Transfers; still drop Upcoming Bill merchants elsewhere.
        if discretionary and not target_is_fixed and is_non_discretionary(tx, bill_keys):
            continue

        matched.append(tx)
        if tx.category:
            display_name = tx.category

    return display_name, matched, total_discretionary, total_outflows


def category_detail(
    db: Session,
    name: str,
    start: date,
    end: date,
    *,
    discretionary: bool = True,
    top_n: int = 10,
    compare_previous: bool = False,
    series_start: date | None = None,
) -> dict | None:
    """Deep dive one Monzo category: merchants, monthly series, largest txs, pace, optional prior pay period."""
    top_n = _clamp_top_n(top_n)
    target = name.strip() or "Uncategorised"
    target_is_fixed = _normalize_category(target) in NON_DISCRETIONARY_CATEGORIES
    window_start = (
        series_start if series_start is not None and series_start < start else start
    )
    if window_start < start:
        display_name, window_matched, _, _ = _category_outflows(
            db, name, window_start, end, discretionary=discretionary
        )
        period_matched = [tx for tx in window_matched if tx.date >= start]
        _, _, total_discretionary, total_outflows = _category_outflows(
            db, name, start, end, discretionary=discretionary
        )
    else:
        display_name, period_matched, total_discretionary, total_outflows = (
            _category_outflows(db, name, start, end, discretionary=discretionary)
        )
        window_matched = period_matched

    if not period_matched and not window_matched:
        return None

    total = sum(
        (abs(t.amount or Decimal("0.00")) for t in period_matched), Decimal("0.00")
    )
    count = len(period_matched)
    avg = (
        (total / Decimal(count)).quantize(Decimal("0.01"))
        if count
        else Decimal("0.00")
    )
    dates = sorted(t.date for t in period_matched) if period_matched else []
    avg_daily, normalised_28d = _pace(total, start, end)

    merch: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for tx in period_matched:
        merch[tx.merchant or "Unknown"] += abs(tx.amount or Decimal("0.00"))
    by_merchant = [
        NamedTotal(name=k, total=v)
        for k, v in sorted(merch.items(), key=lambda item: item[1], reverse=True)[:top_n]
    ]

    share = None
    denom = total_outflows if target_is_fixed else total_discretionary
    if denom > 0 and total > 0:
        share = (total / denom * Decimal("100")).quantize(Decimal("0.1"))

    previous = None
    if compare_previous:
        prior_start, prior_end = _previous_pay_bounds(db, end)
        _, prev_matched, _, _ = _category_outflows(
            db, display_name, prior_start, prior_end, discretionary=discretionary
        )
        prev_total = sum(
            (abs(t.amount or Decimal("0.00")) for t in prev_matched),
            Decimal("0.00"),
        )
        previous = _previous_block(
            total=total,
            prev_total=prev_total,
            prev_count=len(prev_matched),
            prior_start=prior_start,
            prior_end=prior_end,
        )

    series_for_month = window_matched if window_start < start else period_matched
    first_seen = dates[0] if dates else (sorted(t.date for t in window_matched)[0] if window_matched else None)
    last_seen = dates[-1] if dates else (sorted(t.date for t in window_matched)[-1] if window_matched else None)

    return {
        "name": display_name,
        "query": name,
        "start": start,
        "end": end,
        "discretionary": discretionary,
        "total": total,
        "count": count,
        "average": avg,
        "avg_daily": avg_daily,
        "normalised_28d": normalised_28d,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "share_of_spent_pct": share,
        "by_merchant": by_merchant,
        "by_month": _by_month_rows(series_for_month),
        "largest": _largest_rows(
            period_matched, top_n=top_n, default_category=display_name
        ),
        "previous": previous,
        "has_period_spend": bool(period_matched),
    }
