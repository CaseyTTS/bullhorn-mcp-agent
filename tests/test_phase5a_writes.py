"""Phase 5A write ownership, attribution and the create_note verification procedure
(D-5A-11, D-5A-14, D-5A-15; A3-3, A3-4; AC-13, AC-14, AC-20; SR-10, SR-11, SR-17; SA3-3, SA3-4)."""

from __future__ import annotations

import json
import logging
import shutil

import pytest
import respx

from bullhorn_mcp.identity import deploy, principal
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.tenant import changes as tenant_changes
from bullhorn_mcp.tenant.profile_v2 import rest_url_fingerprint
from bullhorn_mcp.tenant.store import SetupStore
from bullhorn_mcp.tools import notes as notes_tools
from bullhorn_mcp.tools import session as session_tool
from bullhorn_mcp.tools import setup as setup_tools
from bullhorn_mcp.writes import pipeline

from ._identity_helpers import (
    ADMIN,
    ALICE,
    BOB,
    REST_1,
    SVC,
    TK1,
    TK2,
    activate_shared,
    caller,
    link,
    pk,
)
from . import _identity_helpers
from ._notes_helpers import NOTE_ACTION, NOTE_ACTION_OTHER, NOTE_ID, OK, SECRET_COMMENT, mock_targets, mock_write, readback, write_calls
from ._tenant_helpers import NOW, init_tenant, snapshot, write_snapshot

shared = _identity_helpers.shared  # fixture

DEACTIVATE = {"op": "deactivate_value_mapping", "key": "note.action.other"}
REST_SWIMLANE_MOVED = "https://rest77.bullhornstaffing.com/rest-services/abc123"  # same corpToken, new fingerprint


def _j(text):
    assert not text.startswith("ERROR"), text
    return json.loads(text)


@pytest.fixture
def tenant(shared, monkeypatch):
    """A valid tenant profile in tenant one's setup store, created by the setup admin."""
    monkeypatch.setenv("BULLHORN_ENABLED_WRITE_SCOPES", "note.create")
    store = SetupStore(shared.tenant().setup_store)
    write_snapshot(store, snapshot(rest_fp=OK.rest_url_fingerprint))
    link(shared.store, TK1, ADMIN)
    with caller(ADMIN):
        init_tenant(store, [NOTE_ACTION, NOTE_ACTION_OTHER])
    return store


def positive_verdict(store, rest_url=REST_1):
    tenant_changes.append_verification(store, tenant_changes.NOTE_WRITE_LOG, {
        "kind": "verdict", "positive": True, "rest_url_fingerprint": rest_url_fingerprint(rest_url), "note_id": 1,
    })


def preview(subject=ALICE, target_id=100, **kw):
    with caller(subject):
        return json.loads(notes_tools.create_note(target_type="candidate", target_id=target_id, action_type="Screen Call",
                                                  comments=SECRET_COMMENT, **kw))


def confirm(result, subject=ALICE, decision="approve"):
    with caller(subject):
        return json.loads(notes_tools.confirm_write(operation_id=result["operation_id"], preview_hash=result["preview_hash"],
                                                    decision=decision))


class TestOwnership:
    """D-5A-11 / AC-13 / SR-10: no cross-user confirm, no confirm under another tenant or execution identity."""

    @respx.mock
    def test_other_user_cannot_confirm_even_as_approver(self, shared, tenant):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE)
        link(shared.store, TK1, BOB)  # BOB is in roles.write_approvers
        mock_targets()
        put, _ = mock_write()
        res = preview(ALICE)
        assert res["status"] == "previewed"
        for decision in ("approve", "reject"):
            out = confirm(res, BOB, decision)
            assert out["status"] == "denied" and out["reason"] == "not_owner"
        assert not put.called
        assert confirm(res, ALICE)["status"] in ("committed", "partially_committed")
        assert put.call_count == 1

    @respx.mock
    def test_changed_execution_identity_denied(self, shared, tenant, monkeypatch):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE)
        mock_targets()
        put, _ = mock_write()
        res = preview(ALICE)
        # Same principal and tenant, but the session now resolves to another Bullhorn user.
        monkeypatch.setattr(principal, "HV_C5_VERIFIED", True)
        rec = shared.store.get(TK1, pk(ALICE))
        rec.bullhorn_user_ref = "corporate-user-42"
        shared.store.put(rec)
        assert confirm(res, ALICE)["reason"] == "execution_identity_changed"
        assert not put.called

    @respx.mock
    def test_changed_tenant_denied(self, tmp_path, monkeypatch):
        s = activate_shared(tmp_path, monkeypatch, tenants=2, tenant_claim="bh_tenant")
        try:
            monkeypatch.setenv("BULLHORN_ENABLED_WRITE_SCOPES", "note.create")
            store1 = SetupStore(s.tenant("one").setup_store)
            write_snapshot(store1, snapshot(rest_fp=OK.rest_url_fingerprint))
            link(s.store, TK1, ADMIN)
            with caller(ADMIN, bh_tenant="one"):
                init_tenant(store1, [NOTE_ACTION, NOTE_ACTION_OTHER])
            positive_verdict(store1)
            link(s.store, TK1, ALICE)
            link(s.store, TK2, ALICE, rest_url="https://rest42.bullhornstaffing.com/rest-services/zzz999")
            mock_targets()
            put, _ = mock_write()
            with caller(ALICE, bh_tenant="one"):
                res = json.loads(notes_tools.create_note(target_type="candidate", target_id=100, action_type="Screen Call",
                                                         comments=SECRET_COMMENT))
            assert res["status"] == "previewed"
            # Copy the pending file into tenant two's store: a confirm there must still be denied.
            src = store1.root / "writes" / "pending" / f"{res['operation_id']}.json"
            dst_dir = s.tenant("two").setup_store / "writes" / "pending"
            dst_dir.mkdir(parents=True)
            shutil.copy(src, dst_dir / src.name)
            with caller(ALICE, bh_tenant="two"):
                out = json.loads(notes_tools.confirm_write(operation_id=res["operation_id"], preview_hash=res["preview_hash"],
                                                           decision="approve"))
            assert out["status"] == "denied" and out["reason"] == "not_owner" and not put.called
        finally:
            deploy.reset()

    def test_commit_mapping_changes_by_other_principal_denied(self, shared, tenant):
        """A3-3 / SA3-3: even another setup admin cannot commit a proposal they did not create."""
        with caller(ADMIN):
            proposal = _j(setup_tools.propose_mapping_changes(changes=[DEACTIVATE]))
        other_admin = "admin-two"
        dep = deploy.current()
        from dataclasses import replace

        t1 = dep.tenants[TK1]
        t1 = replace(t1, roles=replace(t1.roles, setup_admins=t1.roles.setup_admins | {pk(other_admin)}))
        deploy.activate(replace(dep, tenants={TK1: t1}), session_store=shared.store)
        link(shared.store, TK1, other_admin)
        with caller(other_admin):
            out = _j(setup_tools.commit_mapping_changes(proposal_id=proposal["proposal_id"], diff_hash=proposal["diff_hash"],
                                                        decision="approve"))
        assert out["status"] == "denied" and out["reason"] == "not_owner"
        with caller(ADMIN):
            mine = _j(setup_tools.commit_mapping_changes(proposal_id=proposal["proposal_id"], diff_hash=proposal["diff_hash"],
                                                         decision="approve"))
        assert mine["status"] == "committed"

    def test_non_admin_cannot_propose_or_commit(self, shared, tenant):
        with caller(ADMIN):
            p = _j(setup_tools.propose_mapping_changes(changes=[DEACTIVATE]))
        link(shared.store, TK1, ALICE)
        with caller(ALICE):
            out = setup_tools.propose_mapping_changes(changes=[DEACTIVATE])  # B-4: shared mode needs a tenant admin
            assert out.startswith("ERROR") and "setup admin of this tenant" in out
            res = _j(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve"))
        assert res["status"] == "refused" and "setup_admins" in res["reason"]


class TestAttribution:
    """AC-14 / SR-11: the identity triple in audit, journal and ledger equals the requester's."""

    @respx.mock
    def test_identity_triple_recorded(self, shared, tenant, caplog):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE)
        mock_targets()
        mock_write()
        with caplog.at_level(logging.INFO, logger="bullhorn_mcp.audit"):
            res = preview(ALICE)
            out = confirm(res, ALICE)
        assert out["record_id"] == NOTE_ID
        expected = {
            "initiating_principal": pk(ALICE),
            "tenant_key": TK1,
            "executing_bullhorn_identity": principal.link_label(TK1, pk(ALICE), shared.store.get(TK1, pk(ALICE)).link_id),
        }
        lines = [json.loads(line) for line in (tenant.root / "writes" / "journal.jsonl").read_text().splitlines()]
        mine = [line for line in lines if line.get("correlation_id") == res["correlation_id"]]
        assert mine and all({k: line.get(k) for k in expected} == expected for line in mine)
        ledger_entries = [json.loads(p.read_text()) for p in (tenant.root / "writes" / "ledger").glob("*.g1.json")]
        assert any(e.get("identity") == expected for e in ledger_entries)
        pending = json.loads((tenant.root / "writes" / "pending" / f"{res['operation_id']}.json").read_text())
        assert {k: pending[k] for k in expected} == expected
        assert pk(ALICE) in caplog.text and TK1 in caplog.text
        assert SECRET_COMMENT not in caplog.text and SECRET_COMMENT not in (tenant.root / "writes" / "journal.jsonl").read_text()

    @respx.mock
    def test_derived_idempotency_key_is_per_principal(self, shared, tenant):
        """The same note by two users is two operations: B's write is never a 'duplicate' of A's."""
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE)
        link(shared.store, TK1, BOB)
        mock_targets()
        put, _ = mock_write()
        assert confirm(preview(ALICE), ALICE)["status"] == "committed"
        b = preview(BOB)
        assert b["status"] == "previewed" and b["preview"]["idempotency"]["verdict"] == "new"
        assert confirm(b, BOB)["status"] == "committed" and put.call_count == 2


class TestEnablementRule:
    @respx.mock
    def test_disabled_without_verdict(self, shared, tenant):
        link(shared.store, TK1, ALICE)
        mock_targets()
        res = preview(ALICE)
        assert res["status"] == "rejected_validation" and res["errors"][0]["code"] == "unsupported_association"
        with caller(ALICE):
            assert json.loads(session_tool.bullhorn_session())["create_note_enabled_for_tenant"] is False

    @respx.mock
    def test_positive_verdict_enables_target_without_commenting_person(self, shared, tenant):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE)
        mock_targets()
        res = preview(ALICE)
        assert res["status"] == "previewed"
        assert "commentingPerson" not in res["preview"]["request"]["body"]
        with caller(ALICE):
            assert json.loads(session_tool.bullhorn_session())["create_note_enabled_for_tenant"] is True
        # person-type associations stay behind the guard (only the target was verified)
        with caller(ALICE):
            assoc = json.loads(notes_tools.create_note(target_type="candidate", target_id=100, action_type="Screen Call",
                                                       comments="x", associations=[{"type": "candidate", "id": 101}]))
        assert assoc["status"] == "rejected_validation"

    @respx.mock
    def test_fingerprint_change_re_disables(self, shared, tenant):
        positive_verdict(tenant)
        link(shared.store, TK1, ALICE, rest_url=REST_SWIMLANE_MOVED)
        mock_targets()
        with caller(ALICE):
            out = json.loads(notes_tools.create_note(target_type="candidate", target_id=100, action_type="Screen Call", comments="x"))
            assert json.loads(session_tool.bullhorn_session())["create_note_enabled_for_tenant"] is False
        assert out["status"] != "previewed"

    @respx.mock
    def test_negative_verdict_keeps_disabled(self, shared, tenant):
        tenant_changes.append_verification(tenant, tenant_changes.NOTE_WRITE_LOG, {
            "kind": "verdict", "positive": False, "rest_url_fingerprint": rest_url_fingerprint(REST_1),
        })
        link(shared.store, TK1, ALICE)
        mock_targets()
        assert preview(ALICE)["status"] == "rejected_validation"

    def test_local_mode_production_guard_unchanged(self):
        assert pipeline.HV_B11_VERIFIED is False and pipeline.P4B8_CLOSED is True


def _authorize(store, subject=ADMIN, target_id=100, action="Screen Call", target_type="candidate"):
    with caller(subject):
        p = setup_tools.propose_mapping_changes(changes=[{"op": "authorize_note_write_verification", "target_type": target_type,
                                                          "target_id": target_id, "action_type": action}])
        if p.startswith("ERROR"):
            return p
        p = json.loads(p)
        return _j(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve"))


class TestVerificationProcedure:
    """D-5A-15 / A3-4 / AC-20 / SR-17 / SA3-4."""

    def test_admin_only(self, shared, tenant):
        link(shared.store, TK1, ALICE)
        out = _authorize(tenant, ALICE)
        assert out.startswith("ERROR") and "setup admin" in out

    def test_admin_without_linked_session_is_denied_by_the_tier_gate(self, shared, tenant):
        shared.store.delete(TK1, pk(ADMIN))
        assert _authorize(tenant, ADMIN).endswith("bullhorn_auth_required")

    def test_service_denied(self, shared, tenant):
        assert _authorize(tenant, SVC).endswith("service_identity_read_only")

    def test_local_mode_refused(self, tenant):
        deploy.reset()
        with pytest.raises(ProfileError, match="unsupported_in_local_mode"):
            tenant_changes.propose(tenant, [{"op": "reset_note_write_verification"}], now=NOW)

    def test_mixing_with_other_ops_refused(self, shared, tenant):
        with caller(ADMIN):
            out = setup_tools.propose_mapping_changes(changes=[
                {"op": "authorize_note_write_verification", "target_type": "candidate", "target_id": 100, "action_type": "Screen Call"},
                {"op": "set_setting", "name": "reporting_timezone", "value": "UTC"},
            ])
        assert out.startswith("ERROR") and "only operation" in out

    @pytest.mark.parametrize("bad", [{"target_type": "job"}, {"target_id": 0}, {"target_id": True}, {"action_type": " "}])
    def test_invalid_authorization_refused(self, shared, tenant, bad):
        args = {"target_type": "candidate", "target_id": 100, "action_type": "Screen Call", **bad}
        assert _authorize(tenant, **{"target_id": args["target_id"], "action": args["action_type"],
                                     "target_type": args["target_type"]}).startswith("ERROR")

    @respx.mock
    def test_full_procedure_single_write_and_verdict(self, shared, tenant):
        auth = _authorize(tenant)
        assert auth["status"] == "committed" and auth["action"] == "authorize_note_write_verification"
        mock_targets()
        put, _ = mock_write(read=readback(person=100))
        # exact match only: another target, an association or another principal stays behind the guard
        assert preview(ADMIN, target_id=101)["status"] == "rejected_validation"
        with caller(ADMIN):
            assoc = json.loads(notes_tools.create_note(target_type="candidate", target_id=100, action_type="Screen Call",
                                                       comments="x", associations=[{"type": "job", "id": 200}]))
        assert assoc["status"] == "rejected_validation"
        link(shared.store, TK1, ALICE)
        assert preview(ALICE)["status"] == "rejected_validation"
        # direct mode never runs the verification write
        res = preview(ADMIN)
        assert res["status"] == "previewed" and "commentingPerson" not in res["preview"]["request"]["body"]
        assert not write_calls()  # preview first: nothing written
        out = confirm(res, ADMIN)
        assert out["status"] == "committed" and put.call_count == 1
        # the verdict is recorded (no note text), the authorization is consumed, a second write is refused
        lines = tenant_changes.read_verifications(tenant, tenant_changes.NOTE_WRITE_LOG)
        verdict = lines[-1]
        assert verdict["kind"] == "verdict" and verdict["positive"] is True and verdict["note_id"] == NOTE_ID
        assert verdict["note_entity_present"] is True and verdict["commenting_person_id"] == 7
        assert verdict["principal"] == pk(ADMIN) and verdict["rest_url_fingerprint"] == rest_url_fingerprint(REST_1)
        assert SECRET_COMMENT not in (tenant.root / "verifications" / "note_write.jsonl").read_text()
        assert confirm(res, ADMIN)["status"] == "refused"
        assert put.call_count == 1
        again = _authorize(tenant)
        assert again.startswith("ERROR") and "verdict already exists" in again
        # enablement: ordinary users can now create notes on person targets of this tenant
        assert preview(ALICE)["status"] == "previewed"

    @respx.mock
    def test_negative_observation_keeps_disabled(self, shared, tenant):
        _authorize(tenant)
        mock_targets()
        mock_write(read=readback(person=100, entities=[]))
        res = preview(ADMIN)
        out = confirm(res, ADMIN)
        assert out["status"] == "partially_committed"
        assert tenant_changes.read_verifications(tenant, tenant_changes.NOTE_WRITE_LOG)[-1]["positive"] is False
        link(shared.store, TK1, ALICE)
        assert preview(ALICE)["status"] == "rejected_validation"

    @respx.mock
    def test_reset_allows_a_new_authorization(self, shared, tenant):
        positive_verdict(tenant)
        assert _authorize(tenant).startswith("ERROR")
        with caller(ADMIN):
            p = _j(setup_tools.propose_mapping_changes(changes=[{"op": "reset_note_write_verification"}]))
            assert _j(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"],
                                                         decision="approve"))["status"] == "committed"
        assert _authorize(tenant)["status"] == "committed"
        history = tenant.read_history()
        assert [h["action"] for h in history[-2:]] == ["reset_note_write_verification", "authorize_note_write_verification"]
        assert tenant.active_version() == 1  # no profile version was written

    def test_verification_proposal_hash_binding(self, shared, tenant):
        with caller(ADMIN):
            p = _j(setup_tools.propose_mapping_changes(changes=[{"op": "reset_note_write_verification"}]))
            assert _j(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash="0" * 64,
                                                         decision="approve"))["status"] == "refused"
        path = tenant.proposal_path(p["proposal_id"])
        data = json.loads(path.read_text())
        data["verification"] = {"target_type": "candidate", "target_id": 999, "action_type": "Screen Call"}
        data["action"] = "authorize_note_write_verification"
        path.write_text(json.dumps(data))
        with caller(ADMIN):
            out = _j(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve"))
        assert out["status"] == "refused" and "diff_hash" in out["reason"]
