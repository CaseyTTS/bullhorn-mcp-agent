"""Tenant note action types (D-4B-11, D-4B-12).

The valid set is the union of the values of the **active** ``note_action``
value mappings of the active profile whose effective validation state is
``valid`` (``values_unverified`` is allowed: HV-B10 / HV-A2). An unknown
value is rejected with the valid values and suggestions; it is **never**
substituted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schema.errors import describe_value
from ..tenant.profile_v2 import TenantProfileV2

MAX_VALID_VALUES = 100
MAX_SUGGESTIONS = 10
MAX_INPUT_CHARS = 200
REQUIREMENT = "value_mapping:note.action:note_action"


@dataclass(frozen=True)
class ActionTypeSet:
    values: tuple[str, ...] = ()
    semantics: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.values)


@dataclass(frozen=True)
class Rejection:
    value: str
    valid_values: tuple[str, ...]
    suggestions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "unknown_action_type",
            "message": f"action type {self.value} is not a valid action type for this tenant; it was not substituted",
            "valid_values": list(self.valid_values),
            "suggestions": list(self.suggestions),
        }


def valid_set(profile: TenantProfileV2 | None, states: Mapping[str, str] | None = None) -> ActionTypeSet:
    """The tenant's usable note action values (in profile order) and their semantic tags."""
    if profile is None:
        return ActionTypeSet()
    states = states or {}
    values: list[str] = []
    semantics: dict[str, str | None] = {}
    for rec in profile.value_mappings:
        if not rec.active or rec.target.kind != "note_action":
            continue
        if states.get(rec.diff_key, rec.validation.state) != "valid":
            continue
        for v in rec.values:
            if isinstance(v, str) and v not in semantics:
                values.append(v)
                semantics[v] = rec.target.semantic
            elif isinstance(v, str) and semantics.get(v) is None:
                semantics[v] = rec.target.semantic
    return ActionTypeSet(tuple(values), semantics)


def _norm(text: str) -> str:
    return " ".join(text.replace("_", " ").replace("-", " ").casefold().split())


def suggestions_for(value: str, aset: ActionTypeSet) -> tuple[str, ...]:
    """Tenant values whose label or tag matches ``value`` case-insensitively (suggestions only)."""
    needle = _norm(value[:MAX_INPUT_CHARS])
    if not needle:
        return ()
    out: list[str] = []
    for v in aset.values:
        label = _norm(v)
        tag = aset.semantics.get(v)
        if label == needle or (tag is not None and _norm(tag) == needle) or (len(needle) >= 3 and (needle in label or label in needle)):
            out.append(v)
        if len(out) >= MAX_SUGGESTIONS:
            break
    return tuple(out)


def validate(value: object, aset: ActionTypeSet) -> str | Rejection:
    """Return ``value`` itself when it is exactly a tenant value; otherwise a ``Rejection``."""
    if isinstance(value, str) and value in aset.semantics:
        return value
    shown = describe_value(value)
    suggestions = suggestions_for(value, aset) if isinstance(value, str) else ()
    return Rejection(shown, aset.values[:MAX_VALID_VALUES], suggestions)
