"""The activity event (``CANONICAL_ACTIVITY_VOCABULARY.md`` §1).

One shape for every concept. Phase 4B emits only ``note_created``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ORIGINS = ("observed", "written_by_mcp")
LINK_KEYS = (
    "candidate_id",
    "job_id",
    "recruiter_id",
    "owner_id",
    "author_id",
    "client_corporation_id",
    "client_contact_id",
    "submission_id",
    "appointment_id",
    "placement_id",
    "note_id",
    "offer_ref",
)


def tagged_json(value: Any) -> str:
    """Type-tagged canonical JSON: ``1``, ``"1"``, ``True`` and ``1.0`` never collide."""

    def tag(v: Any) -> Any:
        if v is None:
            return ["n"]
        if isinstance(v, bool):
            return ["b", v]
        if isinstance(v, int):
            return ["i", str(v)]
        if isinstance(v, float):
            return ["f", repr(v)]
        if isinstance(v, str):
            return ["s", v]
        if isinstance(v, (list, tuple)):
            return ["l", [tag(x) for x in v]]
        if isinstance(v, dict):
            return ["d", [[str(k), tag(v[k])] for k in sorted(v, key=str)]]
        raise TypeError(f"untaggable value of type {type(v).__name__}")

    return json.dumps(tag(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def make_activity_id(concept: str, entity: str, record_id: Any, discriminator: str = "") -> str:
    """Deterministic ``sha256`` hex of (concept, source entity, source record id, discriminator)."""
    return sha256_hex(tagged_json(["activity/v1", concept, entity, record_id, discriminator]))


@dataclass(frozen=True)
class ActivityEvent:
    activity_id: str
    concept: str
    occurred_at: str | None
    source: dict[str, Any]
    links: dict[str, Any]
    origin: str
    unresolved_links: tuple[str, ...] = ()
    state: Any = None
    attribution: Any = None
    definition: Any = None
    evidence: Any = None
    extra_links: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        links = {k: self.links.get(k) for k in LINK_KEYS}
        links.update(self.extra_links)
        return {
            "activity_id": self.activity_id,
            "concept": self.concept,
            "occurred_at": self.occurred_at,
            "state": self.state,
            "source": dict(self.source),
            "links": links,
            "unresolved_links": list(self.unresolved_links),
            "attribution": self.attribution,
            "definition": self.definition,
            "evidence": self.evidence,
            "origin": self.origin,
        }
