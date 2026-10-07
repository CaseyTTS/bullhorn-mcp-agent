"""``get_recruiting_metrics`` (Phase 6 M1, tool 23): period counts and stage ratios, two-tier.

- The legacy ``permissions.check`` runs first (the 5A tier gate; ``workspace_only`` reaches
  this tool through ``TIER2_ALLOWLIST``; a service principal stays denied by ``SERVICE_READ_TOOLS``).
- The tier comes only from the caller's verified identity, never from arguments.
  ``bullhorn_user`` / ``local``: the caller's own client (``server.get_client()``), exact values
  and provenance. ``workspace_only``: ``metrics/tier2.py`` (service identity, aggregate-only).
- Exactly one audit record per call: known metric and period names, then ``{type, length}``
  of each value, the audit-only keyed ``request_hmac``, the ``tier`` and ``execution``
  (``caller`` | ``service``). No value is logged.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Any

from .. import server
from ..crosscutting import audit, permissions
from ..metrics import catalog as metric_catalog
from ..metrics import compute as metric_compute
from ..metrics import tier2 as metric_tier2
from ..reads import records as record_service
from ..reads.records import value_shape
from ..tenant.timeutil import utc_now
from .records import _caller, _error, _identity, _respond

TOOL = "get_recruiting_metrics"
TIER1 = ("bullhorn_user", "local")


def _now() -> _dt.datetime:
    """The tool's clock (patched in tests)."""
    return utc_now()


def audit_args(args: dict[str, Any], tenant: str | None, principal: str | None) -> dict[str, Any]:
    """Names, then types and lengths, then the keyed ``request_hmac`` (5C B-2 / R2-B1)."""
    invalid = 0
    out: dict[str, Any] = {}
    known = metric_catalog.load().metrics
    metrics = args.get("metrics")
    names = [m for m in metrics if type(m) is str and m in known] if type(metrics) is list else []
    invalid += (len(metrics) if type(metrics) is list else 1) - len(names)
    out["metrics"] = names
    period = args.get("period")
    if type(period) is str and period in metric_compute.PERIODS:
        out["period"] = period
    else:
        invalid += 1
    for name in ("date_from", "date_to"):
        out[name] = value_shape(args.get(name))
    out["invalid_items"] = invalid
    out["request_hmac"] = record_service.request_id(tenant, principal, args)
    return out


@server.mcp.tool()
def get_recruiting_metrics(metrics: list[str], date_from: str, date_to: str, period: str = "month") -> str:
    """Recruiting metrics per calendar period: counts and same-period stage ratios.

    Counts come from the tenant's configured activity concepts; a metric whose concept is
    not configured returns its status (e.g. "definition_missing") and no numbers. A count
    that could not be enumerated completely returns "incomplete" (never a partial number).

    Args:
        metrics: 1 to 8 of jobs_created, client_submissions, interviews_scheduled, offers_extended,
            placements_created, submission_to_interview, interview_to_offer, offer_to_placement.
        date_from: Inclusive date (YYYY-MM-DD) in the reporting timezone, on a period boundary.
        date_to: Exclusive date (YYYY-MM-DD) on a period boundary; at most 12 months or 4 quarters.
        period: "month" (default) or "quarter".

    Returns:
        JSON with status, tier, period, reporting_timezone, definition and per-metric cells.
    """
    args: dict[str, Any] = {"metrics": metrics, "date_from": date_from, "date_to": date_to, "period": period}
    start = time.perf_counter()
    entry = audit_args(args, *_caller())
    decision = permissions.check(TOOL, "read")
    if not decision.allowed:
        elapsed = (time.perf_counter() - start) * 1000
        audit.log_invocation(tool=TOOL, args=entry, result_summary="denied", duration_ms=elapsed, success=False)
        return server._permission_denied_message(TOOL, decision)
    tier: str | None = None
    execution: str | None = None
    try:
        ident = _identity()
        tier = ident.access_tier
        if tier == "workspace_only":
            execution = "service"
            result = metric_tier2.run(ident, args, os.environ, _now())
        elif tier in TIER1:
            execution = "caller"
            ctx = record_service.make_context(ident, None, os.environ, _now())
            result = metric_compute.run_tier1(ctx, args, server.get_client)
        else:
            result = {"status": "error", "error": "internal_error", "message": "internal error (tier)"}
    except Exception as exc:  # the MCP boundary: raw exceptions never escape
        result = {"status": "error", "tier": "workspace_only", "error": "internal_error"} if tier == "workspace_only" else _error(exc)
    entry["tier"] = tier
    entry["execution"] = execution
    summary = str(result.get("status"))
    if result.get("status") in ("error", "rate_limited", "unavailable"):
        summary += f": {result.get('error')}"
    audit.log_invocation(
        tool=TOOL,
        args=entry,
        result_summary=summary,
        duration_ms=(time.perf_counter() - start) * 1000,
        success=result.get("status") != "error",
    )
    return _respond(result)
