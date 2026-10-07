"""Capability -> required-mapping registry (TS-7, D-4A-6).

Each capability is gated on its own requirements, so one capability can be
``ok`` while another is unresolved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schema.errors import path_segment
from .profile_v2 import TenantProfileV2, load_activity_concepts


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

# ---------------------------------------------------------------------- #
# Phase 5C (Amendment C3-2): state-only entries, so the 4A aggregation is unchanged.
# Concept-mapping and setting requirements are evaluated by ``concept_requirements``
# and surfaced only in ``requirement_details`` and the tools' ``missing_requirements``.
# ---------------------------------------------------------------------- #

READ_STATES = ("setup_valid", "setup_revalidation_required")
# Triage L-1: the single concept source is ``mappings/activity_concepts.yaml`` (D4).
ACTIVITY_CONCEPTS: tuple[str, ...] = tuple(sorted(load_activity_concepts()))
# Vocabulary §2 kind "state": derived at read time, never a stored mapping (``find_records`` rejects them).
DERIVED_STATE_CONCEPTS = frozenset({"interview_upcoming", "offer_pending"})


def _activity_requirement(concept: str) -> CapabilityRequirement:
    if concept == "note_created":  # as get_notes (4B): notes.read states
        return CapabilityRequirement(states=READ_STATES)
    if concept == "job_created":  # D-5C-14: attribution job.primary_recruiter_id is config-required
        return CapabilityRequirement(field_mappings=(("job", "primary_recruiter_id"),), states=("setup_valid",))
    return CapabilityRequirement(states=("setup_valid",))  # D-5C-6: every other concept needs setup_valid


PHASE5C_CAPABILITIES: Mapping[str, CapabilityRequirement] = {
    "records.find": CapabilityRequirement(states=READ_STATES, all_field_mappings=True),
    "records.user": CapabilityRequirement(states=READ_STATES),
    **{f"activity.{c}": _activity_requirement(c) for c in ACTIVITY_CONCEPTS},
}
CAPABILITIES = {**CAPABILITIES, **PHASE5C_CAPABILITIES}


@dataclass(frozen=True)
class ConceptRule:
    """Tenant-configuration requirements of one concept (§2). Every group must be satisfied.

    A group is a tuple of ``(entity, concept)`` alternatives: at least one active,
    valid concept mapping must match one of them.
    """

    groups: tuple[tuple[tuple[str, str], ...], ...] = ()
    settings: tuple[str, ...] = ()


_SCHEDULED = (("appointment", "interview_scheduled"),)
_CANCELLED = (("appointment", "interview_cancelled"),)
OFFER_ENTITIES = ("submission", "placement")


def _offer(*concepts: str) -> tuple[tuple[str, str], ...]:
    return tuple((e, c) for c in concepts for e in OFFER_ENTITIES)


CONCEPT_RULES: Mapping[str, ConceptRule] = {
    "client_submission": ConceptRule(groups=((("submission", "client_submission"),),), settings=("client_submission_dating",)),
    "interview_scheduled": ConceptRule(groups=(_SCHEDULED,)),
    "interview_completed": ConceptRule(
        groups=(_SCHEDULED, (("appointment", "interview_completed"),)), settings=("interview_completion_rule",)
    ),
    "interview_cancelled": ConceptRule(groups=(_SCHEDULED, _CANCELLED)),
    "interview_upcoming": ConceptRule(groups=(_SCHEDULED, _CANCELLED)),
    "offer_extended": ConceptRule(groups=(_offer("offer_extended"),), settings=("client_submission_dating",)),
    "offer_accepted": ConceptRule(groups=(_offer("offer_accepted"),), settings=("client_submission_dating",)),
    "offer_declined": ConceptRule(groups=(_offer("offer_declined"),), settings=("client_submission_dating",)),
    "offer_pending": ConceptRule(
        groups=(_offer("offer_extended"), _offer("offer_accepted", "offer_declined")), settings=("client_submission_dating",)
    ),
}


def concept_mappings(profile: TenantProfileV2 | None, states: Mapping[str, str], entity: str, concept: str) -> list[Any]:
    """Active, valid concept value mappings ``-> concept`` on a field of ``entity``."""
    if profile is None:
        return []
    return [
        rec
        for rec in profile.value_mappings
        if rec.active
        and rec.target.kind == "concept"
        and rec.target.name == concept
        and rec.entity == entity
        and states.get(rec.diff_key, rec.validation.state) == "valid"
    ]


def ordering_values(profile: TenantProfileV2 | None, states: Mapping[str, str], entity: str, name: str) -> list[Any] | None:
    """The values of the active, non-broken ordering mapping for ``(entity, name)`` (``None`` when there is none)."""
    if profile is None:
        return None
    for vrec in profile.value_mappings:
        if (
            vrec.active
            and vrec.target.kind == "ordering"
            and vrec.target.entity == entity
            and vrec.target.field == name
            and states.get(vrec.diff_key, vrec.validation.state) != "broken"
        ):
            return list(vrec.values)
    return None


def concept_entities(concept: str) -> tuple[str, ...]:
    """The entities that may carry ``concept``'s own value mapping, from ``CONCEPT_RULES`` (``()`` when none)."""
    rule = CONCEPT_RULES.get(concept)
    if rule is None:
        return ()
    return tuple(dict.fromkeys(e for group in rule.groups for e, c in group if c == concept))


def _group_requirement(group: tuple[tuple[str, str], ...]) -> str:
    entities = "|".join(dict.fromkeys(e for e, _ in group))
    concepts = "|".join(dict.fromkeys(c for _, c in group))
    return f"value_mapping:{entities}:{concepts}"


def concept_requirements(concept: str, profile: TenantProfileV2 | None, states: Mapping[str, str]) -> tuple[str, ...]:
    """Missing tenant configuration for ``concept`` (TS-7 strings); ``()`` when every requirement holds."""
    rule = CONCEPT_RULES.get(concept)
    if rule is None:
        return ()
    missing: list[str] = []
    for group in rule.groups:
        if not any(concept_mappings(profile, states, e, c) for e, c in group):
            missing.append(_group_requirement(group))
    for name in rule.settings:
        if profile is None or getattr(profile.settings, name, None) is None:
            missing.append(f"setting:{name}")
    if (
        concept == "interview_completed"
        and profile is not None
        and getattr(profile.settings, "interview_completion_rule", None) == "end_passed_not_cancelled"
        and not concept_mappings(profile, states, "appointment", "interview_cancelled")
    ):
        missing.append(_group_requirement(_CANCELLED))
    return tuple(missing)


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
        if not hit and cap.startswith("activity."):  # Phase 5C: concept mappings and settings
            rule = CONCEPT_RULES.get(cap[len("activity."):])
            if rule is not None and key.startswith("value:"):
                wanted = {c for group in rule.groups for _, c in group}
                for content in contents:
                    target = content.get("target") if isinstance(content, dict) else None
                    if isinstance(target, dict) and target.get("kind") == "concept" and target.get("name") in wanted:
                        hit = True
            elif rule is not None and key.startswith("setting:") and key[len("setting:"):] in rule.settings:
                hit = True
        if hit:
            out.append(cap)
    return out
