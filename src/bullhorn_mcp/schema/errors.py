"""Error hierarchy for the canonical schema & mapping layer.

All messages built from input values go through ``describe_value`` /
``path_segment`` so that hostile input (for example a YAML alias bomb) can
never produce an unbounded message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_VALUE_CHARS = 80
MAX_ERROR_LINES = 100

_PLAIN_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


def describe_value(value: object) -> str:
    """Bounded, safe description of an arbitrary (possibly hostile) value.

    Scalars (``str``, ``int``, ``float``, ``bool``, ``None``) are shown with
    ``repr`` truncated to at most ``MAX_VALUE_CHARS`` characters. Anything else
    is shown only by type name; containers are never converted or iterated.
    """
    if value is None or isinstance(value, (str, bool, float)) or type(value) is int:
        if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 256:
            return "<int>"
        # Slice strings before repr so a huge string is never fully copied.
        text = repr(value[: MAX_VALUE_CHARS + 1]) if isinstance(value, str) else repr(value)
        if len(text) > MAX_VALUE_CHARS or (isinstance(value, str) and len(value) > MAX_VALUE_CHARS):
            text = text[: MAX_VALUE_CHARS - 3] + "..."
        return text
    return f"<{type(value).__name__}>"


def path_segment(value: object) -> str:
    """A key as it should appear in an error path: plain identifiers verbatim, else described."""
    if isinstance(value, str) and len(value) <= MAX_VALUE_CHARS and _PLAIN_SEGMENT.fullmatch(value):
        return value
    return describe_value(value)


def truncate_text(text: str, limit: int = 300) -> str:
    """Bound free text (for example a parser's own error message)."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cap_errors(errors: Iterable[str], limit: int = MAX_ERROR_LINES) -> tuple[str, ...]:
    """At most ``limit`` lines, then one ``... and N more`` line."""
    items = tuple(errors)
    if len(items) <= limit:
        return items
    return items[:limit] + (f"... and {len(items) - limit} more",)


class SchemaError(Exception):
    """Base class for every error raised by ``bullhorn_mcp.schema``."""


class _AggregatedError(SchemaError):
    """An error that carries every validation problem found, not just the first.

    ``errors`` holds every problem; the message lists at most
    ``MAX_ERROR_LINES`` of them.
    """

    kind = "validation"

    def __init__(self, errors: Iterable[str], source: str = "<dict>") -> None:
        self.errors: tuple[str, ...] = tuple(truncate_text(e, 1000) for e in errors)
        self.source = truncate_text(source, 300)
        count = len(self.errors)
        noun = "problem" if count == 1 else "problems"
        details = "\n".join(f"  - {e}" for e in cap_errors(self.errors))
        super().__init__(f"Invalid {self.kind} {self.source} ({count} {noun}):\n{details}")


class CatalogError(_AggregatedError):
    """A packaged catalog (canonical or Bullhorn) failed validation."""

    kind = "catalog"


class ProfileError(_AggregatedError):
    """A tenant mapping profile failed validation."""

    kind = "mapping profile"


class UnknownCanonicalFieldError(SchemaError):
    """A caller asked for a canonical entity or field name that does not exist."""
