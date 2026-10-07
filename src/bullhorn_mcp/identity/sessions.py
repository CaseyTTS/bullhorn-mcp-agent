"""Per-caller Bullhorn session resolution (Phase 5A, D-5A-8, Amendment A2-3).

``resolve_client()`` replaces the process-global client in ``shared`` mode:

1. read the principal (``identity.principal``; fail closed);
2. a **service** principal gets a client bound to that tenant's read-only
   service identity; every other caller gets *their own* linked session, or
   ``BullhornSessionRequired``. A ``workspace_only`` caller never causes a
   service-session resolution (A2-3), and there is no fallback of any kind;
3. an expired session is refreshed under a per-``(tenant, principal)`` lock with
   the refresh grant + REST login (HV-C3/HV-C4); the rotated refresh token is
   stored atomically. A failed refresh deletes the session (D-5A-4 step 5);
4. a **new** ``BullhornClient`` is returned for every call.

``BullhornSessionRequired`` subclasses ``AuthenticationError`` so the legacy
tools' existing ``ERROR:`` / ``connected: false`` paths report it unchanged.

5A triage:

- B-2: the browser confirm page no longer activates a session. It stores a
  *principal link* for the login's initiating principal and shows a one-time
  8-character code; only ``complete_link`` by that same principal, with that code
  (constant-time; 5 failures destroy it; 10-minute TTL), activates the session.
- B-3: completion mints a ``link_id``; refresh preserves it, logout/re-link replace it.
- L-3: per-principal locks live in a bounded LRU.
- L-5: a session is deleted ``session_store.max_session_days`` after link completion.
"""

from __future__ import annotations

import base64
import collections
import datetime as _dt
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from typing import Any, cast

from ..auth import AuthenticationError, BullhornAuth, BullhornSession
from ..auth.oauth_code import OAuthCodeClient, OAuthFlowError, tenant_key_for_rest_url
from ..bullhorn.client import BullhornClient
from ..config import BullhornConfig
from . import deploy
from .principal import IdentityContext, IdentityRequired, current_identity
from .session_store import SessionRecord, new_id

logger = logging.getLogger("bullhorn_mcp.identity")

EXPIRY_MARGIN_SECONDS = 60
# HV-C4: Bullhorn documents no fixed session lifetime ("Never assume that a REST session will not
# expire"); a session is treated as valid for at most this long and is refreshed on any 401.
BH_SESSION_SECONDS = 600
LOGIN_TTL_SECONDS = 600
LINK_CODE_TTL_SECONDS = 600
MAX_LINK_CODE_FAILURES = 5
MAX_LOCKS = 10_000
START_PATH = "/oauth/bullhorn/start"
_CODE_RE = re.compile(r"[A-Z2-7]{8}", re.ASCII)
_CODE_KEY = secrets.token_bytes(32)  # process key for the keyed code hash (shared mode runs one process)


class BullhornSessionRequired(AuthenticationError):
    """The caller has no usable Bullhorn session (login with bullhorn_session). Never carries secrets."""

    def __init__(self, reason: str = "bullhorn_session_required") -> None:
        self.reason = reason
        super().__init__(f"{reason}: link your Bullhorn account with bullhorn_session(action='login')")


def _now() -> float:
    """The session clock (patched in tests)."""
    return time.time()


_locks: collections.OrderedDict[tuple[str, str], threading.Lock] = collections.OrderedDict()
_locks_guard = threading.Lock()


def principal_lock(tenant_key: str, principal_key: str) -> threading.Lock:
    """One lock per ``(tenant, principal)``: refreshes and logins are serialised (HV-C4, HV-C9).

    L-3: at most ``MAX_LOCKS`` are kept; the least recently used *unlocked* ones are evicted.
    """
    key = (tenant_key, principal_key)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        _locks.move_to_end(key)
        if len(_locks) > MAX_LOCKS:
            for old in list(_locks)[: len(_locks) - MAX_LOCKS]:
                if old != key and not _locks[old].locked():
                    del _locks[old]
        return lock


def oauth_client(tenant: Any) -> OAuthCodeClient:
    creds = deploy.credentials()
    ref = tenant.oauth.client_secret_ref
    return OAuthCodeClient(
        client_id=tenant.oauth.client_id,
        client_secret=lambda: creds.resolve(ref),
        redirect_uri=tenant.oauth.redirect_uri,
        auth_url=tenant.oauth.auth_url,
        login_url=tenant.oauth.login_url,
    )


def _reason(exc: BaseException) -> str:
    return exc.reason if isinstance(exc, OAuthFlowError) else type(exc).__name__


def _refresh_locked(tenant: Any, rec: SessionRecord) -> SessionRecord | None:
    """Refresh grant + REST login. On any failure the session is deleted (``None``)."""
    store = deploy.session_store()
    if not rec.refresh_token:
        store.delete(rec.tenant_key, rec.principal_key)
        return None
    try:
        client = oauth_client(tenant)
        tokens = client.refresh(rec.refresh_token, rec.auth_origin)
        rest = client.rest_login(tokens.access_token)
    except Exception as exc:
        logger.warning("bullhorn session refresh failed (%s); the session was deleted", _reason(exc))
        store.delete(rec.tenant_key, rec.principal_key)
        return None
    if tenant_key_for_rest_url(rest.rest_url) != rec.tenant_key:
        logger.warning("bullhorn session refresh returned another tenant; the session was deleted")
        store.delete(rec.tenant_key, rec.principal_key)
        return None
    now = _now()
    new = SessionRecord(
        tenant_key=rec.tenant_key,
        principal_key=rec.principal_key,
        rest_url=rest.rest_url,
        bh_rest_token=rest.bh_rest_token,
        bh_expires_at=now + BH_SESSION_SECONDS,
        access_token=tokens.access_token,
        access_expires_at=now + tokens.expires_in,
        refresh_token=tokens.refresh_token,  # HV-C3: rotated; the previous one is no longer valid
        auth_origin=tokens.auth_origin,
        bullhorn_user_ref=rec.bullhorn_user_ref,
        created_at=rec.created_at,
        last_refresh=now,
        link_id=rec.link_id,  # B-3: a refresh keeps the link (and so the execution-identity label)
    )
    store.put(new)
    return new


def _expired_absolutely(rec: SessionRecord) -> bool:
    """L-5: the absolute lifetime, counted from link completion."""
    return _now() >= rec.created_at + deploy.current().session_max_days * 86_400


def load_valid_session(tenant: Any, principal_key: str) -> SessionRecord | None:
    """The caller's session for ``tenant`` (refreshed if expired), or ``None``. Never raises for auth failures."""
    store = deploy.session_store()
    tk = tenant.tenant_key
    with principal_lock(tk, principal_key):
        rec = store.get(tk, principal_key)
        if rec is None:
            return None
        if _expired_absolutely(rec) or not rec.link_id:
            store.delete(tk, principal_key)
            return None
        if _now() < rec.bh_expires_at - EXPIRY_MARGIN_SECONDS:
            return rec
        return _refresh_locked(tenant, rec)


def force_refresh(tenant: Any, principal_key: str, used_token: str) -> SessionRecord:
    """After a 401: refresh unless another call already did. Raises ``BullhornSessionRequired``."""
    store = deploy.session_store()
    tk = tenant.tenant_key
    with principal_lock(tk, principal_key):
        rec = store.get(tk, principal_key)
        if rec is None or _expired_absolutely(rec) or not rec.link_id:
            if rec is not None:
                store.delete(tk, principal_key)
            raise BullhornSessionRequired("bullhorn_session_expired")
        if rec.bh_rest_token != used_token and _now() < rec.bh_expires_at - EXPIRY_MARGIN_SECONDS:
            return rec
        new = _refresh_locked(tenant, rec)
    if new is None:
        raise BullhornSessionRequired("bullhorn_session_expired")
    return new


class UserSessionAuth:
    """The ``client.auth`` of a per-call client: exposes ``session`` and ``_refresh_session`` only."""

    def __init__(self, tenant: Any, principal_key: str, record: SessionRecord) -> None:
        self._tenant = tenant
        self._principal_key = principal_key
        self._record = record

    def __repr__(self) -> str:
        return "UserSessionAuth(<redacted>)"

    @property
    def session(self) -> BullhornSession:
        if _now() >= self._record.bh_expires_at - EXPIRY_MARGIN_SECONDS:
            rec = load_valid_session(self._tenant, self._principal_key)
            if rec is None:
                raise BullhornSessionRequired("bullhorn_session_expired")
            self._record = rec
        return BullhornSession(
            bh_rest_token=self._record.bh_rest_token, rest_url=self._record.rest_url, expires_at=self._record.bh_expires_at
        )

    def _refresh_session(self) -> None:
        self._record = force_refresh(self._tenant, self._principal_key, self._record.bh_rest_token)


# ---------------------------------------------------------------------- #
# Service identity (D-5A-5 (b), D-5-11): reads only, service principals only
# ---------------------------------------------------------------------- #

_service_auth: dict[str, BullhornAuth] = {}
_service_guard = threading.Lock()


def service_client(tenant: Any) -> BullhornClient:
    if tenant is None or tenant.service is None:
        raise BullhornSessionRequired("service_identity_not_configured")
    creds = deploy.credentials()
    with _service_guard:
        auth = _service_auth.get(tenant.tenant_key)
        if auth is None:
            s = tenant.service
            try:
                config = BullhornConfig(
                    client_id=s.client_id,
                    client_secret=creds.resolve(s.client_secret_ref),
                    username=creds.resolve(s.username_ref),
                    password=creds.resolve(s.password_ref),
                    auth_url=tenant.oauth.auth_url,
                    login_url=tenant.oauth.login_url,
                )
            except ValueError:
                raise BullhornSessionRequired("service_identity_not_configured") from None
            auth = BullhornAuth(config)
            _service_auth[tenant.tenant_key] = auth
    if tenant_key_for_rest_url(auth.session.rest_url) != tenant.tenant_key:
        raise BullhornSessionRequired("service_identity_tenant_mismatch")
    return BullhornClient(auth)


def reset_caches() -> None:
    """Tests: forget cached service auth objects and locks."""
    with _service_guard:
        _service_auth.clear()
    with _locks_guard:
        _locks.clear()


# ---------------------------------------------------------------------- #
# resolve_client (server.get_client() in shared mode)
# ---------------------------------------------------------------------- #


def resolve_client() -> BullhornClient:
    try:
        ident = current_identity()
    except IdentityRequired:
        raise BullhornSessionRequired("identity_required") from None
    if ident.access_tier == "service":
        return service_client(ident.tenant)
    if ident.access_tier != "bullhorn_user" or ident.principal_key is None:
        raise BullhornSessionRequired()
    rec = load_valid_session(ident.tenant, ident.principal_key)
    if rec is None:
        raise BullhornSessionRequired()
    return BullhornClient(cast(BullhornAuth, UserSessionAuth(ident.tenant, ident.principal_key, rec)))


# ---------------------------------------------------------------------- #
# Login / logout / status helpers (used by tools/session.py and the OAuth routes)
# ---------------------------------------------------------------------- #


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def begin_login(ident: IdentityContext, *, verification: bool = False) -> dict[str, str]:
    """Create a single-use pending login (D-5A-4 step 1) and return the start URL."""
    dep = deploy.current()
    assert ident.tenant_key is not None and ident.principal_key is not None and dep.service_base_url is not None
    login_id, state, now = new_id(), new_id(), _now()
    deploy.session_store().put_login(
        login_id,
        {
            "state": state,
            "tenant_key": ident.tenant_key,
            "principal_key": ident.principal_key,
            "display": ident.principal_display,
            "created_at": now,
            "expires_at": now + LOGIN_TTL_SECONDS,
            "verification": verification,
        },
    )
    return {"login_url": f"{dep.service_base_url}{START_PATH}?login={login_id}", "expires_at": _iso(now + LOGIN_TTL_SECONDS)}


def logout(ident: IdentityContext) -> bool:
    """Delete only ``(tenant_key, principal_key)`` of the caller. Local-only: HV-C6 (revocation) is undocumented."""
    assert ident.tenant_key is not None and ident.principal_key is not None
    with principal_lock(ident.tenant_key, ident.principal_key):
        store = deploy.session_store()
        store.delete_principal_link(ident.tenant_key, ident.principal_key)
        return bool(store.delete(ident.tenant_key, ident.principal_key))


# ---------------------------------------------------------------------- #
# B-2: link completion with a one-time confirmation code
# ---------------------------------------------------------------------- #


def _code_hash(tenant_key: str, principal_key: str, salt: str, code: str) -> str:
    message = f"{tenant_key}|{principal_key}|{salt}|{code}".encode("ascii", "replace")
    return hmac.new(_CODE_KEY, message, hashlib.sha256).hexdigest()


def stage_principal_link(record: SessionRecord) -> str:
    """Store a confirmed link for the record's principal; return the one-time code (shown once, never stored)."""
    code = base64.b32encode(secrets.token_bytes(5)).decode("ascii")  # 8 chars of [A-Z2-7], 40 bits
    salt = secrets.token_urlsafe(16)
    with principal_lock(record.tenant_key, record.principal_key):
        deploy.session_store().put_principal_link(
            record.tenant_key,
            record.principal_key,
            {
                "record": record.to_dict(),
                "salt": salt,
                "code_hash": _code_hash(record.tenant_key, record.principal_key, salt, code),
                "expires_at": _now() + LINK_CODE_TTL_SECONDS,
                "failures": 0,
            },
        )
    return code


def has_pending_link(ident: IdentityContext) -> bool:
    if ident.tenant_key is None or ident.principal_key is None:
        return False
    data = deploy.session_store().get_principal_link(ident.tenant_key, ident.principal_key)
    return data is not None and isinstance(data.get("expires_at"), (int, float)) and _now() < data["expires_at"]


def complete_link(ident: IdentityContext, confirmation: str) -> dict[str, Any]:
    """Activate the caller's own pending link if ``confirmation`` matches (B-2). Never returns the code."""
    assert ident.tenant_key is not None and ident.principal_key is not None
    tk, pk = ident.tenant_key, ident.principal_key
    store = deploy.session_store()
    with principal_lock(tk, pk):
        data = store.get_principal_link(tk, pk)
        if data is None:
            return {"status": "refused", "reason": "no_pending_link"}
        if not isinstance(data.get("expires_at"), (int, float)) or _now() >= data["expires_at"]:
            store.delete_principal_link(tk, pk)
            return {"status": "refused", "reason": "link_expired"}
        code = confirmation.strip().upper() if isinstance(confirmation, str) else ""
        stored = data.get("code_hash")
        ok = (
            _CODE_RE.fullmatch(code) is not None
            and isinstance(stored, str)
            and hmac.compare_digest(_code_hash(tk, pk, str(data.get("salt")), code), stored)
        )
        if not ok:
            prior = data.get("failures")
            failures = prior + 1 if type(prior) is int else MAX_LINK_CODE_FAILURES
            if failures >= MAX_LINK_CODE_FAILURES:
                store.delete_principal_link(tk, pk)
                return {"status": "refused", "reason": "invalid_confirmation", "pending_link": False}
            store.put_principal_link(tk, pk, {**data, "failures": failures})
            return {"status": "refused", "reason": "invalid_confirmation", "pending_link": True}
        record = SessionRecord.from_dict(data.get("record"))
        store.delete_principal_link(tk, pk)  # single use, whatever happens next
        if record is None or (record.tenant_key, record.principal_key) != (tk, pk) or deploy.current().tenant(tk) is None:
            return {"status": "refused", "reason": "invalid_link"}
        record.link_id = secrets.token_urlsafe(16)  # B-3: a new link, a new execution-identity label
        record.created_at = _now()
        store.put(record)
    return {"linked": True}


def session_state(ident: IdentityContext) -> tuple[str, str | None]:
    """``(active|none|service, expires_at)`` for the caller (computed after tier resolution)."""
    if ident.access_tier == "service":
        return "service", None
    if ident.tenant_key is None or ident.principal_key is None:
        return "none", None
    rec = deploy.session_store().get(ident.tenant_key, ident.principal_key)
    if rec is None:
        return "none", None
    if _now() >= rec.bh_expires_at - EXPIRY_MARGIN_SECONDS:
        return "expired", _iso(rec.bh_expires_at)
    return "active", _iso(rec.bh_expires_at)
