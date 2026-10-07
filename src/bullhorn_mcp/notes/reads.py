"""Verified note reads (``get_notes``, §1.6).

Only the verified mechanisms of ``PHASE4B_HV_VERIFICATION.md`` are used:

- **Verified (HV-B5):** the to-many reads ``GET /entity/JobOrder/{id}/notes``
  and ``GET /entity/Placement/{id}/notes``, with ``fields``, ``start`` and
  ``count``.
- **Not verified:** every other scope, the ``action_type`` / ``author`` /
  date filters, and ``include_deleted=True``. Each returns
  ``unsupported_filter`` listing the supported filters, and no request is
  made. Every filter is **validated first**, so an unknown action type is
  still rejected and a naive datetime is still an error.

No caller string is ever placed in a request: requests are built only from
validated ints.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..schema.errors import describe_value
from ..tenant.timeutil import FilterError, resolve_zone
from ..tenant.timeutil import parse_bound as _parse_bound
from .action_types import ActionTypeSet, Rejection, validate
from .model import NoteRecord

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_START = 100_000
MAX_ID = 2**63 - 1
MAX_AUTHOR_CHARS = 200
SCOPE_PARAMS = {
    "candidate_id": "candidate",
    "job_id": "job",
    "client_corporation_id": "client_corporation",
    "client_contact_id": "client_contact",
    "placement_id": "placement",
    "submission_id": "submission",
}
TARGET_TYPES = tuple(SCOPE_PARAMS.values())
# HV-B5: verified to-many note reads.
SUPPORTED_SCOPES: Mapping[str, tuple[str, str]] = {"job": ("JobOrder", "notes"), "placement": ("Placement", "notes")}
SUPPORTED_FILTERS = (
    "job_id",
    "placement_id",
    "target_type in ('job', 'placement') with target_id",
    "include_deleted=false",
    "limit",
    "start",
)
LIST_FIELDS = "id,dateAdded,action,comments,isDeleted,personReference,commentingPerson,jobOrder"
ORDERING_WARNING = (
    "ordering: notes are in Bullhorn's order for this endpoint; a descending dateAdded order is not a verified "
    "parameter of the to-many read (HV-B5)"
)

__all__ = ["FilterError", "parse_bound"]  # FilterError is re-exported from tenant/timeutil.py (D-5C-9)


def _zone(name: str) -> _dt.tzinfo:
    """The reporting timezone resolver (``tenant/timeutil.resolve_zone``; patched in tests)."""
    return resolve_zone(name)


def parse_bound(value: object, name: str, timezone_name: str) -> int:
    """``tenant/timeutil.parse_bound`` (the single parser, D-5C-9), with this module's ``_zone`` bound at call time."""
    return _parse_bound(value, name, timezone_name, _zone)


def _is_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ID


@dataclass(frozen=True)
class NoteQuery:
    scope_type: str
    scope_id: int
    action_type: str | None
    author: str | None
    date_from_ms: int | None
    date_to_ms: int | None
    include_deleted: bool
    limit: int
    start: int


def validate_basic(args: Mapping[str, Any], timezone_name: str) -> tuple[NoteQuery | None, list[dict[str, Any]]]:
    """Every check that needs neither the tenant profile nor Bullhorn."""
    errors: list[dict[str, Any]] = []

    def err(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    scopes: list[tuple[str, int]] = []
    ttype, tid = args.get("target_type"), args.get("target_id")
    if ttype is not None or tid is not None:
        if not isinstance(ttype, str) or ttype not in TARGET_TYPES:
            err("invalid_target_type", f"target_type {describe_value(ttype)} is not one of {list(TARGET_TYPES)}")
        elif not _is_id(tid):
            err("invalid_id", f"target_id must be an int >= 1, got {describe_value(tid)}")
        else:
            assert isinstance(tid, int)
            scopes.append((ttype, tid))
    for param, stype in SCOPE_PARAMS.items():
        value = args.get(param)
        if value is None:
            continue
        if not _is_id(value):
            err("invalid_id", f"{param} must be an int >= 1, got {describe_value(value)}")
        else:
            scopes.append((stype, value))
    if not errors and not scopes:
        err("missing_scope", "exactly one primary scope is required: target_type + target_id, or one of " + ", ".join(SCOPE_PARAMS))
    elif len(scopes) > 1:
        err("multiple_scopes", "exactly one primary scope is allowed")

    action = args.get("action_type")
    if action is not None and (not isinstance(action, str) or not action):
        err("invalid_action_type", f"action_type must be a non-empty string, got {describe_value(action)}")
    author = args.get("author")
    if author is not None and (not isinstance(author, str) or not author.strip() or len(author) > MAX_AUTHOR_CHARS):
        err("invalid_author", f"author must be a Bullhorn user id or a name of at most {MAX_AUTHOR_CHARS} characters")

    bounds: dict[str, int | None] = {"date_from": None, "date_to": None}
    for name in bounds:
        value = args.get(name)
        if value is None:
            continue
        try:
            bounds[name] = parse_bound(value, name, timezone_name)
        except FilterError as exc:
            err("invalid_date", str(exc))
    if bounds["date_from"] is not None and bounds["date_to"] is not None and bounds["date_from"] >= bounds["date_to"]:
        err("invalid_date", "date_from must be earlier than date_to (the range is [date_from, date_to))")

    include_deleted = args.get("include_deleted", False)
    if not isinstance(include_deleted, bool):
        err("invalid_include_deleted", "include_deleted must be a boolean")
    limit = args.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        err("invalid_limit", f"limit must be an int from 1 to {MAX_LIMIT}, got {describe_value(limit)}")
    start = args.get("start", 0)
    if type(start) is not int or not 0 <= start <= MAX_START:
        err("invalid_start", f"start must be an int from 0 to {MAX_START}, got {describe_value(start)}")
    if errors:
        return None, errors
    stype, sid = scopes[0]
    return (
        NoteQuery(
            scope_type=stype,
            scope_id=sid,
            action_type=action,
            author=author.strip() if isinstance(author, str) else None,
            date_from_ms=bounds["date_from"],
            date_to_ms=bounds["date_to"],
            include_deleted=bool(include_deleted),
            limit=int(limit),
            start=int(start),
        ),
        [],
    )


def validate_action(query: NoteQuery, aset: ActionTypeSet) -> list[dict[str, Any]]:
    if query.action_type is None:
        return []
    checked = validate(query.action_type, aset)
    return [checked.to_dict()] if isinstance(checked, Rejection) else []


def unsupported(query: NoteQuery) -> list[str]:
    """Filters (or the scope) whose Bullhorn mechanism is unresolved in HV-1 (never approximated)."""
    out: list[str] = []
    if query.scope_type not in SUPPORTED_SCOPES:
        out.append(f"scope:{query.scope_type} (no verified note query for this record type; HV-B5)")
    if query.action_type is not None:
        out.append("action_type (no verified note query mechanism; HV-B5)")
    if query.author is not None:
        out.append("author (no verified note query mechanism; HV-B5, HV-B9)")
    if query.date_from_ms is not None or query.date_to_ms is not None:
        out.append("date_from/date_to (no verified note query mechanism; HV-B5)")
    if query.include_deleted:
        out.append("include_deleted=true (Bullhorn does not return soft-deleted to-many records)")
    return out


def fetch(writer: Any, query: NoteQuery, semantics: Mapping[str, str | None]) -> dict[str, Any]:
    """Run a supported query through the verified to-many read and normalize the page."""
    entity, association = SUPPORTED_SCOPES[query.scope_type]
    raw = writer.fetch_to_many(entity, query.scope_id, association, LIST_FIELDS, query.start, query.limit + 1)
    warnings: list[str] = [ORDERING_WARNING]
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        warnings.append("Bullhorn returned no usable data list")
        data = []
    truncated = len(data) > query.limit
    page = data[: query.limit]
    notes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    deleted = 0
    for item in page:
        record, rec_warnings = NoteRecord.from_bullhorn(item, semantics)
        warnings.extend(rec_warnings)
        if record is None:
            continue
        if record.is_deleted is True and not query.include_deleted:
            deleted += 1
            continue
        notes.append(record.to_dict())
        events.append(record.to_event("observed").to_dict())
    if deleted:
        warnings.append(f"{deleted} deleted note(s) excluded")
    return {
        "status": "ok",
        "scope": {"type": query.scope_type, "id": query.scope_id},
        "notes": notes,
        "events": events,
        "next_start": query.start + query.limit if truncated else None,
        "truncated": truncated,
        "warnings": warnings[:50],
    }

