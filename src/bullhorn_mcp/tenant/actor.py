"""Actor resolution for configuration changes (D-4A-2).

The actor comes only from the ``BULLHORN_MCP_ACTOR`` environment variable,
never from a tool argument (an LLM could spoof an argument). When
``BULLHORN_SETUP_ADMINS`` (a comma-separated list) is set, the actor must be
in it.
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


def resolve_actor(env: Mapping[str, str] | None = None) -> ActorResolution:
    """Resolve the configuration-change actor from the environment. Never raises."""
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
