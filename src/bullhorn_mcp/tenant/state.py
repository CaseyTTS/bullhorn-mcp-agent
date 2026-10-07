"""Setup state machine and capability gating (TS-7, D-4A-4, D-4A-6).

``compute_setup_state`` is a pure function of the environment, the
connection-check result, the store contents and the catalog fingerprint. The
first matching row wins:

1. ``disconnected``: a credential variable is missing, or the connection check failed.
2. ``connected_setup_required``: no store, or no active version and no open proposal.
3. ``setup_in_progress``: no active version, at least one open proposal.
4. ``setup_invalid``: the active version fails to load, or has an active ``broken``
   mapping in its last recorded validation.
5. ``setup_revalidation_required``: catalog or ``rest_url`` fingerprint changed,
   unresolved drift recorded by discovery, or imported records still unvalidated.
6. ``setup_valid``.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schema.errors import SchemaError, path_segment, truncate_text
from .actor import ACTOR_ENV_VAR, resolve_actor
from .capabilities import CAPABILITIES, GateResult, evaluate_capability
from .changes import is_open
from .profile_v2 import TenantProfileV2, current_catalog_fingerprint
from .store import STORE_ENV_VAR, SetupStore, store_from_env
from .timeutil import utc_now

CREDENTIAL_ENV_VARS = ("BULLHORN_CLIENT_ID", "BULLHORN_CLIENT_SECRET", "BULLHORN_USERNAME", "BULLHORN_PASSWORD")

STATES = (
    "disconnected",
    "connected_setup_required",
    "setup_in_progress",
    "setup_invalid",
    "setup_revalidation_required",
    "setup_valid",
)


@dataclass(frozen=True)
class ConnectionCheck:
    """Result of a connection check. ``None`` passed instead means "not checked"."""

    ok: bool
    rest_url_fingerprint: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SetupState:
    state: str
    missing_requirements: tuple[str, ...] = ()
    capabilities: Mapping[str, GateResult] = field(default_factory=dict)
    active_version: int | None = None
    open_proposals: tuple[str, ...] = ()
    last_validation: Mapping[str, Any] | None = None
    connection_checked: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "missing_requirements": list(self.missing_requirements),
            "capabilities": {k: v.to_dict() for k, v in self.capabilities.items()},
            "active_version": self.active_version,
            "open_proposals": list(self.open_proposals),
            "last_validation": dict(self.last_validation) if self.last_validation is not None else None,
            "connection_checked": self.connection_checked,
            "detail": self.detail,
        }


def _capabilities(profile: TenantProfileV2 | None, states: Mapping[str, str], state: str) -> dict[str, GateResult]:
    return {name: evaluate_capability(name, profile, states, state) for name in CAPABILITIES}


def _result(
    state: str,
    missing: list[str],
    *,
    profile: TenantProfileV2 | None = None,
    states: Mapping[str, str] | None = None,
    active_version: int | None = None,
    open_proposals: tuple[str, ...] = (),
    last_validation: Mapping[str, Any] | None = None,
    connection_checked: bool = False,
    detail: str | None = None,
) -> SetupState:
    caps = _capabilities(profile, states or {}, state)
    all_missing = list(dict.fromkeys(missing))
    if state in ("setup_invalid", "setup_revalidation_required", "setup_valid"):
        for gate in caps.values():
            for item in gate.missing:
                if not item.startswith("state:") and item not in all_missing:
                    all_missing.append(item)
    return SetupState(
        state=state,
        missing_requirements=tuple(all_missing),
        capabilities=caps,
        active_version=active_version,
        open_proposals=open_proposals,
        last_validation=last_validation,
        connection_checked=connection_checked,
        detail=None if detail is None else truncate_text(detail, 300),
    )


def effective_states(profile: TenantProfileV2, discovery_doc: Mapping[str, Any] | None) -> tuple[dict[str, str], Mapping[str, Any] | None]:
    """Record states from the last recorded validation of this version, else from the version file."""
    states = {r.key: r.validation.state for r in profile.field_mappings}
    states.update({r.diff_key: r.validation.state for r in profile.value_mappings})
    last = discovery_doc.get("last_validation") if discovery_doc else None
    if isinstance(last, dict) and last.get("version") == profile.profile_version and isinstance(last.get("states"), dict):
        for key, value in last["states"].items():
            if key in states and isinstance(value, str):
                states[key] = value
        return states, last
    return states, None


def _shared_row1(execution_tier: str | None = None) -> list[str] | None:
    """``None`` in local mode. Shared mode (A3-2): row 1 holds only for a ``bullhorn_user``
    or ``service`` caller; the credential environment variables are ignored.
    ``execution_tier="service"`` (Phase 6 M1-A1): row 1 is held by the resolved service session."""
    from ..identity import deploy

    if not deploy.is_shared():
        return None
    if execution_tier == "service":
        return []
    from ..identity.principal import current_tier, has_bullhorn_access

    return [] if has_bullhorn_access(current_tier()) else ["bullhorn_session"]


def compute_setup_state(
    env: Mapping[str, str] | None = None,
    connection: ConnectionCheck | None = None,
    catalog_fingerprint: str | None = None,
    now: _dt.datetime | None = None,
    *,
    execution_tier: str | None = None,
) -> SetupState:
    """Compute the setup state. Never raises for store/profile problems (they become states).

    ``execution_tier`` (Phase 6 M1-A1): ``None`` (the caller's tier) or ``"service"``; anything else is a ``ValueError``.
    """
    if execution_tier not in (None, "service"):
        raise ValueError("execution_tier must be None or 'service'")
    env = os.environ if env is None else env
    catalog_fingerprint = catalog_fingerprint or current_catalog_fingerprint()
    now = now or utc_now()
    checked = connection is not None

    # Row 1 (Amendment A3-2: in shared mode the caller's session, not the environment)
    shared_missing = _shared_row1(execution_tier)
    if shared_missing is not None:
        missing = shared_missing
    else:
        missing = [f"env:{name}" for name in CREDENTIAL_ENV_VARS if not (env.get(name) or "").strip()]
    if missing:
        return _result("disconnected", missing, connection_checked=checked)
    if connection is not None and not connection.ok:
        return _result("disconnected", ["connection:failed"], connection_checked=True, detail=connection.error)

    actor_missing = [] if resolve_actor(env).actor else [f"env:{ACTOR_ENV_VAR}"]

    # Row 2 (store)
    try:
        store: SetupStore | None = store_from_env(env)
    except SchemaError as exc:
        return _result("connected_setup_required", ["store:unusable", *actor_missing], connection_checked=checked, detail=str(exc))
    if store is None:
        return _result("connected_setup_required", [f"env:{STORE_ENV_VAR}", *actor_missing], connection_checked=checked)

    try:
        active = store.active_version()
        proposals = tuple(p["proposal_id"] for p in store.list_proposals() if is_open(p, now) and isinstance(p.get("proposal_id"), str))
        discovery_doc = store.read_discovery()
    except SchemaError as exc:
        return _result("connected_setup_required", ["store:unusable", *actor_missing], connection_checked=checked, detail=str(exc))

    if active is None:
        if not proposals:  # Row 2
            return _result(
                "connected_setup_required", ["profile:active_version", *actor_missing], open_proposals=proposals, connection_checked=checked
            )
        return _result(  # Row 3
            "setup_in_progress", ["profile:active_version", *actor_missing], open_proposals=proposals, connection_checked=checked
        )

    # Row 4
    try:
        profile = store.read_version(active)
    except SchemaError as exc:
        return _result(
            "setup_invalid",
            ["profile:invalid", *actor_missing],
            active_version=active,
            open_proposals=proposals,
            connection_checked=checked,
            detail=str(exc),
        )
    states, last = effective_states(profile, discovery_doc)
    broken = [r.key for r in profile.field_mappings if r.active and states.get(r.key) == "broken"]
    broken += [r.diff_key for r in profile.value_mappings if r.active and states.get(r.diff_key) == "broken"]
    common: dict[str, Any] = {
        "profile": profile,
        "states": states,
        "active_version": active,
        "open_proposals": proposals,
        "last_validation": last,
        "connection_checked": checked,
    }
    if broken:
        return _result("setup_invalid", [f"broken:{k}" for k in broken] + actor_missing, **common)

    # Row 5
    reasons: list[str] = []
    if profile.catalog_fingerprint != catalog_fingerprint:
        reasons.append("revalidate:catalog_changed")
    if (
        connection is not None
        and profile.rest_url_fingerprint is not None
        and connection.rest_url_fingerprint is not None
        and connection.rest_url_fingerprint != profile.rest_url_fingerprint
    ):
        reasons.append("revalidate:rest_url_changed")
    if discovery_doc and discovery_doc.get("drift_unresolved") is True:
        reasons.append("revalidate:drift_unresolved")
    unvalidated = [r.key for r in profile.field_mappings if r.active and states.get(r.key) == "unvalidated"]
    unvalidated += [r.diff_key for r in profile.value_mappings if r.active and states.get(r.diff_key) == "unvalidated"]
    if unvalidated:
        reasons.append("revalidate:unvalidated_mappings")
    if reasons:
        return _result("setup_revalidation_required", reasons + actor_missing, **common)

    # Row 6
    return _result("setup_valid", actor_missing, **common)


def require_capability(name: str, state: SetupState | None = None, env: Mapping[str, str] | None = None) -> GateResult:
    """The capability gate. Without ``state``, the state is computed without a connection check."""
    if name not in CAPABILITIES:
        return GateResult(False, (f"capability:unknown:{path_segment(name)}",))
    current = state if state is not None else compute_setup_state(env)
    gate = current.capabilities.get(name)
    return gate if gate is not None else GateResult(False, (f"state:{current.state}",))
