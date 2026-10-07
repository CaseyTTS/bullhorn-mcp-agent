"""Shared helpers for the Phase 4B notes/write tests (not a test module)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth, BullhornSession
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.tenant.profile_v2 import rest_url_fingerprint
from bullhorn_mcp.tenant.state import ConnectionCheck
from bullhorn_mcp.writes.pipeline import WriteContext

from ._tenant_helpers import NOW, REST_URL, env, init_tenant, make_store, snapshot, write_snapshot

TOKEN = "tok_" + "A" * 40
OK = ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint(REST_URL))
NOTE_ACTION = {
    "op": "set_value_mapping",
    "key": "note.action.screen",
    "target": {"kind": "note_action", "semantic": "candidate_screen"},
    "bullhorn_field": "action",
    "values": ["Screen Call", "Left Message"],
}
NOTE_ACTION_OTHER = {
    "op": "set_value_mapping",
    "key": "note.action.other",
    "target": {"kind": "note_action", "semantic": None},
    "bullhorn_field": "action",
    "values": ["Client Call"],
}
CANDIDATE = {"id": 100, "firstName": "Ada", "lastName": "Lovelace", "status": "Active", "owner": {"id": 7}, "isDeleted": False}
CANDIDATE_2 = {"id": 101, "firstName": "Alan", "lastName": "Turing", "status": "New", "owner": {"id": 7}, "isDeleted": False}
CONTACT = {"id": 300, "firstName": "Grace", "lastName": "Hopper", "clientCorporation": {"id": 9}, "isDeleted": False}
JOB = {"id": 200, "title": "Engineer", "status": "Open", "clientCorporation": {"id": 9}, "owner": {"id": 8}, "isDeleted": False}
NOTE_ID = 555
DATE_ADDED = 1_790_000_000_000
SECRET_COMMENT = "Confidential screening notes: salary 123k, relocating"


def valid_store(tmp_path, actions: list[dict[str, Any]] | None = None):
    store = make_store(tmp_path)
    write_snapshot(store, snapshot(rest_fp=OK.rest_url_fingerprint))
    init_tenant(store, [NOTE_ACTION, NOTE_ACTION_OTHER] if actions is None else actions)
    return store


def write_env(store, **extra: str) -> dict[str, str]:
    values = {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create", **extra}
    return env(store, **{k: v for k, v in values.items() if v is not None})


def make_client() -> BullhornClient:
    auth = Mock(spec=BullhornAuth)
    auth.session = BullhornSession(bh_rest_token=TOKEN, rest_url=REST_URL, expires_at=time.time() + 600)
    return BullhornClient(auth)


CURRENT_USER_ID = 7


def context(
    e: dict[str, str], client: BullhornClient | None = None, now=NOW, connection: ConnectionCheck = OK, current_user=lambda: CURRENT_USER_ID
) -> WriteContext:
    """A pipeline context. ``current_user`` is the (mocked) HV-B11 resolver; it only takes effect while
    ``pipeline.HV_B11_VERIFIED`` is patched to True (see ``hv_b11_verified``)."""
    return WriteContext(client=client or make_client(), env=e, now=now, connection=lambda: connection, current_user=current_user)


@pytest.fixture
def hv_b11_verified(monkeypatch):
    """Simulate a verified HV-B11 mechanism (the production default is unresolved: guard on)."""
    from bullhorn_mcp.writes import pipeline

    monkeypatch.setattr(pipeline, "HV_B11_VERIFIED", True)
    pipeline._USER_CACHE.clear()
    yield
    pipeline._USER_CACHE.clear()


def mock_targets(records: list[dict[str, Any]] | None = None) -> None:
    entities = {100: "Candidate", 101: "Candidate", 300: "ClientContact", 200: "JobOrder"}
    for rec in records or [CANDIDATE, CANDIDATE_2, CONTACT, JOB]:
        respx.get(f"{REST_URL}/entity/{entities[rec['id']]}/{rec['id']}").mock(
            return_value=httpx.Response(200, json={"data": rec})
        )


def readback(
    person: int | None = 100,
    job: int | None = None,
    candidates: list[int] | None = None,
    contacts: list[int] | None = None,
    entities: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": NOTE_ID,
        "dateAdded": DATE_ADDED,
        "action": "Screen Call",
        "isDeleted": False,
        "personReference": {"id": person} if person else None,
        "commentingPerson": {"id": 7},
        "jobOrder": {"id": job} if job else None,
        "candidates": {"total": len(candidates or []), "data": [{"id": i} for i in candidates or []]},
        "clientContacts": {"total": len(contacts or []), "data": [{"id": i} for i in contacts or []]},
    }
    if entities is None:
        entities = ([(person, "User")] if person else []) + ([(job, "JobOrder")] if job else [])
        entities += [(i, "User") for i in (candidates or []) + (contacts or [])]
    data["entities"] = {"total": len(entities), "data": [{"targetEntityID": i, "targetEntityName": n} for i, n in entities]}
    return data


def mock_write(created: dict[str, Any] | None = None, read: dict[str, Any] | None = None, status: int = 200):
    put = respx.put(f"{REST_URL}/entity/Note").mock(
        return_value=httpx.Response(status, json=created or {"changedEntityId": NOTE_ID, "changeType": "INSERT"})
    )
    get = respx.get(f"{REST_URL}/entity/Note/{NOTE_ID}").mock(return_value=httpx.Response(200, json={"data": read or readback()}))
    return put, get


def write_calls() -> list[Any]:
    return [c for c in respx.calls if c.request.method in ("PUT", "POST", "DELETE")]
