"""Tier 2 (``workspace_only``) execution of ``get_recruiting_metrics`` (Phase 6 M1 §3, §4).

This is the **only** module that resolves the tenant's service identity
(``identity.sessions.service_client``) for metrics. The tenant (and with it ``k``, the
setup store and the service session) comes only from the caller's verified identity
(A3-1). The output is built and validated by ``tier2_policy``; every failure is a generic
``error`` / ``rate_limited`` / ``unavailable`` with a fixed code (P-5).

Phase 6 M2 (D-6-2..D-6-4): Tier 2 requires the per-tenant analytics grant
(``tenants[tk].roles.analytics_viewers`` in the admin config, evaluated against the selected
tenant only). Without it the caller is denied before any service-session resolution or
Bullhorn call. Being unlinked, expired, pending or logged out never implies the grant.
P6-4: at most ``MAX_CONCURRENT_PER_TENANT`` Tier 2 computations run at once per tenant
(non-blocking); an excess call is ``rate_limited`` with zero calls.
"""

from __future__ import annotations

import datetime as _dt
import threading
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import Any

from ..activity import service as activity_service
from ..auth import AuthenticationError
from ..bullhorn.errors import BullhornAPIError
from ..bullhorn.reads import ReadRateLimited
from ..identity import sessions
from ..identity.principal import IdentityContext, service_label
from ..identity.roles import Roles
from ..reads.records import Outcome, check_setup, load_tenant, make_context
from . import catalog as catalog_mod
from . import compute
from . import tier2_policy as policy


MAX_CONCURRENT_PER_TENANT = 2  # P6-4
DENIED: dict[str, Any] = {"status": "denied", "tier": policy.TIER, "error": "analytics_permission_required"}

_slots_lock = threading.Lock()
_slots: dict[str, threading.BoundedSemaphore] = {}


def _slot(tenant_key: str) -> threading.BoundedSemaphore:
    with _slots_lock:
        sem = _slots.get(tenant_key)
        if sem is None:
            sem = _slots[tenant_key] = threading.BoundedSemaphore(MAX_CONCURRENT_PER_TENANT)
        return sem


def has_analytics_grant(ident: IdentityContext) -> bool:
    """The analytics grant (D-6-2/D-6-4): server-side admin config of the selected tenant only."""
    tenant = ident.tenant
    if tenant is None or ident.tenant_key is None or getattr(tenant, "tenant_key", None) != ident.tenant_key:
        return False
    roles = getattr(tenant, "roles", None)
    return isinstance(roles, Roles) and roles.is_analytics_viewer(ident.principal_key)


def run(ident: IdentityContext, args: Mapping[str, Any], env: Mapping[str, str], now: _dt.datetime) -> dict[str, Any]:
    """Never raises; returns a policy-validated aggregate or a generic error."""
    if ident.access_tier != policy.TIER:
        return policy.generic("unavailable", "internal_error")
    try:
        if not has_analytics_grant(ident):
            return dict(DENIED)
        assert ident.tenant_key is not None
        sem = _slot(ident.tenant_key)
    except Exception:
        return policy.generic("error", "internal_error")
    if not sem.acquire(blocking=False):
        return policy.generic("rate_limited", "rate_limited")
    try:
        return _run(ident, args, env, now)
    except Outcome:
        return policy.generic("unavailable", "setup_required")
    except ReadRateLimited:
        return policy.generic("rate_limited", "rate_limited")
    except (BullhornAPIError, AuthenticationError):
        return policy.generic("error", "bullhorn_error")
    except Exception:
        return policy.generic("error", "internal_error")
    finally:
        sem.release()


def _run(ident: IdentityContext, args: Mapping[str, Any], env: Mapping[str, str], now: _dt.datetime) -> dict[str, Any]:
    ctx = make_context(ident, None, env, now)
    load_tenant(ctx)  # the caller's tenant store (A3-1); local files only
    cat = catalog_mod.load()
    req = compute.validate(args, cat)
    if isinstance(req, list):
        return policy.rejected(req)
    k = policy.cohort(ctx.profile)
    tenant = ident.tenant
    if tenant is None or ident.tenant_key is None or getattr(tenant, "service", None) is None:
        return policy.generic("unavailable", "service_identity_not_configured")
    try:
        client = sessions.service_client(tenant)
    except sessions.BullhornSessionRequired:
        return policy.generic("unavailable", "service_identity_not_configured")
    ctx.client = client
    label = service_label(ident.tenant_key)
    ctx.ident = replace(ident, executing_bullhorn_identity=label, service_identity=label)
    check_setup(ctx, execution_tier="service")  # M1-A1: row 1 is held by the resolved service session
    counts = compute.compute(ctx, req, cat, partial(activity_service.get_activity, execution_tier="service"))
    doc = policy.build(req, cat, counts, k, ctx.timezone, now=now)
    try:
        policy.validate_output(doc, cat)
    except policy.PolicyViolation:
        return policy.generic("error", "policy_violation")
    return doc
