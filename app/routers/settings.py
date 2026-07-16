from decimal import Decimal

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import AccountSettingsOut, SettingsUpdate
from app.services.balance import get_or_create_settings, seed_balance

router = APIRouter(tags=["settings"])


@router.get("/api/settings", response_model=AccountSettingsOut)
def api_get_settings(db: Session = Depends(get_db)) -> AccountSettingsOut:
    return AccountSettingsOut.model_validate(get_or_create_settings(db))


@router.patch("/api/settings", response_model=AccountSettingsOut)
def api_update_settings(
    payload: SettingsUpdate, db: Session = Depends(get_db)
) -> AccountSettingsOut:
    row = get_or_create_settings(db)
    data = payload.model_dump(exclude_unset=True)
    if "current_balance" in data and data["current_balance"] is not None:
        row = seed_balance(db, data["current_balance"])
        data.pop("current_balance")
    for key, value in data.items():
        if value is not None:
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return AccountSettingsOut.model_validate(row)


@router.post("/settings")
def form_update_settings(
    current_balance: str = Form(...),
    payday_day: int = Form(...),
    monthly_income_estimate: str = Form(...),
    reserved_buffer: str = Form(...),
    email_to: str = Form(""),
    db: Session = Depends(get_db),
):
    row = get_or_create_settings(db)
    seed_balance(db, Decimal(current_balance.replace("£", "").replace(",", "").strip() or "0"))
    row = get_or_create_settings(db)
    row.payday_day = max(1, min(28, payday_day))
    row.monthly_income_estimate = Decimal(
        monthly_income_estimate.replace("£", "").replace(",", "").strip() or "0"
    )
    row.reserved_buffer = Decimal(
        reserved_buffer.replace("£", "").replace(",", "").strip() or "0"
    )
    row.email_to = email_to.strip() or None
    db.commit()
    return RedirectResponse(url="/settings?saved=1", status_code=303)
