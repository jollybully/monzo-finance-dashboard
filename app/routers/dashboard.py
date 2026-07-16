from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ReportRun
from app.services.analytics import month_to_date_stats, savings_rate, summarize_period
from app.services.balance import get_or_create_settings
from app.services.reports import generate_and_send_report
from app.services.safe_spend import calculate_safe_spend
from app.services.sheets_sync import sync_transactions

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"£{amount:,.2f}"


templates.env.filters["money"] = _money


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    safe = calculate_safe_spend(db)
    mtd = month_to_date_stats(db)
    income = settings.monthly_income_estimate or Decimal("0.00")
    rate = savings_rate(income, mtd.spent)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active": "overview",
            "settings": settings,
            "safe": safe,
            "mtd": mtd,
            "income": income,
            "savings_rate": rate,
            "sync_message": request.query_params.get("sync"),
            "sync_error": request.query_params.get("error"),
        },
    )


@router.post("/sync")
def sync_now(db: Session = Depends(get_db)):
    try:
        result = sync_transactions(db)
        msg = f"{result.message}. Balance {_money(result.current_balance)}"
        return RedirectResponse(url=f"/?sync={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?error={exc}", status_code=303)


@router.get("/spending", response_class=HTMLResponse)
def spending(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    mtd = month_to_date_stats(db, today)
    first = today.replace(day=1)
    prev_end = first - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    prev = summarize_period(db, prev_start, prev_end, top_n=8)
    return templates.TemplateResponse(
        request,
        "spending.html",
        {
            "active": "spending",
            "mtd": mtd,
            "prev": prev,
        },
    )


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
        msg = f"Generated {period} report #{run.id} ({run.status})"
        return RedirectResponse(url=f"/reports?message={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/reports?error={exc}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "settings": settings,
            "saved": request.query_params.get("saved"),
        },
    )
