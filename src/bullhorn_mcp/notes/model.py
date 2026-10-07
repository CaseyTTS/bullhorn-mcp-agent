"""The canonical Note (Phase 4B §1.3) and its ``note_created`` event.

``NoteRecord.from_bullhorn`` never raises on hostile shapes: a malformed
record yields ``None`` plus a warning, and a malformed optional field yields
``None`` for that field plus a warning. A link type whose association was not
read (or has no Note field at all) is listed in ``unresolved_links``; it is
never silently omitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..activity.events import ActivityEvent, make_activity_id
from ..schema.errors import describe_value
from ..tenant.timeutil import coerce_epoch_millis_to_utc_iso

CONCEPT = "note_created"
LINK_TYPES = (
    "candidate_ids",
    "job_ids",
    "client_contact_ids",
    "client_corporation_ids",
    "placement_ids",
    "submission_ids",
)
# Note to-many association fields per link type (HV-B2). Corporations and submissions have none.
_TO_MANY_SOURCES: Mapping[str, str] = {
    "candidate_ids": "candidates",
    "client_contact_ids": "clientContacts",
    "placement_ids": "placements",
    "job_ids": "jobOrders",
}
MAX_LINKS_PER_TYPE = 1000
MAX_WARNINGS = 20


def _plain_id(value: Any) -> int | None:
    return value if type(value) is int and 1 <= value <= 2**63 - 1 else None


def _to_one_id(raw: Mapping[str, Any], key: str, warnings: list[str]) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, dict):
        found = _plain_id(value.get("id"))
        if found is not None:
            return found
    warnings.append(f"{key}: unusable association value {describe_value(value)}")
    return None


def _to_many_ids(value: Any, key: str, warnings: list[str]) -> list[int] | None:
    """A to-many value (a list, or ``{total, data}``) -> sorted ids; ``None`` when unusable."""
    items: Any = value
    total: Any = None
    if isinstance(value, dict):
        items, total = value.get("data"), value.get("total")
    if not isinstance(items, list):
        warnings.append(f"{key}: unusable to-many value {describe_value(value)}")
        return None
    ids: set[int] = set()
    bad = 0
    for item in items[:MAX_LINKS_PER_TYPE]:
        found = _plain_id(item.get("id")) if isinstance(item, dict) else None
        if found is None:
            bad += 1
        else:
            ids.add(found)
    if bad:
        warnings.append(f"{key}: {bad} malformed association entries ignored")
    if len(items) > MAX_LINKS_PER_TYPE:
        warnings.append(f"{key}: only the first {MAX_LINKS_PER_TYPE} associations are kept")
    if type(total) is int and total > len(items):
        warnings.append(f"{key}: Bullhorn returned {len(items)} of {total} associations")
    return sorted(ids)


@dataclass(frozen=True)
class NoteRecord:
    id: int
    date_added: str | None
    action_type: str | None
    action_semantic: str | None
    body: str | None
    author_id: int | None
    person_id: int | None
    is_deleted: bool | None
    links: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    unresolved_links: tuple[str, ...] = ()

    @classmethod
    def from_bullhorn(
        cls, raw: Any, semantics: Mapping[str, str | None] | None = None
    ) -> tuple[NoteRecord | None, list[str]]:
        """Parse one raw Bullhorn Note. Returns ``(record or None, warnings)``; never raises."""
        warnings: list[str] = []
        if not isinstance(raw, dict):
            return None, [f"note: malformed record {describe_value(raw)}"]
        note_id = _plain_id(raw.get("id"))
        if note_id is None:
            return None, [f"note: malformed id {describe_value(raw.get('id'))}"]
        where = f"note {note_id}"
        date_added: str | None = None
        if raw.get("dateAdded") is not None:
            try:
                date_added = coerce_epoch_millis_to_utc_iso(raw.get("dateAdded"))
            except ValueError:
                warnings.append(f"{where}: unusable dateAdded {describe_value(raw.get('dateAdded'))}")
        action = raw.get("action")
        if action is not None and not isinstance(action, str):
            warnings.append(f"{where}: unusable action {describe_value(action)}")
            action = None
        body = raw.get("comments")
        if body is not None and not isinstance(body, str):
            warnings.append(f"{where}: unusable comments of type {type(body).__name__}")
            body = None
        is_deleted = raw.get("isDeleted")
        if is_deleted is not None and not isinstance(is_deleted, bool):
            warnings.append(f"{where}: unusable isDeleted {describe_value(is_deleted)}")
            is_deleted = None
        field_warnings: list[str] = []
        person_id = _to_one_id(raw, "personReference", field_warnings)
        author_id = _to_one_id(raw, "commentingPerson", field_warnings)
        job_order_id = _to_one_id(raw, "jobOrder", field_warnings)

        links: dict[str, tuple[int, ...]] = {}
        unresolved: list[str] = []
        for link in LINK_TYPES:
            source = _TO_MANY_SOURCES.get(link)
            ids: list[int] | None = None
            if source is not None and source in raw and raw[source] is not None:
                ids = _to_many_ids(raw[source], source, field_warnings)
            elif source is not None and source in raw:
                field_warnings.append(f"{source}: null to-many value")
            if link == "job_ids" and job_order_id is not None:
                ids = sorted(set(ids or []) | {job_order_id}) if ids is not None else None
                if ids is None:
                    links[link] = (job_order_id,)
                    unresolved.append(link)  # the primary jobOrder is known; jobOrders was not read
                    continue
            if ids is None:
                links[link] = ()
                unresolved.append(link)
            else:
                links[link] = tuple(ids)
        warnings.extend(f"{where}: {w}" for w in field_warnings)
        semantic = semantics.get(action) if (semantics is not None and isinstance(action, str)) else None
        record = cls(
            id=note_id,
            date_added=date_added,
            action_type=action,
            action_semantic=semantic,
            body=body,
            author_id=author_id,
            person_id=person_id,
            is_deleted=is_deleted,
            links=links,
            unresolved_links=tuple(unresolved),
        )
        return record, warnings[:MAX_WARNINGS]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date_added": self.date_added,
            "action_type": self.action_type,
            "action_semantic": self.action_semantic,
            "body": self.body,
            "author_id": self.author_id,
            "person_id": self.person_id,
            "is_deleted": self.is_deleted,
            "links": {k: list(self.links.get(k, ())) for k in LINK_TYPES},
            "unresolved_links": list(self.unresolved_links),
        }

    def to_event(self, origin: str = "observed") -> ActivityEvent:
        if origin not in ("observed", "written_by_mcp"):
            raise ValueError("origin must be 'observed' or 'written_by_mcp'")
        extra: dict[str, Any] = {k: list(self.links.get(k, ())) for k in LINK_TYPES}
        return ActivityEvent(
            activity_id=make_activity_id(CONCEPT, "note", self.id),
            concept=CONCEPT,
            occurred_at=self.date_added,
            source={"canonical_entity": "note", "id": self.id},
            links={"note_id": self.id, "author_id": self.author_id},
            origin=origin,
            unresolved_links=self.unresolved_links,
            evidence={"action_type": self.action_type, "action_semantic": self.action_semantic},
            extra_links=extra,
        )
