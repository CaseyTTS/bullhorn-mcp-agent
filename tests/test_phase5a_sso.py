"""SSO tenant enablement procedure (Amendment A3-6; R-A3c / SR-28 / SA3-5)."""

from __future__ import annotations

import json
from dataclasses import replace

import anyio
import httpx
import pytest
import respx

from bullhorn_mcp.identity import deploy
from bullhorn_mcp.tenant import changes as tenant_changes
from bullhorn_mcp.tenant.store import SetupStore
from bullhorn_mcp.tools import session as session_tool
from bullhorn_mcp.tools import setup as setup_tools

from ._identity_helpers import ADMIN, ALICE, CLIENT_SECRET, TK1, activate_shared, caller, link, pk
from .test_oauth_routes import ACCESS, BH, CODE, REFRESH, _form, _post, _start, client, complete, link_code, mock_bullhorn


@pytest.fixture
def sso(tmp_path, monkeypatch):
    s = activate_shared(tmp_path, monkeypatch, sso=True)
    yield s
    deploy.reset()


def _login(subject):
    with caller(subject):
        return json.loads(session_tool.bullhorn_session(action="login"))


def _verify_login(admin_url, state_ok=True):
    async def flow():
        async with client() as c:
            state = await _start(c, admin_url)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state if state_ok else 'nope'}")
            if r.status_code == 200:
                link_id, csrf = _form(r.text)
                done = await _post(c, link_id, csrf)
                return r.status_code, link_code(done.text)
            return r.status_code, None

    status, code = anyio.run(flow)
    if code is not None:
        assert complete(ADMIN, code) == {"linked": True}  # B-2: the admin finishes linking in the assistant
    return status


def _records(sso):
    return tenant_changes.read_verifications(SetupStore(sso.tenant().setup_store), tenant_changes.SSO_LOGIN_LOG)


def _enable(verification_id, op="enable_sso_login"):
    change = {"op": op} if op == "disable_sso_login" else {"op": op, "verification_id": verification_id}
    with caller(ADMIN):
        p = setup_tools.propose_mapping_changes(changes=[change])
        if p.startswith("ERROR"):
            return p
        p = json.loads(p)
        return json.loads(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve"))


def test_unverified_sso_tenant_refuses_ordinary_users(sso):
    out = _login(ALICE)
    assert out["status"] == "refused" and out["reason"] == "unsupported_sso"


@respx.mock
def test_admin_verification_login_records_no_secrets_and_links(sso, monkeypatch):
    mock_bullhorn()
    respx.get(url__regex=r".*/ping$").mock(return_value=httpx.Response(200, json={"sessionExpires": 4_102_444_800_000}))
    out = _login(ADMIN)
    assert out.get("sso_verification") is True and "login_url" in out
    assert _verify_login(out["login_url"]) == 200
    records = _records(sso)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "observation" and rec["positive"] is True and rec["code_with_valid_state"] is True
    assert rec["exchange_host"] == "auth.bullhornstaffing.com" and rec["exchange_host_trusted"] is True
    assert rec["rest_login_result"] == "ok" and rec["access_token_lifetime_s"] == 600 and rec["principal"] == pk(ADMIN)
    text = (sso.tenant().setup_store / "verifications" / "sso_login.jsonl").read_text()
    for secret in (CODE, ACCESS, REFRESH, BH, CLIENT_SECRET, "alice", "admin-sub@example.test"):
        assert secret not in text
    assert sso.store.get(TK1, pk(ADMIN)) is not None  # the admin linked through the confirm page + code


@respx.mock
def test_failed_observation_cannot_enable(sso):
    mock_bullhorn()
    out = _login(ADMIN)
    assert _verify_login(out["login_url"], state_ok=False) == 400
    rec = _records(sso)[-1]
    assert rec["positive"] is False and rec["code_with_valid_state"] is False
    link(sso.store, TK1, ADMIN)
    refused = _enable(rec["verification_id"])
    assert refused.startswith("ERROR") and "positive SSO verification" in refused


@respx.mock
def test_enablement_requires_positive_record_and_explicit_commit(sso):
    mock_bullhorn()
    _verify_login(_login(ADMIN)["login_url"])
    vid = _records(sso)[-1]["verification_id"]
    assert _login(ALICE)["reason"] == "unsupported_sso"  # never enabled automatically
    for bogus in ("0" * 32, "not-an-id"):
        assert _enable(bogus).startswith("ERROR")
    with caller(ALICE):  # not an admin
        link(sso.store, TK1, ALICE)
        p = setup_tools.propose_mapping_changes(changes=[{"op": "enable_sso_login", "verification_id": vid}])
        assert p.startswith("ERROR") and "setup admin" in p
    assert _enable(vid)["status"] == "committed"
    assert "login_url" in _login(ALICE)
    assert _enable(None, "disable_sso_login")["status"] == "committed"
    assert _login(ALICE)["reason"] == "unsupported_sso"


@respx.mock
def test_auth_host_change_re_disables(sso):
    mock_bullhorn()
    _verify_login(_login(ADMIN)["login_url"])
    assert _enable(_records(sso)[-1]["verification_id"])["status"] == "committed"
    assert "login_url" in _login(ALICE)
    dep = deploy.current()
    tenant = dep.tenants[TK1]
    moved = replace(tenant, oauth=replace(tenant.oauth, auth_url="https://auth-west.bullhornstaffing.com"))
    deploy.activate(replace(dep, tenants={TK1: moved}), session_store=sso.store)
    assert _login(ALICE)["reason"] == "unsupported_sso"


def test_sso_ops_refused_for_non_sso_tenant(tmp_path, monkeypatch):
    s = activate_shared(tmp_path, monkeypatch, sso=False)
    try:
        link(s.store, TK1, ADMIN)
        assert _enable("0" * 32).startswith("ERROR")
        assert "login_url" in _login(ALICE)
    finally:
        deploy.reset()
