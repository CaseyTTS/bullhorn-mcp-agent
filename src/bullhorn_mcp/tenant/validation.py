"""Validation of a v2 profile against the latest discovery snapshot.

Structural validation lives in ``TenantProfileV2.from_dict``. This module adds
the meta-snapshot checks, conflicts and the summary that proposals, commits
and ``manage_mapping_profile(action="validate")`` report.

Record states:

- ``valid``: every raw source is present in the snapshot.
- ``broken``: a raw source is absent from a usable snapshot.
- ``unresolved``: the entity's discovery failed or its metadata is unusable.
- ``unvalidated``: no snapshot covers the entity (or the record was imported
  and has not been validated yet; see ``keep_unvalidated``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from ..schema.bullhorn_catalog import BullhornCatalog, load_bullhorn_catalog
from ..schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from ..schema.errors import describe_value, truncate_text
from .profile_v2 import (
    MAX_DETAIL_CHARS,
    FieldMappingRecord,
    RecordValidation,
    TenantProfileV2,
    ValueMappingRecord,
    find_value_conflicts,
    tagged,
)
from .revalidation import DiscoverySnapshot, EntitySnapshot

IMPORTED_DETAIL = "imported; not yet validated against metadata"


@dataclass(frozen=True)
class EvaluationResult:
    states: dict[str, RecordValidation] = field(default_factory=dict)
    broken: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    unvalidated: tuple[str, ...] = ()
    values_unverified: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    snapshot_checked_at: str | None = None

    @property
    def ok(self) -> bool:
        return not self.broken and not self.conflicts

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot_checked_at": self.snapshot_checked_at,
            "broken": list(self.broken),
            "conflicts": list(self.conflicts),
            "unresolved": list(self.unresolved),
            "unvalidated": list(self.unvalidated),
            "values_unverified": list(self.values_unverified),
        }

    def state_map(self) -> dict[str, str]:
        return {k: v.state for k, v in self.states.items()}


def _detail(text: str) -> str:
    return truncate_text(text, MAX_DETAIL_CHARS)


def _entity_problem(snap: EntitySnapshot | None) -> RecordValidation | None:
    if snap is None:
        return RecordValidation(state="unvalidated", detail="no discovery snapshot for this entity")
    if snap.error is not None:
        return RecordValidation(state="unresolved", detail=_detail(f"discovery failed: {snap.error}"))
    if snap.meta_unusable:
        return RecordValidation(state="unresolved", detail="entity metadata is unusable")
    return None


def evaluate_field_record(rec: FieldMappingRecord, snapshot: DiscoverySnapshot | None) -> RecordValidation:
    snap = snapshot.entity(rec.entity) if snapshot else None
    problem = _entity_problem(snap)
    checked_at = snapshot.checked_at if snapshot else None
    if problem is not None:
        return replace(problem, checked_at=checked_at if problem.state != "unvalidated" else None)
    assert snap is not None
    missing = [s for s in rec.target.sources if s not in snap.fields]
    if missing:
        return RecordValidation(
            state="broken",
            checked_at=checked_at,
            detail=_detail("raw source(s) absent from metadata: " + ", ".join(describe_value(m) for m in missing)),
        )
    return RecordValidation(state="valid", checked_at=checked_at)


def evaluate_value_record(rec: ValueMappingRecord, snapshot: DiscoverySnapshot | None) -> RecordValidation:
    snap = snapshot.entity(rec.entity) if snapshot else None
    problem = _entity_problem(snap)
    checked_at = snapshot.checked_at if snapshot else None
    if problem is not None:
        return replace(
            problem,
            checked_at=checked_at if problem.state != "unvalidated" else None,
            values_unverified=True,
        )
    assert snap is not None
    fsnap = snap.fields.get(rec.bullhorn_field)
    if fsnap is None:
        return RecordValidation(
            state="broken",
            checked_at=checked_at,
            detail=_detail(f"field {describe_value(rec.bullhorn_field)} absent from metadata"),
            values_unverified=True,
        )
    allowed = fsnap.option_values()
    if allowed is None:
        # HV-A2: options may be missing from the meta response; values are administrator-confirmed.
        return RecordValidation(
            state="valid", checked_at=checked_at, detail="field options not present in metadata", values_unverified=True
        )
    gone = [v for v in rec.values if tagged(v) not in allowed]
    if gone:
        return RecordValidation(
            state="valid",
            checked_at=checked_at,
            detail=_detail("values not among field options: " + ", ".join(describe_value(v) for v in gone[:10])),
            values_unverified=True,
        )
    return RecordValidation(state="valid", checked_at=checked_at, values_unverified=False)


def evaluate(
    profile: TenantProfileV2,
    snapshot: DiscoverySnapshot | None,
    *,
    keep_unvalidated: Iterable[str] = (),
    canonical: CanonicalCatalog | None = None,
    bullhorn: BullhornCatalog | None = None,
) -> EvaluationResult:
    """Evaluate every record against ``snapshot``.

    Keys in ``keep_unvalidated`` stay ``unvalidated`` unless the snapshot
    shows them ``broken`` (imported records wait for an explicit validate).
    """
    canonical = canonical or load_canonical_catalog()
    bullhorn = bullhorn or load_bullhorn_catalog()
    keep = set(keep_unvalidated)
    states: dict[str, RecordValidation] = {}
    broken: list[str] = []
    unvalidated: list[str] = []
    unverified: list[str] = []
    unresolved_records: set[str] = set()

    for rec in profile.field_mappings:
        result = evaluate_field_record(rec, snapshot)
        if rec.key in keep and result.state != "broken":
            result = RecordValidation(state="unvalidated", detail=IMPORTED_DETAIL)
        states[rec.key] = result
        if not rec.active:
            continue
        if result.state == "broken":
            broken.append(rec.key)
        elif result.state == "unvalidated":
            unvalidated.append(rec.key)
        elif result.state == "unresolved":
            unresolved_records.add(f"{rec.entity}.{rec.field}")

    for vrec in profile.value_mappings:
        vresult = evaluate_value_record(vrec, snapshot)
        if vrec.diff_key in keep and vresult.state != "broken":
            vresult = RecordValidation(
                state="unvalidated", detail=IMPORTED_DETAIL, values_unverified=True
            )
        states[vrec.diff_key] = vresult
        if not vrec.active:
            continue
        if vresult.state == "broken":
            broken.append(vrec.diff_key)
        elif vresult.state == "unvalidated":
            unvalidated.append(vrec.diff_key)
        if vresult.values_unverified:
            unverified.append(vrec.diff_key)

    unresolved: list[str] = []
    for canon in canonical.entities:
        if bullhorn.bullhorn_entity_for(canon) is None:
            continue
        for fname in canonical.entity(canon).field_names:
            if fname == "id":
                continue
            rec_active = profile.field_record(canon, fname, active_only=True)
            if rec_active is None or f"{canon}.{fname}" in unresolved_records:
                unresolved.append(f"{canon}.{fname}")

    return EvaluationResult(
        states=states,
        broken=tuple(broken),
        unresolved=tuple(unresolved),
        unvalidated=tuple(unvalidated),
        values_unverified=tuple(unverified),
        conflicts=tuple(find_value_conflicts(profile.value_mappings)),
        snapshot_checked_at=snapshot.checked_at if snapshot else None,
    )


def apply_states(profile: TenantProfileV2, result: EvaluationResult) -> TenantProfileV2:
    """Return ``profile`` with each record's ``validation`` replaced by its evaluated state."""
    fields = tuple(replace(r, validation=result.states.get(r.key, r.validation)) for r in profile.field_mappings)
    values = tuple(replace(r, validation=result.states.get(r.diff_key, r.validation)) for r in profile.value_mappings)
    return replace(profile, field_mappings=fields, value_mappings=values)


def imported_keys(profile: TenantProfileV2) -> set[str]:
    """Keys of imported records that have not been validated against metadata yet."""

    def pending(v: RecordValidation) -> bool:
        return v.state == "unvalidated" and v.detail == IMPORTED_DETAIL

    keys = {r.key for r in profile.field_mappings if pending(r.validation)}
    keys |= {r.diff_key for r in profile.value_mappings if pending(r.validation)}
    return keys
