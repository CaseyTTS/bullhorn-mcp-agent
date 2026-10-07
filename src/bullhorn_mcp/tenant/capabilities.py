"""Capability -> required-mapping registry (TS-7, D-4A-6).

Each capability is gated on its own requirements, so one capability can be
``ok`` while another is unresolved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schema.errors import path_segment
from .profile_v2 import TenantProfileV2


@dataclass(frozen=True)
class CapabilityRequirement:
    # (entity, field) pairs that need an active field mapping in state "valid".
    field_mappings: tuple[tuple[str, str], ...] = ()
    # (entity, field) pairs that need an active "ordering" value mapping (not broken).
    orderings: tuple[tuple[str, str], ...] = ()
    # Setup states in which the capability is available (None: any state).
    states: tuple[str, ...] | None = None
    # Whether every field-mapping change affects the capability.
    all_field_mappings: bool = False


CAPABILITIES: Mapping[str, CapabilityRequirement] = {
    "jobs.priority": CapabilityRequirement(field_mappings=(("job", "priority"),), orderings=(("job", "priority"),)),
    "jobs.primary_recruiter": CapabilityRequirement(field_mappings=(("job", "primary_recruiter_id"),)),
    "canonical_reads": CapabilityRequirement(
        states=("setup_valid", "setup_revalidation_required"), all_field_mappings=True
    ),
    # Phase 4B (Amendment A1/C2): state-only requirements. The "at least one usable
    # note_action value mapping" rule for notes.create is enforced by writes/pipeline.py.
    "notes.read": CapabilityRequirement(states=("setup_valid", "setup_revalidation_required")),
    "notes.create": CapabilityRequirement(states=("setup_valid",)),
}


@dataclass(frozen=True)
class GateResult:
    ok: bool
    missing: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": list(self.missing)}


def field_requirement(entity: str, name: str) -> str:
    return f"mapping:{entity}.{name}"


def ordering_requirement(entity: str, name: str) -> str:
    return f"value_mapping:{entity}.{name}:ordering"


def evaluate_capability(
    name: str,
    profile: TenantProfileV2 | None,
    states: Mapping[str, str],
    setup_state: str,
) -> GateResult:
    """Gate one capability. ``states`` maps record keys to their effective validation state."""
    req = CAPABILITIES.get(name)
    if req is None:
        return GateResult(False, (f"capability:unknown:{path_segment(name)}",))
    missing: list[str] = []
    if req.states is not None and setup_state not in req.states:
        missing.append(f"state:{setup_state}")
    for entity, fname in req.field_mappings:
        rec = profile.field_record(entity, fname, active_only=True) if profile else None
        if rec is None or states.get(rec.key, rec.validation.state) != "valid":
            missing.append(field_requirement(entity, fname))
    for entity, fname in req.orderings:
        found = False
        if profile is not None:
            for vrec in profile.value_mappings:
                if (
                    vrec.active
                    and vrec.target.kind == "ordering"
                    and vrec.target.entity == entity
                    and vrec.target.field == fname
                    and states.get(vrec.diff_key, vrec.validation.state) != "broken"
                ):
                    found = True
                    break
        if not found:
            missing.append(ordering_requirement(entity, fname))
    return GateResult(not missing, tuple(missing))


def capabilities_for_key(key: str, contents: list[dict[str, Any] | None]) -> list[str]:
    """Capabilities affected by a change to ``key`` (``contents``: old and new record content)."""
    out: list[str] = []
    for cap, req in CAPABILITIES.items():
        hit = False
        if key.startswith("field:"):
            if req.all_field_mappings or key[len("field:"):] in {f"{e}.{f}" for e, f in req.field_mappings}:
                hit = True
        elif key.startswith("value:"):
            for content in contents:
                target = content.get("target") if isinstance(content, dict) else None
                if (
                    isinstance(target, dict)
                    and target.get("kind") == "ordering"
                    and (target.get("entity"), target.get("field")) in req.orderings
                ):
                    hit = True
        elif key.startswith("meta:") and req.states is not None:
            hit = True
        if hit:
            out.append(cap)
    return out
