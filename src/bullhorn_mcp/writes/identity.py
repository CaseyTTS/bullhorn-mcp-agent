"""Target resolution into identity cards (§1.4 stage 2).

Each target is fetched with ``client.get(entity, id, fields=...)`` using only
Phase 3 catalog fields plus ``isDeleted`` (documented on Candidate,
ClientContact and JobOrder; see ``PHASE4B_HV_VERIFICATION.md``). A missing
target, a deleted target or an API error raises ``TargetError``, which stops
the pipeline (``rejected_target``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..bullhorn.writes import safe_error_text
from ..schema.bullhorn_catalog import NestedField, RawField
from ..tenant.profile_v2 import TenantProfileV2

TARGET_TYPES = ("candidate", "job", "client_contact", "client_corporation", "placement", "submission")
MAX_NAME_CHARS = 200


class TargetError(Exception):
    """A target could not be resolved. ``code`` is ``target_not_found``, ``target_deleted`` or ``target_error``."""

    def __init__(self, target_type: str, target_id: int, code: str, message: str) -> None:
        super().__init__(message)
        self.target_type = target_type
        self.target_id = target_id
        self.code = code
        self.message = safe_error_text(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "type": self.target_type, "id": self.target_id, "message": self.message}


def _id_of(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    return value if type(value) is int else None


def _text(value: Any) -> str | None:
    return value[:MAX_NAME_CHARS] if isinstance(value, str) else None


def _name(raw: Mapping[str, Any]) -> str | None:
    parts = [p for p in (_text(raw.get("firstName")), _text(raw.get("lastName"))) if p]
    return " ".join(parts) if parts else None


def _candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": _name(raw), "status": _text(raw.get("status")), "owner_id": _id_of(raw.get("owner"))}


def _contact(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": _name(raw), "client_corporation_id": _id_of(raw.get("clientCorporation"))}


def _job(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": _text(raw.get("title")),
        "status": _text(raw.get("status")),
        "client_corporation_id": _id_of(raw.get("clientCorporation")),
        "owner_id": _id_of(raw.get("owner")),
    }


def _corporation(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": _text(raw.get("name"))}


def _pair(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": _id_of(raw.get("candidate")),
        "job_id": _id_of(raw.get("jobOrder")),
        "status": _text(raw.get("status")),
    }


@dataclass(frozen=True)
class _Spec:
    entity: str
    fields: str
    card: Callable[[Mapping[str, Any]], dict[str, Any]]
    soft_delete: bool


SPECS: Mapping[str, _Spec] = {
    "candidate": _Spec("Candidate", "id,firstName,lastName,status,owner,isDeleted", _candidate, True),
    "client_contact": _Spec("ClientContact", "id,firstName,lastName,clientCorporation,isDeleted", _contact, True),
    "job": _Spec("JobOrder", "id,title,status,clientCorporation,owner,isDeleted", _job, True),
    "client_corporation": _Spec("ClientCorporation", "id,name", _corporation, False),
    "placement": _Spec("Placement", "id,candidate,jobOrder,status", _pair, False),
    "submission": _Spec("JobSubmission", "id,candidate,jobOrder,status", _pair, False),
}


def _primary_recruiter_source(profile: TenantProfileV2 | None, states: Mapping[str, str]) -> RawField | NestedField | None:
    """The active, valid 4A ``job.primary_recruiter_id`` mapping target (plain or nested), if any."""
    if profile is None:
        return None
    rec = profile.field_record("job", "primary_recruiter_id", active_only=True)
    if rec is None or states.get(rec.key, rec.validation.state) != "valid":
        return None
    return rec.target if isinstance(rec.target, (RawField, NestedField)) else None


def resolve(
    client: Any,
    target_type: str,
    target_id: int,
    profile: TenantProfileV2 | None = None,
    states: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch one target and return its identity card. Raises ``TargetError`` only."""
    spec = SPECS.get(target_type)
    if spec is None:
        raise TargetError(target_type, target_id, "target_error", "unsupported target type")
    fields = spec.fields
    recruiter = _primary_recruiter_source(profile, states or {}) if target_type == "job" else None
    if recruiter is not None:
        name = recruiter.name if isinstance(recruiter, RawField) else recruiter.field
        if name not in fields.split(","):
            fields = f"{fields},{name}"
    try:
        raw = client.get(spec.entity, target_id, fields=fields)
    except Exception as exc:  # any client failure stops the pipeline, bounded and redacted
        text = safe_error_text(exc)
        code = "target_not_found" if " 404 " in f" {text} " or "404 -" in text else "target_error"
        raise TargetError(target_type, target_id, code, text) from None
    if not isinstance(raw, dict) or _id_of(raw.get("id")) != target_id:
        raise TargetError(target_type, target_id, "target_not_found", f"{spec.entity} {target_id} was not found")
    if spec.soft_delete and raw.get("isDeleted") is True:
        raise TargetError(target_type, target_id, "target_deleted", f"{spec.entity} {target_id} is deleted")
    card: dict[str, Any] = {"type": target_type, "id": target_id, **spec.card(raw)}
    if recruiter is not None:
        value = raw.get(recruiter.name) if isinstance(recruiter, RawField) else raw.get(recruiter.field)
        if isinstance(recruiter, NestedField):
            value = value.get(recruiter.key) if isinstance(value, dict) else None
        card["primary_recruiter_id"] = value if type(value) is int else None
    return card
