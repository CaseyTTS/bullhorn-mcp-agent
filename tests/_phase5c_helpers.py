"""Shared helpers for the Phase 5C read tests (not a test module).

Every value here is synthetic and modelled only on documented shapes (G-7): no
tenant names, mappings or captured payloads.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import respx

from bullhorn_mcp.auth import BullhornAuth, BullhornSession
from bullhorn_mcp.bullhorn import reads as bh_reads
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.identity.principal import IdentityContext
from bullhorn_mcp.reads import records as rec_service
from bullhorn_mcp.tenant.profile_v2 import rest_url_fingerprint

from ._tenant_helpers import NOW, REST_URL, env, field_entry, init_tenant, make_store, propose_and_commit, snapshot, tenant_payloads
from ._tenant_helpers import write_snapshot

TOKEN = "tok5c_" + "B" * 40
QUERY_RE = re.compile(r"^" + re.escape(REST_URL) + r"/query/[A-Za-z]+$")
FP = rest_url_fingerprint(REST_URL)

# Tenant custom fields used by the synthetic tenant (labels only; no real tenant data).
APPOINTMENT_EXTRA = {"customText1": {"label": "Interview State"}}
PLACEMENT_EXTRA = {"customInt1": {"label": "Credited Recruiter"}, "customInt2": {"label": "Client Company"}}
JOB_EXTRA = {
    "customText12": {"label": "Priority", "options": [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]},
    "customInt3": {"label": "Primary Recruiter"},
}
CANDIDATE_EXTRA = {"customEncryptedText1": {"label": "Restricted"}, "customText3": {"label": "Region"}}

PRIMARY_RECRUITER = {"op": "set_field_mapping", "entity": "job", "field": "primary_recruiter_id", "target": "customInt3"}
PRIORITY = {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"}
PRIORITY_ORDER = {
    "op": "set_value_mapping", "key": "job.priority.order", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
    "bullhorn_field": "customText12", "values": ["A", "B"],
}
APPOINTMENT_STATUS = {"op": "set_field_mapping", "entity": "appointment", "field": "status", "target": "customText1"}
PLACEMENT_RECRUITER = {"op": "set_field_mapping", "entity": "placement", "field": "recruiter_id", "target": "customInt1"}
PLACEMENT_CLIENT = {"op": "set_field_mapping", "entity": "placement", "field": "client_corporation_id", "target": "customInt2"}


def concept(key: str, entity: str, field: str, name: str, values: list[Any]) -> dict[str, Any]:
    return {"op": "set_value_mapping", "key": key, "entity": entity, "target": {"kind": "concept", "name": name},
            "bullhorn_field": field, "values": values}


CLIENT_SUBMISSION = concept("sub.client", "submission", "status", "client_submission", ["Client Submitted"])
INTERVIEW = concept("appt.interview", "appointment", "type", "interview_scheduled", ["Interview"])
INTERVIEW_DONE = concept("appt.done", "appointment", "customText1", "interview_completed", ["Done"])
INTERVIEW_CANCELLED = concept("appt.cancelled", "appointment", "customText1", "interview_cancelled", ["Cancelled"])
OFFER_EXTENDED = concept("sub.offer", "submission", "status", "offer_extended", ["Offer Extended"])
OFFER_ACCEPTED = concept("sub.accepted", "submission", "status", "offer_accepted", ["Offer Accepted"])
OFFER_DECLINED = concept("sub.declined", "submission", "status", "offer_declined", ["Offer Declined"])
DATING = {"op": "set_setting", "name": "client_submission_dating", "value": "submission_date_added"}
RULE_MAPPED = {"op": "set_setting", "name": "interview_completion_rule", "value": "mapped_state_only"}
RULE_END = {"op": "set_setting", "name": "interview_completion_rule", "value": "end_passed_not_cancelled"}

FULL_CONFIG = [
    PRIMARY_RECRUITER, PRIORITY, PRIORITY_ORDER, APPOINTMENT_STATUS, PLACEMENT_RECRUITER, PLACEMENT_CLIENT,
    CLIENT_SUBMISSION, INTERVIEW, INTERVIEW_DONE, INTERVIEW_CANCELLED, OFFER_EXTENDED, OFFER_ACCEPTED, OFFER_DECLINED,
    DATING, RULE_END,
]


def payloads() -> dict[str, dict[str, Any]]:
    out = tenant_payloads(JOB_EXTRA)
    for bh, extra in (("Appointment", APPOINTMENT_EXTRA), ("Placement", PLACEMENT_EXTRA), ("Candidate", CANDIDATE_EXTRA)):
        out[bh]["fields"].extend(field_entry(n, **dict(a)) for n, a in extra.items())
    for bh in ("Appointment", "Placement"):  # integer custom fields
        for f in out[bh]["fields"]:
            if f["name"].startswith("customInt"):
                f["dataType"] = "Integer"
    for f in out["JobOrder"]["fields"]:
        if f["name"] == "customInt3":
            f["dataType"] = "Integer"
    return out


def tenant_store(tmp_path, changes: list[dict[str, Any]] | None = None, *, drift: bool = False, rest_fp: str | None = FP):
    """A local setup store with a snapshot and an active profile (``changes`` after ``init_tenant``)."""
    store = make_store(tmp_path)
    write_snapshot(store, snapshot(payloads(), rest_fp=rest_fp))
    init_tenant(store, FULL_CONFIG if changes is None else changes)
    if drift:  # an unresolved drift report after the commit: setup_revalidation_required
        write_snapshot(store, snapshot(payloads(), rest_fp=rest_fp), drift=True)
    return store


def snapshot_only_store(tmp_path):
    store = make_store(tmp_path)
    write_snapshot(store, snapshot(payloads(), rest_fp=FP))
    return store


def commit_more(store, changes: list[dict[str, Any]]) -> None:
    propose_and_commit(store, changes)


def make_client(token: str = TOKEN, rest_url: str = REST_URL) -> BullhornClient:
    auth = Mock(spec=BullhornAuth)
    auth.session = BullhornSession(bh_rest_token=token, rest_url=rest_url, expires_at=time.time() + 600)
    return BullhornClient(auth)


def local_ident(name: str = "local:tester") -> IdentityContext:
    return IdentityContext(
        initiating_principal=name, principal_display=name, tenant_key=None, executing_bullhorn_identity=None,
        mode="local", access_tier="local",
    )


def user_ident(principal: str, tenant: str = "tenant-1", link: str = "bh-link:aaa") -> IdentityContext:
    return IdentityContext(
        initiating_principal=principal, principal_display=principal, tenant_key=tenant, executing_bullhorn_identity=link,
        mode="user", access_tier="bullhorn_user",
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def reader_factory(clock: FakeClock | None = None, rng=lambda: 1.0):
    clock = clock or FakeClock()

    def make(client, key):
        return bh_reads.EntityReader(client, key, clock=clock, sleep=clock.sleep, rng=rng)

    return make


def context(store, *, ident: IdentityContext | None = None, client: BullhornClient | None = None, now: dt.datetime = NOW,
            support=None, clock: FakeClock | None = None, **env_extra: str) -> rec_service.ReadContext:
    ctx = rec_service.make_context(ident or local_ident(), client or make_client(), env(store, **env_extra), now, support)
    ctx.reader_factory = reader_factory(clock)
    return ctx


def query_route(entity: str, pages: list[list[dict[str, Any]]] | None = None, status: int = 200):
    """Mock ``GET /query/{entity}``; successive calls return successive pages (the last repeats)."""
    pages = pages if pages is not None else [[]]
    responses = [httpx.Response(status, json={"start": 0, "count": len(p), "data": p}) for p in pages]
    route = respx.get(f"{REST_URL}/query/{entity}")
    if len(responses) == 1:
        route.mock(return_value=responses[0])
    else:
        route.mock(side_effect=responses + [responses[-1]] * 10)
    return route


def params(call: Any) -> dict[str, str]:
    q = parse_qs(urlsplit(str(call.request.url)).query, keep_blank_values=True)
    return {k: v[0] for k, v in q.items()}


def bh_calls() -> list[Any]:
    return [c for c in respx.calls if c.request.url.host.endswith("bullhornstaffing.com")]
