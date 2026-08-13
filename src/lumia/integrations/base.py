"""Shared plumbing for every external API.

Design rule: an integration never raises on a missing API key. If the
service isn't configured it runs in mock mode — returning realistic,
deterministic data and recording the call — so the agents are testable and
the repo runs end to end without credentials.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import ServiceCredentials

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
TIMEOUT_SECONDS = 20.0

#: A retry needs room for the wait *and* the attempt after it, or it is just
#: a slower way to fail.
MIN_RETRY_HEADROOM = 3.0


class IntegrationError(RuntimeError):
    """A call to an external service failed in a way the agent should see."""


@dataclass
class CallRecord:
    """One outbound call, real or mocked. Useful for audit and tests."""

    service: str
    method: str
    path: str
    mocked: bool
    payload: dict[str, Any] | None = None


@dataclass
class Integration:
    """Base class for a third-party service wrapper."""

    credentials: ServiceCredentials
    calls: list[CallRecord] = field(default_factory=list)
    #: The run's remaining time, when one is in progress. Without it a call
    #: assumes it is the only thing happening; with it, three retries at
    #: twenty seconds each cannot quietly consume half a run's budget.
    budget: Any = None

    def _timeout(self, preferred: float = TIMEOUT_SECONDS) -> float:
        if self.budget is None:
            return preferred
        return self.budget.timeout_for(preferred)

    def _has_time(self) -> bool:
        return self.budget is None or self.budget.usable

    @property
    def name(self) -> str:
        return self.credentials.name

    @property
    def live(self) -> bool:
        """True when real credentials are present."""
        return self.credentials.configured

    def _headers(self) -> dict[str, str]:
        """Override per service if it doesn't use bearer auth."""
        return {
            "Authorization": f"Bearer {self.credentials.api_key}",
            "Content-Type": "application/json",
        }

    def _record(self, method: str, path: str, mocked: bool, payload: dict[str, Any] | None = None) -> None:
        self.calls.append(
            CallRecord(service=self.name, method=method, path=path, mocked=mocked, payload=payload)
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        mock: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue an HTTP call, or return `mock` when unconfigured.

        Retries idempotent-ish failures (429/5xx) with exponential backoff.
        """
        if not self.live:
            self._record(method, path, mocked=True, payload=json)
            result = dict(mock or {})
            result["_mocked"] = True
            result["_reason"] = f"{self.name} is not configured; set its API key in .env to go live"
            return result

        url = f"{str(self.credentials.base_url).rstrip('/')}/{path.lstrip('/')}"
        last_error: Exception | None = None

        if not self._has_time():
            raise IntegrationError(
                f"{self.name} call to {path} was not attempted: the run's time budget is spent"
            )

        for attempt in range(MAX_RETRIES):
            try:
                response = httpx.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(),
                    timeout=self._timeout(),
                )
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
            else:
                if response.status_code not in RETRY_STATUS:
                    self._record(method, path, mocked=False, payload=json)
                    return self._parse(response)
                last_error = IntegrationError(
                    f"{self.name} returned {response.status_code}: {response.text[:200]}"
                )

            if attempt < MAX_RETRIES - 1:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
                # Retrying into a spent budget only fails slower than stopping.
                # `remaining` is None on an uncapped run, which is not a small
                # number — it means there is nothing to run out of.
                left = None if self.budget is None else self.budget.remaining
                if left is not None and left <= delay + MIN_RETRY_HEADROOM:
                    log.warning(
                        "%s call failed (%s); no time left in the run to retry", self.name, last_error
                    )
                    break
                log.warning("%s call failed (%s); retrying in %.1fs", self.name, last_error, delay)
                time.sleep(delay)

        raise IntegrationError(f"{self.name} call to {path} failed after {MAX_RETRIES} attempts: {last_error}")

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise IntegrationError(f"HTTP {response.status_code}: {response.text[:300]}")
        if not response.content:
            return {"status": "ok", "http_status": response.status_code}
        try:
            data = response.json()
        except ValueError:
            return {"status": "ok", "raw": response.text[:2000]}
        return data if isinstance(data, dict) else {"data": data}
