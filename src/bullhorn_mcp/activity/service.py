"""The ``get_activity`` service (Phase 5C, §3.2).

Each requested concept is evaluated independently and returns its own block
``{status, events, truncated, complete, unsupported, missing_requirements}``;
one concept failing never widens or alters another. Concept availability goes
through ``tenant/capabilities.py`` (``activity.<concept>`` state gate plus
``concept_requirements``). There is no cross-concept merge (the timeline is
Phase 6, D-5-5); within a concept events are in server order (HV-Q4).

``note_created`` delegates to the verified scopes of ``notes/reads.py`` (job,
placement); there is no second note-reading path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..bullhorn.query_syntax import render_fields
from ..bullhorn.writes import EntityWriter
from ..notes import reads as note_reads
from ..notes.action_types import valid_set
from ..reads import cursor as cursors
from ..reads.records import (
    MAX_PAGES,
    MAX_WARNINGS,
    OFFSET_WARNING,
    Outcome,
    ReadContext,
    build_where,
    check_setup,
    request_id,
    value_shape,
    fetch,
    load_tenant,
    normalized_hash,
    rejected,
)
from ..schema.query_builder import Problem
from ..tenant.capabilities import ACTIVITY_CONCEPTS, concept_requirements, evaluate_capability
from ..tenant.timeutil import FilterError, parse_bound, parse_utc, resolve_zone
from . import derivers
from .derivers import Filters

MAX_CONCEPTS = 8
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_ID = 2**63 - 1
MAX_UNSCOPED_DAYS = 366
DAY_MS = 86_400_000
NOTE_SCOPES = tuple(note_reads.SUPPORTED_SCOPES)  # HV-B5: the verified to-many note reads


VALUE_ARGS = ("scope_id", "recruiter_id", "date_from", "date_to", "cursor")


def audit_args(args: Mapping[str, Any], tenant: str | None, principal: str | None) -> dict[str, Any]:
    """Audit-safe ``get_activity`` arguments (triage B-2): known concept and scope names only;
    every value becomes ``{type, length}``; anything invalid is only counted."""
    invalid = 0
    out: dict[str, Any] = {}
    concepts = args.get("concepts")
    names = [c for c in concepts if type(c) is str and c in ACTIVITY_CONCEPTS] if type(concepts) is list else []
    invalid += (len(concepts) if type(concepts) is list else 1) - len(names)
    out["concepts"] = names
    scope_type = args.get("scope_type")
    if scope_type is not None:
        if type(scope_type) is str and scope_type in derivers.SCOPE_TYPES:
            out["scope_type"] = scope_type
        else:
            invalid += 1
    for name in VALUE_ARGS:
        if args.get(name) is not None:
            out[name] = value_shape(args[name])
    limit = args.get("limit", DEFAULT_LIMIT)
    if type(limit) is int and 1 <= limit <= MAX_LIMIT:
        out["limit"] = limit
    else:
        invalid += 1
    out["invalid_items"] = invalid
    out["request_hmac"] = request_id(tenant, principal, args)
    return out


def _is_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ID


def _block(name: str, **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "status": name,
        "events": [],
        "truncated": False,
        "complete": False,
        "unsupported": [],
        "missing_requirements": [],
    }
    block.update(extra)
    return block


def validate(args: Mapping[str, Any], timezone_name: str) -> tuple[Filters | None, list[Problem]]:
    errors: list[Problem] = []
    concepts = args.get("concepts")
    if type(concepts) is not list or not 1 <= len(concepts) <= MAX_CONCEPTS:
        errors.append(Problem("invalid_concept", "concepts", f"concepts must be a list of 1 to {MAX_CONCEPTS} concept IDs"))
    else:
        seen: set[str] = set()
        for c in concepts:
            if type(c) is not str or c not in ACTIVITY_CONCEPTS:
                errors.append(Problem("invalid_concept", c if type(c) is str and len(c) <= 64 else "concepts", "not a known concept ID"))
            elif c in seen:
                errors.append(Problem("invalid_concept", c, "duplicate concept"))
            else:
                seen.add(c)
    scope_type, scope_id = args.get("scope_type"), args.get("scope_id")
    if scope_type is not None and (type(scope_type) is not str or scope_type not in derivers.SCOPE_TYPES):
        errors.append(Problem("invalid_value", "scope_type", f"scope_type must be one of {list(derivers.SCOPE_TYPES)}"))
    if (scope_type is None) != (scope_id is None):
        errors.append(Problem("invalid_value", "scope_id", "scope_id is required exactly when scope_type is given"))
    elif scope_id is not None and not _is_id(scope_id):
        errors.append(Problem("invalid_value", "scope_id", "scope_id must be an int from 1 to 2^63-1"))
    recruiter = args.get("recruiter_id")
    if recruiter is not None and not _is_id(recruiter):
        errors.append(Problem("invalid_value", "recruiter_id", "recruiter_id must be an int from 1 to 2^63-1"))
    bounds: dict[str, int | None] = {"date_from": None, "date_to": None}
    for name in bounds:
        value = args.get(name)
        if value is None:
            continue
        try:
            bounds[name] = parse_bound(value, name, timezone_name)
        except FilterError as exc:
            errors.append(Problem("invalid_value", name, str(exc)))
    lo, hi = bounds["date_from"], bounds["date_to"]
    if lo is not None and hi is not None and lo >= hi:
        errors.append(Problem("invalid_value", "date_from", "date_from must be earlier than date_to (the range is [date_from, date_to))"))
    if scope_type is None and (lo is None or hi is None) and not any(e.item in ("date_from", "date_to") for e in errors):
        errors.append(Problem("invalid_value", "scope", "scope_type + scope_id, or both date_from and date_to, are required"))
    if scope_type is None and lo is not None and hi is not None and hi - lo > MAX_UNSCOPED_DAYS * DAY_MS:
        errors.append(Problem("range_too_wide", "date_to", f"an unscoped range may not exceed {MAX_UNSCOPED_DAYS} days"))
    limit = args.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        errors.append(Problem("invalid_value", "limit", f"limit must be an int from 1 to {MAX_LIMIT}"))
    cur = args.get("cursor")
    if cur is not None and type(cur) is not str:
        errors.append(Problem("invalid_cursor", "cursor", "invalid_cursor"))
    if errors:
        return None, errors
    return (
        Filters(
            scope_type=scope_type,
            scope_id=scope_id,
            recruiter_id=recruiter,
            date_from_ms=lo,
            date_to_ms=hi,
            now_ms=0,
        ),
        [],
    )


def get_activity(
    ctx: ReadContext, args: Mapping[str, Any], *, max_pages: int = MAX_PAGES, execution_tier: str | None = None
) -> dict[str, Any]:
    """``max_pages`` (Phase 6 M1): the per-concept page budget of one call; the tool always uses the default.
    ``execution_tier`` (M1-A1): passed to ``check_setup``; ``"service"`` only from ``metrics/tier2.py``."""
    try:
        return _get_activity(ctx, args, max_pages, execution_tier)
    except Outcome as out:
        return out.result


def _get_activity(
    ctx: ReadContext, args: Mapping[str, Any], max_pages: int = MAX_PAGES, execution_tier: str | None = None
) -> dict[str, Any]:
    load_tenant(ctx)
    flt, errors = validate(args, ctx.timezone)
    if flt is None:
        return rejected(errors)
    flt = Filters(flt.scope_type, flt.scope_id, flt.recruiter_id, flt.date_from_ms, flt.date_to_ms, int(ctx.now.timestamp() * 1000))
    concepts: list[str] = list(args["concepts"])
    limit: int = args.get("limit", DEFAULT_LIMIT)
    req_hash = normalized_hash(ctx, args)
    binding = ctx.binding(req_hash)
    positions: dict[str, Any] | None = None
    if args.get("cursor") is not None:
        try:
            mode, pos = cursors.decode(args["cursor"], binding, int(ctx.now.timestamp()))
        except cursors.InvalidCursor:
            return rejected([Problem("invalid_cursor", "cursor", "invalid_cursor")])
        if mode != "activity" or not isinstance(pos, dict) or set(pos) != set(concepts):
            return rejected([Problem("invalid_cursor", "cursor", "invalid_cursor")])
        positions = pos
    check_setup(ctx, execution_tier=execution_tier)

    blocks: dict[str, dict[str, Any]] = {}
    next_positions: dict[str, Any] = {}
    warnings = list(ctx.warnings)
    for concept in concepts:
        position = positions.get(concept) if positions is not None else [0, 0]
        if position is None:  # finished on an earlier page
            blocks[concept] = _block("ok", complete=True)
            next_positions[concept] = None
            continue
        if not (isinstance(position, list) and len(position) == 2 and all(type(p) is int and p >= 0 for p in position)):
            return rejected([Problem("invalid_cursor", "cursor", "invalid_cursor")])
        block, nxt, block_warnings = _concept(ctx, concept, flt, limit, position, max_pages)
        blocks[concept] = block
        next_positions[concept] = nxt
        warnings.extend(w for w in block_warnings if w not in warnings)
    next_cursor = None
    if any(v is not None for v in next_positions.values()):
        next_cursor = cursors.encode(binding, "activity", next_positions, int(ctx.now.timestamp()))
    return {
        "status": "ok",
        "concepts": blocks,
        "next_cursor": next_cursor,
        "reporting_timezone": ctx.timezone,
        "provenance": ctx.provenance(req_hash),
        "warnings": warnings[:MAX_WARNINGS],
    }


def _concept(
    ctx: ReadContext, concept: str, flt: Filters, limit: int, position: list[int], max_pages: int = MAX_PAGES
) -> tuple[dict[str, Any], Any, list[str]]:
    """One concept block, its next position (``None`` when finished) and its warnings."""
    if concept in derivers.UNSUPPORTED_CONCEPTS:
        reason, hv = derivers.UNSUPPORTED_CONCEPTS[concept]
        return _block("unsupported", unsupported=[Problem("unsupported_concept", concept, reason, hv).unsupported()]), None, []
    gate = evaluate_capability(f"activity.{concept}", ctx.profile, ctx.states, ctx.state)
    if not gate.ok:
        if any(m.startswith("state:") for m in gate.missing):
            return _block("setup_revalidation_required", missing_requirements=[f"state:{ctx.state}"]), None, []
        return _block("definition_missing", missing_requirements=list(gate.missing)), None, []
    if concept == "note_created":
        return _notes(ctx, flt, limit, position)
    missing = concept_requirements(concept, ctx.profile, ctx.states)
    if missing:
        return _block("definition_missing", missing_requirements=list(missing)), None, []
    plan = derivers.plan(ctx, concept, flt)
    if plan.unsupported:
        return _block("unsupported", unsupported=[p.unsupported() for p in plan.unsupported]), None, []
    if plan.missing:
        return _block("definition_missing", missing_requirements=list(dict.fromkeys(plan.missing))), None, []
    wheres: list[str] = []
    for src in plan.sources:
        where = build_where(ctx, src.clauses)
        if isinstance(where, Problem):
            return _block("unsupported", unsupported=[Problem(where.code, concept, where.reason, where.hv).unsupported()]), None, []
        wheres.append(where)
    # Read the sources in order, from the cursor position, within the per-concept page cap.
    events: list[dict[str, Any]] = []
    index, start = position
    truncated = False
    nxt: Any = None
    pages_left = max_pages
    deleted = 0
    while index < len(plan.sources) and pages_left > 0:
        src = plan.sources[index]
        want = limit - len(events)
        page = fetch(
            ctx, src.ent, wheres[index], render_fields(src.fields()), start, want,
            derivers.keep_row(concept, src), pages_left,
        )
        pages_left -= page.requests
        deleted += page.deleted
        for row in page.rows:
            evt = derivers.event(ctx, concept, src, row)
            if evt is not None:
                events.append(evt)
        if page.truncated and len(page.rows) < want and pages_left > 0 and page.next_start is not None:
            start = page.next_start  # fetch's own page cap (<= MAX_PAGES) with budget left: continue (max_pages > MAX_PAGES only)
            continue
        if page.truncated:
            truncated = True
            nxt = [index, page.next_start]
            break
        index, start = index + 1, 0
        if len(events) >= limit and index < len(plan.sources):
            truncated, nxt = True, [index, 0]
            break
    if not truncated and index < len(plan.sources):  # page cap reached between sources
        truncated, nxt = True, [index, start]
    warnings = [OFFSET_WARNING, *plan.warnings]
    if deleted:
        warnings.append(f"{concept}: {deleted} deleted record(s) excluded")
    return _block("ok", events=events, truncated=truncated, complete=not truncated), nxt, warnings


def _notes(ctx: ReadContext, flt: Filters, limit: int, position: list[int]) -> tuple[dict[str, Any], Any, list[str]]:
    """``note_created`` through the verified note reads of ``notes/reads.py`` (4B HV-B5)."""
    problems: list[Problem] = []
    if flt.scope_type not in NOTE_SCOPES:
        problems.append(Problem("unsupported_filter", "note_created", "only job and placement scopes are verified for notes", "HV-B5"))
    if flt.recruiter_id is not None:
        problems.append(Problem("unsupported_filter", "note_created", "recruiter_id has no verified note mechanism", "HV-B5"))
    if flt.date_from_ms is not None or flt.date_to_ms is not None:
        problems.append(Problem("unsupported_filter", "note_created", "date_from/date_to have no verified note mechanism", "HV-B5"))
    if problems:
        return _block("unsupported", unsupported=[p.unsupported() for p in problems]), None, []
    assert flt.scope_type is not None and flt.scope_id is not None
    query = note_reads.NoteQuery(
        scope_type=flt.scope_type,
        scope_id=flt.scope_id,
        action_type=None,
        author=None,
        date_from_ms=None,
        date_to_ms=None,
        include_deleted=False,
        limit=limit,
        start=position[1],
    )
    aset = valid_set(ctx.profile, ctx.states)
    page = note_reads.fetch(EntityWriter(ctx.client), query, aset.semantics)
    events: list[dict[str, Any]] = []
    for evt in page["events"]:
        evt = dict(evt)
        evt["attribution"] = {"rule": "attr.note_created.v1", "field": None, "recruiter_id": None, "resolved": False}
        evt["definition"] = {"profile_version": ctx.profile_version, "rule": "note_created.v1", "mappings": [], "settings": {}}
        moment = parse_utc(evt.get("occurred_at"))
        evt["occurred_at_local"] = _local(moment, ctx.timezone) if moment is not None else None
        events.append(evt)
    nxt = [0, page["next_start"]] if page["next_start"] is not None else None
    return _block("ok", events=events, truncated=page["truncated"], complete=not page["truncated"]), nxt, list(page["warnings"])


def _local(moment: Any, timezone_name: str) -> str | None:
    try:
        return str(moment.astimezone(resolve_zone(timezone_name)).isoformat(timespec="milliseconds"))
    except (ValueError, OverflowError, LookupError, OSError):
        return None

