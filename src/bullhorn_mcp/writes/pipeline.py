"""Minimal safe-write pipeline for ``create_note`` (§1.4) and the generic ``confirm_write``.

Stages:

1. canonical op;
2. target resolution;
3. validation;
4. permission (scope policy, the ``notes.create`` gate and Amendment A1/C2's
   ``note_action`` requirement);
5. idempotency;
6. preview;
7. approval;
8. write;
9. audit/journal;
10. normalized result.

Stage 1 and the network-free part of stage 3 (unsupported target or
association types, the action type) run before any HTTP request. Every
unsupported mechanism (``PHASE4B_HV_VERIFICATION.md``) is refused without
calling Bullhorn.

Nothing here deletes or compensates. Every outcome is journaled and audited,
and the note text never reaches the journal or the audit log (D-4B-8).

Phase 5A (``shared`` mode only; ``local`` mode is unchanged):

- D-5A-11: a pending write records ``{initiating_principal, tenant_key,
  executing_bullhorn_identity}``; ``confirm_write`` by anyone else, or under a
  changed tenant or execution identity, is ``denied`` (no cross-user confirm).
- D-5A-14 / AC-14: the identity triple is part of the derived idempotency key
  and is recorded in the pending file, the ledger entry and every journal line.
- D-5A-15 / A3-4: an admin-authorized verification write lifts the HV-B11 guard
  only for the exact authorized ``(tenant, principal, target, action)`` with no
  associations, omits ``commentingPerson``, uses the fixed ledger key
  ``verification:note:v1:<tenant>``, consumes the authorization exactly once and
  records the read-back verdict. A positive verdict with an unchanged
  ``rest_url`` fingerprint (and P4B-8 closed) enables person targets.
"""

from __future__ import annotations

import datetime as _dt
import hmac
import os
import re
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..auth import AuthenticationError
from ..bullhorn.errors import BullhornAPIError
from ..bullhorn.writes import EntityWriter, WriteOutcomeUnknown, safe_error_text
from ..activity.events import sha256_hex, tagged_json
from ..notes.action_types import REQUIREMENT as NOTE_ACTION_REQUIREMENT
from ..notes.action_types import ActionTypeSet, Rejection, valid_set, validate
from ..notes.model import NoteRecord
from ..schema.errors import SchemaError, describe_value
from ..tenant.profile_v2 import TenantProfileV2
from ..tenant.state import ConnectionCheck, compute_setup_state, effective_states, require_capability
from ..tenant.store import SetupStore, SetupStoreError, store_from_env
from ..tenant.timeutil import format_utc, parse_utc
from . import journal
from .identity import TARGET_TYPES, TargetError, resolve
from .ledger import IdempotencyKey, Ledger, Verdict, dumps, exclusive_create, read_json, write_dir
from .policy import NOTE_CREATE_SCOPE, check_scope, direct_mode

OPERATION = "create_note"
TOOL = "create_note"
CONFIRM_TOOL = "confirm_write"
PENDING_TTL = _dt.timedelta(minutes=30)
MAX_COMMENT_CHARS = 10_000  # HV-B7: Bullhorn allows String(2147483647); the 10,000 cap applies
MAX_ASSOCIATIONS = 10
MAX_ID = 2**63 - 1
OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
HASH_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}", re.ASCII)
ENTITY_TYPE = {"canonical": "note", "bullhorn": "Note"}

# HV-B2: a Note needs a person (personReference is Not null, no documented default).
PERSON_TARGETS = ("candidate", "client_contact")
# HV-B3: writable Note to-many association fields.
TO_MANY_FIELDS = {"candidate": "candidates", "client_contact": "clientContacts"}
# HV-B4 / HV-B6: the NoteEntity targetEntityName for each verified type.
NOTE_ENTITY_NAMES = {"candidate": "User", "client_contact": "User", "job": "JobOrder"}
READBACK_FIELDS = (
    "id,dateAdded,action,isDeleted,personReference,commentingPerson,jobOrder,"
    "candidates[10](id),clientContacts[10](id),entities[10](targetEntityID,targetEntityName)"
)
SUPPORTED_ASSOCIATIONS = "candidate, client_contact, and at most one job"

# HV-B11 (review fix B-2): a documented mechanism for the authenticated user's CorporateUser id.
# UNRESOLVED (PHASE4B_HV_VERIFICATION.md): without it commentingPerson cannot be sent, so NoteEntity
# auto-creation (record visibility) is not guaranteed for person targets, and every target or
# association type whose visibility depends on it is disabled (unsupported_association).
HV_B11_VERIFIED = False
NOTE_ENTITY_DEPENDENT = ("candidate", "client_contact")
_USER_CACHE: dict[str, int] = {}
P4B8_CLOSED = True  # Phase 5B closed P4B-8 (comment scrubbing before redaction); D-5A-15 step 3
_IDENTITY_KEYS = ("initiating_principal", "tenant_key", "executing_bullhorn_identity")


def atomic_write(path: Any, data: Any) -> None:
    """Pending-operation files only (never the ledger): temp file + ``os.replace``."""
    text = dumps(data)
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        raise SetupStoreError(f"setup store: could not write {path.name} ({type(exc).__name__})") from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


@dataclass
class WriteContext:
    client: Any
    env: Mapping[str, str]
    now: _dt.datetime
    connection: Callable[[], ConnectionCheck]
    writer: Any = None
    # HV-B11: resolves the authenticated user's CorporateUser id. Server-side only; never a tool argument.
    current_user: Callable[[], int] | None = None

    def __post_init__(self) -> None:
        if self.writer is None:
            self.writer = EntityWriter(self.client)


@dataclass(frozen=True)
class CreateNoteOp:
    target_type: str
    target_id: int
    action_type: str
    comments: str
    associations: tuple[tuple[str, int], ...] = ()
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "action_type": self.action_type,
            "comments": self.comments,
            "associations": [{"type": t, "id": i} for t, i in self.associations],
            "idempotency_key": self.idempotency_key,
        }

    @property
    def comments_sha256(self) -> str:
        return sha256_hex(self.comments)


@dataclass
class _Prepared:
    op: CreateNoteOp
    cards: list[dict[str, Any]]
    body: dict[str, Any]
    plan: list[dict[str, Any]]
    key: IdempotencyKey
    verdict: Verdict
    warnings: list[str] = field(default_factory=list)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _result(status: str, correlation_id: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operation": OPERATION,
        "status": status,
        "entity_type": dict(ENTITY_TYPE),
        "record_id": None,
        "associations": [],
        "targets": [],
        "activity": [],
        "correlation_id": correlation_id,
        "warnings": [],
        "errors": [],
    }
    out.update(extra)
    out["warnings"] = [safe_error_text(w, 500) for w in out["warnings"][:50]]
    return out


def _is_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ID


# ---------------------------------------------------------------------- #
# Stage 1: canonical op
# ---------------------------------------------------------------------- #


def _comment_errors(comments: object) -> list[dict[str, Any]]:
    if not isinstance(comments, str):
        return [_error("invalid_comments", f"comments must be a string, got {describe_value(comments)}")]
    if not comments:
        return [_error("invalid_comments", "comments must not be empty")]
    if len(comments) > MAX_COMMENT_CHARS:
        return [_error("comments_too_long", f"comments must be at most {MAX_COMMENT_CHARS} characters, got {len(comments)}")]
    for ch in comments:
        if ch in "\n\r\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cs"):
            return [_error("comments_control_characters", "comments must not contain control characters other than \\n, \\r and \\t")]
    return []


def parse_op(
    target_type: object,
    target_id: object,
    action_type: object,
    comments: object,
    associations: object,
    idempotency_key: object,
) -> tuple[CreateNoteOp | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(target_type, str) or target_type not in TARGET_TYPES:
        errors.append(_error("invalid_target_type", f"target_type {describe_value(target_type)} is not one of {list(TARGET_TYPES)}"))
    if not _is_id(target_id):
        errors.append(_error("invalid_id", f"target_id must be an int >= 1, got {describe_value(target_id)}"))
    if not isinstance(action_type, str) or not action_type:
        errors.append(_error("invalid_action_type", f"action_type must be a non-empty string, got {describe_value(action_type)}"))
    errors.extend(_comment_errors(comments))
    assocs: list[tuple[str, int]] = []
    if associations is not None:
        if not isinstance(associations, list):
            errors.append(_error("invalid_associations", "associations must be a list of {type, id} objects or null"))
        elif len(associations) > MAX_ASSOCIATIONS:
            errors.append(_error("too_many_associations", f"at most {MAX_ASSOCIATIONS} associations are allowed, got {len(associations)}"))
        else:
            seen = {(target_type, target_id)} if isinstance(target_type, str) and _is_id(target_id) else set()
            for i, item in enumerate(associations):
                if not isinstance(item, dict) or set(item) != {"type", "id"}:
                    errors.append(_error("invalid_association", f"associations[{i}] must be exactly {{type, id}}"))
                    continue
                atype, aid = item["type"], item["id"]
                if not isinstance(atype, str) or atype not in TARGET_TYPES:
                    errors.append(
                        _error(
                            "invalid_association",
                            f"associations[{i}].type {describe_value(atype)} is not one of {list(TARGET_TYPES)}",
                        )
                    )
                    continue
                if not _is_id(aid):
                    errors.append(_error("invalid_id", f"associations[{i}].id must be an int >= 1, got {describe_value(aid)}"))
                    continue
                if (atype, aid) in seen:
                    errors.append(_error("duplicate_association", f"associations[{i}] duplicates {atype} {aid}"))
                    continue
                seen.add((atype, aid))
                assocs.append((atype, aid))
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key)):
        errors.append(_error("invalid_idempotency_key", f"idempotency_key must match {IDEMPOTENCY_KEY_RE.pattern}"))
    if errors:
        return None, errors
    assert isinstance(target_type, str) and isinstance(target_id, int) and isinstance(action_type, str) and isinstance(comments, str)
    key = idempotency_key if isinstance(idempotency_key, str) else None
    return CreateNoteOp(target_type, target_id, action_type, comments, tuple(assocs), key), []


def _op_from_stored(data: Any) -> CreateNoteOp | None:
    if not isinstance(data, dict):
        return None
    op, errors = parse_op(
        data.get("target_type"),
        data.get("target_id"),
        data.get("action_type"),
        data.get("comments"),
        data.get("associations"),
        data.get("idempotency_key"),
    )
    return None if errors else op


# ---------------------------------------------------------------------- #
# Stage 3 (network-free part): verified mechanisms and the action type
# ---------------------------------------------------------------------- #


def hv_b11_available(ctx: WriteContext) -> bool:
    """Is a verified current-user mechanism (HV-B11) available in this context?"""
    return HV_B11_VERIFIED and ctx.current_user is not None


def _identity() -> Any:
    """``None`` in local mode; the caller's identity context in shared mode (raises ``IdentityRequired``)."""
    from ..identity import principal

    return principal.shared_identity()


def _triple(ident: Any) -> dict[str, Any]:
    return dict(ident.triple()) if ident is not None else {}


def _rest_fingerprint(ctx: WriteContext) -> str | None:
    try:
        check = ctx.connection()
    except Exception:
        return None
    return check.rest_url_fingerprint if check.ok else None


def verdict_enabled(ctx: WriteContext, store: SetupStore | None, ident: Any) -> bool:
    """D-5A-15 step 3: person targets enabled by a positive, fingerprint-current verification verdict."""
    if ident is None or store is None or not P4B8_CLOSED:
        return False
    from ..tenant.changes import note_write_enabled

    try:
        return note_write_enabled(store, _rest_fingerprint(ctx))
    except SchemaError:
        return False


def _authorization(ctx: WriteContext, store: SetupStore | None, ident: Any, op: CreateNoteOp) -> dict[str, Any] | None:
    """A3-4: the open admin authorization for exactly this op (shared mode, setup admin, linked session)."""
    if ident is None or store is None or ident.access_tier != "bullhorn_user":
        return None
    from ..tenant.changes import find_note_authorization

    if ident.tenant is None or not ident.tenant.roles.is_setup_admin(ident.principal_key):  # B-4: per tenant
        return None
    try:
        return find_note_authorization(
            store,
            tenant_key=ident.tenant_key,
            principal=ident.principal_key,
            target_type=op.target_type,
            target_id=op.target_id,
            action_type=op.action_type,
            has_associations=bool(op.associations),
            now=ctx.now,
            executing=ident.executing_bullhorn_identity,  # B-3: bound to the link it was authorized under
        )
    except SchemaError:
        return None


def support_errors(op: CreateNoteOp, hv_b11: bool = False, target_verified: bool = False) -> list[dict[str, Any]]:
    """Unsupported target/association types (HV-B2/B3/B4/B11, D-4B-5): the whole request is rejected.

    ``target_verified`` (Phase 5A): the target's NoteEntity creation was verified for this
    tenant (D-5A-15), so only person-type *associations* stay behind the HV-B11 guard.
    """
    errors: list[dict[str, Any]] = []
    if not hv_b11:
        # B-2: NoteEntity auto-creation needs commentingPerson, which needs HV-B11.
        refs = [] if target_verified else [(op.target_type, op.target_id)]
        refs += [(t, i) for t, i in op.associations]
        for atype, aid in refs:
            if atype in NOTE_ENTITY_DEPENDENT:
                errors.append(
                    _error(
                        "unsupported_association",
                        f"{atype} {aid} is not supported: its record visibility depends on NoteEntity auto-creation, "
                        "which needs commentingPerson, and no documented mechanism for the authenticated user's "
                        "CorporateUser id is verified (HV-B11)",
                        type=atype,
                        id=aid,
                    )
                )
        if errors:
            return errors
    if op.target_type not in PERSON_TARGETS:
        errors.append(
            _error(
                "unsupported_target",
                f"target_type {op.target_type!r} is not supported: a Bullhorn Note requires a person "
                f"(personReference); supported target types: {list(PERSON_TARGETS)}",
            )
        )
    jobs = 0
    for atype, aid in op.associations:
        if atype == "job":
            jobs += 1
            if jobs == 1:
                continue
        if atype in TO_MANY_FIELDS:
            continue
        errors.append(
            _error(
                "unsupported_association",
                f"association {atype} {aid} is not supported (no verified Bullhorn mechanism); supported: {SUPPORTED_ASSOCIATIONS}",
                type=atype,
                id=aid,
            )
        )
    return errors


def _load_profile(store: SetupStore | None) -> tuple[TenantProfileV2 | None, dict[str, str]]:
    if store is None:
        return None, {}
    try:
        active = store.active_version()
        if active is None:
            return None, {}
        profile = store.read_version(active)
        states, _ = effective_states(profile, store.read_discovery())
    except SchemaError:
        return None, {}
    return profile, states


# ---------------------------------------------------------------------- #
# Stage 4: permission
# ---------------------------------------------------------------------- #


def gate_missing(ctx: WriteContext, aset: ActionTypeSet) -> list[str]:
    """The ``notes.create`` gate (check_connection=True, D-4B-16) plus the A1/C2 note_action requirement."""
    state = compute_setup_state(ctx.env, ctx.connection(), now=ctx.now)
    missing: list[str] = []
    for cap in ("notes.read", "notes.create"):
        for item in require_capability(cap, state).missing:
            if item not in missing:
                missing.append(item)
    if not aset.usable:
        missing.append(NOTE_ACTION_REQUIREMENT)
    return missing


# ---------------------------------------------------------------------- #
# Stages 2, 5, 6 (shared by preview, confirm and direct)
# ---------------------------------------------------------------------- #


def _key_for(op: CreateNoteOp, actor: str, identity: Mapping[str, Any] | None = None) -> IdempotencyKey:
    payload: dict[str, Any] = {
        "op": OPERATION,
        "target": [op.target_type, op.target_id],
        "associations": sorted([t, i] for t, i in op.associations),
        "action": op.action_type,
        "comments_sha256": op.comments_sha256,
        "actor": actor,
    }
    if identity:
        payload["identity"] = {k: identity.get(k) for k in _IDENTITY_KEYS}  # Phase 5A (D-5A-14): shared mode only
    payload_hash = sha256_hex(tagged_json(payload))
    if op.idempotency_key is not None:
        return IdempotencyKey("caller", op.idempotency_key, payload_hash)
    return IdempotencyKey("derived", payload_hash, payload_hash)


def _body_and_plan(op: CreateNoteOp) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body: dict[str, Any] = {
        "action": op.action_type,
        "comments": op.comments,
        "personReference": {"id": op.target_id},
    }
    plan: list[dict[str, Any]] = [
        {"type": op.target_type, "id": op.target_id, "role": "target", "mechanism": "create body: personReference"}
    ]
    to_many: dict[str, list[int]] = {}
    for atype, aid in op.associations:
        if atype == "job":
            body["jobOrder"] = {"id": aid}
            plan.append({"type": "job", "id": aid, "role": "association", "mechanism": "create body: jobOrder"})
        else:
            to_many.setdefault(atype, []).append(aid)
    for atype, ids in to_many.items():
        fname = TO_MANY_FIELDS[atype]
        for aid in ids:
            plan.append(
                {"type": atype, "id": aid, "role": "association", "mechanism": f"PUT /entity/Note/{{id}}/{fname}/{{ids}}"}
            )
    return body, plan


def _session_key(ctx: WriteContext) -> str | None:
    try:
        token = ctx.client.auth.session.bh_rest_token
    except Exception:  # no session: no cache
        return None
    return sha256_hex(token) if isinstance(token, str) and token else None


def _current_user_id(ctx: WriteContext) -> int:
    """The authenticated user's CorporateUser id (HV-B11), cached per session. Never from tool arguments."""
    if not hv_b11_available(ctx) or ctx.current_user is None:
        raise TargetError("current_user", 0, "target_error", "the authenticated user's CorporateUser id is not available (HV-B11)")
    key = _session_key(ctx)
    if key is not None and key in _USER_CACHE:
        return _USER_CACHE[key]
    try:
        value = ctx.current_user()
    except Exception as exc:
        raise TargetError("current_user", 0, "target_error", "could not resolve the authenticated user: " + safe_error_text(exc)) from None
    if not _is_id(value):
        raise TargetError("current_user", 0, "target_error", "the authenticated user's CorporateUser id is not a valid id")
    if key is not None:
        _USER_CACHE[key] = value
    return value


def _resolve_all(ctx: WriteContext, op: CreateNoteOp, profile: TenantProfileV2 | None, states: Mapping[str, str]) -> list[dict[str, Any]]:
    cards = [resolve(ctx.client, op.target_type, op.target_id, profile, states)]
    for atype, aid in op.associations:
        cards.append(resolve(ctx.client, atype, aid, profile, states))
    return cards


def _preview_core(prep: _Prepared, mode: str) -> dict[str, Any]:
    """The hashed part of a preview (no timestamps, no ids)."""
    return {
        "operation": OPERATION,
        "mode": mode,
        "request": {"method": "PUT", "path": "/entity/Note", "body": prep.body},
        "association_plan": prep.plan,
        "targets": prep.cards,
        "idempotency": {"key_kind": prep.key.kind, **prep.verdict.to_dict()},
        "requires_confirmation": mode == "preview",
    }


def preview_hash(core: Mapping[str, Any]) -> str:
    return sha256_hex(tagged_json(dict(core)))


def _journal_fields(op: CreateNoteOp, actor: str | None, correlation_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "actor": actor,
        "operation": OPERATION,
        "entity": "Note",
        "targets": [{"type": op.target_type, "id": op.target_id}] + [{"type": t, "id": i} for t, i in op.associations],
        "comments_sha256": op.comments_sha256,
        "comments_length": len(op.comments),
        **extra,
    }


def _verdict_result(verdict: Verdict, correlation_id: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if verdict.status == "duplicate":
        return _result(
            "duplicate",
            correlation_id,
            record_id=verdict.record_id,
            targets=cards or [],
            warnings=["an identical note was already written by this server; nothing was written"],
        )
    if verdict.status == "in_doubt":
        return _result(
            "in_doubt",
            correlation_id,
            targets=cards or [],
            errors=[
                _error(
                    "in_doubt",
                    "a previous write with this idempotency key did not finish; verify in Bullhorn manually. "
                    "It is never retried automatically.",
                )
            ],
        )
    if verdict.status == "conflict":
        return _result(
            "rejected_validation",
            correlation_id,
            targets=cards or [],
            errors=[_error("idempotency_key_conflict", "idempotency_key was already used for a different request")],
        )
    return None


def _prepare(
    ctx: WriteContext,
    op: CreateNoteOp,
    store: SetupStore | None,
    actor: str,
    correlation_id: str,
    ident: Any = None,
    verification: Mapping[str, Any] | None = None,
) -> tuple[_Prepared | None, dict[str, Any] | None, ActionTypeSet]:
    """Stages 3 (network-free), 2 and 5. Returns ``(prepared, None, aset)`` or ``(None, refusal, aset)``."""
    profile, states = _load_profile(store)
    aset = valid_set(profile, states)
    hv_b11 = hv_b11_available(ctx)
    verified = not hv_b11 and verification is None and verdict_enabled(ctx, store, ident)
    if verification is not None and not hv_b11:
        errors = support_errors(op, True)  # A3-4: exactly the authorized target, no associations
    else:
        errors = support_errors(op, hv_b11, target_verified=verified)
    if aset.usable:
        checked = validate(op.action_type, aset)
        if isinstance(checked, Rejection):
            errors.append(checked.to_dict())
    if errors:
        return None, _result("rejected_validation", correlation_id, errors=errors), aset
    omit_commenting = not hv_b11 and (verification is not None or verified)  # HV-B8: defaults to the creating user
    try:
        cards = _resolve_all(ctx, op, profile, states)
        commenting_person = None if omit_commenting else _current_user_id(ctx)
    except TargetError as exc:
        return None, _result("rejected_target", correlation_id, errors=[exc.to_dict()]), aset
    body, plan = _body_and_plan(op)
    if commenting_person is not None:
        body["commentingPerson"] = {"id": commenting_person}
    key = _key_for(op, actor, _triple(ident))
    if verification is not None and ident is not None and store is not None:
        from ..tenant.changes import note_write_generation  # L-6: one key per reset generation

        generation = note_write_generation(store)
        key = IdempotencyKey("verification", f"verification:note:v1:{ident.tenant_key}:{generation}", key.payload_hash)
    verdict = Ledger(store).lookup(key, ctx.now) if store is not None else Verdict("new")
    prep = _Prepared(op, cards, body, plan, key, verdict, ["duplicate_probe_skipped: no verified Bullhorn note query mechanism (HV-B5)"])
    return prep, None, aset


# ---------------------------------------------------------------------- #
# create_note
# ---------------------------------------------------------------------- #


def create_note(
    ctx: WriteContext,
    target_type: object,
    target_id: object,
    action_type: object,
    comments: object,
    associations: object = None,
    idempotency_key: object = None,
    dry_run: object = True,
) -> dict[str, Any]:
    correlation_id = uuid.uuid4().hex
    if dry_run is not True and dry_run is not False:
        return _result("rejected_validation", correlation_id, errors=[_error("invalid_dry_run", "dry_run must be a boolean")])
    direct = dry_run is False
    if direct and not direct_mode(ctx.env):
        return _result(
            "refused",
            correlation_id,
            errors=[
                _error(
                    "confirmation_required",
                    "dry_run=False is only allowed when BULLHORN_NOTE_CREATE_MODE=direct; "
                    "call create_note with dry_run=True, then confirm_write(operation_id, preview_hash, 'approve')",
                )
            ],
        )
    op, errors = parse_op(target_type, target_id, action_type, comments, associations, idempotency_key)
    if op is None:
        return _result("rejected_validation", correlation_id, errors=errors)

    try:
        store = store_from_env(ctx.env)
    except SchemaError:
        store = None
    scope = check_scope(NOTE_CREATE_SCOPE, ctx.env, approving=direct)
    actor = scope.actor or ""
    ident = _identity()
    authorization = None if direct else _authorization(ctx, store, ident, op)  # A3-4: always previewed first
    prep, refusal, aset = _prepare(ctx, op, store, actor, correlation_id, ident, authorization)
    if refusal is not None:
        return refusal
    assert prep is not None
    triple = _triple(ident)
    if authorization is not None:
        triple["verification"] = authorization.get("authorization_id")

    # Stage 4: permission (the legacy permissions.check runs first, in the tool).
    missing = list(scope.missing) + gate_missing(ctx, aset)
    if store is None and "env:BULLHORN_SETUP_STORE" not in missing:
        missing.append("env:BULLHORN_SETUP_STORE")
    if missing:
        return _result("denied", correlation_id, targets=prep.cards, missing_requirements=missing,
                       errors=[_error("denied", "create_note is not permitted: " + ", ".join(missing))])
    assert store is not None

    refused = _verdict_result(prep.verdict, correlation_id, prep.cards)
    if refused is not None:
        return refused

    mode = "direct" if direct else "preview"
    core = _preview_core(prep, mode)
    phash = preview_hash(core)
    if direct:
        journal.record(store, TOOL, "confirmed", **_journal_fields(op, actor, correlation_id, approval="direct", mode="direct", **triple))
        return _execute(ctx, store, prep, actor, correlation_id, operation_id=None, mode="direct", identity=triple or None)

    operation_id = uuid.uuid4().hex
    expires_at = format_utc(ctx.now + PENDING_TTL)
    pending = {
        "operation_id": operation_id,
        "operation": OPERATION,
        "status": "pending",
        "preview_hash": phash,
        "correlation_id": correlation_id,
        "actor": actor,
        "created_at": format_utc(ctx.now),
        "expires_at": expires_at,
        "op": op.to_dict(),
        **triple,
    }
    atomic_write(write_dir(store, "pending") / f"{operation_id}.json", pending)
    journal.record(
        store,
        TOOL,
        "previewed",
        **_journal_fields(op, actor, correlation_id, operation_id=operation_id, approval="pending", mode="preview", **triple),
    )
    return _result(
        "previewed",
        correlation_id,
        operation_id=operation_id,
        preview_hash=phash,
        expires_at=expires_at,
        requires_confirmation=True,
        targets=prep.cards,
        associations=[{"type": p["type"], "id": p["id"], "status": "planned"} for p in prep.plan],
        preview=core,
        warnings=prep.warnings,
    )


# ---------------------------------------------------------------------- #
# confirm_write
# ---------------------------------------------------------------------- #


def _refused(reason: str, message: str, correlation_id: str | None, **extra: Any) -> dict[str, Any]:
    return _result("refused", correlation_id, reason=reason, errors=[_error(reason, message)], **extra)


def confirm_write(ctx: WriteContext, operation_id: object, preview_hash_value: object, decision: object) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        return _refused("invalid_decision", "decision must be 'approve' or 'reject'", None)
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.fullmatch(operation_id):
        return _refused("unknown_operation", "unknown operation_id", None)
    try:
        store = store_from_env(ctx.env)
    except SchemaError:
        store = None
    if store is None:
        return _refused("unknown_operation", "unknown operation_id (no setup store is configured)", None)
    pending_dir = write_dir(store, "pending")
    path = pending_dir / f"{operation_id}.json"
    pending = read_json(path)
    if pending is None or pending.get("corrupt") is True or pending.get("operation") != OPERATION:
        return _refused("unknown_operation", "unknown operation_id", None)
    correlation_id = pending.get("correlation_id") if isinstance(pending.get("correlation_id"), str) else None
    ident = _identity()
    if ident is not None:
        # D-5A-11: only the initiating principal, in the same tenant and execution identity, may confirm.
        if pending.get("initiating_principal") != ident.initiating_principal or pending.get("tenant_key") != ident.tenant_key:
            return _result(
                "denied",
                correlation_id,
                reason="not_owner",
                errors=[_error("denied", "this preview belongs to another principal or tenant")],
            )
        if pending.get("executing_bullhorn_identity") != ident.executing_bullhorn_identity:
            # 5A triage B-3: a logout + re-link (possibly another Bullhorn account) changes the label.
            return _result(
                "denied",
                correlation_id,
                reason="execution_identity_changed",
                errors=[_error("denied", "the Bullhorn account linked to this principal changed since the preview")],
            )
    stored_hash = pending.get("preview_hash")
    if (
        not isinstance(preview_hash_value, str)
        or not isinstance(stored_hash, str)
        or not HASH_RE.fullmatch(preview_hash_value)
        or not hmac.compare_digest(preview_hash_value, stored_hash)
    ):
        return _refused("hash_mismatch", "preview_hash does not match the previewed operation", correlation_id)
    marker = pending_dir / f"{operation_id}.consumed"
    if pending.get("status") != "pending" or marker.exists():
        return _refused("already_consumed", "this operation was already confirmed, rejected or expired", correlation_id)
    expires = parse_utc(pending.get("expires_at"))
    if expires is None or ctx.now >= expires:
        atomic_write(path, {**pending, "status": "expired"})
        return _refused("expired", "the preview has expired; create a new preview", correlation_id)
    op = _op_from_stored(pending.get("op"))
    preview_actor = pending.get("actor")
    if op is None or not isinstance(preview_actor, str) or not isinstance(correlation_id, str):
        return _refused("unknown_operation", "the stored operation is corrupt", correlation_id)

    scope = check_scope(NOTE_CREATE_SCOPE, ctx.env, approving=True)
    if scope.missing:
        reason = (
            "actor_missing"
            if any(m.startswith("env:") for m in scope.missing)
            else "approver_not_allowed"
            if any(m.startswith("approver:") for m in scope.missing)
            else "scope_disabled"
        )
        return _refused(reason, "confirm_write is not permitted: " + ", ".join(scope.missing), correlation_id,
                        missing_requirements=list(scope.missing))
    actor = scope.actor or ""
    triple = _triple(ident)
    authorization: dict[str, Any] | None = None
    if ident is not None and pending.get("verification") is not None:
        triple["verification"] = pending.get("verification")
        authorization = _authorization(ctx, store, ident, op)
        if authorization is None or authorization.get("authorization_id") != pending.get("verification"):
            authorization = None
            if decision == "approve":
                return _refused("stale_preview", "the verification authorization is no longer open", correlation_id)

    if decision == "reject":
        if not exclusive_create(marker, {"decision": "reject", "actor": actor, "at": format_utc(ctx.now)}):
            return _refused("already_consumed", "this operation was already confirmed or rejected", correlation_id)
        atomic_write(path, {**pending, "status": "rejected"})
        jf = _journal_fields(op, actor, correlation_id, operation_id=operation_id, approval="rejected", **triple)
        journal.record(store, CONFIRM_TOOL, "rejected", **jf)
        return _result("rejected", correlation_id, operation_id=operation_id)

    # Re-run stages 2-5 against the current state.
    prep, refusal, aset = _prepare(ctx, op, store, preview_actor, correlation_id, ident, authorization)
    if refusal is not None:
        return _refused("stale_preview", "the operation no longer validates: " + refusal["status"], correlation_id,
                        details=refusal.get("errors", []))
    assert prep is not None
    missing = gate_missing(ctx, aset)
    if missing:
        # B-1: a failed gate (including the A1/C2 note_action requirement) is a denial, not a refusal.
        return _result(
            "denied",
            correlation_id,
            reason="gate_failed",
            missing_requirements=missing,
            targets=prep.cards,
            errors=[_error("denied", "create_note is not permitted: " + ", ".join(missing))],
        )
    if not hmac.compare_digest(preview_hash(_preview_core(prep, "preview")), stored_hash):
        return _refused("stale_preview", "a target or the idempotency verdict changed since the preview; create a new preview",
                        correlation_id, targets=prep.cards)
    if not exclusive_create(marker, {"decision": "approve", "actor": actor, "at": format_utc(ctx.now)}):
        return _refused("already_consumed", "this operation was already confirmed or rejected", correlation_id)
    if authorization is not None:
        from ..tenant.changes import consume_note_authorization

        if not consume_note_authorization(
            store, str(authorization.get("authorization_id")), {"operation_id": operation_id, "at": format_utc(ctx.now)}
        ):
            return _refused("already_consumed", "the verification authorization was already used", correlation_id)
    atomic_write(path, {**pending, "status": "consumed"})
    jf = _journal_fields(op, actor, correlation_id, operation_id=operation_id, approval="approved", **triple)
    journal.record(store, CONFIRM_TOOL, "confirmed", **jf)
    refused = _verdict_result(prep.verdict, correlation_id, prep.cards)
    if refused is not None:  # pragma: no cover - a changed verdict is already a stale preview
        return refused
    observed: dict[str, Any] = {}
    result = _execute(
        ctx, store, prep, actor, correlation_id, operation_id=operation_id, mode="confirmed", identity=triple or None, observed=observed
    )
    if authorization is not None and ident is not None:
        _record_verdict(ctx, store, ident, authorization, result, observed, correlation_id)
    return result


def _record_verdict(
    ctx: WriteContext,
    store: SetupStore,
    ident: Any,
    authorization: Mapping[str, Any],
    result: Mapping[str, Any],
    observed: Mapping[str, Any],
    correlation_id: str,
) -> None:
    """D-5A-15 (d): the append-only verdict of the single verification write (no note text, no tokens)."""
    from ..tenant.changes import NOTE_WRITE_LOG, append_verification

    raw = observed.get("raw")
    associations = [a for a in result.get("associations", []) if isinstance(a, dict)]
    target = next((a for a in associations if a.get("role") == "target"), None)
    present = target is not None and target.get("note_entity") == "present"
    commenting = raw.get("commentingPerson") if isinstance(raw, dict) else None
    commenting_id = commenting.get("id") if isinstance(commenting, dict) and type(commenting.get("id")) is int else None
    record_id = result.get("record_id")
    append_verification(
        store,
        NOTE_WRITE_LOG,
        {
            "kind": "verdict",
            "authorization_id": authorization.get("authorization_id"),
            "note_id": record_id if type(record_id) is int else None,
            "status": result.get("status"),
            "associations": [{k: a.get(k) for k in ("type", "id", "role", "status", "note_entity")} for a in associations],
            "note_entity_present": present,
            "commenting_person_id": commenting_id,
            "positive": bool(present and result.get("status") == "committed"),
            "rest_url_fingerprint": _rest_fingerprint(ctx),
            "principal": ident.principal_key,
            "tenant_key": ident.tenant_key,
            "at": format_utc(ctx.now),
            "correlation_id": correlation_id,
        },
    )


# ---------------------------------------------------------------------- #
# Stage 8: write, then read back
# ---------------------------------------------------------------------- #


def _assoc_present(raw: Mapping[str, Any], atype: str, aid: int, role: str) -> bool | None:
    """Is the association visible in the read-back? ``None`` when the field is unusable."""
    if role == "target":
        ref = raw.get("personReference")
        return isinstance(ref, dict) and ref.get("id") == aid
    if atype == "job":
        ref = raw.get("jobOrder")
        return isinstance(ref, dict) and ref.get("id") == aid
    value = raw.get(TO_MANY_FIELDS[atype])
    items = value.get("data") if isinstance(value, dict) else value
    if not isinstance(items, list):
        return None
    return any(isinstance(i, dict) and i.get("id") == aid for i in items)


def _note_entity(raw: Mapping[str, Any], atype: str, aid: int) -> str:
    value = raw.get("entities")
    items = value.get("data") if isinstance(value, dict) else value
    if not isinstance(items, list):
        return "unknown"
    name = NOTE_ENTITY_NAMES.get(atype)
    found = any(
        isinstance(i, dict) and i.get("targetEntityID") == aid and i.get("targetEntityName") == name for i in items
    )
    return "present" if found else "absent"


def _execute(
    ctx: WriteContext,
    store: SetupStore,
    prep: _Prepared,
    actor: str,
    correlation_id: str,
    *,
    operation_id: str | None,
    mode: str,
    identity: Mapping[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    op = prep.op
    ledger = Ledger(store)
    jfields = _journal_fields(op, actor, correlation_id, operation_id=operation_id, mode=mode, approval=mode, **(identity or {}))
    ledger_identity = {k: identity.get(k) for k in _IDENTITY_KEYS} if identity else None
    claim = ledger.begin(prep.key, ctx.now, operation_id=operation_id or "", correlation_id=correlation_id, identity=ledger_identity)
    if claim.status != "new" or claim.generation is None:
        refused = _verdict_result(claim, correlation_id, prep.cards)
        assert refused is not None
        return refused
    generation = claim.generation

    def scrubbed(exc: BaseException) -> str:
        # B-5: the note text is removed before the error is bounded and redacted.
        return safe_error_text(exc, scrub=op.comments)

    def in_doubt(err: str) -> dict[str, Any]:
        # The write may have happened: the ledger generation stays pending (the key is not reusable).
        journal.record(store, TOOL, "in_doubt", **jfields, bullhorn_response={"error": err}, outcome="in_doubt")
        return _result("in_doubt", correlation_id, targets=prep.cards,
                       errors=[_error("in_doubt", "the create outcome is unknown; verify in Bullhorn manually: " + err)])

    try:
        response = ctx.writer.create("Note", prep.body)
    except AuthenticationError as exc:
        # B-6: the session could not be established or refreshed, so no write was accepted.
        ledger.finish(prep.key, generation, ctx.now, "failed")
        err = scrubbed(exc)
        journal.record(store, TOOL, "failed", **jfields, bullhorn_response={"error": err}, outcome="failed")
        return _result("failed", correlation_id, targets=prep.cards, errors=[_error("authentication_error", err)])
    except WriteOutcomeUnknown as exc:
        return in_doubt(scrubbed(exc))  # B-4: a 200 response whose body is not a JSON object
    except BullhornAPIError as exc:
        ledger.finish(prep.key, generation, ctx.now, "failed")
        err = scrubbed(exc)
        journal.record(store, TOOL, "failed", **jfields, bullhorn_response={"error": err}, outcome="failed")
        return _result("failed", correlation_id, targets=prep.cards, errors=[_error("bullhorn_error", err)])
    except Exception as exc:  # transport or anything unexpected once the request may have been sent
        return in_doubt(scrubbed(exc))

    record_id = response.get("changedEntityId") if isinstance(response, dict) else None
    change_type = response.get("changeType") if isinstance(response, dict) else None
    summary = {
        "changedEntityId": record_id if type(record_id) is int else None,
        "changeType": change_type if isinstance(change_type, str) else None,
    }
    if type(record_id) is not int or record_id < 1 or change_type != "INSERT":
        journal.record(store, TOOL, "in_doubt", **jfields, bullhorn_response=summary, outcome="in_doubt")
        return _result("in_doubt", correlation_id, targets=prep.cards,
                       errors=[_error("in_doubt", "Bullhorn returned an unexpected create response; verify in Bullhorn manually")])
    ledger.finish(prep.key, generation, ctx.now, "committed", record_id)

    warnings = list(prep.warnings)
    association_errors: dict[tuple[str, int], str] = {}
    to_many: dict[str, list[int]] = {}
    for atype, aid in op.associations:
        if atype in TO_MANY_FIELDS:
            to_many.setdefault(atype, []).append(aid)
    for atype, ids in to_many.items():
        try:
            ctx.writer.associate("Note", record_id, TO_MANY_FIELDS[atype], ids)
        except WriteOutcomeUnknown as exc:  # may have happened: the read-back decides
            warnings.append(f"association {atype} outcome unknown: " + scrubbed(exc))
        except Exception as exc:  # recorded per association; never retried, never compensated
            err = scrubbed(exc)
            for aid in ids:
                association_errors[(atype, aid)] = err

    raw: dict[str, Any] | None = None
    try:
        got = ctx.client.get("Note", record_id, fields=READBACK_FIELDS)
        raw = got if isinstance(got, dict) else None
        if raw is None:
            warnings.append("read-back returned no usable record")
    except Exception as exc:
        warnings.append("read-back failed: " + scrubbed(exc))
    if observed is not None:
        observed["raw"] = raw

    results: list[dict[str, Any]] = []
    roles = [(op.target_type, op.target_id, "target")] + [(t, i, "association") for t, i in op.associations]
    for atype, aid, role in roles:
        entry: dict[str, Any] = {"type": atype, "id": aid, "role": role}
        if (atype, aid) in association_errors:
            entry.update(status="failed", error=association_errors[(atype, aid)])
        elif raw is None:
            entry["status"] = "unverified"
        else:
            present = _assoc_present(raw, atype, aid, role)
            entry["status"] = "present" if present else ("missing" if present is False else "unverified")
        entry["note_entity"] = _note_entity(raw, atype, aid) if raw is not None else "unknown"
        if entry["status"] == "present" and entry["note_entity"] != "present":
            # B-2: linked but not shown to be visible on the record (HV-B4): never "committed".
            entry["status"] = "note_entity_absent" if entry["note_entity"] == "absent" else "unverified"
            warnings.append(f"no NoteEntity found for {atype} {aid}: the note may not show on that record's Notes tab (HV-B4)")
        results.append(entry)

    present_count = sum(1 for r in results if r["status"] == "present")
    if present_count == len(results):
        status = "committed"
    elif present_count == 0 and raw is not None and all(r["status"] in ("missing", "failed") for r in results):
        status = "failed_orphan"
    else:
        status = "partially_committed"

    record, rec_warnings = NoteRecord.from_bullhorn(raw if raw is not None else {"id": record_id})
    warnings.extend(rec_warnings)
    activity = [record.to_event("written_by_mcp").to_dict()] if record is not None else []
    journal.record(
        store,
        TOOL,
        status,
        **jfields,
        record_id=record_id,
        bullhorn_response=summary,
        associations=[{k: r[k] for k in ("type", "id", "status")} for r in results],
        outcome=status,
    )
    errors = [_error("association_failed", r["error"], type=r["type"], id=r["id"]) for r in results if r["status"] == "failed"]
    if status == "failed_orphan":
        errors.append(_error("failed_orphan", f"note {record_id} was created but no association could be verified; it was not deleted"))
    return _result(
        status,
        correlation_id,
        record_id=record_id,
        associations=results,
        targets=prep.cards,
        activity=activity,
        warnings=warnings,
        errors=errors,
        mode=mode,
    )
