"""Notes / activity MCP tools (Phase 4B, CT-1): ``get_notes``, ``create_note``, ``confirm_write``.

- ``create_note`` previews by default (``dry_run=True``). A write needs either
  ``confirm_write`` or ``BULLHORN_NOTE_CREATE_MODE=direct``.
- Every tool calls the legacy ``permissions.check`` first and emits exactly one
  audit record per call (``create_note`` audits the comment hash and length,
  never the text).
- No raw exception escapes a tool: every failure is a bounded, redacted
  ``ERROR:`` string.
- Text longer than 600 characters (a note body, the previewed comments) is
  returned as ``{"text_chunks": [...], "length": n}``, so that no output line
  exceeds 10,000 characters. A note body is cut at 10,000 characters, with
  ``"truncated": true``.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from collections.abc import Callable
from typing import Any

from .. import server
from ..activity.events import sha256_hex
from ..auth import AuthenticationError
from ..bullhorn.errors import BullhornAPIError
from ..bullhorn.writes import EntityWriter, safe_error_text
from ..crosscutting import audit, permissions
from ..notes import reads as note_reads
from ..notes.action_types import valid_set
from ..schema.errors import SchemaError, truncate_text
from ..tenant.profile_v2 import rest_url_fingerprint
from ..tenant.state import ConnectionCheck, compute_setup_state, effective_states, require_capability
from ..tenant.store import store_from_env
from ..tenant.timeutil import utc_now
from ..writes import pipeline

CHUNK_CHARS = 600
MAX_TEXT_CHARS = 10_000
MAX_LIST_ITEMS = 500
MAX_OUTPUT_CHARS = 2_000_000


def _now() -> _dt.datetime:
    """The tools' clock (patched in tests)."""
    return utc_now()


def _shape(value: Any, depth: int = 0) -> Any:
    """Bound the output: long text becomes chunks, lists are capped, depth is capped."""
    if depth > 40:
        return "<nested>"
    if isinstance(value, str):
        if len(value) <= CHUNK_CHARS:
            return value
        text = value[:MAX_TEXT_CHARS]
        out: dict[str, Any] = {
            "text_chunks": [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)],
            "length": len(value),
        }
        if len(value) > MAX_TEXT_CHARS:
            out["truncated"] = True
        return out
    if isinstance(value, dict):
        return {truncate_text(str(k), 200): _shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_shape(v, depth + 1) for v in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f"... and {len(value) - MAX_LIST_ITEMS} more")
        return items
    return value


def _respond(data: dict[str, Any]) -> str:
    text = server.format_response(_shape(data))
    return text if len(text) <= MAX_OUTPUT_CHARS else truncate_text(text, MAX_OUTPUT_CHARS)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (AuthenticationError, BullhornAPIError, ValueError)):
        return safe_error_text(exc)
    if isinstance(exc, SchemaError):
        return safe_error_text(f"{type(exc).__name__}: " + " | ".join(str(exc).splitlines()[:20]), 2000)
    return f"internal error ({type(exc).__name__})"


def _invoke(tool: str, operation: str, args: dict[str, Any], body: Callable[[], tuple[str, str]]) -> str:
    """Legacy permission check first, then the body, then exactly one audit record. Never raises."""
    start = time.perf_counter()
    decision = permissions.check(tool, operation)
    if not decision.allowed:
        audit.log_invocation(tool=tool, args=args, result_summary="denied", duration_ms=(time.perf_counter() - start) * 1000, success=False)
        return server._permission_denied_message(tool, decision)
    try:
        result, summary = body()
        success = True
    except Exception as exc:  # the MCP boundary: raw exceptions never escape
        message = _error_text(exc)
        result = f"ERROR: {message}"
        summary = f"error: {truncate_text(message.splitlines()[0] if message else '', 200)}"
        success = False
    audit.log_invocation(tool=tool, args=args, result_summary=summary, duration_ms=(time.perf_counter() - start) * 1000, success=success)
    return result


def _connection() -> ConnectionCheck:
    try:
        session = server.get_client().auth.session
        return ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint(session.rest_url))
    except Exception as exc:
        return ConnectionCheck(ok=False, error=safe_error_text(exc))


def _context() -> pipeline.WriteContext:
    client = server.get_client()
    return pipeline.WriteContext(client=client, env=os.environ, now=_now(), connection=_connection, writer=EntityWriter(client))


def _summary(result: dict[str, Any]) -> str:
    text = f"{result.get('status')}: correlation_id={result.get('correlation_id')}"
    if result.get("record_id") is not None:
        text += f" record_id={result['record_id']}"
    return text


def _comment_digest(comments: Any) -> Any:
    if isinstance(comments, str):
        return {"sha256": sha256_hex(comments), "length": len(comments)}
    return f"<{type(comments).__name__}>"


# ---------------------------------------------------------------------- #
# Tools
# ---------------------------------------------------------------------- #


@server.mcp.tool()
def get_notes(
    target_type: str | None = None,
    target_id: int | None = None,
    candidate_id: int | None = None,
    job_id: int | None = None,
    client_corporation_id: int | None = None,
    client_contact_id: int | None = None,
    placement_id: int | None = None,
    submission_id: int | None = None,
    action_type: str | None = None,
    author: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    start: int = 0,
) -> str:
    """Read canonical notes (and their note_created events) for one record.

    Exactly one primary scope is required: target_type + target_id, or one of
    candidate_id, job_id, client_corporation_id, client_contact_id, placement_id,
    submission_id. Only verified Bullhorn mechanisms are used; a scope or filter
    without one returns status "unsupported_filter" with the supported filters
    (it is never approximated).

    Args:
        target_type: "candidate", "job", "client_contact", "client_corporation", "placement" or "submission".
        target_id: The record id for target_type.
        candidate_id: Primary scope: a candidate id.
        job_id: Primary scope: a job id.
        client_corporation_id: Primary scope: a client corporation id.
        client_contact_id: Primary scope: a client contact id.
        placement_id: Primary scope: a placement id.
        submission_id: Primary scope: a submission id.
        action_type: A tenant note action value (validated; never substituted).
        author: A Bullhorn user id or name.
        date_from: Inclusive ISO-8601 date or datetime with offset (a date means midnight in the reporting timezone).
        date_to: Exclusive ISO-8601 date or datetime with offset.
        include_deleted: Include deleted notes (default false).
        limit: Page size, 1 to 50 (default 20).
        start: Offset of the page (default 0).

    Returns:
        JSON with status, notes, events, next_start, truncated and warnings.
    """
    args: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
        "candidate_id": candidate_id,
        "job_id": job_id,
        "client_corporation_id": client_corporation_id,
        "client_contact_id": client_contact_id,
        "placement_id": placement_id,
        "submission_id": submission_id,
        "action_type": action_type,
        "author": author,
        "date_from": date_from,
        "date_to": date_to,
        "include_deleted": include_deleted,
        "limit": limit,
        "start": start,
    }

    def body() -> tuple[str, str]:
        env = os.environ
        now = _now()
        profile = None
        states: dict[str, str] = {}
        timezone_name = "UTC"
        try:
            store = store_from_env(env)
            active = store.active_version() if store is not None else None
            if store is not None and active is not None:
                profile = store.read_version(active)
                states, _ = effective_states(profile, store.read_discovery())
                timezone_name = profile.settings.reporting_timezone
        except SchemaError:
            profile = None
        query, errors = note_reads.validate_basic(args, timezone_name)
        if query is None:
            return _respond({"status": "rejected", "errors": errors}), "rejected"
        gate = require_capability("notes.read", compute_setup_state(env, None, now=now))
        if not gate.ok:
            return _respond({"status": "denied", "missing_requirements": list(gate.missing)}), "denied"
        aset = valid_set(profile, states)
        action_errors = note_reads.validate_action(query, aset)
        if action_errors:
            return _respond({"status": "rejected", "errors": action_errors}), "rejected"
        missing = note_reads.unsupported(query)
        if missing:
            result = {"status": "unsupported_filter", "unsupported": missing, "supported_filters": list(note_reads.SUPPORTED_FILTERS)}
            return _respond(result), "unsupported_filter"
        page = note_reads.fetch(EntityWriter(server.get_client()), query, aset.semantics)
        return _respond(page), f"ok: {len(page['notes'])} notes"

    return _invoke("get_notes", "read", args, body)


@server.mcp.tool()
def create_note(
    target_type: str,
    target_id: int,
    action_type: str,
    comments: str,
    associations: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = True,
) -> str:
    """Create a Bullhorn note through the safe-write pipeline (preview by default).

    With dry_run=True (the default) nothing is written to Bullhorn: the result
    holds operation_id and preview_hash. Execute exactly that preview with
    confirm_write(operation_id, preview_hash, "approve"). dry_run=False is
    accepted only when BULLHORN_NOTE_CREATE_MODE=direct. Writing requires the
    note.create scope (BULLHORN_ENABLED_WRITE_SCOPES) and BULLHORN_MCP_ACTOR.

    Args:
        target_type: The person the note is about: "candidate" or "client_contact".
        target_id: The record id of the target.
        action_type: A tenant note action value (exact; never substituted).
        comments: The note text (1 to 10,000 characters; stored unchanged).
        associations: Additional records, each {"type": ..., "id": ...} (at most 10).
        idempotency_key: Optional caller key (kept 7 days) that prevents a duplicate write.
        dry_run: Preview only (default true).

    Returns:
        JSON with operation, status, entity_type, record_id, associations, targets,
        activity, correlation_id, and operation_id/preview_hash for a preview.
    """
    args = {
        "target_type": target_type,
        "target_id": target_id,
        "action_type": action_type,
        "comments": _comment_digest(comments),
        "associations": associations,
        "idempotency_key": idempotency_key,
        "dry_run": dry_run,
    }

    def body() -> tuple[str, str]:
        result = pipeline.create_note(
            _context(), target_type, target_id, action_type, comments, associations, idempotency_key, dry_run
        )
        return _respond(result), _summary(result)

    return _invoke("create_note", "write", args, body)


@server.mcp.tool()
def confirm_write(operation_id: str, preview_hash: str, decision: str) -> str:
    """Approve or reject a previewed write. The approver comes from BULLHORN_MCP_ACTOR.

    The write runs only if the preview_hash matches, the preview has not expired
    or been used, the actor is allowed, the scope and setup gate still hold, and
    nothing previewed has changed (otherwise: refused, e.g. stale_preview).

    Args:
        operation_id: The operation_id returned by the preview.
        preview_hash: The preview_hash returned by the preview.
        decision: "approve" or "reject".

    Returns:
        JSON with the normalized write result (status committed, partially_committed,
        failed, failed_orphan, rejected or refused).
    """
    args = {"operation_id": operation_id, "preview_hash": preview_hash, "decision": decision}

    def body() -> tuple[str, str]:
        result = pipeline.confirm_write(_context(), operation_id, preview_hash, decision)
        return _respond(result), _summary(result)

    return _invoke("confirm_write", "write", args, body)
