"""The single YAML parse choke point for the schema layer (Phase 3 review triage, F-9).

Every YAML document the schema layer reads goes through ``safe_parse_yaml``.
It uses the PyYAML safe loader only (C-5) and converts every failure the safe
loader can raise on hostile input into one bounded ``YamlParseFailure``.
Callers convert that into their documented error (``ProfileError`` /
``CatalogError``).

The caught tuple is explicit and deliberately excludes ``Exception`` /
``BaseException``: ``MemoryError``, ``KeyboardInterrupt`` and ``SystemExit``
always propagate.
"""

from __future__ import annotations

from typing import Any

import yaml

from .errors import SchemaError, truncate_text

YAML_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    yaml.YAMLError,
    ValueError,  # includes UnicodeError and the int-digit limit
    RecursionError,
    LookupError,  # KeyError, IndexError
    AttributeError,
    TypeError,
    ArithmeticError,  # OverflowError, ZeroDivisionError
)


class YamlParseFailure(SchemaError):
    """Internal: YAML text could not be parsed. Carries a bounded message."""


def _message(exc: BaseException) -> str:
    if isinstance(exc, RecursionError):
        return "YAML nesting too deep"
    return f"YAML parse error ({type(exc).__name__}): {truncate_text(str(exc))}"


def safe_parse_yaml(text: str) -> Any:
    """Parse ``text`` with the PyYAML safe loader; any parse failure becomes ``YamlParseFailure``."""
    try:
        return yaml.safe_load(text)
    except YAML_PARSE_ERRORS as exc:
        raise YamlParseFailure(_message(exc)) from exc
