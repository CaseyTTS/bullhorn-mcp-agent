"""Bullhorn standard-field catalog and raw mapping targets.

This is one of the few modules allowed to know Bullhorn entity/field names
(see the Phase 3 work package, C-2). The names themselves live in
``bullhorn_standard_fields.yaml``; every one of them was verified against the
Bullhorn REST entity reference (see docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md).
"""

from __future__ import annotations

import functools
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Union

from .canonical_catalog import SUPPORTED_VERSIONS, CanonicalCatalog, load_canonical_catalog, read_packaged_text
from .errors import CatalogError, describe_value, path_segment, truncate_text
from .yaml_safe import YAML_PARSE_ERRORS, YamlParseFailure, safe_parse_yaml

# All of these are applied with ``fullmatch`` (``$`` would accept a trailing newline) and
# compiled with re.ASCII: identifiers have exactly one, ASCII-only definition (F-12).
RAW_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
BULLHORN_ENTITY_RE = re.compile(r"[A-Z][A-Za-z0-9]*", re.ASCII)
# A template is literal text, escaped braces, or exact {rawName} placeholders - nothing else.
TEMPLATE_RE = re.compile(r"(?:[^{}]|\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_]*\})*", re.ASCII)
# Catalog patterns are data, not code (F-13): an identifier literal, optionally containing
# exactly one ``\d+`` followed by an optional identifier suffix. Linear-time, ASCII-only.
PATTERN_GRAMMAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\\d\+[A-Za-z0-9_]*)?", re.ASCII)
MAX_PATTERN_CHARS = 64
MAX_PATTERNS = 64
# re.error plus the F-9 tuple: never Exception/BaseException.
_PATTERN_COMPILE_ERRORS: tuple[type[BaseException], ...] = (re.error, *YAML_PARSE_ERRORS)

_RESOURCE_NAME = "bullhorn_standard_fields.yaml"


# ---------------------------------------------------------------------- #
# Raw mapping targets (shared by the catalog and tenant profiles)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawField:
    """A plain raw field: ``firstName``."""

    name: str

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.name,)

    def to_data(self) -> Any:
        return self.name


@dataclass(frozen=True)
class NestedField:
    """A nested lookup on an association/composite: ``{field: owner, key: id}``."""

    field: str
    key: str

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.field,)

    def to_data(self) -> Any:
        return {"field": self.field, "key": self.key}


@dataclass(frozen=True)
class TemplateField:
    """A template built only from ``{rawName}`` placeholders."""

    template: str
    parts: tuple[tuple[str, str | None], ...]  # (literal, placeholder-or-None)

    @property
    def placeholders(self) -> tuple[str, ...]:
        seen: list[str] = []
        for _, name in self.parts:
            if name is not None and name not in seen:
                seen.append(name)
        return tuple(seen)

    @property
    def sources(self) -> tuple[str, ...]:
        return self.placeholders

    def to_data(self) -> Any:
        return self.template


RawTarget = Union[RawField, NestedField, TemplateField]


def is_raw_name(value: Any) -> bool:
    return isinstance(value, str) and RAW_NAME_RE.fullmatch(value) is not None


def parse_template(text: str) -> tuple[TemplateField | None, list[str]]:
    """Validate a template string; only literal text, ``{{``/``}}`` and ``{rawName}`` are allowed."""
    if not TEMPLATE_RE.fullmatch(text):
        return None, [
            f"template {describe_value(text)}: only literal text, '{{{{', '}}}}' and "
            "{rawName} placeholders are allowed"
        ]
    try:
        pieces = list(string.Formatter().parse(text))
    except ValueError as exc:  # pragma: no cover - TEMPLATE_RE already excludes malformed braces
        return None, [f"malformed template {describe_value(text)}: {truncate_text(str(exc))}"]
    parts: list[tuple[str, str | None]] = []
    for literal, field_name, format_spec, conversion in pieces:
        if field_name is None:
            parts.append((literal, None))
            continue
        if not is_raw_name(field_name) or format_spec or conversion is not None:  # pragma: no cover - defence in depth
            return None, [f"template {describe_value(text)}: placeholders must be exact {{rawName}} forms"]
        parts.append((literal, field_name))
    if not any(name is not None for _, name in parts):
        return None, [f"template {describe_value(text)}: contains no placeholders"]
    return TemplateField(template=text, parts=tuple(parts)), []


def parse_target(value: Any, where: str, *, allow_type: bool = False) -> tuple[RawTarget | None, Any, list[str]]:
    """Parse one mapping target.

    Returns ``(target, declared_type, errors)``. ``declared_type`` is only ever
    non-None when ``allow_type`` is true and a nested dict carried ``type``;
    it is returned unvalidated (the caller validates it).
    """
    if isinstance(value, str):
        if "{" in value or "}" in value:
            tmpl, errs = parse_template(value)
            return tmpl, None, [f"{where}: {e}" for e in errs]
        if is_raw_name(value):
            return RawField(value), None, []
        return None, None, [f"{where}: raw field name {describe_value(value)} must match {RAW_NAME_RE.pattern}"]
    if isinstance(value, dict):
        allowed = ("field", "key", "type") if allow_type else ("field", "key")
        errors: list[str] = []
        for k in value:
            if not isinstance(k, str) or k not in allowed:
                errors.append(f"{where}: unknown key {describe_value(k)} in nested mapping")
        fld, key = value.get("field"), value.get("key")
        for label, part in (("field", fld), ("key", key)):
            if not is_raw_name(part):
                errors.append(f"{where}: nested '{label}' must be a raw field name matching {RAW_NAME_RE.pattern}")
        if errors:
            return None, None, errors
        assert isinstance(fld, str) and isinstance(key, str)
        return NestedField(fld, key), value.get("type"), []
    return None, None, [f"{where}: target must be a raw name, a template string or a {{field, key}} mapping"]


# ---------------------------------------------------------------------- #
# Catalog
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class BullhornEntity:
    name: str
    canonical_entity: str
    standard_fields: tuple[str, ...]
    default_mappings: Mapping[str, RawTarget]


class BullhornCatalog:
    """Validated, read-only view of ``bullhorn_standard_fields.yaml``."""

    def __init__(
        self,
        version: int,
        entities: Mapping[str, BullhornEntity],
        custom_field_patterns: tuple[str, ...],
        sensitive_field_patterns: tuple[str, ...],
    ) -> None:
        self.version = version
        self._entities = MappingProxyType(dict(entities))
        self._by_canonical = MappingProxyType({e.canonical_entity: e for e in entities.values()})
        self.custom_field_patterns = custom_field_patterns
        self.sensitive_field_patterns = sensitive_field_patterns
        self._custom_res = _compile_pattern_list(custom_field_patterns, "custom_field_patterns")
        self._sensitive_res = _compile_pattern_list(sensitive_field_patterns, "sensitive_field_patterns")

    @property
    def entities(self) -> Mapping[str, BullhornEntity]:
        return self._entities

    def bullhorn_entity_for(self, canonical: str) -> str | None:
        if not isinstance(canonical, str):
            return None
        found = self._by_canonical.get(canonical)
        return found.name if found else None

    def canonical_entity_for(self, bullhorn: str) -> str | None:
        if not isinstance(bullhorn, str):
            return None
        found = self._entities.get(bullhorn)
        return found.canonical_entity if found else None

    def standard_fields(self, bullhorn: str) -> tuple[str, ...]:
        if not isinstance(bullhorn, str):
            return ()
        found = self._entities.get(bullhorn)
        return found.standard_fields if found else ()

    def is_custom_field(self, name: str) -> bool:
        # Gated on the single raw-name definition (F-12): never admits a name validation rejects.
        return is_raw_name(name) and any(r.fullmatch(name) for r in self._custom_res)

    def is_sensitive(self, name: str) -> bool:
        return isinstance(name, str) and any(r.fullmatch(name) for r in self._sensitive_res)

    def default_mappings(self, canonical_entity: str) -> Mapping[str, RawTarget]:
        found = self._by_canonical.get(canonical_entity) if isinstance(canonical_entity, str) else None
        return found.default_mappings if found else MappingProxyType({})

    # ------------------------------------------------------------------ #

    @classmethod
    def from_yaml_text(cls, text: str, canonical: CanonicalCatalog, source: str = "<yaml>") -> BullhornCatalog:
        try:
            data = safe_parse_yaml(text)
        except YamlParseFailure as exc:
            raise CatalogError([str(exc)], source) from exc
        return cls.from_dict(data, canonical, source)

    @classmethod
    def from_dict(cls, data: Any, canonical: CanonicalCatalog, source: str = "<dict>") -> BullhornCatalog:
        if not isinstance(data, dict):
            raise CatalogError(["top level must be a mapping"], source)
        errors: list[str] = []

        version: int | None = None
        if "version" not in data:
            errors.append("missing required key 'version'")
        else:
            v = data["version"]
            if isinstance(v, bool) or not isinstance(v, int) or v not in SUPPORTED_VERSIONS:
                errors.append(f"unsupported version {describe_value(v)} (supported: {sorted(SUPPORTED_VERSIONS)})")
            else:
                version = v

        allowed_top = ("version", "custom_field_patterns", "sensitive_field_patterns", "entities")
        for key in data:
            if not isinstance(key, str) or key not in allowed_top:
                errors.append(f"unknown top-level key: {describe_value(key)}")

        custom_patterns = _parse_patterns(data.get("custom_field_patterns"), "custom_field_patterns", errors)
        sensitive_patterns = _parse_patterns(data.get("sensitive_field_patterns"), "sensitive_field_patterns", errors)

        entities: dict[str, BullhornEntity] = {}
        raw_entities = data.get("entities")
        if not isinstance(raw_entities, dict) or not raw_entities:
            errors.append("'entities' must be a non-empty mapping")
        else:
            seen_canonical: dict[str, str] = {}
            for name, body in raw_entities.items():
                ent = _parse_bullhorn_entity(name, body, canonical, errors)
                if ent is None:
                    continue
                if ent.canonical_entity in seen_canonical:
                    errors.append(
                        f"entities.{path_segment(ent.name)}: canonical_entity {describe_value(ent.canonical_entity)} already bound to "
                        f"{describe_value(seen_canonical[ent.canonical_entity])}"
                    )
                    continue
                seen_canonical[ent.canonical_entity] = ent.name
                entities[ent.name] = ent

        if errors:
            raise CatalogError(errors, source)
        assert version is not None
        return cls(version, entities, custom_patterns, sensitive_patterns)


def compile_catalog_pattern(pat: Any) -> tuple[re.Pattern[str] | None, str | None]:
    """Check one catalog pattern against the fixed grammar, then compile it (F-13).

    Returns ``(compiled, None)`` or ``(None, error_message)``. This is the only
    place catalog patterns are compiled; both the validation and construction
    paths use it.
    """
    if not isinstance(pat, str):
        return None, f"pattern must be a string, got {describe_value(pat)}"
    if len(pat) > MAX_PATTERN_CHARS:
        return None, f"pattern {describe_value(pat)} is longer than {MAX_PATTERN_CHARS} characters"
    if not PATTERN_GRAMMAR_RE.fullmatch(pat):
        return None, f"pattern {describe_value(pat)} must be an identifier optionally containing one \\d+"
    try:
        return re.compile(pat, re.ASCII), None
    except _PATTERN_COMPILE_ERRORS as exc:  # defence in depth; the grammar should make this unreachable
        return None, f"pattern {describe_value(pat)} could not be compiled ({type(exc).__name__}): {truncate_text(str(exc))}"


def _compile_pattern_list(patterns: Any, where: str) -> tuple[re.Pattern[str], ...]:
    """Construction path: compile through the shared function; any problem is a CatalogError."""
    errors: list[str] = []
    compiled = _check_pattern_list(patterns, where, errors)
    if errors:
        raise CatalogError(errors, where)
    return compiled


def _check_pattern_list(value: Any, where: str, errors: list[str]) -> tuple[re.Pattern[str], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        errors.append(f"{where}: must be a list of patterns")
        return ()
    if len(value) > MAX_PATTERNS:
        errors.append(f"{where}: at most {MAX_PATTERNS} patterns are allowed, got {len(value)}")
        return ()
    out: list[re.Pattern[str]] = []
    for i, pat in enumerate(value):
        compiled, error = compile_catalog_pattern(pat)
        if error is not None or compiled is None:
            errors.append(f"{where}[{i}]: {error}")
            continue
        out.append(compiled)
    return tuple(out)


def _parse_patterns(value: Any, where: str, errors: list[str]) -> tuple[str, ...]:
    """Validation path: same shared check; returns the accepted pattern strings."""
    return tuple(c.pattern for c in _check_pattern_list(value, where, errors))


def _parse_bullhorn_entity(name: Any, body: Any, canonical: CanonicalCatalog, errors: list[str]) -> BullhornEntity | None:
    where = f"entities.{path_segment(name)}"
    ok = True
    if not isinstance(name, str) or not BULLHORN_ENTITY_RE.fullmatch(name):
        errors.append(f"{where}: entity name must match {BULLHORN_ENTITY_RE.pattern}")
        ok = False
    if not isinstance(body, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    for key in body:
        if not isinstance(key, str) or key not in ("canonical_entity", "standard_fields", "default_mappings"):
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False

    canon = body.get("canonical_entity")
    if not isinstance(canon, str) or not canonical.has_entity(canon):
        errors.append(f"{where}.canonical_entity: {describe_value(canon)} is not a canonical entity")
        return None

    std_raw = body.get("standard_fields")
    std: list[str] = []
    if not isinstance(std_raw, list) or not std_raw:
        errors.append(f"{where}.standard_fields: must be a non-empty list")
        ok = False
    else:
        for i, f in enumerate(std_raw):
            if not is_raw_name(f):
                errors.append(f"{where}.standard_fields[{i}]: {describe_value(f)} must match {RAW_NAME_RE.pattern}")
                ok = False
            elif f in std:
                errors.append(f"{where}.standard_fields: duplicate {describe_value(f)}")
                ok = False
            else:
                std.append(f)
    std_set = set(std)

    mappings: dict[str, RawTarget] = {}
    dm = body.get("default_mappings", {})
    if not isinstance(dm, dict):
        errors.append(f"{where}.default_mappings: must be a mapping")
        ok = False
        dm = {}
    for cname, target_value in dm.items():
        mwhere = f"{where}.default_mappings.{path_segment(cname)}"
        if not isinstance(cname, str) or not canonical.has_field(canon, cname):
            errors.append(f"{mwhere}: {describe_value(cname)} is not a field of canonical entity {describe_value(canon)}")
            ok = False
            continue
        target, _, terrs = parse_target(target_value, mwhere)
        if terrs or target is None:
            errors.extend(terrs)
            ok = False
            continue
        for src in target.sources:
            if src not in std_set:
                errors.append(f"{mwhere}: raw source {describe_value(src)} is not listed in standard_fields")
                ok = False
        mappings[cname] = target

    # Canonical ``id`` always maps to raw ``id`` (Phase 3 review triage, F-5).
    if isinstance(dm, dict) and dm.get("id") != "id":
        errors.append(f"{where}.default_mappings.id: canonical 'id' must map to exactly the raw field 'id'")
        ok = False

    if not ok:
        return None
    return BullhornEntity(
        name=str(name),
        canonical_entity=canon,
        standard_fields=tuple(std),
        default_mappings=MappingProxyType(mappings),
    )


@functools.lru_cache(maxsize=1)
def load_bullhorn_catalog() -> BullhornCatalog:
    """Load (once) and validate the packaged Bullhorn catalog. No network."""
    return BullhornCatalog.from_yaml_text(read_packaged_text(_RESOURCE_NAME), load_canonical_catalog(), _RESOURCE_NAME)
