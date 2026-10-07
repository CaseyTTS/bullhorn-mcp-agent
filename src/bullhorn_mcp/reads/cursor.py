"""Opaque, bound paging cursors (Phase 5C, D-5C-11, Amendment C1-1).

A cursor is ``base64url(payload).base64url(HMAC-SHA256(key, payload_b64))``.

The payload holds: the version; keyed digests (HMAC-SHA256 under the cursor
key) of the tenant key, the principal key and, in shared mode, the
``executing_bullhorn_identity`` (the 5A B-3 ``bh-link:`` label, so a logout +
re-link invalidates outstanding cursors while a refresh does not); the request
binding ``r`` (cursor key, over tenant, principal, link and request); the active
profile version; the paging mode and position; and ``issued_at``. It never holds
query text, raw field names or record data. Any mismatch, tampering,
non-canonical encoding (triage L-3) or expiry (1 hour) is ``invalid_cursor``.

Three keys, each HKDF-SHA256 from the 5A active session key with its own
``info`` (``HKDF_INFO``, ``AUDIT_HKDF_INFO``, ``PROVENANCE_HKDF_INFO``; triage R2-B1):

- the cursor key (signature, identity digests and ``r``);
- the audit key: ``request_hmac`` over tenant, principal and request, written only
  to audit/log records and never to any tool output;
- the provenance key: the model-visible ``provenance.request_hash`` (same message).

No two of these values can be equal, and none is an unkeyed digest. Local mode,
and a session store without keys, derive all three from one random per-process
secret.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..activity.events import tagged_json

VERSION = 1
MAX_AGE_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60
MAX_CURSOR_CHARS = 2048
HKDF_INFO = b"bullhorn-mcp/cursor/v1"
AUDIT_HKDF_INFO = b"bullhorn-mcp/audit-digest/v2"
PROVENANCE_HKDF_INFO = b"bullhorn-mcp/provenance/v1"
_PROCESS_SECRET = secrets.token_bytes(32)


class InvalidCursor(ValueError):
    """The cursor is unusable. The message is fixed (it never echoes the cursor)."""

    def __init__(self) -> None:
        super().__init__("invalid_cursor")


@dataclass(frozen=True)
class Binding:
    tenant: str
    principal: str
    link: str | None
    request_hash: str
    profile_version: int | None


def _derive(info: bytes) -> bytes:
    """HKDF-SHA256 of the 5A active session key; of a random per-process secret otherwise."""
    material: bytes = _PROCESS_SECRET
    try:
        from ..identity import deploy

        if deploy.is_shared():
            keys = getattr(deploy.session_store(), "keys", None)
            active = getattr(keys, "active", None)
            found = keys.key(active) if keys is not None and isinstance(active, str) else None
            if isinstance(found, bytes) and found:
                material = found
    except Exception:  # fail safe: the process secret is still secret and unguessable
        pass
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(material)


def cursor_key() -> bytes:
    return _derive(HKDF_INFO)


def audit_key() -> bytes:
    return _derive(AUDIT_HKDF_INFO)


def provenance_key() -> bytes:
    return _derive(PROVENANCE_HKDF_INFO)


def _request_message(tenant: str, principal: str, normalized: dict[str, Any]) -> bytes:
    return tagged_json(["request/v1", tenant, principal, normalized]).encode("utf-8", "surrogatepass")


def request_hmac(tenant: str, principal: str, normalized: dict[str, Any], key: bytes | None = None) -> str:
    """The audit request identifier (triage R2-B1): HMAC-SHA256 under the audit key over tenant, principal and request.

    It appears only in audit/log records, never in any tool output.
    """
    return hmac.new(key or audit_key(), _request_message(tenant, principal, normalized), "sha256").hexdigest()


def provenance_hash(tenant: str, principal: str, normalized: dict[str, Any], key: bytes | None = None) -> str:
    """The model-visible ``provenance.request_hash`` (triage R2-B1): the same message under the provenance key."""
    return hmac.new(key or provenance_key(), _request_message(tenant, principal, normalized), "sha256").hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    """Strict, canonical decoding (triage L-3): it must re-encode to exactly ``text``."""
    if not text or any(ch not in _B64_ALPHABET for ch in text):
        raise ValueError("not base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64(data) != text:
        raise ValueError("non-canonical base64url")
    return data


_B64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _r(binding: Binding, key: bytes) -> str | None:
    """The cursor's request binding (R2-B1): keyed with the cursor key over tenant, principal, link and request."""
    return _h(tagged_json(["cursor-request/v1", binding.tenant, binding.principal, binding.link, binding.request_hash]), key)


def _h(value: str | None, key: bytes) -> str | None:
    return None if value is None else hmac.new(key, value.encode("utf-8", "surrogatepass"), "sha256").hexdigest()


def encode(binding: Binding, mode: str, position: Any, issued_at: int, key: bytes | None = None) -> str:
    k = key or cursor_key()
    payload = {
        "v": VERSION,
        "t": _h(binding.tenant, k),
        "p": _h(binding.principal, k),
        "b": _h(binding.link, k),
        "r": _r(binding, k),
        "pv": binding.profile_version,
        "m": mode,
        "pos": position,
        "iat": issued_at,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("ascii"))
    sig = _b64(hmac.new(k, body.encode("ascii"), "sha256").digest())
    return f"{body}.{sig}"


def decode(cursor: object, binding: Binding, now: int, key: bytes | None = None) -> tuple[str, Any]:
    """``(mode, position)`` for a valid cursor bound to ``binding``; else ``InvalidCursor``.

    Triage L-3: the signature is re-encoded canonically and compared as a string in
    constant time; a non-canonical body or signature (padding, another alphabet) is invalid.
    """
    if type(cursor) is not str or not 3 <= len(cursor) <= MAX_CURSOR_CHARS or cursor.count(".") != 1:
        raise InvalidCursor()
    body, sig = cursor.split(".")
    k = key or cursor_key()
    try:
        expected = _b64(hmac.new(k, body.encode("ascii"), "sha256").digest())
        if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
            raise InvalidCursor()
        payload = json.loads(_unb64(body).decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error):
        raise InvalidCursor() from None
    if not isinstance(payload, dict) or set(payload) != {"v", "t", "p", "b", "r", "pv", "m", "pos", "iat"}:
        raise InvalidCursor()
    iat = payload["iat"]
    if (
        payload["v"] != VERSION
        or payload["t"] != _h(binding.tenant, k)
        or payload["p"] != _h(binding.principal, k)
        or payload["b"] != _h(binding.link, k)
        or payload["r"] != _r(binding, k)
        or payload["pv"] != binding.profile_version
        or type(iat) is not int
        or not now - MAX_AGE_SECONDS <= iat <= now + CLOCK_SKEW_SECONDS
        or not isinstance(payload["m"], str)
    ):
        raise InvalidCursor()
    return payload["m"], payload["pos"]
