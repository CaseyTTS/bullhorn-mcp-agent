"""The ``find_records`` service (Phase 5C, §3.1) and the read plumbing shared with ``get_activity``.

The service takes the caller's ``IdentityContext`` and the per-call client
explicitly; it never resolves a client itself (G-4). Every request goes out
through ``bullhorn/reads.py::EntityReader`` on that client. The status
vocabulary is §3.4; every non-``ok`` status except ``error`` is reached with
zero Bullhorn record requests.

Paging (Amendment C3-3, HV-Q4): offset only, ``consistency: "offset"`` with a
warning, no ``orderBy``, at most ``limit + 1`` records over at most 5 page
requests; ``complete`` only when a page returns fewer rows than requested.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..bullhorn.query_syntax import AnyOf, Clause, Literal, Predicate, UnsupportedValue, query_request, render_fields, render_where
from ..bullhorn.reads import EntityReader
from ..schema.bullhorn_catalog import BullhornCatalog, NestedField, RawField, TemplateField, load_bullhorn_catalog
from ..schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from ..schema.errors import SchemaError
from ..schema.query_builder import (
    FILTER_KEYS,
    FIND_ENTITIES,
    OPERATORS,
    SORT_KEYS,
    FilterSpec,
    Problem,
    Resolved,
    Resolver,
    item_name,
    literals_for,
    missing_requirement,
    validate_fields,
    validate_filters,
    validate_sort,
    value_literal,
)
from ..tenant.capabilities import (
    CONCEPT_RULES,
    DERIVED_STATE_CONCEPTS,
    concept_entities,
    concept_mappings,
    concept_requirements,
    evaluate_capability,
    ordering_values,
)
from ..tenant.profile_v2 import TenantProfileV2, current_catalog_fingerprint, rest_url_fingerprint, tagged
from ..tenant.revalidation import load_snapshot
from ..tenant.state import ConnectionCheck, SetupState, compute_setup_state, effective_states
from ..tenant.store import store_from_env
from ..tenant.timeutil import coerce_epoch_millis_to_utc_iso, format_utc
from . import cursor as cursors
from .support import EntitySupport, QuerySupport, load_query_support

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_PAGES = 5
MAX_WARNINGS = 50
TEXT_CHARS = 2000
QUERY_SUPPORT_VERSION = 1
VOCABULARY_VERSION = 1
OFFSET_WARNING = (
    "consistency: offset paging (HV-Q4: the /query orderBy direction is undocumented, so no order is requested); "
    "records are in server order, which is not guaranteed, and changes between pages can shift results"
)
REVALIDATION_WARNING = "setup_revalidation_required: the tenant profile needs revalidation; results use the last valid mappings"
SETUP_REQUIRED_STATES = ("disconnected", "connected_setup_required", "setup_in_progress")

DEFAULT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "candidate": ("id", "first_name", "last_name", "email", "status", "date_added", "owner_id"),
    "job": ("id", "title", "status", "is_open", "date_added", "client_corporation_id", "owner_id"),
    "submission": ("id", "candidate_id", "job_id", "status", "date_added", "sending_user_id"),
    "placement": ("id", "candidate_id", "job_id", "submission_id", "status", "start_date", "date_added"),
    "client_corporation": ("id", "name", "status", "date_added"),
    "client_contact": ("id", "first_name", "last_name", "email", "status", "client_corporation_id"),
    "appointment": ("id", "subject", "type", "start_at", "end_at", "candidate_id", "job_id", "owner_id"),
    "user": ("id", "first_name", "last_name", "email", "status"),
}


class Outcome(Exception):
    """A finished non-``ok`` result (carried out of nested helpers)."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("status"))
        self.result = result


def status(name: str, **payload: Any) -> dict[str, Any]:
    return {"status": name, **payload}


def rejected(problems: Sequence[Problem]) -> dict[str, Any]:
    return status("rejected_validation", errors=[p.error() for p in problems][:MAX_WARNINGS])


def unsupported(problems: Sequence[Problem]) -> dict[str, Any]:
    return status("unsupported", unsupported=[p.unsupported() for p in problems][:MAX_WARNINGS])


# ---------------------------------------------------------------------- #
# Context: tenant profile, discovery snapshot, setup state, binding
# ---------------------------------------------------------------------- #


@dataclass
class ReadContext:
    ident: Any  # identity.principal.IdentityContext
    client: Any
    env: Mapping[str, str]
    now: _dt.datetime
    support: QuerySupport
    canonical: CanonicalCatalog
    bullhorn: BullhornCatalog
    profile: TenantProfileV2 | None = None
    states: dict[str, str] = field(default_factory=dict)
    snapshot: Any = None
    setup: SetupState | None = None
    timezone: str = "UTC"
    warnings: list[str] = field(default_factory=list)
    reader: EntityReader | None = None
    reader_factory: Callable[..., EntityReader] = EntityReader

    @property
    def profile_version(self) -> int | None:
        return self.profile.profile_version if self.profile is not None else None

    def snapshot_fields(self, entity: str) -> frozenset[str] | None:
        ent = self.snapshot.entity(entity) if self.snapshot is not None else None
        if ent is None or not ent.usable:
            return None
        return frozenset(ent.fields)

    def resolver(self, entity: str) -> Resolver:
        return Resolver(entity, self.canonical, self.bullhorn, self.profile, self.states, self.snapshot_fields(entity))

    def binding(self, request_hash: str) -> cursors.Binding:
        ident = self.ident
        principal = ident.principal_key or ident.initiating_principal
        link = ident.executing_bullhorn_identity if ident.mode != "local" else None
        return cursors.Binding(ident.tenant_key or "local", principal, link, request_hash, self.profile_version)

    def get_reader(self) -> EntityReader:
        if self.reader is None:
            ident = self.ident
            key = (ident.tenant_key or "local", ident.principal_key or ident.initiating_principal)
            self.reader = self.reader_factory(self.client, key)
        return self.reader

    @property
    def state(self) -> str:
        return self.setup.state if self.setup is not None else "connected_setup_required"

    def provenance(self, request_hash: str) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "catalog_fingerprint": current_catalog_fingerprint(),
            "query_support_version": QUERY_SUPPORT_VERSION,
            "vocabulary_version": VOCABULARY_VERSION,
            "request_hash": request_hash,
            "retrieved_at": format_utc(self.now),
        }


def load_tenant(ctx: ReadContext) -> None:
    """Load the active profile, its record states and the discovery snapshot (local files only)."""
    try:
        store = store_from_env(ctx.env)
        active = store.active_version() if store is not None else None
        doc = store.read_discovery() if store is not None else None
        if store is not None and active is not None:
            ctx.profile = store.read_version(active)
            ctx.states, _ = effective_states(ctx.profile, doc)
            ctx.timezone = ctx.profile.settings.reporting_timezone
        try:
            ctx.snapshot = load_snapshot(doc)
        except (SchemaError, TypeError, ValueError, KeyError):
            ctx.snapshot = None
    except SchemaError:
        ctx.profile, ctx.states, ctx.snapshot = None, {}, None


def check_setup(ctx: ReadContext, *, execution_tier: str | None = None) -> None:
    """Setup state with the caller's own session ``rest_url`` (D-5C-6, D-5C-7). Raises ``Outcome``.

    ``execution_tier`` (Phase 6 M1-A1): ``"service"`` only from ``metrics/tier2.py``; ``None`` is unchanged.
    """
    if execution_tier not in (None, "service"):
        raise ValueError("execution_tier must be None or 'service'")
    if ctx.profile is None and ctx.snapshot is None:
        raise Outcome(status("setup_required", missing_requirements=["profile:active_version"]))
    fingerprint = rest_url_fingerprint(ctx.client.auth.session.rest_url)  # the caller's session; never returned
    ctx.setup = compute_setup_state(ctx.env, ConnectionCheck(ok=True, rest_url_fingerprint=fingerprint), now=ctx.now,
                                   execution_tier=execution_tier)
    if (
        ctx.profile is not None
        and ctx.profile.rest_url_fingerprint is not None
        and fingerprint is not None
        and ctx.profile.rest_url_fingerprint != fingerprint
    ):
        raise Outcome(status("setup_revalidation_required", missing_requirements=["revalidate:rest_url_changed"]))
    if ctx.state in SETUP_REQUIRED_STATES:
        raise Outcome(status("setup_required", missing_requirements=[m for m in ctx.setup.missing_requirements][:MAX_WARNINGS]))
    if ctx.state == "setup_invalid":
        raise Outcome(status("setup_revalidation_required", missing_requirements=list(ctx.setup.missing_requirements)[:MAX_WARNINGS]))
    if ctx.state == "setup_revalidation_required":
        ctx.warnings.append(REVALIDATION_WARNING)


def capability(ctx: ReadContext, name: str) -> tuple[bool, tuple[str, ...]]:
    gate = evaluate_capability(name, ctx.profile, ctx.states, ctx.state)
    return gate.ok, gate.missing


def normalized_hash(ctx: ReadContext, args: Mapping[str, Any]) -> str:
    """The keyed request HMAC (triage B-2): cursor binding and ``provenance.request_hash``."""
    ident = ctx.ident
    principal = ident.principal_key or ident.initiating_principal
    return cursors.provenance_hash(ident.tenant_key or "local", principal, {k: v for k, v in args.items() if k != "cursor"})


# ---------------------------------------------------------------------- #
# Entity support (HV-Q1, HV-Q6, HV-Q8)
# ---------------------------------------------------------------------- #


def entity_support(ctx: ReadContext, entity: str) -> EntitySupport | Problem:
    bh = ctx.bullhorn.bullhorn_entity_for(entity)
    ent = ctx.support.entities.get(bh) if bh is not None else None
    if bh is None or ent is None or ent.canonical_entity != entity:
        hv = "HV-Q8" if entity == "user" else "HV-Q1"
        return Problem("unsupported_entity", entity, "no verified read operation for this entity", hv)
    if ent.operation != "query":
        return Problem("unsupported_entity", entity, "only /query is verified; Lucene index fields are unresolved", "HV-Q3")
    if not ent.soft_delete_resolved:
        return Problem("unsupported_entity", entity, "deleted_semantics_unresolved", "HV-Q6")
    return ent


def soft_delete_clause(ent: EntitySupport) -> Clause | None:
    """``(isDeleted = false OR isDeleted IS NULL)`` (Amendment C3-3, HV-Q6-I); ``None`` when not soft-deletable.

    The same term is used for every soft-deletable entity, whether or not its flag is
    documented as nullable: a NULL flag means "not deleted" (HV-Q6-I).
    """
    if ent.soft_delete_field is None:
        return None
    false = Predicate(ent.soft_delete_field, "eq", (Literal("bool", False),))
    return AnyOf((false, Predicate(ent.soft_delete_field, "is_null")))


def is_deleted(ent: EntitySupport, row: Mapping[str, Any]) -> bool:
    return ent.soft_delete_field is not None and row.get(ent.soft_delete_field) is True


# ---------------------------------------------------------------------- #
# Selection, filters and values
# ---------------------------------------------------------------------- #


def selection(targets: Sequence[Any], extra: Sequence[str] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """``fields=`` items for the targets (plain names, ``field(key)`` groups, template placeholders)."""
    plain: dict[str, None] = {"id": None}
    nested: dict[str, dict[str, None]] = {}
    for target in targets:
        if isinstance(target, RawField):
            plain[target.name] = None
        elif isinstance(target, NestedField):
            nested.setdefault(target.field, {})[target.key] = None
        elif isinstance(target, TemplateField):
            for name in target.placeholders:
                plain[name] = None
    for name in extra:
        plain[name] = None
    out: list[tuple[str, tuple[str, ...]]] = [(n, ()) for n in plain if n not in nested]
    out.extend((n, tuple(keys)) for n, keys in nested.items())
    return out


def filter_predicate(ctx: ReadContext, ent: EntitySupport, spec: FilterSpec, res: Resolved) -> Predicate | Problem:
    path = res.path
    if path is None:
        return Problem("unsupported_filter", spec.field, "a composed (template) field is output-only", None)  # D-5C-5 rule
    if "." in path and not ctx.support.syntax.association_paths_verified:
        return Problem("unsupported_filter", spec.field, "association paths are unresolved", "HV-Q2")
    if isinstance(res.target, NestedField) and not ctx.support.syntax.nested_fields_verified:
        return Problem("unsupported_filter", spec.field, "nested sub-field selection is unresolved", "HV-Q13")
    ops = ent.ops_for(path, res.raw_custom)
    if ops is None:
        hv = "HV-Q5" if res.ctype in ("datetime", "date") else "HV-Q2"  # R2-L3: the date-representation guard is HV-Q5
        return Problem("unsupported_filter", spec.field, "this field has no verified filter mechanism", hv)
    if spec.op == "starts_with" and not ctx.support.syntax.prefix_match_verified:
        return Problem("unsupported_operator", spec.field, "prefix matching (LIKE) is not documented", "HV-Q2")
    if spec.op not in ops:
        return Problem("unsupported_operator", spec.field, f"operator {spec.op!r} is not verified for this field", "HV-Q2")
    if spec.op == "is_null":
        return Predicate(path, "is_null")
    values = spec.value if spec.op == "in" else (spec.value,)
    lits = literals_for(res.ctype, values, ctx.support.syntax.string_escape_verified)
    if isinstance(lits, str):
        return Problem("unsupported_value", spec.field, lits, "HV-Q2")
    return Predicate(path, spec.op, lits)


def mapping_clause(ctx: ReadContext, ent: EntitySupport, records: Sequence[Any], item: str, op: str = "in") -> Clause | Problem:
    """``field IN (mapped values)`` over every given value-mapping record (OR across fields)."""
    by_field: dict[str, list[Any]] = {}
    for rec in records:
        by_field.setdefault(rec.bullhorn_field, [])
        for v in rec.values:
            if all(tagged(v) != tagged(w) for w in by_field[rec.bullhorn_field]):
                by_field[rec.bullhorn_field].append(v)
    preds: list[Predicate] = []
    for fname, values in by_field.items():
        if ctx.bullhorn.is_sensitive(fname):
            return Problem("unsupported_concept", item, "the mapped field is a restricted field", None)  # D-5C-5 rule
        ops = ent.ops_for(fname, ctx.bullhorn.is_custom_field(fname))
        if ops is None or "in" not in ops:
            return Problem("unsupported_concept", item, "the mapped field has no verified filter mechanism", "HV-Q2")
        lits = [value_literal(v) for v in values]
        if any(lit is None for lit in lits):
            return Problem("unsupported_value", item, "a mapped value has no verified literal form (HV-Q2 charset)", "HV-Q2")
        preds.append(Predicate(fname, op, tuple(lit for lit in lits if lit is not None)))
    if not preds:
        return Problem("unsupported_concept", item, "no mapping", None)
    return preds[0] if len(preds) == 1 else AnyOf(tuple(preds))


def mapped_fields(records: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(rec.bullhorn_field for rec in records))


def extract(target: Any, row: Mapping[str, Any]) -> Any:
    if isinstance(target, RawField):
        return row.get(target.name)
    if isinstance(target, NestedField):
        outer = row.get(target.field)
        return outer.get(target.key) if isinstance(outer, dict) else None
    if isinstance(target, TemplateField):
        pieces: list[str] = []
        for literal_text, name in target.parts:
            pieces.append(literal_text)
            if name is None:
                continue
            value = row.get(name)
            if value is None or isinstance(value, (dict, list)):
                return None
            pieces.append(str(value))
        return "".join(pieces)
    return None


def shape_value(ctype: str, value: Any) -> Any:
    if value is None:
        return None
    if ctype in ("datetime", "date"):
        try:
            return coerce_epoch_millis_to_utc_iso(value)
        except ValueError:
            return None
    if ctype in ("id", "reference"):
        return value if type(value) is int else None
    if ctype == "text":
        if not isinstance(value, str):
            return None
        return {"text": value[:TEXT_CHARS], "truncated": len(value) > TEXT_CHARS, "length": len(value)}
    if ctype == "boolean":
        return value if type(value) is bool else None
    if ctype in ("integer", "number"):
        return value if type(value) in (int, float) else None
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


# ---------------------------------------------------------------------- #
# Offset paging (HV-Q4)
# ---------------------------------------------------------------------- #


@dataclass
class Page:
    rows: list[dict[str, Any]]
    next_start: int | None
    truncated: bool
    complete: bool
    deleted: int
    requests: int


def fetch(
    ctx: ReadContext,
    ent: EntitySupport,
    where: str,
    fields_param: str,
    start: int,
    limit: int,
    keep: Callable[[Mapping[str, Any]], bool] | None = None,
    max_pages: int = MAX_PAGES,
) -> Page:
    """At most ``limit + 1`` kept rows over at most ``max_pages`` (<= ``MAX_PAGES``) requests, from offset ``start``."""
    reader = ctx.get_reader()
    want = limit + 1
    kept: list[tuple[int, dict[str, Any]]] = []
    position = start
    exhausted = False
    deleted = 0
    requests = 0
    max_pages = max(1, min(max_pages, MAX_PAGES))
    while requests < max_pages and len(kept) < want:
        count = min(ent.max_page_size, want - len(kept))
        endpoint, params = query_request(ent.name, where, fields_param, position, count)
        data = reader.get(endpoint, params)["data"]
        requests += 1
        for offset, row in enumerate(data):
            if not isinstance(row, dict):
                continue
            if is_deleted(ent, row):  # defensive post-filter (D-5C-8)
                deleted += 1
                continue
            if keep is not None and not keep(row):
                continue
            kept.append((position + offset, row))
        position += len(data)
        if len(data) < count:
            exhausted = True
            break
    if len(kept) > limit:
        return Page([r for _, r in kept[:limit]], kept[limit][0], True, False, deleted, requests)
    if exhausted:
        return Page([r for _, r in kept], None, False, True, deleted, requests)
    return Page([r for _, r in kept], position, True, False, deleted, requests)


def build_where(ctx: ReadContext, clauses: list[Clause]) -> str | Problem:
    try:
        return render_where(clauses, ctx.support.syntax.max_where_chars)
    except UnsupportedValue as exc:
        hv = "HV-Q12" if "exceeds" in str(exc) else "HV-Q2"
        return Problem("unsupported_value", "filters", str(exc), hv)


def base_clauses(ent: EntitySupport) -> list[Clause]:
    soft = soft_delete_clause(ent)
    # /query requires a where clause; ``id IS NOT NULL`` is always true (id is the not-null primary key).
    return [soft] if soft is not None else [Predicate("id", "is_not_null")]


# ---------------------------------------------------------------------- #
# Priority (RAG-2, §3.1)
# ---------------------------------------------------------------------- #


def priority_order(ctx: ReadContext) -> list[Any] | None:
    return ordering_values(ctx.profile, ctx.states, "job", "priority")


def priority_rank(order: Sequence[Any], value: Any) -> int | None:
    for i, v in enumerate(order):
        if value is not None and tagged(v) == tagged(value):
            return i + 1
    return None


# ---------------------------------------------------------------------- #
# find_records
# ---------------------------------------------------------------------- #


def _validate_basic(args: Mapping[str, Any]) -> list[Problem]:
    errors: list[Problem] = []
    entity = args.get("entity")
    if type(entity) is not str or entity not in FIND_ENTITIES:
        errors.append(Problem("invalid_value", "entity", f"entity must be one of {list(FIND_ENTITIES)}"))
    concept = args.get("concept")
    if concept is not None and type(concept) is not str:
        errors.append(Problem("invalid_concept", "concept", "concept must be a concept ID string"))
    if type(args.get("include_deleted", False)) is not bool:
        errors.append(Problem("invalid_value", "include_deleted", "include_deleted must be a boolean"))
    limit = args.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        errors.append(Problem("invalid_value", "limit", f"limit must be an int from 1 to {MAX_LIMIT}"))
    cur = args.get("cursor")
    if cur is not None and type(cur) is not str:
        errors.append(Problem("invalid_cursor", "cursor", "invalid_cursor"))
    return errors


def find_records(ctx: ReadContext, args: Mapping[str, Any]) -> dict[str, Any]:
    """Run ``find_records``. Never raises for request problems (they are statuses)."""
    try:
        return _find_records(ctx, args)
    except Outcome as out:
        return out.result


def _find_records(ctx: ReadContext, args: Mapping[str, Any]) -> dict[str, Any]:
    errors = _validate_basic(args)
    if errors:
        return rejected(errors)
    entity: str = args["entity"]
    load_tenant(ctx)
    resolver = ctx.resolver(entity)
    allow = resolver.allowlist
    filters, f_errors, f_unsupported = validate_filters(args.get("filters"), allow, resolver.types, ctx.timezone)
    requested, fld_errors = validate_fields(args.get("fields"), allow)
    sort, sort_errors = validate_sort(args.get("sort"), allow)
    concept = args.get("concept")
    concept_problem: Problem | None = None
    if concept is not None:
        if concept == "interview_rescheduled" and entity == "appointment":
            concept_problem = Problem("unsupported_concept", concept, "no verified reschedule representation", "HV-Q9")
        elif concept in DERIVED_STATE_CONCEPTS:
            f_errors.append(
                Problem("invalid_concept", concept, "a derived state concept is not a current-state filter; use get_activity")
            )
        elif concept not in CONCEPT_RULES or entity not in concept_entities(concept):
            f_errors.append(Problem("invalid_concept", item_name(concept), "not a mapping-based concept of this entity"))
    errors = f_errors + fld_errors + sort_errors
    if errors:
        return rejected(errors)
    req_hash = normalized_hash(ctx, args)
    binding = ctx.binding(req_hash)
    start = 0
    if args.get("cursor") is not None:
        try:
            mode, pos = cursors.decode(args["cursor"], binding, int(ctx.now.timestamp()))
        except cursors.InvalidCursor:
            return rejected([Problem("invalid_cursor", "cursor", "invalid_cursor")])
        if mode != "offset" or type(pos) is not int or pos < 0:
            return rejected([Problem("invalid_cursor", "cursor", "invalid_cursor")])
        start = pos

    ent = entity_support(ctx, entity)
    if isinstance(ent, Problem):
        return unsupported([ent])
    check_setup(ctx)
    ok, missing = capability(ctx, "records.user" if entity == "user" else "records.find")
    if not ok:
        return status("setup_required", missing_requirements=list(missing))

    unsupported_items: list[Problem] = list(f_unsupported)
    restricted: list[Problem] = []
    missing_defs: list[str] = []
    clauses: list[Clause] = base_clauses(ent)

    # Concept (current-state) filter: needs setup_valid and the tenant mapping (§2, D-5C-6).
    concept_records: list[Any] = []
    parent_field: str | None = None  # Amendment C3-3a: interview instances are parent appointments only
    if concept_problem is not None:
        unsupported_items.append(concept_problem)
    elif concept is not None:
        if ctx.state != "setup_valid":
            return status("setup_revalidation_required", missing_requirements=list(ctx.setup.missing_requirements if ctx.setup else ()))
        # Triage B-1: the one concept definition (``capabilities.concept_requirements``), as in get_activity.
        concept_missing = concept_requirements(concept, ctx.profile, ctx.states)
        if concept_missing:
            return status("definition_missing", missing_requirements=list(concept_missing))
        if (
            concept == "interview_completed"
            and ctx.profile is not None
            and ctx.profile.settings.interview_completion_rule == "end_passed_not_cancelled"
        ):
            return unsupported([Problem("unsupported_concept", concept, "rule_not_renderable_as_current_state", None)])
        for group in CONCEPT_RULES[concept].groups:
            names = [c for e, c in group if e == entity]
            recs = [r for c in names for r in concept_mappings(ctx.profile, ctx.states, entity, c)]
            if not recs:  # the requirement is met on another entity only
                missing_defs.append(f"value_mapping:{entity}:{'|'.join(dict.fromkeys(c for _, c in group))}")
                continue
            clause = mapping_clause(ctx, ent, recs, concept)
            if isinstance(clause, Problem):
                unsupported_items.append(clause)
            else:
                clauses.append(clause)
                concept_records.extend(recs)
        if concept.startswith("interview_"):
            if ent.instance_filter is None:
                unsupported_items.append(Problem("unsupported_concept", concept, "invitee_copies_unresolved", "HV-Q9b"))
            else:
                clauses.append(Predicate(ent.instance_filter[0], "is_null"))
                parent_field = ent.instance_filter[0]

    # Filters
    order: list[Any] | None = None
    for spec in filters:
        res = resolver.resolve(spec.field)
        if res is None:
            missing_defs.append(missing_requirement(entity, spec.field))
            continue
        if res.sensitive:
            restricted.append(Problem("restricted_field", spec.field, "this field is restricted and cannot be filtered"))
            continue
        if entity == "job" and spec.field == "priority":
            if ctx.state != "setup_valid":
                return status("setup_revalidation_required", missing_requirements=list(ctx.setup.missing_requirements if ctx.setup else ()))
            pok, pmissing = capability(ctx, "jobs.priority")
            order = priority_order(ctx)
            if not pok or order is None:
                missing_defs.extend(m for m in pmissing if not m.startswith("state:"))
                continue
            values = spec.value if spec.op == "in" else (spec.value,)
            if spec.op != "is_null" and any(all(tagged(v) != tagged(o) for o in order) for v in values):
                restricted.append(
                    Problem("invalid_value", "priority", f"priority values must be among the tenant's ordered values: {order[:50]}")
                )
                continue
        pred = filter_predicate(ctx, ent, spec, res)
        if isinstance(pred, Problem):
            unsupported_items.append(pred)
        else:
            clauses.append(pred)

    # Output fields
    out_fields = list(requested) if requested is not None else list(DEFAULT_FIELDS[entity])
    out_resolved: list[Resolved] = []
    for name in out_fields:
        res = resolver.resolve(name)
        if res is None:
            if requested is not None:
                missing_defs.append(missing_requirement(entity, name))
            else:
                ctx.warnings.append(f"field {name} omitted: {missing_requirement(entity, name)} is not configured")
            continue
        if res.sensitive:
            if requested is not None:
                restricted.append(Problem("restricted_field", name, "this field is restricted and cannot be returned"))
            continue
        if isinstance(res.target, NestedField) and not ctx.support.syntax.nested_fields_verified:
            ctx.warnings.append(f"field {name} omitted: nested sub-field selection is unresolved (HV-Q13)")
            continue
        out_resolved.append(res)

    if sort is not None:
        sres = resolver.resolve(sort.field)
        if sres is not None and sres.sensitive:
            restricted.append(Problem("restricted_field", sort.field, "this field is restricted and cannot be sorted"))
        else:
            unsupported_items.append(Problem("unsupported_sort", sort.field, "no verified sort direction for /query orderBy", "HV-Q4"))
    if args.get("include_deleted") is True:
        unsupported_items.append(
            Problem("unsupported_filter", "include_deleted", "whether /query returns soft-deleted rows is undocumented", "HV-Q6")
        )

    if restricted:
        return rejected(restricted)
    if missing_defs:
        return status("definition_missing", missing_requirements=list(dict.fromkeys(missing_defs)))
    if unsupported_items:
        return unsupported(unsupported_items)
    if len(out_resolved) > ctx.support.syntax.max_fields:
        return unsupported([Problem("unsupported_value", "fields", "too many fields (HV-Q12)", "HV-Q12")])

    where = build_where(ctx, clauses)
    if isinstance(where, Problem):
        return unsupported([where])
    want_rank = entity == "job" and any(r.name == "priority" for r in out_resolved)
    if want_rank and order is None and capability(ctx, "jobs.priority")[0]:
        order = priority_order(ctx)
    extra = [ent.soft_delete_field] if ent.soft_delete_field else []
    extra += mapped_fields(concept_records)
    keep: Callable[[Mapping[str, Any]], bool] | None = None
    if parent_field is not None:
        extra.append(parent_field)
        field_name = parent_field

        def keep(row: Mapping[str, Any]) -> bool:  # defensive post-filter matching the server-side term
            return row.get(field_name) is None

    fields_param = render_fields(selection([r.target for r in out_resolved], extra))

    page = fetch(ctx, ent, where, fields_param, start, args.get("limit", DEFAULT_LIMIT), keep)
    records: list[dict[str, Any]] = []
    for row in page.rows:
        rec: dict[str, Any] = {r.name: shape_value(r.ctype, extract(r.target, row)) for r in out_resolved}
        if want_rank and order is not None:
            rec["priority_rank"] = priority_rank(order, extract(next(r.target for r in out_resolved if r.name == "priority"), row))
        rid = row.get("id")
        rec["source"] = {"system": "bullhorn", "entity": entity, "id": rid if type(rid) is int else None}
        records.append(rec)
    warnings = list(ctx.warnings)
    warnings.append(OFFSET_WARNING)
    if page.deleted:
        warnings.append(f"{page.deleted} deleted record(s) excluded")
    next_cursor = None
    if page.next_start is not None:
        next_cursor = cursors.encode(binding, "offset", page.next_start, int(ctx.now.timestamp()))
    return {
        "status": "ok",
        "entity": entity,
        "records": records,
        "count": len(records),
        "truncated": page.truncated,
        "complete": page.complete,
        "next_cursor": next_cursor,
        "consistency": "offset",
        "reporting_timezone": ctx.timezone,
        "provenance": ctx.provenance(req_hash),
        "warnings": warnings[:MAX_WARNINGS],
    }


def make_context(ident: Any, client: Any, env: Mapping[str, str], now: _dt.datetime, support: QuerySupport | None = None) -> ReadContext:
    return ReadContext(
        ident=ident,
        client=client,
        env=env,
        now=now,
        support=support or load_query_support(),
        canonical=load_canonical_catalog(),
        bullhorn=load_bullhorn_catalog(),
    )


# ---------------------------------------------------------------------- #
# Audit records (triage B-2): names and shapes only, never values or digests
# ---------------------------------------------------------------------- #

_SHAPE_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "list", dict: "object", tuple: "list"}


def value_shape(value: Any) -> dict[str, Any]:
    """``{type, length}`` of a caller-supplied value: never the value, never a digest of it."""
    kind = _SHAPE_TYPES.get(type(value), "other")
    length = len(value) if type(value) in (str, list, dict, tuple) else None
    return {"type": kind, "length": length}


def request_id(tenant: str | None, principal: str | None, args: Mapping[str, Any]) -> str | None:
    """The audit-only ``request_hmac`` (R2-B1; ``cursor`` excluded); ``None`` if it cannot be serialized."""
    try:
        return cursors.request_hmac(tenant or "local", principal or "unknown", {k: v for k, v in args.items() if k != "cursor"})
    except (TypeError, ValueError, RecursionError):
        return None


def _limit_entry(value: Any, maximum: int) -> Any:
    return value if type(value) is int and 1 <= value <= maximum else None


def audit_args(args: Mapping[str, Any], tenant: str | None, principal: str | None) -> dict[str, Any]:
    """Audit-safe ``find_records`` arguments (triage B-2).

    Kept: the entity, the concept and, after validation, allowlisted canonical field
    and operator names. Every value (filter values, the cursor) becomes ``{type, length}``.
    Anything that fails validation is only counted (``invalid_items``).
    """
    invalid = 0
    out: dict[str, Any] = {}
    entity = args.get("entity")
    allow: frozenset[str] = frozenset()
    if type(entity) is str and entity in FIND_ENTITIES:
        out["entity"] = entity
        allow = frozenset(f.name for f in load_canonical_catalog().entity(entity).fields)
    else:
        invalid += 1
    concept = args.get("concept")
    if concept is not None:
        if type(concept) is str and concept in CONCEPT_RULES:
            out["concept"] = concept
        else:
            invalid += 1
    filters = args.get("filters")
    kept: list[dict[str, Any]] = []
    if type(filters) is list:
        for f in filters:
            if (
                type(f) is dict
                and all(type(k) is str and k in FILTER_KEYS for k in f)
                and type(f.get("field")) is str
                and f["field"] in allow
                and type(f.get("op")) is str
                and f["op"] in OPERATORS
            ):
                entry: dict[str, Any] = {"field": f["field"], "op": f["op"]}
                if "value" in f:
                    entry["value"] = value_shape(f["value"])
                kept.append(entry)
            else:
                invalid += 1
    elif filters is not None:
        invalid += 1
    out["filters"] = kept
    fields = args.get("fields")
    if fields is not None:
        names = [n for n in fields if type(n) is str and n in allow] if type(fields) is list else []
        invalid += (len(fields) if type(fields) is list else 1) - len(names)
        out["fields"] = names
    sort = args.get("sort")
    if sort is not None:
        if (
            type(sort) is dict
            and all(type(k) is str and k in SORT_KEYS for k in sort)
            and type(sort.get("field")) is str
            and sort["field"] in allow
            and sort.get("direction", "asc") in ("asc", "desc")
        ):
            out["sort"] = {"field": sort["field"], "direction": sort.get("direction", "asc")}
        else:
            invalid += 1
    include_deleted = args.get("include_deleted", False)
    if type(include_deleted) is bool:
        out["include_deleted"] = include_deleted
    else:
        invalid += 1
    limit = _limit_entry(args.get("limit", DEFAULT_LIMIT), MAX_LIMIT)
    if limit is None:
        invalid += 1
    else:
        out["limit"] = limit
    if args.get("cursor") is not None:
        out["cursor"] = value_shape(args["cursor"])
    out["invalid_items"] = invalid
    out["request_hmac"] = request_id(tenant, principal, args)
    return out
