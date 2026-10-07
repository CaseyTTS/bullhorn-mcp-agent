"""One deriver per activity concept (Phase 5C, §2; ``CANONICAL_ACTIVITY_VOCABULARY.md`` §2).

A deriver turns a concept, the tenant's configuration and the request filters
into read plans (``Source``: entity, clauses, selection) and turns returned
rows into vocabulary §1 events through ``activity/events.py``. Business
meaning comes only from the tenant's value mappings and settings (G-2), and
availability is decided through ``tenant/capabilities.py`` (by the service).

Fail-closed rules applied here:

- Interviews are derived only from appointments classified by the tenant's
  ``interview_scheduled`` mapping, and only from **parent** appointments
  (``parentAppointment IS NULL``, HV-Q9b, Amendment C3-3a). One event per parent.
- ``interview_rescheduled``, ``job_status_changed`` and
  ``candidate_status_changed`` are ``unsupported_concept`` (HV-Q9, HV-Q10).
- ``client_submission_dating = status_history`` is ``unsupported_concept``
  (``status_history_unresolved``, HV-Q10).
- ``interview_cancelled`` has no verified cancellation time, so a date range
  makes that concept ``unsupported_filter``.
- A filter a concept cannot honour makes that concept unsupported; it is never dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..bullhorn.query_syntax import Clause, Predicate
from ..reads.records import (
    ReadContext,
    base_clauses,
    entity_support,
    extract,
    filter_predicate,
    mapped_fields,
    mapping_clause,
    selection,
    shape_value,
)
from ..reads.support import EntitySupport
from ..schema.bullhorn_catalog import NestedField
from ..schema.query_builder import FilterSpec, Problem, Resolved
from ..tenant.capabilities import concept_mappings
from ..tenant.timeutil import epoch_millis_to_local_iso, format_utc
from .events import ActivityEvent, make_activity_id

UNSUPPORTED_CONCEPTS: Mapping[str, tuple[str, str]] = {
    "interview_rescheduled": ("no verified reschedule representation (no reschedule link or history)", "HV-Q9"),
    "job_status_changed": ("status_history_unresolved", "HV-Q10"),
    "candidate_status_changed": ("status_history_unresolved", "HV-Q10"),
}
SCOPE_TYPES = ("candidate", "job", "client_corporation", "client_contact", "placement", "submission")
# Scope -> canonical field of the source entity ("id": the record itself).
SCOPES: Mapping[str, Mapping[str, str]] = {
    "job": {"job": "id", "client_corporation": "client_corporation_id", "client_contact": "client_contact_id"},
    "submission": {"submission": "id", "candidate": "candidate_id", "job": "job_id"},
    "appointment": {"candidate": "candidate_id", "job": "job_id", "client_contact": "client_contact_id"},
    "placement": {
        "placement": "id", "candidate": "candidate_id", "job": "job_id", "submission": "submission_id",
        "client_corporation": "client_corporation_id",
    },
}
# Event link key -> canonical field of the source entity.
LINKS: Mapping[str, Mapping[str, str]] = {
    "job": {
        "job_id": "id", "owner_id": "owner_id", "client_corporation_id": "client_corporation_id",
        "client_contact_id": "client_contact_id",
    },
    "submission": {"submission_id": "id", "candidate_id": "candidate_id", "job_id": "job_id"},
    "appointment": {
        "appointment_id": "id", "candidate_id": "candidate_id", "job_id": "job_id", "client_contact_id": "client_contact_id",
        "owner_id": "owner_id",
    },
    "placement": {
        "placement_id": "id", "candidate_id": "candidate_id", "job_id": "job_id", "submission_id": "submission_id",
        "client_corporation_id": "client_corporation_id",
    },
}
# D-5C-14: attribution (recruiter_id) by source entity, per concept family.
ATTRIBUTION: Mapping[str, str] = {
    "job": "primary_recruiter_id",
    "submission": "sending_user_id",
    "appointment": "owner_id",
    "placement": "recruiter_id",
}
# Concept -> (source entities, timestamp canonical field, kind).
CONCEPT_SOURCES: Mapping[str, tuple[tuple[str, ...], str | None, str]] = {
    "job_created": (("job",), "date_added", "event"),
    "submission_created": (("submission",), "date_added", "event"),
    "client_submission": (("submission",), "date_added", "event"),
    "interview_scheduled": (("appointment",), "date_added", "event"),
    "interview_completed": (("appointment",), "end_at", "event"),
    "interview_cancelled": (("appointment",), None, "event"),
    "interview_upcoming": (("appointment",), "start_at", "state"),
    "offer_extended": (("submission", "placement"), "date_added", "event"),
    "offer_accepted": (("submission", "placement"), "date_added", "event"),
    "offer_declined": (("submission", "placement"), "date_added", "event"),
    "offer_pending": (("submission", "placement"), "date_added", "state"),
    "placement_created": (("placement",), "date_added", "event"),
}
OFFER_CONCEPTS = ("offer_extended", "offer_accepted", "offer_declined", "offer_pending")
INTERVIEW_CONCEPTS = ("interview_scheduled", "interview_completed", "interview_cancelled", "interview_upcoming")
RECURRENCE_WARNING = "recurrence_not_expanded: a recurring appointment series is one record and is not expanded (HV-Q9)"


@dataclass(frozen=True)
class Filters:
    scope_type: str | None
    scope_id: int | None
    recruiter_id: int | None
    date_from_ms: int | None
    date_to_ms: int | None
    now_ms: int


@dataclass
class Source:
    """One read plan of a concept (one entity)."""

    entity: str
    ent: EntitySupport
    clauses: list[Clause]
    resolved: dict[str, Resolved]  # canonical field -> resolution (timestamp, attribution, links)
    unresolved: list[str]  # link keys whose canonical field is not configured
    mapping_records: list[Any] = field(default_factory=list)
    extra_fields: list[str] = field(default_factory=list)

    def fields(self) -> list[tuple[str, tuple[str, ...]]]:
        extra = list(self.extra_fields)
        if self.ent.soft_delete_field:
            extra.append(self.ent.soft_delete_field)
        return selection([r.target for r in self.resolved.values()], extra + mapped_fields(self.mapping_records))


@dataclass
class Plan:
    concept: str
    sources: list[Source] = field(default_factory=list)
    unsupported: list[Problem] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _spec(field_name: str, op: str, value: Any) -> FilterSpec:
    return FilterSpec(field_name, op, value)


def _restricted(concept: str, name: str) -> Problem:
    return Problem("unsupported_filter", concept, f"{name} is a restricted field", None)


def plan(ctx: ReadContext, concept: str, filters: Filters) -> Plan:
    """The read plan for one concept (zero Bullhorn calls)."""
    out = Plan(concept)
    if concept in UNSUPPORTED_CONCEPTS:
        reason, hv = UNSUPPORTED_CONCEPTS[concept]
        out.unsupported.append(Problem("unsupported_concept", concept, reason, hv))
        return out
    entities, ts_field, _kind = CONCEPT_SOURCES[concept]
    if concept in ("client_submission", *OFFER_CONCEPTS):
        dating = ctx.profile.settings.client_submission_dating if ctx.profile is not None else None
        if dating == "status_history":
            history = ctx.support.history.get("submission_status")
            if history is None or not history.verified:
                out.unsupported.append(Problem("unsupported_concept", concept, "status_history_unresolved", "HV-Q10"))
                return out
    if ts_field is None and (filters.date_from_ms is not None or filters.date_to_ms is not None):
        out.unsupported.append(
            Problem("unsupported_filter", concept, "cancellation_time_unresolved: no verified cancellation time (HV-Q9)", "HV-Q9")
        )
        return out
    for entity in entities:
        if concept in OFFER_CONCEPTS:
            wanted = ["offer_extended"] if concept == "offer_pending" else [concept]
            if not any(concept_mappings(ctx.profile, ctx.states, entity, c) for c in wanted):
                continue  # the offer mapping is on the other entity
        src = _source(ctx, concept, entity, ts_field, filters, out)
        if src is not None:
            out.sources.append(src)
    if not out.sources and not out.unsupported and not out.missing:
        out.missing.append(f"value_mapping:{'|'.join(entities)}:{concept}")
    if concept in INTERVIEW_CONCEPTS and out.sources:
        if any(s.ent.recurrence_single_record for s in out.sources):
            out.warnings.append(RECURRENCE_WARNING)
    return out


def _source(ctx: ReadContext, concept: str, entity: str, ts_field: str | None, f: Filters, out: Plan) -> Source | None:
    ent = entity_support(ctx, entity)
    if isinstance(ent, Problem):
        out.unsupported.append(Problem("unsupported_concept", concept, f"{ent.reason} ({entity})", ent.hv))
        return None
    resolver = ctx.resolver(entity)
    resolved: dict[str, Resolved] = {}
    unresolved: list[str] = []
    clauses: list[Clause] = base_clauses(ent)
    problems: list[Problem] = []

    def need(name: str) -> Resolved | None:
        if name in resolved:
            return resolved[name]
        res = resolver.resolve(name) if name in resolver.types else None
        if res is not None and isinstance(res.target, NestedField) and not ctx.support.syntax.nested_fields_verified:
            return None  # HV-Q13 unresolved: nested links are not read (reported as unresolved)
        if res is not None and not res.sensitive:
            resolved[name] = res
            return res
        return None

    for key, cname in LINKS[entity].items():
        if need(cname) is None:
            unresolved.append(key)
    attribution = ATTRIBUTION[entity]
    if need(attribution) is None:
        unresolved.append("recruiter_id")
    if ts_field is not None and need(ts_field) is None:
        out.missing.append(f"mapping:{entity}.{ts_field}")
        return None

    # Concept-specific clauses (tenant mappings only).
    records: list[Any] = []
    extra_fields: list[str] = []
    if concept in INTERVIEW_CONCEPTS:
        if ent.instance_filter is None:  # Amendment C3-3a
            out.unsupported.append(Problem("unsupported_concept", concept, "invitee_copies_unresolved", "HV-Q9b"))
            return None
        clauses.append(Predicate(ent.instance_filter[0], "is_null"))
        extra_fields.append(ent.instance_filter[0])  # for the defensive parent check (keep_row)
        groups: list[tuple[str, str]] = [("interview_scheduled", "in")]
        rule = ctx.profile.settings.interview_completion_rule if ctx.profile is not None else None
        if concept == "interview_completed" and rule == "mapped_state_only":
            groups.append(("interview_completed", "in"))
        elif concept == "interview_completed":
            groups.append(("interview_cancelled", "not_in_or_null"))
            end = need("end_at")
            if end is None:
                out.missing.append("mapping:appointment.end_at")
                return None
            pred = filter_predicate(ctx, ent, _spec("end_at", "lt", f.now_ms), end)
            problems.append(pred) if isinstance(pred, Problem) else clauses.append(pred)
        elif concept == "interview_cancelled":
            groups.append(("interview_cancelled", "in"))
        elif concept == "interview_upcoming":
            groups.append(("interview_cancelled", "not_in_or_null"))
            start = need("start_at")
            if start is None:
                out.missing.append("mapping:appointment.start_at")
                return None
            pred = filter_predicate(ctx, ent, _spec("start_at", "gte", f.now_ms), start)
            problems.append(pred) if isinstance(pred, Problem) else clauses.append(pred)
        for name, op in groups:
            recs = concept_mappings(ctx.profile, ctx.states, "appointment", name)
            clause = mapping_clause(ctx, ent, recs, concept, op)
            problems.append(clause) if isinstance(clause, Problem) else clauses.append(clause)
            records.extend(recs)
    elif concept == "client_submission" or concept in OFFER_CONCEPTS:
        name = "offer_extended" if concept == "offer_pending" else concept
        recs = concept_mappings(ctx.profile, ctx.states, entity, name)
        clause = mapping_clause(ctx, ent, recs, concept)
        problems.append(clause) if isinstance(clause, Problem) else clauses.append(clause)
        records.extend(recs)

    # Request filters: scope, recruiter, date range. Never dropped (§3.2).
    if f.scope_type is not None:
        scope_field = SCOPES[entity].get(f.scope_type)
        res = need(scope_field) if scope_field is not None else None
        if scope_field is None or res is None:
            problems.append(Problem("unsupported_filter", concept, f"scope {f.scope_type} is not supported for this concept", None))
        else:
            pred = filter_predicate(ctx, ent, _spec(scope_field, "eq", f.scope_id), res)
            problems.append(pred) if isinstance(pred, Problem) else clauses.append(pred)
    if f.recruiter_id is not None:
        res = resolved.get(attribution)
        if res is None:
            problems.append(Problem("unsupported_filter", concept, "recruiter_id: the attribution field is not configured", None))
        else:
            pred = filter_predicate(ctx, ent, _spec(attribution, "eq", f.recruiter_id), res)
            problems.append(pred) if isinstance(pred, Problem) else clauses.append(pred)
    if ts_field is not None:
        res = resolved[ts_field]
        for op, value in (("gte", f.date_from_ms), ("lt", f.date_to_ms)):
            if value is None:
                continue
            pred = filter_predicate(ctx, ent, _spec(ts_field, op, value), res)
            problems.append(pred) if isinstance(pred, Problem) else clauses.append(pred)
    if problems:
        for p in problems:
            out.unsupported.append(Problem(p.code, concept, p.reason, p.hv))
        return None
    return Source(entity, ent, clauses, resolved, unresolved, records, extra_fields)


def _value(src: Source, cname: str, row: Mapping[str, Any]) -> Any:
    res = src.resolved.get(cname)
    if res is None:
        return None
    return shape_value(res.ctype, extract(res.target, row))


def _raw_ts(src: Source, cname: str | None, row: Mapping[str, Any]) -> int | None:
    res = src.resolved.get(cname) if cname is not None else None
    value = extract(res.target, row) if res is not None else None
    return value if type(value) is int else None


def event(ctx: ReadContext, concept: str, src: Source, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """One vocabulary §1 event (``origin: observed``) for a returned row."""
    rid = row.get("id")
    if type(rid) is not int:
        return None
    _entities, ts_field, kind = CONCEPT_SOURCES[concept]
    links = {key: _value(src, cname, row) for key, cname in LINKS[src.entity].items()}
    attribution_field = ATTRIBUTION[src.entity]
    recruiter = _value(src, attribution_field, row)
    links["recruiter_id"] = recruiter
    ms = _raw_ts(src, ts_field, row)
    occurred = _value(src, ts_field, row) if ts_field is not None else None
    defining_at = None
    if kind == "state":  # vocabulary §2: a state's timestamp is the read time; its defining timestamp is evidence
        defining_at = occurred
        occurred = format_utc(ctx.now)
        ms = int(ctx.now.timestamp() * 1000)
    matched = []
    for rec in src.mapping_records:
        value = row.get(rec.bullhorn_field)
        if value is not None and any(type(value) is type(v) and value == v for v in rec.values):
            matched.append({"mapping": rec.key, "concept": rec.target.name, "value": value})
    state = None
    if kind == "state":
        state = "upcoming" if concept == "interview_upcoming" else "pending"
    elif concept == "interview_cancelled":
        state = "cancelled"
    evt = ActivityEvent(
        activity_id=make_activity_id(concept, src.entity, rid),
        concept=concept,
        occurred_at=occurred,
        source={"canonical_entity": src.entity, "id": rid},
        links=links,
        origin="observed",
        unresolved_links=tuple(src.unresolved),
        state=state,
        attribution={
            "rule": f"attr.{concept}.v1",
            "field": f"{src.entity}.{attribution_field}",
            "recruiter_id": recruiter,
            "resolved": "recruiter_id" not in src.unresolved,
        },
        definition={
            "profile_version": ctx.profile_version,
            "rule": f"{concept}.v1",
            "mappings": sorted({rec.key for rec in src.mapping_records}),
            "settings": _settings(ctx, concept),
        },
        evidence={
            "timestamp_field": None if ts_field is None else f"{src.entity}.{ts_field}",
            **({"defining_at": defining_at} if kind == "state" else {}),
            "matched": matched,
        },
    ).to_dict()
    evt["occurred_at_local"] = epoch_millis_to_local_iso(ms, ctx.timezone) if ms is not None else None
    return evt


def _settings(ctx: ReadContext, concept: str) -> dict[str, Any]:
    if ctx.profile is None:
        return {}
    s = ctx.profile.settings
    if concept in ("client_submission", *OFFER_CONCEPTS):
        return {"client_submission_dating": s.client_submission_dating}
    if concept == "interview_completed":
        return {"interview_completion_rule": s.interview_completion_rule}
    return {}


def keep_row(concept: str, src: Source) -> Any:
    """A defensive row check matching the server-side concept filter (``None``: keep all)."""
    if concept not in INTERVIEW_CONCEPTS:
        return None
    parent = src.ent.instance_filter[0] if src.ent.instance_filter else None

    def keep(row: Mapping[str, Any]) -> bool:
        return parent is None or row.get(parent) is None

    return keep

