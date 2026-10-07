"""Per-caller session resolution (D-5A-8; AC-9..AC-12, SR-6..SR-9, R-A2d)."""

from __future__ import annotations

import time
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.auth import AuthenticationError
from bullhorn_mcp.identity import sessions
from bullhorn_mcp.identity.principal import current_identity
from bullhorn_mcp.identity.sessions import BullhornSessionRequired, resolve_client

from ._identity_helpers import (
    ADMIN,
    ALICE,
    AUTH,
    BOB,
    LOGIN,
    REST_1,
    REST_2,
    SVC,
    TK1,
    TK2,
    caller,
    link,
    pk,
)
from . import _identity_helpers

shared = _identity_helpers.shared  # fixture
shared2 = _identity_helpers.shared2  # fixture

def mock_job(rest=REST_1, job_id=1):
    return respx.get(f"{rest}/entity/JobOrder/{job_id}").mock(return_value=httpx.Response(200, json={"data": {"id": job_id}}))


def mock_refresh(rest=REST_1, token="bh-refreshed", refresh="refresh-rotated"):
    t = respx.post(f"{AUTH}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc-new", "refresh_token": refresh, "expires_in": 600})
    )
    lg = respx.post(f"{LOGIN}/rest-services/login").mock(
        return_value=httpx.Response(200, json={"BhRestToken": token, "restUrl": rest})
    )
    return t, lg


def password_grant_routes():
    return respx.get(url__regex=r".*/oauth/authorize.*")


class TestResolveClient:
    @respx.mock
    def test_per_call_client_bound_to_callers_session(self, shared):
        link(shared.store, TK1, ALICE, token="bh-A")
        route = mock_job()
        with caller(ALICE):
            c1, c2 = resolve_client(), server.get_client()
            assert c1 is not c2  # never a process-global client
            c1.get("JobOrder", 1)
        assert route.calls[0].request.headers["BhRestToken"] == "bh-A"
        assert server._client is None

    def test_no_session_raises_session_required(self, shared):
        with caller(ALICE):
            with pytest.raises(BullhornSessionRequired) as info:
                resolve_client()
        assert isinstance(info.value, AuthenticationError)

    def test_no_identity(self, shared):
        with pytest.raises(BullhornSessionRequired, match="identity_required"):
            resolve_client()

    @respx.mock
    def test_same_user_two_tenants_independent(self, shared2):
        link(shared2.store, TK1, ALICE, token="bh-A-1", rest_url=REST_1)
        link(shared2.store, TK2, ALICE, token="bh-A-2", rest_url=REST_2)
        r1, r2 = mock_job(REST_1), mock_job(REST_2)
        with caller(ALICE, bh_tenant="one"):
            resolve_client().get("JobOrder", 1)
        with caller(ALICE, bh_tenant="two"):
            resolve_client().get("JobOrder", 1)
        assert r1.calls[0].request.headers["BhRestToken"] == "bh-A-1"
        assert r2.calls[0].request.headers["BhRestToken"] == "bh-A-2"


class TestRefresh:
    @respx.mock
    def test_expired_session_refreshes_and_rotates(self, shared):
        link(shared.store, TK1, ALICE, token="bh-old", expires_in=-10, refresh="refresh-old")
        token_route, _ = mock_refresh()
        route = mock_job()
        with caller(ALICE):
            resolve_client().get("JobOrder", 1)
        assert route.calls[0].request.headers["BhRestToken"] == "bh-refreshed"
        assert "refresh-old" in str(token_route.calls[0].request.url)
        stored = shared.store.get(TK1, pk(ALICE))
        assert stored.refresh_token == "refresh-rotated" and stored.bh_rest_token == "bh-refreshed"

    @respx.mock
    def test_failed_refresh_deletes_and_never_falls_back(self, shared):
        link(shared.store, TK1, ALICE, expires_in=-10)
        respx.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        grant = password_grant_routes()
        with caller(ALICE), patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")):
            with pytest.raises(BullhornSessionRequired):
                resolve_client()
            assert current_identity().access_tier == "workspace_only"  # R-A2d: tier flips
        assert shared.store.get(TK1, pk(ALICE)) is None
        assert not grant.called  # AC-11: no password-grant request

    @respx.mock
    def test_refresh_returning_another_tenant_deletes(self, shared):
        link(shared.store, TK1, ALICE, expires_in=-10)
        mock_refresh(rest=REST_2)
        with caller(ALICE):
            with pytest.raises(BullhornSessionRequired):
                resolve_client()
        assert shared.store.get(TK1, pk(ALICE)) is None

    def test_missing_refresh_token_deletes(self, shared):
        link(shared.store, TK1, ALICE, expires_in=-10, refresh=None)
        with caller(ALICE):
            with pytest.raises(BullhornSessionRequired):
                resolve_client()
        assert shared.store.get(TK1, pk(ALICE)) is None

    @respx.mock
    def test_401_forces_refresh_and_retry(self, shared):
        link(shared.store, TK1, ALICE, token="bh-stale")
        mock_refresh(token="bh-fresh")
        seen = []

        def handler(request):
            seen.append(request.headers["BhRestToken"])
            return httpx.Response(401) if len(seen) == 1 else httpx.Response(200, json={"data": {"id": 1}})

        respx.get(f"{REST_1}/entity/JobOrder/1").mock(side_effect=handler)
        with caller(ALICE):
            assert resolve_client().get("JobOrder", 1) == {"id": 1}
        assert seen == ["bh-stale", "bh-fresh"]

    @respx.mock
    def test_relinking_restores_tier(self, shared):
        with caller(ALICE):
            assert current_identity().access_tier == "workspace_only"
            link(shared.store, TK1, ALICE)
            assert current_identity().access_tier == "bullhorn_user"


class TestLogout:
    def test_logout_scope(self, shared2):
        link(shared2.store, TK1, ALICE)
        link(shared2.store, TK2, ALICE, rest_url=REST_2)
        link(shared2.store, TK1, BOB)
        with caller(ALICE, bh_tenant="one"):
            sessions.logout(current_identity())
        assert shared2.store.get(TK1, pk(ALICE)) is None
        assert shared2.store.get(TK2, pk(ALICE)) is not None
        assert shared2.store.get(TK1, pk(BOB)) is not None


class TestServiceIdentity:
    @respx.mock
    def test_service_principal_uses_service_identity(self, tmp_path, monkeypatch):
        from bullhorn_mcp.identity import deploy

        from ._identity_helpers import activate_shared

        activate_shared(tmp_path, monkeypatch, service=True)
        try:
            respx.get(url__regex=r"https://auth\.bullhornstaffing\.com/oauth/authorize.*").mock(
                return_value=httpx.Response(302, headers={"location": "https://mcp.example.test/cb?code=svc-code"})
            )
            respx.post(f"{AUTH}/oauth/token").mock(
                return_value=httpx.Response(200, json={"access_token": "svc-acc", "refresh_token": "svc-ref", "expires_in": 600})
            )
            respx.get(f"{LOGIN}/rest-services/login").mock(
                return_value=httpx.Response(200, json={"BhRestToken": "bh-service", "restUrl": REST_1})
            )
            route = mock_job()
            with caller(SVC):
                resolve_client().get("JobOrder", 1)
            assert route.calls[0].request.headers["BhRestToken"] == "bh-service"
        finally:
            deploy.reset()
            sessions.reset_caches()

    def test_service_without_credentials_is_session_required(self, shared):
        with caller(SVC):
            with pytest.raises(BullhornSessionRequired, match="service_identity_not_configured"):
                resolve_client()

    def test_workspace_only_and_users_never_resolve_service(self, shared):
        link(shared.store, TK1, ADMIN)
        with patch.object(sessions, "service_client", side_effect=AssertionError("service resolved")) as svc:
            for who in (ALICE, BOB):
                with caller(who):
                    with pytest.raises(BullhornSessionRequired):
                        resolve_client()
            with caller(ADMIN):
                resolve_client()
        assert not svc.called


def test_session_state_labels(shared):
    with caller(ALICE):
        assert sessions.session_state(current_identity()) == ("none", None)
    link(shared.store, TK1, ALICE)
    with caller(ALICE):
        state, expires = sessions.session_state(current_identity())
    assert state == "active" and expires.endswith("Z")
    assert time.time() < sessions._now() + 5
