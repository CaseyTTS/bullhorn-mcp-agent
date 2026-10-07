"""Controlled configuration change: proposals (the dry-run) and commits (TS-4, D-4A-9).

``propose`` applies change operations to a draft built on the current active
version, validates it against the latest discovery snapshot, diffs it,
hashes the diff and stores the proposal. It writes nothing except
``proposals/``.

``commit`` re-validates and is refused - with nothing changed - when the
proposal is unknown, not open, expired, its hash does not match, its base is
stale, the actor is missing or not allowed, or the draft has a structural
error, a conflict, or an active ``broken`` mapping.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
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
    TENANT_ID_RE,
    FieldMappingRecord,
    RecordValidation,
    Settings,
    TenantProfileV2,
    ValueMappingRecord,
    current_catalog_fingerprint,
    parse_field_record,
    parse_value_record,
    records_by_key,
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
}


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
        self.fields: dict[tuple[str, str], FieldMappingRecord] = {(r.entity, r.field): r for r in base.field_mappings} if base else {}
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
            field_mappings=tuple(self.fields.values()),
            value_mappings=tuple(self.values.values()),
            settings=self.settings,
        )


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
        if name != "reporting_timezone":
            errors.append(f"{where}.name: {describe_value(name)} is not a known setting (known: ['reporting_timezone'])")
            return
        try:
            validate_timezone(value)
        except ValueError as exc:
            errors.append(f"{where}.value: {truncate_text(str(exc))}")
            return
        assert isinstance(value, str)
        draft.settings = Settings(reporting_timezone=value)
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

    raise AssertionError(op)  # pragma: no cover - _check_op_shape admits only known ops


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
    draft.fields = {(r.entity, r.field): r for r in target.field_mappings}
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
        draft.fields = {
            (r.entity, r.field): replace(r, validation=_imported(), created_at=r.created_at or ctx.now, updated_at=ctx.now)
            for r in doc.field_mappings
        }
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
        draft.fields = fields
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
