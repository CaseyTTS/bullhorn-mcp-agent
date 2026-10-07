"""Token verifiers (Phase 5A, D-5A-3; AC-7 / SR-4; R-A2c crafted claims)."""

from __future__ import annotations

import base64
import json
import time

import anyio
import httpx
import jwt
import pytest
import respx

from bullhorn_mcp.identity.verifiers import IntrospectionTokenVerifier, JwksTokenVerifier

from ._identity_helpers import ALICE, ISSUER, JWKS, OTHER_ISSUER, RESOURCE, WRONG_KEY, make_jwt


def verifier(**kw):
    return JwksTokenVerifier(
        allowed_issuers=frozenset({ISSUER}), resource_server_url=RESOURCE, jwks=JWKS, keep_claims=("email", "bh_tenant"), **kw
    )


def verify(token, v=None):
    return anyio.run((v or verifier()).verify_token, token)


class TestJwks:
    def test_valid_token(self):
        at = verify(make_jwt(ALICE, email="a@example.test", bh_tenant="one", azp="host-client"))
        assert at is not None and at.subject == ALICE and at.client_id == "host-client"
        assert at.resource == RESOURCE and at.claims == {"iss": ISSUER, "email": "a@example.test", "bh_tenant": "one"}

    def test_crafted_claims_are_dropped(self):
        at = verify(make_jwt(ALICE, access_tier="bullhorn_user", bullhorn_linked=True, tier="service", roles=["admin"]))
        assert at is not None and set(at.claims) == {"iss"}

    def test_audience_list_containing_resource(self):
        assert verify(make_jwt(ALICE, aud=["https://other.example", RESOURCE])) is not None

    @pytest.mark.parametrize(
        "token_kwargs",
        [
            {"key": WRONG_KEY},  # wrong key
            {"exp_delta": -120},  # expired (beyond leeway)
            {"aud": "https://other.example/mcp"},  # wrong audience
            {"issuer": OTHER_ISSUER},  # issuer not allowed
            {"subject": None},  # no sub
            {"kid": "unknown-kid"},
        ],
    )
    def test_rejected(self, token_kwargs):
        assert verify(make_jwt(**{"subject": ALICE, **token_kwargs})) is None

    def test_tampered_payload_rejected(self):
        token = make_jwt(ALICE)
        head, payload, sig = token.split(".")
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        data["sub"] = "bob-sub"
        forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
        assert verify(f"{head}.{forged}.{sig}") is None

    def test_alg_none_and_hs256_rejected(self):
        none_token = jwt.encode({"iss": ISSUER, "aud": RESOURCE, "sub": ALICE, "exp": int(time.time()) + 60}, None,
                                algorithm="none", headers={"kid": "k1"})
        assert verify(none_token) is None
        hs = jwt.encode({"iss": ISSUER, "aud": RESOURCE, "sub": ALICE, "exp": int(time.time()) + 60}, "k" * 32,
                        algorithm="HS256", headers={"kid": "k1"})
        assert verify(hs) is None

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "x" * 20_000])
    def test_garbage_rejected(self, token):
        assert verify(token) is None

    def test_symmetric_algorithms_refused_at_construction(self):
        with pytest.raises(ValueError):
            verifier(algorithms=("HS256",))

    def test_repr_has_no_keys(self):
        assert repr(verifier()) == "JwksTokenVerifier(<config>)"
        assert repr(introspector()) == "IntrospectionTokenVerifier(<config>)"


INTROSPECT = "https://idp.example.test/introspect"


def introspector():
    return IntrospectionTokenVerifier(
        introspection_url=INTROSPECT, client_id="rs", client_secret=lambda: "rs-secret",
        allowed_issuers=frozenset({ISSUER}), resource_server_url=RESOURCE, keep_claims=("email",),
    )


class TestIntrospection:
    def _resp(self, **over):
        data = {"active": True, "sub": ALICE, "iss": ISSUER, "aud": RESOURCE, "exp": int(time.time()) + 300,
                "client_id": "host", "email": "a@example.test", "access_tier": "service"}
        data.update(over)
        return data

    @respx.mock
    def test_active(self):
        route = respx.post(INTROSPECT).mock(return_value=httpx.Response(200, json=self._resp()))
        at = anyio.run(introspector().verify_token, "opaque")
        assert at is not None and at.subject == ALICE and at.claims == {"iss": ISSUER, "email": "a@example.test"}
        assert route.calls[0].request.headers["authorization"].startswith("Basic ")

    @pytest.mark.parametrize(
        "over",
        [
            {"active": False},
            {"active": "true"},
            {"aud": "https://other.example"},
            {"iss": OTHER_ISSUER},
            {"sub": None},
            {"exp": int(time.time()) - 600},
        ],
    )
    @respx.mock
    def test_rejected(self, over):
        respx.post(INTROSPECT).mock(return_value=httpx.Response(200, json=self._resp(**over)))
        assert anyio.run(introspector().verify_token, "opaque") is None

    @respx.mock
    def test_errors_rejected(self):
        respx.post(INTROSPECT).mock(side_effect=httpx.ConnectError("down"))
        assert anyio.run(introspector().verify_token, "opaque") is None
        respx.post(INTROSPECT).mock(return_value=httpx.Response(500))
        assert anyio.run(introspector().verify_token, "opaque") is None
