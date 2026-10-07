"""Structured filters over canonical names (Phase 5C, §3.1 and §3.3 steps 1-3).

No caller string ever becomes query text. A request is an AST of canonical
names, checked in three steps:

1. **Validate** (no tenant data needed): exact types, no unknown keys in a
   ``Filter`` / ``Sort``, operators by canonical type, bounded values.
   Unknown names are ``unknown_field`` with up to 3 suggestions drawn only from
   the entity's canonical allowlist (``difflib``, cutoff 0.75; NB-2 strict mode,
   D-5C-4). Raw Bullhorn names are never accepted.
2. **Resolve** each canonical name to a raw target (D-5C-5): the active profile
   record if it is ``valid``; else the packaged default, only if its raw source
   is in the latest discovery snapshot; else ``definition_missing``
   (``mapping:<entity>.<field>``). A sensitive raw source is ``restricted_field``;
   a template target is output-only (``unsupported_filter``).
3. **Check** the raw path and operator against the query-support entry, and
   build typed ``Literal`` values (``bullhorn/query_syntax.py`` renders them).

The Phase 3 translator is not used or modified.
"""

from __future__ import annotations

import difflib
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..bullhorn.query_syntax import STRING_RE, Literal
from ..tenant.timeutil import FilterError, parse_bound
from .bullhorn_catalog import BullhornCatalog, NestedField, RawField, RawTarget, TemplateField
from .canonical_catalog import CanonicalCatalog
from .errors import describe_value

FIND_ENTITIES = (
    "candidate", "job", "submission", "placement", "client_corporation", "client_contact", "appointment", "user",
)
OPERATORS = ("eq", "in", "gt", "gte", "lt", "lte", "is_null", "starts_with")
TYPE_OPS: Mapping[str, frozenset[str]] = {
    "id": frozenset({"eq", "in"}),
    "reference": frozenset({"eq", "in"}),
    "string": frozenset({"eq", "in", "is_null", "starts_with"}),
    "boolean": frozenset({"eq"}),
    "integer": frozenset({"eq", "in", "gt", "gte", "lt", "lte"}),
    "number": frozenset({"eq", "in", "gt", "gte", "lt", "lte"}),
    "datetime": frozenset({"gte", "lt", "is_null"}),
    "date": frozenset({"gte", "lt", "is_null"}),
    "text": frozenset(),
    "list": frozenset(),
}
FILTER_KEYS = frozenset({"field", "op", "value"})
SORT_KEYS = frozenset({"field", "direction"})
MAX_FILTERS = 10
MAX_IN = 50
MAX_STRING = 200
MAX_FIELDS = 30
MAX_ID = 2**63 - 1
SUGGEST_CUTOFF = 0.75
MAX_SUGGESTIONS = 3
MAX_NAME_CHARS = 64


@dataclass(frozen=True)
class Problem:
    """One validation error or unsupported item. Never carries a raw Bullhorn name or a filter value."""

    code: str
    item: str
    reason: str
    hv: str | None = None
    suggestions: tuple[str, ...] = ()

    def error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "item": self.item, "message": self.reason}
        if self.suggestions:
            out["suggestions"] = list(self.suggestions)
        return out

    def unsupported(self) -> dict[str, Any]:
        return {"code": self.code, "item": self.item, "reason": self.reason, "hv": self.hv}


def item_name(value: object) -> str:
    """A bounded display of a caller-supplied name (for ``item``)."""
    if isinstance(value, str) and len(value) <= MAX_NAME_CHARS and value.isascii() and value.isprintable():
        return value
    return describe_value(value)


def suggestions(name: object, allowlist: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
        return ()
    return tuple(difflib.get_close_matches(name, list(allowlist), n=MAX_SUGGESTIONS, cutoff=SUGGEST_CUTOFF))


def unknown_field(name: object, allowlist: Sequence[str], where: str) -> Problem:
    return Problem(
        "unknown_field",
        item_name(name),
        f"{where}: not a canonical field of this entity",
        suggestions=suggestions(name, allowlist),
    )


# ---------------------------------------------------------------------- #
# Step 1: validation
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilterSpec:
    field: str
    op: str
    value: Any = None  # normalized (bounds as epoch ms; strings NFC)
    index: int = 0


@dataclass(frozen=True)
class SortSpec:
    field: str
    direction: str


def _is_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ID


def _clean_string(value: object) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= MAX_STRING:
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        return None
    norm = unicodedata.normalize("NFC", value)
    return norm if 1 <= len(norm) <= MAX_STRING else None


def _scalar(ctype: str, value: object, timezone_name: str, label: str) -> tuple[Any, str | None]:
    """``(normalized, None)`` or ``(None, message)`` for one value of a canonical type."""
    if ctype in ("id", "reference"):
        return (value, None) if _is_id(value) else (None, f"{label} must be an int from 1 to 2^63-1, got {describe_value(value)}")
    if ctype == "string":
        norm = _clean_string(value)
        if norm is None:
            return None, f"{label} must be a string of 1 to {MAX_STRING} characters without control characters"
        return norm, None
    if ctype == "boolean":
        return (value, None) if type(value) is bool else (None, f"{label} must be a boolean, got {describe_value(value)}")
    if ctype in ("integer", "number"):
        if type(value) is int and -MAX_ID <= value <= MAX_ID:
            return value, None
        if type(value) is float and math.isfinite(value):
            return value, None
        return None, f"{label} must be an int or a finite float, got {describe_value(value)}"
    if ctype in ("datetime", "date"):
        try:
            return parse_bound(value, label, timezone_name), None
        except FilterError as exc:
            return None, str(exc)
    return None, f"{label}: values of type {ctype} cannot be filtered"


def validate_filters(
    raw: object, allowlist: Sequence[str], types: Mapping[str, str], timezone_name: str
) -> tuple[list[FilterSpec], list[Problem], list[Problem]]:
    """``(filters, errors, unsupported)``. ``errors`` are ``rejected_validation`` items."""
    errors: list[Problem] = []
    unsupported: list[Problem] = []
    out: list[FilterSpec] = []
    if raw is None:
        return out, errors, unsupported
    if type(raw) is not list:
        return out, [Problem("invalid_value", "filters", "filters must be a list of filter objects")], unsupported
    if len(raw) > MAX_FILTERS:
        return out, [Problem("invalid_value", "filters", f"at most {MAX_FILTERS} filters are allowed")], unsupported
    for i, entry in enumerate(raw):
        where = f"filters[{i}]"
        if type(entry) is not dict:
            errors.append(Problem("invalid_value", where, f"{where} must be an object with field, op and value"))
            continue
        extra = [k for k in entry if not (type(k) is str and k in FILTER_KEYS)]
        if extra:
            errors.append(Problem("invalid_value", where, f"{where}: unknown key(s); only field, op and value are allowed"))
            continue
        name, op = entry.get("field"), entry.get("op")
        if type(name) is not str or name not in allowlist:
            errors.append(unknown_field(name, allowlist, where))
            continue
        if type(op) is not str or op not in OPERATORS:
            errors.append(Problem("invalid_operator", name, f"{where}: op must be one of {list(OPERATORS)}"))
            continue
        ctype = types[name]
        allowed = TYPE_OPS.get(ctype, frozenset())
        if not allowed:
            unsupported.append(Problem("unsupported_filter", name, f"fields of type {ctype} cannot be filtered", None))
            continue
        if op not in allowed:
            reason = f"{where}: op {op!r} is not allowed for type {ctype} (allowed: {sorted(allowed)})"
            errors.append(Problem("invalid_operator", name, reason))
            continue
        has_value = "value" in entry
        value = entry.get("value")
        if op == "is_null":
            if has_value:
                errors.append(Problem("invalid_value", name, f"{where}: is_null takes no value"))
                continue
            out.append(FilterSpec(name, op, None, i))
            continue
        if not has_value:
            errors.append(Problem("invalid_value", name, f"{where}: a value is required"))
            continue
        if op == "in":
            if type(value) is not list or not 1 <= len(value) <= MAX_IN:
                errors.append(Problem("invalid_value", name, f"{where}: in takes a list of 1 to {MAX_IN} values"))
                continue
            normalized: list[Any] = []
            bad = None
            for j, v in enumerate(value):
                norm, msg = _scalar(ctype, v, timezone_name, f"{where}.value[{j}]")
                if msg is not None:
                    bad = msg
                    break
                normalized.append(norm)
            if bad is not None:
                errors.append(Problem("invalid_value", name, bad))
                continue
            out.append(FilterSpec(name, op, tuple(normalized), i))
            continue
        norm, msg = _scalar(ctype, value, timezone_name, f"{where}.value")
        if msg is not None:
            errors.append(Problem("invalid_value", name, msg))
            continue
        out.append(FilterSpec(name, op, norm, i))
    return out, errors, unsupported


def validate_sort(raw: object, allowlist: Sequence[str]) -> tuple[SortSpec | None, list[Problem]]:
    if raw is None:
        return None, []
    if type(raw) is not dict:
        return None, [Problem("invalid_value", "sort", "sort must be an object with field and direction")]
    if any(not (type(k) is str and k in SORT_KEYS) for k in raw):
        return None, [Problem("invalid_value", "sort", "sort: unknown key(s); only field and direction are allowed")]
    name, direction = raw.get("field"), raw.get("direction", "asc")
    if type(name) is not str or name not in allowlist:
        return None, [unknown_field(name, allowlist, "sort")]
    if type(direction) is not str or direction not in ("asc", "desc"):
        return None, [Problem("invalid_value", "sort", "sort.direction must be 'asc' or 'desc'")]
    return SortSpec(name, direction), []


def validate_fields(raw: object, allowlist: Sequence[str]) -> tuple[list[str] | None, list[Problem]]:
    if raw is None:
        return None, []
    if type(raw) is not list or not 1 <= len(raw) <= MAX_FIELDS:
        return None, [Problem("invalid_value", "fields", f"fields must be a list of 1 to {MAX_FIELDS} canonical field names")]
    errors: list[Problem] = []
    out: list[str] = []
    for i, name in enumerate(raw):
        if type(name) is not str or name not in allowlist:
            errors.append(unknown_field(name, allowlist, f"fields[{i}]"))
        elif name not in out:
            out.append(name)
    return (out if not errors else None), errors


# ---------------------------------------------------------------------- #
# Step 2: resolution (D-5C-5)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class Resolved:
    name: str
    ctype: str
    target: RawTarget
    sensitive: bool
    raw_custom: bool  # the raw source is a tenant custom field (query-support custom_fields)

    @property
    def template(self) -> bool:
        return isinstance(self.target, TemplateField)

    @property
    def path(self) -> str | None:
        if isinstance(self.target, RawField):
            return self.target.name
        if isinstance(self.target, NestedField):
            return f"{self.target.field}.{self.target.key}"
        return None


@dataclass
class Resolver:
    """Strict canonical -> raw resolution for one entity (D-5C-4, D-5C-5)."""

    entity: str
    canonical: CanonicalCatalog
    bullhorn: BullhornCatalog
    profile: Any  # TenantProfileV2 | None
    states: Mapping[str, str]
    snapshot_fields: frozenset[str] | None  # raw field names of the latest discovery snapshot
    _types: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for f in self.canonical.entity(self.entity).fields:
            self._types[f.name] = f.type
        if self.profile is not None:
            for rec in self.profile.field_mappings:
                if rec.active and rec.entity == self.entity and rec.kind == "custom":
                    self._types.setdefault(rec.field, rec.type or "string")

    @property
    def allowlist(self) -> tuple[str, ...]:
        return tuple(self._types)

    @property
    def types(self) -> Mapping[str, str]:
        return self._types

    def resolve(self, name: str) -> Resolved | None:
        """The raw target, or ``None`` (``definition_missing``: ``mapping:<entity>.<field>``)."""
        ctype = self._types[name]
        target: RawTarget | None = None
        if name == "id":
            target = RawField("id")  # every entity has ``id`` (S1 "Entity ids"; Phase 3 F-5)
        else:
            rec = self.profile.field_record(self.entity, name, active_only=True) if self.profile is not None else None
            if rec is not None and self.states.get(rec.key, rec.validation.state) == "valid":
                target = rec.target
            else:
                default = self.bullhorn.default_mappings(self.entity).get(name)
                if default is not None and self.snapshot_fields is not None and all(
                    s in self.snapshot_fields for s in default.sources
                ):
                    target = default
        if target is None:
            return None
        sensitive = any(self.bullhorn.is_sensitive(s) for s in target.sources)
        if isinstance(target, NestedField):
            sensitive = sensitive or self.bullhorn.is_sensitive(target.key)
        raw_custom = any(self.bullhorn.is_custom_field(s) for s in target.sources)
        return Resolved(name, ctype, target, sensitive, raw_custom)


def missing_requirement(entity: str, name: str) -> str:
    return f"mapping:{entity}.{name}"


# ---------------------------------------------------------------------- #
# Step 3: literals
# ---------------------------------------------------------------------- #


def literals_for(ctype: str, values: Sequence[Any], string_escape_verified: bool) -> tuple[Literal, ...] | str:
    """Typed literals, or a reason string (``unsupported_value``)."""
    out: list[Literal] = []
    for v in values:
        if ctype in ("datetime", "date"):
            out.append(Literal("ts", v))
        elif ctype == "boolean":
            out.append(Literal("bool", v))
        elif type(v) is int:
            out.append(Literal("int", v))
        elif type(v) is float:  # only integer literals are documented (HV-Q2); no float renderer exists
            return "decimal values have no verified literal form (HV-Q2)"
        elif type(v) is str:
            if not string_escape_verified and STRING_RE.fullmatch(v) is None:
                return "the value uses characters outside the verified set [A-Za-z0-9 ._@-] (HV-Q2: no documented escape rule)"
            out.append(Literal("str", v))
        else:
            return "the value has no verified literal form"
    return tuple(out)


def value_literal(value: Any) -> Literal | None:
    """A literal for a tenant value-mapping value (``str`` or ``int``)."""
    if type(value) is int:
        return Literal("int", value)
    if type(value) is str and STRING_RE.fullmatch(value) is not None:
        return Literal("str", value)
    return None
