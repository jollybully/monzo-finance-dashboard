from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.models import Budget, Transaction


@dataclass
class BudgetProgress:
    id: int
    category: str
    monthly_limit: Decimal
    spent: Decimal
    remaining: Decimal
    pct: Decimal
    over: bool


def list_seen_categories(db: Session) -> list[str]:
    rows = (
        db.query(distinct(Transaction.category))
        .filter(Transaction.category.isnot(None), Transaction.category != "")
        .order_by(Transaction.category)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def list_budgets(db: Session, *, active_only: bool = False) -> list[Budget]:
    q = db.query(Budget)
    if active_only:
        q = q.filter(Budget.active.is_(True))
    return q.order_by(Budget.category).all()


def budget_progress(db: Session, today: date | None = None) -> list[BudgetProgress]:
    today = today or date.today()
    start = today.replace(day=1)
    from app.services.analytics import (
        _signed_spend_contribution,
        is_income_credit,
    )

    txs = (
        db.query(Transaction)
        .filter(Transaction.date >= start, Transaction.date <= today)
        .all()
    )
    spent_map: dict[str, Decimal] = {}
    for tx in txs:
        amount = tx.amount or Decimal("0.00")
        if amount == 0 or is_income_credit(tx):
            continue
        cat = tx.category or "Uncategorised"
        spent_map[cat] = spent_map.get(cat, Decimal("0.00")) + _signed_spend_contribution(
            amount
        )

    result: list[BudgetProgress] = []
    for b in list_budgets(db, active_only=True):
        spent = spent_map.get(b.category, Decimal("0.00"))
        if spent < 0:
            spent = Decimal("0.00")
        remaining = b.monthly_limit - spent
        pct = (
            (spent / b.monthly_limit * Decimal("100")).quantize(Decimal("0.1"))
            if b.monthly_limit > 0
            else Decimal("0")
        )
        result.append(
            BudgetProgress(
                id=b.id,
                category=b.category,
                monthly_limit=b.monthly_limit,
                spent=spent,
                remaining=remaining,
                pct=pct,
                over=spent > b.monthly_limit,
            )
        )
    return result


def over_budget(db: Session, today: date | None = None) -> list[BudgetProgress]:
    return [b for b in budget_progress(db, today) if b.over]


def create_budget(
    db: Session, *, category: str, monthly_limit: Decimal, active: bool = True
) -> Budget:
    row = Budget(
        category=category.strip(),
        monthly_limit=monthly_limit,
        active=active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_budget(
    db: Session,
    budget_id: int,
    *,
    category: str,
    monthly_limit: Decimal,
    active: bool,
) -> Budget | None:
    row = db.get(Budget, budget_id)
    if not row:
        return None
    row.category = category.strip()
    row.monthly_limit = monthly_limit
    row.active = active
    db.commit()
    db.refresh(row)
    return row


def delete_budget(db: Session, budget_id: int) -> bool:
    row = db.get(Budget, budget_id)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True
