"""5A triage B-3 (link_id execution identity; T-B3a..d, SR-31) and B-4 (per-tenant roles; T-B4a..c, SR-32)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bullhorn_mcp.identity import deploy, principal
from bullhorn_mcp.identity.principal import current_identity
from bullhorn_mcp.tenant import changes as tenant_changes
from bullhorn_mcp.tenant.store import SetupStore
from bullhorn_mcp.tools import session as session_tool
from bullhorn_mcp.tools import setup as setup_tools

from . import _identity_helpers
from ._identity_helpers import ADMIN, ALICE, AUTH, LOGIN, REST_1, REST_2, SVC, TK1, TK2, admin_config, caller, link, pk
from ._notes_helpers import NOTE_ID, mock_targets, mock_write, readback
from . import test_phase5a_writes
from .test_phase5a_writes import DEACTIVATE, _authorize, confirm, positive_verdict, preview

shared = _identity_helpers.shared  # fixture
tenant = test_phase5a_writes.tenant  # fixture


def journal(store):
    return [json.loads(line) for line in (store.root / "writes" / "journal.jsonl").read_text().splitlines()]


class TestB3:
    @respx.mock
    def test_t_b3a_relink_between_preview_and_confirm_denied(self, shared, tenant):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE, link_id="link-X")
        mock_targets()
        put, _ = mock_write()
        res = preview(ALICE)
        assert res["status"] == "previewed"
        with caller(ALICE):
            session_tool.bullhorn_session(action="logout")
        link(shared.store, TK1, ALICE, link_id="link-Y", token="bh-other-account")
        out = confirm(res, ALICE)
        assert out["status"] == "denied" and out["reason"] == "execution_identity_changed"
        assert put.call_count == 0

    @respx.mock
    def test_t_b3b_refresh_keeps_the_link(self, shared, tenant):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE, link_id="link-X", expires_in=600)
        mock_targets()
        put, _ = mock_write()
        res = preview(ALICE)
        rec = shared.store.get(TK1, pk(ALICE))
        rec.bh_expires_at = 0  # force a refresh before the confirm
        shared.store.put(rec)
        respx.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2"}))
        respx.post(f"{LOGIN}/rest-services/login").mock(return_value=httpx.Response(200, json={"BhRestToken": "bh-new", "restUrl": REST_1}))
        out = confirm(res, ALICE)
        assert out["status"] == "committed" and put.call_count == 1
        assert shared.store.get(TK1, pk(ALICE)).link_id == "link-X"

    @respx.mock
    def test_t_b3c_journal_labels_differ_per_link(self, shared, tenant):
        positive_verdict(tenant)
        mock_targets()
        mock_write()
        labels = []
        for link_id in ("link-X", "link-Y"):
            link(shared.store, TK1, ALICE, link_id=link_id)
            res = preview(ALICE)
            confirm(res, ALICE)
            labels.append({e["executing_bullhorn_identity"] for e in journal(tenant) if e.get("correlation_id") == res["correlation_id"]})
        assert len(labels[0]) == len(labels[1]) == 1 and labels[0] != labels[1]
        assert labels[0] == {principal.link_label(TK1, pk(ALICE), "link-X")}

    @respx.mock
    def test_t_b3d_authorization_bound_to_the_link(self, shared, tenant):
        link(shared.store, TK1, ADMIN, link_id="admin-link-X")
        assert _authorize(tenant)["status"] == "committed"
        mock_targets()
        mock_write(read=readback(person=100))
        link(shared.store, TK1, ADMIN, link_id="admin-link-Y")  # re-link
        assert preview(ADMIN)["status"] == "rejected_validation"
        link(shared.store, TK1, ADMIN, link_id="admin-link-X")
        assert preview(ADMIN)["status"] == "previewed"


@pytest.fixture
def two_tenants(tmp_path, monkeypatch):
    """Admin and service principal of tenant one only; tenant two has its own (empty) roles."""
    cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant", sso=True)
    cfg["tenants"][TK2]["roles"] = {"setup_admins": [], "write_approvers": []}
    cfg["tenants"][TK2]["service_principals"] = []
    monkeypatch.setenv(_identity_helpers.CLIENT_SECRET_ENV, _identity_helpers.CLIENT_SECRET)
    dep = deploy.parse_admin_config(cfg)
    from bullhorn_mcp.identity.session_store import MemorySessionStore

    store = MemorySessionStore()
    deploy.activate(dep, session_store=store)
    yield dep, store
    deploy.reset()


class TestB4:
    def test_t_b4a_admin_of_t1_is_not_admin_in_t2(self, two_tenants):
        _dep, store = two_tenants
        link(store, TK2, ADMIN, rest_url=REST_2)
        with caller(ADMIN, bh_tenant="two"):
            assert setup_tools.propose_mapping_changes(changes=[DEACTIVATE]).startswith("ERROR")
            res = json.loads(setup_tools.commit_mapping_changes(proposal_id="0" * 32, diff_hash="0" * 64, decision="approve"))
            assert res["status"] == "refused"
            for change in ({"op": "reset_note_write_verification"}, {"op": "enable_sso_login", "verification_id": "0" * 32},
                           {"op": "authorize_note_write_verification", "target_type": "candidate", "target_id": 1,
                            "action_type": "x"}):
                out = setup_tools.propose_mapping_changes(changes=[change])
                assert out.startswith("ERROR") and "setup admin" in out
            store.delete(TK2, pk(ADMIN))
            login = json.loads(session_tool.bullhorn_session(action="login"))
            assert login["reason"] == "unsupported_sso"  # no admin SSO-verification exception in T2
        with caller(ADMIN, bh_tenant="one"):
            assert json.loads(session_tool.bullhorn_session(action="login")).get("sso_verification") is True

    def test_t_b4b_top_level_roles_with_several_tenants_refused(self, tmp_path):
        cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant")
        cfg["roles"] = {"setup_admins": [pk(ADMIN)]}
        with pytest.raises(deploy.DeploymentError, match="exactly one tenant"):
            deploy.parse_admin_config(cfg)
        cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant")
        cfg["service_principals"] = [pk(SVC)]
        with pytest.raises(deploy.DeploymentError, match="exactly one tenant"):
            deploy.parse_admin_config(cfg)

    def test_single_tenant_top_level_and_tenant_roles_both_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["tenants"][TK1]["roles"] = {"setup_admins": [pk(ALICE)]}
        with pytest.raises(deploy.DeploymentError, match="not both"):
            deploy.parse_admin_config(cfg)

    def test_t_b4c_service_principal_is_per_tenant(self, two_tenants):
        with caller(SVC, bh_tenant="one"):
            assert current_identity().access_tier == "service"
        with caller(SVC, bh_tenant="two"):
            assert current_identity().access_tier == "workspace_only"

    def test_write_approvers_are_per_tenant(self, two_tenants):
        from bullhorn_mcp.writes.policy import check_scope

        _dep, store = two_tenants
        link(store, TK2, ALICE, rest_url=REST_2)
        with caller(ALICE, bh_tenant="two"):
            decision = check_scope("note.create", {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create"}, approving=True)
        assert decision.allowed is True  # T2 has no approver list (empty = not restricted)
        link(store, TK1, ALICE)
        with caller(ALICE, bh_tenant="one"):
            assert check_scope("note.create", {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create"}, approving=True).allowed

    def test_verification_generation_in_ledger_key(self, shared, tenant):  # L-6
        assert tenant_changes.note_write_generation(tenant) == 0
        with caller(ADMIN):
            p = json.loads(setup_tools.propose_mapping_changes(changes=[{"op": "reset_note_write_verification"}]))
            setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve")
        assert tenant_changes.note_write_generation(tenant) == 1

    @respx.mock
    def test_second_verification_after_reset_writes_again(self, shared, tenant):  # L-6, within the 24 h window
        mock_targets()
        put, _ = mock_write(read=readback(person=100))
        for _ in range(2):
            assert _authorize(tenant)["status"] == "committed"
            assert confirm(preview(ADMIN), ADMIN)["record_id"] == NOTE_ID
            with caller(ADMIN):
                p = json.loads(setup_tools.propose_mapping_changes(changes=[{"op": "reset_note_write_verification"}]))
                setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve")
        assert put.call_count == 2
        assert SetupStore(tenant.root).active_version() == 1
