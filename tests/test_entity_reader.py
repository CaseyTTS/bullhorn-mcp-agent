"""Phase 5C read transport (D-5C-2, D-5C-12; AC-13, HV-Q7, HV-Q11)."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx

from bullhorn_mcp.bullhorn import log_scrub
from bullhorn_mcp.bullhorn import reads as bh_reads
from bullhorn_mcp.bullhorn.reads import EntityReader, ReadFailed, ReadRateLimited

from ._phase5c_helpers import TOKEN, FakeClock, make_client
from ._tenant_helpers import REST_URL

URL = f"{REST_URL}/query/Candidate"
PARAMS = {"where": "id = 1", "fields": "id", "count": 2, "start": 0}
OK = httpx.Response(200, json={"start": 0, "count": 1, "data": [{"id": 1}]})
SOURCE = Path(bh_reads.__file__)


@pytest.fixture(autouse=True)
def _reset():
    bh_reads.reset_limits()
    yield
    bh_reads.reset_limits()


def reader(client=None, clock=None, rng=lambda: 1.0, key=("t", "p"), budget=30.0):
    clock = clock or FakeClock()
    return EntityReader(client or make_client(), key, clock=clock, sleep=clock.sleep, rng=rng, budget=budget), clock


class TestRequests:
    @respx.mock
    def test_get_with_session_token(self):
        route = respx.get(URL).mock(return_value=OK)
        r, _ = reader()
        assert r.get("/query/Candidate", PARAMS)["data"] == [{"id": 1}]
        req = route.calls[0].request
        assert req.method == "GET" and req.headers["BhRestToken"] == TOKEN
        assert r.requests == 1

    @pytest.mark.parametrize("endpoint", ["/entity/Candidate/1", "/query/candidate", "/query/Candidate?x=1", "/search/Candidate"])
    def test_endpoint_allowlist(self, endpoint):
        r, _ = reader()
        with pytest.raises(ValueError):
            r.get(endpoint, PARAMS)

    def test_param_keys_fixed(self):
        r, _ = reader()
        with pytest.raises(ValueError):
            r.get("/query/Candidate", {**PARAMS, "orderBy": "id"})

    @respx.mock
    @pytest.mark.parametrize("body", [{"x": 1}, [1], {"data": "x"}])
    def test_bad_body(self, body):
        respx.get(URL).mock(return_value=httpx.Response(200, json=body))
        r, _ = reader()
        with pytest.raises(ReadFailed):
            r.get("/query/Candidate", PARAMS)

    def test_source_issues_only_get(self):
        text = SOURCE.read_text(encoding="utf-8")
        assert re.findall(r"http\.(\w+)\(", text) == ["get"]
        for verb in (".post(", ".put(", ".delete(", ".patch(", ".request(", ".stream("):
            assert verb not in text

    def test_scrub_filter_installed(self):
        reader()
        import logging

        assert any(isinstance(f, log_scrub.QueryStringScrubFilter) for f in logging.getLogger("httpx").filters)


class TestRetries:
    @respx.mock
    @pytest.mark.parametrize("status", [429, 503])
    def test_rate_statuses_retried_twice_with_backoff(self, status):
        route = respx.get(URL).mock(side_effect=[httpx.Response(status), httpx.Response(status), OK])
        r, clock = reader()
        assert r.get("/query/Candidate", PARAMS)["data"]
        assert route.call_count == 3
        assert clock.sleeps == [1.0, 2.0]  # base 1.0 s (HV-Q7), exponential, full jitter (rng = 1.0)

    @respx.mock
    @pytest.mark.parametrize("status", [429, 503])
    def test_third_failure_is_rate_limited(self, status):
        route = respx.get(URL).mock(return_value=httpx.Response(status, headers={"Retry-After": "0"}))
        r, clock = reader()
        with pytest.raises(ReadRateLimited):
            r.get("/query/Candidate", PARAMS)
        assert route.call_count == 3 and clock.sleeps == [1.0, 2.0]  # Retry-After is never parsed

    @respx.mock
    def test_retry_after_ignored(self):
        respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "25"}), OK])
        r, clock = reader(rng=lambda: 0.5)
        r.get("/query/Candidate", PARAMS)
        assert clock.sleeps == [0.5]

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 504])
    def test_server_errors_retried_once(self, status):
        route = respx.get(URL).mock(return_value=httpx.Response(status))
        r, _ = reader()
        with pytest.raises(ReadFailed) as info:
            r.get("/query/Candidate", PARAMS)
        assert route.call_count == 2 and str(info.value) == f"bullhorn_error: {status}"

    @respx.mock
    @pytest.mark.parametrize("status", [400, 403, 404, 410])
    def test_permission_and_client_errors_not_retried(self, status):
        """HV-Q11: a refusal is an error; it is never retried (under this or any other identity)."""
        route = respx.get(URL).mock(return_value=httpx.Response(status, text="secret body detail"))
        client = make_client()
        r, _ = reader(client)
        with pytest.raises(ReadFailed) as info:
            r.get("/query/Candidate", PARAMS)
        assert route.call_count == 1 and "secret" not in str(info.value)
        client.auth._refresh_session.assert_not_called()

    @respx.mock
    def test_backoff_cap(self):
        respx.get(URL).mock(return_value=httpx.Response(429))
        r, clock = reader()
        assert bh_reads.BUDGET_SECONDS == 30.0
        assert min(bh_reads.BACKOFF_CAP_SECONDS, bh_reads.BACKOFF_BASE_SECONDS * 2**5) == 8.0
        with pytest.raises(ReadRateLimited):
            r.get("/query/Candidate", PARAMS)
        assert all(s <= 8.0 for s in clock.sleeps)


class TestRefresh:
    @respx.mock
    def test_401_refreshes_once(self):
        client = make_client()
        route = respx.get(URL).mock(side_effect=[httpx.Response(401), OK])
        r, _ = reader(client)
        r.get("/query/Candidate", PARAMS)
        client.auth._refresh_session.assert_called_once()
        assert route.call_count == 2

    @respx.mock
    def test_second_401_fails(self):
        client = make_client()
        respx.get(URL).mock(return_value=httpx.Response(401))
        r, _ = reader(client)
        with pytest.raises(ReadFailed):
            r.get("/query/Candidate", PARAMS)
        client.auth._refresh_session.assert_called_once()

    @respx.mock
    def test_failed_refresh_raises(self):
        from bullhorn_mcp.identity.sessions import BullhornSessionRequired

        client = make_client()
        client.auth._refresh_session.side_effect = BullhornSessionRequired("bullhorn_session_expired")
        respx.get(URL).mock(return_value=httpx.Response(401))
        r, _ = reader(client)
        with pytest.raises(BullhornSessionRequired):
            r.get("/query/Candidate", PARAMS)


class TestBudget:
    @respx.mock
    def test_budget_enforced_by_backoff(self):
        respx.get(URL).mock(return_value=httpx.Response(429))
        r, clock = reader(budget=1.5)
        with pytest.raises(ReadRateLimited):
            r.get("/query/Candidate", PARAMS)
        assert sum(clock.sleeps) < 1.5

    @respx.mock
    def test_budget_exhausted_before_request(self):
        route = respx.get(URL).mock(return_value=OK)
        r, clock = reader()
        clock.t += 31
        with pytest.raises(ReadRateLimited):
            r.get("/query/Candidate", PARAMS)
        assert route.call_count == 0

    @respx.mock
    def test_timeout_is_rate_limited(self):
        respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        r, _ = reader()
        with pytest.raises(ReadRateLimited):
            r.get("/query/Candidate", PARAMS)

    @respx.mock
    def test_transport_error(self):
        respx.get(URL).mock(side_effect=httpx.ConnectError("boom https://x?where=secret"))
        r, _ = reader()
        with pytest.raises(ReadFailed) as info:
            r.get("/query/Candidate", PARAMS)
        assert "secret" not in str(info.value)


class TestConcurrency:
    def test_at_most_two_in_flight_per_principal(self):
        active = {"now": 0, "max": 0}
        lock = threading.Lock()
        gate = threading.Event()

        def slow(request):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            gate.wait(0.2)
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return OK

        with respx.mock:
            respx.get(URL).mock(side_effect=slow)
            errors = []

            def run():
                try:
                    EntityReader(make_client(), ("t", "same")).get("/query/Candidate", PARAMS)
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        assert not errors
        assert active["max"] <= 2

    def test_other_principals_not_blocked(self):
        a = bh_reads._semaphore(("t", "a"))
        b = bh_reads._semaphore(("t", "b"))
        assert a is not b
        assert a.acquire(blocking=False) and a.acquire(blocking=False) and not a.acquire(blocking=False)
        assert b.acquire(blocking=False)
