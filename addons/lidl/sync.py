from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from addons.lidl.client import LidlApiError, LidlAuthError, LidlClient
from addons.lidl.parse import currency_code, parse_money, parse_receipt_items
from addons.receipts.linking import find_matching_transaction_id
from addons.receipts.models import Receipt, ReceiptItem, SourceAuth

logger = logging.getLogger(__name__)

SOURCE = "lidl"
MERCHANT_KEY = "lidl"


@dataclass
class SyncResult:
    listed: int = 0
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    linked: int = 0
    errors: int = 0
    message: str = ""


def _parse_purchased_at(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_or_bootstrap_auth(db: Session, bootstrap_refresh_token: str) -> SourceAuth:
    row = db.get(SourceAuth, SOURCE)
    if row is None:
        row = SourceAuth(source=SOURCE)
        db.add(row)
        db.flush()
    if not row.refresh_token and bootstrap_refresh_token:
        row.refresh_token = bootstrap_refresh_token.strip()
        db.commit()
        db.refresh(row)
    return row


def persist_tokens(
    db: Session,
    auth: SourceAuth,
    *,
    refresh_token: str,
    access_token: str | None = None,
    access_expires_at: datetime | None = None,
    clear_error: bool = False,
) -> None:
    """Persist rotated refresh token BEFORE further API work."""
    auth.refresh_token = refresh_token
    if access_token is not None:
        auth.access_token = access_token
    if access_expires_at is not None:
        auth.access_expires_at = access_expires_at
    if clear_error:
        auth.last_error = None
    db.commit()


def mark_auth_error(db: Session, auth: SourceAuth, message: str) -> None:
    auth.last_error = message[:2000]
    db.commit()


def mark_auth_success(db: Session, auth: SourceAuth) -> None:
    auth.last_success_at = datetime.now(timezone.utc)
    auth.last_error = None
    db.commit()


def known_external_ids(db: Session) -> set[str]:
    rows = db.query(Receipt.external_id).filter(Receipt.source == SOURCE).all()
    return {r[0] for r in rows}


def upsert_ticket(db: Session, payload: dict[str, Any]) -> tuple[Receipt, bool]:
    external_id = str(payload.get("id") or "").strip()
    if not external_id:
        raise ValueError("Ticket payload missing id")

    existing = (
        db.query(Receipt)
        .filter(Receipt.source == SOURCE, Receipt.external_id == external_id)
        .one_or_none()
    )
    purchased_at = _parse_purchased_at(payload.get("date"))
    purchase_date = purchased_at.date()
    total = parse_money(payload.get("totalAmount"))
    currency = currency_code(payload.get("currency"))
    store = payload.get("store") or {}
    if not isinstance(store, dict):
        store = {}

    items = parse_receipt_items(payload)
    raw_json = json.dumps(payload, ensure_ascii=False, default=str)

    created = existing is None
    receipt = existing or Receipt(
        source=SOURCE,
        external_id=external_id,
        purchased_at=purchased_at,
        purchase_date=purchase_date,
        total_amount=total,
        currency=currency,
        merchant_key=MERCHANT_KEY,
    )
    receipt.purchased_at = purchased_at
    receipt.purchase_date = purchase_date
    receipt.total_amount = total
    receipt.currency = currency
    receipt.merchant_key = MERCHANT_KEY
    receipt.store_id = str(store.get("id") or "") or None
    receipt.store_name = str(store.get("name") or "") or None
    receipt.store_locality = str(store.get("locality") or "") or None
    receipt.store_postcode = str(store.get("postalCode") or "") or None
    receipt.raw_json = raw_json
    receipt.synced_at = datetime.now(timezone.utc)

    if created:
        db.add(receipt)
        db.flush()
    else:
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt.id).delete()

    for item in items:
        db.add(
            ReceiptItem(
                receipt_id=receipt.id,
                product_id=item.product_id,
                description=item.description[:512],
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                discount_total=item.discount_total,
                net_total=item.net_total,
                is_weight=item.is_weight,
                tax_type=item.tax_type,
            )
        )

    if receipt.transaction_id is None:
        tx_id = find_matching_transaction_id(
            db,
            purchase_date=purchase_date,
            total_amount=total,
            merchant_key=MERCHANT_KEY,
        )
        if tx_id:
            receipt.transaction_id = tx_id

    db.commit()
    db.refresh(receipt)
    return receipt, created


def sync_lidl_receipts(
    db: Session,
    client: LidlClient,
    *,
    bootstrap_refresh_token: str = "",
    full: bool = False,
    max_pages: int | None = None,
) -> SyncResult:
    result = SyncResult()
    auth = get_or_bootstrap_auth(db, bootstrap_refresh_token)
    if not auth.refresh_token:
        result.message = "No Lidl refresh token — set LIDL_REFRESH_TOKEN"
        result.errors = 1
        return result

    client.set_tokens(
        refresh_token=auth.refresh_token,
        access_token=auth.access_token,
        access_expires_at=auth.access_expires_at,
    )

    try:
        tokens = client.refresh_access_token()
        persist_tokens(
            db,
            auth,
            refresh_token=tokens["refresh_token"],
            access_token=tokens["access_token"],
            access_expires_at=tokens["access_expires_at"],
            clear_error=True,
        )
        # Re-bind client to DB-persisted token in case of further rotations on 401 retry
        client.set_tokens(
            refresh_token=tokens["refresh_token"],
            access_token=tokens["access_token"],
            access_expires_at=tokens["access_expires_at"],
        )
    except LidlAuthError as exc:
        mark_auth_error(db, auth, str(exc))
        result.message = f"Auth failed: {exc}"
        result.errors = 1
        return result
    except LidlApiError as exc:
        mark_auth_error(db, auth, str(exc))
        result.message = f"Token API error: {exc}"
        result.errors = 1
        return result

    existing = set() if full else known_external_ids(db)
    page = 1
    total_pages = 1
    stop_listing = False

    try:
        while page <= total_pages and not stop_listing:
            if max_pages is not None and page > max_pages:
                break
            payload = client.list_tickets_page(page)
            # Persist token if 401 retry rotated it
            if client.refresh_token and client.refresh_token != auth.refresh_token:
                persist_tokens(
                    db,
                    auth,
                    refresh_token=client.refresh_token,
                    access_token=client.access_token,
                    access_expires_at=client.access_expires_at,
                )

            tickets = payload.get("tickets") or []
            size = int(payload.get("size") or len(tickets) or 20)
            total_count = int(payload.get("totalCount") or 0)
            if size > 0 and total_count > 0:
                total_pages = max(1, (total_count + size - 1) // size)

            if not tickets:
                break

            for summary in tickets:
                result.listed += 1
                ticket_id = str(summary.get("id") or "").strip()
                if not ticket_id:
                    continue
                if ticket_id in existing and not full:
                    result.skipped += 1
                    # Newest-first: once we hit a known id, older ones are known too
                    stop_listing = True
                    break

                try:
                    detail = client.get_ticket(ticket_id)
                    if client.refresh_token and client.refresh_token != auth.refresh_token:
                        persist_tokens(
                            db,
                            auth,
                            refresh_token=client.refresh_token,
                            access_token=client.access_token,
                            access_expires_at=client.access_expires_at,
                        )
                    result.fetched += 1
                    receipt, created = upsert_ticket(db, detail)
                    if created:
                        result.inserted += 1
                    else:
                        result.skipped += 1
                    if receipt.transaction_id:
                        result.linked += 1
                    existing.add(ticket_id)
                except Exception as exc:  # noqa: BLE001 — continue syncing other tickets
                    logger.exception("Failed ticket %s", ticket_id)
                    result.errors += 1
                    mark_auth_error(db, auth, f"ticket {ticket_id}: {exc}")

            page += 1

        mark_auth_success(db, auth)
        # Final token persist after any mid-sync rotations
        if client.refresh_token:
            persist_tokens(
                db,
                auth,
                refresh_token=client.refresh_token,
                access_token=client.access_token,
                access_expires_at=client.access_expires_at,
            )
        result.message = (
            f"listed={result.listed} fetched={result.fetched} "
            f"inserted={result.inserted} skipped={result.skipped} "
            f"linked={result.linked} errors={result.errors}"
        )
    except LidlAuthError as exc:
        mark_auth_error(db, auth, str(exc))
        result.errors += 1
        result.message = f"Auth failed mid-sync: {exc}"
    except LidlApiError as exc:
        mark_auth_error(db, auth, str(exc))
        result.errors += 1
        result.message = f"API error: {exc}"

    return result
