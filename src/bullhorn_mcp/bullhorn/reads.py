"""Bullhorn read transport for Phase 5C (D-5C-2, D-5C-12).

``EntityReader(client)`` follows the 4B ``EntityWriter`` pattern: ``client.py``
is unchanged, and requests go out on the passed client's session
(``client.auth.session``: ``rest_url`` and ``BhRestToken``). In shared mode the
client is the per-call 5A client, so the 401 path is the 5A session refresh,
which keeps the link; there is no fallback to any other credential.

- **GET only**, built from ``bullhorn/query_syntax.py`` output (``/query/{Entity}``).
- **401**: one ``client.auth._refresh_session()`` and one retry. A failed refresh
  raises (no partial results, no other identity).
- **429 / 503**: at most 2 retries (3 attempts), exponential backoff with full
  jitter, base 1.0 s (HV-Q7: "Wait 1 second then retry"), cap 8 s.
  ``Retry-After`` is never parsed (its form is undocumented).
- **500 / 502 / 504**: at most one retry.
- **Budget**: 30 s of wall clock per reader (one reader per tool call); when it
  is exceeded, ``ReadRateLimited`` is raised and partial results are discarded.
- **Concurrency**: at most 2 requests in flight per ``(tenant_key, principal_key)``.
- Any other non-200 (including a Bullhorn permission refusal, HV-Q11) raises
  ``ReadFailed`` with only the status code; it is never retried under another
  identity.

This module never logs the URL, the parameters or the response body, and it
installs the Amendment C2 query-string scrub filter.
"""

from __future__ import annotations

import collections
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import log_scrub
from .errors import BullhornAPIError

BUDGET_SECONDS = 30.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 8.0
MAX_RATE_RETRIES = 2  # 429 / 503
MAX_SERVER_RETRIES = 1  # 500 / 502 / 504
MAX_IN_FLIGHT = 2
MAX_KEYS = 10_000
REQUEST_TIMEOUT_SECONDS = 20.0
RATE_STATUSES = frozenset({429, 503})
SERVER_STATUSES = frozenset({500, 502, 504})
PARAM_KEYS = frozenset({"where", "fields", "count", "start"})
_ENDPOINT_RE = re.compile(r"/query/[A-Z][A-Za-z0-9]{0,63}", re.ASCII)


class ReadFailed(BullhornAPIError):
    """A read failed (``bullhorn_error``). The message carries only a status code or a fixed reason."""


class ReadRateLimited(BullhornAPIError):
    """Rate limited, or the per-call budget was exceeded (``rate_limited``)."""


_semaphores: collections.OrderedDict[tuple[str, str], threading.BoundedSemaphore] = collections.OrderedDict()
_semaphores_guard = threading.Lock()


def _semaphore(key: tuple[str, str]) -> threading.BoundedSemaphore:
    with _semaphores_guard:
        sem = _semaphores.get(key)
        if sem is None:
            sem = _semaphores[key] = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        _semaphores.move_to_end(key)
        while len(_semaphores) > MAX_KEYS:
            _semaphores.popitem(last=False)
        return sem


def reset_limits() -> None:
    """Tests: forget the per-principal semaphores."""
    with _semaphores_guard:
        _semaphores.clear()


class EntityReader:
    """``GET /query/{Entity}`` on the client's session, with retries, a budget and a concurrency cap."""

    def __init__(
        self,
        client: Any,
        concurrency_key: tuple[str, str] = ("local", "local"),
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        budget: float = BUDGET_SECONDS,
    ) -> None:
        log_scrub.install()  # Amendment C2 (idempotent)
        self.client = client
        self._key = concurrency_key
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._deadline = clock() + budget
        self.requests = 0  # Bullhorn requests issued by this reader (for tests and provenance)

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    def _backoff(self, attempt: int) -> None:
        delay = self._rng() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
        if delay >= self._remaining():
            raise ReadRateLimited("rate_limited: the per-call time budget would be exceeded")
        self._sleep(delay)

    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """One logical read. Returns the response object (with a ``data`` list)."""
        if type(endpoint) is not str or _ENDPOINT_RE.fullmatch(endpoint) is None:
            raise ValueError("invalid endpoint")
        if not isinstance(params, dict) or not set(params) <= PARAM_KEYS:
            raise ValueError("invalid parameters")
        if self._remaining() <= 0:
            raise ReadRateLimited("rate_limited: the per-call time budget is exhausted")
        sem = _semaphore(self._key)
        if not sem.acquire(timeout=max(0.0, self._remaining())):
            raise ReadRateLimited("rate_limited: too many concurrent reads for this caller")
        try:
            return self._get(endpoint, params)
        finally:
            sem.release()

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        auth = self.client.auth
        session = auth.session
        refreshed = False
        rate_retries = server_retries = 0
        with httpx.Client() as http:
            while True:
                remaining = self._remaining()
                if remaining <= 0:
                    raise ReadRateLimited("rate_limited: the per-call time budget is exhausted")
                self.requests += 1
                try:
                    response = http.get(
                        f"{session.rest_url}{endpoint}",
                        params=params,
                        headers={"BhRestToken": session.bh_rest_token},
                        timeout=min(REQUEST_TIMEOUT_SECONDS, remaining),
                    )
                except httpx.TimeoutException:
                    raise ReadRateLimited("rate_limited: the request timed out") from None
                except httpx.HTTPError as exc:
                    raise ReadFailed(f"bullhorn_error: transport failure ({type(exc).__name__})") from None
                status = response.status_code
                if status == 200:
                    break
                if status == 401 and not refreshed:
                    refreshed = True
                    auth._refresh_session()  # 5A refresh in shared mode; raises when it fails
                    session = auth.session
                    continue
                if status in RATE_STATUSES and rate_retries < MAX_RATE_RETRIES:
                    self._backoff(rate_retries)
                    rate_retries += 1
                    continue
                if status in SERVER_STATUSES and server_retries < MAX_SERVER_RETRIES:
                    self._backoff(server_retries)
                    server_retries += 1
                    continue
                if status in RATE_STATUSES:
                    raise ReadRateLimited(f"rate_limited: {status}")
                raise ReadFailed(f"bullhorn_error: {status}")
        try:
            data = response.json()
        except ValueError:
            raise ReadFailed("bullhorn_error: the response is not JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ReadFailed("bullhorn_error: the response has no data list")
        return data
