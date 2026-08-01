from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session


def normalize_merchant(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def find_matching_transaction_id(
    db: Session,
    *,
    purchase_date: date,
    total_amount: Decimal,
    merchant_key: str,
    day_slack: int = 1,
) -> int | None:
    """Best-effort link: same absolute amount, nearby date, merchant contains key."""
    if not merchant_key or total_amount is None:
        return None

    start = purchase_date - timedelta(days=day_slack)
    end = purchase_date + timedelta(days=day_slack)
    amount = abs(total_amount).quantize(Decimal("0.01"))
    pattern = f"%{merchant_key}%"

    rows = db.execute(
        text(
            """
            SELECT id
            FROM transactions
            WHERE date >= :start
              AND date <= :end
              AND ABS(amount) = :amount
              AND LOWER(COALESCE(merchant, '')) LIKE :pattern
            ORDER BY ABS(date - CAST(:purchase_date AS date)) ASC, id DESC
            LIMIT 1
            """
        ),
        {
            "start": start,
            "end": end,
            "amount": amount,
            "pattern": pattern,
            "purchase_date": purchase_date,
        },
    ).fetchall()
    if not rows:
        return None
    return int(rows[0][0])
