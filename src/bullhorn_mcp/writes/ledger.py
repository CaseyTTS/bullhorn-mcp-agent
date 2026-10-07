"""Persistent idempotency ledger (D-4B-6, review fix B-3) and the write-area file helpers.

Layout under the 4A setup store::

    writes/ledger/<sha256>.g<N>.json          generation N of a key's entry (open(..., "x"); never rewritten)
    writes/ledger/<sha256>.g<N>.result.json   the outcome of generation N (open(..., "x"); never rewritten)
    writes/pending/<op_id>.json               pending operations (pipeline.py)
    writes/journal.jsonl                      append-only write journal (journal.py)

The current entry of a key is its highest generation. A generation without a
result file is ``pending``; the result file makes it ``committed`` (with
``record_id``) or ``failed``.

- A derived key is honoured for 24 hours, a caller-supplied key for 7 days.
- A ``failed`` or expired ``committed`` generation ``N`` is superseded only by
  creating generation ``N+1`` exclusively: under any interleaving at most one
  writer gets ``Verdict("new")`` per generation. A losing writer re-reads, sees
  the winner's ``pending`` generation and gets ``in_doubt``; never ``new``.
- A ``pending`` generation (for example after a crash) is **in doubt**: the
  write is refused and must be verified manually. It is never retried
  automatically.
- Nothing in the ledger is ever replaced, renamed or deleted.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..activity.events import sha256_hex
from ..tenant.store import SetupStore, SetupStoreError
from ..tenant.timeutil import format_utc, parse_utc

DERIVED_WINDOW = _dt.timedelta(hours=24)
CALLER_WINDOW = _dt.timedelta(days=7)
MAX_ENTRY_BYTES = 100_000
MAX_GENERATION = 999_999_999
_GENERATION_RE = re.compile(r"([0-9a-f]{64})\.g([1-9][0-9]{0,8})\.json", re.ASCII)


# ---------------------------------------------------------------------- #
# File helpers (every OSError -> SetupStoreError)
# ---------------------------------------------------------------------- #


def write_dir(store: SetupStore, *parts: str) -> Path:
    path = store.root
    for part in ("writes", *parts):
        path = path / part
        try:
            path.mkdir(exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise SetupStoreError(f"setup store: {part}/ is not a directory")
        except SetupStoreError:
            raise
        except OSError as exc:
            raise SetupStoreError(f"setup store: could not create {part}/ ({type(exc).__name__})") from exc
    return path


def dumps(data: Any) -> str:
    try:
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SetupStoreError(f"setup store: data is not serializable ({type(exc).__name__})") from exc


def read_json(path: Path) -> Any:
    """``None`` when absent. A corrupt/partial file reads as ``{"corrupt": True}`` (never as absent)."""
    try:
        if path.is_symlink():
            return {"corrupt": True}
        if not path.exists():
            return None
        if path.stat().st_size > MAX_ENTRY_BYTES * 20:
            return {"corrupt": True}
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return {"corrupt": True}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return {"corrupt": True}
    return data if isinstance(data, dict) else {"corrupt": True}


def exclusive_create(path: Path, data: Any) -> bool:
    """Create ``path`` exclusively. ``False`` when it already exists (someone else won)."""
    text = dumps(data)
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        return False
    except OSError as exc:
        raise SetupStoreError(f"setup store: could not create {path.name} ({type(exc).__name__})") from exc
    return True


# ---------------------------------------------------------------------- #
# Ledger
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class IdempotencyKey:
    kind: str  # "caller" | "derived"
    value: str  # the caller's key, or the derived sha256
    payload_hash: str

    @property
    def file_id(self) -> str:
        return sha256_hex(f"{self.kind}:{self.value}")

    @property
    def window(self) -> _dt.timedelta:
        return CALLER_WINDOW if self.kind == "caller" else DERIVED_WINDOW


@dataclass(frozen=True)
class Verdict:
    status: str  # "new" | "duplicate" | "in_doubt" | "conflict"
    record_id: int | None = None
    generation: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.status, "record_id": self.record_id}


class Ledger:
    def __init__(self, store: SetupStore) -> None:
        self.store = store

    def _dir(self, *, create: bool) -> Path:
        return write_dir(self.store, "ledger") if create else self.store.root / "writes" / "ledger"

    def _generation_path(self, key: IdempotencyKey, generation: int, *, create_dir: bool = False) -> Path:
        return self._dir(create=create_dir) / f"{key.file_id}.g{generation}.json"

    @staticmethod
    def _result_path(entry_path: Path) -> Path:
        return entry_path.with_name(entry_path.name[: -len(".json")] + ".result.json")

    def current_generation(self, key: IdempotencyKey) -> int:
        """The highest generation number of ``key`` (0 when there is none)."""
        directory = self._dir(create=False)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise SetupStoreError(f"setup store: could not list writes/ledger/ ({type(exc).__name__})") from exc
        best = 0
        for name in names:
            m = _GENERATION_RE.fullmatch(name)
            if m and m.group(1) == key.file_id:
                best = max(best, int(m.group(2)))
        return best

    def _path(self, key: IdempotencyKey, *, create_dir: bool = False) -> Path:
        """The current generation's entry path (generation 1 when the key is new)."""
        return self._generation_path(key, max(1, self.current_generation(key)), create_dir=create_dir)

    def _state(self, key: IdempotencyKey, generation: int) -> dict[str, Any]:
        """The effective state of one generation: its entry merged with its result, or ``{"corrupt": True}``."""
        path = self._generation_path(key, generation)
        entry = read_json(path)
        if entry is None:
            return {"corrupt": True}  # listed but unreadable/vanished: never treated as absent
        if entry.get("corrupt") is True or entry.get("generation") != generation or entry.get("file_id") != key.file_id:
            return {"corrupt": True}
        result = read_json(self._result_path(path))
        if result is None:
            return {**entry, "status": "pending"}
        if result.get("corrupt") is True or result.get("generation") != generation or result.get("status") not in ("committed", "failed"):
            return {"corrupt": True}
        return {**entry, **result}

    @staticmethod
    def _live(state: dict[str, Any], now: _dt.datetime) -> bool:
        if state.get("status") == "pending" or state.get("corrupt") is True:
            return True  # in doubt: never expires on its own
        if state.get("status") == "failed":
            return False
        expires = parse_utc(state.get("expires_at"))
        return expires is None or now < expires

    def _verdict(self, key: IdempotencyKey, state: dict[str, Any]) -> Verdict:
        if state.get("corrupt") is True or state.get("status") == "pending":
            return Verdict("in_doubt")
        if state.get("payload_hash") != key.payload_hash:
            return Verdict("conflict")
        record_id = state.get("record_id")
        return Verdict("duplicate", record_id if type(record_id) is int else None)

    def lookup(self, key: IdempotencyKey, now: _dt.datetime) -> Verdict:
        generation = self.current_generation(key)
        if generation == 0:
            return Verdict("new")
        state = self._state(key, generation)
        if not self._live(state, now):
            return Verdict("new")
        return self._verdict(key, state)

    def begin(
        self,
        key: IdempotencyKey,
        now: _dt.datetime,
        *,
        operation_id: str,
        correlation_id: str,
        identity: dict[str, Any] | None = None,
    ) -> Verdict:
        """Claim the key by exclusively creating the next generation (``pending``).

        Returns ``Verdict("new", generation=N)`` only for the single winner of generation N.
        Phase 5A (AC-14): a shared-mode write records its identity triple in the entry.
        """
        self._dir(create=True)
        for _ in range(3):
            current = self.current_generation(key)
            if current:
                state = self._state(key, current)
                if self._live(state, now):
                    return self._verdict(key, state)
            nxt = current + 1
            if nxt > MAX_GENERATION:
                return Verdict("in_doubt")
            entry = {
                "file_id": key.file_id,
                "generation": nxt,
                "kind": key.kind,
                "payload_hash": key.payload_hash,
                "operation_id": operation_id,
                "correlation_id": correlation_id,
                "created_at": format_utc(now),
            }
            if identity is not None:
                entry["identity"] = dict(identity)
            if exclusive_create(self._generation_path(key, nxt), entry):
                return Verdict("new", generation=nxt)
            # Lost the race for generation `nxt`: re-read; the winner's pending entry is now current.
        return Verdict("in_doubt")

    def finish(self, key: IdempotencyKey, generation: int, now: _dt.datetime, status: str, record_id: int | None = None) -> None:
        """Record the outcome of ``generation`` (exclusive create; an existing result is never replaced)."""
        if status not in ("committed", "failed"):
            raise ValueError("status must be 'committed' or 'failed'")
        result = {
            "generation": generation,
            "status": status,
            "record_id": record_id,
            "updated_at": format_utc(now),
            "expires_at": format_utc(now + key.window),
        }
        if not exclusive_create(self._result_path(self._generation_path(key, generation)), result):
            raise SetupStoreError("setup store: the ledger outcome was already recorded")
