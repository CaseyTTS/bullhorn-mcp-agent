"""Renderer for the verified ``/query`` ``where`` syntax (Phase 5C, §3.3 step 4-5; HV-Q2).

This is the only module that assembles a Bullhorn query string for 5C. Its
input is a structured clause list built by ``schema/query_builder.py`` and the
activity derivers: field paths come from validated catalog / profile raw names
and the query-support allowlist, operators from a fixed table, and every
model-supplied value is a ``Literal`` rendered by ``literal()``, the single
escaping function for this syntax:

- ints: ``str(int)`` on an exact ``int`` (64-bit);
- booleans: ``true`` / ``false`` (HV-Q2 "Boolean values");
- timestamps: epoch milliseconds, an exact ``int`` (HV-Q2 "Datetime values: UNIX long millis", HV-Q5);
- strings: single-quoted (HV-Q2 example ``lastName = 'smith'``). No escape rule
  is documented, so a string must match ``STRING_RE``; anything else is
  ``UnsupportedValue`` and nothing is sent.

Only ``/search`` (Lucene) is unresolved (HV-Q3), and 5C does not use it, so no
Lucene renderer exists (Amendment C3-3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

STRING_RE = re.compile(r"[A-Za-z0-9 ._@-]{1,200}", re.ASCII)
PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", re.ASCII)
ENTITY_RE = re.compile(r"[A-Z][A-Za-z0-9]{0,63}", re.ASCII)
MAX_INT = 2**63 - 1

# Canonical / internal operator -> JPQL token (HV-Q2 "Simple comparisons", "IS [NOT] NULL", "[NOT] IN").
# ``is_not_null`` and ``not_in_or_null`` are internal only (never accepted from a caller).
COMPARISONS = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
LITERAL_KINDS = ("int", "bool", "ts", "str")


class UnsupportedValue(ValueError):
    """A value has no verified literal form (the request is not sent)."""


class RenderError(ValueError):
    """A clause is structurally invalid (a programming error upstream; nothing is sent)."""


@dataclass(frozen=True)
class Literal:
    kind: str  # int | bool | ts | str
    value: object


@dataclass(frozen=True)
class Predicate:
    path: str
    op: str  # eq gt gte lt lte in is_null is_not_null not_in_or_null
    values: tuple[Literal, ...] = ()


@dataclass(frozen=True)
class AnyOf:
    """``( p1 OR p2 ... )`` - used for the soft-delete term and internal composites."""

    predicates: tuple[Predicate, ...]


Clause = Predicate | AnyOf


def literal(lit: Literal) -> str:
    """The single escaping function of this syntax (§3.3 step 4)."""
    if lit.kind in ("int", "ts"):
        if type(lit.value) is not int or not -MAX_INT <= lit.value <= MAX_INT:
            raise UnsupportedValue("integer literal out of range")
        return str(lit.value)
    if lit.kind == "bool":
        if type(lit.value) is not bool:
            raise UnsupportedValue("boolean literal must be a bool")
        return "true" if lit.value else "false"
    if lit.kind == "str":
        if type(lit.value) is not str or STRING_RE.fullmatch(lit.value) is None:
            raise UnsupportedValue("string value outside the verified character set")
        return "'" + lit.value + "'"
    raise RenderError("unknown literal kind")


def _path(path: str) -> str:
    if type(path) is not str or PATH_RE.fullmatch(path) is None:
        raise RenderError("invalid field path")
    return path


def _predicate(p: Predicate) -> str:
    path = _path(p.path)
    if p.op in COMPARISONS:
        if len(p.values) != 1:
            raise RenderError("a comparison takes exactly one value")
        return f"{path} {COMPARISONS[p.op]} {literal(p.values[0])}"
    if p.op == "in":
        if not 1 <= len(p.values) <= 500:
            raise RenderError("IN takes 1 to 500 values")
        return f"{path} IN (" + ", ".join(literal(v) for v in p.values) + ")"
    if p.op == "not_in_or_null":
        if not 1 <= len(p.values) <= 500:
            raise RenderError("NOT IN takes 1 to 500 values")
        return f"({path} NOT IN (" + ", ".join(literal(v) for v in p.values) + f") OR {path} IS NULL)"
    if p.op == "is_null":
        if p.values:
            raise RenderError("IS NULL takes no value")
        return f"{path} IS NULL"
    if p.op == "is_not_null":
        if p.values:
            raise RenderError("IS NOT NULL takes no value")
        return f"{path} IS NOT NULL"
    raise RenderError("unknown operator")


def render_where(clauses: Sequence[Clause], max_chars: int) -> str:
    """``c1 AND c2 ...``. An empty list is a programming error (``where`` is required by /query)."""
    if not clauses:
        raise RenderError("at least one clause is required")
    parts: list[str] = []
    for clause in clauses:
        if isinstance(clause, AnyOf):
            if not clause.predicates:
                raise RenderError("empty OR group")
            parts.append("(" + " OR ".join(_predicate(p) for p in clause.predicates) + ")")
        elif isinstance(clause, Predicate):
            parts.append(_predicate(clause))
        else:
            raise RenderError("unknown clause")
    text = " AND ".join(parts)
    if len(text) > max_chars:
        raise UnsupportedValue(f"the rendered filter exceeds {max_chars} characters (HV-Q12)")
    return text


def render_fields(selection: Sequence[tuple[str, tuple[str, ...]]]) -> str:
    """``fields=``: ``name`` or ``name(sub,sub)`` (HV-Q13 nested to-one/composite sub-fields)."""
    out: list[str] = []
    for name, subs in selection:
        _path(name)
        if "." in name:
            raise RenderError("a field name is a single raw name")
        if subs:
            for s in subs:
                if "." in _path(s):
                    raise RenderError("a sub-field is a single raw name")
            out.append(f"{name}({','.join(subs)})")
        else:
            out.append(name)
    return ",".join(out)


def query_request(entity: str, where: str, fields: str, start: int, count: int) -> tuple[str, dict[str, str | int]]:
    """``(endpoint, params)`` with the fixed key set ``where, fields, count, start`` (§3.3 step 5; no ``orderBy``, HV-Q4)."""
    if type(entity) is not str or ENTITY_RE.fullmatch(entity) is None:
        raise RenderError("invalid entity name")
    if type(start) is not int or type(count) is not int or start < 0 or not 1 <= count <= 500:
        raise RenderError("invalid paging")
    return f"/query/{entity}", {"where": where, "fields": fields, "count": count, "start": start}
