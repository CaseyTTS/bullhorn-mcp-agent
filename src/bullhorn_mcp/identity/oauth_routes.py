"""Bullhorn OAuth browser routes (Phase 5A, D-5A-4 steps 2-4; Amendment A3-6).

Registered with ``FastMCP.custom_route`` (unauthenticated by design, HV-M5) and
served only in ``shared`` mode, only for a ``Host`` in ``server.allowed_hosts``
(HV-M7: the SDK's DNS-rebinding check covers the MCP endpoint, not custom routes).

* ``GET  /oauth/bullhorn/start?login=<id>``: looks up the pending login, sets a
  browser-binding cookie and redirects to the verified Bullhorn authorize URL.
* ``GET  /oauth/bullhorn/callback?code&state``: consumes the pending login
  atomically (known, unexpired, unused, state matches, same browser), exchanges
  the code at the verified host, performs the REST login, checks the session's
  tenant, then shows a **confirmation page** naming the workspace principal.
  Nothing is stored yet.
* ``POST /oauth/bullhorn/confirm``: CSRF token + browser cookie + single-use link.
  This does **not** activate anything (5A triage B-2): it stores a pending link
  for the login's *initiating* principal and shows a one-time confirmation code.
  The session becomes active only when that same principal submits the code with
  ``bullhorn_session(action="complete_link", confirmation=...)``. A forwarded
  login URL completed by someone else therefore links nothing to the forwarder.
  The form body is read streamed and refused above 4 KB (L-1).

Every failure renders the same generic page; the log gets a bounded reason code.
Codes, state, tokens and secrets are never echoed, logged or stored outside the
encrypted session store. An SSO verification login (A3-6) appends an observation
record without any token, code or claim to ``<setup_store>/verifications/sso_login.jsonl``.
"""

from __future__ import annotations

import hmac
import html
import logging
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..auth.oauth_code import OAuthFlowError, tenant_key_for_rest_url
from ..auth.trusted_origins import DEFAULT_POLICY
from . import deploy, sessions
from .session_store import SessionRecord, new_id

logger = logging.getLogger("bullhorn_mcp.identity")

START_PATH = sessions.START_PATH
CALLBACK_PATH = deploy.CALLBACK_PATH
CONFIRM_PATH = "/oauth/bullhorn/confirm"
COOKIE = "__Host-bhmcp_login"
LINK_TTL_SECONDS = 600
MAX_FORM_BYTES = 4096
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'><title>"
        + html.escape(title)
        + "</title></head><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
        + body_html
        + "</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=dict(_SECURITY_HEADERS))


def _fail(reason: str, status: int = 400) -> HTMLResponse:
    logger.warning("bullhorn oauth route refused (%s)", reason[:80])
    response = _page(
        "Bullhorn sign-in failed",
        "<h1>Bullhorn sign-in could not be completed</h1><p>Start again from your assistant with "
        "<code>bullhorn_session(action=\"login\")</code>.</p>",
        status,
    )
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def _host_ok(request: Request) -> bool:
    dep = deploy.current()
    return dep.shared and request.headers.get("host", "") in dep.allowed_hosts


def _cookie(request: Request) -> str | None:
    value = request.cookies.get(COOKIE)
    return value if isinstance(value, str) and 16 <= len(value) <= 128 else None


def _record_sso(tenant: Any, pending: dict[str, Any], **observed: Any) -> str | None:
    """A3-6: append one server-observable SSO verification record (no token, code or claim)."""
    from ..tenant.changes import SSO_LOGIN_LOG, append_verification, sso_client_fingerprint
    from ..tenant.store import SetupStore

    verification_id = uuid.uuid4().hex
    entry = {
        "kind": "observation",
        "verification_id": verification_id,
        "tenant_key": tenant.tenant_key,
        "principal": pending.get("principal_key"),
        "correlation_id": uuid.uuid4().hex,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "login_started_at": pending.get("created_at"),
        "auth_url": tenant.oauth.auth_url,
        "client_fingerprint": sso_client_fingerprint(tenant),
        **observed,
    }
    entry["positive"] = bool(
        entry.get("code_with_valid_state") and entry.get("exchange_result") == "ok"
        and entry.get("exchange_host_trusted") and entry.get("rest_login_result") == "ok"
    )
    try:
        append_verification(SetupStore(tenant.setup_store), SSO_LOGIN_LOG, entry)
    except Exception as exc:
        logger.warning("sso verification record could not be written (%s)", type(exc).__name__)
        return None
    return verification_id


async def start(request: Request) -> Response:
    if not _host_ok(request):
        return Response(status_code=404)
    login_id = request.query_params.get("login", "")
    try:
        pending = deploy.session_store().get_login(login_id)
    except Exception:
        pending = None
    if pending is None or not isinstance(pending.get("expires_at"), (int, float)) or sessions._now() >= pending["expires_at"]:
        return _fail("unknown_or_expired_login")
    tenant = deploy.current().tenant(pending.get("tenant_key"))
    if tenant is None:
        return _fail("tenant_not_configured")
    response = RedirectResponse(sessions.oauth_client(tenant).authorize_url(pending["state"]), status_code=302)
    for k, v in _SECURITY_HEADERS.items():
        response.headers[k] = v
    response.set_cookie(COOKIE, login_id, max_age=sessions.LOGIN_TTL_SECONDS, path="/", secure=True, httponly=True, samesite="lax")
    return response


def _exchange(tenant: Any, code: str) -> tuple[Any, Any]:
    client = sessions.oauth_client(tenant)
    tokens = client.exchange_code(code)
    return tokens, client.rest_login(tokens.access_token)


async def callback(request: Request) -> Response:
    if not _host_ok(request):
        return Response(status_code=404)
    login_id = _cookie(request)
    if login_id is None:
        return _fail("missing_browser_binding")
    try:
        pending = deploy.session_store().take_login(login_id)  # single use, atomic
    except Exception:
        pending = None
    if pending is None:
        return _fail("unknown_or_reused_login")
    tenant = deploy.current().tenant(pending.get("tenant_key"))
    if tenant is None:
        return _fail("tenant_not_configured")
    verification = pending.get("verification") is True
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    state_ok = isinstance(pending.get("state"), str) and hmac.compare_digest(state.encode(), str(pending["state"]).encode())
    expired = not isinstance(pending.get("expires_at"), (int, float)) or sessions._now() >= pending["expires_at"]
    if expired or not state_ok or not code or "error" in request.query_params:
        if verification:
            _record_sso(tenant, pending, code_with_valid_state=False, exchange_result="not_attempted",
                        exchange_host=None, exchange_host_trusted=None, rest_login_result="not_attempted")
        return _fail("expired_login" if expired else "state_mismatch_or_no_code")
    try:
        tokens, rest = await anyio.to_thread.run_sync(_exchange, tenant, code)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, OAuthFlowError) else type(exc).__name__
        if verification:
            _record_sso(tenant, pending, code_with_valid_state=True, exchange_result=reason[:80],
                        exchange_host=None, exchange_host_trusted=None, rest_login_result="not_attempted")
        return _fail(f"exchange_failed:{reason}")
    tk = tenant_key_for_rest_url(rest.rest_url)
    exchange_host = tokens.auth_origin.split("://", 1)[-1]
    if verification:
        client = sessions.oauth_client(tenant)
        session_expires = await anyio.to_thread.run_sync(client.ping, rest)
        _record_sso(
            tenant,
            pending,
            code_with_valid_state=True,
            exchange_result="ok",
            exchange_host=exchange_host,
            exchange_host_trusted=DEFAULT_POLICY.is_trusted_host(exchange_host),
            rest_login_result="ok" if tk == tenant.tenant_key else "tenant_mismatch",
            access_token_lifetime_s=tokens.expires_in,
            session_expires_in_s=(int(session_expires / 1000 - time.time()) if session_expires else None),
        )
    if tk is None or tk != pending.get("tenant_key") or deploy.current().tenant(tk) is None:
        return _fail("tenant_mismatch")
    now = sessions._now()
    record = SessionRecord(
        tenant_key=tk,
        principal_key=str(pending.get("principal_key")),
        rest_url=rest.rest_url,
        bh_rest_token=rest.bh_rest_token,
        bh_expires_at=now + sessions.BH_SESSION_SECONDS,
        access_token=tokens.access_token,
        access_expires_at=now + tokens.expires_in,
        refresh_token=tokens.refresh_token,
        auth_origin=tokens.auth_origin,
        created_at=now,
        last_refresh=now,
    )
    link_id, csrf = new_id(), new_id()
    deploy.session_store().put_link(
        link_id,
        {"login_id": login_id, "csrf": csrf, "expires_at": now + LINK_TTL_SECONDS, "record": record.to_dict(),
         "display": pending.get("display")},
    )
    display = html.escape(str(pending.get("display") or "this workspace account"))
    body = (
        "<h1>Link your Bullhorn account?</h1>"
        f"<p>Workspace account: <strong>{display}</strong></p>"
        f"<p>Bullhorn tenant: <strong>{html.escape(tenant.alias)}</strong></p>"
        "<p>Only continue if <em>you</em> asked your assistant to sign in to Bullhorn just now. "
        "The assistant will then act in Bullhorn as you.</p>"
        f"<form method='post' action='{CONFIRM_PATH}'>"
        f"<input type='hidden' name='link' value='{html.escape(link_id)}'>"
        f"<input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
        "<button type='submit'>Link this Bullhorn account</button></form>"
    )
    return _page("Link your Bullhorn account", body)


async def confirm(request: Request) -> Response:
    if not _host_ok(request):
        return Response(status_code=404)
    origin = request.headers.get("origin")
    if origin is not None and origin != deploy.current().service_base_url:
        return _fail("cross_origin_confirm", 403)
    login_id = _cookie(request)
    if login_id is None:
        return _fail("missing_browser_binding", 403)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        return _fail("form_too_large", 413)
    raw = b""
    async for chunk in request.stream():  # L-1: never buffer more than MAX_FORM_BYTES
        raw += chunk
        if len(raw) > MAX_FORM_BYTES:
            return _fail("form_too_large", 413)
    form = parse_qs(raw.decode("ascii", "replace"), max_num_fields=4)
    link_id = (form.get("link") or [""])[0]
    csrf = (form.get("csrf") or [""])[0]
    try:
        link = deploy.session_store().take_link(link_id)  # single use, atomic
    except Exception:
        link = None
    if link is None:
        return _fail("unknown_or_reused_link", 403)
    if not hmac.compare_digest(str(link.get("login_id")).encode(), login_id.encode()) or not hmac.compare_digest(
        str(link.get("csrf")).encode(), csrf.encode()
    ):
        return _fail("csrf_or_browser_mismatch", 403)
    if not isinstance(link.get("expires_at"), (int, float)) or sessions._now() >= link["expires_at"]:
        return _fail("expired_link", 403)
    record = SessionRecord.from_dict(link.get("record"))
    if record is None or deploy.current().tenant(record.tenant_key) is None:
        return _fail("invalid_link", 403)
    code = sessions.stage_principal_link(record)  # B-2: nothing is active until the code comes back
    display = html.escape(str(link.get("display") or "this workspace account"))
    response = _page(
        "Finish linking in your assistant",
        "<h1>One more step</h1>"
        f"<p>This Bullhorn sign-in will be linked to the workspace account <strong>{display}</strong>.</p>"
        "<p><strong>Only continue if you are this person.</strong> Enter this code yourself in your assistant "
        "(it asks for it with <code>bullhorn_session(action=\"complete_link\")</code>). "
        "Never give it to anyone.</p>"
        f"<p style='font-size:2em;letter-spacing:0.2em'><code>{html.escape(code)}</code></p>"
        "<p>The code expires in 10 minutes and works once.</p>",
    )
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


def register(mcp: Any) -> None:
    """Register the three routes on the FastMCP instance (they answer only in shared mode)."""
    mcp.custom_route(START_PATH, methods=["GET"])(start)
    mcp.custom_route(CALLBACK_PATH, methods=["GET"])(callback)
    mcp.custom_route(CONFIRM_PATH, methods=["POST"])(confirm)
