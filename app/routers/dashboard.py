from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ReportRun
from app.services.analytics import (
    NON_DISCRETIONARY_CATEGORIES,
    category_detail,
    pay_period_to_date_stats,
    previous_pay_period_stats,
    savings_rate,
)
from app.services.balance import get_or_create_settings
from app.services.bills import (
    create_bill,
    deactivate_bill,
    delete_bill,
    list_all_bills,
    update_bill,
)
from app.services.budgets import (
    budget_progress,
    create_budget,
    delete_budget,
    list_budgets,
    list_seen_categories,
    update_budget,
)
from app.services.forecast import build_forecast
from app.services.income import (
    create_income_rule,
    current_pay_period,
    delete_income_rule,
    list_all_rules,
    monthly_income_total,
)
from app.services.recurring import (
    accept_suggestion,
    detect_recurring,
    dismiss_suggestion,
    list_suggestions,
)
from app.services.reports import generate_and_send_report
from app.services.safe_spend import calculate_safe_spend
from app.services.sheets_sync import sync_transactions
from app.services.insights import (
    generate_insight,
    get_latest_insight,
    insights_configured,
)

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"£{amount:,.2f}"


def _parse_money(value: str) -> Decimal:
    cleaned = value.replace("£", "").replace(",", "").strip() or "0"
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value}") from exc


def _as_date(value: date | datetime | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _shortdate(value: date | datetime | str | None) -> str:
    parsed = _as_date(value)
    if parsed is None:
        return "—"
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def _shortdatetime(value: datetime | date | str | None) -> str:
    if value is None or value == "":
        return "never"
    if isinstance(value, datetime):
        return f"{value.day} {value.strftime('%b %Y, %H:%M')}"
    if isinstance(value, date):
        return _shortdate(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return f"{parsed.day} {parsed.strftime('%b %Y, %H:%M')}"
    except ValueError:
        return text


def _urlquote(value: str | None) -> str:
    return quote(str(value or ""), safe="")


templates.env.filters["money"] = _money
templates.env.filters["shortdate"] = _shortdate
templates.env.filters["shortdatetime"] = _shortdatetime
templates.env.filters["urlquote"] = _urlquote


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    safe = calculate_safe_spend(db)
    period = current_pay_period(db)
    stats = pay_period_to_date_stats(db)
    income = monthly_income_total(db)
    rate = savings_rate(income, stats.spent)
    insight = get_latest_insight(db, ok_only=True) if insights_configured() else None
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active": "overview",
            "settings": settings,
            "safe": safe,
            "period": period,
            "mtd": stats,
            "income": income,
            "savings_rate": rate,
            "insights_enabled": insights_configured(),
            "insight": insight,
            "sync_message": request.query_params.get("sync"),
            "sync_error": request.query_params.get("error"),
            "insight_error": request.query_params.get("insight_error"),
            "insight_message": request.query_params.get("insight"),
        },
    )


@router.post("/insights/refresh")
def refresh_insights(db: Session = Depends(get_db)):
    if not insights_configured():
        return RedirectResponse(
            url=f"/?insight_error={quote('Gemini API key not configured')}",
            status_code=303,
        )
    try:
        view = generate_insight(db, force=True)
        if view.ok:
            return RedirectResponse(url="/?insight=updated", status_code=303)
        err = view.error or "Insight generation failed"
        return RedirectResponse(url=f"/?insight_error={quote(err)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/?insight_error={quote(str(exc))}",
            status_code=303,
        )


@router.post("/sync")
def sync_now(db: Session = Depends(get_db)):
    try:
        result = sync_transactions(db)
        msg = quote(f"{result.message}. Balance {_money(result.current_balance)}")
        return RedirectResponse(url=f"/?sync={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?error={quote(str(exc))}", status_code=303)


@router.get("/spending", response_class=HTMLResponse)
def spending(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    period = current_pay_period(db, today)
    mtd = pay_period_to_date_stats(db, today, top_n=8)
    prev = previous_pay_period_stats(db, today, top_n=8)
    budgets = budget_progress(db, today)
    return templates.TemplateResponse(
        request,
        "spending.html",
        {
            "active": "spending",
            "period": period,
            "mtd": mtd,
            "prev": prev,
            "budgets": budgets,
        },
    )


@router.get("/categories/{name}", response_class=HTMLResponse)
def category_page(request: Request, name: str, db: Session = Depends(get_db)):
    today = date.today()
    category_name = unquote(name).strip() or "Uncategorised"
    period = current_pay_period(db, today)
    # Fixed Monzo categories are excluded from discretionary rankings; still show them here.
    cat_key = category_name.strip().lower().replace("_", " ")
    discretionary = cat_key not in NON_DISCRETIONARY_CATEGORIES
    detail = category_detail(
        db,
        category_name,
        period.start,
        today,
        discretionary=discretionary,
        top_n=10,
        compare_previous=True,
    )
    trend_start = today - timedelta(days=183)
    trend = category_detail(
        db,
        category_name,
        trend_start,
        today,
        discretionary=discretionary,
        top_n=1,
        compare_previous=False,
    )
    budget = next(
        (b for b in budget_progress(db, today) if b.category == category_name),
        None,
    )
    return templates.TemplateResponse(
        request,
        "category_detail.html",
        {
            "active": "spending",
            "period": period,
            "category_name": category_name,
            "detail": detail,
            "by_month": trend["by_month"] if trend else [],
            "budget": budget,
        },
    )


@router.get("/bills", response_class=HTMLResponse)
def bills_page(request: Request, db: Session = Depends(get_db)):
    suggestions = list_suggestions(db)
    bills = list_all_bills(db)
    edit_raw = request.query_params.get("edit", "").strip()
    edit_bill = None
    if edit_raw.isdigit():
        edit_id = int(edit_raw)
        edit_bill = next((b for b in bills if b.id == edit_id), None)
    return templates.TemplateResponse(
        request,
        "bills.html",
        {
            "active": "bills",
            "bills": bills,
            "edit_bill": edit_bill,
            "suggestions": suggestions,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/bills")
def bills_create(
    name: str = Form(...),
    amount: str = Form(...),
    frequency: str = Form("monthly"),
    due_day: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    next_due_date: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        due_day_val = int(due_day) if due_day.strip() else None
        explicit = date.fromisoformat(next_due_date) if next_due_date.strip() else None
        create_bill(
            db,
            name=name,
            amount=_parse_money(amount),
            frequency=frequency,
            due_day=due_day_val,
            next_due_date=explicit,
            category=category or None,
            notes=notes or None,
        )
        return RedirectResponse(url="/bills?message=Bill+added", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/bills?error={quote(str(exc))}", status_code=303)


@router.post("/bills/{bill_id}/update")
def bills_update(
    bill_id: int,
    name: str = Form(...),
    amount: str = Form(...),
    frequency: str = Form(...),
    due_day: str = Form(""),
    next_due_date: str = Form(...),
    category: str = Form(""),
    notes: str = Form(""),
    active: str = Form("1"),
    db: Session = Depends(get_db),
):
    try:
        due_day_val = int(due_day) if due_day.strip() else None
        row = update_bill(
            db,
            bill_id,
            name=name,
            amount=_parse_money(amount),
            frequency=frequency,
            due_day=due_day_val,
            next_due_date=date.fromisoformat(next_due_date),
            category=category or None,
            notes=notes or None,
            active=active == "1",
        )
        if not row:
            return RedirectResponse(url="/bills?error=Bill+not+found", status_code=303)
        return RedirectResponse(url="/bills?message=Bill+updated", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/bills?error={quote(str(exc))}", status_code=303)


@router.post("/bills/{bill_id}/deactivate")
def bills_deactivate(bill_id: int, db: Session = Depends(get_db)):
    deactivate_bill(db, bill_id)
    return RedirectResponse(url="/bills?message=Bill+deactivated", status_code=303)


@router.post("/bills/{bill_id}/delete")
def bills_delete(bill_id: int, db: Session = Depends(get_db)):
    delete_bill(db, bill_id)
    return RedirectResponse(url="/bills?message=Bill+deleted", status_code=303)


@router.post("/bills/suggestions/scan")
def bills_scan(db: Session = Depends(get_db)):
    found = detect_recurring(db)
    return RedirectResponse(
        url=f"/bills?message={quote(f'Found {len(found)} suggestions')}",
        status_code=303,
    )


@router.post("/bills/suggestions/{suggestion_id}/accept")
def bills_accept(suggestion_id: int, db: Session = Depends(get_db)):
    bill = accept_suggestion(db, suggestion_id)
    if not bill:
        return RedirectResponse(url="/bills?error=Suggestion+not+found", status_code=303)
    return RedirectResponse(
        url=f"/bills?message={quote(f'Accepted {bill.name}')}", status_code=303
    )


@router.post("/bills/suggestions/{suggestion_id}/dismiss")
def bills_dismiss(suggestion_id: int, db: Session = Depends(get_db)):
    dismiss_suggestion(db, suggestion_id)
    return RedirectResponse(url="/bills?message=Suggestion+dismissed", status_code=303)


@router.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request, db: Session = Depends(get_db)):
    forecast = build_forecast(db, days=30)
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {"active": "forecast", "forecast": forecast},
    )


@router.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "budgets.html",
        {
            "active": "budgets",
            "budgets": list_budgets(db),
            "progress": budget_progress(db),
            "categories": list_seen_categories(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/budgets")
def budgets_create(
    category: str = Form(...),
    monthly_limit: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        create_budget(db, category=category, monthly_limit=_parse_money(monthly_limit))
        return RedirectResponse(url="/budgets?message=Budget+added", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/budgets?error={quote(str(exc))}", status_code=303)


@router.post("/budgets/{budget_id}/update")
def budgets_update(
    budget_id: int,
    category: str = Form(...),
    monthly_limit: str = Form(...),
    active: str = Form("1"),
    db: Session = Depends(get_db),
):
    try:
        row = update_budget(
            db,
            budget_id,
            category=category,
            monthly_limit=_parse_money(monthly_limit),
            active=active == "1",
        )
        if not row:
            return RedirectResponse(url="/budgets?error=Not+found", status_code=303)
        return RedirectResponse(url="/budgets?message=Budget+updated", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/budgets?error={quote(str(exc))}", status_code=303)


@router.post("/budgets/{budget_id}/delete")
def budgets_delete(budget_id: int, db: Session = Depends(get_db)):
    delete_budget(db, budget_id)
    return RedirectResponse(url="/budgets?message=Budget+deleted", status_code=303)


@router.get("/reports", response_class=HTMLResponse)
def reports_list(request: Request, db: Session = Depends(get_db)):
    rows = (
        db.query(ReportRun)
        .order_by(ReportRun.sent_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "active": "reports",
            "reports": rows,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(ReportRun, report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(
        request,
        "report_detail.html",
        {"active": "reports", "report": row},
    )


@router.post("/reports/send")
def send_report(
    period: str = Form(...),
    send_email_flag: str = Form("1"),
    db: Session = Depends(get_db),
):
    if period not in {"daily", "weekly", "monthly"}:
        return RedirectResponse(url="/reports?error=Invalid+period", status_code=303)
    try:
        run = generate_and_send_report(db, period, send=send_email_flag == "1")
        msg = quote(f"Generated {period} report #{run.id} ({run.status})")
        return RedirectResponse(url=f"/reports?message={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/reports?error={quote(str(exc))}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "settings": settings,
            "income_rules": list_all_rules(db),
            "monthly_income": monthly_income_total(db),
            "saved": request.query_params.get("saved"),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/settings/income")
def settings_add_income(
    name: str = Form(...),
    amount: str = Form(...),
    frequency: str = Form("monthly"),
    rule_type: str = Form(...),
    rule_value: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        value = int(rule_value) if rule_value.strip() else None
        if rule_type == "last_friday":
            value = None
        create_income_rule(
            db,
            name=name,
            amount=_parse_money(amount),
            frequency=frequency,
            rule_type=rule_type,
            rule_value=value,
        )
        return RedirectResponse(url="/settings?message=Income+rule+added", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings?error={quote(str(exc))}", status_code=303
        )


@router.post("/settings/income/{rule_id}/delete")
def settings_delete_income(rule_id: int, db: Session = Depends(get_db)):
    delete_income_rule(db, rule_id)
    return RedirectResponse(url="/settings?message=Income+rule+removed", status_code=303)
