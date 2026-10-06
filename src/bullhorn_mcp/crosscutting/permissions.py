"""Permission checks for Bullhorn MCP tool invocations.

Phase 1 ships this as a hardcoded default-allow pass-through: every tool and
operation is permitted and never requires approval. The signature is
deliberately stable so a later phase can swap in real, config-driven policy
logic without changing this function's signature or any of its callers.
"""

from dataclasses import dataclass


@dataclass
class PermissionDecision:
    """The result of a permission check for a tool invocation."""

    allowed: bool
    requires_approval: bool
    reason: str | None = None


def check(tool: str, operation: str = "read") -> PermissionDecision:
    """Check whether a tool invocation is permitted.

    Phase 1: always returns an allow decision that does not require
    approval, regardless of ``tool`` or ``operation``.
    """
    return PermissionDecision(allowed=True, requires_approval=False)
