"""Shared-mode HTTP harness: the real SDK stack (streamable-HTTP, stateless, bearer auth).

HV-M1 (401 before any tool), HV-M6 (per-request token under concurrency, mandatory),
HV-M7 (Host allowlist), AC-6, AC-8 / SR-5 (N >= 20 interleaved users), R-A2e, R-A3a / SR-27.
"""

from __future__ import annotations

import asyncio
import json
import random

import anyio
import httpx
import pytest
import respx

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from bullhorn_mcp import server
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.verifiers import JwksTokenVerifier

from ._identity_helpers import (
    ALICE,
    BASE,
    BOB,
    ISSUER,
    JWKS,
    RESOURCE,
    REST_1,
    REST_2,
    TK1,
    TK2,
    WRONG_KEY,
    activate_shared,
    link,
    make_jwt,
)
from . import _identity_helpers

shared = _identity_helpers.shared  # fixture

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def verifier():
    return JwksTokenVerifier(allowed_issuers=frozenset({ISSUER}), resource_server_url=RESOURCE, jwks=JWKS,
                             keep_claims=("email", "name", "bh_tenant"))


def build_app(dep, *, share_tools=True, **extra):
    app = FastMCP("phase5a-harness", json_response=True, **deploy.shared_fastmcp_kwargs(dep, verifier()), **extra)
    if share_tools:
        app._tool_manager = server.mcp._tool_manager  # the real 20 tools, behind the real dispatch gate
    return app


def call_body(name, arguments, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


async def call(client, token, name, arguments, rid=1):
    headers = dict(HEADERS)
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return await client.post("/mcp", json=call_body(name, arguments, rid), headers=headers)


def text_of(response):
    data = response.json()
    return "".join(c.get("text", "") for c in data["result"]["content"])


async def with_app(app, fn, base=BASE):
    starlette_app = app.streamable_http_app()
    async with app.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=starlette_app), base_url=base) as client:
            return await fn(client)


class TestBearerAuth:
    """HV-M1 / AC-6 / SR-4: 401 before any tool runs."""

    @pytest.mark.parametrize(
        "token",
        [
            None,
            "not-a-jwt",
            make_jwt(ALICE, key=WRONG_KEY),
            make_jwt(ALICE, exp_delta=-600),
            make_jwt(ALICE, aud="https://other.example/mcp"),
            make_jwt(ALICE, issuer="https://evil.example"),
            make_jwt(ALICE, bh_tenant="one", key=WRONG_KEY),  # R-A3a: a forged alias in an unverified token
        ],
        ids=["missing", "garbage", "wrong-key", "expired", "wrong-aud", "wrong-iss", "forged-alias"],
    )
    def test_rejected_before_any_tool(self, shared, token):
        link(shared.store, TK1, ALICE)

        async def fn(client):
            with respx.mock(assert_all_mocked=False) as router:
                r = await call(client, token, "get_job", {"job_id": 1})
            assert r.status_code == 401 and not router.calls

        anyio.run(with_app, build_app(shared.deployment), fn)

    def test_wrong_host_header_refused(self, shared):  # HV-M7
        async def fn(client):
            r = await call(client, make_jwt(ALICE), "bullhorn_session", {})
            assert r.status_code == 421

        anyio.run(with_app, build_app(shared.deployment), fn, "https://evil.example")

    def test_verified_token_without_subject_is_identity_required(self, shared):
        class SubjectlessVerifier:
            async def verify_token(self, token):
                at = await verifier().verify_token(token)
                return at.model_copy(update={"subject": None}) if at else None

        kwargs = deploy.shared_fastmcp_kwargs(shared.deployment, SubjectlessVerifier())
        app = FastMCP("subjectless", json_response=True, **kwargs)
        app._tool_manager = server.mcp._tool_manager
        link(shared.store, TK1, ALICE)

        async def fn(client):
            with respx.mock(assert_all_mocked=False) as router:
                for name, args in (("get_job", {"job_id": 1}), ("bullhorn_session", {}), ("setup_status", {})):
                    r = await call(client, make_jwt(ALICE), name, args)
                    assert r.status_code == 200 and text_of(r).endswith("identity_required")
            assert not router.calls

        anyio.run(with_app, app, fn)


class TestHvM6:
    """HV-M6 (mandatory): inside a handler, get_access_token() is *that* request's token under concurrency."""

    def test_contextvar_is_per_request_under_interleaving(self, shared):
        app = build_app(shared.deployment, share_tools=False)

        in_flight = {"now": 0, "peak": 0}

        @app.tool()
        async def whoami() -> str:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            first = get_access_token()
            await anyio.sleep(0.01 + random.random() / 50)  # force interleaving with other requests
            second = get_access_token()
            in_flight["now"] -= 1
            return f"{first.subject if first else None}|{second.subject if second else None}"

        subjects = [f"user-{i % 7}" for i in range(40)]

        async def fn(client):
            responses = await asyncio.gather(*(call(client, make_jwt(s), "whoami", {}, i) for i, s in enumerate(subjects)))
            for s, r in zip(subjects, responses):
                assert r.status_code == 200 and text_of(r) == f"{s}|{s}"

        anyio.run(with_app, app, fn)
        assert in_flight["peak"] > 1  # the handlers really did overlap


class TestConcurrentIsolation:
    """AC-8 / SR-5 / R-A2e: interleaved users never cross sessions, and a process-global client is never used."""

    def test_two_users_same_tenant(self, shared):
        link(shared.store, TK1, ALICE, token="bh-ALICE")
        link(shared.store, TK1, BOB, token="bh-BOB")
        owners = {}
        seen = []

        def handler(request):
            job_id = int(str(request.url.path).rsplit("/", 1)[1])
            seen.append((job_id, request.headers["BhRestToken"]))
            return httpx.Response(200, json={"data": {"id": job_id, "owner_token_seen": request.headers["BhRestToken"]}})

        requests = []
        for i in range(30):
            who = ALICE if i % 2 == 0 else BOB
            job_id = (1000 if who == ALICE else 2000) + i
            owners[job_id] = who
            requests.append((who, job_id))
        random.shuffle(requests)

        async def fn(client):
            with respx.mock(assert_all_mocked=True) as router:
                router.get(url__regex=rf"{REST_1}/entity/JobOrder/\d+.*").mock(side_effect=handler)
                responses = await asyncio.gather(
                    *(call(client, make_jwt(who), "get_job", {"job_id": jid}, n) for n, (who, jid) in enumerate(requests))
                )
            for (who, jid), r in zip(requests, responses):
                body = json.loads(text_of(r))
                assert body["id"] == jid and body["owner_token_seen"] == f"bh-{'ALICE' if who == ALICE else 'BOB'}"

        anyio.run(with_app, build_app(shared.deployment), fn)
        assert len(seen) == 30
        for job_id, token in seen:
            assert token == ("bh-ALICE" if owners[job_id] == ALICE else "bh-BOB")
        assert server._client is None

    def test_linked_and_workspace_only_interleaved(self, shared):
        link(shared.store, TK1, ALICE, token="bh-ALICE")  # BOB has no session: workspace_only

        def handler(request):
            assert request.headers["BhRestToken"] == "bh-ALICE"
            return httpx.Response(200, json={"data": {"id": 1, "secret_field": "alice-record"}})

        plan = [ALICE if i % 2 else BOB for i in range(24)]

        async def fn(client):
            with respx.mock(assert_all_mocked=True) as router:
                route = router.get(url__regex=rf"{REST_1}/entity/JobOrder/1.*").mock(side_effect=handler)
                responses = await asyncio.gather(*(call(client, make_jwt(w), "get_job", {"job_id": 1}, n) for n, w in enumerate(plan)))
            for who, r in zip(plan, responses):
                text = text_of(r)
                if who == BOB:
                    assert text == "ERROR: permission denied for get_job: bullhorn_auth_required"
                    assert "alice-record" not in text
                else:
                    assert "alice-record" in text
            assert route.call_count == plan.count(ALICE)

        anyio.run(with_app, build_app(shared.deployment), fn)

    def test_same_user_two_tenants_via_claim(self, tmp_path, monkeypatch):
        s = activate_shared(tmp_path, monkeypatch, tenants=2, tenant_claim="bh_tenant")
        try:
            link(s.store, TK1, ALICE, token="bh-T1", rest_url=REST_1)
            link(s.store, TK2, ALICE, token="bh-T2", rest_url=REST_2)

            async def fn(client):
                with respx.mock(assert_all_mocked=True) as router:
                    r1 = router.get(url__regex=rf"{REST_1}/entity/JobOrder/.*").mock(return_value=httpx.Response(200, json={"data": {}}))
                    r2 = router.get(url__regex=rf"{REST_2}/entity/JobOrder/.*").mock(return_value=httpx.Response(200, json={"data": {}}))
                    plan = ["one", "two"] * 10
                    await asyncio.gather(*(call(client, make_jwt(ALICE, bh_tenant=t), "get_job", {"job_id": 5}, n)
                                           for n, t in enumerate(plan)))
                    missing = await call(client, make_jwt(ALICE), "get_job", {"job_id": 5}, 99)
                    unknown = await call(client, make_jwt(ALICE, bh_tenant="three"), "get_job", {"job_id": 5}, 100)
                assert {c.request.headers["BhRestToken"] for c in r1.calls} == {"bh-T1"} and r1.call_count == 10
                assert {c.request.headers["BhRestToken"] for c in r2.calls} == {"bh-T2"} and r2.call_count == 10
                assert text_of(missing).endswith("identity_required") and text_of(unknown).endswith("identity_required")

            anyio.run(with_app, build_app(s.deployment), fn)
        finally:
            deploy.reset()
            sessions.reset_caches()
