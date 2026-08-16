"""HTTP client with request budgeting, retry/backoff, and quota accounting.

The free API tier is the binding constraint on this pipeline, so every request is
counted before it is spent, and the provider's own quota headers are believed over
our local guess when they are present.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# RapidAPI's gateway reports remaining monthly quota in these headers.
HEADER_QUOTA_REMAINING = "x-ratelimit-requests-remaining"
HEADER_QUOTA_LIMIT = "x-ratelimit-requests-limit"


class BudgetExhausted(Exception):
    """The per-run request budget is spent. Not an error: checkpoint and stop."""


class QuotaExhausted(Exception):
    """The provider says the account's quota is gone. Not an error: stop cleanly."""


class HttpError(Exception):
    """A non-retryable HTTP failure, or retries exhausted."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RequestBudget:
    """Counts requests for one run. ``max_requests=None`` means unlimited."""

    max_requests: int | None = None
    used: int = 0
    quota_remaining: int | None = None
    quota_limit: int | None = None

    @property
    def remaining(self) -> int | None:
        if self.max_requests is None:
            return None
        return max(0, self.max_requests - self.used)

    def can_spend(self, n: int = 1) -> bool:
        return self.max_requests is None or self.used + n <= self.max_requests

    def spend(self, n: int = 1) -> None:
        """Reserve budget before issuing a request."""
        if not self.can_spend(n):
            raise BudgetExhausted(
                f"run budget exhausted after {self.used} requests "
                f"(max_requests_per_run={self.max_requests})"
            )
        self.used += n

    def observe_quota(self, remaining: int | None, limit: int | None) -> None:
        if remaining is not None:
            self.quota_remaining = remaining
        if limit is not None:
            self.quota_limit = limit


@dataclass
class HttpClient:
    """Minimal JSON client over urllib (no third-party HTTP dependency)."""

    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    #: Minimum seconds between requests. Being throttled costs more time than
    #: pacing does, so it is cheaper to stay under the limit than to retry past it.
    min_request_interval: float = 0.0
    user_agent: str = "pd-books/0.1 (+metadata ingestion)"
    sleep = staticmethod(time.sleep)
    _last_request_at: float = field(default=0.0, repr=False)

    def _throttle(self) -> None:
        if self.min_request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.min_request_interval:
            self.sleep(self.min_request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return f"{url}?{urllib.parse.urlencode(clean)}" if clean else url

    @staticmethod
    def _read_int_header(headers: Any, name: str) -> int | None:
        raw = headers.get(name) if headers else None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_cap)
            except ValueError:
                pass
        # Exponential with full jitter, so parallel runs don't resynchronize.
        return random.uniform(0, min(self.backoff_base * (2**attempt), self.backoff_cap))

    def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        budget: RequestBudget | None = None,
    ) -> dict[str, Any]:
        """GET a JSON document, spending one unit of budget.

        Retries are free: the budget is charged once per logical request, since the
        provider only bills successful gateway calls in practice and we must not let
        a retry storm silently eat the run budget.
        """
        if budget is not None:
            budget.spend(1)

        url = self._build_url(path, params)
        request_headers = {"Accept": "application/json", "User-Agent": self.user_agent, **self.headers}
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, headers=request_headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if budget is not None:
                        budget.observe_quota(
                            self._read_int_header(resp.headers, HEADER_QUOTA_REMAINING),
                            self._read_int_header(resp.headers, HEADER_QUOTA_LIMIT),
                        )
                    body = resp.read().decode("utf-8")
                return json.loads(body)

            except urllib.error.HTTPError as exc:
                remaining = self._read_int_header(exc.headers, HEADER_QUOTA_REMAINING)
                if budget is not None:
                    budget.observe_quota(
                        remaining, self._read_int_header(exc.headers, HEADER_QUOTA_LIMIT)
                    )
                # A 429 with no quota left is terminal for this run; retrying cannot help.
                if exc.code == 429 and remaining == 0:
                    raise QuotaExhausted(
                        "provider reports 0 requests remaining on the current plan"
                    ) from exc
                if exc.code not in RETRY_STATUSES or attempt == self.max_retries:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:200]
                    except Exception:  # noqa: BLE001 - body is best-effort context only
                        pass
                    raise HttpError(f"GET {url} failed: HTTP {exc.code} {detail}", exc.code) from exc
                last_error = exc
                delay = self._backoff_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                logger.warning(
                    "HTTP %s on %s (attempt %d/%d), retrying in %.1fs",
                    exc.code, path, attempt + 1, self.max_retries, delay,
                )
                self.sleep(delay)

            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.max_retries:
                    raise HttpError(f"GET {url} failed: {exc}") from exc
                last_error = exc
                delay = self._backoff_delay(attempt, None)
                logger.warning(
                    "%s on %s (attempt %d/%d), retrying in %.1fs",
                    type(exc).__name__, path, attempt + 1, self.max_retries, delay,
                )
                self.sleep(delay)

        raise HttpError(f"GET {url} failed after {self.max_retries} retries: {last_error}")
