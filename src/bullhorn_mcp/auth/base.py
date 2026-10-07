"""Abstract seam for Bullhorn authentication providers."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthProvider(Protocol):
    """Structural protocol describing the public surface an auth provider
    must expose to be usable by :class:`BullhornClient`.

    This does not change any behavior today - it documents the seam that
    ``BullhornAuth`` already satisfies, so that a future alternate auth
    implementation has a contract to implement against.
    """

    @property
    def session(self) -> object:
        """Return the current session, refreshing it if needed."""
        ...
