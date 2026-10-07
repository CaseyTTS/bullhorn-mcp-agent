"""Translate between canonical names and raw Bullhorn names/records.

Resolution order for a canonical name: profile ``custom`` > profile
``standard`` override > catalog ``default_mappings``. Values pass through
unchanged (no coercion in Phase 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .bullhorn_catalog import BullhornCatalog, NestedField, RawField, RawTarget, TemplateField
from .canonical_catalog import KEY_RE, CanonicalCatalog
from .errors import SchemaError, UnknownCanonicalFieldError, describe_value, path_segment
from .mapping_profile import MappingProfile

_MISSING = object()


@dataclass(frozen=True)
class FieldResolution:
    """Result of ``canonical_to_raw``."""

    bullhorn_entity: str | None
    raw_fields: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def fields_param(self) -> str:
        """The raw fields as a Bullhorn ``fields=`` value."""
        return ",".join(self.raw_fields)


@dataclass(frozen=True)
class CanonicalRecord:
    """Result of ``raw_to_canonical``."""

    entity: str
    bullhorn_entity: str | None
    fields: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unmapped: dict[Any, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "bullhorn_entity": self.bullhorn_entity,
            "fields": dict(self.fields),
            "custom": dict(self.custom),
            "missing": list(self.missing),
            "unmapped": dict(self.unmapped),
        }


class FieldTranslator:
    """Canonical <-> raw translation for one catalog pair and (optional) profile."""

    def __init__(
        self,
        canonical_catalog: CanonicalCatalog,
        bullhorn_catalog: BullhornCatalog,
        profile: MappingProfile | None = None,
    ) -> None:
        self.canonical = canonical_catalog
        self.bullhorn = bullhorn_catalog
        self.profile = profile

    # ------------------------------------------------------------------ #

    def _check_entity(self, entity: str) -> None:
        if not self.canonical.has_entity(entity):
            raise UnknownCanonicalFieldError(f"Unknown canonical entity: {describe_value(entity)}")

    def shared_mappings(self, entity: str) -> dict[str, RawTarget]:
        """Effective standard mappings (defaults + profile overrides), canonical order."""
        self._check_entity(entity)
        defaults = self.bullhorn.default_mappings(entity)
        overrides: Mapping[str, RawTarget] = {}
        ep = self.profile.entity(entity) if self.profile else None
        if ep is not None:
            overrides = ep.standard
        out: dict[str, RawTarget] = {}
        for name in self.canonical.entity(entity).field_names:
            if name in overrides:
                out[name] = overrides[name]
            elif name in defaults:
                out[name] = defaults[name]
        return out

    def custom_mappings(self, entity: str) -> dict[str, RawTarget]:
        """Tenant-added custom mappings for an entity (empty with no profile)."""
        self._check_entity(entity)
        ep = self.profile.entity(entity) if self.profile else None
        if ep is None:
            return {}
        return {name: cm.target for name, cm in ep.custom.items()}

    # ------------------------------------------------------------------ #

    def canonical_to_raw(self, entity: str, fields: list[str] | None = None) -> FieldResolution:
        """Resolve canonical names to the raw Bullhorn fields needed to fetch them.

        ``fields=None`` means every mapped field. Unknown entities, and names that
        are neither canonical fields nor valid custom-name candidates, raise
        ``UnknownCanonicalFieldError``. Known names with no mapping go to
        ``unresolved``.
        """
        self._check_entity(entity)
        if fields is not None and (not isinstance(fields, (list, tuple)) or not all(isinstance(f, str) for f in fields)):
            raise SchemaError("fields must be a list of canonical field names or None")
        canonical_names = self.canonical.entity(entity).field_names
        shared = self.shared_mappings(entity)
        custom = self.custom_mappings(entity)
        bh_entity = self.bullhorn.bullhorn_entity_for(entity)

        if fields is None:
            if bh_entity is None:
                requested = list(canonical_names)
            else:
                requested = list(shared) + [c for c in custom if c not in shared]
        else:
            requested = list(fields)

        for name in requested:
            if name in canonical_names or name in custom:
                continue
            # A snake_case name that is not canonical may be a tenant custom name
            # that this profile does not define: it is reported as unresolved.
            if not KEY_RE.fullmatch(name):
                raise UnknownCanonicalFieldError(f"Unknown canonical field: {entity}.{path_segment(name)}")

        if bh_entity is None:
            return FieldResolution(bullhorn_entity=None, raw_fields=[], unresolved=_dedupe(requested))

        raw: list[str] = ["id"]
        unresolved: list[str] = []
        for name in requested:
            target = custom.get(name) or shared.get(name)
            if target is None:
                unresolved.append(name)
                continue
            raw.extend(target.sources)
        return FieldResolution(bullhorn_entity=bh_entity, raw_fields=_dedupe(raw), unresolved=_dedupe(unresolved))

    # ------------------------------------------------------------------ #

    def raw_to_canonical(self, entity: str, record: dict[str, Any]) -> CanonicalRecord:
        """Translate one raw Bullhorn record to canonical form.

        Never raises ``KeyError``/``TypeError`` on hostile shapes; a non-dict
        record raises ``SchemaError``.

        Nothing is silently dropped. Consumption is tracked at two levels:
        whole raw keys (plain fields and template placeholders) and
        ``(outer, key)`` pairs (nested lookups). A key consumed whole is left
        out of ``unmapped``. A dict-valued key consumed only through pairs
        appears in ``unmapped`` as the residual dict of its unconsumed
        sub-keys (values verbatim, order kept), and is omitted only when that
        residual is empty. Any other key appears in ``unmapped`` unchanged.
        A mapping that lands in ``missing`` consumes nothing.
        """
        self._check_entity(entity)
        if not isinstance(record, dict):
            raise SchemaError(f"record must be a dict, got {type(record).__name__}")
        shared = self.shared_mappings(entity)
        custom = self.custom_mappings(entity)
        whole: set[Any] = set()
        pairs: dict[Any, set[Any]] = {}
        out_fields: dict[str, Any] = {}
        out_custom: dict[str, Any] = {}
        missing: list[str] = []

        for name in self.canonical.entity(entity).field_names:
            if name not in shared:
                missing.append(name)
                continue
            value = _extract(shared[name], record, whole, pairs)
            if value is _MISSING:
                missing.append(name)
            else:
                out_fields[name] = value

        for name, target in custom.items():
            value = _extract(target, record, whole, pairs)
            if value is _MISSING:
                missing.append(name)
            else:
                out_custom[name] = value

        unmapped: dict[Any, Any] = {}
        for k, v in record.items():
            if k in whole:
                continue
            used_keys = pairs.get(k)
            if used_keys and isinstance(v, dict):
                residual = {sk: sv for sk, sv in v.items() if sk not in used_keys}
                if residual:
                    unmapped[k] = residual
            else:
                unmapped[k] = v
        return CanonicalRecord(
            entity=entity,
            bullhorn_entity=self.bullhorn.bullhorn_entity_for(entity),
            fields=out_fields,
            custom=out_custom,
            missing=missing,
            unmapped=unmapped,
        )

    def raw_to_canonical_many(self, entity: str, records: list[dict[str, Any]]) -> list[CanonicalRecord]:
        if not isinstance(records, list):
            raise SchemaError(f"records must be a list, got {type(records).__name__}")
        return [self.raw_to_canonical(entity, r) for r in records]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _extract(target: RawTarget, record: dict[str, Any], whole: set[Any], pairs: dict[Any, set[Any]]) -> Any:
    """Return the value (``_MISSING`` when unavailable) and record what it consumed.

    Consumption is recorded only on success.
    """
    if isinstance(target, RawField):
        if target.name in record:
            whole.add(target.name)
            return record[target.name]
        return _MISSING
    if isinstance(target, NestedField):
        outer = record.get(target.field, _MISSING)
        if not isinstance(outer, dict) or target.key not in outer:
            return _MISSING
        pairs.setdefault(target.field, set()).add(target.key)
        return outer[target.key]
    if isinstance(target, TemplateField):
        pieces: list[str] = []
        for literal, name in target.parts:
            pieces.append(literal)
            if name is None:
                continue
            value = record.get(name)
            if value is None:
                return _MISSING
            pieces.append(str(value))
        whole.update(target.placeholders)
        return "".join(pieces)
    return _MISSING  # pragma: no cover - exhaustive over RawTarget
