from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel


def money(value: Decimal | float | int | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def _convert(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return money(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _convert(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return _convert(asdict(value))
    if isinstance(value, dict):
        return {str(k): _convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v) for v in value]
    return value


def to_jsonable(value: Any) -> Any:
    return _convert(value)
