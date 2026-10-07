"""Caller principal and identity context (Phase 5A, D-5A-2 / D-5A-9, Amendments A2-1 and A3-1).

In ``shared`` mode the principal comes **only** from the verified bearer token of
the current HTTP request (``get_access_token()``, a per-request contextvar;
HV-M3/HV-M6). ``principal_key = sha256(json([issuer, subject]))``. A missing
token, subject or issuer, an issuer that is not allowed, or an unresolvable
tenant fails closed with ``IdentityRequired`` (``identity_required``).
``client_id`` and display claims are never an identity.

``access_tier`` (A2-1) is computed here at request time, never from tool
arguments or client-chosen claims:

=====================  ==================
caller                 access_tier
=====================  ==================
``local`` mode         ``local``
service principal      ``service``
valid linked session   ``bullhorn_user``
anyone else            ``workspace_only``
=====================  ==================

Nothing is cached across requests: every call recomputes from the current token.
"""

from __future__ import annotations

import getpass
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from . import deploy
from .roles import principal_key_for

TIERS = ("bullhorn_user", "workspace_only", "service", "local")
TIER2_ALLOWLIST = frozenset({"bullhorn_session", "setup_status", "get_recruiting_metrics"})  # A2-2; Phase 6 M1 (D-5-24)
HV_C5_VERIFIED = False  # no documented current-user CorporateUser id (PHASE5A_HV_VERIFICATION.md, HV-C5)
MAX_DISPLAY_CHARS = 120
_SAFE_DISPLAY_RE = re.compile(r"[^\w .@+\-'()]", re.UNICODE)


class IdentityRequired(Exception):
    """No acceptable caller identity. ``reason`` is a fixed code, never a claim value."""

    def __init__(self, reason: str = "identity_required") -> None:
        self.reason = reason
        super().__init__("identity_required")


@dataclass(frozen=True)
class IdentityContext:
    initiating_principal: str
    principal_display: str
    tenant_key: str | None
    executing_bullhorn_identity: str | None
    mode: str  # "user" | "service" | "local"
    access_tier: str
    service_identity: str | None = None
    tenant: Any = None  # deploy.TenantConfig in shared mode

    @property
    def principal_key(self) -> str | None:
        return None if self.mode == "local" else self.initiating_principal

    def triple(self) -> dict[str, str | None]:
        return {
            "initiating_principal": self.initiating_principal,
            "tenant_key": self.tenant_key,
            "executing_bullhorn_identity": self.executing_bullhorn_identity,
        }


def _display(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C")
    return _SAFE_DISPLAY_RE.sub("", text).strip()[:MAX_DISPLAY_CHARS]


def link_label(tenant_key: str, principal_key: str, link_id: str) -> str:
    """The execution-identity label while HV-C5 is unresolved (5A triage B-3): one per completed link.

    A logout followed by a re-link (possibly to another Bullhorn account) mints a new ``link_id``,
    so the label changes and a preview made under the old link can no longer be confirmed.
    """
    return "bh-link:" + hashlib.sha256(f"{tenant_key}|{principal_key}|{link_id}".encode("ascii")).hexdigest()


def service_label(tenant_key: str) -> str:
    return "bh-service:" + hashlib.sha256(f"{tenant_key}|service".encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Principal:
    key: str
    display: str
    tenant: Any


def principal_from_token(token: Any, dep: deploy.Deployment) -> Principal:
    """Resolve the caller from a *verified* ``AccessToken``. Raises ``IdentityRequired``."""
    if token is None or dep.auth is None:
        raise IdentityRequired("identity_required")
    subject = getattr(token, "subject", None)
    claims = getattr(token, "claims", None) or {}
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject or not isinstance(issuer, str) or not issuer:
        raise IdentityRequired("identity_required")
    if issuer not in dep.auth.allowed_issuers:
        raise IdentityRequired("identity_required")
    tenant: deploy.TenantConfig | None
    if dep.auth.tenant_claim is None:
        if len(dep.tenants) != 1:
            raise IdentityRequired("identity_required")
        tenant = next(iter(dep.tenants.values()))
    else:
        alias = claims.get(dep.auth.tenant_claim)
        tenant = dep.tenant_by_alias(alias) if isinstance(alias, str) else None
        if tenant is None:
            raise IdentityRequired("identity_required")
    display = ""
    for name in dep.auth.display_claims:
        display = _display(claims.get(name))
        if display:
            break
    key = principal_key_for(issuer, subject)
    return Principal(key=key, display=display or f"principal:{key[:12]}", tenant=tenant)


def local_identity() -> IdentityContext:
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return IdentityContext(
        initiating_principal=f"local:{_display(user) or 'unknown'}",
        principal_display=f"local:{_display(user) or 'unknown'}",
        tenant_key=None,
        executing_bullhorn_identity=None,
        mode="local",
        access_tier="local",
    )


def current_identity() -> IdentityContext:
    """The identity context of the current call. Raises ``IdentityRequired`` in shared mode."""
    dep = deploy.current()
    if not dep.shared:
        return local_identity()
    p = principal_from_token(get_access_token(), dep)
    tk = p.tenant.tenant_key
    if p.tenant.roles.is_service(p.key):  # B-4: roles are per tenant
        return IdentityContext(
            initiating_principal=p.key,
            principal_display=p.display,
            tenant_key=tk,
            executing_bullhorn_identity=service_label(tk),
            mode="service",
            access_tier="service",
            service_identity=service_label(tk),
            tenant=p.tenant,
        )
    from . import sessions  # local import: sessions imports this module

    record = sessions.load_valid_session(p.tenant, p.key)
    linked = record is not None and bool(record.link_id)  # a session without a completed link is not usable
    executing = None
    if linked:
        assert record is not None and record.link_id
        label = link_label(tk, p.key, record.link_id)
        # If HV-C5 is ever verified, both the Bullhorn user and the link are recorded and compared.
        executing = f"bh-user:{record.bullhorn_user_ref}|{label}" if HV_C5_VERIFIED and record.bullhorn_user_ref else label
    return IdentityContext(
        initiating_principal=p.key,
        principal_display=p.display,
        tenant_key=tk,
        executing_bullhorn_identity=executing,
        mode="user",
        access_tier="bullhorn_user" if linked else "workspace_only",
        tenant=p.tenant,
    )


def current_tier() -> str | None:
    """The caller's ``access_tier`` (``None`` when no acceptable identity). The single tier check (L-8)."""
    try:
        return current_identity().access_tier
    except IdentityRequired:
        return None


def has_bullhorn_access(tier: str | None) -> bool:
    """Does the tier carry a usable Bullhorn session (linked user or service)?"""
    return tier in ("bullhorn_user", "service")


def shared_identity() -> IdentityContext | None:
    """``None`` in local mode; the shared-mode identity otherwise (raises ``IdentityRequired``)."""
    if not deploy.is_shared():
        return None
    return current_identity()
