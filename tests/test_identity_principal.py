"""Caller principal, tenant selection and access tier (D-5A-2, A2-1, A3-1; SR-3, SR-27, R-A2c, R-A3a)."""

from __future__ import annotations

import pytest

from bullhorn_mcp.identity import deploy
from bullhorn_mcp.identity.principal import (
    IdentityRequired,
    current_identity,
    local_identity,
    principal_from_token,
    link_label,
)
from bullhorn_mcp.identity.roles import principal_key_for

from ._identity_helpers import (
    ALICE,
    BOB,
    ISSUER,
    OTHER_ISSUER,
    SVC,
    TK1,
    TK2,
    access_token,
    caller,
    link,
    no_caller,
    pk,
)
from . import _identity_helpers

shared = _identity_helpers.shared  # fixture
shared2 = _identity_helpers.shared2  # fixture

class TestPrincipalKey:
    def test_issuer_and_subject_both_matter(self):
        assert principal_key_for(ISSUER, ALICE) != principal_key_for(OTHER_ISSUER, ALICE)
        assert principal_key_for(ISSUER, ALICE) != principal_key_for(ISSUER, BOB)

    def test_unambiguous_encoding(self):
        # Concatenation collisions are impossible: ("a|b", "c") vs ("a", "b|c").
        assert principal_key_for("https://i/a|b", "c") != principal_key_for("https://i/a", "b|c")
        assert principal_key_for("ab", "c") != principal_key_for("a", "bc")

    def test_client_id_and_display_never_identify(self, shared):
        a1 = principal_from_token(access_token(ALICE, email="x@example.test"), shared.deployment)
        tok = access_token(ALICE, email="other@example.test")
        tok.client_id = "a-different-client"
        a2 = principal_from_token(tok, shared.deployment)
        assert a1.key == a2.key == pk(ALICE)
        b = principal_from_token(access_token(BOB, email="x@example.test"), shared.deployment)
        assert b.key != a1.key


class TestFailClosed:
    @pytest.mark.parametrize(
        "token",
        [
            None,
            access_token(None),
            access_token(""),
            access_token(ALICE, issuer=None),
            access_token(ALICE, issuer=OTHER_ISSUER),
        ],
    )
    def test_identity_required(self, shared, token):
        with pytest.raises(IdentityRequired):
            principal_from_token(token, shared.deployment)

    def test_no_request_token_in_shared_mode(self, shared):
        with no_caller():
            with pytest.raises(IdentityRequired):
                current_identity()


class TestTenantSelection:
    """A3-1: single tenant without claim; claim -> exactly one alias; otherwise identity_required."""

    def test_single_tenant_without_claim(self, shared):
        assert principal_from_token(access_token(ALICE), shared.deployment).tenant.tenant_key == TK1

    def test_single_tenant_ignores_a_claim_value(self, shared):
        assert principal_from_token(access_token(ALICE, bh_tenant="two"), shared.deployment).tenant.tenant_key == TK1

    def test_claim_selects_tenant(self, shared2):
        dep = shared2.deployment
        assert principal_from_token(access_token(ALICE, bh_tenant="one"), dep).tenant.tenant_key == TK1
        assert principal_from_token(access_token(ALICE, bh_tenant="two"), dep).tenant.tenant_key == TK2

    @pytest.mark.parametrize("claims", [{}, {"bh_tenant": 2}, {"bh_tenant": ["one"]}, {"bh_tenant": "three"}, {"bh_tenant": "ONE"}])
    def test_bad_claim_is_identity_required(self, shared2, claims):
        with pytest.raises(IdentityRequired):
            principal_from_token(access_token(ALICE, **claims), shared2.deployment)

    def test_multiple_tenants_without_claim_fail_closed_even_if_activated(self, shared2):
        dep = shared2.deployment
        from dataclasses import replace

        broken = replace(dep, auth=replace(dep.auth, tenant_claim=None))
        with pytest.raises(IdentityRequired):
            principal_from_token(access_token(ALICE, bh_tenant="one"), broken)


class TestTier:
    def test_local(self):
        deploy.reset()
        ident = current_identity()
        assert ident.mode == "local" and ident.access_tier == "local" and ident.initiating_principal.startswith("local:")
        assert local_identity().tenant_key is None

    def test_workspace_only_without_session(self, shared):
        with caller(ALICE):
            ident = current_identity()
        assert ident.access_tier == "workspace_only" and ident.executing_bullhorn_identity is None
        assert ident.initiating_principal == pk(ALICE) and ident.tenant_key == TK1

    def test_bullhorn_user_with_session(self, shared):
        rec = link(shared.store, TK1, ALICE)
        with caller(ALICE):
            ident = current_identity()
        assert ident.access_tier == "bullhorn_user"
        # HV-C5 unresolved: never a guessed id; B-3: the label is per completed link
        assert ident.executing_bullhorn_identity == link_label(TK1, pk(ALICE), rec.link_id)

    def test_service(self, shared):
        with caller(SVC):
            ident = current_identity()
        assert ident.access_tier == "service" and ident.mode == "service" and ident.service_identity

    def test_crafted_claims_never_raise_the_tier(self, shared):
        with caller(ALICE, access_tier="bullhorn_user", bullhorn_linked=True, tier="service"):
            assert current_identity().access_tier == "workspace_only"

    def test_session_in_other_tenant_does_not_count(self, shared2):
        link(shared2.store, TK2, ALICE, rest_url="https://rest42.bullhornstaffing.com/rest-services/zzz999")
        with caller(ALICE, bh_tenant="one"):
            assert current_identity().access_tier == "workspace_only"
        with caller(ALICE, bh_tenant="two"):
            assert current_identity().access_tier == "bullhorn_user"

    def test_other_principal_session_does_not_count(self, shared):
        link(shared.store, TK1, BOB)
        with caller(ALICE):
            assert current_identity().access_tier == "workspace_only"
        with caller(BOB, issuer=OTHER_ISSUER):
            with pytest.raises(IdentityRequired):
                current_identity()

    def test_display_is_sanitised(self, shared):
        with caller(ALICE, email="<script>alert(1)</script>\x00‮"):
            display = current_identity().principal_display
        assert "<" not in display and "\x00" not in display and "‮" not in display
