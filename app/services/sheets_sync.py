from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transaction
from app.schemas import SyncResult
from app.services.balance import apply_balance_delta, get_or_create_settings, mark_synced

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

HEADER_ALIASES: dict[str, str] = {
    "transaction id": "monzo_transaction_id",
    "date": "date",
    "time": "time",
    "type": "type",
    "name": "merchant",
    "category": "category",
    "amount": "amount",
    "currency": "currency",
    "notes": "notes",
    "notes and #tags": "notes",
    "description": "description",
}


def _normalize_header(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # Google Sheets serial date as number string
    try:
        serial = float(value)
        # Sheets epoch is 1899-12-30
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(serial))
    except (ValueError, OverflowError):
        return None


def _parse_time(value: str) -> time | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    try:
        # Fraction of day
        fraction = float(value)
        total_seconds = int(round(fraction * 86400)) % 86400
        hours, rem = divmod(total_seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        return time(hours, minutes, seconds)
    except (ValueError, OverflowError):
        return None


def _parse_amount(value: str) -> Decimal | None:
    value = value.strip().replace("£", "").replace(",", "")
    if not value:
        return None
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _build_column_map(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        key = HEADER_ALIASES.get(_normalize_header(str(cell)))
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def fetch_sheet_rows() -> list[list[str]]:
    settings = get_settings()
    if not settings.google_sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not configured")
    creds_path = Path(settings.google_credentials_file)
    if not creds_path.is_file():
        raise FileNotFoundError(
            f"Google credentials not found at {settings.google_credentials_file}"
        )

    credentials = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=SCOPES
    )
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=settings.google_sheet_id, range=settings.google_sheet_range)
        .execute()
    )
    return result.get("values", [])


def sync_transactions(db: Session) -> SyncResult:
    rows = fetch_sheet_rows()
    if not rows:
        mark_synced(db)
        settings = get_or_create_settings(db)
        return SyncResult(
            inserted=0,
            updated=0,
            balance_delta=Decimal("0.00"),
            current_balance=settings.current_balance,
            message="Sheet returned no rows",
        )

    column_map = _build_column_map([str(c) for c in rows[0]])
    required = {"monzo_transaction_id", "date", "amount"}
    missing = required - set(column_map)
    if missing:
        raise ValueError(f"Sheet header missing required columns: {sorted(missing)}")

    existing = {
        t.monzo_transaction_id: t
        for t in db.query(Transaction).all()
    }

    account = get_or_create_settings(db)
    # Only apply amounts for txs after the balance was seeded. Historical
    # backfill must not rewrite a manually seeded Monzo balance.
    balance_as_of = account.balance_updated_at
    if balance_as_of is not None and balance_as_of.tzinfo is None:
        balance_as_of = balance_as_of.replace(tzinfo=timezone.utc)
    app_tz = ZoneInfo(get_settings().app_tz)

    inserted = 0
    updated = 0
    balance_delta = Decimal("0.00")
    skipped_balance = 0

    for raw in rows[1:]:
        row = [str(c) if c is not None else "" for c in raw]
        tx_id = _cell(row, column_map.get("monzo_transaction_id"))
        if not tx_id:
            continue

        tx_date = _parse_date(_cell(row, column_map.get("date")))
        amount = _parse_amount(_cell(row, column_map.get("amount")))
        if tx_date is None or amount is None:
            logger.warning("Skipping row with bad date/amount: %s", tx_id)
            continue

        tx_time = _parse_time(_cell(row, column_map.get("time"))) if "time" in column_map else None
        tx_type = _cell(row, column_map.get("type")) or None
        merchant = _cell(row, column_map.get("merchant")) or None
        category = _cell(row, column_map.get("category")) or None
        currency = _cell(row, column_map.get("currency")) or "GBP"
        notes = _cell(row, column_map.get("notes")) or None
        description = _cell(row, column_map.get("description")) or None

        current = existing.get(tx_id)
        if current is None:
            db.add(
                Transaction(
                    monzo_transaction_id=tx_id,
                    date=tx_date,
                    time=tx_time,
                    type=tx_type,
                    merchant=merchant,
                    description=description,
                    category=category,
                    amount=amount,
                    currency=currency,
                    notes=notes,
                )
            )
            inserted += 1
            if balance_as_of is None:
                skipped_balance += 1
            else:
                tx_dt = datetime.combine(
                    tx_date, tx_time or time.min, tzinfo=app_tz
                ).astimezone(timezone.utc)
                if tx_dt > balance_as_of:
                    balance_delta += amount
                else:
                    skipped_balance += 1
            continue

        dirty = False
        for field, value in (
            ("date", tx_date),
            ("time", tx_time),
            ("type", tx_type),
            ("merchant", merchant),
            ("description", description),
            ("category", category),
            ("currency", currency),
            ("notes", notes),
        ):
            if getattr(current, field) != value:
                setattr(current, field, value)
                dirty = True
        # Amount changes are unusual; do not re-apply to balance to avoid drift.
        if current.amount != amount:
            logger.warning(
                "Amount changed for %s (%s -> %s); balance not adjusted",
                tx_id,
                current.amount,
                amount,
            )
            current.amount = amount
            dirty = True
        if dirty:
            updated += 1

    if balance_delta != 0:
        apply_balance_delta(db, balance_delta)
    else:
        db.commit()

    settings = mark_synced(db)
    msg = f"Synced {inserted} new, {updated} updated"
    if balance_as_of is None and inserted:
        msg += " (balance unchanged — set Current balance in Settings to match Monzo)"
    elif skipped_balance and balance_delta == 0:
        msg += f" ({skipped_balance} historical txs ignored for balance)"
    elif balance_delta != 0:
        msg += f" (balance {balance_delta:+})"
    return SyncResult(
        inserted=inserted,
        updated=updated,
        balance_delta=balance_delta,
        current_balance=settings.current_balance,
        message=msg,
    )
