"""The canonical (ATS-agnostic) catalog.

This module, like ``canonical_schema.yaml``, must never contain ATS-specific
entity or field names. It defines the shared vocabulary only.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import Any

from .errors import CatalogError, UnknownCanonicalFieldError, describe_value, path_segment
from .yaml_safe import YamlParseFailure, safe_parse_yaml

SUPPORTED_VERSIONS = frozenset({1})

VALID_TYPES = frozenset(
    {"id", "string", "text", "integer", "number", "boolean", "datetime", "date", "reference", "list"}
)

# Always applied with fullmatch (``$`` would accept a trailing newline).
KEY_RE = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)

_RESOURCE_PACKAGE = "bullhorn_mcp.mappings"
_RESOURCE_NAME = "canonical_schema.yaml"

_FIELD_KEYS = frozenset({"type", "required", "ref", "description"})
_ENTITY_KEYS = frozenset({"description", "fields"})


@dataclass(frozen=True)
class CanonicalField:
    """One field of a canonical entity."""

    name: str
    type: str
    required: bool = False
    ref: str | None = None
    description: str = ""


@dataclass(frozen=True)
class CanonicalEntity:
    """A canonical entity and its ordered fields."""

    name: str
    description: str
    fields: tuple[CanonicalField, ...]

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def get_field(self, name: str) -> CanonicalField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None


class CanonicalCatalog:
    """Validated, read-only view of ``canonical_schema.yaml``."""

    def __init__(self, version: int, entities: Mapping[str, CanonicalEntity]) -> None:
        self.version = version
        self._entities = MappingProxyType(dict(entities))

    @property
    def entities(self) -> Mapping[str, CanonicalEntity]:
        return self._entities

    def has_entity(self, name: str) -> bool:
        return isinstance(name, str) and name in self._entities

    def entity(self, name: str) -> CanonicalEntity:
        if not self.has_entity(name):
            raise UnknownCanonicalFieldError(f"Unknown canonical entity: {describe_value(name)}")
        return self._entities[name]

    def has_field(self, entity: str, name: str) -> bool:
        if not self.has_entity(entity):
            return False
        return self._entities[entity].get_field(name) is not None

    def field(self, entity: str, name: str) -> CanonicalField:
        found = self.entity(entity).get_field(name)
        if found is None:
            raise UnknownCanonicalFieldError(f"Unknown canonical field: {path_segment(entity)}.{path_segment(name)}")
        return found

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def from_yaml_text(cls, text: str, source: str = "<yaml>") -> CanonicalCatalog:
        try:
            data = safe_parse_yaml(text)
        except YamlParseFailure as exc:
            raise CatalogError([str(exc)], source) from exc
        return cls.from_dict(data, source)

    @classmethod
    def from_dict(cls, data: Any, source: str = "<dict>") -> CanonicalCatalog:
        errors: list[str] = []
        if not isinstance(data, dict):
            raise CatalogError(["top level must be a mapping"], source)

        version = _check_version(data, errors)

        for key in data:
            if key not in ("version", "entities"):
                errors.append(f"unknown top-level key: {describe_value(key)}")

        raw_entities = data.get("entities")
        entities: dict[str, CanonicalEntity] = {}
        if not isinstance(raw_entities, dict) or not raw_entities:
            errors.append("'entities' must be a non-empty mapping")
        else:
            for ent_name, ent_body in raw_entities.items():
                entity = _parse_entity(ent_name, ent_body, errors)
                if entity is not None:
                    entities[entity.name] = entity

        if errors:
            raise CatalogError(errors, source)
        assert version is not None
        return cls(version, entities)


def _check_version(data: dict[str, Any], errors: list[str]) -> int | None:
    if "version" not in data:
        errors.append("missing required key 'version'")
        return None
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
        errors.append(f"unsupported version {describe_value(version)} (supported: {sorted(SUPPORTED_VERSIONS)})")
        return None
    return version


def _parse_entity(name: Any, body: Any, errors: list[str]) -> CanonicalEntity | None:
    where = f"entities.{path_segment(name)}"
    ok = True
    if not isinstance(name, str) or not KEY_RE.fullmatch(name):
        errors.append(f"{where}: entity key must match {KEY_RE.pattern}")
        ok = False
    if not isinstance(body, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    for key in body:
        if not isinstance(key, str) or key not in _ENTITY_KEYS:
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False
    description = body.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{where}.description: must be a string")
        ok = False
    raw_fields = body.get("fields")
    fields: list[CanonicalField] = []
    if not isinstance(raw_fields, dict) or not raw_fields:
        errors.append(f"{where}.fields: must be a non-empty mapping")
        return None
    for field_name, field_body in raw_fields.items():
        parsed = _parse_field(where, field_name, field_body, errors)
        if parsed is None:
            ok = False
        else:
            fields.append(parsed)
    if not ok:
        return None
    return CanonicalEntity(name=str(name), description=str(description), fields=tuple(fields))


def _parse_field(where: str, name: Any, body: Any, errors: list[str]) -> CanonicalField | None:
    fwhere = f"{where}.fields.{path_segment(name)}"
    ok = True
    if not isinstance(name, str) or not KEY_RE.fullmatch(name):
        errors.append(f"{fwhere}: field key must match {KEY_RE.pattern}")
        ok = False
    if not isinstance(body, dict):
        errors.append(f"{fwhere}: must be a mapping")
        return None
    for key in body:
        if not isinstance(key, str) or key not in _FIELD_KEYS:
            errors.append(f"{fwhere}: unknown key {describe_value(key)}")
            ok = False
    ftype = body.get("type")
    if not isinstance(ftype, str):
        errors.append(f"{fwhere}: type must be a string, got {describe_value(ftype)}")
        ok = False
    elif ftype not in VALID_TYPES:
        errors.append(f"{fwhere}: unknown type {describe_value(ftype)} (allowed: {sorted(VALID_TYPES)})")
        ok = False
    required = body.get("required", False)
    if not isinstance(required, bool):
        errors.append(f"{fwhere}.required: must be a boolean")
        ok = False
    ref = body.get("ref")
    if ftype == "reference":
        if not isinstance(ref, str) or not KEY_RE.fullmatch(ref):
            errors.append(f"{fwhere}: type 'reference' requires 'ref' matching {KEY_RE.pattern}")
            ok = False
    elif ref is not None:
        errors.append(f"{fwhere}: 'ref' is only allowed with type 'reference'")
        ok = False
    description = body.get("description", "")
    if not isinstance(description, str):
        errors.append(f"{fwhere}.description: must be a string")
        ok = False
    if not ok:
        return None
    return CanonicalField(
        name=str(name), type=str(ftype), required=bool(required), ref=ref, description=description
    )


def read_packaged_text(name: str) -> str:
    """Read a packaged mapping resource via ``importlib.resources``."""
    return resources.files(_RESOURCE_PACKAGE).joinpath(name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def load_canonical_catalog() -> CanonicalCatalog:
    """Load (once) and validate the packaged canonical catalog. No network."""
    return CanonicalCatalog.from_yaml_text(read_packaged_text(_RESOURCE_NAME), _RESOURCE_NAME)
