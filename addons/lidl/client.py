from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.lidl.com/connect/token"
TICKETS_BASE = "https://tickets.lidlplus.com"
# Public OAuth client id/secret baked into the Lidl Plus Android app.
BASIC_AUTH = "Basic TGlkbFBsdXNOYXRpdmVDbGllbnQ6c2VjcmV0"


class LidlAuthError(Exception):
    """Refresh token rejected — needs a fresh browser login."""


class LidlApiError(Exception):
    """Transient or unexpected tickets API error."""


class LidlClient:
    def __init__(
        self,
        *,
        country: str = "GB",
        language: str = "en",
        device_id: str = "a1b2c3d4e5f67890",
        app_version: str = "16.45.5",
        timeout: float = 30.0,
    ) -> None:
        self.country = country.upper()
        self.language = language.lower()
        self.device_id = device_id
        self.app_version = app_version
        self._client = httpx.Client(timeout=timeout, http2=False)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.access_expires_at: datetime | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LidlClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def set_tokens(
        self,
        *,
        refresh_token: str,
        access_token: str | None = None,
        access_expires_at: datetime | None = None,
    ) -> None:
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.access_expires_at = access_expires_at

    def refresh_access_token(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise LidlAuthError("No refresh token configured")

        response = self._client.post(
            TOKEN_URL,
            headers={
                "Authorization": BASIC_AUTH,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )
        if response.status_code in (400, 401):
            raise LidlAuthError(
                f"Refresh token rejected ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise LidlApiError(
                f"Token endpoint error {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        access = payload.get("access_token")
        new_refresh = payload.get("refresh_token")
        expires_in = int(payload.get("expires_in") or 3600)
        if not access or not new_refresh:
            raise LidlAuthError("Token response missing access_token or refresh_token")

        self.access_token = access
        self.refresh_token = new_refresh
        self.access_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return {
            "access_token": access,
            "refresh_token": new_refresh,
            "access_expires_at": self.access_expires_at,
        }

    def ensure_access_token(self, *, skew_seconds: int = 60) -> None:
        now = datetime.now(timezone.utc)
        if (
            self.access_token
            and self.access_expires_at
            and self.access_expires_at > now + timedelta(seconds=skew_seconds)
        ):
            return
        self.refresh_access_token()

    def _ticket_headers(self) -> dict[str, str]:
        self.ensure_access_token()
        assert self.access_token
        # Reference clients format Date in Europe/Budapest with a literal GMT suffix.
        stamp = format_datetime(
            datetime.now(ZoneInfo("Europe/Budapest")),
            usegmt=False,
        ).replace("+0100", "GMT").replace("+0200", "GMT")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "App-Version": self.app_version,
            "Operating-System": "Android",
            "App": "com.lidl.eci.lidlplus",
            "Accept-Language": self.language,
            "User-Agent": "okhttp/5.3.2",
            "OS-Version": "16",
            "Model": "sdk_gphone64_arm64",
            "Brand": "Google",
            "deviceid": self.device_id,
            "Date": stamp,
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, url, headers=self._ticket_headers(), **kwargs)
        if response.status_code == 401:
            logger.info("Tickets API 401 — refreshing access token and retrying once")
            self.refresh_access_token()
            response = self._client.request(
                method, url, headers=self._ticket_headers(), **kwargs
            )
        return response

    def list_tickets_page(self, page_number: int = 1) -> dict[str, Any]:
        url = f"{TICKETS_BASE}/api/v2/{self.country}/tickets"
        response = self._request(
            "GET",
            url,
            params={"pageNumber": page_number, "onlyFavorite": "false"},
        )
        if response.status_code >= 400:
            raise LidlApiError(
                f"List tickets failed {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    def iter_ticket_summaries(self):
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = self.list_tickets_page(page)
            tickets = payload.get("tickets") or []
            size = int(payload.get("size") or len(tickets) or 20)
            total_count = int(payload.get("totalCount") or 0)
            if size > 0 and total_count > 0:
                total_pages = max(1, (total_count + size - 1) // size)
            elif not tickets:
                break
            for ticket in tickets:
                yield ticket
            if not tickets:
                break
            page += 1

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        url = f"{TICKETS_BASE}/api/v3/{self.country}/tickets/{ticket_id}"
        response = self._request("GET", url)
        if response.status_code >= 400:
            raise LidlApiError(
                f"Ticket detail failed {response.status_code}: {response.text[:300]}"
            )
        return response.json()
