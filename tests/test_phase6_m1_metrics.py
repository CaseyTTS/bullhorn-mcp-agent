"""Phase 6 M1 ``get_recruiting_metrics`` services (§2-§4; AC-3, AC-4, AC-5, AC-8, AC-9; SR-37).

Every value here is synthetic (G-7): no tenant names, mappings or captured payloads.
"""

from __future__ import annotations

import calendar
import datetime as dt
import inspect
import re
from importlib import resources
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx

from bullhorn_mcp.activity import service as A
from bullhorn_mcp.metrics import catalog as C
from bullhorn_mcp.metrics import compute as M
from bullhorn_mcp.metrics import tier2 as T2
from bullhorn_mcp.metrics import tier2_policy as P
from bullhorn_mcp.metrics.compute import CountResult
from bullhorn_mcp.reads import records as rec_service
from bullhorn_mcp.tenant.capabilities import ACTIVITY_CONCEPTS
from bullhorn_mcp.tenant.profile_v2 import ProfileError, Settings, TenantProfileV2

from ._phase5c_helpers import (
    FULL_CONFIG,
    OFFER_EXTENDED,
    FakeClock,
    bh_calls,
    context,
    local_ident,
    make_client,
    tenant_store,
)
from ._tenant_helpers import REST_URL

# Record-level sentinels planted in every fixture row (AC-10): none may reach a Tier 2 output.
SENTINEL_NAME = "Zqmetricsname"
SENTINEL_EMAIL = "zq.metrics@example.test"
SENTINEL_CLIENT = "Zqclientcorp"
SENTINEL_NOTE = "Zqnotetext confidential"
ID_BASE = 987_650_000  # distinctive record IDs (never in a Tier 2 output)

ENTITY_OF = {
    "job_created": ("JobOrder", {}),
    "client_submission": ("JobSubmission", {"status": "Client Submitted"}),
    "interview_scheduled": ("Appointment", {"type": "Interview", "parentAppointment": None}),
    "offer_extended": ("JobSubmission", {"status": "Offer Extended"}),
    "placement_created": ("Placement", {}),
}
METRIC_CONCEPT = {
    "jobs_created": "job_created",
    "client_submissions": "client_submission",
    "interviews_scheduled": "interview_scheduled",
    "offers_extended": "offer_extended",
    "placements_created": "placement_created",
}
_RANGE_RE = re.compile(r"dateAdded >= (\d+) AND dateAdded < (\d+)")
_IN_RE = re.compile(r"(\w+) IN \(([^)]*)\)")


def ms(year: int, month: int, day: int = 15) -> int:
    return calendar.timegm((year, month, day, 12, 0, 0)) * 1000


class FakeBullhorn:
    """A synthetic Bullhorn ``/query`` that honours the dateAdded range, ``IN`` filters and paging."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.next_id = ID_BASE
        self.tokens: list[str] = []
        self.before: Any = None  # optional hook(request) run before answering

    def add(self, concept: str, year: int, month: int, n: int) -> None:
        entity, extra = ENTITY_OF[concept]
        for _ in range(n):
            self.next_id += 1
            self.rows.setdefault(entity, []).append({
                "id": self.next_id, "dateAdded": ms(year, month), "isDeleted": False, **extra,
                "firstName": SENTINEL_NAME, "email": SENTINEL_EMAIL, "clientCorporation": {"id": self.next_id, "name": SENTINEL_CLIENT},
                "comments": SENTINEL_NOTE, "candidate": {"id": self.next_id + 1}, "jobOrder": {"id": self.next_id + 2},
                "sendingUser": {"id": self.next_id + 3}, "owner": {"id": self.next_id + 4}, "customInt3": self.next_id + 5,
            })

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.before is not None:
            self.before(request)
        self.tokens.append(request.headers.get("BhRestToken", ""))
        entity = request.url.path.rsplit("/", 1)[-1]
        q = request.url.params
        where = q["where"]
        rows = self.rows.get(entity, [])
        rng = _RANGE_RE.search(where)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            rows = [r for r in rows if lo <= r["dateAdded"] < hi]
        for name, values in _IN_RE.findall(where):
            allowed = {v.strip().strip("'") for v in values.split(",")}
            rows = [r for r in rows if r.get(name) in allowed]
        start, count = int(q["start"]), int(q["count"])
        return httpx.Response(200, json={"start": start, "count": len(rows[start:start + count]), "data": rows[start:start + count]})

    def mock(self, rest_url: str = REST_URL) -> None:
        respx.get(url__regex=re.escape(rest_url) + r"/query/[A-Za-z]+").mock(side_effect=self.handler)


def dataset() -> FakeBullhorn:
    fake = FakeBullhorn()
    plan = {
        "job_created": (12, 3, 15),
        "client_submission": (30, 25, 11),
        "interview_scheduled": (14, 9, 12),
        "offer_extended": (10, 0, 20),
        "placement_created": (11, 12, 4),
    }
    for concept, counts in plan.items():
        for month, n in zip((1, 2, 3), counts):
            fake.add(concept, 2026, month, n)
    return fake


Q1 = {"date_from": "2026-01-01", "date_to": "2026-04-01"}
ALL_METRICS = list(C.load().metrics)


@pytest.fixture
def store(tmp_path):
    return tenant_store(tmp_path)


def tier1(store, args: dict[str, Any], *, client=None, clock=None, ctx=None) -> dict[str, Any]:
    ctx = ctx or context(store, client=client, clock=clock)
    client = client or ctx.client
    return M.run_tier1(ctx, {"period": "month", **args}, lambda: client)


def counts(**months: tuple[int, ...]) -> dict[str, CountResult]:
    return {k: CountResult("ok", months=v) for k, v in months.items()}


def req(metrics: list[str], period: str = "month", **bounds: str) -> M.Request:
    out = M.validate({"metrics": metrics, "period": period, **(bounds or Q1)}, C.load())
    assert isinstance(out, M.Request)
    return out


def keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


# ---------------------------------------------------------------------- #
# Catalog (§2)
# ---------------------------------------------------------------------- #


class TestCatalog:
    def test_packaged_catalog(self):
        cat = C.load()
        assert cat.version == 1 and list(cat.metrics) == [
            "jobs_created", "client_submissions", "interviews_scheduled", "offers_extended", "placements_created",
            "submission_to_interview", "interview_to_offer", "offer_to_placement",
        ]
        assert {cat.concept_of(m) for m, v in cat.metrics.items() if v.kind == "count"} <= set(ACTIVITY_CONCEPTS)
        assert cat.counts_for("submission_to_interview") == ("interviews_scheduled", "client_submissions")
        assert cat.counts_for("interview_to_offer") == ("offers_extended", "interviews_scheduled")
        assert cat.counts_for("offer_to_placement") == ("placements_created", "offers_extended")
        for mid, concept in METRIC_CONCEPT.items():
            assert cat.concept_of(mid) == concept

    def test_packaged_resource_in_wheel_tree(self):
        assert resources.files("bullhorn_mcp.mappings").joinpath("metric_catalog.yaml").is_file()

    @pytest.mark.parametrize(
        "text",
        [
            "version: 2\nmetrics: [{id: a, kind: count, concept: job_created}]\n",
            "version: 1\nmetrics: [{id: a, kind: count, concept: not_a_concept}]\n",
            "version: 1\nmetrics: [{id: a, kind: ratio, numerator: b, denominator: a}]\n",
            "version: 1\nmetrics: [{id: a, kind: count, concept: job_created, extra: 1}]\n",
            "version: 1\nmetrics: []\n",
        ],
    )
    def test_malformed_rejected(self, text):
        with pytest.raises(C.CatalogError):
            C.parse(text)


# ---------------------------------------------------------------------- #
# Validation (AC-3): rejected_validation, zero Bullhorn calls
# ---------------------------------------------------------------------- #


BAD_ARGS = [
    {"metrics": ["unknown_metric"], **Q1},
    {"metrics": ALL_METRICS + ["jobs_created"], **Q1},
    {"metrics": [], **Q1},
    {"metrics": ["jobs_created", "jobs_created"], **Q1},
    {"metrics": "jobs_created", **Q1},
    {"metrics": ["jobs_created"], "date_from": "2026-01-02", "date_to": "2026-04-01"},
    {"metrics": ["jobs_created"], "date_from": "2026-02-01", "date_to": "2026-05-01", "period": "quarter"},
    {"metrics": ["jobs_created"], "date_from": "2026-01-01T00:00:00", "date_to": "2026-04-01"},
    {"metrics": ["jobs_created"], "date_from": "2026-01-01T00:00:00+00:00", "date_to": "2026-04-01"},
    {"metrics": ["jobs_created"], "date_from": "2026-01-01", "date_to": "2026-04-01Z"},
    {"metrics": ["jobs_created"], "date_from": 20260101, "date_to": "2026-04-01"},
    {"metrics": ["jobs_created"], "date_from": "2026-13-01", "date_to": "2027-01-01"},
    {"metrics": ["jobs_created"], "date_from": "2025-01-01", "date_to": "2026-02-01"},
    {"metrics": ["jobs_created"], "date_from": "2025-01-01", "date_to": "2026-04-01", "period": "quarter"},
    {"metrics": ["jobs_created"], "date_from": "2026-04-01", "date_to": "2026-01-01"},
    {"metrics": ["jobs_created"], "date_from": "2026-01-01", "date_to": "2026-01-01"},
    {"metrics": ["jobs_created"], **Q1, "period": "week"},
    {"metrics": ["jobs_created"], **Q1, "period": None},
]


class TestValidation:
    @respx.mock
    @pytest.mark.parametrize("args", BAD_ARGS)
    def test_rejected_without_bullhorn_calls(self, store, args):
        resolve = Mock(side_effect=AssertionError("no client may be resolved before validation"))
        out = M.run_tier1(context(store), {"period": "month", **args}, resolve)
        assert out["status"] == "rejected_validation" and out["errors"]
        assert not bh_calls() and not resolve.called

    @respx.mock
    @pytest.mark.parametrize("args", BAD_ARGS)
    def test_tier2_rejected_without_service_identity(self, store, args, monkeypatch):
        monkeypatch.setattr(T2.sessions, "service_client", Mock(side_effect=AssertionError("service identity")))
        out = T2.run(workspace_ident(), {"period": "month", **args}, rec_env(store), NOW_UTC)
        assert out["status"] == "rejected_validation" and out["tier"] == "workspace_only"
        assert {e["item"] for e in out["errors"]} <= set(P.VALIDATION_ITEMS) and not bh_calls()

    def test_errors_never_echo_values(self, store):
        out = M.run_tier1(context(store), {"metrics": [SENTINEL_NAME], "date_from": SENTINEL_EMAIL, "date_to": "x", "period": "q"},
                          lambda: None)
        text = str(out)
        assert SENTINEL_NAME not in text and SENTINEL_EMAIL not in text

    def test_valid_shapes(self):
        r = req(["jobs_created"], "quarter", date_from="2025-10-01", date_to="2026-10-01")
        assert len(r.months) == 12 and [p[0].isoformat() for p in r.periods] == ["2025-10-01", "2026-01-01", "2026-04-01", "2026-07-01"]
        assert r.periods[0][1] == (0, 1, 2)
        r = req(["jobs_created"], date_from="2025-11-01", date_to="2026-11-01")
        assert len(r.periods) == 12 and r.periods[2][0] == dt.date(2026, 1, 1)


# ---------------------------------------------------------------------- #
# Parity with get_activity (AC-4) and Tier 1 output
# ---------------------------------------------------------------------- #


class TestTier1:
    @respx.mock
    def test_counts_equal_get_activity_per_month(self, store):
        fake = dataset()
        fake.mock()
        out = tier1(store, {"metrics": list(METRIC_CONCEPT), **Q1})
        assert out["status"] == "ok" and out["tier"] == "local"
        for mid, concept in METRIC_CONCEPT.items():
            for cell in out["metrics"][mid]["cells"]:
                start = dt.date.fromisoformat(cell["period_start"])
                end = dt.date(start.year + start.month // 12, start.month % 12 + 1, 1)
                act = A.get_activity(context(store), {"concepts": [concept], "date_from": start.isoformat(),
                                                      "date_to": end.isoformat(), "limit": 200})
                block = act["concepts"][concept]
                assert block["complete"] is True and cell["value"] == len(block["events"]), (mid, cell)
        assert [c["value"] for c in out["metrics"]["client_submissions"]["cells"]] == [30, 25, 11]
        assert out["metrics"]["client_submissions"]["total"] == 66

    @respx.mock
    def test_quarter_and_ratios(self, store):
        dataset().mock()
        out = tier1(store, {"metrics": ["interviews_scheduled", "submission_to_interview", "interview_to_offer"], **Q1,
                            "period": "quarter"})
        m = out["metrics"]
        assert m["interviews_scheduled"]["cells"] == [{"period_start": "2026-01-01", "value": 35}]
        assert m["submission_to_interview"]["cells"][0]["value"] == pytest.approx(35 / 66)
        assert m["interview_to_offer"]["total"] == pytest.approx(30 / 35)

    @respx.mock
    def test_ratio_zero_denominator_is_null(self, store):
        dataset().mock()
        out = tier1(store, {"metrics": ["offer_to_placement"], **Q1})
        assert out["metrics"]["offer_to_placement"]["cells"][1] == {"period_start": "2026-02-01", "value": None}

    @respx.mock
    def test_provenance_and_no_record_ids(self, store):
        dataset().mock()
        out = tier1(store, {"metrics": ALL_METRICS, **Q1})
        prov = out["provenance"]
        assert {"profile_version", "metric_catalog_version", "policy_version", "request_hash"} <= set(prov)
        text = str(out)
        for sentinel in (SENTINEL_NAME, SENTINEL_EMAIL, SENTINEL_CLIENT, SENTINEL_NOTE, str(ID_BASE + 1)[:6], "activity_id", "links"):
            assert sentinel not in text

    @respx.mock
    def test_missing_concept_matches_get_activity(self, tmp_path):
        store = tenant_store(tmp_path, [c for c in FULL_CONFIG if c is not OFFER_EXTENDED])
        dataset().mock()
        out = tier1(store, {"metrics": ["offers_extended", "interview_to_offer", "jobs_created"], **Q1})
        act = A.get_activity(context(store), {"concepts": ["offer_extended"], **Q1})["concepts"]["offer_extended"]
        for mid in ("offers_extended", "interview_to_offer"):
            got = out["metrics"][mid]
            assert got["status"] == act["status"] == "definition_missing"
            assert got["missing_requirements"] == act["missing_requirements"] and got["cells"] == [] and "total" not in got
        assert out["metrics"]["jobs_created"]["status"] == "ok"

    @respx.mock
    def test_setup_required_top_level(self, tmp_path):
        from ._tenant_helpers import make_store

        out = tier1(make_store(tmp_path), {"metrics": ["jobs_created"], **Q1})
        assert out["status"] == "setup_required" and not bh_calls()


# ---------------------------------------------------------------------- #
# Incomplete enumeration (AC-5, SM-7)
# ---------------------------------------------------------------------- #


class TestIncomplete:
    def test_service_max_pages_default_unchanged(self):
        sig = inspect.signature(A.get_activity)
        assert sig.parameters["max_pages"].default == 5 == rec_service.MAX_PAGES
        assert sig.parameters["max_pages"].kind is inspect.Parameter.KEYWORD_ONLY

    @respx.mock
    def test_cursor_followed_beyond_one_call(self, store):
        fake = FakeBullhorn()
        fake.add("client_submission", 2026, 1, 450)
        fake.mock()
        out = tier1(store, {"metrics": ["client_submissions"], "date_from": "2026-01-01", "date_to": "2026-02-01"})
        assert out["metrics"]["client_submissions"]["cells"] == [{"period_start": "2026-01-01", "value": 450}]

    @respx.mock
    def test_page_cap_gives_incomplete_without_number(self, store, monkeypatch):
        monkeypatch.setattr(M, "METRICS_MAX_PAGES", 2)
        fake = FakeBullhorn()
        fake.add("client_submission", 2026, 1, 250)
        fake.add("job_created", 2026, 1, 20)
        fake.mock()
        args = {"metrics": ["client_submissions", "submission_to_interview", "jobs_created"], "date_from": "2026-01-01",
                "date_to": "2026-02-01"}
        out = tier1(store, args)
        for mid in ("client_submissions", "submission_to_interview"):
            assert out["metrics"][mid] == {"status": "incomplete", "cells": []}
        assert out["metrics"]["jobs_created"]["cells"][0]["value"] == 20
        t2 = P.build(req(args["metrics"], date_from="2026-01-01", date_to="2026-02-01"), C.load(),
                     {"client_submissions": CountResult("incomplete"), "interviews_scheduled": CountResult("ok", months=(50,)),
                      "jobs_created": CountResult("ok", months=(20,))}, 10, "UTC", now=NOW_UTC)
        P.validate_output(t2, C.load())
        assert t2["metrics"]["client_submissions"] == {"status": "incomplete", "cells": []}

    @respx.mock
    def test_large_page_budget_continues_past_fetch_cap(self, store):
        """With ``max_pages`` 40 the service keeps paging past fetch's own 5-page cap (deleted rows filtered)."""
        rows = [{"id": i, "dateAdded": ms(2026, 1), "isDeleted": True} for i in range(1, 101)]
        responses = [httpx.Response(200, json={"data": rows})] * 7 + [httpx.Response(200, json={"data": []})]
        route = respx.get(f"{REST_URL}/query/JobSubmission").mock(side_effect=responses)
        args = {"concepts": ["submission_created"], "date_from": "2026-01-01", "date_to": "2026-02-01", "limit": 10}
        out = A.get_activity(context(store), args, max_pages=40)
        assert route.call_count == 8 and out["concepts"]["submission_created"]["complete"] is True
        respx.reset()
        route = respx.get(f"{REST_URL}/query/JobSubmission").mock(side_effect=responses)
        out = A.get_activity(context(store), args)
        assert route.call_count == 5 and out["concepts"]["submission_created"]["complete"] is False

    @respx.mock
    def test_time_budget_gives_incomplete_in_both_tiers(self, store, monkeypatch):
        clock = FakeClock()
        fake = dataset()
        fake.before = lambda request: setattr(clock, "t", clock.t + 16)  # each request costs 16 s of the 30 s budget
        fake.mock()
        out = tier1(store, {"metrics": ["jobs_created", "client_submissions", "placements_created"], **Q1}, clock=clock)
        assert out["status"] == "ok"
        assert out["metrics"]["jobs_created"]["status"] == "incomplete"
        assert all(m == {"status": "incomplete", "cells": []} for m in out["metrics"].values())
        clock2 = FakeClock()
        fake.before = lambda request: setattr(clock2, "t", clock2.t + 16)
        monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
        _clock_tier2(monkeypatch, clock2)
        t2 = T2.run(workspace_ident(), {"metrics": ["jobs_created", "client_submissions"], **Q1, "period": "month"}, rec_env(store),
                    NOW_UTC)
        assert t2["status"] == "ok" and all(m == {"status": "incomplete", "cells": []} for m in t2["metrics"].values())

    @respx.mock
    def test_sm7_worst_case_bounded(self, store, monkeypatch):
        """8 metrics x 12 months x 40 pages: the 30 s budget ends the call as ``incomplete``, never an error."""
        clock = FakeClock()

        def endless(request):
            clock.t += 0.05
            n = int(request.url.params["count"])
            start = int(request.url.params["start"])
            return httpx.Response(200, json={"data": [{"id": start + i + 1, "dateAdded": ms(2025, 6), "isDeleted": True}
                                                      for i in range(n)]})

        respx.get(url__regex=re.escape(REST_URL) + r"/query/[A-Za-z]+").mock(side_effect=endless)
        out = tier1(store, {"metrics": ALL_METRICS, "date_from": "2025-01-01", "date_to": "2026-01-01"}, clock=clock)
        assert out["status"] == "ok" and all(m["status"] == "incomplete" and m["cells"] == [] for m in out["metrics"].values())
        assert clock.t - 1000.0 <= 31 and len(respx.calls) <= 40 * 12 * 5


# ---------------------------------------------------------------------- #
# Tier 2 policy (AC-8, AC-9; SR-37, SR-38)
# ---------------------------------------------------------------------- #


def _clock_tier2(monkeypatch, clock) -> None:
    """Give the Tier 2 path's read context a fake clock (no real sleeps)."""
    from ._phase5c_helpers import reader_factory

    def make(*args, **kwargs):
        ctx = rec_service.make_context(*args, **kwargs)
        ctx.reader_factory = reader_factory(clock)
        return ctx

    monkeypatch.setattr(T2, "make_context", make)


NOW_UTC = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.timezone.utc)


def rec_env(store) -> dict[str, str]:
    from ._tenant_helpers import env

    return env(store)


def workspace_ident():
    from bullhorn_mcp.identity.principal import IdentityContext
    from bullhorn_mcp.identity.roles import Roles

    tenant = Mock()
    tenant.tenant_key = "tenant-1"
    tenant.roles = Roles(analytics_viewers=frozenset({"p-ws"}))  # Phase 6 M2: the analytics grant
    return IdentityContext(initiating_principal="p-ws", principal_display="p-ws", tenant_key="tenant-1", executing_bullhorn_identity=None,
                           mode="user", access_tier="workspace_only", tenant=tenant)


class TestTier2Policy:
    def test_default_k_and_setting(self):
        assert P.DEFAULT_K == 10 and P.cohort(None) == 10
        profile = Mock(spec=TenantProfileV2)
        profile.settings = Settings()
        assert P.cohort(profile) == 10
        profile.settings = Settings(tier2_min_cohort=25)
        assert P.cohort(profile) == 25
        profile.settings = Settings(tier2_min_cohort=4)  # never below the floor, even if a profile slipped through
        assert P.cohort(profile) == 10

    def test_cells_below_k_suppressed_including_zero(self):
        doc = P.build(req(["jobs_created"]), C.load(), counts(jobs_created=(9, 0, 10)), 10, "UTC", now=NOW_UTC)
        P.validate_output(doc, C.load())
        cells = doc["metrics"]["jobs_created"]["cells"]
        assert cells[0] == cells[0] | {"value": None, "suppressed": True, "reason": "insufficient_aggregate_population"}
        assert cells[1]["suppressed"] is True and cells[1]["value"] is None
        assert cells[2] == {"period_start": "2026-03-01", "value": 10, "suppressed": False}

    def test_ratio_needs_both_sides_at_least_k(self):
        c = counts(interviews_scheduled=(10, 9, 40), client_submissions=(9, 30, 80))
        doc = P.build(req(["submission_to_interview"]), C.load(), c, 10, "UTC", now=NOW_UTC)
        cells = doc["metrics"]["submission_to_interview"]["cells"]
        assert [x["suppressed"] for x in cells] == [True, True, False]
        assert cells[2]["value"] == pytest.approx(0.5) and cells[0]["value"] is None and cells[1]["value"] is None

    def test_quarter_suppressed_if_any_month_below_k(self):
        """SM-3: a quarter cell is a margin over its months."""
        c = counts(jobs_created=(20, 4, 20, 10, 10, 10), client_submissions=(50, 50, 50, 9, 50, 50),
                   interviews_scheduled=(30, 30, 30, 30, 30, 30))
        r = req(["jobs_created", "submission_to_interview"], "quarter", date_from="2026-01-01", date_to="2026-07-01")
        doc = P.build(r, C.load(), c, 10, "UTC", now=NOW_UTC)
        P.validate_output(doc, C.load())
        assert [x["value"] for x in doc["metrics"]["jobs_created"]["cells"]] == [None, 30]
        assert [x["suppressed"] for x in doc["metrics"]["submission_to_interview"]["cells"]] == [False, True]

    def test_no_margins_anywhere(self):
        c = counts(**{m: (50, 60, 70) for m in METRIC_CONCEPT})
        doc = P.build(req(ALL_METRICS), C.load(), c, 10, "UTC", now=NOW_UTC)
        P.validate_output(doc, C.load())
        assert not keys(doc) & {"total", "totals", "sum", "average", "mean", "provenance", "request_hash", "warnings", "cursor",
                                "next_cursor", "links", "events", "activity_id"}
        assert keys(doc) <= {"status", "tier", "period", "reporting_timezone", "definition", "metric_catalog_version", "policy_version",
                             "k", "metrics", *ALL_METRICS, "missing_requirements", "cells", "period_start", "value", "suppressed",
                             "reason"}

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(total=1),
            lambda d: d.update(provenance={}),
            lambda d: d["definition"].update(request_hash="x"),
            lambda d: d["metrics"]["jobs_created"].update(total=150),
            lambda d: d["metrics"]["jobs_created"]["cells"][0].update(record_id=1),
            lambda d: d["metrics"]["jobs_created"]["cells"][0].update(value=3),
            lambda d: d["metrics"]["jobs_created"]["cells"][0].update(value="50"),
            lambda d: d["metrics"]["jobs_created"]["cells"][1].update(value=7, suppressed=True, reason=SUPPRESSED),
            lambda d: d["metrics"]["jobs_created"].update(status="ok", missing_requirements=["x"]),
            lambda d: d["metrics"].update(not_a_metric={"status": "ok", "cells": []}),
            lambda d: d.update(tier="bullhorn_user"),
            lambda d: d["definition"].update(k=4),
            lambda d: d["metrics"]["jobs_created"].update(status="incomplete"),
        ],
    )
    def test_validator_rejects_injections(self, mutate):
        doc = P.build(req(["jobs_created"]), C.load(), counts(jobs_created=(50, 5, 60)), 10, "UTC", now=NOW_UTC)
        P.validate_output(doc, C.load())
        mutate(doc)
        with pytest.raises(P.PolicyViolation):
            P.validate_output(doc, C.load())

    @respx.mock
    def test_policy_violation_returns_nothing(self, store, monkeypatch):
        dataset().mock()
        monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
        real = P.build
        monkeypatch.setattr(P, "build", lambda *a, **kw: {**real(*a, **kw), "links": [ID_BASE + 1]})
        out = T2.run(workspace_ident(), {"metrics": ["jobs_created"], **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        assert out == {"status": "error", "tier": "workspace_only", "error": "policy_violation"}

    @respx.mock
    def test_golden_tier2_response(self, store, monkeypatch):
        fake = dataset()
        fake.mock()
        monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
        out = T2.run(workspace_ident(), {"metrics": ALL_METRICS, **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        P.validate_output(out, C.load())
        assert set(fake.tokens) == {"svc-token"}
        assert [c["value"] for c in out["metrics"]["client_submissions"]["cells"]] == [30, 25, 11]
        assert [c["value"] for c in out["metrics"]["jobs_created"]["cells"]] == [12, None, 15]
        assert [c["suppressed"] for c in out["metrics"]["interview_to_offer"]["cells"]] == [False, True, False]
        assert out["metrics"]["interview_to_offer"]["cells"][0]["value"] == pytest.approx(10 / 14)
        text = str(out)
        for sentinel in (SENTINEL_NAME, SENTINEL_EMAIL, SENTINEL_CLIENT, SENTINEL_NOTE, str(ID_BASE)[:6], "activity_id"):
            assert sentinel not in text
        quarter = T2.run(workspace_ident(), {"metrics": ALL_METRICS, **Q1, "period": "quarter"}, rec_env(store), NOW_UTC)
        P.validate_output(quarter, C.load())
        assert quarter["metrics"]["client_submissions"]["cells"] == [{"period_start": "2026-01-01", "value": 66, "suppressed": False}]
        assert quarter["metrics"]["jobs_created"]["cells"][0]["suppressed"] is True  # February had 3

    @respx.mock
    def test_unavailable_concept_in_tier2(self, tmp_path, monkeypatch):
        store = tenant_store(tmp_path, [c for c in FULL_CONFIG if c is not OFFER_EXTENDED])
        dataset().mock()
        monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
        out = T2.run(workspace_ident(), {"metrics": ["offers_extended", "offer_to_placement"], **Q1, "period": "month"}, rec_env(store),
                     NOW_UTC)
        P.validate_output(out, C.load())
        for mid in ("offers_extended", "offer_to_placement"):
            assert out["metrics"][mid] == {"status": "definition_missing", "cells": [],
                                           "missing_requirements": ["value_mapping:submission|placement:offer_extended"]}

    def test_no_service_principal_configured(self, store, monkeypatch):
        ident = workspace_ident()
        ident.tenant.service = None
        monkeypatch.setattr(T2.sessions, "service_client", Mock(side_effect=AssertionError("must not be called")))
        out = T2.run(ident, {"metrics": ["jobs_created"], **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        assert out == {"status": "unavailable", "tier": "workspace_only", "error": "service_identity_not_configured"}

    def test_non_workspace_identity_refused(self, store, monkeypatch):
        monkeypatch.setattr(T2.sessions, "service_client", Mock(side_effect=AssertionError("must not be called")))
        out = T2.run(local_ident(), {"metrics": ["jobs_created"], **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        assert out["status"] == "unavailable"

    @respx.mock
    def test_errors_are_generic(self, store, monkeypatch):
        monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
        respx.get(url__regex=re.escape(REST_URL) + r"/query/[A-Za-z]+").mock(
            return_value=httpx.Response(500, text=f"boom {SENTINEL_NAME} {SENTINEL_EMAIL} BhRestToken=svc-token")
        )
        out = T2.run(workspace_ident(), {"metrics": ["jobs_created"], **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        assert out == {"status": "error", "tier": "workspace_only", "error": "bullhorn_error"}
        respx.reset()
        respx.get(url__regex=re.escape(REST_URL) + r"/query/[A-Za-z]+").mock(return_value=httpx.Response(429))
        _clock_tier2(monkeypatch, FakeClock())
        out = T2.run(workspace_ident(), {"metrics": ["jobs_created"], **Q1, "period": "month"}, rec_env(store), NOW_UTC)
        assert out == {"status": "rate_limited", "tier": "workspace_only", "error": "rate_limited"}


SUPPRESSED = "insufficient_aggregate_population"


# ---------------------------------------------------------------------- #
# The tier2_min_cohort setting (P-1, AC-8)
# ---------------------------------------------------------------------- #


def _profile_dict(store) -> dict[str, Any]:
    return store.read_version(store.active_version()).to_dict()


class TestCohortSetting:
    @pytest.mark.parametrize("value", [4, 0, -1, 1001, True, "10", 10.0, None])
    def test_invalid_values_rejected(self, store, value):
        data = _profile_dict(store)
        data["settings"]["tier2_min_cohort"] = value
        with pytest.raises(ProfileError, match="tier2_min_cohort"):
            TenantProfileV2.from_dict(data, source="<test>")

    @pytest.mark.parametrize("value", [5, 10, 1000])
    def test_valid_values_round_trip(self, store, value):
        data = _profile_dict(store)
        assert "tier2_min_cohort" not in data["settings"]  # emitted only when set
        data["settings"]["tier2_min_cohort"] = value
        doc = TenantProfileV2.from_dict(data, source="<test>")
        assert doc.settings.tier2_min_cohort == value and doc.to_dict()["settings"]["tier2_min_cohort"] == value

    def test_admin_import_sets_k_and_k4_rejected(self, store, tmp_path):
        import yaml

        from bullhorn_mcp.tenant.changes import propose

        from ._tenant_helpers import NOW, env, propose_and_commit

        data = _profile_dict(store)
        data["settings"]["tier2_min_cohort"] = 25
        good = tmp_path / "import-good.yaml"
        good.write_text(yaml.safe_dump(data), encoding="utf-8")
        propose_and_commit(store, [{"op": "import_document", "path": str(good)}])
        assert P.cohort(store.read_version(store.active_version())) == 25
        data["settings"]["tier2_min_cohort"] = 4
        bad = tmp_path / "import-bad.yaml"
        bad.write_text(yaml.safe_dump(data), encoding="utf-8")
        before = store.active_version()
        with pytest.raises(ProfileError, match="tier2_min_cohort"):
            propose(store, [{"op": "import_document", "path": str(bad)}], now=NOW, env=env(store))
        assert store.active_version() == before


# ---------------------------------------------------------------------- #
# Amendment M1-B: Tier 2 returns closed periods only (SR-37)
# ---------------------------------------------------------------------- #

MID_MARCH = dt.datetime(2026, 3, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
OPEN = {"value": None, "suppressed": True, "reason": "period_open"}


def _t2(store, monkeypatch, args, now):
    monkeypatch.setattr(T2.sessions, "service_client", Mock(return_value=make_client("svc-token")))
    return T2.run(workspace_ident(), {"period": "month", **args}, rec_env(store), now)


class TestClosedPeriodsOnly:
    @respx.mock
    def test_m1b_a_current_and_next_month_open(self, store, monkeypatch):
        fake = dataset()
        fake.add("client_submission", 2026, 4, 12)
        fake.mock()
        out = _t2(store, monkeypatch, {"metrics": ["client_submissions", "submission_to_interview"], "date_from": "2026-01-01",
                                       "date_to": "2026-05-01"}, MID_MARCH)
        P.validate_output(out, C.load())
        cells = out["metrics"]["client_submissions"]["cells"]
        assert [c["value"] for c in cells[:2]] == [30, 25]
        assert cells[2] == {"period_start": "2026-03-01", **OPEN} and cells[3] == {"period_start": "2026-04-01", **OPEN}
        ratio = out["metrics"]["submission_to_interview"]["cells"]
        assert ratio[0]["suppressed"] is False and ratio[2]["reason"] == ratio[3]["reason"] == "period_open"

    @respx.mock
    def test_m1b_b_quarter_containing_current_month_open(self, store, monkeypatch):
        fake = dataset()
        for month in (10, 11, 12):
            fake.add("client_submission", 2025, month, 10)
        fake.mock()
        out = _t2(store, monkeypatch, {"metrics": ["client_submissions"], "date_from": "2025-10-01", "date_to": "2026-04-01",
                                       "period": "quarter"}, MID_MARCH)
        P.validate_output(out, C.load())
        assert out["metrics"]["client_submissions"]["cells"] == [
            {"period_start": "2025-10-01", "value": 30, "suppressed": False},
            {"period_start": "2026-01-01", **OPEN},  # Jan 30 and Feb 25 are >= k, but March is open
        ]

    @pytest.mark.parametrize(
        "now, open_",
        [
            (dt.datetime(2026, 3, 1, 6, 0, 0, tzinfo=dt.timezone.utc) - dt.timedelta(hours=1), True),  # 23:00 Feb 28 CST (UTC-6)
            (dt.datetime(2026, 3, 1, 6, 0, 0, tzinfo=dt.timezone.utc), False),  # local midnight Mar 1
            (dt.datetime(2026, 3, 1, 7, 0, 0, tzinfo=dt.timezone.utc), False),
        ],
    )
    def test_m1b_c_local_midnight_boundary(self, now, open_):
        r = req(["jobs_created"], date_from="2026-02-01", date_to="2026-03-01")
        doc = P.build(r, C.load(), counts(jobs_created=(40,)), 10, "America/Chicago", now=now)
        P.validate_output(doc, C.load())
        cell = doc["metrics"]["jobs_created"]["cells"][0]
        assert (cell == {"period_start": "2026-02-01", **OPEN}) is open_
        assert open_ or cell["value"] == 40

    @respx.mock
    def test_m1b_d_increment_in_open_month_invisible(self, store, monkeypatch):
        import json

        fake = dataset()
        fake.mock()
        args = {"metrics": ["placements_created", "offer_to_placement"], **Q1}
        first = json.dumps(_t2(store, monkeypatch, args, MID_MARCH), sort_keys=True)
        fake.add("placement_created", 2026, 3, 1)  # one new placement in the open month (11 -> 12)
        second = json.dumps(_t2(store, monkeypatch, args, MID_MARCH), sort_keys=True)
        assert first == second and '"status": "ok"' in first

    @respx.mock
    def test_m1b_e_tier1_still_shows_open_periods(self, store):
        dataset().mock()
        out = tier1(store, {"metrics": ["client_submissions"], "date_from": "2026-01-01", "date_to": "2026-05-01"},
                    ctx=context(store, now=MID_MARCH))
        assert [c["value"] for c in out["metrics"]["client_submissions"]["cells"]] == [30, 25, 11, 0]

    def test_validator_reasons(self):
        doc = P.build(req(["jobs_created"]), C.load(), counts(jobs_created=(50, 50, 50)), 10, "UTC", now=MID_MARCH)
        P.validate_output(doc, C.load())
        doc["metrics"]["jobs_created"]["cells"][2]["reason"] = "period_closed_soon"
        with pytest.raises(P.PolicyViolation):
            P.validate_output(doc, C.load())
