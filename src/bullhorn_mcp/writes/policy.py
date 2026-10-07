"""Write-scope policy (D-4B-1, D-4B-2, D-4B-3).

- A write scope is **disabled until enabled**: it is enabled only when
  ``BULLHORN_ENABLED_WRITE_SCOPES`` (comma list) contains it.
- The actor comes only from ``BULLHORN_MCP_ACTOR`` (validated like 4A's
  ``resolve_actor``; ``BULLHORN_SETUP_ADMINS`` is a configuration-change list
  and does not apply to writes). A write is refused when it is unset.
- When ``BULLHORN_WRITE_APPROVERS`` is set, the approving actor (confirm, or a
  direct-mode write) must be in it.
- ``BULLHORN_NOTE_CREATE_MODE=direct`` allows a single-call write.

``crosscutting/permissions.py`` is not changed; its ``check`` is still called
first by the tools.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from ..tenant.actor import ACTOR_ENV_VAR, resolve_actor

SCOPES_ENV_VAR = "BULLHORN_ENABLED_WRITE_SCOPES"
APPROVERS_ENV_VAR = "BULLHORN_WRITE_APPROVERS"
NOTE_MODE_ENV_VAR = "BULLHORN_NOTE_CREATE_MODE"
NOTE_CREATE_SCOPE = "note.create"
KNOWN_SCOPES = frozenset({NOTE_CREATE_SCOPE})


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    actor: str | None
    missing: tuple[str, ...] = ()


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _csv(raw: object) -> frozenset[str]:
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def enabled_scopes(env: Mapping[str, str] | None = None) -> frozenset[str]:
    return _csv(_env(env).get(SCOPES_ENV_VAR, "")) & KNOWN_SCOPES


def write_actor(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """``(actor, problem)`` from ``BULLHORN_MCP_ACTOR`` only."""
    resolution = resolve_actor({ACTOR_ENV_VAR: _env(env).get(ACTOR_ENV_VAR, "")})
    return resolution.actor, resolution.reason


def check_scope(scope: str, env: Mapping[str, str] | None = None, *, approving: bool = False) -> ScopeDecision:
    """Is ``scope`` enabled, and is there an (allowed) actor? Never raises."""
    env = _env(env)
    missing: list[str] = []
    if scope not in enabled_scopes(env):
        missing.append(f"scope:{scope}")
    actor, _problem = write_actor(env)
    if actor is None:
        missing.append(f"env:{ACTOR_ENV_VAR}")
    elif approving:
        approvers = _csv(env.get(APPROVERS_ENV_VAR, ""))
        if approvers and actor not in approvers:
            missing.append(f"approver:{APPROVERS_ENV_VAR}")
    return ScopeDecision(not missing, actor, tuple(missing))


def direct_mode(env: Mapping[str, str] | None = None) -> bool:
    raw = _env(env).get(NOTE_MODE_ENV_VAR, "")
    return isinstance(raw, str) and raw.strip() == "direct"
