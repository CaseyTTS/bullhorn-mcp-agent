"""Phase 6 M2: the per-tenant Tier 2 analytics grant (D-6-1..D-6-4) and the P6-4 concurrency cap."""

from __future__ import annotations

import copy
import inspect
import threading
from unittest.mock import Mock

import pytest
import respx

from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.principal import IdentityContext
from bullhorn_mcp.identity.roles import Roles
from bullhorn_mcp.metrics import catalog as C
from bullhorn_mcp.metrics import tier2 as T2
from bullhorn_mcp.metrics import tier2_policy as P
from bullhorn_mcp.tools import metrics as metric_tools

from ._identity_helpers import ADMIN, ALICE, BOB, ISSUER, REST_1, TK1, TK2, admin_config, caller, link, pk
from ._phase5c_helpers import FULL_CONFIG
from .test_phase5c_security_tools import install_shared
from .test_phase6_m1_metrics import dataset
from .test_phase6_m1_security import ALICE_TOKEN, SVC_TOKEN, _json, _metrics, _no_service, _service

DENIED = {"status": "denied", "tier": "workspace_only", "error": "analytics_permission_required"}
BASE_ROLES = {"setup_admins": [pk(ADMIN)], "write_approvers": [pk(ADMIN), pk(BOB), pk(ALICE)]}


def _install(tmp_path, monkeypatch, viewers):
    roles = {**BASE_ROLES, "analytics_viewers": viewers} if viewers is not None else BASE_ROLES
    return install_shared(tmp_path, monkeypatch, FULL_CONFIG, service=True, roles=roles)


@pytest.fixture
def ungranted(tmp_path, monkeypatch):
    s = _install(tmp_path, monkeypatch, None)
    yield s
    deploy.reset()
    sessions.reset_caches()


@pytest.fixture
def bob_granted(tmp_path, monkeypatch):
    s = _install(tmp_path, monkeypatch, [pk(BOB)])
    yield s
    deploy.reset()
    sessions.reset_caches()


def _ident(tenant, principal="p-ws", tenant_key=None):
    return IdentityContext(initiating_principal=principal, principal_display=principal,
                           tenant_key=tenant_key or tenant.tenant_key, executing_bullhorn_identity=None,
                           mode="user", access_tier="workspace_only", tenant=tenant)


def _tenant(key="tenant-1", viewers=frozenset({"p-ws"})):
    tenant = Mock()
    tenant.tenant_key = key
    tenant.roles = Roles(analytics_viewers=frozenset(viewers))
    return tenant


# ---------------------------------------------------------------------- #
# Admin config: the grant is a per-tenant role (additive)
# ---------------------------------------------------------------------- #


class TestConfig:
    def test_single_tenant_top_level_form(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["roles"] = {**BASE_ROLES, "analytics_viewers": [pk(BOB), {"issuer": ISSUER, "subject": ALICE}]}
        dep = deploy.parse_admin_config(cfg)
        assert dep.tenants[TK1].roles.analytics_viewers == frozenset({pk(BOB), pk(ALICE)})

    def test_default_is_empty(self, tmp_path):
        dep = deploy.parse_admin_config(admin_config(tmp_path))
        assert dep.tenants[TK1].roles.analytics_viewers == frozenset()

    def test_per_tenant_form_and_isolation(self, tmp_path):
        cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant")
        cfg["tenants"][TK1]["roles"] = {**copy.deepcopy(BASE_ROLES), "analytics_viewers": [pk(BOB)]}
        dep = deploy.parse_admin_config(cfg)
        assert dep.tenants[TK1].roles.is_analytics_viewer(pk(BOB))
        assert not dep.tenants[TK2].roles.is_analytics_viewer(pk(BOB))

    @pytest.mark.parametrize("bad", ["not-a-list", [123], [{"issuer": ISSUER}]])
    def test_invalid_entries_refused(self, tmp_path, bad):
        cfg = admin_config(tmp_path)
        cfg["roles"] = {**BASE_ROLES, "analytics_viewers": bad}
        with pytest.raises(deploy.DeploymentError, match="roles.analytics_viewers"):
            deploy.parse_admin_config(cfg)

    def test_unknown_role_key_still_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["roles"] = {**BASE_ROLES, "analytics_viewer": [pk(BOB)]}
        with pytest.raises(deploy.DeploymentError):
            deploy.parse_admin_config(cfg)


# ---------------------------------------------------------------------- #
# Enforcement through the tool (D-6-2, D-6-3, D-6-4)
# ---------------------------------------------------------------------- #


class TestGrantEnforcement:
    @respx.mock
    def test_ungranted_workspace_only_denied_zero_calls(self, ungranted):
        with _no_service(), caller(BOB):
            text = _metrics()
        assert _json(text) == DENIED
        assert not respx.calls
        for leak in (BOB, pk(BOB), f"{BOB}@example.test", ISSUER, TK1):
            assert leak not in text

    @respx.mock
    def test_linked_user_who_logs_out_is_denied(self, bob_granted):
        link(bob_granted.store, TK1, ALICE, token=ALICE_TOKEN)
        fake = dataset()
        fake.mock(REST_1)
        with _no_service(), caller(ALICE):
            out = _json(_metrics())
        assert out["tier"] == "bullhorn_user" and set(fake.tokens) == {ALICE_TOKEN}  # Tier 1 unchanged, no grant needed
        n = len(respx.calls)
        assert bob_granted.store.delete(TK1, pk(ALICE))  # logged out of Bullhorn
        with _no_service(), caller(ALICE):
            text = _metrics()
        assert _json(text) == DENIED and len(respx.calls) == n
        assert ALICE not in text and pk(ALICE) not in text

    @respx.mock
    def test_granted_unlinked_principal_gets_m1_tier2_output(self, bob_granted):
        fake = dataset()
        fake.mock(REST_1)
        with _service(), caller(BOB):
            out = _json(_metrics())
        assert out["status"] not in ("denied", "error", "rate_limited", "unavailable")
        assert out["tier"] == "workspace_only" and "provenance" not in out
        P.validate_output(out, C.load())
        assert set(fake.tokens) == {SVC_TOKEN}

    @respx.mock
    def test_token_claims_cannot_self_grant(self, ungranted):
        with _no_service(), caller(BOB, analytics_viewers=[pk(BOB)], analytics_viewer=True, roles=["analytics_viewers"]):
            assert _json(_metrics()) == DENIED
        assert not respx.calls

    def test_no_grant_tool_parameter(self):
        params = set(inspect.signature(metric_tools.get_recruiting_metrics).parameters)
        assert params == {"metrics", "date_from", "date_to", "period"}

    def test_default_period_unchanged(self):
        assert inspect.signature(metric_tools.get_recruiting_metrics).parameters["period"].default == "month"


# ---------------------------------------------------------------------- #
# tier2.run unit level: tenant scoping, zero calls
# ---------------------------------------------------------------------- #


class TestTier2Unit:
    def _guard(self, monkeypatch):
        svc = Mock(side_effect=AssertionError("service identity"))
        monkeypatch.setattr(T2.sessions, "service_client", svc)
        run = Mock(side_effect=AssertionError("computation"))
        monkeypatch.setattr(T2, "_run", run)
        return svc, run

    def test_grant_in_t1_has_no_effect_in_t2(self, tmp_path, monkeypatch):
        cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant")
        cfg["tenants"][TK1]["roles"] = {**copy.deepcopy(BASE_ROLES), "analytics_viewers": [pk(BOB)]}
        dep = deploy.parse_admin_config(cfg)
        svc, run = self._guard(monkeypatch)
        assert T2.run(_ident(dep.tenants[TK2], pk(BOB)), {}, {}, None) == DENIED  # type: ignore[arg-type]
        # a mismatched tenant/tenant_key pair never borrows the other tenant's grant
        assert T2.run(_ident(dep.tenants[TK1], pk(BOB), tenant_key=TK2), {}, {}, None) == DENIED  # type: ignore[arg-type]
        assert not svc.called and not run.called

    @pytest.mark.parametrize("tenant", [None, "mock-roles"])
    def test_missing_or_untrusted_roles_denied(self, monkeypatch, tenant):
        svc, run = self._guard(monkeypatch)
        if tenant == "mock-roles":
            tenant = Mock()
            tenant.tenant_key = "tenant-1"  # roles is a Mock, not admin-config Roles
        ident = IdentityContext(initiating_principal="p-ws", principal_display="p-ws", tenant_key="tenant-1",
                                executing_bullhorn_identity=None, mode="user", access_tier="workspace_only", tenant=tenant)
        assert T2.run(ident, {}, {}, None) == DENIED  # type: ignore[arg-type]
        assert not svc.called and not run.called

    def test_tier1_tier_mismatch_unchanged(self, monkeypatch):
        svc, run = self._guard(monkeypatch)
        ident = IdentityContext(initiating_principal="p-ws", principal_display="p-ws", tenant_key="tenant-1",
                                executing_bullhorn_identity="x", mode="user", access_tier="bullhorn_user", tenant=_tenant())
        assert T2.run(ident, {}, {}, None) == {"status": "unavailable", "tier": "workspace_only", "error": "internal_error"}  # type: ignore[arg-type]
        assert not run.called


# ---------------------------------------------------------------------- #
# P6-4: per-tenant concurrency cap
# ---------------------------------------------------------------------- #


class TestConcurrencyCap:
    def test_third_concurrent_call_rate_limited_then_released(self, monkeypatch):
        tenant = _tenant("tenant-cap")
        started = threading.Barrier(3)
        release = threading.Event()
        calls: list[int] = []

        def slow(*args, **kwargs):
            calls.append(1)
            started.wait(timeout=5)
            release.wait(timeout=5)
            return {"status": "ok"}

        svc = Mock(side_effect=AssertionError("service identity"))
        monkeypatch.setattr(T2.sessions, "service_client", svc)
        monkeypatch.setattr(T2, "_run", slow)
        results: list[dict] = []
        threads = [threading.Thread(target=lambda: results.append(T2.run(_ident(tenant), {}, {}, None))) for _ in range(2)]  # type: ignore[arg-type]
        for t in threads:
            t.start()
        started.wait(timeout=5)
        try:
            assert T2.run(_ident(tenant), {}, {}, None) == {"status": "rate_limited", "tier": "workspace_only", "error": "rate_limited"}  # type: ignore[arg-type]
            assert len(calls) == 2 and not svc.called
            # another tenant has its own slots
            other = _tenant("tenant-other")
            monkeypatch.setattr(T2, "_run", Mock(return_value={"status": "ok"}))
            assert T2.run(_ident(other), {}, {}, None) == {"status": "ok"}  # type: ignore[arg-type]
        finally:
            release.set()
            for t in threads:
                t.join(timeout=5)
        assert results == [{"status": "ok"}, {"status": "ok"}]
        monkeypatch.setattr(T2, "_run", Mock(return_value={"status": "ok"}))
        for _ in range(3):
            assert T2.run(_ident(tenant), {}, {}, None) == {"status": "ok"}  # type: ignore[arg-type]

    def test_slots_released_after_exception(self, monkeypatch):
        tenant = _tenant("tenant-exc")
        monkeypatch.setattr(T2, "_run", Mock(side_effect=RuntimeError("boom")))
        for _ in range(4):
            assert T2.run(_ident(tenant), {}, {}, None) == {"status": "error", "tier": "workspace_only", "error": "internal_error"}  # type: ignore[arg-type]
        monkeypatch.setattr(T2, "_run", Mock(return_value={"status": "ok"}))
        assert T2.run(_ident(tenant), {}, {}, None) == {"status": "ok"}  # type: ignore[arg-type]

    def test_denied_callers_do_not_consume_slots(self, monkeypatch):
        tenant = _tenant("tenant-deny", viewers=frozenset())
        for _ in range(4):
            assert T2.run(_ident(tenant), {}, {}, None) == DENIED  # type: ignore[arg-type]
        assert T2._slots.get("tenant-deny") is None
