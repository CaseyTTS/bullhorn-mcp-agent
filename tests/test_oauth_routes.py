"""The Bullhorn OAuth browser routes (D-5A-4; AC-17 / SR-14, login-CSRF and account-linking)."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.routing import Route

from bullhorn_mcp.identity import deploy, oauth_routes, sessions
from bullhorn_mcp.tools import session as session_tool

from ._identity_helpers import (
    ALICE,
    AUTH,
    BASE,
    BOB,
    CLIENT_SECRET,
    LOGIN,
    REST_1,
    REST_2,
    TK1,
    caller,
    pk,
)
from . import _identity_helpers

shared = _identity_helpers.shared  # fixture

CODE = "SENTINEL-AUTH-CODE-5a-c0de"
ACCESS = "SENTINEL-ACCESS-5a-acce55"
REFRESH = "SENTINEL-REFRESH-5a-7e7e"
BH = "SENTINEL-BHREST-5a-bbbb"

APP = Starlette(
    routes=[
        Route(oauth_routes.START_PATH, oauth_routes.start, methods=["GET"]),
        Route(oauth_routes.CALLBACK_PATH, oauth_routes.callback, methods=["GET"]),
        Route(oauth_routes.CONFIRM_PATH, oauth_routes.confirm, methods=["POST"]),
    ]
)


def run(fn):
    return anyio.run(fn)


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=APP), base_url=BASE, follow_redirects=False)


def login_url(subject=ALICE):
    with caller(subject):
        return json.loads(session_tool.bullhorn_session(action="login"))["login_url"]


def mock_bullhorn(rest=REST_1):
    token = respx.post(f"{AUTH}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 600})
    )
    login = respx.post(f"{LOGIN}/rest-services/login").mock(return_value=httpx.Response(200, json={"BhRestToken": BH, "restUrl": rest}))
    return token, login


async def _start(c, url):
    r = await c.get(url.replace(BASE, ""))
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    q = parse_qs(urlsplit(loc).query)
    assert loc.startswith(f"{AUTH}/oauth/authorize?") and q["redirect_uri"] == [BASE + deploy.CALLBACK_PATH]
    return q["state"][0]


def link_code(html):
    """The one-time confirmation code shown on the confirm page (B-2)."""
    return re.search(r"<code>([A-Z2-7]{8})</code>", html).group(1)


def complete(subject, code):
    with caller(subject):
        return json.loads(session_tool.bullhorn_session(action="complete_link", confirmation=code))


def _form(html):
    link = re.search(r"name='link' value='([^']+)'", html).group(1)
    csrf = re.search(r"name='csrf' value='([^']+)'", html).group(1)
    return link, csrf


async def _post(c, link, csrf, **headers):
    return await c.post(oauth_routes.CONFIRM_PATH, content=f"link={link}&csrf={csrf}",
                        headers={"content-type": "application/x-www-form-urlencoded", **headers})


@respx.mock
def test_happy_path_stores_only_after_confirm(shared, caplog):
    mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert r.status_code == 200 and "Link this Bullhorn account" in r.text
            assert "alice-sub@example.test" in r.text and "one" in r.text
            for secret in (CODE, state, ACCESS, REFRESH, BH, CLIENT_SECRET):
                assert secret not in r.text
            assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
            assert shared.store.get(TK1, pk(ALICE)) is None  # nothing stored before the confirm POST
            link, csrf = _form(r.text)
            done = await _post(c, link, csrf)
            assert done.status_code == 200 and "Only continue if you are this person" in done.text
            again = await _post(c, link, csrf)
            assert again.status_code == 403  # the link is single use
            return state, link_code(done.text)

    with caplog.at_level(logging.DEBUG):
        state, code = run(flow)
        assert shared.store.get(TK1, pk(ALICE)) is None  # B-2: nothing active until the code comes back
        with caller(ALICE):
            assert json.loads(session_tool.bullhorn_session())["pending_link"] is True
        assert complete(ALICE, code) == {"linked": True}
    assert code not in caplog.text
    rec = shared.store.get(TK1, pk(ALICE))
    assert rec is not None and rec.bh_rest_token == BH and rec.refresh_token == REFRESH
    for secret in (CODE, state, ACCESS, REFRESH, BH, CLIENT_SECRET):
        assert secret not in caplog.text
    with caller(ALICE):
        assert json.loads(session_tool.bullhorn_session())["access_tier"] == "bullhorn_user"


@pytest.mark.parametrize("bad", ["unknown", "mismatched", "missing_code", "error_param"])
@respx.mock
def test_bad_callback_stores_nothing_and_consumes_login(shared, bad):
    token, _ = mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            query = {
                "unknown": f"code={CODE}&state=not-the-state",
                "mismatched": f"code={CODE}&state={state[:-2]}xx",
                "missing_code": f"state={state}",
                "error_param": f"code={CODE}&state={state}&error=access_denied",
            }[bad]
            r = await c.get(f"{deploy.CALLBACK_PATH}?{query}")
            assert r.status_code == 400 and "could not be completed" in r.text and CODE not in r.text and state not in r.text
            replay = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert replay.status_code == 400  # the pending login was consumed

    run(flow)
    assert not token.called and shared.store.get(TK1, pk(ALICE)) is None


@respx.mock
def test_replayed_callback_refused(shared):
    token, _ = mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            assert (await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")).status_code == 200
            assert (await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")).status_code == 400

    run(flow)
    assert token.call_count == 1


@respx.mock
def test_expired_login_refused(shared, monkeypatch):
    token, _ = mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            monkeypatch.setattr(sessions, "_now", lambda: 10**12)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert r.status_code == 400

    run(flow)
    assert not token.called


@respx.mock
def test_callback_from_another_browser_refused(shared):
    token, _ = mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as victim_browser:
            state = await _start(victim_browser, url)
        async with client() as other_browser:  # no binding cookie
            r = await other_browser.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert r.status_code == 400

    run(flow)
    assert not token.called


@respx.mock
def test_confirm_requires_csrf_cookie_and_matching_login(shared):
    mock_bullhorn()
    url_a = login_url(ALICE)

    async def flow():
        async with client() as c:
            state = await _start(c, url_a)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            link, csrf = _form(r.text)
            assert (await _post(c, link, "wrong-csrf-" + "x" * 30)).status_code == 403
        # the failed attempt consumed the link: nothing is stored
        assert shared.store.get(TK1, pk(ALICE)) is None

    run(flow)


@respx.mock
def test_confirm_for_another_login_rejected(shared):
    """A confirm POST in browser B for browser A's link (login-CSRF / account linking) is rejected."""
    mock_bullhorn()
    url_a, url_b = login_url(ALICE), login_url(BOB)

    async def flow():
        async with client() as a, client() as b:
            sa = await _start(a, url_a)
            ra = await a.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={sa}")
            link_a, csrf_a = _form(ra.text)
            await _start(b, url_b)  # browser B holds its own (different) login cookie
            r = await _post(b, link_a, csrf_a)
            assert r.status_code == 403

    run(flow)
    assert shared.store.get(TK1, pk(ALICE)) is None and shared.store.get(TK1, pk(BOB)) is None


@respx.mock
def test_cross_origin_confirm_rejected(shared):
    mock_bullhorn()
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            link, csrf = _form(r.text)
            assert (await _post(c, link, csrf, origin="https://attacker.example")).status_code == 403

    run(flow)
    assert shared.store.get(TK1, pk(ALICE)) is None


@respx.mock
def test_other_tenant_rest_url_refused(shared):
    mock_bullhorn(rest=REST_2)  # a session for a corpToken that is not this tenant
    url = login_url()

    async def flow():
        async with client() as c:
            state = await _start(c, url)
            r = await c.get(f"{deploy.CALLBACK_PATH}?code={CODE}&state={state}")
            assert r.status_code == 400

    run(flow)
    assert shared.store.get(TK1, pk(ALICE)) is None


def test_wrong_host_and_local_mode_are_404(shared):
    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=APP), base_url="https://evil.example") as c:
            assert (await c.get(f"{oauth_routes.START_PATH}?login=x")).status_code == 404

    run(flow)
    deploy.reset()

    async def local():
        async with client() as c:
            assert (await c.get(f"{oauth_routes.START_PATH}?login=x")).status_code == 404
            assert (await c.get(deploy.CALLBACK_PATH)).status_code == 404

    run(local)


def test_unknown_start_login_refused(shared):
    async def flow():
        async with client() as c:
            r = await c.get(f"{oauth_routes.START_PATH}?login=" + "A" * 43)
            assert r.status_code == 400

    run(flow)


def test_routes_registered_on_the_server():
    from bullhorn_mcp import server

    paths = {r.path for r in server.mcp._custom_starlette_routes}
    assert {oauth_routes.START_PATH, oauth_routes.CALLBACK_PATH, oauth_routes.CONFIRM_PATH} <= paths
