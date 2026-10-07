"""Expanded recruiting reads (Phase 5C, CT-1): ``find_records`` and ``get_activity`` (tools 21 and 22).

Thin MCP wrappers over ``reads/records.py`` and ``activity/service.py``:

- the legacy ``permissions.check`` runs first; in shared mode the 5A gate denies
  a ``workspace_only`` caller before any argument parsing (G-5: neither tool is
  on the Tier 2 allowlist);
- the caller's identity context and the per-call client (``server.get_client()``)
  are passed explicitly to the services; the services never resolve a client;
- exactly one audit record per call, holding only allowlisted names and the type/length
  of each value, plus the audit-only keyed ``request_hmac`` (triage B-2, R2-B1: no value, no unkeyed
  digest; never in a tool output)
  (Amendment C1-2: no filter value reaches a log);
- no raw exception escapes: Bullhorn failures are ``{"status": "error", ...}``
  with a bounded, redacted message.

Importing this module installs the Amendment C2 query-string scrub filter.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from collections.abc import Callable
from typing import Any

from .. import server
from ..activity import service as activity_service
from ..auth import AuthenticationError
from ..bullhorn import log_scrub
from ..bullhorn.errors import BullhornAPIError
from ..bullhorn.reads import ReadRateLimited
from ..bullhorn.writes import safe_error_text
from ..crosscutting import audit, permissions
from ..identity.principal import current_identity, local_identity
from ..reads import records as record_service
from ..schema.errors import truncate_text
from ..tenant.timeutil import utc_now

log_scrub.install()  # Amendment C2 (idempotent; also installed on import of log_scrub)

MAX_OUTPUT_CHARS = 2_000_000
MAX_LIST_ITEMS = 500
MAX_STRING_CHARS = 5000


def _now() -> _dt.datetime:
    """The tools' clock (patched in tests)."""
    return utc_now()


def _bound(value: Any, depth: int = 0) -> Any:
    if depth > 40:
        return "<nested>"
    if isinstance(value, str):
        return truncate_text(value, MAX_STRING_CHARS)
    if isinstance(value, dict):
        return {truncate_text(str(k), 200): _bound(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_bound(v, depth + 1) for v in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f"... and {len(value) - MAX_LIST_ITEMS} more")
        return items
    return value


def _respond(data: dict[str, Any]) -> str:
    text = server.format_response(_bound(data))
    return text if len(text) <= MAX_OUTPUT_CHARS else truncate_text(text, MAX_OUTPUT_CHARS)


def _identity() -> Any:
    from ..identity import deploy

    return current_identity() if deploy.is_shared() else local_identity()


def _caller() -> tuple[str | None, str | None]:
    """The caller's ``(tenant_key, principal)`` for the audit-only request id (``None`` when unavailable)."""
    try:
        ident = _identity()
    except Exception:
        return None, None
    principal: str | None = ident.principal_key or ident.initiating_principal
    tenant: str | None = ident.tenant_key
    return tenant, principal


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ReadRateLimited):
        return {"status": "error", "error": "rate_limited", "message": safe_error_text(exc)}
    if isinstance(exc, (BullhornAPIError, AuthenticationError)):
        return {"status": "error", "error": "bullhorn_error", "message": safe_error_text(exc)}
    return {"status": "error", "error": "internal_error", "message": f"internal error ({type(exc).__name__})"}


def _invoke(tool: str, args: dict[str, Any], audit_args: dict[str, Any], body: Callable[[], dict[str, Any]]) -> str:
    """Permission check first, then the body, then exactly one audit record. Never raises."""
    start = time.perf_counter()
    decision = permissions.check(tool, "read")
    if not decision.allowed:
        elapsed = (time.perf_counter() - start) * 1000
        audit.log_invocation(tool=tool, args=audit_args, result_summary="denied", duration_ms=elapsed, success=False)
        return server._permission_denied_message(tool, decision)
    try:
        result = body()
    except Exception as exc:  # the MCP boundary: raw exceptions never escape
        result = _error(exc)
    summary = str(result.get("status"))
    if result.get("status") == "error":
        summary += f": {result.get('error')}"
    audit.log_invocation(
        tool=tool,
        args=audit_args,
        result_summary=summary,
        duration_ms=(time.perf_counter() - start) * 1000,
        success=result.get("status") != "error",
    )
    return _respond(result)


@server.mcp.tool()
def find_records(
    entity: str,
    filters: list[dict[str, Any]] = [],  # noqa: B006 - the §3.1 schema default; never mutated
    concept: str | None = None,
    fields: list[str] | None = None,
    sort: dict[str, Any] | None = None,
    include_deleted: bool = False,
    limit: int = 25,
    cursor: str | None = None,
) -> str:
    """Find canonical records of one entity with structured, AND-combined filters.

    Only canonical names are accepted (never Bullhorn field names or query text).
    Every filter is applied exactly; a filter, operator or sort without a verified
    Bullhorn mechanism returns status "unsupported" and nothing is read.

    Args:
        entity: candidate, job, submission, placement, client_corporation, client_contact, appointment or user.
        filters: 0 to 10 objects {"field": <canonical name>, "op": <operator>, "value": ...}.
            Operators by type: id/reference eq, in; string eq, in, is_null; boolean eq;
            integer/number eq, in, gt, gte, lt, lte; datetime gte, lt, is_null (ISO-8601 with offset,
            or a date = midnight in the reporting timezone). "in" takes 1 to 50 values.
        concept: Optional tenant concept (e.g. client_submission, interview_completed, offer_extended):
            only records whose current value matches the tenant's mapping.
        fields: 1 to 30 canonical field names to return (default: a standard set).
        sort: {"field", "direction"}; no sort is verified yet (returns "unsupported").
        include_deleted: Include deleted records (default false; not verified, returns "unsupported").
        limit: 1 to 100 (default 25).
        cursor: The next_cursor of a previous call with the same arguments.

    Returns:
        JSON with status, entity, records, count, truncated, complete, next_cursor,
        consistency, reporting_timezone, provenance and warnings.
    """
    args: dict[str, Any] = {
        "entity": entity,
        "filters": filters,
        "concept": concept,
        "fields": fields,
        "sort": sort,
        "include_deleted": include_deleted,
        "limit": limit,
        "cursor": cursor,
    }

    def body() -> dict[str, Any]:
        ctx = record_service.make_context(_identity(), server.get_client(), os.environ, _now())
        return record_service.find_records(ctx, args)

    return _invoke("find_records", args, record_service.audit_args(args, *_caller()), body)


@server.mcp.tool()
def get_activity(
    concepts: list[str],
    scope_type: str | None = None,
    scope_id: int | None = None,
    recruiter_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> str:
    """Read recruiting activity events, one independent block per concept.

    Needs scope_type + scope_id, or both date_from and date_to (an unscoped range
    is at most 366 days). Business concepts (client_submission, interviews, offers)
    come only from the tenant's configured mappings; a missing definition returns
    "definition_missing", an unverified mechanism "unsupported" (never guessed).

    Args:
        concepts: 1 to 8 concept IDs (e.g. job_created, submission_created, client_submission,
            interview_scheduled, interview_completed, placement_created, note_created).
        scope_type: candidate, job, client_corporation, client_contact, placement or submission.
        scope_id: The record id for scope_type (required with it).
        recruiter_id: A user id; filters on each concept's attribution field.
        date_from: Inclusive ISO-8601 date or datetime with offset.
        date_to: Exclusive ISO-8601 date or datetime with offset.
        limit: 1 to 200 events per concept (default 50).
        cursor: The next_cursor of a previous call with the same arguments.

    Returns:
        JSON with status, concepts (per-concept blocks), next_cursor,
        reporting_timezone, provenance and warnings.
    """
    args: dict[str, Any] = {
        "concepts": concepts,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "recruiter_id": recruiter_id,
        "date_from": date_from,
        "date_to": date_to,
        "limit": limit,
        "cursor": cursor,
    }
    audit_args = activity_service.audit_args(args, *_caller())

    def body() -> dict[str, Any]:
        ctx = record_service.make_context(_identity(), server.get_client(), os.environ, _now())
        return activity_service.get_activity(ctx, args)

    return _invoke("get_activity", args, audit_args, body)
