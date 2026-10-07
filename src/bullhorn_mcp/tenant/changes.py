"""Controlled configuration change: proposals (the dry-run) and commits (TS-4, D-4A-9).

``propose`` applies change operations to a draft built on the current active
version, validates it against the latest discovery snapshot, diffs it,
hashes the diff and stores the proposal. It writes nothing except
``proposals/``.

``commit`` re-validates and is refused - with nothing changed - when the
proposal is unknown, not open, expired, its hash does not match, its base is
stale, the actor is missing or not allowed, or the draft has a structural
error, a conflict, or an active ``broken`` mapping.

Phase 5A: in ``shared`` mode a proposal records its creator (``initiating_principal``,
``tenant_key``) and only that principal may commit or reject it (A3-3). The
verification ops (D-5A-15 / A3-4, A3-6) are single-op proposals with their own
commit branch (see the end of this module).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..schema.bullhorn_catalog import BullhornCatalog, load_bullhorn_catalog
from ..schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from ..schema.errors import ProfileError, SchemaError, describe_value, path_segment, truncate_text
from ..schema.mapping_profile import MappingProfile
from ..schema.yaml_safe import YamlParseFailure
from . import yaml_strict
from .actor import resolve_actor
from .capabilities import capabilities_for_key
from .profile_v2 import (
    FORMAT,
    MAX_NOTE_ACTION_CHARS,
    NOTE_ACTION_ENTITY,
    NOTE_ACTION_FIELD,
    SETTING_CHOICES,
    TENANT_ID_RE,
    FieldMappingRecord,
    RecordValidation,
    Settings,
    TenantProfileV2,
    ValueMappingRecord,
    ValueTarget,
    current_catalog_fingerprint,
    parse_field_record,
    parse_value_record,
    records_by_key,
    setting_value_error,
    tagged,
)
from .revalidation import DiscoverySnapshot, load_snapshot
from .store import SetupStore, SetupStoreError, check_external_path, read_external_text
from .timeutil import DEFAULT_TIMEZONE, format_utc, parse_utc, validate_timezone
from .validation import IMPORTED_DETAIL, apply_states, evaluate, imported_keys

PROPOSAL_TTL = _dt.timedelta(hours=24)
MAX_CHANGES = 200

_OP_KEYS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    # op: (required keys, optional keys) - "op" itself is implied.
    "init_tenant": (frozenset({"tenant_id"}), frozenset({"label"})),
    "apply_verified_defaults": (frozenset(), frozenset({"entities"})),
    "set_field_mapping": (frozenset({"entity", "field", "target"}), frozenset({"kind", "type"})),
    "deactivate_field_mapping": (frozenset({"entity", "field"}), frozenset()),
    "remove_field_mapping": (frozenset({"entity", "field"}), frozenset()),
    "set_value_mapping": (frozenset({"key", "target", "bullhorn_field", "values"}), frozenset({"entity"})),
    "deactivate_value_mapping": (frozenset({"key"}), frozenset()),
    "remove_value_mapping": (frozenset({"key"}), frozenset()),
    "set_setting": (frozenset({"name", "value"}), frozenset()),
    "rollback_to": (frozenset({"version"}), frozenset()),
    "import_document": (frozenset({"path"}), frozenset()),
    # Phase 5B (D-5B-3 / D-5B-4): propose-only, committed through the 4A commit flow.
    "apply_discovered_note_actions": (frozenset({"values"}), frozenset({"key_prefix"})),
    "reactivate_value_mapping": (frozenset({"key"}), frozenset()),
}
MAX_ADOPTED_VALUES = 100


# ---------------------------------------------------------------------- #
# Diff and hash
# ---------------------------------------------------------------------- #


def compute_diff(base: TenantProfileV2 | None, draft: TenantProfileV2) -> list[dict[str, Any]]:
    """``[{key, change: added|removed|changed|deactivated, old, new}]`` sorted by key."""
    old_map = records_by_key(base)
    new_map = records_by_key(draft)
    diff: list[dict[str, Any]] = []
    for key in sorted(set(old_map) | set(new_map)):
        old, new = old_map.get(key), new_map.get(key)
        if old == new:
            continue
        if old is None:
            change = "added"
        elif new is None:
            change = "removed"
        elif (
            old.get("active") is True
            and new.get("active") is False
            and {k: v for k, v in old.items() if k != "active"} == {k: v for k, v in new.items() if k != "active"}
        ):
            change = "deactivated"
        else:
            change = "changed"
        diff.append({"key": key, "change": change, "old": old, "new": new})
    return diff


def _tag(value: Any) -> Any:
    if isinstance(value, dict):
        items = [[_tag(k), _tag(v)] for k, v in value.items()]
        items.sort(key=lambda kv: json.dumps(kv[0], ensure_ascii=True))
        return ["dict", items]
    if isinstance(value, (list, tuple)):
        return ["list", [_tag(v) for v in value]]
    return [type(value).__name__, repr(value)]


def compute_diff_hash(base_version: int | None, diff: list[dict[str, Any]]) -> str:
    """sha256 over type-tagged canonical JSON of ``{base_version, diff}`` (``1``, ``True`` and ``1.0`` differ)."""
    payload = json.dumps(_tag({"base_version": base_version, "diff": diff}), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def compute_dependencies(diff: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in diff:
        caps = capabilities_for_key(entry["key"], [entry.get("old"), entry.get("new")])
        if caps:
            out[entry["key"]] = caps
    return out


# ---------------------------------------------------------------------- #
# Draft building
# ---------------------------------------------------------------------- #


class _Draft:
    """Mutable working copy while change operations are applied."""

    def __init__(self, base: TenantProfileV2 | None) -> None:
        self.tenant_id: str | None = base.tenant_id if base else None
        self.tenant_label: str | None = base.tenant_label if base else None
        self.rest_url_fingerprint: str | None = base.rest_url_fingerprint if base else None
        self.fields, self.inactive_extra = _split_fields(base.field_mappings if base else ())
        self.values: dict[str, ValueMappingRecord] = {r.key: r for r in base.value_mappings} if base else {}
        self.settings: Settings = base.settings if base else Settings()
        self.action = "commit"
        self.defaults: dict[str, list[str]] = {}

    def build(self, version: int) -> TenantProfileV2:
        assert self.tenant_id is not None
        return TenantProfileV2(
            tenant_id=self.tenant_id,
            tenant_label=self.tenant_label,
            profile_version=version,
            catalog_fingerprint=current_catalog_fingerprint(),
            rest_url_fingerprint=self.rest_url_fingerprint,
            field_mappings=self.field_mappings(),
            value_mappings=tuple(self.values.values()),
            settings=self.settings,
        )


    def field_mappings(self) -> tuple[FieldMappingRecord, ...]:
        """The primary records, preceded by the inactive duplicates kept beside an active primary (triage L-2)."""
        kept = [r for r in self.inactive_extra if (p := self.fields.get((r.entity, r.field))) is not None and p.active]
        return tuple(kept) + tuple(self.fields.values())


def _split_fields(
    records: Iterable[FieldMappingRecord],
) -> tuple[dict[tuple[str, str], FieldMappingRecord], list[FieldMappingRecord]]:
    """5C triage L-2: one primary record per ``(entity, field)`` (the active one when there is one, whatever
    the order) plus the other records of that slot, so no active mapping is dropped and none is lost."""
    primary: dict[tuple[str, str], FieldMappingRecord] = {}
    extra: list[FieldMappingRecord] = []
    for rec in records:
        slot = (rec.entity, rec.field)
        current = primary.get(slot)
        if current is None:
            primary[slot] = rec
        elif rec.active and not current.active:
            extra.append(current)
            primary[slot] = rec
        else:
            extra.append(rec)
    return primary, extra


class _Context:
    def __init__(
        self,
        store: SetupStore,
        base: TenantProfileV2 | None,
        snapshot: DiscoverySnapshot | None,
        now: str,
        canonical: CanonicalCatalog,
        bullhorn: BullhornCatalog,
    ) -> None:
        self.store = store
        self.base = base
        self.snapshot = snapshot
        self.now = now
        self.canonical = canonical
        self.bullhorn = bullhorn


def _check_op_shape(i: int, change: Any, errors: list[str]) -> str | None:
    where = f"changes[{i}]"
    if not isinstance(change, dict):
        errors.append(f"{where}: must be an object with an 'op'")
        return None
    op = change.get("op")
    if not isinstance(op, str) or op not in _OP_KEYS:
        errors.append(f"{where}.op: {describe_value(op)} is not one of {sorted(_OP_KEYS)}")
        return None
    required, optional = _OP_KEYS[op]
    ok = True
    for key in change:
        if key != "op" and (not isinstance(key, str) or (key not in required and key not in optional)):
            errors.append(f"{where} ({op}): unknown key {describe_value(key)}")
            ok = False
    for key in sorted(required):
        if key not in change:
            errors.append(f"{where} ({op}): missing required key {key!r}")
            ok = False
    return op if ok else None


def _apply_op(i: int, op: str, change: dict[str, Any], draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    where = f"changes[{i}] ({op})"
    if op == "init_tenant":
        if ctx.base is not None or draft.tenant_id is not None:
            errors.append(f"{where}: only allowed when no active version exists")
            return
        tid = change.get("tenant_id")
        if not isinstance(tid, str) or not TENANT_ID_RE.fullmatch(tid):
            errors.append(f"{where}.tenant_id: {describe_value(tid)} must match {TENANT_ID_RE.pattern}")
            return
        label = change.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 200):
            errors.append(f"{where}.label: must be null or a string of at most 200 characters")
            return
        draft.tenant_id, draft.tenant_label = tid, label
        return
    if draft.tenant_id is None:
        errors.append(f"{where}: no tenant yet; the first change set must start with init_tenant")
        return

    if op == "set_field_mapping":
        entity, name = change.get("entity"), change.get("field")
        kind = change.get("kind")
        if kind is None:
            kind = "standard" if isinstance(entity, str) and isinstance(name, str) and ctx.canonical.has_field(entity, name) else "custom"
        entry: dict[str, Any] = {
            "entity": entity,
            "field": name,
            "kind": kind,
            "target": change.get("target"),
            "source": "administrator",
            "active": True,
        }
        if change.get("type") is not None:
            entry["type"] = change["type"]
        rec = parse_field_record(where, entry, ctx.canonical, ctx.bullhorn, errors)
        if rec is None:
            return
        existing = draft.fields.get((rec.entity, rec.field))
        if existing is not None and existing.content() == rec.content():
            return
        draft.fields[(rec.entity, rec.field)] = replace(
            rec, created_at=existing.created_at if existing and existing.created_at else ctx.now, updated_at=ctx.now
        )
        return

    if op in ("deactivate_field_mapping", "remove_field_mapping"):
        slot = (change.get("entity"), change.get("field"))
        existing = draft.fields.get(slot) if all(isinstance(s, str) for s in slot) else None  # type: ignore[arg-type]
        if existing is None:
            errors.append(f"{where}: no field mapping for {path_segment(slot[0])}.{path_segment(slot[1])}")
            return
        if op == "remove_field_mapping":
            del draft.fields[(existing.entity, existing.field)]
        elif existing.active:
            draft.fields[(existing.entity, existing.field)] = replace(existing, active=False, updated_at=ctx.now)
        return

    if op == "set_value_mapping":
        ventry: dict[str, Any] = {
            "key": change.get("key"),
            "target": change.get("target"),
            "bullhorn_field": change.get("bullhorn_field"),
            "values": change.get("values"),
            "source": "administrator",
            "active": True,
        }
        if "entity" in change:
            ventry["entity"] = change["entity"]
        vrec = parse_value_record(where, ventry, ctx.canonical, ctx.bullhorn, errors)
        if vrec is None:
            return
        vexisting = draft.values.get(vrec.key)
        if vexisting is not None and vexisting.content() == vrec.content():
            return
        draft.values[vrec.key] = replace(
            vrec,
            created_at=vexisting.created_at if vexisting and vexisting.created_at else ctx.now,
            updated_at=ctx.now,
        )
        return

    if op in ("deactivate_value_mapping", "remove_value_mapping"):
        key = change.get("key")
        vexisting = draft.values.get(key) if isinstance(key, str) else None
        if vexisting is None:
            errors.append(f"{where}: no value mapping {describe_value(key)}")
            return
        if op == "remove_value_mapping":
            del draft.values[vexisting.key]
        elif vexisting.active:
            draft.values[vexisting.key] = replace(vexisting, active=False, updated_at=ctx.now)
        return

    if op == "set_setting":
        name, value = change.get("name"), change.get("value")
        if isinstance(name, str) and name in SETTING_CHOICES:  # Phase 5C (D-5C-15): the closed-enum settings
            setting_error = setting_value_error(name, value)
            if setting_error is not None:
                errors.append(f"{where}.value: {setting_error}")
                return
            assert isinstance(value, str)
            draft.settings = replace(draft.settings, **{name: value})
            return
        if name != "reporting_timezone":
            errors.append(
                f"{where}.name: {describe_value(name)} is not a known setting "
                f"(known: {['reporting_timezone', *SETTING_CHOICES]})"
            )
            return
        try:
            validate_timezone(value)
        except ValueError as exc:
            errors.append(f"{where}.value: {truncate_text(str(exc))}")
            return
        assert isinstance(value, str)
        draft.settings = replace(draft.settings, reporting_timezone=value)
        return

    if op == "rollback_to":
        _apply_rollback(where, change.get("version"), draft, ctx, errors)
        return

    if op == "import_document":
        _apply_import(where, change.get("path"), draft, ctx, errors)
        return

    if op == "apply_verified_defaults":
        _apply_defaults(where, change.get("entities"), draft, ctx, errors)
        return

    if op == "reactivate_value_mapping":
        _apply_reactivate(where, change.get("key"), draft, ctx, errors)
        return

    if op == "apply_discovered_note_actions":
        _apply_discovered(where, change.get("values"), change.get("key_prefix"), draft, ctx, errors)
        return

    raise AssertionError(op)  # pragma: no cover - _check_op_shape admits only known ops


def _apply_reactivate(where: str, key: Any, draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    """Phase 5B (D-5B-4): restore a deactivated value mapping unchanged (only ``active`` flips back)."""
    existing = draft.values.get(key) if isinstance(key, str) else None
    if existing is None:
        errors.append(f"{where}: no value mapping {describe_value(key)}")
        return
    if existing.active:
        errors.append(f"{where}: value mapping {path_segment(existing.key)} is already active")
        return
    # P5B-4: refuse when another active record already maps one of these values on the same field.
    mine = {tagged(v) for v in existing.values}
    for other in draft.values.values():
        if (
            other.key != existing.key
            and other.active
            and other.entity == existing.entity
            and other.bullhorn_field == existing.bullhorn_field
            and mine & {tagged(v) for v in other.values}
        ):
            errors.append(
                f"{where}: value mapping {path_segment(existing.key)} cannot be reactivated: active record "
                f"{path_segment(other.key)} already maps one of its values on {path_segment(existing.bullhorn_field)}"
            )
            return
    draft.values[existing.key] = replace(existing, active=True, updated_at=ctx.now)


def _apply_discovered(where: str, values: Any, key_prefix: Any, draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    """Phase 5B (D-5B-3): adopt administrator-selected values of the latest snapshot's verified sources.

    Each value becomes (or updates) one ``note_action`` record with ``source: discovered``,
    ``semantic: null`` and its ``discovery_source``. Values that are not exactly present in a
    verified source are rejected (administrator-entered values use ``set_value_mapping``).
    """
    # Local import: notes.action_discovery imports the tenant package, which imports this module.
    from ..notes.action_discovery import DEFAULT_KEY_PREFIX, KEY_PREFIX_RE, discovered_key, verified_sources_by_value

    prefix = DEFAULT_KEY_PREFIX if key_prefix is None else key_prefix
    if not isinstance(prefix, str) or KEY_PREFIX_RE.fullmatch(prefix) is None:
        errors.append(
            f"{where}.key_prefix: {describe_value(key_prefix)} must match {KEY_PREFIX_RE.pattern} "
            "(keys are always note.action.<key_prefix>.<value>)"
        )
        return
    if not isinstance(values, list) or not values or len(values) > MAX_ADOPTED_VALUES:
        errors.append(f"{where}.values: must be a non-empty list of at most {MAX_ADOPTED_VALUES} strings")
        return
    by_value = verified_sources_by_value(ctx.snapshot.note_actions if ctx.snapshot is not None else None)
    if not by_value:
        errors.append(
            f"{where}: the latest discovery snapshot has no verified note action source; run discover_schema "
            "(administrator-entered values use set_value_mapping)"
        )
        return
    seen: set[str] = set()
    for j, value in enumerate(values):
        vwhere = f"{where}.values[{j}]"
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_NOTE_ACTION_CHARS:
            errors.append(
                f"{vwhere}: a note action must be a non-blank string of at most {MAX_NOTE_ACTION_CHARS} characters, "
                f"got {describe_value(value)}"
            )
            continue
        if value in seen:
            errors.append(f"{vwhere}: duplicate value {describe_value(value)}")
            continue
        seen.add(value)
        source = by_value.get(value)
        if source is None:
            errors.append(
                f"{vwhere}: {describe_value(value)} is not a value of a verified discovery source "
                "(administrator-entered values use set_value_mapping)"
            )
            continue
        key = discovered_key(prefix, value)
        record = ValueMappingRecord(
            key=key,
            entity=NOTE_ACTION_ENTITY,
            target=ValueTarget(kind="note_action", semantic=None),
            bullhorn_field=NOTE_ACTION_FIELD,
            values=(value,),
            source="discovered",
            discovery_source=source,
        )
        existing = draft.values.get(key)
        if existing is not None:
            same_record = (
                existing.source == "discovered" and existing.target.kind == "note_action" and existing.values == (value,)
            )
            if not same_record:
                errors.append(f"{vwhere}: key {path_segment(key)} is already used by another value mapping")
                continue
            if not existing.active:
                errors.append(f"{vwhere}: {path_segment(key)} is deactivated; use reactivate_value_mapping")
                continue
            if existing.discovery_source == source and existing.target.semantic is None:
                continue  # already adopted, unchanged
            draft.values[key] = replace(record, created_at=existing.created_at or ctx.now, updated_at=ctx.now)
            continue
        owner = next(
            (r for r in draft.values.values() if r.target.kind == "note_action" and value in r.values),
            None,
        )
        if owner is not None:
            hint = "" if owner.active else "; use reactivate_value_mapping"
            errors.append(f"{vwhere}: {describe_value(value)} is already mapped by {path_segment(owner.key)}{hint}")
            continue
        draft.values[key] = replace(record, created_at=ctx.now, updated_at=ctx.now)


def _apply_rollback(where: str, version: Any, draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    if isinstance(version, bool) or not isinstance(version, int):
        errors.append(f"{where}.version: must be an integer, got {describe_value(version)}")
        return
    if version not in ctx.store.list_versions():
        errors.append(f"{where}.version: version {version} does not exist")
        return
    try:
        target = ctx.store.read_version(version)
    except SchemaError as exc:
        errors.append(f"{where}: version {version} cannot be loaded: {truncate_text(str(exc))}")
        return
    if target.tenant_id != draft.tenant_id:
        errors.append(f"{where}: version {version} belongs to a different tenant")
        return
    draft.tenant_label = target.tenant_label
    draft.fields, draft.inactive_extra = _split_fields(target.field_mappings)
    draft.values = {r.key: r for r in target.value_mappings}
    draft.settings = target.settings
    draft.action = "rollback"


def _imported(validation_values_unverified: bool | None = None) -> RecordValidation:
    return RecordValidation(state="unvalidated", detail=IMPORTED_DETAIL, values_unverified=validation_values_unverified)


def _apply_import(where: str, raw_path: Any, draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    try:
        path = check_external_path(raw_path, ctx.store.root, for_write=False)
        text = read_external_text(path)
        data = yaml_strict.parse(text)
    except (SetupStoreError, YamlParseFailure) as exc:
        errors.append(f"{where}: {truncate_text(str(exc))}")
        return
    if isinstance(data, dict) and "format" in data:
        try:
            doc = TenantProfileV2.from_dict(data, source="<import>", canonical=ctx.canonical, bullhorn=ctx.bullhorn)
        except ProfileError as exc:
            errors.extend(f"{where}: {e}" for e in exc.errors)
            return
        if doc.tenant_id != draft.tenant_id:
            errors.append(f"{where}: document tenant.id does not match this tenant")
            return
        draft.fields, draft.inactive_extra = _split_fields(
            replace(r, validation=_imported(), created_at=r.created_at or ctx.now, updated_at=ctx.now) for r in doc.field_mappings
        )
        draft.values = {
            r.key: replace(r, validation=_imported(True), created_at=r.created_at or ctx.now, updated_at=ctx.now)
            for r in doc.value_mappings
        }
        draft.settings = doc.settings
        draft.tenant_label = doc.tenant_label
        draft.action = "import"
        return
    if isinstance(data, dict) and "version" in data:
        try:
            v1 = MappingProfile.from_dict(data, ctx.canonical, source="<import>")
        except ProfileError as exc:
            errors.extend(f"{where}: {e}" for e in exc.errors)
            return
        if v1.tenant is not None and v1.tenant != draft.tenant_id:
            errors.append(f"{where}: document tenant does not match this tenant")
            return
        fields: dict[tuple[str, str], FieldMappingRecord] = {}
        for entity, ep in v1.entities.items():
            if ctx.bullhorn.bullhorn_entity_for(entity) is None:
                errors.append(f"{where}: entity {path_segment(entity)} has no Bullhorn entity in the catalog")
                continue
            for name, target in ep.standard.items():
                fields[(entity, name)] = FieldMappingRecord(
                    entity=entity, field=name, kind="standard", target=target, source="administrator",
                    validation=_imported(), created_at=ctx.now, updated_at=ctx.now,
                )
            for name, cm in ep.custom.items():
                fields[(entity, name)] = FieldMappingRecord(
                    entity=entity, field=name, kind="custom", target=cm.target,
                    type=None if cm.type == "string" else cm.type, source="administrator",
                    validation=_imported(), created_at=ctx.now, updated_at=ctx.now,
                )
        draft.fields, draft.inactive_extra = fields, []
        draft.action = "import"
        return
    errors.append(f"{where}: document is neither a v2 ({FORMAT!r}) nor a v1 ('version: 1') profile")


def _apply_defaults(where: str, entities: Any, draft: _Draft, ctx: _Context, errors: list[str]) -> None:
    bound = [e for e in ctx.canonical.entities if ctx.bullhorn.bullhorn_entity_for(e) is not None]
    if entities is None:
        selected = bound
    elif isinstance(entities, list) and all(isinstance(e, str) for e in entities):
        selected = []
        for e in entities:
            if e not in bound:
                errors.append(f"{where}.entities: {describe_value(e)} is not a catalog entity")
            elif e not in selected:
                selected.append(e)
    else:
        errors.append(f"{where}.entities: must be a list of canonical entity names or null")
        return
    applied: list[str] = []
    unresolved: list[str] = []
    kept: list[str] = []
    for entity in selected:
        snap = ctx.snapshot.entity(entity) if ctx.snapshot else None
        defaults = ctx.bullhorn.default_mappings(entity)
        for name in ctx.canonical.entity(entity).field_names:
            if name == "id":
                continue
            label = f"{entity}.{name}"
            target = defaults.get(name)
            if target is None or snap is None or not snap.usable or any(s not in snap.fields for s in target.sources):
                unresolved.append(label)
                continue
            existing = draft.fields.get((entity, name))
            if existing is not None and existing.source != "standard":
                kept.append(label)
                continue
            rec = FieldMappingRecord(
                entity=entity,
                field=name,
                kind="standard",
                target=target,
                source="standard",
                validation=RecordValidation(state="valid", checked_at=ctx.snapshot.checked_at if ctx.snapshot else None),
                created_at=existing.created_at if existing and existing.created_at else ctx.now,
                updated_at=ctx.now,
            )
            if existing is not None and existing.content() == rec.content():
                continue
            draft.fields[(entity, name)] = rec
            applied.append(label)
    draft.defaults = {"applied": applied, "unresolved": unresolved, "kept_existing": kept}


# ---------------------------------------------------------------------- #
# Propose
# ---------------------------------------------------------------------- #


def _load_base(store: SetupStore) -> tuple[int | None, TenantProfileV2 | None]:
    active = store.active_version()
    return active, (store.read_version(active) if active is not None else None)


def _next_version(store: SetupStore, base_version: int | None) -> int:
    existing = store.list_versions()
    return max(existing + [base_version or 0]) + 1


def propose(
    store: SetupStore,
    changes: Any,
    base_version: Any = None,
    *,
    now: _dt.datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build, validate, diff, hash and store a proposal. Writes only ``proposals/<id>.json``.

    Raises ``ProfileError`` for invalid change operations, ``SetupStoreError``
    for store problems (both bounded).
    """
    canonical = load_canonical_catalog()
    bullhorn = load_bullhorn_catalog()
    if not isinstance(changes, list):
        raise ProfileError(["changes: must be a list of change objects"], "<changes>")
    if len(changes) > MAX_CHANGES:
        raise ProfileError([f"changes: at most {MAX_CHANGES} operations are allowed, got {len(changes)}"], "<changes>")
    if any(isinstance(c, dict) and c.get("op") in VERIFICATION_OP_KEYS for c in changes):
        return _propose_verification(store, changes, base_version, now)
    if _proposal_owner() is not None and not resolve_actor(env).allowed:
        # 5A triage B-4: in shared mode only a setup admin of the selected tenant may propose.
        raise ProfileError(["denied: only a setup admin of this tenant may propose mapping changes"], "<changes>")
    active, base = _load_base(store)
    if base_version is not None and (isinstance(base_version, bool) or base_version != active):
        raise ProfileError(
            [f"base_version {describe_value(base_version)} is stale: the active version is {describe_value(active)}"], "<changes>"
        )
    discovery_doc = store.read_discovery()
    snapshot = load_snapshot(discovery_doc)
    now_text = format_utc(now)
    ctx = _Context(store, base, snapshot, now_text, canonical, bullhorn)

    draft = _Draft(base)
    errors: list[str] = []
    for i, change in enumerate(changes):
        op = _check_op_shape(i, change, errors)
        if op is not None:
            _apply_op(i, op, change, draft, ctx, errors)
    if not errors and draft.tenant_id is None:
        errors.append("changes: no tenant yet; the first change set must start with init_tenant")
    if errors:
        raise ProfileError(errors, "<changes>")
    if snapshot is not None and snapshot.rest_url_fingerprint is not None:
        draft.rest_url_fingerprint = snapshot.rest_url_fingerprint

    built = draft.build(_next_version(store, active))
    # Round-trip through structural validation; conflicts are reported, not raised.
    built = TenantProfileV2.from_dict(built.to_dict(), source="<draft>", canonical=canonical, bullhorn=bullhorn, allow_conflicts=True)
    result = evaluate(built, snapshot, keep_unvalidated=imported_keys(built), canonical=canonical, bullhorn=bullhorn)
    built = apply_states(built, result)

    diff = compute_diff(base, built)
    if not diff:
        raise ProfileError(["the change set produces no changes"], "<changes>")
    digest = compute_diff_hash(active, diff)
    proposal_id = uuid.uuid4().hex
    actor = resolve_actor(env)
    proposal = {
        "proposal_id": proposal_id,
        "base_version": active,
        "action": draft.action,
        "changes": changes,
        "draft": built.to_dict(),
        "diff": diff,
        "diff_hash": digest,
        "conflicts": list(result.conflicts),
        "dependencies": compute_dependencies(diff),
        "validation": result.summary(),
        "defaults": draft.defaults or None,
        "actor": actor.actor,
        "created_at": now_text,
        "expires_at": format_utc(now + PROPOSAL_TTL),
        "status": "open",
    }
    owner = _proposal_owner()
    if owner is not None:
        proposal.update(owner)
    store.save_proposal(proposal_id, proposal, create=True)
    return {
        "proposal_id": proposal_id,
        "diff_hash": digest,
        "expires_at": proposal["expires_at"],
        "base_version": active,
        "diff": diff,
        "conflicts": proposal["conflicts"],
        "dependencies": proposal["dependencies"],
        "validation": proposal["validation"],
        "defaults": proposal["defaults"],
    }


def is_open(proposal: Mapping[str, Any], now: _dt.datetime) -> bool:
    expires = parse_utc(proposal.get("expires_at"))
    return proposal.get("status") == "open" and expires is not None and now < expires


# ---------------------------------------------------------------------- #
# Commit
# ---------------------------------------------------------------------- #


def commit(
    store: SetupStore,
    proposal_id: Any,
    diff_hash: Any,
    decision: Any,
    *,
    now: _dt.datetime,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Approve or reject a proposal. Every refusal leaves the store unchanged."""
    correlation_id = uuid.uuid4().hex

    def refused(reason: str) -> dict[str, Any]:
        return {"status": "refused", "reason": truncate_text(reason, 1000), "correlation_id": correlation_id}

    if decision not in ("approve", "reject"):
        return refused("decision must be 'approve' or 'reject'")
    actor = resolve_actor(env)
    if not actor.allowed or actor.actor is None:
        return refused(actor.reason or "actor not allowed")
    proposal = store.load_proposal(proposal_id)
    if proposal is None:
        return refused("unknown proposal_id")
    problem = _ownership_problem(proposal)
    if problem is not None:
        return {"status": "denied", "reason": problem, "correlation_id": correlation_id}
    if proposal.get("status") != "open":
        return refused(f"proposal is not open (status: {describe_value(proposal.get('status'))})")
    expires = parse_utc(proposal.get("expires_at"))
    if expires is None or now >= expires:
        return refused("proposal has expired")
    stored_hash = proposal.get("diff_hash")
    if not isinstance(diff_hash, str) or not isinstance(stored_hash, str) or not hmac.compare_digest(
        diff_hash.encode("utf-8", "replace"), stored_hash.encode("utf-8", "replace")
    ):
        return refused("diff_hash does not match the proposal")

    base_version = proposal.get("base_version")
    if base_version is not None and (isinstance(base_version, bool) or not isinstance(base_version, int)):
        return refused("proposal file is corrupt (base_version)")
    if proposal.get("action") in VERIFICATION_OP_KEYS or "verification" in proposal:
        return _commit_verification(store, proposal, proposal_id, decision, now, correlation_id)

    lock = store.acquire_commit_lock()
    try:
        active = store.active_version()
        if active != base_version:
            return refused(f"stale proposal: built on version {base_version}, but the active version is {active}")
        base = store.read_version(base_version) if base_version is not None else None
        try:
            draft = TenantProfileV2.from_dict(proposal.get("draft"), source="<proposal draft>", allow_conflicts=True)
        except ProfileError as exc:
            return refused(f"proposal draft is invalid: {exc}")
        diff = compute_diff(base, draft)
        if compute_diff_hash(base_version, diff) != stored_hash:
            return refused("proposal content does not match its diff_hash")

        if decision == "reject":
            store.save_proposal(
                proposal_id, {**proposal, "status": "rejected", "decided_by": actor.actor, "decided_at": format_utc(now)}
            )
            return {"status": "rejected", "correlation_id": correlation_id}

        discovery_doc = store.read_discovery()
        snapshot = load_snapshot(discovery_doc)
        result = evaluate(draft, snapshot, keep_unvalidated=imported_keys(draft))
        if result.conflicts:
            return refused("conflicts: " + "; ".join(result.conflicts[:5]))
        if result.broken:
            return refused("active mapping(s) are broken: " + ", ".join(result.broken[:20]))
        version = _next_version(store, base_version)
        final = replace(apply_states(draft, result), profile_version=version)
        # Structural validation without allow_conflicts (the document as it will be stored).
        TenantProfileV2.from_dict(final.to_dict(), source="<commit>")
        now_text = format_utc(now)
        store.write_version(final)
        store.set_active(version, now_text, actor.actor, correlation_id)
        store.append_history(
            {
                "correlation_id": correlation_id,
                "timestamp": now_text,
                "actor": actor.actor,
                "action": proposal.get("action") if proposal.get("action") in ("commit", "rollback", "import") else "commit",
                "version": version,
                "prev_version": base_version,
                "proposal_id": proposal_id,
                "diff": diff,
                "validation_summary": result.summary(),
            }
        )
        store.save_proposal(
            proposal_id,
            {**proposal, "status": "committed", "decided_by": actor.actor, "decided_at": now_text, "version": version},
        )
        if discovery_doc is not None:
            store.write_discovery(
                {
                    **discovery_doc,
                    "drift_unresolved": False,
                    "last_validation": {
                        "version": version,
                        "checked_at": now_text,
                        "states": result.state_map(),
                        "summary": result.summary(),
                    },
                }
            )
        return {"status": "committed", "version": version, "correlation_id": correlation_id}
    finally:
        store.release_commit_lock(lock)


# ---------------------------------------------------------------------- #
# Phase 5A: admin verification procedures (D-5A-15 / A3-4) and SSO enablement (A3-6)
# ---------------------------------------------------------------------- #
#
# Shared mode only. Each op is a single-op proposal with a synthetic diff entry
# (``verification:note_write`` / ``verification:sso_login``), bound by the normal
# 4A diff hash, actor and expiry, and committed through its own branch that writes
# no profile version: only an append-only record under ``<setup_store>/verifications/``
# plus a history line. Nothing here ever runs automatically.

VERIFICATION_OP_KEYS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "authorize_note_write_verification": (frozenset({"target_type", "target_id", "action_type"}), frozenset()),
    "reset_note_write_verification": (frozenset(), frozenset()),
    "enable_sso_login": (frozenset({"verification_id"}), frozenset()),
    "disable_sso_login": (frozenset(), frozenset()),
}
VERIFICATION_TARGET_TYPES = ("candidate", "client_contact")
AUTHORIZATION_TTL = _dt.timedelta(hours=24)
VERIFICATIONS_DIR = "verifications"
NOTE_WRITE_LOG = "note_write.jsonl"
SSO_LOGIN_LOG = "sso_login.jsonl"
CONSUMED_DIR = "note_write_consumed"
MAX_VERIFICATION_LINES = 100_000
MAX_VERIFICATION_BYTES = 20_000_000
_HEX32_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)


def _verification_dir(store: SetupStore, *parts: str) -> Path:
    path = store.root
    for part in (VERIFICATIONS_DIR, *parts):
        path = path / part
        try:
            path.mkdir(exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise SetupStoreError(f"setup store: {part}/ is not a directory")
        except SetupStoreError:
            raise
        except OSError as exc:
            raise SetupStoreError(f"setup store: could not create {part}/ ({type(exc).__name__})") from exc
    return path


def append_verification(store: SetupStore, name: str, entry: dict[str, Any]) -> None:
    """Append one line to ``verifications/<name>`` (append-only; never rewritten)."""
    line = json.dumps(entry, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    try:
        with open(_verification_dir(store) / name, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise SetupStoreError(f"setup store: could not append to verifications/{name} ({type(exc).__name__})") from exc


def read_verifications(store: SetupStore, name: str) -> list[dict[str, Any]]:
    """Every line of ``verifications/<name>``; an unreadable line reads as ``{"corrupt": True}``."""
    path = _verification_dir(store) / name
    try:
        if path.is_symlink():
            return [{"corrupt": True}]
        if not path.exists():
            return []
        if path.stat().st_size > MAX_VERIFICATION_BYTES:
            return [{"corrupt": True}]
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [{"corrupt": True}]
    out: list[dict[str, Any]] = []
    for line in text.splitlines()[:MAX_VERIFICATION_LINES]:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            item = None
        out.append(item if isinstance(item, dict) else {"corrupt": True})
    return out


def _since_reset(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = max((i for i, e in enumerate(lines) if e.get("kind") == "reset"), default=-1)
    return lines[last + 1 :]


def note_write_generation(store: SetupStore) -> int:
    """L-6: the number of committed resets (part of the verification ledger key)."""
    return sum(1 for e in read_verifications(store, NOTE_WRITE_LOG) if e.get("kind") == "reset")


def note_write_verdict(store: SetupStore) -> dict[str, Any] | None:
    """The latest verdict since the last reset (``{"corrupt": True}`` fails closed as a negative verdict)."""
    lines = _since_reset(read_verifications(store, NOTE_WRITE_LOG))
    if any(e.get("corrupt") is True for e in lines):
        return {"kind": "verdict", "positive": False, "corrupt": True}
    verdicts = [e for e in lines if e.get("kind") == "verdict"]
    return verdicts[-1] if verdicts else None


def note_write_enabled(store: SetupStore, rest_url_fingerprint: str | None) -> bool:
    """D-5A-15 step 3: a positive verdict whose ``rest_url_fingerprint`` is unchanged."""
    verdict = note_write_verdict(store)
    return (
        verdict is not None
        and verdict.get("positive") is True
        and isinstance(rest_url_fingerprint, str)
        and verdict.get("rest_url_fingerprint") == rest_url_fingerprint
    )


def is_authorization_consumed(store: SetupStore, authorization_id: str) -> bool:
    if not isinstance(authorization_id, str) or not _HEX32_RE.fullmatch(authorization_id):
        return True
    path = _verification_dir(store, CONSUMED_DIR) / f"{authorization_id}.consumed"
    return path.exists() or path.is_symlink()


def consume_note_authorization(store: SetupStore, authorization_id: str, entry: dict[str, Any]) -> bool:
    """Exclusive create: ``True`` only for the single caller that consumes the authorization."""
    if not isinstance(authorization_id, str) or not _HEX32_RE.fullmatch(authorization_id):
        return False
    path = _verification_dir(store, CONSUMED_DIR) / f"{authorization_id}.consumed"
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=True))
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        return False
    except OSError as exc:
        raise SetupStoreError(f"setup store: could not consume the authorization ({type(exc).__name__})") from exc
    return True


def find_note_authorization(
    store: SetupStore,
    *,
    tenant_key: str,
    principal: str,
    target_type: str,
    target_id: int,
    action_type: str,
    has_associations: bool,
    now: _dt.datetime,
    executing: str | None = None,
) -> dict[str, Any] | None:
    """The open authorization for exactly this ``(tenant, principal, target, action)`` with no associations.

    5A triage B-3: it is also bound to the execution-identity label it was committed under.
    """
    if has_associations:
        return None
    lines = _since_reset(read_verifications(store, NOTE_WRITE_LOG))
    if any(e.get("corrupt") is True or e.get("kind") == "verdict" for e in lines):
        return None
    auths = [e for e in lines if e.get("kind") == "authorization"]
    if not auths:
        return None
    auth = auths[-1]
    expires = parse_utc(auth.get("expires_at"))
    if (
        auth.get("tenant_key") != tenant_key
        or auth.get("principal") != principal
        or executing is None
        or auth.get("executing_bullhorn_identity") != executing
        or auth.get("target_type") != target_type
        or type(auth.get("target_id")) is not int
        or auth.get("target_id") != target_id
        or auth.get("action_type") != action_type
        or expires is None
        or now >= expires
        or is_authorization_consumed(store, str(auth.get("authorization_id")))
    ):
        return None
    return auth


def sso_client_fingerprint(tenant: Any) -> str:
    """The OAuth client configuration a verification attests (no secret is included)."""
    o = tenant.oauth
    payload = json.dumps([o.client_id, o.redirect_uri, o.auth_url, o.login_url], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def sso_login_enabled(store: SetupStore, tenant: Any) -> bool:
    """A3-6: enabled only by a committed ``enable_sso_login`` for the *current* auth host and client config."""
    lines = read_verifications(store, SSO_LOGIN_LOG)
    if any(e.get("corrupt") is True for e in lines):
        return False
    switches = [e for e in lines if e.get("kind") in ("enabled", "disabled")]
    if not switches or switches[-1].get("kind") != "enabled":
        return False
    last = switches[-1]
    return last.get("tenant_key") == tenant.tenant_key and last.get("client_fingerprint") == sso_client_fingerprint(tenant)


def _positive_sso_observation(store: SetupStore, verification_id: Any, tenant: Any) -> dict[str, Any] | None:
    if not isinstance(verification_id, str) or not _HEX32_RE.fullmatch(verification_id):
        return None
    for e in read_verifications(store, SSO_LOGIN_LOG):
        if (
            e.get("kind") == "observation"
            and e.get("verification_id") == verification_id
            and e.get("positive") is True
            and e.get("tenant_key") == tenant.tenant_key
            and e.get("client_fingerprint") == sso_client_fingerprint(tenant)
        ):
            return e
    return None


def _verification_identity() -> Any:
    """The caller of a verification op: shared mode, a setup admin with a linked Bullhorn session."""
    from ..identity import principal

    try:
        ident = principal.shared_identity()
    except principal.IdentityRequired:
        raise ProfileError(["identity_required"], "<changes>") from None
    if ident is None:
        raise ProfileError(["unsupported_in_local_mode: verification operations require shared mode"], "<changes>")
    if ident.access_tier != "bullhorn_user" or ident.tenant is None or not ident.tenant.roles.is_setup_admin(ident.principal_key):
        raise ProfileError(["denied: a setup admin with a linked Bullhorn session is required"], "<changes>")
    return ident


def _verification_payload(op: str, change: dict[str, Any]) -> dict[str, Any]:
    if op == "authorize_note_write_verification":
        return {"target_type": change.get("target_type"), "target_id": change.get("target_id"), "action_type": change.get("action_type")}
    if op == "enable_sso_login":
        return {"verification_id": change.get("verification_id")}
    return {}


def _verification_diff(op: str, payload: Any) -> list[dict[str, Any]]:
    key = "verification:sso_login" if op in ("enable_sso_login", "disable_sso_login") else "verification:note_write"
    return [{"key": key, "change": op, "old": None, "new": payload}]


def _verification_problems(op: str, payload: dict[str, Any], store: SetupStore, ident: Any) -> list[str]:
    """Preconditions, checked at propose and again at commit."""
    tenant = ident.tenant
    if op == "authorize_note_write_verification":
        problems = []
        if payload.get("target_type") not in VERIFICATION_TARGET_TYPES:
            problems.append(f"target_type must be one of {list(VERIFICATION_TARGET_TYPES)}")
        tid = payload.get("target_id")
        if type(tid) is not int or not 1 <= tid <= 2**63 - 1:
            problems.append("target_id must be an int >= 1")
        action = payload.get("action_type")
        if not isinstance(action, str) or not action.strip() or len(action) > MAX_NOTE_ACTION_CHARS:
            problems.append("action_type must be a non-blank string")
        if note_write_verdict(store) is not None:
            problems.append("a note-write verdict already exists; commit reset_note_write_verification first")
        return problems
    if op in ("enable_sso_login", "disable_sso_login"):
        if tenant is None or not tenant.sso:
            return ["this tenant is not configured as an SSO tenant"]
        if op == "enable_sso_login" and _positive_sso_observation(store, payload.get("verification_id"), tenant) is None:
            return ["verification_id must refer to a positive SSO verification login for the current auth host and client"]
    return []


def _propose_verification(store: SetupStore, changes: list[Any], base_version: Any, now: _dt.datetime) -> dict[str, Any]:
    if len(changes) != 1:
        raise ProfileError(["changes: a verification operation must be the only operation in its proposal"], "<changes>")
    change = changes[0]
    op = change.get("op")
    required, optional = VERIFICATION_OP_KEYS[op]
    errors = [
        f"changes[0] ({op}): unknown key {describe_value(k)}" for k in change if k != "op" and k not in required and k not in optional
    ]
    errors += [f"changes[0] ({op}): missing required key {k!r}" for k in sorted(required) if k not in change]
    if errors:
        raise ProfileError(errors, "<changes>")
    ident = _verification_identity()
    active = store.active_version()
    if base_version is not None and (isinstance(base_version, bool) or base_version != active):
        raise ProfileError(
            [f"base_version {describe_value(base_version)} is stale: the active version is {describe_value(active)}"], "<changes>"
        )
    payload = _verification_payload(op, change)
    problems = _verification_problems(op, payload, store, ident)
    if problems:
        raise ProfileError([f"changes[0] ({op}): {p}" for p in problems], "<changes>")
    diff = _verification_diff(op, payload)
    digest = compute_diff_hash(active, diff)
    proposal_id = uuid.uuid4().hex
    now_text = format_utc(now)
    proposal = {
        "proposal_id": proposal_id,
        "base_version": active,
        "action": op,
        "changes": changes,
        "verification": payload,
        "diff": diff,
        "diff_hash": digest,
        "conflicts": [],
        "dependencies": {},
        "validation": None,
        "defaults": None,
        "actor": ident.principal_key,
        "initiating_principal": ident.principal_key,
        "tenant_key": ident.tenant_key,
        "created_at": now_text,
        "expires_at": format_utc(now + PROPOSAL_TTL),
        "status": "open",
    }
    store.save_proposal(proposal_id, proposal, create=True)
    return {
        "proposal_id": proposal_id,
        "diff_hash": digest,
        "expires_at": proposal["expires_at"],
        "base_version": active,
        "diff": diff,
        "conflicts": [],
        "dependencies": {},
        "validation": None,
        "defaults": None,
    }


def _ownership_problem(proposal: Mapping[str, Any]) -> str | None:
    """A3-3: in shared mode only the proposal's creator (same tenant) may commit or reject it."""
    from ..identity import principal

    try:
        ident = principal.shared_identity()
    except principal.IdentityRequired:
        return "identity_required"
    if ident is None:
        return None
    if proposal.get("initiating_principal") != ident.principal_key or proposal.get("tenant_key") != ident.tenant_key:
        return "not_owner"
    return None


def _commit_verification(
    store: SetupStore, proposal: dict[str, Any], proposal_id: str, decision: str, now: _dt.datetime, correlation_id: str
) -> dict[str, Any]:
    def refused(reason: str) -> dict[str, Any]:
        return {"status": "refused", "reason": truncate_text(reason, 1000), "correlation_id": correlation_id}

    try:
        ident = _verification_identity()
    except ProfileError as exc:
        return refused(exc.errors[0] if exc.errors else "denied")
    op = proposal.get("action")
    payload = proposal.get("verification")
    if not isinstance(op, str) or op not in VERIFICATION_OP_KEYS or not isinstance(payload, dict):
        return refused("proposal file is corrupt (verification)")
    expected = _verification_diff(op, payload)
    if proposal.get("diff") != expected or compute_diff_hash(proposal.get("base_version"), expected) != proposal.get("diff_hash"):
        return refused("proposal content does not match its diff_hash")
    lock = store.acquire_commit_lock()
    try:
        now_text = format_utc(now)
        if decision == "reject":
            store.save_proposal(
                proposal_id, {**proposal, "status": "rejected", "decided_by": ident.principal_key, "decided_at": now_text}
            )
            return {"status": "rejected", "correlation_id": correlation_id}
        problems = _verification_problems(op, payload, store, ident)
        if problems:
            return refused("; ".join(problems))
        base = {"principal": ident.principal_key, "tenant_key": ident.tenant_key, "at": now_text,
                "executing_bullhorn_identity": ident.executing_bullhorn_identity,
                "correlation_id": correlation_id, "proposal_id": proposal_id}
        extra: dict[str, Any] = {}
        if op == "authorize_note_write_verification":
            authorization_id = uuid.uuid4().hex
            append_verification(store, NOTE_WRITE_LOG, {
                **base, "kind": "authorization", "authorization_id": authorization_id, **payload,
                "expires_at": format_utc(now + AUTHORIZATION_TTL),
            })
            extra = {"authorization_id": authorization_id, "expires_at": format_utc(now + AUTHORIZATION_TTL)}
        elif op == "reset_note_write_verification":
            append_verification(store, NOTE_WRITE_LOG, {**base, "kind": "reset"})
        else:
            tenant = ident.tenant
            append_verification(store, SSO_LOGIN_LOG, {
                **base,
                "kind": "enabled" if op == "enable_sso_login" else "disabled",
                "verification_id": payload.get("verification_id"),
                "auth_url": tenant.oauth.auth_url,
                "client_fingerprint": sso_client_fingerprint(tenant),
            })
        store.append_history(
            {
                "correlation_id": correlation_id,
                "timestamp": now_text,
                "actor": ident.principal_key,
                "action": op,
                "version": None,
                "prev_version": proposal.get("base_version"),
                "proposal_id": proposal_id,
                "diff": expected,
                "validation_summary": None,
            }
        )
        store.save_proposal(proposal_id, {**proposal, "status": "committed", "decided_by": ident.principal_key, "decided_at": now_text})
        return {"status": "committed", "correlation_id": correlation_id, "action": op, **extra}
    finally:
        store.release_commit_lock(lock)


def _proposal_owner() -> dict[str, Any] | None:
    """Shared mode: the creator recorded on a proposal (A3-3). ``None`` in local mode."""
    from ..identity import principal

    ident = principal.shared_identity()
    if ident is None:
        return None
    return {"initiating_principal": ident.principal_key, "tenant_key": ident.tenant_key}


__all__ = [
    "DEFAULT_TIMEZONE",
    "MAX_CHANGES",
    "PROPOSAL_TTL",
    "commit",
    "compute_dependencies",
    "compute_diff",
    "compute_diff_hash",
    "is_open",
    "propose",
]
