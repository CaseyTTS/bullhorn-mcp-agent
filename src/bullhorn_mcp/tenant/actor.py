"""Actor resolution for configuration changes (D-4A-2).

The actor comes only from the ``BULLHORN_MCP_ACTOR`` environment variable,
never from a tool argument (an LLM could spoof an argument). When
``BULLHORN_SETUP_ADMINS`` (a comma-separated list) is set, the actor must be
in it.

Phase 5A (D-5A-10): in ``shared`` mode the environment is ignored. The actor is
the authenticated caller's principal key (``identity.principal``), and it is
allowed only when the admin config lists it in ``roles.setup_admins``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

ACTOR_ENV_VAR = "BULLHORN_MCP_ACTOR"
ADMINS_ENV_VAR = "BULLHORN_SETUP_ADMINS"
MAX_ACTOR_CHARS = 128
_ACTOR_RE = re.compile(r"[A-Za-z0-9_.@+\-]+(?: [A-Za-z0-9_.@+\-]+)*", re.ASCII)


@dataclass(frozen=True)
class ActorResolution:
    actor: str | None
    allowed: bool
    reason: str | None = None


def _shared_actor() -> ActorResolution | None:
    """``None`` in local mode; the identity-context actor in shared mode (D-5A-10)."""
    from ..identity import deploy

    if not deploy.is_shared():
        return None
    from ..identity.principal import IdentityRequired, current_identity

    try:
        ident = current_identity()
    except IdentityRequired:
        return ActorResolution(None, False, "identity_required")
    if ident.principal_key is None:
        return ActorResolution(None, False, "identity_required")
    if ident.tenant is None or not ident.tenant.roles.is_setup_admin(ident.principal_key):  # B-4: per tenant
        return ActorResolution(ident.principal_key, False, "principal is not listed in roles.setup_admins")
    return ActorResolution(ident.principal_key, True, None)


def resolve_actor(env: Mapping[str, str] | None = None) -> ActorResolution:
    """Resolve the configuration-change actor from the environment. Never raises."""
    shared = _shared_actor()
    if shared is not None:
        return shared
    env = os.environ if env is None else env
    raw = env.get(ACTOR_ENV_VAR, "")
    actor = raw.strip() if isinstance(raw, str) else ""
    if not actor:
        return ActorResolution(None, False, f"{ACTOR_ENV_VAR} is not set")
    if len(actor) > MAX_ACTOR_CHARS or not _ACTOR_RE.fullmatch(actor):
        return ActorResolution(None, False, f"{ACTOR_ENV_VAR} is not a valid actor identifier")
    admins_raw = env.get(ADMINS_ENV_VAR, "")
    if isinstance(admins_raw, str) and admins_raw.strip():
        admins = {a.strip() for a in admins_raw.split(",") if a.strip()}
        if actor not in admins:
            return ActorResolution(actor, False, f"actor is not listed in {ADMINS_ENV_VAR}")
    return ActorResolution(actor, True, None)
