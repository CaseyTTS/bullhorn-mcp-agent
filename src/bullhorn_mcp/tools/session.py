"""The ``bullhorn_session`` MCP tool (Phase 5A, tool 20) and the shared-mode dispatch gate.

``bullhorn_session(action="status"|"login"|"complete_link"|"logout", confirmation=None)``:
the caller, the tenant and the tier always come from the verified request
(``identity.principal``), never from arguments. ``confirmation`` is the one-time
code shown on the browser confirm page; it is accepted only with
``complete_link`` (5A triage B-2). No action returns a token, an OAuth code,
a state value, a confirmation code or a secret.

- ``status``: mode, principal display, tenant hint, session state, expiry,
  execution-identity label, ``access_tier`` and whether ``create_note`` is
  enabled for the tenant.
- ``login``: a single-use HTTPS start URL (the browser does Bullhorn sign-in,
  including SSO/Duo; the server never sees passwords or MFA codes). Refused in
  ``local`` mode, for service principals, and with ``unsupported_sso`` on an SSO
  tenant whose SSO login is not enabled, except for that tenant's setup admins,
  whose login runs in verification mode (Amendment A3-6).
- ``complete_link``: activates the caller's *own* pending link when the code
  matches (5 wrong codes destroy it; it expires after 10 minutes).
- ``logout``: deletes only the caller's own session (and pending link) for the
  current tenant (local only: Bullhorn token revocation is undocumented, HV-C6).

Importing this module also installs the **dispatch gate** (Amendment A2-2) on
the server's tool manager: in ``shared`` mode every tool call, including any
tool registered later, is checked with ``permissions.check`` before the tool
runs, so a tool that forgets its own check is still denied by default.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from .. import server
from ..crosscutting import audit, permissions
from ..identity import deploy, oauth_routes, sessions
from ..identity.principal import IdentityRequired, current_identity

ACTIONS = ("status", "login", "complete_link", "logout")
TOOL = "bullhorn_session"


def _create_note_enabled(ident: Any) -> bool:
    """Is ``create_note`` (person targets) enabled for the caller's tenant? (D-5A-15 step 3)"""
    from ..tenant.changes import note_write_enabled
    from ..tenant.profile_v2 import rest_url_fingerprint
    from ..tenant.store import SetupStore
    from ..writes import pipeline

    if pipeline.HV_B11_VERIFIED:
        return True
    if not deploy.is_shared() or ident.tenant is None or ident.principal_key is None or not pipeline.P4B8_CLOSED:
        return False
    rec = deploy.session_store().get(ident.tenant_key, ident.principal_key) if ident.access_tier == "bullhorn_user" else None
    if rec is None:
        return False
    try:
        return note_write_enabled(SetupStore(ident.tenant.setup_store), rest_url_fingerprint(rec.rest_url))
    except Exception:
        return False


def _sso_login_enabled(tenant: Any) -> bool:
    from ..tenant.changes import sso_login_enabled
    from ..tenant.store import SetupStore

    try:
        return sso_login_enabled(SetupStore(tenant.setup_store), tenant)
    except Exception:
        return False


def _status() -> dict[str, Any]:
    ident = current_identity()
    if ident.mode == "local":
        return {
            "mode": "local",
            "principal_display": ident.principal_display,
            "tenant_key_hint": None,
            "session": "none",
            "expires_at": None,
            "executing_identity_label": "local:env-credentials",
            "access_tier": "local",
            "create_note_enabled_for_tenant": _create_note_enabled(ident),
            "pending_link": False,
        }
    session, expires_at = sessions.session_state(ident)
    return {
        "mode": "shared",
        "principal_display": ident.principal_display,
        "tenant_key_hint": ident.tenant_key[:8] if ident.tenant_key else None,
        "session": session,
        "expires_at": expires_at,
        "executing_identity_label": ident.executing_bullhorn_identity,
        "access_tier": ident.access_tier,
        "create_note_enabled_for_tenant": _create_note_enabled(ident),
        "pending_link": sessions.has_pending_link(ident),
    }


def _refused(reason: str, message: str) -> dict[str, Any]:
    return {"status": "refused", "reason": reason, "message": message}


def _login() -> dict[str, Any]:
    if not deploy.is_shared():
        return _refused("login_unsupported_in_local_mode", "local mode signs in with the configured environment credentials")
    ident = current_identity()
    if ident.access_tier == "service":
        return _refused("service_identity", "service principals use the configured read-only service identity")
    tenant = ident.tenant
    verification = False
    if tenant.sso and not _sso_login_enabled(tenant):
        if not tenant.roles.is_setup_admin(ident.principal_key):  # B-4: an admin of *this* tenant
            return _refused(
                "unsupported_sso",
                "Bullhorn SSO sign-in is not yet enabled for this tenant; a setup admin must verify it first",
            )
        verification = True  # A3-6: a setup admin's verification login
    out: dict[str, Any] = dict(sessions.begin_login(ident, verification=verification))
    if verification:
        out["sso_verification"] = True
    return out


def _complete_link(confirmation: str) -> dict[str, Any]:
    if not deploy.is_shared():
        return _refused("complete_link_unsupported_in_local_mode", "local mode has no per-user Bullhorn session")
    ident = current_identity()
    if ident.access_tier == "service":
        return _refused("service_identity", "service principals have no per-user session")
    return sessions.complete_link(ident, confirmation)


def _logout() -> dict[str, Any]:
    if not deploy.is_shared():
        return _refused("logout_unsupported_in_local_mode", "local mode has no per-user Bullhorn session")
    ident = current_identity()
    if ident.access_tier == "service":
        return _refused("service_identity", "service principals have no per-user session")
    sessions.logout(ident)
    return {"logged_out": True}


@server.mcp.tool()
def bullhorn_session(action: str = "status", confirmation: str | None = None) -> str:
    """Show, start, complete or end your own Bullhorn sign-in for this workspace.

    Sign-in happens in your browser through Bullhorn (including SSO/MFA); never
    give passwords or MFA codes to the assistant. After the browser step, the
    page shows a one-time code: the user enters it here with action="complete_link".

    Args:
        action: "status" (default), "login" (returns a sign-in link),
            "complete_link" (finish linking with the code from the browser page) or "logout".
        confirmation: The one-time code from the browser page; only for "complete_link".

    Returns:
        JSON with the session status, the sign-in link, {"linked": true} or {"logged_out": true}.
    """
    start = time.perf_counter()
    args = {"action": action, "confirmation": None if confirmation is None else "<provided>"}  # never the code
    operation = "read" if action == "status" else "session"
    decision = permissions.check(TOOL, operation)
    if not decision.allowed:
        audit.log_invocation(tool=TOOL, args=args, result_summary="denied", duration_ms=(time.perf_counter() - start) * 1000, success=False)
        return server._permission_denied_message(TOOL, decision)
    try:
        if action not in ACTIONS:
            result = f"ERROR: action must be one of {list(ACTIONS)}"
            summary, success = "error: invalid action", False
        elif (action == "complete_link") != (confirmation is not None) or (
            confirmation is not None and (not isinstance(confirmation, str) or len(confirmation) > 64)
        ):
            reason = "confirmation_required" if action == "complete_link" else "confirmation_not_allowed"
            result = server.format_response({"status": "rejected_validation", "reason": reason})
            summary, success = f"rejected_validation: {reason}", False
        else:
            if action == "complete_link":
                assert confirmation is not None
                data = _complete_link(confirmation)
            else:
                data = _status() if action == "status" else _login() if action == "login" else _logout()
            result = server.format_response(data)
            summary, success = f"ok: {action}", True
    except IdentityRequired:
        result, summary, success = "ERROR: identity_required", "error: identity_required", False
    except Exception as exc:  # the MCP boundary: never a raw exception, never a secret
        result, summary, success = f"ERROR: internal error ({type(exc).__name__})", f"error: {type(exc).__name__}", False
    audit.log_invocation(tool=TOOL, args=args, result_summary=summary, duration_ms=(time.perf_counter() - start) * 1000, success=success)
    return result


# ---------------------------------------------------------------------- #
# Dispatch gate (A2-2) and OAuth routes
# ---------------------------------------------------------------------- #


def install_dispatch_gate(mcp: Any) -> None:
    """Wrap ``mcp._tool_manager.call_tool`` once: in shared mode, deny by policy before any tool runs."""
    manager = mcp._tool_manager
    if getattr(manager, "_bhmcp_gated", False):
        return
    original = manager.call_tool

    async def gated_call_tool(name: str, arguments: dict[str, Any], context: Any = None, convert_result: bool = False) -> Any:
        if deploy.is_shared():
            decision = permissions.check(name, "dispatch")
            if not decision.allowed:
                audit.log_invocation(tool=name, args={}, result_summary="denied", duration_ms=0.0, success=False)
                tool = manager.get_tool(name)
                if tool is None:
                    raise ToolError(f"Unknown tool: {name}")
                message = server._permission_denied_message(name, decision)
                return tool.fn_metadata.convert_result(message) if convert_result else message
        return await original(name, arguments, context=context, convert_result=convert_result)

    setattr(manager, "call_tool", gated_call_tool)
    setattr(manager, "_bhmcp_gated", True)


install_dispatch_gate(server.mcp)
oauth_routes.register(server.mcp)
