from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send_pushover(
    title: str,
    message: str,
    *,
    priority: int = 0,
) -> None:
    settings = get_settings()
    if not settings.pushover_enabled:
        raise ValueError("Pushover is disabled (PUSHOVER_ENABLED=false)")
    if not settings.pushover_app_token or not settings.pushover_user_key:
        raise ValueError("PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are required")

    data = {
        "token": settings.pushover_app_token,
        "user": settings.pushover_user_key,
        "title": title[:250],
        "message": message[:1024],
        "priority": priority,
    }
    if settings.pushover_device:
        data["device"] = settings.pushover_device

    with httpx.Client(timeout=20.0) as client:
        response = client.post(PUSHOVER_URL, data=data)
        if response.status_code != 200:
            raise RuntimeError(
                f"Pushover HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"Pushover error: {payload}")

    logger.info("Sent Pushover: %s", title)
