from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountSettings


def get_or_create_settings(db: Session) -> AccountSettings:
    settings_row = db.query(AccountSettings).order_by(AccountSettings.id).first()
    if settings_row:
        return settings_row

    defaults = get_settings()
    settings_row = AccountSettings(
        current_balance=Decimal("0.00"),
        payday_day=28,
        monthly_income_estimate=Decimal("0.00"),
        reserved_buffer=Decimal(str(defaults.reserved_buffer_default)),
    )
    db.add(settings_row)
    db.commit()
    db.refresh(settings_row)
    return settings_row


def seed_balance(db: Session, amount: Decimal) -> AccountSettings:
    row = get_or_create_settings(db)
    row.current_balance = amount
    row.balance_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def apply_balance_delta(db: Session, delta: Decimal) -> AccountSettings:
    row = get_or_create_settings(db)
    row.current_balance = (row.current_balance or Decimal("0.00")) + delta
    row.balance_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def mark_synced(db: Session) -> AccountSettings:
    row = get_or_create_settings(db)
    row.last_sync_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row
