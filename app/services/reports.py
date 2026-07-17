from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ReportRun
from app.services.analytics import day_stats, savings_rate, summarize_period
from app.services.balance import get_or_create_settings
from app.services.bills import bills_due_by
from app.services.budgets import over_budget
from app.services.email import send_email
from app.services.income import monthly_income_total
from app.services.pushover import send_pushover
from app.services.insights import ensure_insight_for_report
from app.services.safe_spend import calculate_safe_spend

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

CATEGORY_EMOJI = {
    "groceries": "🛒",
    "eating out": "🍽️",
    "eating_out": "🍽️",
    "transport": "🚌",
    "bills": "📄",
    "entertainment": "🎬",
    "shopping": "🛍️",
    "holidays": "✈️",
    "savings": "💰",
    "general": "•",
    "finances": "💳",
    "personal care": "🧴",
}


def _money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"£{amount:,.2f}"


def _cat_emoji(name: str | None) -> str:
    if not name:
        return ""
    return CATEGORY_EMOJI.get(name.strip().lower(), "")


jinja_env.filters["money"] = _money
jinja_env.filters["cat_emoji"] = _cat_emoji


def _recipient(db: Session) -> str:
    settings = get_settings()
    row = get_or_create_settings(db)
    return (row.email_to or settings.email_to or "").strip()


def _week_bounds(today: date) -> tuple[date, date]:
    weekday = today.weekday()
    this_monday = today - timedelta(days=weekday)
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(days=1)
    return start, end


def _month_bounds_for_report(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    start = last_prev.replace(day=1)
    return start, last_prev


def _urgent_bills(db: Session, today: date, within_days: int = 3):
    end = today + timedelta(days=within_days)
    return bills_due_by(db, end, today=today)


def _render(template_name: str, context: dict) -> tuple[str, str]:
    html = jinja_env.get_template(f"email/{template_name}.html").render(**context)
    text = jinja_env.get_template(f"email/{template_name}.txt").render(**context)
    return html, text


def _smtp_ready() -> bool:
    return bool(get_settings().smtp_host)


def _pushover_ready() -> bool:
    cfg = get_settings()
    return bool(
        cfg.pushover_enabled and cfg.pushover_app_token and cfg.pushover_user_key
    )


def build_daily_report(
    db: Session, today: date | None = None
) -> tuple[str, str, str, date, date, str, str, int]:
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    safe = calculate_safe_spend(db, today)
    yday = day_stats(db, yesterday, top_n=3)
    overs = over_budget(db, today)
    urgent_bills = _urgent_bills(db, today)
    largest = yday.largest[0] if yday.largest else None
    priority = 0

    context = {
        "title": "Yesterday",
        "today": today,
        "yesterday": yesterday,
        "spent": yday.spent,
        "top_merchants": yday.by_merchant[:3],
        "largest": largest,
        "balance": safe.current_balance,
        "safe_daily_spend": safe.safe_daily_spend,
        "days_until_payday": safe.days_until_payday,
        "next_payday": safe.next_payday,
        "urgent_bills": urgent_bills,
        "over_budget": overs,
    }
    html, text = _render("daily", context)
    safe_s = _money(safe.safe_daily_spend)
    days = safe.days_until_payday
    subject = f"💷 Safe {safe_s} · yesterday {_money(yday.spent)}"

    # Pushover: safe spend first (lock-screen glance), then yesterday, then alerts
    push_title = f"Safe {safe_s} today"
    push_lines = [
        f"{days} day{'s' if days != 1 else ''} to payday · {_money(safe.current_balance)} left",
        "",
        f"Yesterday {_money(yday.spent)}",
    ]
    for m in yday.by_merchant[:3]:
        push_lines.append(f"• {m.name} {_money(m.total)}")
    if urgent_bills or overs:
        push_lines.append("")
        for b in urgent_bills:
            push_lines.append(f"⚠ {b.name} {_money(b.amount)} · {b.next_due_date}")
        for b in overs:
            push_lines.append(
                f"⚠ {b.category} {_money(b.spent)} / {_money(b.monthly_limit)}"
            )
    push_body = "\n".join(push_lines)

    return subject, html, text, yesterday, yesterday, push_title, push_body, priority


def build_weekly_report(
    db: Session, today: date | None = None
) -> tuple[str, str, str, date, date, str, str, int]:
    today = today or date.today()
    start, end = _week_bounds(today)
    prev_start = start - timedelta(days=7)
    prev_end = end - timedelta(days=7)
    week = summarize_period(db, start, end)
    prev = summarize_period(db, prev_start, prev_end)
    delta = week.spent - prev.spent
    overs = over_budget(db, today)
    urgent_bills = _urgent_bills(db, today, within_days=7)
    insight = ensure_insight_for_report(db, today)
    priority = 0

    context = {
        "title": "Weekly review",
        "start": start,
        "end": end,
        "spent": week.spent,
        "income": week.income,
        "prev_spent": prev.spent,
        "spent_delta": delta,
        "top_categories": week.by_category,
        "top_merchants": week.by_merchant,
        "largest": week.largest,
        "urgent_bills": urgent_bills,
        "over_budget": overs,
        "insight": insight,
    }
    html, text = _render("weekly", context)
    sign = "+" if delta > 0 else ""
    subject = f"📊 Week {_money(week.spent)} ({sign}{_money(delta)} vs last)"
    safe = calculate_safe_spend(db, today)
    safe_s = _money(safe.safe_daily_spend)

    top = week.by_category[0] if week.by_category else None
    push_title = f"Week done · safe {safe_s}/day"
    push_lines = [
        f"Spent {_money(week.spent)} ({sign}{_money(delta)} vs last week)",
        f"{start} → {end}",
    ]
    if top:
        push_lines.append(f"Top: {top.name} {_money(top.total)}")
    push_lines.append(f"Safe daily now: {safe_s} · {safe.days_until_payday}d to payday")
    if overs:
        push_lines.append(
            f"⚠ {len(overs)} categor{'y' if len(overs) == 1 else 'ies'} over budget"
        )
    if insight and insight.ok and insight.headline:
        push_lines.append(f"Coach: {insight.headline}")
    push_body = "\n".join(push_lines)

    return subject, html, text, start, end, push_title, push_body, priority


def build_monthly_report(
    db: Session, today: date | None = None
) -> tuple[str, str, str, date, date, str, str, int]:
    today = today or date.today()
    if today.day == 1:
        start, end = _month_bounds_for_report(today)
    else:
        start = today.replace(day=1)
        end = today
    month = summarize_period(db, start, end, top_n=10)
    income_est = monthly_income_total(db)
    income_for_rate = income_est if income_est > 0 else month.income
    rate = savings_rate(income_for_rate, month.spent)
    overs = over_budget(db, today)
    priority = 0
    label = start.strftime("%B %Y")

    context = {
        "title": "Monthly review",
        "start": start,
        "end": end,
        "label": label,
        "spent": month.spent,
        "income_actual": month.income,
        "income_estimate": income_est,
        "savings_rate": rate,
        "top_categories": month.by_category,
        "largest": month.largest,
        "days_in_month": monthrange(start.year, start.month)[1],
        "over_budget": overs,
    }
    html, text = _render("monthly", context)
    rate_s = f"{rate}%" if rate is not None else "—"
    subject = f"📅 {label} · spent {_money(month.spent)} · savings {rate_s}"
    safe = calculate_safe_spend(db, today)
    safe_s = _money(safe.safe_daily_spend)

    push_title = f"{label} · safe {safe_s}/day"
    push_lines = [
        f"Spent {_money(month.spent)}",
        f"Inflows {_money(month.income)} · savings {rate_s}",
    ]
    if month.by_category:
        top = month.by_category[0]
        push_lines.append(f"Top: {top.name} {_money(top.total)}")
    push_lines.append(f"Safe daily now: {safe_s} · {safe.days_until_payday}d to payday")
    if overs:
        push_lines.append(
            f"⚠ {len(overs)} categor{'y' if len(overs) == 1 else 'ies'} over budget"
        )
    push_body = "\n".join(push_lines)

    return subject, html, text, start, end, push_title, push_body, priority


def _already_sent(
    db: Session, period: str, period_start: date, period_end: date
) -> bool:
    return (
        db.query(ReportRun)
        .filter(
            ReportRun.period == period,
            ReportRun.period_start == period_start,
            ReportRun.period_end == period_end,
            ReportRun.status == "sent",
        )
        .first()
        is not None
    )


def catch_up_missed_reports(db: Session) -> list[str]:
    """Send at most one missed digest per cadence after downtime.

    Never backfills a multi-day backlog — only the current slot, within a
    short grace window after the scheduled time.
    """
    cfg = get_settings()
    tz = ZoneInfo(cfg.app_tz)
    now = datetime.now(tz)
    today = now.date()
    caught: list[str] = []

    def _send(period: str) -> None:
        run = generate_and_send_report(db, period, send=True, today=today)
        msg = f"{period}={run.status}"
        caught.append(msg)
        logger.info("Catch-up %s report: id=%s status=%s", period, run.id, run.status)

    if cfg.report_daily_enabled:
        scheduled = datetime.combine(
            today, time(cfg.report_daily_hour, cfg.report_daily_minute), tzinfo=tz
        )
        # Same calendar day only, up to 18h after the slot
        if now >= scheduled and (now - scheduled) <= timedelta(hours=18):
            yesterday = today - timedelta(days=1)
            if not _already_sent(db, "daily", yesterday, yesterday):
                _send("daily")

    if cfg.report_weekly_enabled and today.weekday() <= 2:  # Mon–Wed
        monday = today - timedelta(days=today.weekday())
        scheduled = datetime.combine(
            monday, time(cfg.report_weekly_hour, cfg.report_weekly_minute), tzinfo=tz
        )
        if now >= scheduled:
            start, end = _week_bounds(today)
            if not _already_sent(db, "weekly", start, end):
                _send("weekly")

    if cfg.report_monthly_enabled and today.day <= 3:
        first = today.replace(day=1)
        scheduled = datetime.combine(
            first, time(cfg.report_monthly_hour, cfg.report_monthly_minute), tzinfo=tz
        )
        if now >= scheduled:
            start, end = _month_bounds_for_report(today)
            if not _already_sent(db, "monthly", start, end):
                _send("monthly")

    return caught


def generate_and_send_report(
    db: Session,
    period: str,
    *,
    send: bool = True,
    today: date | None = None,
) -> ReportRun:
    builders = {
        "daily": build_daily_report,
        "weekly": build_weekly_report,
        "monthly": build_monthly_report,
    }
    if period not in builders:
        raise ValueError(f"Unknown report period: {period}")

    (
        subject,
        html,
        text,
        start,
        end,
        push_title,
        push_body,
        priority,
    ) = builders[period](db, today)

    status = "sent"
    errors: list[str] = []
    delivered = False
    cfg = get_settings()

    if send:
        want_email = not (period == "daily" and not cfg.report_daily_email)
        if want_email and _smtp_ready():
            try:
                send_email(_recipient(db), subject, html, text)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to email %s report", period)
                errors.append(f"email: {exc}")
        elif want_email:
            errors.append("email: SMTP not configured")

        if _pushover_ready():
            try:
                send_pushover(push_title, push_body, priority=priority)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to Pushover %s report", period)
                errors.append(f"pushover: {exc}")
        elif cfg.pushover_enabled and (
            cfg.pushover_app_token or cfg.pushover_user_key
        ):
            errors.append("pushover: incomplete credentials")

        if not delivered:
            status = "failed"
            if not errors:
                errors.append("No delivery channel configured (SMTP or Pushover)")
    else:
        # Preview only — persist HTML without notifying
        delivered = True

    error = "; ".join(errors) if errors else None

    run = ReportRun(
        period=period,
        period_start=start,
        period_end=end,
        subject=subject,
        body_html=html,
        body_text=text,
        status=status,
        error=error,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
