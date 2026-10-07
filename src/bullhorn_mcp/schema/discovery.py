"""Tenant schema discovery and draft-profile building.

By default discovery issues ``/meta/{entity}`` calls only (through a
``MetaSource``). Sample values are fetched only when the caller opts in with
``include_sample_values=True`` *and* supplies an explicit ``sample_source``;
sensitive fields are never sampled and every sampled value is redacted.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..bullhorn.meta import EntityMeta, MetaSource
from .bullhorn_catalog import BullhornCatalog, RawTarget, is_raw_name
from .canonical_catalog import CanonicalCatalog, load_canonical_catalog
from .errors import SchemaError, describe_value
from .mapping_profile import EntityProfile, MappingProfile, UnmappedField

SampleSource = Callable[[str, list[str]], Mapping[str, Any] | None]
"""``sample_source(bullhorn_entity, field_names) -> one raw record (or None)``."""

# Masking regexes are deliberately Unicode-wide (no re.ASCII; F-12 #4 exemption): they must catch non-ASCII digits/emails.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")  # applied with fullmatch
_DIGIT_RE = re.compile(r"\d")  # Unicode \d: masks every decimal digit, not just 0-9


def redact_sample(value: Any) -> Any:
    """Redact one sampled value per the Phase 3 table. ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "<boolean>"
    if isinstance(value, (int, float, Decimal)):
        return "<number>"
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        return "<date>"
    if isinstance(value, dict):
        return "<object>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "<list>"
    if isinstance(value, str):
        stripped = value.strip()
        if _EMAIL_RE.fullmatch(stripped):
            return "a***@***"
        if len(_DIGIT_RE.findall(value)) >= 4:
            return _DIGIT_RE.sub("*", value)
        if len(value) <= 3:
            return "***"
        return value[:2] + "***"
    return "<object>"


# ---------------------------------------------------------------------- #
# Report model
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class BrokenMapping:
    canonical_field: str
    origin: str  # "catalog" | "profile_standard" | "profile_custom"
    target: Any
    missing_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_field": self.canonical_field,
            "origin": self.origin,
            "target": self.target,
            "missing_sources": list(self.missing_sources),
        }


@dataclass(frozen=True)
class MappedCustomField:
    field: str
    label: str
    canonical_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "label": self.label, "canonical_fields": list(self.canonical_fields)}


@dataclass(frozen=True)
class UnmappedCustomField:
    field: str
    label: str
    data_type: str | None
    field_type: str | None
    options: tuple[tuple[Any, Any], ...] | None
    required: bool
    read_only: bool
    appears_configured: bool
    sensitive: bool
    sample: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "data_type": self.data_type,
            "field_type": self.field_type,
            "options": None if self.options is None else [{"value": v, "label": lbl} for v, lbl in self.options],
            "required": self.required,
            "read_only": self.read_only,
            "appears_configured": self.appears_configured,
            "sensitive": self.sensitive,
            "sample": self.sample,
        }


@dataclass(frozen=True)
class UnrecognizedField:
    field: str
    label: str
    data_type: str | None
    field_type: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "label": self.label, "data_type": self.data_type, "field_type": self.field_type}


@dataclass(frozen=True)
class EntityDiscovery:
    canonical_entity: str
    bullhorn_entity: str
    standard_present: tuple[str, ...] = ()
    standard_missing_in_tenant: tuple[str, ...] = ()
    mappings_broken: tuple[BrokenMapping, ...] = ()
    custom_mapped: tuple[MappedCustomField, ...] = ()
    custom_unmapped: tuple[UnmappedCustomField, ...] = ()
    other_unrecognized: tuple[UnrecognizedField, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_entity": self.canonical_entity,
            "bullhorn_entity": self.bullhorn_entity,
            "standard_present": list(self.standard_present),
            "standard_missing_in_tenant": list(self.standard_missing_in_tenant),
            "mappings_broken": [m.to_dict() for m in self.mappings_broken],
            "custom_mapped": [m.to_dict() for m in self.custom_mapped],
            "custom_unmapped": [m.to_dict() for m in self.custom_unmapped],
            "other_unrecognized": [m.to_dict() for m in self.other_unrecognized],
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass(frozen=True)
class DiscoveryReport:
    entities: tuple[EntityDiscovery, ...] = field(default_factory=tuple)

    def entity(self, name: str) -> EntityDiscovery | None:
        for e in self.entities:
            if name in (e.canonical_entity, e.bullhorn_entity):
                return e
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"entities": [e.to_dict() for e in self.entities]}


# ---------------------------------------------------------------------- #
# Discoverer
# ---------------------------------------------------------------------- #


class SchemaDiscoverer:
    def __init__(
        self,
        meta_source: MetaSource,
        canonical_catalog: CanonicalCatalog,
        bullhorn_catalog: BullhornCatalog,
        profile: MappingProfile | None = None,
    ) -> None:
        self.meta_source = meta_source
        self.canonical = canonical_catalog
        self.bullhorn = bullhorn_catalog
        self.profile = profile

    def _resolve_targets(self, entities: Sequence[str] | None) -> list[str]:
        """Return Bullhorn entity names to discover (catalog entities only)."""
        if entities is None:
            return list(self.bullhorn.entities)
        if isinstance(entities, str) or not isinstance(entities, (list, tuple)):
            raise SchemaError("entities must be a list of entity names or None")
        out: list[str] = []
        for name in entities:
            if not isinstance(name, str):
                raise SchemaError(f"entity name must be a string, got {type(name).__name__}")
            if name in self.bullhorn.entities:
                bh: str | None = name
            elif self.canonical.has_entity(name):
                bh = self.bullhorn.bullhorn_entity_for(name)
                if bh is None:
                    continue  # canonical entity with no Bullhorn binding: not a catalog entity
            else:
                raise SchemaError(f"unknown entity {describe_value(name)}")
            if bh is not None and bh not in out:
                out.append(bh)
        return out

    def discover(
        self,
        entities: Sequence[str] | None = None,
        include_sample_values: bool = False,
        sample_source: SampleSource | None = None,
    ) -> DiscoveryReport:
        results: list[EntityDiscovery] = []
        for bh in self._resolve_targets(entities):
            canon = self.bullhorn.canonical_entity_for(bh)
            assert canon is not None
            try:
                meta = self.meta_source.get_entity_meta(bh)
            except Exception as exc:  # one failing entity never aborts the others
                results.append(EntityDiscovery(canonical_entity=canon, bullhorn_entity=bh, error=f"{type(exc).__name__}: {exc}"))
                continue
            results.append(self._discover_entity(canon, bh, meta, include_sample_values, sample_source))
        return DiscoveryReport(entities=tuple(results))

    def _discover_entity(
        self,
        canon: str,
        bh: str,
        meta: EntityMeta,
        include_sample_values: bool,
        sample_source: SampleSource | None,
    ) -> EntityDiscovery:
        warnings = list(meta.warnings)
        # F-12 #3: only names that pass the single raw-name definition are classified.
        meta_names = {n for n in meta.field_names if is_raw_name(n)}
        standard = self.bullhorn.standard_fields(bh)
        standard_set = set(standard)

        ep: EntityProfile | None = self.profile.entity(canon) if self.profile else None
        defaults = self.bullhorn.default_mappings(canon)

        # (canonical name, origin, target) for every effective mapping.
        effective: list[tuple[str, str, RawTarget]] = []
        for name in self.canonical.entity(canon).field_names:
            if ep is not None and name in ep.standard:
                effective.append((name, "profile_standard", ep.standard[name]))
            elif name in defaults:
                effective.append((name, "catalog", defaults[name]))
        if ep is not None:
            for name, cm in ep.custom.items():
                effective.append((name, "profile_custom", cm.target))

        broken: list[BrokenMapping] = []
        profile_sources: dict[str, list[str]] = {}
        for name, origin, target in effective:
            missing_sources = tuple(s for s in target.sources if s not in meta_names)
            if missing_sources:
                broken.append(BrokenMapping(name, origin, target.to_data(), missing_sources))
            if origin != "catalog":
                for s in target.sources:
                    profile_sources.setdefault(s, []).append(name)

        custom_mapped: list[MappedCustomField] = []
        custom_unmapped: list[UnmappedCustomField] = []
        other: list[UnrecognizedField] = []
        invalid_names: list[str] = []
        for fm in meta.fields:
            if not is_raw_name(fm.name):
                # Reported, never routed toward standard/custom buckets, samples or drafts.
                invalid_names.append(fm.name)
                other.append(UnrecognizedField(fm.name, fm.label, fm.data_type, fm.field_type))
                continue
            if self.bullhorn.is_custom_field(fm.name):
                if fm.name in profile_sources:
                    custom_mapped.append(MappedCustomField(fm.name, fm.label, tuple(profile_sources[fm.name])))
                else:
                    custom_unmapped.append(
                        UnmappedCustomField(
                            field=fm.name,
                            label=fm.label,
                            data_type=fm.data_type,
                            field_type=fm.field_type,
                            options=_report_options(fm.name, fm.options, warnings),
                            required=fm.required,
                            read_only=fm.read_only,
                            appears_configured=fm.label != fm.name,
                            sensitive=self.bullhorn.is_sensitive(fm.name),
                        )
                    )
            elif fm.name not in standard_set and fm.name not in profile_sources:
                other.append(UnrecognizedField(fm.name, fm.label, fm.data_type, fm.field_type))

        if invalid_names:
            shown = ", ".join(describe_value(n) for n in invalid_names[:10])
            more = ", ..." if len(invalid_names) > 10 else ""
            warnings.append(
                f"{len(invalid_names)} field name(s) are not valid raw field names and were not classified: {shown}{more}"
            )

        if include_sample_values and sample_source is not None:
            custom_unmapped = self._attach_samples(bh, custom_unmapped, sample_source, warnings)

        return EntityDiscovery(
            canonical_entity=canon,
            bullhorn_entity=bh,
            standard_present=tuple(f for f in standard if f in meta_names),
            standard_missing_in_tenant=tuple(f for f in standard if f not in meta_names),
            mappings_broken=tuple(broken),
            custom_mapped=tuple(custom_mapped),
            custom_unmapped=tuple(custom_unmapped),
            other_unrecognized=tuple(other),
            warnings=tuple(warnings),
        )

    def _attach_samples(
        self,
        bh: str,
        fields: list[UnmappedCustomField],
        sample_source: SampleSource,
        warnings: list[str],
    ) -> list[UnmappedCustomField]:
        wanted = [f.field for f in fields if not f.sensitive and not self.bullhorn.is_sensitive(f.field)]
        if not wanted:
            return fields
        try:
            record = sample_source(bh, list(wanted))
        except Exception as exc:
            warnings.append(f"sample fetch failed: {type(exc).__name__}: {exc}")
            return fields
        if record is None:
            return fields
        if not isinstance(record, Mapping):
            warnings.append("sample source returned a non-mapping; samples ignored")
            return fields
        wanted_set = set(wanted)
        out: list[UnmappedCustomField] = []
        for f in fields:
            if f.field in wanted_set and f.field in record:
                out.append(_with_sample(f, redact_sample(record[f.field])))
            else:
                out.append(f)
        return out


def _is_non_finite(value: Any) -> bool:
    return isinstance(value, float) and not math.isfinite(value)


def _report_options(
    name: str, options: tuple[tuple[Any, Any], ...] | None, warnings: list[str]
) -> tuple[tuple[Any, Any], ...] | None:
    """Keep the report strict JSON (F-11 #4): non-finite numbers drop the field's options."""
    if options is None:
        return None
    if any(_is_non_finite(v) or _is_non_finite(lbl) for v, lbl in options):
        warnings.append(f"field {describe_value(name)} has non-finite option values; options omitted")
        return None
    return options


def _with_sample(f: UnmappedCustomField, sample: Any) -> UnmappedCustomField:
    return UnmappedCustomField(
        field=f.field,
        label=f.label,
        data_type=f.data_type,
        field_type=f.field_type,
        options=f.options,
        required=f.required,
        read_only=f.read_only,
        appears_configured=f.appears_configured,
        sensitive=f.sensitive,
        sample=sample,
    )


# ---------------------------------------------------------------------- #
# Draft profile
# ---------------------------------------------------------------------- #


_UNSUPPORTED = object()


def _draft_label(label: Any) -> Any:
    """Normalize a meta option label to the profile's ``str | None`` form (F-10 #1)."""
    if label is None or isinstance(label, str):
        return label
    if isinstance(label, bool):  # before int: bool subclasses int
        return "true" if label else "false"
    if isinstance(label, int):
        return str(label)
    if isinstance(label, float):
        # For floats, str() is Python's shortest round-trip form (identical to repr): 1.5 -> "1.5".
        return str(label) if math.isfinite(label) else None
    return _UNSUPPORTED


def _draft_options(options: tuple[tuple[Any, Any], ...] | None) -> tuple[tuple[Any, Any], ...] | None:
    """Meta options -> profile options (F-10).

    Labels are normalized to ``str | None``. A non-finite float value, or any
    value/label outside the supported scalar types, drops the whole options
    list for that field to ``None`` (the field itself is still listed).
    """
    if options is None:
        return None
    out: list[tuple[Any, Any]] = []
    for value, label in options:
        if value is not None and not isinstance(value, (str, int, float, bool)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        draft_label = _draft_label(label)
        if draft_label is _UNSUPPORTED:
            return None
        out.append((value, draft_label))
    return tuple(out)


def build_draft_profile(
    report: DiscoveryReport,
    tenant: str | None,
    existing: MappingProfile | None = None,
    *,
    generated_at: str | None = None,
    canonical: CanonicalCatalog | None = None,
) -> MappingProfile:
    """Build a draft profile from a discovery report. Pure and deterministic.

    ``standard`` keeps only ``existing`` overrides (catalog defaults are not
    copied), ``custom`` is preserved verbatim, and ``unmapped_bullhorn_fields``
    is filled from ``custom_unmapped`` sorted by field name. An entity whose
    discovery errored keeps the existing unmapped list. ``generated_at`` is
    taken from the argument, else from ``existing``.
    """
    canonical = canonical or load_canonical_catalog()
    entities: dict[str, EntityProfile] = {}
    discovered = {e.canonical_entity: e for e in report.entities}
    names: list[str] = sorted(set(discovered) | set(existing.entities if existing else ()))
    for name in names:
        prev = existing.entity(name) if existing else None
        disc = discovered.get(name)
        if disc is not None and disc.error is None:
            unmapped = tuple(
                UnmappedField(
                    field=u.field,
                    label=u.label,
                    data_type=u.data_type,
                    field_type=u.field_type,
                    options=_draft_options(u.options),
                    required=u.required,
                    read_only=u.read_only,
                )
                for u in sorted(disc.custom_unmapped, key=lambda u: u.field)
            )
        else:
            unmapped = prev.unmapped_bullhorn_fields if prev else ()
        entities[name] = EntityProfile(
            standard=dict(prev.standard) if prev else {},
            custom=dict(prev.custom) if prev else {},
            unmapped_bullhorn_fields=unmapped,
        )
    draft = MappingProfile(
        version=1,
        tenant=tenant,
        generated_at=generated_at if generated_at is not None else (existing.generated_at if existing else None),
        entities=entities,
    )
    # Round-trip through validation so a draft can never be an invalid profile.
    return MappingProfile.from_dict(draft.to_dict(), canonical, source="<draft>")
