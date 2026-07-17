from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import InsightRun
from app.services.insight_facts import InsightFacts, build_insight_facts

logger = logging.getLogger(__name__)

ALLOWED_STATUS = {"on_track", "at_risk", "overspending", "unknown"}
FRESH_HOURS = 24

SYSTEM_PROMPT = """You are a concise personal finance coach for a UK Monzo user.
Use British English and GBP (£).
You receive ONLY pre-computed facts. Never invent numbers, merchants, or categories.
Only reference amounts that appear in the facts JSON.
Return STRICT JSON with this shape:
{
  "status": "on_track" | "at_risk" | "overspending" | "unknown",
  "headline": "one short sentence",
  "actions": [{"text": "concrete action", "impact": "why it helps"}],
  "leaks": [{"text": "subscription or drift finding"}],
  "habits": [{"text": "pattern observation"}]
}
Rules:
- Max 3 actions, max 3 leaks, max 3 habits. Omit empty arrays as [].
- Prefer ranked, actionable advice over summaries of the numbers.
- Prefer pay-period course correction, money leaks, and habit patterns.
- If pace.status is present, align your status with it unless facts clearly contradict.
- Keep each text under 140 characters.
"""


@dataclass
class InsightView:
    id: int
    status: str
    coach_status: str | None
    headline: str | None
    actions: list[dict[str, str]]
    leaks: list[dict[str, str]]
    habits: list[dict[str, str]]
    created_at: datetime | None
    error: str | None
    model: str | None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.headline)


def insights_configured() -> bool:
    cfg = get_settings()
    return bool(cfg.insights_enabled and cfg.gemini_api_key.strip())


def _parse_json_list(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            row = {"text": text}
            impact = str(item.get("impact") or "").strip()
            if impact:
                row["impact"] = impact
            out.append(row)
    return out


def insight_to_view(row: InsightRun) -> InsightView:
    return InsightView(
        id=row.id,
        status=row.status,
        coach_status=row.coach_status,
        headline=row.headline,
        actions=_parse_json_list(row.actions_json),
        leaks=_parse_json_list(row.leaks_json),
        habits=_parse_json_list(row.habits_json),
        created_at=row.created_at,
        error=row.error,
        model=row.model,
    )


def get_latest_insight(db: Session, *, ok_only: bool = True) -> InsightView | None:
    q = db.query(InsightRun).order_by(InsightRun.created_at.desc())
    if ok_only:
        q = q.filter(InsightRun.status == "ok")
    row = q.first()
    return insight_to_view(row) if row else None


def get_fresh_insight(db: Session, *, max_age_hours: int = FRESH_HOURS) -> InsightView | None:
    insight = get_latest_insight(db, ok_only=True)
    if not insight or not insight.created_at:
        return None
    created = insight.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    if age > timedelta(hours=max_age_hours):
        return None
    return insight


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _fact_amount_tokens(facts: InsightFacts) -> set[str]:
    """Collect numeric amount strings present in facts for soft validation."""
    return set(re.findall(r"\d+\.\d{2}", facts.to_json()))


def _normalize_amount_token(raw: str) -> str:
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return raw


def _text_uses_only_known_amounts(text: str, known: set[str]) -> bool:
    """Reject bullets that cite £ amounts not present in the facts payload."""
    if not known:
        return True
    cited = re.findall(r"£\s*(\d+(?:\.\d{1,2})?)", text)
    for amt in cited:
        if _normalize_amount_token(amt) not in known:
            return False
    return True


def _validate_response(data: dict[str, Any], facts: InsightFacts) -> dict[str, Any]:
    status = str(data.get("status") or facts.pace.get("status") or "unknown")
    if status not in ALLOWED_STATUS:
        status = str(facts.pace.get("status") or "unknown")

    headline = str(data.get("headline") or "").strip()
    if not headline:
        raise ValueError("Model returned empty headline")

    known = _fact_amount_tokens(facts)
    if not _text_uses_only_known_amounts(headline, known):
        # Fall back to a deterministic headline from pace facts
        pace = facts.pace
        headline = (
            f"Pace {pace.get('status', 'unknown')}: "
            f"avg £{pace.get('avg_daily_spend')} vs safe £{pace.get('safe_daily_spend')}/day"
        )

    def clean_items(raw: Any, *, with_impact: bool, limit: int) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []
        items: list[dict[str, str]] = []
        for item in raw:
            if len(items) >= limit:
                break
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text or not _text_uses_only_known_amounts(text, known):
                continue
            row = {"text": text[:200]}
            if with_impact:
                impact = str(item.get("impact") or "").strip()
                if impact and _text_uses_only_known_amounts(impact, known):
                    row["impact"] = impact[:200]
            items.append(row)
        return items

    return {
        "status": status,
        "headline": headline[:280],
        "actions": clean_items(data.get("actions"), with_impact=True, limit=3),
        "leaks": clean_items(data.get("leaks"), with_impact=False, limit=3),
        "habits": clean_items(data.get("habits"), with_impact=False, limit=3),
    }


def _call_gemini(facts: InsightFacts) -> dict[str, Any]:
    cfg = get_settings()
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.gemini_api_key.strip())
    user_prompt = (
        "Here are the pre-computed finance facts as JSON. "
        "Produce coaching JSON only.\n\n"
        f"{facts.to_json()}"
    )
    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise ValueError("Empty response from Gemini")
    return _extract_json(text)


def generate_insight(
    db: Session,
    *,
    force: bool = False,
    today=None,
) -> InsightView:
    """Generate (or reuse fresh) insight. Always returns a view; may be failed."""
    cfg = get_settings()
    if not insights_configured():
        raise ValueError("Insights are not configured (set GEMINI_API_KEY)")

    if not force:
        fresh = get_fresh_insight(db)
        if fresh:
            return fresh

    facts = build_insight_facts(db, today)
    row = InsightRun(
        status="pending",
        facts_json=facts.to_json(),
        facts_hash=facts.facts_hash(),
        model=cfg.gemini_model,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        raw = _call_gemini(facts)
        parsed = _validate_response(raw, facts)
        row.status = "ok"
        row.coach_status = parsed["status"]
        row.headline = parsed["headline"]
        row.actions_json = json.dumps(parsed["actions"])
        row.leaks_json = json.dumps(parsed["leaks"])
        row.habits_json = json.dumps(parsed["habits"])
        row.error = None
        db.commit()
        db.refresh(row)
        return insight_to_view(row)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Insight generation failed")
        row.status = "failed"
        row.error = str(exc)[:2000]
        db.commit()
        db.refresh(row)
        # Prefer last good insight for display
        good = get_latest_insight(db, ok_only=True)
        if good and good.id != row.id:
            return good
        return insight_to_view(row)


def ensure_insight_for_report(db: Session, today=None) -> InsightView | None:
    """For weekly digests: return a fresh/ok insight, or None on total failure."""
    if not insights_configured():
        return None
    try:
        view = generate_insight(db, force=False, today=today)
        return view if view.ok else None
    except Exception:
        logger.exception("Could not ensure insight for report")
        return get_latest_insight(db, ok_only=True)
