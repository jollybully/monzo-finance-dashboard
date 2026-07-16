from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class SettingsUpdate(BaseModel):
    current_balance: Decimal | None = None
    payday_day: int | None = Field(default=None, ge=1, le=28)
    monthly_income_estimate: Decimal | None = None
    reserved_buffer: Decimal | None = None
    email_to: str | None = None


class AccountSettingsOut(BaseModel):
    current_balance: Decimal
    balance_updated_at: datetime | None
    payday_day: int
    monthly_income_estimate: Decimal
    reserved_buffer: Decimal
    email_to: str | None
    last_sync_at: datetime | None

    model_config = {"from_attributes": True}


class SyncResult(BaseModel):
    inserted: int
    updated: int
    balance_delta: Decimal
    current_balance: Decimal
    message: str


class SafeSpendOut(BaseModel):
    current_balance: Decimal
    reserved_buffer: Decimal
    available: Decimal
    days_until_payday: int
    next_payday: date
    safe_daily_spend: Decimal


class ReportRunOut(BaseModel):
    id: int
    period: str
    period_start: date
    period_end: date
    subject: str
    sent_at: datetime
    status: str
    error: str | None

    model_config = {"from_attributes": True}
