"""The write journal (``writes/journal.jsonl``) and its audit bridge (§1.4 stage 9, D-4B-8).

Every transition is one journal line plus one ``audit.log_invocation`` record.
Neither ever contains the note text: only ``comments_sha256`` and
``comments_length``. Every error string passes through ``safe_error_text``
(bounded and redacted).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..bullhorn.writes import safe_error_text
from ..crosscutting import audit
from ..schema.errors import truncate_text
from ..tenant.store import SetupStore, SetupStoreError
from .ledger import write_dir

TRANSITIONS = (
    "previewed",
    "confirmed",
    "rejected",
    "committed",
    "partially_committed",
    "failed",
    "failed_orphan",
    "in_doubt",
)
_ALLOWED_KEYS = (
    "correlation_id",
    "operation_id",
    "actor",
    "operation",
    "entity",
    "record_id",
    "targets",
    "approval",
    "bullhorn_response",
    "outcome",
    "mode",
    "comments_sha256",
    "comments_length",
    "associations",
    "at",
)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<nested>"
    if isinstance(value, str):
        return safe_error_text(value, 300)
    if isinstance(value, dict):
        return {truncate_text(str(k), 64): _scrub(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in list(value)[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def journal_entry(transition: str, **fields: Any) -> dict[str, Any]:
    """A journal line: only the allowed keys (never ``comments``), every string bounded and redacted."""
    if transition not in TRANSITIONS:
        raise ValueError("unknown journal transition")
    entry: dict[str, Any] = {"transition": transition}
    for key in _ALLOWED_KEYS:
        if key in fields:
            entry[key] = _scrub(fields[key])
    return entry


def record(store: SetupStore | None, tool: str, transition: str, **fields: Any) -> dict[str, Any]:
    """Append one journal line and emit one audit record for ``transition``."""
    entry = journal_entry(transition, **fields)
    audit.log_invocation(
        tool=tool,
        args={"write_transition": entry},
        result_summary=f"{transition}: correlation_id={entry.get('correlation_id')}",
        duration_ms=0.0,
        success=transition not in ("failed", "failed_orphan", "in_doubt"),
    )
    if store is not None:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        try:
            with open(write_dir(store) / "journal.jsonl", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise SetupStoreError(f"setup store: could not append to writes/journal.jsonl ({type(exc).__name__})") from exc
    return entry
