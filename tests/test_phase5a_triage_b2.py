"""5A triage B-2: link completion bound to the initiating principal by a one-time code (T-B2a..f, SR-30)."""

from __future__ import annotations

import json
import logging
import os

import anyio
import httpx
import respx

from bullhorn_mcp import server
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.session_store import EncryptedFileSessionStore, SessionKeys
from bullhorn_mcp.tools import jobs
from bullhorn_mcp.tools import session as session_tool

from . import _identity_helpers
from ._identity_helpers import ALICE, BOB, REST_1, TK1, activate_shared, caller, pk
from .test_oauth_routes import _form, _post, _start, client, complete, link_code, login_url, mock_bullhorn

shared = _identity_helpers.shared  # fixture
MALLORY = "mallory-sub"
CODE = "SENTINEL-AUTH-CODE-5a-c0de"


def browser_completes(url, bullhorn_rest=REST_1):
    """A browser (here: Alice's) completes the given login URL and clicks 'Link'; returns the shown code."""

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert r.status_code == 200, r.text
            link, csrf = _form(r.text)
            done = await _post(c, link, csrf)
            assert done.status_code == 200
            return link_code(done.text)

    return anyio.run(flow)


def status(subject):
    with caller(subject):
        return json.loads(session_tool.bullhorn_session())


@respx.mock
def test_t_b2a_forwarded_login_url_links_nothing(shared):
    mock_bullhorn()  # the browser user authenticates in Bullhorn (as Alice)
    url = login_url(MALLORY)  # Mallory starts a login and forwards the URL to Alice
    alice_code = browser_completes(url)
    assert shared.store.get(TK1, pk(MALLORY)) is None  # nothing active for Mallory
    rest = respx.get(url__regex=rf"{REST_1}/entity/.*").mock(return_value=httpx.Response(200, json={"data": {}}))
    with caller(MALLORY):
        assert jobs.get_job(job_id=1) == "ERROR: permission denied for get_job: bullhorn_auth_required"
        for guess in ("AAAAAAAA", "", "22222222"):
            out = json.loads(session_tool.bullhorn_session(action="complete_link", confirmation=guess or "x"))
            assert out.get("linked") is not True
    assert not rest.called
    assert status(MALLORY)["access_tier"] == "workspace_only"
    assert alice_code  # only Alice's browser ever saw it


@respx.mock
def test_t_b2b_correct_code_from_another_principal_does_nothing(shared):
    mock_bullhorn()
    code = browser_completes(login_url(MALLORY))
    for other in (ALICE, BOB):
        assert complete(other, code) == {"status": "refused", "reason": "no_pending_link"}
        assert shared.store.get(TK1, pk(other)) is None and status(other)["access_tier"] == "workspace_only"
    assert shared.store.get(TK1, pk(MALLORY)) is None
    assert status(MALLORY)["pending_link"] is True  # still pending, still inactive


@respx.mock
def test_t_b2c_five_failures_destroy_the_link(shared):
    mock_bullhorn()
    code = browser_completes(login_url(ALICE))
    wrong = "AAAAAAAA" if code != "AAAAAAAA" else "BBBBBBBB"
    for i in range(4):
        assert complete(ALICE, wrong) == {"status": "refused", "reason": "invalid_confirmation", "pending_link": True}
    assert complete(ALICE, wrong)["pending_link"] is False  # the 5th failure destroys it
    assert complete(ALICE, code) == {"status": "refused", "reason": "no_pending_link"}
    assert shared.store.get(TK1, pk(ALICE)) is None


@respx.mock
def test_t_b2c_expired_and_reuse(shared, monkeypatch):
    mock_bullhorn()
    code = browser_completes(login_url(ALICE))
    real = sessions._now
    monkeypatch.setattr(sessions, "_now", lambda: real() + sessions.LINK_CODE_TTL_SECONDS + 1)
    assert complete(ALICE, code) == {"status": "refused", "reason": "link_expired"}
    monkeypatch.setattr(sessions, "_now", real)
    code2 = browser_completes(login_url(ALICE))
    assert complete(ALICE, code2) == {"linked": True}
    assert complete(ALICE, code2) == {"status": "refused", "reason": "no_pending_link"}  # single use


@respx.mock
def test_t_b2d_legitimate_flow(shared):
    mock_bullhorn()
    code = browser_completes(login_url(ALICE))
    assert status(ALICE)["access_tier"] == "workspace_only" and status(ALICE)["pending_link"] is True
    assert complete(ALICE, code.lower()) == {"linked": True}  # case-insensitive entry
    s = status(ALICE)
    assert s["access_tier"] == "bullhorn_user" and s["pending_link"] is False and s["executing_identity_label"].startswith("bh-link:")


@respx.mock
def test_t_b2e_code_never_in_outputs_logs_or_store(tmp_path, monkeypatch, caplog):
    keys = SessionKeys({"k1": os.urandom(32)}, "k1")
    (tmp_path / "enc").mkdir()
    store = EncryptedFileSessionStore(tmp_path / "enc", keys)
    activate_shared(tmp_path, monkeypatch, store=store)
    try:
        mock_bullhorn()
        outputs = []
        with caplog.at_level(logging.DEBUG):
            url = login_url(ALICE)
            code = browser_completes(url)
            with caller(ALICE):
                outputs.append(session_tool.bullhorn_session())
                outputs.append(session_tool.bullhorn_session(action="complete_link", confirmation="WRONGXYZ"))
                outputs.append(session_tool.bullhorn_session(action="complete_link", confirmation=code))
                outputs.append(session_tool.bullhorn_session())
        blob = "\n".join(outputs) + caplog.text
        assert code not in blob and code.lower() not in blob
        raw = b"".join(p.read_bytes() for p in (tmp_path / "enc").rglob("*") if p.is_file())
        assert code.encode() not in raw
        assert json.loads(outputs[-1])["access_tier"] == "bullhorn_user"
    finally:
        deploy.reset()


def test_t_b2e_memory_store_holds_only_a_keyed_hash(shared):
    from ._identity_helpers import link

    rec = link(shared.store, TK1, ALICE)
    shared.store.delete(TK1, pk(ALICE))
    code = sessions.stage_principal_link(rec)
    data = shared.store.get_principal_link(TK1, pk(ALICE))
    assert code not in json.dumps(data) and len(data["code_hash"]) == 64


def test_t_b2f_schema_and_confirmation_validation(shared):
    props = server.mcp._tool_manager._tools["bullhorn_session"].parameters["properties"]
    assert set(props) == {"action", "confirmation"}
    with caller(ALICE):
        for action in ("status", "login", "logout"):
            out = json.loads(session_tool.bullhorn_session(action=action, confirmation="ABCDEFGH"))
            assert out == {"status": "rejected_validation", "reason": "confirmation_not_allowed"}
        out = json.loads(session_tool.bullhorn_session(action="complete_link"))
        assert out == {"status": "rejected_validation", "reason": "confirmation_required"}
        assert json.loads(session_tool.bullhorn_session(action="complete_link", confirmation="X" * 65))["status"] == "rejected_validation"


def test_service_cannot_complete_and_local_refuses(shared):
    from ._identity_helpers import SVC

    with caller(SVC):
        out = session_tool.bullhorn_session(action="complete_link", confirmation="ABCDEFGH")
    assert out.endswith("service_identity_read_only")
    deploy.reset()
    out = json.loads(session_tool.bullhorn_session(action="complete_link", confirmation="ABCDEFGH"))
    assert out["reason"] == "complete_link_unsupported_in_local_mode"


def test_logout_drops_a_pending_link(shared):
    from ._identity_helpers import link

    rec = link(shared.store, TK1, ALICE)
    sessions.stage_principal_link(rec)
    with caller(ALICE):
        assert json.loads(session_tool.bullhorn_session(action="logout")) == {"logged_out": True}
    assert shared.store.get_principal_link(TK1, pk(ALICE)) is None
