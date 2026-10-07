"""Tier 2 (``workspace_only``) execution of ``get_recruiting_metrics`` (Phase 6 M1 §3, §4).

This is the **only** module that resolves the tenant's service identity
(``identity.sessions.service_client``) for metrics. The tenant (and with it ``k``, the
setup store and the service session) comes only from the caller's verified identity
(A3-1). The output is built and validated by ``tier2_policy``; every failure is a generic
``error`` / ``rate_limited`` / ``unavailable`` with a fixed code (P-5).
"""

from __future__ import annotations

import datetime as _dt
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
from ..reads.records import Outcome, check_setup, load_tenant, make_context
from . import catalog as catalog_mod
from . import compute
from . import tier2_policy as policy


def run(ident: IdentityContext, args: Mapping[str, Any], env: Mapping[str, str], now: _dt.datetime) -> dict[str, Any]:
    """Never raises; returns a policy-validated aggregate or a generic error."""
    if ident.access_tier != policy.TIER:
        return policy.generic("unavailable", "internal_error")
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
