from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, inspect
from sqlalchemy.orm import Session, joinedload

logger = logging.getLogger(__name__)


def receipts_available(db: Session) -> bool:
    """True when the receipts tables exist (addon has been run at least once)."""
    try:
        return inspect(db.bind).has_table("receipts")
    except Exception:  # noqa: BLE001
        logger.debug("receipts table check failed", exc_info=True)
        return False


def _models():
    from addons.receipts.models import Receipt, ReceiptItem

    return Receipt, ReceiptItem


def list_receipt_sources(db: Session) -> list[dict[str, Any]]:
    """Active receipt sources with counts — for Spending discoverability."""
    if not receipts_available(db):
        return []
    try:
        Receipt, ReceiptItem = _models()
        rows = (
            db.query(
                Receipt.source,
                Receipt.merchant_key,
                func.count(Receipt.id.distinct()).label("receipt_count"),
                func.count(ReceiptItem.id).label("item_count"),
                func.max(Receipt.purchase_date).label("latest"),
            )
            .outerjoin(ReceiptItem, ReceiptItem.receipt_id == Receipt.id)
            .group_by(Receipt.source, Receipt.merchant_key)
            .order_by(func.max(Receipt.purchase_date).desc())
            .all()
        )
        out: list[dict[str, Any]] = []
        for source, merchant_key, receipt_count, item_count, latest in rows:
            display = (merchant_key or source or "Receipts").title()
            out.append(
                {
                    "source": source,
                    "merchant_key": merchant_key,
                    "display_name": display,
                    "receipt_count": int(receipt_count or 0),
                    "item_count": int(item_count or 0),
                    "latest": latest,
                    "href": f"/merchants/{display}",
                }
            )
        return out
    except Exception:  # noqa: BLE001
        logger.exception("list_receipt_sources failed")
        return []


def merchant_key_for_name(name: str | None) -> str | None:
    if not name:
        return None
    key = " ".join(name.strip().lower().split())
    if "lidl" in key:
        return "lidl"
    return None


def source_for_merchant_key(merchant_key: str) -> str | None:
    if merchant_key == "lidl":
        return "lidl"
    return None


def list_receipts_for_merchant(
    db: Session,
    merchant_name: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
) -> list[Any]:
    if not receipts_available(db):
        return []
    merchant_key = merchant_key_for_name(merchant_name)
    if not merchant_key:
        return []
    Receipt, _ReceiptItem = _models()
    q = db.query(Receipt).filter(Receipt.merchant_key == merchant_key)
    if start is not None:
        q = q.filter(Receipt.purchase_date >= start)
    if end is not None:
        q = q.filter(Receipt.purchase_date <= end)
    return (
        q.order_by(Receipt.purchased_at.desc(), Receipt.id.desc())
        .limit(limit)
        .all()
    )


def top_items_for_merchant(
    db: Session,
    merchant_name: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if not receipts_available(db):
        return []
    merchant_key = merchant_key_for_name(merchant_name)
    if not merchant_key:
        return []
    Receipt, ReceiptItem = _models()
    filters = [Receipt.merchant_key == merchant_key]
    if start is not None:
        filters.append(Receipt.purchase_date >= start)
    if end is not None:
        filters.append(Receipt.purchase_date <= end)

    rows = (
        db.query(
            ReceiptItem.product_id,
            ReceiptItem.description,
            func.sum(ReceiptItem.net_total).label("total"),
            func.sum(ReceiptItem.quantity).label("qty"),
            func.count(ReceiptItem.id).label("lines"),
        )
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .filter(and_(*filters))
        .group_by(ReceiptItem.product_id, ReceiptItem.description)
        .order_by(func.sum(ReceiptItem.net_total).desc())
        .limit(limit)
        .all()
    )
    out: list[dict[str, Any]] = []
    for product_id, description, total, qty, lines in rows:
        out.append(
            {
                "product_id": product_id,
                "description": description,
                "total": total or Decimal("0"),
                "quantity": qty or Decimal("0"),
                "lines": int(lines or 0),
                "item_key": product_id or description,
            }
        )
    return out


def get_receipt(db: Session, source: str, external_id: str) -> Any | None:
    if not receipts_available(db):
        return None
    Receipt, _ReceiptItem = _models()
    return (
        db.query(Receipt)
        .options(joinedload(Receipt.items))
        .filter(Receipt.source == source, Receipt.external_id == external_id)
        .one_or_none()
    )


def item_spend(
    db: Session,
    source: str,
    *,
    product_id: str | None = None,
    description: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any] | None:
    if not receipts_available(db):
        return None
    if not product_id and not description:
        return None
    Receipt, ReceiptItem = _models()
    filters = [Receipt.source == source]
    if product_id:
        filters.append(ReceiptItem.product_id == product_id)
    else:
        filters.append(ReceiptItem.description == description)
    if start is not None:
        filters.append(Receipt.purchase_date >= start)
    if end is not None:
        filters.append(Receipt.purchase_date <= end)

    lines = (
        db.query(ReceiptItem, Receipt)
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .filter(and_(*filters))
        .order_by(Receipt.purchased_at.desc())
        .all()
    )
    if not lines:
        return {
            "source": source,
            "product_id": product_id,
            "description": description,
            "total": Decimal("0"),
            "quantity": Decimal("0"),
            "count": 0,
            "lines": [],
            "by_month": [],
        }

    display_name = lines[0][0].description
    total = Decimal("0")
    qty = Decimal("0")
    by_month: dict[str, Decimal] = {}
    serialized = []
    for item, receipt in lines:
        total += item.net_total or Decimal("0")
        qty += item.quantity or Decimal("0")
        month = receipt.purchase_date.strftime("%Y-%m")
        by_month[month] = by_month.get(month, Decimal("0")) + (item.net_total or Decimal("0"))
        serialized.append(
            {
                "date": receipt.purchase_date,
                "purchased_at": receipt.purchased_at,
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "net_total": item.net_total,
                "receipt_external_id": receipt.external_id,
                "store_name": receipt.store_name,
            }
        )

    return {
        "source": source,
        "product_id": product_id or lines[0][0].product_id,
        "description": display_name,
        "total": total,
        "quantity": qty,
        "count": len(lines),
        "lines": serialized,
        "by_month": [
            {"month": m, "total": by_month[m]}
            for m in sorted(by_month.keys())
        ],
    }


def list_receipts(
    db: Session,
    source: str | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
) -> list[Any]:
    if not receipts_available(db):
        return []
    Receipt, _ReceiptItem = _models()
    q = db.query(Receipt)
    if source:
        q = q.filter(Receipt.source == source)
    if start is not None:
        q = q.filter(Receipt.purchase_date >= start)
    if end is not None:
        q = q.filter(Receipt.purchase_date <= end)
    return (
        q.order_by(Receipt.purchased_at.desc(), Receipt.id.desc())
        .limit(limit)
        .all()
    )


def merchant_receipt_context(
    db: Session,
    merchant_name: str,
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Soft enrichment payload for merchant pages. Never raises for missing addon."""
    empty = {
        "available": False,
        "source": None,
        "receipts": [],
        "top_items": [],
    }
    try:
        if not receipts_available(db):
            return empty
        merchant_key = merchant_key_for_name(merchant_name)
        if not merchant_key:
            return empty
        source = source_for_merchant_key(merchant_key)
        receipts = list_receipts_for_merchant(
            db, merchant_name, start=start, end=end, limit=30
        )
        items = top_items_for_merchant(
            db, merchant_name, start=start, end=end, limit=20
        )
        return {
            "available": bool(receipts or items),
            "source": source,
            "receipts": receipts,
            "top_items": items,
        }
    except Exception:  # noqa: BLE001 — never break merchant page
        logger.exception("Receipt enrichment failed for %s", merchant_name)
        return empty
