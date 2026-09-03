"""Shared HTTP helpers: bearer auth and service-protection-aware retries.

Dataverse enforces service protection limits per user, per web server, over a
five-minute sliding window (6,000 requests / 20 minutes execution time / 52+
concurrent). On 429 it returns a ``Retry-After`` header in seconds, and the
documented behavior for a non-interactive client is to wait exactly that long.
Ignoring Retry-After causes the penalty duration to be *extended*.

https://learn.microsoft.com/en-us/power-apps/developer/data-platform/api-limits
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Mapping

import httpx
from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 300.0
DEFAULT_TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=180.0, pool=30.0)


class TokenProvider:
    """Caches a bearer token per scope until shortly before it expires."""

    def __init__(self, credential: TokenCredential) -> None:
        self._credential = credential
        self._cache: dict[str, tuple[str, float]] = {}

    def token(self, scope: str) -> str:
        cached = self._cache.get(scope)
        now = time.time()
        if cached and cached[1] - 300 > now:
            return cached[0]
        access_token = self._credential.get_token(scope)
        self._cache[scope] = (access_token.token, float(access_token.expires_on))
        return access_token.token

    def auth_header(self, scope: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(scope)}"}


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor Retry-After when present, otherwise exponential backoff with jitter."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            # Retry-After may legally be an HTTP-date; fall through to backoff.
            logger.debug("Non-numeric Retry-After header: %r", retry_after)
    return min(2.0**attempt + random.uniform(0, 1), 60.0)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    max_attempts: int = 6,
) -> httpx.Response:
    """Issue a request, retrying throttling and transient server errors."""
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, headers=dict(headers or {}), json=json_body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
            delay = min(2.0**attempt + random.uniform(0, 1), 60.0)
            logger.warning(
                "Transport error on %s %s (attempt %d/%d): %s. Retrying in %.1fs.",
                method,
                url,
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS:
            return response

        if attempt == max_attempts - 1:
            return response

        delay = _retry_delay(response, attempt)
        logger.warning(
            "HTTP %d on %s %s (attempt %d/%d). Waiting %.1fs before retry.",
            response.status_code,
            method,
            url,
            attempt + 1,
            max_attempts,
            delay,
        )
        time.sleep(delay)

    raise RuntimeError(f"Request to {url} failed after {max_attempts} attempts") from last_error
