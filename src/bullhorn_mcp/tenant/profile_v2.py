"""Tenant profile v2 documents (D-4A-8).

A v2 document is the store's unit of versioning. It is never written over the
v1 profile format: ``schema/mapping_profile.py`` is unchanged, and a v2
document reaches the Phase 3 translator only through ``to_v1_profile()``,
which goes through ``MappingProfile.from_dict``.

Validation is aggregated: one ``ProfileError`` lists every problem found,
with every input value shown through ``describe_value`` / ``path_segment``.
"""

from __future__ import annotations

import functools
import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any

import yaml

from ..schema.bullhorn_catalog import BullhornCatalog, RawTarget, is_raw_name, load_bullhorn_catalog, parse_target
from ..schema.canonical_catalog import KEY_RE, CanonicalCatalog, load_canonical_catalog, read_packaged_text
from ..schema.errors import ProfileError, describe_value, path_segment, truncate_text
from ..schema.mapping_profile import CUSTOM_TYPES, MappingProfile
from ..schema.yaml_safe import YAML_PARSE_ERRORS, YamlParseFailure
from . import yaml_strict
from .timeutil import DEFAULT_TIMEZONE, normalize_timestamp, validate_timezone

FORMAT = "tenant-profile/v2"

FIELD_KINDS = ("standard", "custom")
FIELD_SOURCES = ("standard", "discovered", "administrator")
VALUE_SOURCES = ("discovered", "administrator")
VALIDATION_STATES = ("valid", "unresolved", "broken", "unvalidated")
VALUE_TARGET_KINDS = ("concept", "ordering")
VALUE_TARGET_KINDS = VALUE_TARGET_KINDS + ("note_action",)  # type: ignore[assignment]  # Phase 4B (D-4B-12)

TENANT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}", re.ASCII)
VALUE_KEY_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}", re.ASCII)
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
MAX_LABEL_CHARS = 200
MAX_DETAIL_CHARS = 500
MAX_VALUE_CHARS = 500
MAX_VALUES = 500
MAX_FIELD_MAPPINGS = 5000
MAX_VALUE_MAPPINGS = 1000

_TOP_KEYS = frozenset(
    {
        "format",
        "tenant",
        "profile_version",
        "catalog_fingerprint",
        "rest_url_fingerprint",
        "field_mappings",
        "value_mappings",
        "settings",
    }
)
_FIELD_KEYS = frozenset(
    {"entity", "field", "kind", "target", "type", "source", "active", "validation", "created_at", "updated_at"}
)
_VALUE_KEYS = frozenset(
    {"key", "entity", "target", "bullhorn_field", "values", "source", "active", "validation", "created_at", "updated_at"}
)
_VALIDATION_KEYS = frozenset({"state", "checked_at", "detail", "values_unverified"})
_SETTINGS_KEYS = frozenset({"reporting_timezone"})

_CONCEPTS_RESOURCE = "activity_concepts.yaml"
_NOTE_SEMANTICS_RESOURCE = "note_action_semantics.yaml"
# note_action value targets (Phase 4B, D-4B-12): always Note.action, a String (30) (HV-B10).
NOTE_ACTION_ENTITY = "note"
NOTE_ACTION_FIELD = "action"
MAX_NOTE_ACTION_CHARS = 30


# ---------------------------------------------------------------------- #
# Packaged resources
# ---------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def load_activity_concepts() -> frozenset[str]:
    """The packaged concept IDs (``mappings/activity_concepts.yaml``, D-4A-15)."""
    try:
        data = yaml_strict.parse(read_packaged_text(_CONCEPTS_RESOURCE))
    except YamlParseFailure as exc:
        raise ProfileError([str(exc)], _CONCEPTS_RESOURCE) from exc
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) != {"version", "concepts"}:
        raise ProfileError(["must be a mapping with exactly 'version' and 'concepts'"], _CONCEPTS_RESOURCE)
    if data["version"] != 1 or isinstance(data["version"], bool):
        errors.append(f"unsupported version {describe_value(data['version'])}")
    concepts = data["concepts"]
    if not isinstance(concepts, list) or not concepts:
        errors.append("concepts: must be a non-empty list")
        concepts = []
    out: set[str] = set()
    for i, c in enumerate(concepts):
        if not isinstance(c, str) or not KEY_RE.fullmatch(c):
            errors.append(f"concepts[{i}]: {describe_value(c)} must match {KEY_RE.pattern}")
        elif c in out:
            errors.append(f"concepts[{i}]: duplicate {describe_value(c)}")
        else:
            out.add(c)
    if errors:
        raise ProfileError(errors, _CONCEPTS_RESOURCE)
    return frozenset(out)


@functools.lru_cache(maxsize=1)
def load_note_action_semantics() -> tuple[str, ...]:
    """The packaged note action semantic tags (``mappings/note_action_semantics.yaml``, D-4B-4)."""
    try:
        data = yaml_strict.parse(read_packaged_text(_NOTE_SEMANTICS_RESOURCE))
    except YamlParseFailure as exc:
        raise ProfileError([str(exc)], _NOTE_SEMANTICS_RESOURCE) from exc
    if not isinstance(data, dict) or set(data) != {"version", "tags"}:
        raise ProfileError(["must be a mapping with exactly 'version' and 'tags'"], _NOTE_SEMANTICS_RESOURCE)
    errors: list[str] = []
    if data["version"] != 1 or isinstance(data["version"], bool):
        errors.append(f"unsupported version {describe_value(data['version'])}")
    tags = data["tags"]
    if not isinstance(tags, list) or not tags:
        errors.append("tags: must be a non-empty list")
        tags = []
    out: list[str] = []
    for i, entry in enumerate(tags):
        tag = entry.get("tag") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or set(entry) != {"tag", "meaning"} or not isinstance(entry.get("meaning"), str):
            errors.append(f"tags[{i}]: must be a mapping with exactly 'tag' and 'meaning'")
        elif not isinstance(tag, str) or not KEY_RE.fullmatch(tag):
            errors.append(f"tags[{i}].tag: {describe_value(tag)} must match {KEY_RE.pattern}")
        elif tag in out:
            errors.append(f"tags[{i}].tag: duplicate {describe_value(tag)}")
        else:
            out.append(tag)
    if errors:
        raise ProfileError(errors, _NOTE_SEMANTICS_RESOURCE)
    return tuple(out)


@functools.lru_cache(maxsize=1)
def current_catalog_fingerprint() -> str:
    """sha256 over the packaged canonical + Bullhorn catalog text."""
    digest = hashlib.sha256()
    for name in ("canonical_schema.yaml", "bullhorn_standard_fields.yaml"):
        digest.update(name.encode("ascii") + b"\x00")
        digest.update(read_packaged_text(name).encode("utf-8") + b"\x00")
    return digest.hexdigest()


def rest_url_fingerprint(rest_url: object) -> str | None:
    """sha256 of the session ``restUrl`` (HV-A5). The URL itself, which embeds the corp token, is never stored."""
    if not isinstance(rest_url, str) or not rest_url:
        return None
    return hashlib.sha256(rest_url.encode("utf-8", "surrogatepass")).hexdigest()


# ---------------------------------------------------------------------- #
# Model
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecordValidation:
    state: str = "unvalidated"
    checked_at: str | None = None
    detail: str | None = None
    values_unverified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "checked_at": self.checked_at, "detail": self.detail}
        if self.values_unverified is not None:
            out["values_unverified"] = self.values_unverified
        return out


@dataclass(frozen=True)
class FieldMappingRecord:
    entity: str
    field: str
    kind: str
    target: RawTarget
    source: str
    active: bool = True
    type: str | None = None
    validation: RecordValidation = dc_field(default_factory=RecordValidation)
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def key(self) -> str:
        return f"field:{self.entity}.{self.field}"

    def content(self) -> dict[str, Any]:
        """The reviewable content (what a diff compares): no validation, no timestamps."""
        return {
            "entity": self.entity,
            "field": self.field,
            "kind": self.kind,
            "target": self.target.to_data(),
            "type": self.type,
            "source": self.source,
            "active": self.active,
        }

    def to_dict(self) -> dict[str, Any]:
        out = self.content()
        if self.type is None:
            del out["type"]
        out["validation"] = self.validation.to_dict()
        out["created_at"] = self.created_at
        out["updated_at"] = self.updated_at
        return out


@dataclass(frozen=True)
class ValueTarget:
    kind: str
    name: str | None = None  # concept
    entity: str | None = None  # ordering
    field: str | None = None  # ordering
    semantic: str | None = None  # note_action (a packaged tag, or None)

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "concept":
            return {"kind": "concept", "name": self.name}
        if self.kind == "note_action":
            return {"kind": "note_action", "semantic": self.semantic}
        return {"kind": "ordering", "entity": self.entity, "field": self.field}


@dataclass(frozen=True)
class ValueMappingRecord:
    key: str
    entity: str
    target: ValueTarget
    bullhorn_field: str
    values: tuple[str | int, ...]
    source: str
    active: bool = True
    validation: RecordValidation = dc_field(default_factory=lambda: RecordValidation(values_unverified=True))
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def diff_key(self) -> str:
        return f"value:{self.key}"

    def content(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "entity": self.entity,
            "target": self.target.to_dict(),
            "bullhorn_field": self.bullhorn_field,
            "values": list(self.values),
            "source": self.source,
            "active": self.active,
        }

    def to_dict(self) -> dict[str, Any]:
        out = self.content()
        out["validation"] = self.validation.to_dict()
        out["created_at"] = self.created_at
        out["updated_at"] = self.updated_at
        return out


@dataclass(frozen=True)
class Settings:
    reporting_timezone: str = DEFAULT_TIMEZONE

    def to_dict(self) -> dict[str, Any]:
        return {"reporting_timezone": self.reporting_timezone}


@dataclass(frozen=True)
class TenantProfileV2:
    tenant_id: str
    profile_version: int
    catalog_fingerprint: str
    tenant_label: str | None = None
    rest_url_fingerprint: str | None = None
    field_mappings: tuple[FieldMappingRecord, ...] = ()
    value_mappings: tuple[ValueMappingRecord, ...] = ()
    settings: Settings = dc_field(default_factory=Settings)

    # ------------------------------------------------------------------ #

    def field_record(self, entity: str, name: str, *, active_only: bool = False) -> FieldMappingRecord | None:
        for rec in self.field_mappings:
            if rec.entity == entity and rec.field == name and (rec.active or not active_only):
                return rec
        return None

    def value_record(self, key: str) -> ValueMappingRecord | None:
        for rec in self.value_mappings:
            if rec.key == key:
                return rec
        return None

    def with_changes(self, **kwargs: Any) -> TenantProfileV2:
        return replace(self, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "tenant": {"id": self.tenant_id, "label": self.tenant_label},
            "profile_version": self.profile_version,
            "catalog_fingerprint": self.catalog_fingerprint,
            "rest_url_fingerprint": self.rest_url_fingerprint,
            "field_mappings": [r.to_dict() for r in self.field_mappings],
            "value_mappings": [r.to_dict() for r in self.value_mappings],
            "settings": self.settings.to_dict(),
        }

    def to_yaml_text(self) -> str:
        """Serialize, then prove the text round-trips to an equal document (F-11 style)."""
        try:
            text = yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=False, default_flow_style=False)
        except YAML_PARSE_ERRORS as exc:
            raise ProfileError([f"profile cannot be serialized ({type(exc).__name__})"], "<serialize>") from exc
        try:
            again = TenantProfileV2.from_yaml_text(text, source="<serialize self-check>", allow_conflicts=True)
        except ProfileError as exc:
            raise ProfileError(["profile cannot be serialized losslessly"], "<serialize>") from exc
        if again != self:
            raise ProfileError(["profile cannot be serialized losslessly"], "<serialize>")
        return text

    # ------------------------------------------------------------------ #

    def to_v1_profile(self, canonical: CanonicalCatalog | None = None) -> MappingProfile:
        """The v1 view of the active records, built only through ``MappingProfile.from_dict``.

        ``type`` is carried only where the v1 format can carry it (custom
        records with a nested ``{field, key}`` target); the Phase 3 translator
        does not use it.
        """
        canonical = canonical or load_canonical_catalog()
        return MappingProfile.from_dict(self.to_v1_dict(), canonical, source="<v2 -> v1>")

    def to_v1_dict(self) -> dict[str, Any]:
        entities: dict[str, dict[str, Any]] = {}
        stamps: list[str] = []
        for rec in self.field_mappings:
            if rec.updated_at:
                stamps.append(rec.updated_at)
            if not rec.active:
                continue
            body = entities.setdefault(rec.entity, {"standard": {}, "custom": {}})
            data = rec.target.to_data()
            if rec.kind == "standard":
                body["standard"][rec.field] = data
            else:
                if isinstance(data, dict) and rec.type is not None and rec.type != "string":
                    data = {**data, "type": rec.type}
                body["custom"][rec.field] = data
        return {
            "version": 1,
            "tenant": self.tenant_id,
            "generated_at": max(stamps) if stamps else None,
            "entities": entities,
        }

    # ------------------------------------------------------------------ #

    @classmethod
    def from_yaml_text(cls, text: str, source: str = "<yaml>", *, allow_conflicts: bool = False) -> TenantProfileV2:
        try:
            data = yaml_strict.parse(text)
        except YamlParseFailure as exc:
            raise ProfileError([str(exc)], source) from exc
        return cls.from_dict(data, source=source, allow_conflicts=allow_conflicts)

    @classmethod
    def from_dict(
        cls,
        data: Any,
        source: str = "<dict>",
        *,
        canonical: CanonicalCatalog | None = None,
        bullhorn: BullhornCatalog | None = None,
        allow_conflicts: bool = False,
    ) -> TenantProfileV2:
        """Validate a v2 document. Raises only ``ProfileError`` (aggregated).

        ``allow_conflicts=True`` skips the cross-record concept-conflict rule
        (used for proposals, which report conflicts instead of failing).
        """
        canonical = canonical or load_canonical_catalog()
        bullhorn = bullhorn or load_bullhorn_catalog()
        if not isinstance(data, dict):
            raise ProfileError(["top level must be a mapping"], source)
        errors: list[str] = []
        for key in data:
            if not isinstance(key, str) or key not in _TOP_KEYS:
                errors.append(f"unknown top-level key: {describe_value(key)}")
        fmt = data.get("format")
        if fmt != FORMAT:
            errors.append(f"format must be {FORMAT!r}, got {describe_value(fmt)}")

        tenant_id, tenant_label = _parse_tenant(data.get("tenant"), errors)

        version = data.get("profile_version")
        if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 999_999:
            errors.append(f"profile_version: must be an integer from 1 to 999999, got {describe_value(version)}")
            version = None

        catalog_fp = data.get("catalog_fingerprint")
        if not isinstance(catalog_fp, str) or not FINGERPRINT_RE.fullmatch(catalog_fp):
            errors.append("catalog_fingerprint: must be a 64-character lowercase sha256 hex digest")
            catalog_fp = None
        rest_fp = data.get("rest_url_fingerprint")
        if rest_fp is not None and (not isinstance(rest_fp, str) or not FINGERPRINT_RE.fullmatch(rest_fp)):
            errors.append("rest_url_fingerprint: must be null or a 64-character lowercase sha256 hex digest")
            rest_fp = None

        field_records = _parse_field_mappings(data.get("field_mappings"), canonical, bullhorn, errors)
        value_records = _parse_value_mappings(data.get("value_mappings"), canonical, bullhorn, errors)
        settings = _parse_settings(data.get("settings"), errors)

        if not allow_conflicts:
            errors.extend(find_value_conflicts(value_records))

        if errors:
            raise ProfileError(errors, source)
        assert tenant_id is not None and version is not None and catalog_fp is not None and settings is not None
        return cls(
            tenant_id=tenant_id,
            tenant_label=tenant_label,
            profile_version=version,
            catalog_fingerprint=catalog_fp,
            rest_url_fingerprint=rest_fp,
            field_mappings=tuple(field_records),
            value_mappings=tuple(value_records),
            settings=settings,
        )


# ---------------------------------------------------------------------- #
# Parsing helpers
# ---------------------------------------------------------------------- #


def tagged(value: Any) -> tuple[str, Any]:
    """Type-aware identity of a scalar value (``1``, ``True`` and ``1.0`` differ)."""
    return (type(value).__name__, value)


def find_value_conflicts(records: Iterable[ValueMappingRecord]) -> list[str]:
    """Two active concept mappings sharing one ``(bullhorn_field, value)`` are a conflict."""
    owners: dict[tuple[str, tuple[str, Any]], str] = {}
    conflicts: list[str] = []
    reported: set[tuple[str, str, tuple[str, Any]]] = set()
    for rec in records:
        if not rec.active or rec.target.kind != "concept":
            continue
        for v in rec.values:
            slot = (rec.bullhorn_field, tagged(v))
            other = owners.get(slot)
            if other is None:
                owners[slot] = rec.key
                continue
            if other == rec.key or (other, rec.key, tagged(v)) in reported:
                continue
            reported.add((other, rec.key, tagged(v)))
            conflicts.append(
                f"value_mappings: conflict: value {describe_value(v)} of field {path_segment(rec.bullhorn_field)} "
                f"is mapped by both {path_segment(other)} and {path_segment(rec.key)}"
            )
    return conflicts


def _parse_tenant(raw: Any, errors: list[str]) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict):
        errors.append("tenant: must be a mapping with 'id' and optional 'label'")
        return None, None
    for key in raw:
        if not isinstance(key, str) or key not in ("id", "label"):
            errors.append(f"tenant: unknown key {describe_value(key)}")
    tid = raw.get("id")
    if not isinstance(tid, str) or not TENANT_ID_RE.fullmatch(tid):
        errors.append(f"tenant.id: {describe_value(tid)} must match {TENANT_ID_RE.pattern}")
        tid = None
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARS):
        errors.append(f"tenant.label: must be null or a string of at most {MAX_LABEL_CHARS} characters")
        label = None
    return tid, label


def _parse_timestamp(where: str, key: str, value: Any, errors: list[str]) -> tuple[bool, str | None]:
    if value is None:
        return True, None
    norm = normalize_timestamp(value)
    if norm is None:
        errors.append(f"{where}.{key}: must be an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ), got {describe_value(value)}")
        return False, None
    return True, norm


def _parse_validation(where: str, raw: Any, errors: list[str], *, value_mapping: bool) -> RecordValidation | None:
    if raw is None:
        return RecordValidation(values_unverified=True if value_mapping else None)
    if not isinstance(raw, dict):
        errors.append(f"{where}.validation: must be a mapping")
        return None
    ok = True
    for key in raw:
        if not isinstance(key, str) or key not in _VALIDATION_KEYS or (key == "values_unverified" and not value_mapping):
            errors.append(f"{where}.validation: unknown key {describe_value(key)}")
            ok = False
    state = raw.get("state", "unvalidated")
    if not isinstance(state, str) or state not in VALIDATION_STATES:
        errors.append(f"{where}.validation.state: {describe_value(state)} is not one of {list(VALIDATION_STATES)}")
        ok = False
    tok, checked_at = _parse_timestamp(f"{where}.validation", "checked_at", raw.get("checked_at"), errors)
    detail = raw.get("detail")
    if detail is not None and (not isinstance(detail, str) or len(detail) > MAX_DETAIL_CHARS):
        errors.append(f"{where}.validation.detail: must be null or a string of at most {MAX_DETAIL_CHARS} characters")
        ok = False
    vu: bool | None = None
    if value_mapping:
        vu_raw = raw.get("values_unverified", True)
        if not isinstance(vu_raw, bool):
            errors.append(f"{where}.validation.values_unverified: must be a boolean")
            ok = False
        else:
            vu = vu_raw
    if not ok or not tok:
        return None
    return RecordValidation(state=state, checked_at=checked_at, detail=detail, values_unverified=vu)


def _parse_list(raw: Any, name: str, limit: int, errors: list[str]) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{name}: must be a list")
        return []
    if len(raw) > limit:
        errors.append(f"{name}: at most {limit} records are allowed, got {len(raw)}")
        return []
    return raw


def parse_field_record(
    where: str,
    entry: Any,
    canonical: CanonicalCatalog,
    bullhorn: BullhornCatalog,
    errors: list[str],
) -> FieldMappingRecord | None:
    """Validate one field-mapping record (shared by documents and change operations)."""
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    ok = True
    for key in entry:
        if not isinstance(key, str) or key not in _FIELD_KEYS:
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False
    entity = entry.get("entity")
    name = entry.get("field")
    if not isinstance(entity, str) or not canonical.has_entity(entity):
        errors.append(f"{where}.entity: {describe_value(entity)} is not a canonical entity")
        return None
    if bullhorn.bullhorn_entity_for(entity) is None:
        errors.append(f"{where}.entity: canonical entity {path_segment(entity)} has no Bullhorn entity in the catalog")
        return None
    kind = entry.get("kind", "standard")
    if not isinstance(kind, str) or kind not in FIELD_KINDS:
        errors.append(f"{where}.kind: {describe_value(kind)} is not one of {list(FIELD_KINDS)}")
        return None
    fwhere = f"{where}[{path_segment(entity)}.{path_segment(name)}]"
    if name == "id":
        errors.append(f"{fwhere}: canonical 'id' of entity {path_segment(entity)} always maps to raw 'id' and cannot be mapped")
        return None
    canonical_fields = set(canonical.entity(entity).field_names)
    if kind == "standard":
        if not isinstance(name, str) or name not in canonical_fields:
            errors.append(f"{fwhere}.field: {describe_value(name)} is not a canonical field of {path_segment(entity)}")
            return None
    else:
        if not isinstance(name, str) or not KEY_RE.fullmatch(name):
            errors.append(f"{fwhere}.field: custom name must match {KEY_RE.pattern}")
            return None
        if name in canonical_fields:
            errors.append(f"{fwhere}.field: custom name collides with canonical field {path_segment(entity)}.{path_segment(name)}")
            return None
    target, _, terrs = parse_target(entry.get("target"), f"{fwhere}.target")
    if terrs or target is None:
        errors.extend(terrs or [f"{fwhere}.target: invalid target"])
        ok = False
    ftype = entry.get("type")
    if ftype is not None:
        if kind != "custom":
            errors.append(f"{fwhere}.type: only custom mappings carry a type")
            ok = False
        elif not isinstance(ftype, str) or ftype not in CUSTOM_TYPES:
            errors.append(f"{fwhere}.type: {describe_value(ftype)} is not allowed (allowed: {sorted(CUSTOM_TYPES)})")
            ok = False
    source = entry.get("source", "administrator")
    if not isinstance(source, str) or source not in FIELD_SOURCES:
        errors.append(f"{fwhere}.source: {describe_value(source)} is not one of {list(FIELD_SOURCES)}")
        ok = False
    active = entry.get("active", True)
    if not isinstance(active, bool):
        errors.append(f"{fwhere}.active: must be a boolean")
        ok = False
    validation = _parse_validation(fwhere, entry.get("validation"), errors, value_mapping=False)
    c_ok, created = _parse_timestamp(fwhere, "created_at", entry.get("created_at"), errors)
    u_ok, updated = _parse_timestamp(fwhere, "updated_at", entry.get("updated_at"), errors)
    if not ok or validation is None or not c_ok or not u_ok or target is None:
        return None
    return FieldMappingRecord(
        entity=entity,
        field=name,
        kind=kind,
        target=target,
        type=ftype,
        source=source,
        active=active,
        validation=validation,
        created_at=created,
        updated_at=updated,
    )


def _parse_field_mappings(
    raw: Any, canonical: CanonicalCatalog, bullhorn: BullhornCatalog, errors: list[str]
) -> list[FieldMappingRecord]:
    records: list[FieldMappingRecord] = []
    for i, entry in enumerate(_parse_list(raw, "field_mappings", MAX_FIELD_MAPPINGS, errors)):
        rec = parse_field_record(f"field_mappings[{i}]", entry, canonical, bullhorn, errors)
        if rec is not None:
            records.append(rec)
    by_key: dict[tuple[str, str], list[FieldMappingRecord]] = {}
    for rec in records:
        by_key.setdefault((rec.entity, rec.field), []).append(rec)
    for (entity, name), group in by_key.items():
        if len(group) < 2:
            continue
        label = f"{path_segment(entity)}.{path_segment(name)}"
        if sum(1 for r in group if r.active) > 1:
            errors.append(f"field_mappings: more than one active record for {label}")
        else:
            errors.append(f"field_mappings: more than one record for {label}")
    return records


def parse_value_values(where: str, raw: Any, errors: list[str]) -> tuple[str | int, ...] | None:
    if not isinstance(raw, list) or not raw:
        errors.append(f"{where}.values: must be a non-empty list of strings or integers")
        return None
    if len(raw) > MAX_VALUES:
        errors.append(f"{where}.values: at most {MAX_VALUES} values are allowed, got {len(raw)}")
        return None
    out: list[str | int] = []
    seen: set[tuple[str, Any]] = set()
    ok = True
    for j, v in enumerate(raw):
        if isinstance(v, float) and not math.isfinite(v):
            errors.append(f"{where}.values[{j}]: must be a finite scalar, got {describe_value(v)}")
            ok = False
            continue
        if isinstance(v, bool) or not isinstance(v, (str, int)) or (isinstance(v, int) and v.bit_length() > 63):
            errors.append(f"{where}.values[{j}]: must be a string or a 64-bit integer, got {describe_value(v)}")
            ok = False
            continue
        if isinstance(v, str) and len(v) > MAX_VALUE_CHARS:
            errors.append(f"{where}.values[{j}]: string longer than {MAX_VALUE_CHARS} characters")
            ok = False
            continue
        if tagged(v) in seen:
            errors.append(f"{where}.values[{j}]: duplicate value {describe_value(v)}")
            ok = False
            continue
        seen.add(tagged(v))
        out.append(v)
    return tuple(out) if ok else None


def parse_value_target(where: str, raw: Any, canonical: CanonicalCatalog, errors: list[str]) -> ValueTarget | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}.target: must be {{kind: concept, name}} or {{kind: ordering, entity, field}}")
        return None
    kind = raw.get("kind")
    if kind == "concept":
        for key in raw:
            if key not in ("kind", "name"):
                errors.append(f"{where}.target: unknown key {describe_value(key)}")
                return None
        name = raw.get("name")
        if not isinstance(name, str) or name not in load_activity_concepts():
            errors.append(f"{where}.target.name: {describe_value(name)} is not a known activity concept")
            return None
        return ValueTarget(kind="concept", name=name)
    if kind == "ordering":
        for key in raw:
            if key not in ("kind", "entity", "field"):
                errors.append(f"{where}.target: unknown key {describe_value(key)}")
                return None
        entity, fname = raw.get("entity"), raw.get("field")
        if not isinstance(entity, str) or not isinstance(fname, str) or not canonical.has_field(entity, fname):
            errors.append(
                f"{where}.target: ordering target {describe_value(entity)}.{describe_value(fname)} is not a canonical field"
            )
            return None
        return ValueTarget(kind="ordering", entity=entity, field=fname)
    if kind == "note_action":
        for key in raw:
            if key not in ("kind", "semantic"):
                errors.append(f"{where}.target: unknown key {describe_value(key)}")
                return None
        semantic = raw.get("semantic")
        if semantic is not None and (not isinstance(semantic, str) or semantic not in load_note_action_semantics()):
            errors.append(
                f"{where}.target.semantic: {describe_value(semantic)} is not null or one of {list(load_note_action_semantics())}"
            )
            return None
        return ValueTarget(kind="note_action", semantic=semantic)
    errors.append(f"{where}.target.kind: {describe_value(kind)} is not one of {list(VALUE_TARGET_KINDS)}")
    return None


def parse_value_record(
    where: str,
    entry: Any,
    canonical: CanonicalCatalog,
    bullhorn: BullhornCatalog,
    errors: list[str],
) -> ValueMappingRecord | None:
    """Validate one value-mapping record (shared by documents and change operations)."""
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    ok = True
    for key in entry:
        if not isinstance(key, str) or key not in _VALUE_KEYS:
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False
    key_value = entry.get("key")
    if not isinstance(key_value, str) or not VALUE_KEY_RE.fullmatch(key_value):
        errors.append(f"{where}.key: {describe_value(key_value)} must match {VALUE_KEY_RE.pattern}")
        return None
    vwhere = f"{where}[{key_value}]"
    target = parse_value_target(vwhere, entry.get("target"), canonical, errors)
    entity = entry.get("entity")
    if target is not None and target.kind == "note_action":
        if entity is None:
            entity = NOTE_ACTION_ENTITY
        elif entity != NOTE_ACTION_ENTITY:
            errors.append(f"{vwhere}.entity: note_action mappings must be on entity {NOTE_ACTION_ENTITY!r}")
            ok = False
        if entry.get("bullhorn_field") != NOTE_ACTION_FIELD:
            errors.append(f"{vwhere}.bullhorn_field: note_action mappings must be on field {NOTE_ACTION_FIELD!r}")
            ok = False
    if target is not None and target.kind == "ordering":
        if entity is None:
            entity = target.entity
        elif entity != target.entity:
            errors.append(f"{vwhere}.entity: must equal the ordering target's entity")
            ok = False
    if not isinstance(entity, str) or not canonical.has_entity(entity):
        errors.append(f"{vwhere}.entity: {describe_value(entity)} is not a canonical entity (required for concept targets)")
        entity = None
        ok = False
    elif bullhorn.bullhorn_entity_for(entity) is None:
        errors.append(f"{vwhere}.entity: canonical entity {path_segment(entity)} has no Bullhorn entity in the catalog")
        ok = False
    bfield = entry.get("bullhorn_field")
    if not is_raw_name(bfield):
        errors.append(f"{vwhere}.bullhorn_field: {describe_value(bfield)} must be a raw Bullhorn field name")
        ok = False
    values = parse_value_values(vwhere, entry.get("values"), errors)
    if values is not None and target is not None and target.kind == "note_action":
        for j, v in enumerate(values):
            if not isinstance(v, str) or not v.strip() or len(v) > MAX_NOTE_ACTION_CHARS:
                errors.append(
                    f"{vwhere}.values[{j}]: a note action must be a non-blank string of at most "
                    f"{MAX_NOTE_ACTION_CHARS} characters, got {describe_value(v)}"
                )
                ok = False
    source = entry.get("source", "administrator")
    if not isinstance(source, str) or source not in VALUE_SOURCES:
        errors.append(f"{vwhere}.source: {describe_value(source)} is not one of {list(VALUE_SOURCES)}")
        ok = False
    active = entry.get("active", True)
    if not isinstance(active, bool):
        errors.append(f"{vwhere}.active: must be a boolean")
        ok = False
    validation = _parse_validation(vwhere, entry.get("validation"), errors, value_mapping=True)
    c_ok, created = _parse_timestamp(vwhere, "created_at", entry.get("created_at"), errors)
    u_ok, updated = _parse_timestamp(vwhere, "updated_at", entry.get("updated_at"), errors)
    if not ok or target is None or values is None or validation is None or not c_ok or not u_ok:
        return None
    assert isinstance(entity, str) and isinstance(bfield, str)
    return ValueMappingRecord(
        key=key_value,
        entity=entity,
        target=target,
        bullhorn_field=bfield,
        values=values,
        source=source,
        active=active,
        validation=validation,
        created_at=created,
        updated_at=updated,
    )


def _parse_value_mappings(
    raw: Any, canonical: CanonicalCatalog, bullhorn: BullhornCatalog, errors: list[str]
) -> list[ValueMappingRecord]:
    records: list[ValueMappingRecord] = []
    seen: set[str] = set()
    for i, entry in enumerate(_parse_list(raw, "value_mappings", MAX_VALUE_MAPPINGS, errors)):
        rec = parse_value_record(f"value_mappings[{i}]", entry, canonical, bullhorn, errors)
        if rec is None:
            continue
        if rec.key in seen:
            errors.append(f"value_mappings[{i}].key: duplicate key {path_segment(rec.key)}")
            continue
        seen.add(rec.key)
        records.append(rec)
    return records


def _parse_settings(raw: Any, errors: list[str]) -> Settings | None:
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        errors.append("settings: must be a mapping")
        return None
    ok = True
    for key in raw:
        if not isinstance(key, str) or key not in _SETTINGS_KEYS:
            errors.append(f"settings: unknown key {describe_value(key)}")
            ok = False
    tz = raw.get("reporting_timezone", DEFAULT_TIMEZONE)
    try:
        validate_timezone(tz)
    except ValueError as exc:
        errors.append(f"settings.reporting_timezone: {truncate_text(str(exc))}")
        ok = False
    return Settings(reporting_timezone=tz) if ok else None


def records_by_key(profile: TenantProfileV2 | None) -> Mapping[str, dict[str, Any]]:
    """Diffable content of a profile, keyed: ``field:e.f``, ``value:k``, ``setting:n``, ``tenant:label``, ``meta:*``."""
    if profile is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    out["tenant:id"] = {"value": profile.tenant_id}
    out["tenant:label"] = {"value": profile.tenant_label}
    out["meta:catalog_fingerprint"] = {"value": profile.catalog_fingerprint}
    out["meta:rest_url_fingerprint"] = {"value": profile.rest_url_fingerprint}
    out["setting:reporting_timezone"] = {"value": profile.settings.reporting_timezone}
    for rec in profile.field_mappings:
        out[rec.key] = rec.content()
    for vrec in profile.value_mappings:
        out[vrec.diff_key] = vrec.content()
    return out
