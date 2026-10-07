"""Discovery snapshots and the drift (revalidation) report (TS-5).

Discovery is metadata-only: it goes through the Phase 3 ``SchemaDiscoverer``
over a ``MetaSource`` (``MetaDiscovery`` in production). The snapshot keeps,
per entity and field: name, label, data_type, field_type, required,
read_only and options (only when the meta response contained them; HV-A2).
No sample values are ever fetched or stored (D-4A-12).

The drift report never mutates mappings.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..bullhorn.meta import EntityMeta, MetaSource
from ..schema.bullhorn_catalog import BullhornCatalog, is_raw_name, load_bullhorn_catalog
from ..schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from ..schema.discovery import SchemaDiscoverer
from ..schema.errors import describe_value, truncate_text
from .profile_v2 import TenantProfileV2, tagged
from .store import SetupStoreError

MAX_WARNINGS_PER_ENTITY = 100
MAX_ERROR_CHARS = 300
MAX_LABEL_CHARS = 200
MAX_TYPE_CHARS = 100
MAX_OPTIONS = 1000
MAX_OPTION_CHARS = 200
MAX_REPORT_ITEMS = 200
SNAPSHOT_ATTRIBUTES = ("label", "data_type", "field_type", "required", "read_only", "options")

Option = tuple[Any, Any]


# ---------------------------------------------------------------------- #
# Snapshot model
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldSnapshot:
    name: str
    label: str
    data_type: str | None
    field_type: str | None
    required: bool
    read_only: bool
    options: tuple[Option, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "data_type": self.data_type,
            "field_type": self.field_type,
            "required": self.required,
            "read_only": self.read_only,
            "options": None if self.options is None else [[v, lbl] for v, lbl in self.options],
        }

    def option_values(self) -> set[tuple[str, Any]] | None:
        if self.options is None:
            return None
        return {tagged(v) for v, _ in self.options}


@dataclass(frozen=True)
class EntitySnapshot:
    canonical_entity: str
    bullhorn_entity: str
    fields: Mapping[str, FieldSnapshot] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str | None = None
    meta_unusable: bool = False

    @property
    def usable(self) -> bool:
        return self.error is None and not self.meta_unusable

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_entity": self.canonical_entity,
            "bullhorn_entity": self.bullhorn_entity,
            "fields": {name: f.to_dict() for name, f in self.fields.items()},
            "warnings": list(self.warnings),
            "error": self.error,
            "meta_unusable": self.meta_unusable,
        }


@dataclass(frozen=True)
class DiscoverySnapshot:
    checked_at: str
    rest_url_fingerprint: str | None
    entities: Mapping[str, EntitySnapshot]  # keyed by canonical entity

    def entity(self, canonical: str) -> EntitySnapshot | None:
        return self.entities.get(canonical)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "rest_url_fingerprint": self.rest_url_fingerprint,
            "entities": {name: e.to_dict() for name, e in self.entities.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> DiscoverySnapshot:
        """Rebuild a stored snapshot. Any shape problem is a ``SetupStoreError``."""
        try:
            return _snapshot_from_dict(data)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SetupStoreError(f"setup store: discovery snapshot is corrupt ({type(exc).__name__})") from exc


def _req_str(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _opt_str(value: Any) -> str | None:
    return None if value is None else _req_str(value)


def _req_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected a boolean")
    return value


def _snapshot_from_dict(data: Any) -> DiscoverySnapshot:
    if not isinstance(data, dict):
        raise TypeError("snapshot must be an object")
    entities: dict[str, EntitySnapshot] = {}
    raw_entities = data["entities"]
    if not isinstance(raw_entities, dict):
        raise TypeError("entities must be an object")
    for canon, body in raw_entities.items():
        if not isinstance(body, dict) or not isinstance(body["fields"], dict):
            raise TypeError("entity must be an object")
        fields: dict[str, FieldSnapshot] = {}
        for name, f in body["fields"].items():
            if not isinstance(f, dict) or not is_raw_name(name) or f["name"] != name:
                raise ValueError("bad field")
            options = f["options"]
            parsed_options: tuple[Option, ...] | None = None
            if options is not None:
                if not isinstance(options, list):
                    raise TypeError("options must be a list")
                parsed_options = tuple((o[0], o[1]) for o in options if isinstance(o, list) and len(o) == 2)
                if len(parsed_options) != len(options):
                    raise ValueError("bad option")
            fields[name] = FieldSnapshot(
                name=name,
                label=_req_str(f["label"]),
                data_type=_opt_str(f["data_type"]),
                field_type=_opt_str(f["field_type"]),
                required=_req_bool(f["required"]),
                read_only=_req_bool(f["read_only"]),
                options=parsed_options,
            )
        warnings = body["warnings"]
        if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
            raise TypeError("warnings must be strings")
        entities[_req_str(canon)] = EntitySnapshot(
            canonical_entity=_req_str(body["canonical_entity"]),
            bullhorn_entity=_req_str(body["bullhorn_entity"]),
            fields=fields,
            warnings=tuple(warnings),
            error=_opt_str(body["error"]),
            meta_unusable=_req_bool(body["meta_unusable"]),
        )
    rest_fp = data.get("rest_url_fingerprint")
    return DiscoverySnapshot(
        checked_at=_req_str(data["checked_at"]),
        rest_url_fingerprint=_opt_str(rest_fp),
        entities=entities,
    )


def load_snapshot(discovery_doc: Mapping[str, Any] | None) -> DiscoverySnapshot | None:
    """The snapshot inside ``discovery/latest.json`` (``None`` when there is none)."""
    if not discovery_doc or discovery_doc.get("snapshot") is None:
        return None
    return DiscoverySnapshot.from_dict(discovery_doc["snapshot"])


# ---------------------------------------------------------------------- #
# Discovery
# ---------------------------------------------------------------------- #


class _RecordingMetaSource:
    """Wraps a ``MetaSource`` so each entity is fetched at most once and the parsed meta is kept."""

    def __init__(self, inner: MetaSource) -> None:
        self._inner = inner
        self.metas: dict[str, EntityMeta] = {}
        self.failures: dict[str, BaseException] = {}

    def get_entity_meta(self, entity: str) -> EntityMeta:
        if entity in self.failures:
            raise self.failures[entity]
        if entity not in self.metas:
            try:
                self.metas[entity] = self._inner.get_entity_meta(entity)
            except Exception as exc:
                self.failures[entity] = exc
                raise
        return self.metas[entity]


def _bounded_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    items = [truncate_text(str(w), MAX_ERROR_CHARS) for w in warnings]
    if len(items) > MAX_WARNINGS_PER_ENTITY:
        extra = len(items) - MAX_WARNINGS_PER_ENTITY
        items = items[:MAX_WARNINGS_PER_ENTITY] + [f"... and {extra} more warnings"]
    return tuple(items)


def _snapshot_option(value: Any, label: Any) -> Option | None:
    """Keep JSON-safe, finite scalars only; strings are bounded."""
    out: list[Any] = []
    for part in (value, label):
        if part is None or isinstance(part, bool) or type(part) is int:
            if type(part) is int and part.bit_length() > 63:
                return None
            out.append(part)
        elif isinstance(part, float):
            if not math.isfinite(part):
                return None
            out.append(part)
        elif isinstance(part, str):
            out.append(part if len(part) <= MAX_OPTION_CHARS else None)
            if out[-1] is None:
                return None
        else:
            return None
    return (out[0], out[1])


def _field_snapshot(fm: Any, warnings: list[str]) -> FieldSnapshot:
    options: tuple[Option, ...] | None = None
    if fm.options is not None:
        if len(fm.options) > MAX_OPTIONS:
            warnings.append(f"field {describe_value(fm.name)} has more than {MAX_OPTIONS} options; options omitted")
        else:
            collected = [_snapshot_option(value, label) for value, label in fm.options]
            if any(opt is None for opt in collected):
                warnings.append(f"field {describe_value(fm.name)} has options that cannot be recorded; options omitted")
            else:
                options = tuple(opt for opt in collected if opt is not None)
    return FieldSnapshot(
        name=fm.name,
        label=truncate_text(fm.label, MAX_LABEL_CHARS) if isinstance(fm.label, str) else fm.name,
        data_type=None if fm.data_type is None else truncate_text(str(fm.data_type), MAX_TYPE_CHARS),
        field_type=None if fm.field_type is None else truncate_text(str(fm.field_type), MAX_TYPE_CHARS),
        required=fm.required is True,
        read_only=fm.read_only is True,
        options=options,
    )


def entity_snapshot(canonical: str, bullhorn: str, meta: EntityMeta | None, error: str | None) -> EntitySnapshot:
    if meta is None or error is not None:
        return EntitySnapshot(
            canonical_entity=canonical,
            bullhorn_entity=bullhorn,
            error=truncate_text(error or "metadata unavailable", MAX_ERROR_CHARS),
        )
    warnings = list(meta.warnings)
    fields: dict[str, FieldSnapshot] = {}
    for fm in meta.fields:
        if is_raw_name(fm.name):
            fields[fm.name] = _field_snapshot(fm, warnings)
    # NB-7: a structural warning plus zero fields means the metadata cannot be trusted.
    unusable = bool(meta.warnings) and not meta.fields
    return EntitySnapshot(
        canonical_entity=canonical,
        bullhorn_entity=bullhorn,
        fields=fields,
        warnings=_bounded_warnings(warnings),
        meta_unusable=unusable,
    )


def discover(
    meta_source: MetaSource,
    entities: Sequence[str] | None,
    checked_at: str,
    rest_url_fingerprint: str | None,
    canonical: CanonicalCatalog | None = None,
    bullhorn: BullhornCatalog | None = None,
) -> DiscoverySnapshot:
    """Metadata-only discovery of the catalog entities (``SchemaError`` for an invalid entity list)."""
    canonical = canonical or load_canonical_catalog()
    bullhorn = bullhorn or load_bullhorn_catalog()
    recorder = _RecordingMetaSource(meta_source)
    report = SchemaDiscoverer(recorder, canonical, bullhorn).discover(entities)
    out: dict[str, EntitySnapshot] = {}
    for ent in report.entities:
        meta = recorder.metas.get(ent.bullhorn_entity)
        snap = entity_snapshot(ent.canonical_entity, ent.bullhorn_entity, meta, ent.error)
        if snap.error is None:
            # The Phase 3 report adds its own classification warnings (e.g. invalid names).
            snap = EntitySnapshot(
                canonical_entity=snap.canonical_entity,
                bullhorn_entity=snap.bullhorn_entity,
                fields=snap.fields,
                warnings=_bounded_warnings(dict.fromkeys(list(snap.warnings) + list(ent.warnings))),
                meta_unusable=snap.meta_unusable,
            )
        out[ent.canonical_entity] = snap
    return DiscoverySnapshot(checked_at=checked_at, rest_url_fingerprint=rest_url_fingerprint, entities=out)


def merge_snapshots(previous: DiscoverySnapshot | None, current: DiscoverySnapshot) -> DiscoverySnapshot:
    """Entities not rediscovered keep their previous snapshot."""
    entities: dict[str, EntitySnapshot] = dict(previous.entities) if previous else {}
    entities.update(current.entities)
    return DiscoverySnapshot(
        checked_at=current.checked_at,
        rest_url_fingerprint=current.rest_url_fingerprint,
        entities=entities,
    )


# ---------------------------------------------------------------------- #
# Drift report
# ---------------------------------------------------------------------- #


def _cap(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(items), "items": items[:MAX_REPORT_ITEMS], "truncated": max(0, len(items) - MAX_REPORT_ITEMS)}


def build_drift_report(
    previous: DiscoverySnapshot | None,
    current: DiscoverySnapshot,
    profile: TenantProfileV2 | None,
    entities: Iterable[str],
    bullhorn: BullhornCatalog | None = None,
) -> dict[str, Any]:
    """Compare the rediscovered ``entities`` against the previous snapshot and the active version.

    For an entity whose metadata is unusable or whose discovery failed,
    broken/removed findings are suppressed (NB-7). ``unverifiable`` value
    drift (no options in the meta response) is informational and is not a
    finding.
    """
    bullhorn = bullhorn or load_bullhorn_catalog()
    new_fields: list[dict[str, Any]] = []
    removed_fields: list[dict[str, Any]] = []
    changed_fields: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    newly_unmapped: list[dict[str, Any]] = []
    value_drift: list[dict[str, Any]] = []
    meta_unusable: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for canon in entities:
        cur = current.entity(canon)
        if cur is None:
            continue
        prev = previous.entity(canon) if previous else None
        if cur.error is not None:
            errors.append({"entity": canon, "error": cur.error})
            continue
        if cur.meta_unusable:
            meta_unusable.append({"entity": canon, "warnings": list(cur.warnings[:5])})
            continue
        prev_usable = prev is not None and prev.usable
        if prev_usable:
            assert prev is not None
            for name in cur.fields:
                if name not in prev.fields:
                    new_fields.append({"entity": canon, "field": name})
            for name in prev.fields:
                if name not in cur.fields:
                    removed_fields.append({"entity": canon, "field": name})
            for name, f in cur.fields.items():
                old = prev.fields.get(name)
                if old is None:
                    continue
                changed = [a for a in SNAPSHOT_ATTRIBUTES if getattr(old, a) != getattr(f, a)]
                if changed:
                    changed_fields.append({"entity": canon, "field": name, "attributes": changed})

        mapped_sources: set[str] = set()
        if profile is not None:
            for rec in profile.field_mappings:
                if rec.entity != canon or not rec.active:
                    continue
                mapped_sources.update(rec.target.sources)
                missing = [s for s in rec.target.sources if s not in cur.fields]
                if missing:
                    broken.append({"key": rec.key, "missing_sources": missing})
            for vrec in profile.value_mappings:
                if vrec.entity != canon or not vrec.active:
                    continue
                mapped_sources.add(vrec.bullhorn_field)
                fsnap = cur.fields.get(vrec.bullhorn_field)
                if fsnap is None:
                    broken.append({"key": vrec.diff_key, "missing_sources": [vrec.bullhorn_field]})
                    continue
                allowed = fsnap.option_values()
                if allowed is None:
                    value_drift.append({"key": vrec.diff_key, "status": "unverifiable", "values": []})
                    continue
                gone = [v for v in vrec.values if tagged(v) not in allowed]
                if gone:
                    value_drift.append({"key": vrec.diff_key, "status": "drift", "values": gone[:50]})

        if prev_usable:
            assert prev is not None
            for name in cur.fields:
                if bullhorn.is_custom_field(name) and name not in mapped_sources and name not in prev.fields:
                    newly_unmapped.append({"entity": canon, "field": name})

    findings = bool(
        new_fields
        or removed_fields
        or changed_fields
        or broken
        or newly_unmapped
        or meta_unusable
        or errors
        or any(d["status"] == "drift" for d in value_drift)
    )
    return {
        "checked_at": current.checked_at,
        "has_findings": findings,
        "new_fields": _cap(new_fields),
        "removed_fields": _cap(removed_fields),
        "changed_fields": _cap(changed_fields),
        "broken_mappings": _cap(broken),
        "newly_unmapped": _cap(newly_unmapped),
        "value_drift": _cap(value_drift),
        "meta_unusable": _cap(meta_unusable),
        "errors": _cap(errors),
    }
