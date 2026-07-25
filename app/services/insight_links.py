from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.budgets import list_seen_categories

_CATEGORY_KEYS = frozenset({"category"})
_MERCHANT_KEYS = frozenset({"merchant", "top_merchant"})
_AMBIGUOUS_KEYS = frozenset({"name"})
_MIN_NAME_LEN = 2

# Single-word names that collide with ordinary English (e.g. retailer Next vs "next payday").
# These only link when the match looks like a proper noun (Title Case or ALL CAPS).
_AMBIGUOUS_SINGLE_WORDS = frozenset(
    {
        "next",
        "boots",
        "gap",
        "prime",
        "plus",
        "go",
        "now",
        "open",
        "super",
        "family",
        "home",
        "office",
        "general",
        "local",
        "national",
        "and",
        "the",
        "for",
        "with",
        "from",
        "over",
        "under",
        "into",
        "this",
        "that",
        "your",
        "our",
        "all",
        "new",
        "old",
        "day",
        "week",
        "month",
        "period",
        "spend",
        "spent",
        "safe",
        "pace",
        "bill",
        "bills",
        "budget",
        "co",
        "uk",
    }
)


def _looks_like_proper_noun(matched: str) -> bool:
    """True for Title Case or ALL-CAPS brand-style tokens."""
    if not matched:
        return False
    if matched.isupper() and len(matched) > 1:
        return True
    return matched[0].isupper() and not matched.islower()


def _is_ambiguous_single_word(name: str) -> bool:
    cleaned = name.strip()
    if " " in cleaned or "-" in cleaned or "&" in cleaned:
        return False
    return cleaned.lower() in _AMBIGUOUS_SINGLE_WORDS


def _walk_fact_names(
    node: Any,
    *,
    categories: set[str],
    merchants: set[str],
    ambiguous: set[str],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                name = value.strip()
                if len(name) < _MIN_NAME_LEN:
                    continue
                if key in _CATEGORY_KEYS:
                    categories.add(name)
                elif key in _MERCHANT_KEYS:
                    merchants.add(name)
                elif key in _AMBIGUOUS_KEYS:
                    ambiguous.add(name)
            else:
                _walk_fact_names(
                    value,
                    categories=categories,
                    merchants=merchants,
                    ambiguous=ambiguous,
                )
    elif isinstance(node, list):
        for item in node:
            _walk_fact_names(
                item,
                categories=categories,
                merchants=merchants,
                ambiguous=ambiguous,
            )


def _names_from_facts_json(
    facts_json: str | None,
) -> tuple[set[str], set[str], set[str]]:
    if not facts_json:
        return set(), set(), set()
    try:
        data = json.loads(facts_json)
    except json.JSONDecodeError:
        return set(), set(), set()
    categories: set[str] = set()
    merchants: set[str] = set()
    ambiguous: set[str] = set()
    _walk_fact_names(
        data, categories=categories, merchants=merchants, ambiguous=ambiguous
    )
    return categories, merchants, ambiguous


def _list_seen_merchants(db: Session) -> list[str]:
    rows = (
        db.query(distinct(Transaction.merchant))
        .filter(Transaction.merchant.isnot(None), Transaction.merchant != "")
        .order_by(Transaction.merchant)
        .all()
    )
    return [r[0] for r in rows if r[0]]


def collect_link_entities(
    db: Session, facts_json: str | None = None
) -> list[tuple[str, str]]:
    """Return (display_name, kind) for linkifying coach text.

    kind is ``category`` or ``merchant``. Case-insensitive duplicates keep one
    entry; merchant wins over category when both exist.
    """
    categories: dict[str, str] = {}
    for name in list_seen_categories(db):
        cleaned = name.strip()
        if len(cleaned) >= _MIN_NAME_LEN:
            categories[cleaned.lower()] = cleaned
    categories.setdefault("uncategorised", "Uncategorised")

    merchants: dict[str, str] = {}
    for name in _list_seen_merchants(db):
        cleaned = name.strip()
        if len(cleaned) >= _MIN_NAME_LEN:
            merchants[cleaned.lower()] = cleaned

    fact_cats, fact_merch, fact_ambiguous = _names_from_facts_json(facts_json)
    for name in fact_cats:
        categories[name.lower()] = name
    for name in fact_merch:
        merchants[name.lower()] = name
    for name in fact_ambiguous:
        key = name.lower()
        if key in merchants or key in categories:
            continue
        # top_categories / top_merchants both use "name"; DB lists usually
        # already classified them. Remaining unknowns default to merchant.
        merchants[key] = name

    # Prefer merchant when the same string appears in both.
    merged: dict[str, tuple[str, str]] = {}
    for key, display in categories.items():
        merged[key] = (display, "category")
    for key, display in merchants.items():
        merged[key] = (display, "merchant")

    return [(display, kind) for display, kind in merged.values()]


def linkify_finance_text(text: str, entities: list[tuple[str, str]]) -> str:
    """HTML-escape text and wrap known category/merchant names in detail links."""
    if not text:
        return ""

    usable = [
        (name, kind)
        for name, kind in entities
        if name and len(name.strip()) >= _MIN_NAME_LEN
    ]
    if not usable:
        return html.escape(text)

    usable.sort(key=lambda item: len(item[0]), reverse=True)
    by_lower: dict[str, tuple[str, str]] = {}
    patterns: list[str] = []
    for name, kind in usable:
        key = name.lower()
        if key in by_lower:
            continue
        by_lower[key] = (name, kind)
        patterns.append(re.escape(name))

    if not patterns:
        return html.escape(text)

    pattern = re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(patterns) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )

    parts: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        matched = match.group(1)
        display, kind = by_lower[matched.lower()]
        # Avoid linking ordinary English like "next payday" to retailer Next.
        if _is_ambiguous_single_word(display) and not _looks_like_proper_noun(matched):
            continue
        parts.append(html.escape(text[last : match.start()]))
        path = "merchants" if kind == "merchant" else "categories"
        href = quote(display, safe="")
        parts.append(f'<a href="/{path}/{href}">{html.escape(matched)}</a>')
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def build_insight_html(
    *,
    headline: str | None,
    actions: list[dict[str, str]],
    leaks: list[dict[str, str]],
    habits: list[dict[str, str]],
    entities: list[tuple[str, str]],
) -> dict[str, Any]:
    """Linkify coach fields for trusted Overview rendering."""

    def link_items(items: list[dict[str, str]], *, with_impact: bool) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for item in items:
            row = {"text": linkify_finance_text(item.get("text") or "", entities)}
            if with_impact and item.get("impact"):
                row["impact"] = linkify_finance_text(item["impact"], entities)
            rows.append(row)
        return rows

    return {
        "headline": linkify_finance_text(headline or "", entities),
        "actions": link_items(actions, with_impact=True),
        "leaks": link_items(leaks, with_impact=False),
        "habits": link_items(habits, with_impact=False),
    }
