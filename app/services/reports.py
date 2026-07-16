from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ReportRun
from app.services.analytics import month_to_date_stats, savings_rate, summarize_period
from app.services.balance import get_or_create_settings
from app.services.email import send_email
from app.services.safe_spend import calculate_safe_spend

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"£{amount:,.2f}"


jinja_env.filters["money"] = _money


def _recipient(db: Session) -> str:
    settings = get_settings()
    row = get_or_create_settings(db)
    return (row.email_to or settings.email_to or "").strip()


def _week_bounds(today: date) -> tuple[date, date]:
    # Monday–Sunday week containing today; report covers completed week ending Sunday
    # For scheduled Monday report, use previous Mon–Sun.
    weekday = today.weekday()  # Mon=0
    this_monday = today - timedelta(days=weekday)
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(days=1)
    return start, end


def _month_bounds_for_report(today: date) -> tuple[date, date]:
    # First-of-month job: report on previous calendar month
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    start = last_prev.replace(day=1)
    return start, last_prev


def _render(template_name: str, context: dict) -> tuple[str, str]:
    html = jinja_env.get_template(f"email/{template_name}.html").render(**context)
    text = jinja_env.get_template(f"email/{template_name}.txt").render(**context)
    return html, text


def build_daily_report(db: Session, today: date | None = None) -> tuple[str, str, str, date, date]:
    today = today or date.today()
    safe = calculate_safe_spend(db, today)
    mtd = month_to_date_stats(db, today)
    settings = get_or_create_settings(db)
    income = settings.monthly_income_estimate or Decimal("0.00")
    rate = savings_rate(income, mtd.spent)

    context = {
        "title": "Daily Finance Summary",
        "today": today,
        "balance": safe.current_balance,
        "safe_daily_spend": safe.safe_daily_spend,
        "days_until_payday": safe.days_until_payday,
        "next_payday": safe.next_payday,
        "monthly_income": income,
        "mtd_spent": mtd.spent,
        "mtd_income_actual": mtd.income,
        "savings_rate": rate,
        "top_categories": mtd.by_category[:3],
    }
    html, text = _render("daily", context)
    subject = f"Daily Finance Summary — {_money(safe.current_balance)}"
    return subject, html, text, today, today


def build_weekly_report(db: Session, today: date | None = None) -> tuple[str, str, str, date, date]:
    today = today or date.today()
    start, end = _week_bounds(today)
    prev_start = start - timedelta(days=7)
    prev_end = end - timedelta(days=7)
    week = summarize_period(db, start, end)
    prev = summarize_period(db, prev_start, prev_end)
    settings = get_or_create_settings(db)
    delta = week.spent - prev.spent

    context = {
        "title": "Weekly Finance Summary",
        "start": start,
        "end": end,
        "spent": week.spent,
        "income": week.income,
        "prev_spent": prev.spent,
        "spent_delta": delta,
        "balance": settings.current_balance,
        "top_categories": week.by_category,
        "top_merchants": week.by_merchant,
        "largest": week.largest,
    }
    html, text = _render("weekly", context)
    subject = f"Weekly Finance Summary — spent {_money(week.spent)}"
    return subject, html, text, start, end


def build_monthly_report(db: Session, today: date | None = None) -> tuple[str, str, str, date, date]:
    today = today or date.today()
    # If run on the 1st, use previous month; otherwise current month-to-date for manual sends
    if today.day == 1:
        start, end = _month_bounds_for_report(today)
    else:
        start = today.replace(day=1)
        end = today
    month = summarize_period(db, start, end, top_n=10)
    settings = get_or_create_settings(db)
    income_est = settings.monthly_income_estimate or Decimal("0.00")
    income_for_rate = income_est if income_est > 0 else month.income
    rate = savings_rate(income_for_rate, month.spent)

    context = {
        "title": "Monthly Finance Summary",
        "start": start,
        "end": end,
        "spent": month.spent,
        "income_actual": month.income,
        "income_estimate": income_est,
        "savings_rate": rate,
        "balance": settings.current_balance,
        "top_categories": month.by_category,
        "largest": month.largest,
        "days_in_month": monthrange(start.year, start.month)[1],
    }
    html, text = _render("monthly", context)
    label = start.strftime("%B %Y")
    subject = f"Monthly Finance Summary — {label}"
    return subject, html, text, start, end


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

    subject, html, text, start, end = builders[period](db, today)
    status = "sent"
    error = None

    if send:
        try:
            send_email(_recipient(db), subject, html, text)
        except Exception as exc:  # noqa: BLE001 — persist failure for dashboard
            logger.exception("Failed to send %s report", period)
            status = "failed"
            error = str(exc)

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
