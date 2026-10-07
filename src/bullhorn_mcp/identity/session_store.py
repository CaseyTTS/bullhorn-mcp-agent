"""Per-user Bullhorn session storage (Phase 5A, D-5A-6 / D-5-12).

Interface: ``SessionStore.get/put/delete(tenant_key, principal_key)`` for linked
sessions, ``put_login/get_login/take_login(login_id)`` for pending logins,
``put_link/take_link(link_id)`` for callback results awaiting the confirm POST and
``put/get/delete_principal_link(tenant_key, principal_key)`` for a confirmed link
awaiting the one-time confirmation code (5A triage B-2). ``take_*`` is atomic and
single-use. Pending entries older than ``PENDING_MAX_AGE_SECONDS`` are pruned on
every pending put/take and at startup (L-3).

``EncryptedFileSessionStore`` (production): one file per ``(tenant_key, principal_key)``,
AES-256-GCM with ``AAD = tenant_key|principal_key|schema_version``. A file copied
to another owner's name fails to decrypt, and the read fails closed (``None``).
Keys come from ``SessionKeys`` (key ID -> 32-byte key, one active ID), so old
files stay readable after rotation; a file whose key was removed fails closed.
Writes are atomic (temp file + ``os.replace``) and owner-only (``0600``).

``MemorySessionStore`` is for tests. ``KeyringSessionStore`` is a local-only
development adapter; ``identity.deploy`` refuses it in ``shared`` mode.

Nothing here logs or renders a token: ``repr`` of every record is redacted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("bullhorn_mcp.identity")

SCHEMA_VERSION = "bhsess1"
_MAGIC = b"BHS1"
_KID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,32}", re.ASCII)
_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ID_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}", re.ASCII)
MAX_FILE_BYTES = 256_000
PENDING_MAX_AGE_SECONDS = 1800  # every pending TTL is 10 minutes; anything older is garbage


class SessionStoreError(Exception):
    """A configuration or storage problem. The message never contains key material or tokens."""


# ---------------------------------------------------------------------- #
# Keys
# ---------------------------------------------------------------------- #


class SessionKeys:
    """``{key_id: 32-byte key}`` plus the active key ID. ``repr`` shows key IDs only."""

    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if not keys or active not in keys:
            raise SessionStoreError("session keys: the active key ID must be one of the keys")
        for kid, key in keys.items():
            if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
                raise SessionStoreError("session keys: a key ID must match [A-Za-z0-9_.-]{1,32}")
            if not isinstance(key, bytes) or len(key) != 32:
                raise SessionStoreError("session keys: every key must be exactly 32 bytes (AES-256)")
        self._keys = dict(keys)
        self.active = active

    def __repr__(self) -> str:
        return f"SessionKeys(active={self.active!r}, ids={sorted(self._keys)!r})"

    def key(self, kid: str) -> bytes | None:
        return self._keys.get(kid)

    @classmethod
    def from_text(cls, text: str) -> SessionKeys:
        """``{"active": "<kid>", "keys": {"<kid>": "<base64 of 32 bytes>", ...}}``."""
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise SessionStoreError("session keys: not valid JSON") from None
        if not isinstance(data, dict) or set(data) != {"active", "keys"} or not isinstance(data["keys"], dict):
            raise SessionStoreError('session keys: expected {"active": ..., "keys": {...}}')
        keys: dict[str, bytes] = {}
        for kid, raw in data["keys"].items():
            if not isinstance(raw, str):
                raise SessionStoreError("session keys: every key must be a base64 string")
            try:
                keys[kid] = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise SessionStoreError("session keys: a key is not valid base64") from None
        active = data["active"]
        if not isinstance(active, str):
            raise SessionStoreError("session keys: active must be a key ID string")
        return cls(keys, active)


# ---------------------------------------------------------------------- #
# Records
# ---------------------------------------------------------------------- #


@dataclass
class SessionRecord:
    """One linked Bullhorn session. Token fields never appear in ``repr``."""

    tenant_key: str
    principal_key: str
    rest_url: str = field(repr=False)
    bh_rest_token: str = field(repr=False)
    bh_expires_at: float
    access_token: str | None = field(default=None, repr=False)
    access_expires_at: float = 0.0
    refresh_token: str | None = field(default=None, repr=False)
    auth_origin: str | None = None
    bullhorn_user_ref: str | None = None
    created_at: float = 0.0  # link completion time: the absolute lifetime counts from here (L-5)
    last_refresh: float = 0.0
    link_id: str | None = field(default=None, repr=False)  # minted per completed link (B-3)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> SessionRecord | None:
        if not isinstance(data, dict) or set(data) != {f.name for f in fields(cls)}:
            return None
        try:
            rec = cls(**data)
        except TypeError:
            return None
        str_fields = ("tenant_key", "principal_key", "rest_url", "bh_rest_token")
        if not all(isinstance(getattr(rec, n), str) and getattr(rec, n) for n in str_fields):
            return None
        for n in ("bh_expires_at", "access_expires_at", "created_at", "last_refresh"):
            if isinstance(getattr(rec, n), bool) or not isinstance(getattr(rec, n), (int, float)):
                return None
        for n in ("access_token", "refresh_token", "auth_origin", "bullhorn_user_ref", "link_id"):
            if getattr(rec, n) is not None and not isinstance(getattr(rec, n), str):
                return None
        return rec


def _check_owner(tenant_key: str, principal_key: str) -> None:
    if not isinstance(tenant_key, str) or not _HEX64_RE.fullmatch(tenant_key):
        raise SessionStoreError("tenant_key must be 64 lowercase hex characters")
    if not isinstance(principal_key, str) or not _HEX64_RE.fullmatch(principal_key):
        raise SessionStoreError("principal_key must be 64 lowercase hex characters")


def _check_id(value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SessionStoreError("invalid pending-login identifier")


def new_id() -> str:
    """A 256-bit URL-safe random identifier (logins, links, state, CSRF)."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------- #
# Interface
# ---------------------------------------------------------------------- #


class SessionStore(ABC):
    """Abstract session store (D-5-12). A DB-backed adapter can be added later."""

    local_only = False

    @abstractmethod
    def get(self, tenant_key: str, principal_key: str) -> SessionRecord | None: ...

    @abstractmethod
    def put(self, record: SessionRecord) -> None: ...

    @abstractmethod
    def delete(self, tenant_key: str, principal_key: str) -> bool: ...

    @abstractmethod
    def _put_pending(self, kind: str, ident: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def _get_pending(self, kind: str, ident: str, take: bool) -> dict[str, Any] | None: ...

    @abstractmethod
    def prune(self, max_age: float = PENDING_MAX_AGE_SECONDS) -> int:
        """Delete pending entries older than ``max_age`` seconds; returns how many were removed."""

    def put_login(self, login_id: str, data: dict[str, Any]) -> None:
        _check_id(login_id)
        self._put_pending("login", login_id, data)

    def get_login(self, login_id: str) -> dict[str, Any] | None:
        _check_id(login_id)
        return self._get_pending("login", login_id, take=False)

    def take_login(self, login_id: str) -> dict[str, Any] | None:
        """Atomically remove and return a pending login (single use)."""
        _check_id(login_id)
        return self._get_pending("login", login_id, take=True)

    def put_link(self, link_id: str, data: dict[str, Any]) -> None:
        _check_id(link_id)
        self._put_pending("link", link_id, data)

    def take_link(self, link_id: str) -> dict[str, Any] | None:
        _check_id(link_id)
        return self._get_pending("link", link_id, take=True)

    @staticmethod
    def _owner_id(tenant_key: str, principal_key: str) -> str:
        _check_owner(tenant_key, principal_key)
        return hashlib.sha256(f"{tenant_key}|{principal_key}".encode("ascii")).hexdigest()

    def put_principal_link(self, tenant_key: str, principal_key: str, data: dict[str, Any]) -> None:
        """B-2: the confirmed-but-not-completed link of exactly this ``(tenant, principal)``."""
        self._put_pending("plink", self._owner_id(tenant_key, principal_key), data)

    def get_principal_link(self, tenant_key: str, principal_key: str) -> dict[str, Any] | None:
        return self._get_pending("plink", self._owner_id(tenant_key, principal_key), take=False)

    def delete_principal_link(self, tenant_key: str, principal_key: str) -> bool:
        return self._get_pending("plink", self._owner_id(tenant_key, principal_key), take=True) is not None


class MemorySessionStore(SessionStore):
    """In-process store for tests (never used by a shared deployment)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def get(self, tenant_key: str, principal_key: str) -> SessionRecord | None:
        _check_owner(tenant_key, principal_key)
        with self._lock:
            data = self._sessions.get((tenant_key, principal_key))
        rec = SessionRecord.from_dict(json.loads(json.dumps(data))) if data is not None else None
        if rec is not None and (rec.tenant_key, rec.principal_key) != (tenant_key, principal_key):
            return None
        return rec

    def put(self, record: SessionRecord) -> None:
        _check_owner(record.tenant_key, record.principal_key)
        with self._lock:
            self._sessions[(record.tenant_key, record.principal_key)] = record.to_dict()

    def delete(self, tenant_key: str, principal_key: str) -> bool:
        _check_owner(tenant_key, principal_key)
        with self._lock:
            return self._sessions.pop((tenant_key, principal_key), None) is not None

    def prune(self, max_age: float = PENDING_MAX_AGE_SECONDS) -> int:
        cutoff = time.time() - max_age
        with self._lock:
            stale = [k for k, (stored, _) in self._pending.items() if stored < cutoff]
            for k in stale:
                del self._pending[k]
        return len(stale)

    def _put_pending(self, kind: str, ident: str, data: dict[str, Any]) -> None:
        self.prune()
        with self._lock:
            self._pending[(kind, ident)] = (time.time(), json.loads(json.dumps(data)))

    def _get_pending(self, kind: str, ident: str, take: bool) -> dict[str, Any] | None:
        self.prune()
        with self._lock:
            item = self._pending.pop((kind, ident), None) if take else self._pending.get((kind, ident))
        return json.loads(json.dumps(item[1])) if item is not None else None


class EncryptedFileSessionStore(SessionStore):
    """AES-256-GCM file store (production adapter)."""

    def __init__(self, directory: str | os.PathLike[str], keys: SessionKeys) -> None:
        try:
            root = Path(directory).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SessionStoreError("session store directory does not exist") from None
        if not root.is_dir():
            raise SessionStoreError("session store directory is not a directory")
        self.root = root
        self.keys = keys
        for sub in ("sessions", "pending"):
            path = root / sub
            try:
                path.mkdir(mode=0o700, exist_ok=True)
            except OSError:
                raise SessionStoreError(f"session store: cannot create {sub}/") from None
            if path.is_symlink() or not path.is_dir():
                raise SessionStoreError(f"session store: {sub}/ is not a directory")

    def __repr__(self) -> str:
        return "EncryptedFileSessionStore(<redacted>)"

    # -- crypto ---------------------------------------------------------- #

    def _seal(self, plaintext: bytes, aad: bytes) -> bytes:
        kid = self.keys.active
        key = self.keys.key(kid)
        assert key is not None
        nonce = os.urandom(12)
        kid_bytes = kid.encode("ascii")
        return _MAGIC + bytes([len(kid_bytes)]) + kid_bytes + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)

    def _open(self, blob: bytes, aad: bytes) -> bytes | None:
        if len(blob) < 6 or blob[:4] != _MAGIC:
            return None
        n = blob[4]
        kid = blob[5 : 5 + n].decode("ascii", "replace")
        key = self.keys.key(kid)
        if key is None:
            logger.warning("session store: a session file uses an unknown key ID; failing closed")
            return None
        nonce = blob[5 + n : 17 + n]
        try:
            return AESGCM(key).decrypt(nonce, blob[17 + n :], aad)
        except (InvalidTag, ValueError):
            logger.warning("session store: a session file failed authentication; failing closed")
            return None

    # -- files ----------------------------------------------------------- #

    @staticmethod
    def _aad(*parts: str) -> bytes:
        return "|".join((*parts, SCHEMA_VERSION)).encode("ascii")

    def _session_path(self, tenant_key: str, principal_key: str) -> Path:
        name = hashlib.sha256(f"{tenant_key}|{principal_key}".encode("ascii")).hexdigest()
        return self.root / "sessions" / f"{name}.bin"

    def _pending_path(self, kind: str, ident: str) -> Path:
        name = hashlib.sha256(f"{kind}|{ident}".encode("ascii")).hexdigest()
        return self.root / "pending" / f"{kind}-{name}.bin"

    def _write(self, path: Path, blob: bytes) -> None:
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".bin", dir=str(path.parent))
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # pragma: no cover - platforms without POSIX modes
                pass
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            tmp = None
        except OSError:
            raise SessionStoreError("session store: write failed") from None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return None
            return path.read_bytes()
        except OSError:
            return None

    # -- sessions -------------------------------------------------------- #

    def get(self, tenant_key: str, principal_key: str) -> SessionRecord | None:
        _check_owner(tenant_key, principal_key)
        blob = self._read(self._session_path(tenant_key, principal_key))
        if blob is None:
            return None
        plain = self._open(blob, self._aad(tenant_key, principal_key))
        if plain is None:
            return None
        try:
            rec = SessionRecord.from_dict(json.loads(plain.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if rec is None or (rec.tenant_key, rec.principal_key) != (tenant_key, principal_key):
            return None
        return rec

    def put(self, record: SessionRecord) -> None:
        _check_owner(record.tenant_key, record.principal_key)
        plain = json.dumps(record.to_dict(), sort_keys=True).encode("utf-8")
        blob = self._seal(plain, self._aad(record.tenant_key, record.principal_key))
        self._write(self._session_path(record.tenant_key, record.principal_key), blob)

    def delete(self, tenant_key: str, principal_key: str) -> bool:
        _check_owner(tenant_key, principal_key)
        try:
            os.unlink(self._session_path(tenant_key, principal_key))
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise SessionStoreError("session store: delete failed") from None

    # -- pending logins / links -------------------------------------------- #

    def prune(self, max_age: float = PENDING_MAX_AGE_SECONDS) -> int:
        """Remove pending files (and leftover temp/claimed files) older than ``max_age`` (by mtime)."""
        cutoff = time.time() - max_age
        removed = 0
        directory = self.root / "pending"
        try:
            names = os.listdir(directory)
        except OSError:
            return 0
        for name in names:
            path = directory / name
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue
        return removed

    def _put_pending(self, kind: str, ident: str, data: dict[str, Any]) -> None:
        self.prune()
        plain = json.dumps(data, sort_keys=True).encode("utf-8")
        self._write(self._pending_path(kind, ident), self._seal(plain, self._aad(kind, ident)))

    def _get_pending(self, kind: str, ident: str, take: bool) -> dict[str, Any] | None:
        self.prune()
        path = self._pending_path(kind, ident)
        if take:
            claimed = path.with_name(f".taken-{uuid.uuid4().hex}.bin")
            try:
                os.replace(path, claimed)  # atomic: exactly one taker wins
            except OSError:
                return None
            blob = self._read(claimed)
            try:
                os.unlink(claimed)
            except OSError:
                pass
        else:
            blob = self._read(path)
        if blob is None:
            return None
        plain = self._open(blob, self._aad(kind, ident))
        if plain is None:
            return None
        try:
            data = json.loads(plain.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        return data if isinstance(data, dict) else None


class KeyringSessionStore(MemorySessionStore):  # pragma: no cover - optional local development adapter
    """Local-development adapter backed by the OS keyring (optional ``keyring`` package).

    Local only: ``identity.deploy`` refuses it in ``shared`` mode. Pending logins
    stay in memory (local mode has no HTTP callback).
    """

    local_only = True
    SERVICE = "bullhorn-mcp-agent"

    def __init__(self) -> None:
        super().__init__()
        try:
            import keyring
        except ImportError:
            raise SessionStoreError("the keyring package is not installed") from None
        self._keyring = keyring

    def get(self, tenant_key: str, principal_key: str) -> SessionRecord | None:
        _check_owner(tenant_key, principal_key)
        raw = self._keyring.get_password(self.SERVICE, f"{tenant_key}|{principal_key}")
        try:
            rec = SessionRecord.from_dict(json.loads(raw)) if raw else None
        except (ValueError, RecursionError):
            return None
        if rec is None or (rec.tenant_key, rec.principal_key) != (tenant_key, principal_key):
            return None
        return rec

    def put(self, record: SessionRecord) -> None:
        _check_owner(record.tenant_key, record.principal_key)
        self._keyring.set_password(self.SERVICE, f"{record.tenant_key}|{record.principal_key}", json.dumps(record.to_dict()))

    def delete(self, tenant_key: str, principal_key: str) -> bool:
        _check_owner(tenant_key, principal_key)
        try:
            self._keyring.delete_password(self.SERVICE, f"{tenant_key}|{principal_key}")
            return True
        except Exception:
            return False
