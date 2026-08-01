from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass
class ParsedItem:
    product_id: str | None
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    discount_total: Decimal = Decimal("0.00")
    net_total: Decimal = Decimal("0.00")
    is_weight: bool = False
    tax_type: str | None = None
    discounts: list[dict[str, str]] = field(default_factory=list)


_DATA_ATTR = re.compile(
    r'data-(?P<key>[\w-]+)="(?P<val>[^"]*)"',
    re.IGNORECASE,
)
_SPAN_RE = re.compile(
    r"<span(?P<attrs>[^>]*)>(?P<body>.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
_CLASS_RE = re.compile(r'\bclass="(?P<cls>[^"]*)"', re.IGNORECASE)
_MONEY_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_WEIGHT_DETAIL_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*kg\s*@",
    re.IGNORECASE,
)


def parse_money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value).strip().replace("\xa0", "").replace("£", "").replace(" ", "")
    text = text.replace("&pound;", "").replace("£", "")
    if not text:
        return Decimal("0.00")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) <= 2:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def parse_quantity(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("1")
    try:
        qty = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return Decimal("1")
    if qty <= 0:
        return Decimal("1")
    return qty


def currency_code(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("code") or "GBP")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "GBP"


def _finalize(item: ParsedItem) -> ParsedItem:
    if item.line_total == 0 and item.unit_price and item.quantity:
        item.line_total = (item.unit_price * item.quantity).quantize(Decimal("0.01"))
    item.net_total = (item.line_total - item.discount_total).quantize(Decimal("0.01"))
    if item.quantity != int(item.quantity) and item.quantity > 0:
        item.is_weight = True
    return item


def parse_items_line(items_line: list[dict[str, Any]]) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    for row in items_line:
        qty = parse_quantity(row.get("quantity"))
        unit = parse_money(row.get("currentUnitPrice"))
        line_total = parse_money(row.get("originalAmount"))
        if line_total == 0 and unit:
            line_total = (unit * qty).quantize(Decimal("0.01"))
        discounts = row.get("discounts") or []
        discount_total = Decimal("0.00")
        discount_meta: list[dict[str, str]] = []
        for d in discounts:
            amt = abs(parse_money(d.get("amount")))
            discount_total += amt
            discount_meta.append(
                {
                    "description": str(d.get("description") or ""),
                    "amount": str(amt),
                }
            )
        item = ParsedItem(
            product_id=str(row.get("codeInput") or "") or None,
            description=html_lib.unescape(str(row.get("name") or "Unknown")).strip(),
            quantity=qty,
            unit_price=unit,
            line_total=line_total,
            discount_total=discount_total.quantize(Decimal("0.01")),
            is_weight=bool(row.get("isWeight")),
            tax_type=str(row.get("taxGroupName") or "") or None,
            discounts=discount_meta,
        )
        items.append(_finalize(item))
    return items


def _line_total_from_body(body: str, unit: Decimal, qty: Decimal) -> Decimal:
    """Prefer the printed line total at the end of the article body."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", body))
    text = text.replace("\xa0", " ").strip()
    # Skip pure weight-detail lines ("1.020 kg @ £0.90/kg")
    if _WEIGHT_DETAIL_RE.search(text):
        return Decimal("0.00")
    # Trailing money before optional tax letter, e.g. "1.08 A" or "2.90 A"
    match = re.search(r"(\d+[.,]\d{2})\s*[A-Z]?\s*$", text)
    if match:
        return parse_money(match.group(1))
    if unit and qty:
        return (unit * qty).quantize(Decimal("0.01"))
    return Decimal("0.00")


def parse_html_printed_receipt(html: str) -> list[ParsedItem]:
    """Parse article spans from Lidl htmlPrintedReceipt (GB + EU layouts)."""
    if not html:
        return []

    items: list[ParsedItem] = []
    current: ParsedItem | None = None
    pending_discount_label: str | None = None
    # Ignore TOTAL / CARD / TOTAL DISCOUNT footer — those are also css_bold.
    in_items = True

    for match in _SPAN_RE.finditer(html):
        attrs = match.group("attrs") or ""
        body = match.group("body") or ""
        class_match = _CLASS_RE.search(attrs)
        classes = (class_match.group("cls") if class_match else "").lower().split()
        text = html_lib.unescape(re.sub(r"<[^>]+>", "", body)).strip()

        if any(
            c in classes
            for c in (
                "purchase_summary",
                "purchase_tender_information",
                "vat_info",
                "return_code",
            )
        ):
            in_items = False
            if current:
                items.append(_finalize(current))
                current = None
            continue
        if not in_items:
            continue

        if "article" in classes:
            data = {
                m.group("key").lower(): m.group("val") for m in _DATA_ATTR.finditer(attrs)
            }
            # Weight detail duplicate lines share art-id but only show kg @ price
            if _WEIGHT_DETAIL_RE.search(text):
                continue

            qty = parse_quantity(data.get("art-quantity"))
            unit = parse_money(data.get("unit-price"))
            desc = html_lib.unescape(data.get("art-description") or "").strip()
            if not desc:
                desc = text.split("  ")[0].strip() or "Unknown"
            # Drop trailing product codes appended to description ("Broccoli 0082904")
            if data.get("art-id") and desc.endswith(data["art-id"]):
                desc = desc[: -len(data["art-id"])].strip()

            line_total = _line_total_from_body(body, unit, qty)
            if line_total == 0:
                continue

            # Deduplicate consecutive identical weighed article headers
            if (
                current
                and current.product_id
                and current.product_id == (data.get("art-id") or None)
                and current.quantity == qty
                and current.unit_price == unit
            ):
                continue

            if current:
                items.append(_finalize(current))
            current = ParsedItem(
                product_id=(data.get("art-id") or None),
                description=desc,
                quantity=qty,
                unit_price=unit,
                line_total=line_total,
                tax_type=data.get("tax-type"),
            )
            pending_discount_label = None
            continue

        # GB line discounts: bold "Price Cut" then a negative amount like "-0.04".
        # Do NOT treat footer totals ("40.60") as discounts.
        if "discount" in classes or "css_bold" in classes:
            cleaned = text.replace(" ", "").replace("£", "").replace("&pound;", "")
            money_match = _MONEY_RE.search(cleaned)
            is_negative_amount = bool(
                money_match and cleaned.lstrip().startswith("-")
            )
            if current is not None and is_negative_amount:
                amt = abs(parse_money(money_match.group(0)))
                # Line discounts can't exceed the line total
                amt = min(amt, current.line_total)
                if amt > 0:
                    current.discount_total = (current.discount_total + amt).quantize(
                        Decimal("0.01")
                    )
                    current.discounts.append(
                        {
                            "description": pending_discount_label or "",
                            "amount": str(amt),
                        }
                    )
                    pending_discount_label = None
            elif text and not money_match:
                pending_discount_label = text
            elif text and money_match and not is_negative_amount:
                # Labels like "TOTAL" / positive amounts — ignore
                pending_discount_label = None

    if current:
        items.append(_finalize(current))
    return items


def parse_receipt_items(payload: dict[str, Any]) -> list[ParsedItem]:
    items_line = payload.get("itemsLine")
    if isinstance(items_line, list) and items_line:
        return parse_items_line(items_line)
    html = payload.get("htmlPrintedReceipt")
    if isinstance(html, str) and html.strip():
        return parse_html_printed_receipt(html)
    return []
