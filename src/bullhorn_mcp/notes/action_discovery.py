"""Note action discovery, drift inputs and adoption helpers (Phase 5B, D-5B-1..D-5B-6).

Discovery sources, in order (D-5B-1):

1. ``meta``: the ``Note.action`` options of the ``/meta/Note`` response, already
   captured by the 4A discovery snapshot (HV-A2 / HV-B10).
2. ``settings``: ``GET /settings/commentActionList``, **only** while
   ``SETTINGS_ACTION_SOURCE_VERIFIED`` is ``True``. It is ``False`` because
   HV-D2/HV-D3 are unresolved (``docs/architecture/PHASE5B_HV_VERIFICATION.md``),
   so the source is never called and is reported as ``unresolved``.
3. ``administrator``: values an administrator enters with ``set_value_mapping``
   (not a discovery source; nothing here produces them).

Values are never guessed, normalised or substituted. Discovery never changes
the profile: discovered values reach it only through ``propose_mapping_changes``
-> ``commit_mapping_changes`` with ``apply_discovered_note_actions`` (D-5B-3).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from ..bullhorn.settings_reader import SettingsReader
from ..bullhorn.writes import safe_error_text
from ..schema.errors import SchemaError
from ..tenant.profile_v2 import NOTE_ACTION_FIELD, NOTE_ENTITY  # P5B-13: the single source
from ..tenant.revalidation import (
    MAX_NOTE_ACTION_VALUE_CHARS,
    MAX_NOTE_ACTION_VALUES,
    DiscoverySnapshot,
    NoteActionSource,
    NoteActionSources,
)
from .action_types import REQUIREMENT, valid_set

# HV-D1..D3 (PHASE5B_HV_VERIFICATION.md): D2 and D3 are unresolved, so the source stays off.
SETTINGS_ACTION_SOURCE_VERIFIED = False
SETTINGS_NAME = "commentActionList"
MAX_WARNING_CHARS = 300
DEFAULT_KEY_PREFIX = "discovered"
# Sec-N4: an adopted key is always "note.action.<prefix>.<slug>_<hash8>"; the prefix is one plain segment.
KEY_NAMESPACE = "note.action"
KEY_PREFIX_RE = re.compile(r"[a-z][a-z0-9_]{0,31}", re.ASCII)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _source(status: str, values: list[str] | tuple[str, ...] = (), warning: str | None = None) -> NoteActionSource:
    return NoteActionSource(status=status, values=tuple(values), warning=warning)


def _collect(raw: Any, what: str) -> NoteActionSource:
    """A verified source from a list of strings; anything malformed makes the whole source unverifiable."""
    if not isinstance(raw, list):
        # Sec-N2: only the type name (and length) of the payload, never its content.
        size = f" of length {len(raw)}" if hasattr(raw, "__len__") else ""
        return _source("unverifiable", warning=f"{what}: expected a list of strings, got {type(raw).__name__}{size}"[:MAX_WARNING_CHARS])
    if not raw:
        return _source("unverifiable", warning=f"{what}: no values")
    if len(raw) > MAX_NOTE_ACTION_VALUES:
        return _source("unverifiable", warning=f"{what}: more than {MAX_NOTE_ACTION_VALUES} values")
    values: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_NOTE_ACTION_VALUE_CHARS:
            size = f" of length {len(item)}" if hasattr(item, "__len__") else ""
            problem = (
                f"a value ({type(item).__name__}{size}) is not a non-blank string of at most {MAX_NOTE_ACTION_VALUE_CHARS} characters"
            )
            return _source("unverifiable", warning=f"{what}: {problem}")
        values[item] = None
    return _source("verified", list(values))


def meta_source(snapshot: DiscoverySnapshot) -> NoteActionSource:
    """Source 1: the ``Note.action`` options recorded in the discovery snapshot (no request)."""
    ent = snapshot.entity(NOTE_ENTITY)
    if ent is None or not ent.usable:
        return _source("unverifiable", warning="Note metadata is unavailable")
    fsnap = ent.fields.get(NOTE_ACTION_FIELD)
    if fsnap is None:
        return _source("unverifiable", warning="Note.action is absent from metadata")
    if fsnap.options is None:
        return _source("unverifiable", warning="Note.action options are not present in metadata (HV-A2)")
    return _collect([value for value, _ in fsnap.options], "Note.action options")


def settings_source(client: Any) -> NoteActionSource:
    """Source 2: ``GET /settings/commentActionList`` - only when HV-D1..D3 are verified. Never raises."""
    if not SETTINGS_ACTION_SOURCE_VERIFIED:
        return _source("unresolved", warning="HV-D1..D3 unresolved: /settings/commentActionList is not consulted")
    try:
        data = SettingsReader(client).get([SETTINGS_NAME])
    except Exception as exc:  # HV-D3: any failure (including non-200) is unverifiable; never guessed
        return _source("unverifiable", warning=safe_error_text(exc, MAX_WARNING_CHARS))
    if SETTINGS_NAME not in data:
        return _source("unverifiable", warning=f"settings response has no {SETTINGS_NAME!r}")
    return _collect(data[SETTINGS_NAME], SETTINGS_NAME)


def discover_note_actions(snapshot: DiscoverySnapshot, client: Any, checked_at: str) -> NoteActionSources:
    """Every source's value set (D-5B-1). Read-only: never changes the profile."""
    return NoteActionSources(
        checked_at=checked_at,
        sources={"meta": meta_source(snapshot), "settings": settings_source(client)},
    )


# ---------------------------------------------------------------------- #
# Adoption (D-5B-3)
# ---------------------------------------------------------------------- #


def verified_sources_by_value(sources: NoteActionSources | None) -> dict[str, str]:
    """``{value: first verified source containing it}`` (source order: meta, then settings).

    Sec-N4: the ``settings`` source is ignored while ``SETTINGS_ACTION_SOURCE_VERIFIED`` is ``False``,
    whatever a (possibly forged or stale) snapshot claims.
    """
    out: dict[str, str] = {}
    if sources is None:
        return out
    for name, values in sources.verified().items():
        if name == "settings" and not SETTINGS_ACTION_SOURCE_VERIFIED:
            continue
        for value in values:
            out.setdefault(value, name)
    return out


def discovered_key(prefix: str, value: str) -> str:
    """The deterministic record key for an adopted value (``note.action.{prefix}.{slug}_{hash8}``)."""
    if not isinstance(prefix, str) or KEY_PREFIX_RE.fullmatch(prefix) is None:
        raise ValueError("invalid key prefix")
    slug = _SLUG_RE.sub("_", value.casefold().encode("ascii", "ignore").decode("ascii")).strip("_")[:40].strip("_")
    if not slug or not slug[0].isalpha():
        slug = "v_" + slug if slug else "v"
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{KEY_NAMESPACE}.{prefix}.{slug}_{digest}"


# ---------------------------------------------------------------------- #
# setup_status requirement_details (D-5B-6)
# ---------------------------------------------------------------------- #


def note_action_requirements(env: Mapping[str, str]) -> list[str]:
    """``[REQUIREMENT]`` unless the active profile has a usable note_action mapping. Never raises."""
    from ..tenant.state import effective_states  # local: tenant.state imports tenant.changes, which imports this module
    from ..tenant.store import store_from_env

    try:
        store = store_from_env(env)
        active = store.active_version() if store is not None else None
        if store is None or active is None:
            return [REQUIREMENT]
        profile = store.read_version(active)
        states, _ = effective_states(profile, store.read_discovery())
    except (SchemaError, OSError):
        return [REQUIREMENT]
    return [] if valid_set(profile, states).usable else [REQUIREMENT]
