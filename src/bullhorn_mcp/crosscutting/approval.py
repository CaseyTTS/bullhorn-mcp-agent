"""In-memory approval gate for pending write operations.

Operations that require approval (per ``permissions.check``) are parked here
as pending entries, keyed by an opaque token, until they are confirmed or
rejected. Phase 1 wires the gate in but nothing yet produces
``requires_approval=True`` decisions, so in practice this is exercised only
by tests and by code paths proven unreachable today.
"""

from uuid import uuid4


class ApprovalGate:
    """Tracks pending operations awaiting approval, in memory."""

    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}

    def create_pending(self, tool: str, payload: dict) -> str:
        """Record a pending operation and return a token identifying it."""
        token = uuid4().hex
        self._pending[token] = {"tool": tool, "payload": payload}
        return token

    def confirm(self, token: str) -> dict:
        """Pop and return the pending entry for ``token``.

        Raises:
            KeyError: if ``token`` does not identify a pending entry.
        """
        return self._pending.pop(token)

    def reject(self, token: str) -> None:
        """Remove the pending entry for ``token`` without executing it.

        Raises:
            KeyError: if ``token`` does not identify a pending entry.
        """
        del self._pending[token]

    def get(self, token: str) -> dict | None:
        """Return the pending entry for ``token`` without consuming it."""
        return self._pending.get(token)


# Default module-level instance, mirroring permissions.py's module-function
# style so callers don't need to manage an ApprovalGate instance themselves.
_default_gate = ApprovalGate()


def create_pending(tool: str, payload: dict) -> str:
    return _default_gate.create_pending(tool, payload)


def confirm(token: str) -> dict:
    return _default_gate.confirm(token)


def reject(token: str) -> None:
    _default_gate.reject(token)


def get(token: str) -> dict | None:
    return _default_gate.get(token)
