from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from sqlalchemy.orm import Session

from app.models import BillSuggestion, Transaction, UpcomingBill
from app.services.bills import create_bill


def _normalize_merchant(name: str | None) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def detect_recurring(db: Session, *, today: date | None = None) -> list[BillSuggestion]:
    """Scan ~6 months of outflows and upsert suggested bills."""
    today = today or date.today()
    start = today - timedelta(days=183)

    active_merchants = {
        _normalize_merchant(b.name)
        for b in db.query(UpcomingBill).filter(UpcomingBill.active.is_(True)).all()
    }

    txs = (
        db.query(Transaction)
        .filter(
            Transaction.date >= start,
            Transaction.date <= today,
            Transaction.amount < 0,
            Transaction.merchant.isnot(None),
        )
        .order_by(Transaction.date)
        .all()
    )

    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    display_name: dict[str, str] = {}
    for tx in txs:
        key = _normalize_merchant(tx.merchant)
        if not key:
            continue
        by_merchant[key].append(tx)
        display_name[key] = tx.merchant or key

    existing = {
        _normalize_merchant(s.merchant): s
        for s in db.query(BillSuggestion).all()
    }

    results: list[BillSuggestion] = []

    for key, rows in by_merchant.items():
        if key in active_merchants:
            continue
        if len(rows) < 3:
            continue

        dates = sorted({r.date for r in rows})
        if len(dates) < 3:
            continue

        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        med_gap = median(gaps)
        amounts = [abs(r.amount) for r in rows]
        typical = (sum(amounts) / Decimal(len(amounts))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        frequency = None
        confidence = Decimal("0")
        if 25 <= med_gap <= 35:
            frequency = "monthly"
            confidence = Decimal("80")
        elif 6 <= med_gap <= 9:
            frequency = "weekly"
            confidence = Decimal("75")
        else:
            continue

        # Tighter gaps → higher confidence
        spread = max(gaps) - min(gaps)
        if spread <= 5:
            confidence += Decimal("10")
        confidence = min(confidence, Decimal("99"))

        prev = existing.get(key)
        if prev and prev.status in {"accepted", "dismissed"}:
            # Refresh amount/last_seen but keep status
            prev.typical_amount = typical
            prev.last_seen = dates[-1]
            prev.frequency_guess = frequency
            prev.confidence = confidence
            results.append(prev)
            continue

        if prev:
            prev.typical_amount = typical
            prev.last_seen = dates[-1]
            prev.frequency_guess = frequency
            prev.confidence = confidence
            prev.status = "suggested"
            results.append(prev)
        else:
            row = BillSuggestion(
                merchant=display_name[key],
                typical_amount=typical,
                frequency_guess=frequency,
                last_seen=dates[-1],
                confidence=confidence,
                status="suggested",
            )
            db.add(row)
            results.append(row)

    db.commit()
    for r in results:
        db.refresh(r)
    return (
        db.query(BillSuggestion)
        .filter(BillSuggestion.status == "suggested")
        .order_by(BillSuggestion.confidence.desc(), BillSuggestion.merchant)
        .all()
    )


def list_suggestions(db: Session, *, status: str = "suggested") -> list[BillSuggestion]:
    return (
        db.query(BillSuggestion)
        .filter(BillSuggestion.status == status)
        .order_by(BillSuggestion.confidence.desc(), BillSuggestion.merchant)
        .all()
    )


def accept_suggestion(db: Session, suggestion_id: int) -> UpcomingBill | None:
    sug = db.get(BillSuggestion, suggestion_id)
    if not sug or sug.status != "suggested":
        return None

    due_day = 1
    if sug.frequency_guess == "weekly":
        due_day = sug.last_seen.weekday()
    else:
        due_day = min(28, sug.last_seen.day)

    bill = create_bill(
        db,
        name=sug.merchant,
        amount=sug.typical_amount,
        frequency=sug.frequency_guess,
        due_day=due_day,
        category=None,
        notes="Accepted from recurring suggestion",
    )
    sug.status = "accepted"
    db.commit()
    return bill


def dismiss_suggestion(db: Session, suggestion_id: int) -> bool:
    sug = db.get(BillSuggestion, suggestion_id)
    if not sug:
        return False
    sug.status = "dismissed"
    db.commit()
    return True
