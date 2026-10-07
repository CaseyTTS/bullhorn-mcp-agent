"""The HV-verified query-support resource (Phase 5C, D-5C-3).

``mappings/bullhorn_query_support.yaml`` is the only place where 5C records
Bullhorn query mechanics. It is loaded through ``importlib.resources`` and
validated strictly: unknown keys, a missing HV ID, an unknown operator or an
unknown operation all fail the load (``SupportError``).

``QuerySupport.from_dict`` is also how tests mark an HV item unresolved: a
modified copy of the packaged document is validated by the same code.
"""

from __future__ import annotations

import copy
import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..schema.bullhorn_catalog import BULLHORN_ENTITY_RE, RAW_NAME_RE
from ..schema.canonical_catalog import read_packaged_text
from ..schema.errors import CatalogError, describe_value, path_segment
from ..schema.yaml_safe import YamlParseFailure
from ..tenant import yaml_strict

RESOURCE = "bullhorn_query_support.yaml"
OPERATORS = ("eq", "in", "gt", "gte", "lt", "lte", "is_null")
OPERATIONS = ("query", "search")
HV_RE = re.compile(r"HV-Q[0-9]{1,2}[a-z]?", re.ASCII)
PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", re.ASCII)

_TOP = {"version", "syntax", "entities", "history_sources"}
_SYNTAX = {
    "hv", "string_escape_verified", "prefix_match_verified", "float_literal_verified", "association_paths_verified",
    "order_by_direction_verified", "max_where_chars", "max_fields", "nested_fields_verified",
}
_ENTITY = {
    "canonical_entity", "operation", "hv", "paging", "soft_delete", "sortable", "custom_fields", "filterable",
    "instance_filter", "recurrence_single_record",
}


class SupportError(CatalogError):
    """The query-support resource is invalid."""

    kind = "query support"


@dataclass(frozen=True)
class SyntaxSupport:
    hv: str
    string_escape_verified: bool
    prefix_match_verified: bool
    float_literal_verified: bool
    association_paths_verified: bool
    order_by_direction_verified: bool
    max_where_chars: int
    max_fields: int
    nested_fields_verified: bool


@dataclass(frozen=True)
class EntitySupport:
    name: str  # Bullhorn entity name
    canonical_entity: str
    operation: str
    hv: str
    max_page_size: int
    soft_delete_field: str | None
    soft_delete_nullable: bool
    soft_delete_resolved: bool
    filterable: Mapping[str, frozenset[str]]
    custom_ops: frozenset[str] | None
    sortable: Mapping[str, str] = field(default_factory=dict)
    instance_filter: tuple[str, str] | None = None
    recurrence_single_record: bool = False

    def ops_for(self, raw_path: str, is_custom: bool) -> frozenset[str] | None:
        if raw_path in self.filterable:
            return self.filterable[raw_path]
        if is_custom and self.custom_ops is not None:
            return self.custom_ops
        return None


@dataclass(frozen=True)
class HistorySource:
    name: str
    entity: str | None
    verified: bool
    reason: str
    hv: str


@dataclass(frozen=True)
class QuerySupport:
    version: int
    syntax: SyntaxSupport
    entities: Mapping[str, EntitySupport]
    history: Mapping[str, HistorySource]

    def for_canonical(self, canonical: str) -> EntitySupport | None:
        for ent in self.entities.values():
            if ent.canonical_entity == canonical:
                return ent
        return None

    @classmethod
    def from_dict(cls, data: Any, source: str = "<dict>") -> QuerySupport:
        errors: list[str] = []
        if not isinstance(data, dict):
            raise SupportError(["top level must be a mapping"], source)
        for key in data:
            if key not in _TOP:
                errors.append(f"unknown top-level key {describe_value(key)}")
        if data.get("version") != 1 or isinstance(data.get("version"), bool):
            errors.append(f"unsupported version {describe_value(data.get('version'))}")
        syntax = _syntax(data.get("syntax"), errors)
        entities: dict[str, EntitySupport] = {}
        raw_entities = data.get("entities")
        if not isinstance(raw_entities, dict) or not raw_entities:
            errors.append("entities: must be a non-empty mapping")
        else:
            for name, body in raw_entities.items():
                ent = _entity(name, body, errors)
                if ent is not None:
                    entities[ent.name] = ent
        history: dict[str, HistorySource] = {}
        raw_history = data.get("history_sources", {})
        if not isinstance(raw_history, dict):
            errors.append("history_sources: must be a mapping")
        else:
            for name, body in raw_history.items():
                src = _history(name, body, errors)
                if src is not None:
                    history[src.name] = src
        if errors or syntax is None:
            raise SupportError(errors or ["invalid syntax section"], source)
        return cls(1, syntax, MappingProxyType(entities), MappingProxyType(history))


def _hv(where: str, value: Any, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not HV_RE.fullmatch(value):
        errors.append(f"{where}: an HV ID (HV-Q<n>) is required, got {describe_value(value)}")
        return None
    return value


def _keys(where: str, body: dict[str, Any], allowed: set[str], errors: list[str]) -> None:
    for key in body:
        if key not in allowed:
            errors.append(f"{where}: unknown key {describe_value(key)}")


def _bool(where: str, value: Any, errors: list[str]) -> bool:
    if not isinstance(value, bool):
        errors.append(f"{where}: must be a boolean")
        return False
    return value


def _int(where: str, value: Any, lo: int, hi: int, errors: list[str]) -> int:
    if type(value) is not int or not lo <= value <= hi:
        errors.append(f"{where}: must be an int from {lo} to {hi}")
        return lo
    return value


def _ops(where: str, value: Any, errors: list[str]) -> frozenset[str] | None:
    if not isinstance(value, list) or not value or any(not isinstance(o, str) or o not in OPERATORS for o in value):
        errors.append(f"{where}: ops must be a non-empty list of {list(OPERATORS)}")
        return None
    return frozenset(value)


def _syntax(raw: Any, errors: list[str]) -> SyntaxSupport | None:
    body = raw.get("query") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or set(raw) != {"query"} or not isinstance(body, dict):
        errors.append("syntax: must be a mapping with exactly 'query'")
        return None
    _keys("syntax.query", body, _SYNTAX, errors)
    for key in _SYNTAX:
        if key not in body:
            errors.append(f"syntax.query: missing {key!r}")
    if any(e.startswith("syntax.query") for e in errors):
        return None
    hv = _hv("syntax.query.hv", body["hv"], errors)
    return SyntaxSupport(
        hv=hv or "",
        string_escape_verified=_bool("syntax.query.string_escape_verified", body["string_escape_verified"], errors),
        prefix_match_verified=_bool("syntax.query.prefix_match_verified", body["prefix_match_verified"], errors),
        float_literal_verified=_bool("syntax.query.float_literal_verified", body["float_literal_verified"], errors),
        association_paths_verified=_bool("syntax.query.association_paths_verified", body["association_paths_verified"], errors),
        order_by_direction_verified=_bool("syntax.query.order_by_direction_verified", body["order_by_direction_verified"], errors),
        max_where_chars=_int("syntax.query.max_where_chars", body["max_where_chars"], 1, 7500, errors),
        max_fields=_int("syntax.query.max_fields", body["max_fields"], 1, 100, errors),
        nested_fields_verified=_bool("syntax.query.nested_fields_verified", body["nested_fields_verified"], errors),
    )


def _entity(name: Any, body: Any, errors: list[str]) -> EntitySupport | None:
    where = f"entities.{path_segment(name)}"
    if not isinstance(name, str) or not BULLHORN_ENTITY_RE.fullmatch(name):
        errors.append(f"{where}: entity name must match {BULLHORN_ENTITY_RE.pattern}")
        return None
    if not isinstance(body, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    before = len(errors)
    _keys(where, body, _ENTITY, errors)
    hv = _hv(f"{where}.hv", body.get("hv"), errors)
    canonical = body.get("canonical_entity")
    if not isinstance(canonical, str) or not canonical:
        errors.append(f"{where}.canonical_entity: required")
    operation = body.get("operation")
    if operation not in OPERATIONS:
        errors.append(f"{where}.operation: must be one of {list(OPERATIONS)}")
    paging = body.get("paging")
    max_page = 1
    if not isinstance(paging, dict) or set(paging) != {"mode", "max_page_size", "total_in_response", "hv"}:
        errors.append(f"{where}.paging: must have exactly mode, max_page_size, total_in_response and hv")
    else:
        _hv(f"{where}.paging.hv", paging.get("hv"), errors)
        if paging.get("mode") not in ("offset", "keyset"):
            errors.append(f"{where}.paging.mode: must be 'offset' or 'keyset'")
        max_page = _int(f"{where}.paging.max_page_size", paging.get("max_page_size"), 1, 500, errors)
        _bool(f"{where}.paging.total_in_response", paging.get("total_in_response"), errors)
    soft = body.get("soft_delete")
    soft_field: str | None = None
    nullable = False
    resolved = False
    if not isinstance(soft, dict):
        errors.append(f"{where}.soft_delete: must be a mapping")
    else:
        _hv(f"{where}.soft_delete.hv", soft.get("hv"), errors)
        if "unresolved" in soft:  # an entity whose soft-delete semantics are not verified (HV-Q6 guard)
            if set(soft) != {"unresolved", "reason", "hv"} or soft.get("unresolved") is not True:
                errors.append(f"{where}.soft_delete: an unresolved entry needs exactly unresolved: true, reason and hv")
        elif soft.get("field") is None:
            if set(soft) != {"field", "reason", "hv"} or not isinstance(soft.get("reason"), str):
                errors.append(f"{where}.soft_delete: a non-soft-deletable entity needs exactly field: null, reason and hv")
            else:
                resolved = True
        else:
            field_name = soft.get("field")
            if set(soft) != {"field", "nullable", "hv"} or not isinstance(field_name, str) or not RAW_NAME_RE.fullmatch(field_name):
                errors.append(f"{where}.soft_delete: needs exactly field (a raw name), nullable and hv")
            else:
                soft_field = soft["field"]
                nullable = _bool(f"{where}.soft_delete.nullable", soft.get("nullable"), errors)
                resolved = True
    sortable = body.get("sortable")
    if sortable != {}:
        if not isinstance(sortable, dict):
            errors.append(f"{where}.sortable: must be a mapping")
    filterable: dict[str, frozenset[str]] = {}
    raw_f = body.get("filterable")
    if not isinstance(raw_f, dict) or not raw_f:
        errors.append(f"{where}.filterable: must be a non-empty mapping")
    else:
        for path, spec in raw_f.items():
            fwhere = f"{where}.filterable.{path_segment(path)}"
            if not isinstance(path, str) or not PATH_RE.fullmatch(path):
                errors.append(f"{fwhere}: must be a raw field name or a to-one path")
                continue
            if not isinstance(spec, dict) or set(spec) != {"ops", "hv"}:
                errors.append(f"{fwhere}: must have exactly ops and hv")
                continue
            if _hv(f"{fwhere}.hv", spec.get("hv"), errors) is None:
                continue
            ops = _ops(fwhere, spec.get("ops"), errors)
            if ops is not None:
                filterable[path] = ops
    custom_ops: frozenset[str] | None = None
    custom = body.get("custom_fields")
    if custom is not None:
        if not isinstance(custom, dict) or set(custom) != {"ops", "hv"}:
            errors.append(f"{where}.custom_fields: must have exactly ops and hv")
        elif _hv(f"{where}.custom_fields.hv", custom.get("hv"), errors) is not None:
            custom_ops = _ops(f"{where}.custom_fields", custom.get("ops"), errors)
    instance: tuple[str, str] | None = None
    raw_inst = body.get("instance_filter")
    if raw_inst is not None:
        if (
            not isinstance(raw_inst, dict)
            or set(raw_inst) != {"field", "op", "hv"}
            or not isinstance(raw_inst.get("field"), str)
            or not RAW_NAME_RE.fullmatch(raw_inst["field"])
            or raw_inst.get("op") != "is_null"
        ):
            errors.append(f"{where}.instance_filter: needs exactly field (a raw name), op: is_null and hv")
        elif _hv(f"{where}.instance_filter.hv", raw_inst.get("hv"), errors) is not None:
            instance = (raw_inst["field"], "is_null")
    recurrence = False
    raw_rec = body.get("recurrence_single_record")
    if raw_rec is not None:
        if not isinstance(raw_rec, dict) or set(raw_rec) != {"value", "hv"}:
            errors.append(f"{where}.recurrence_single_record: needs exactly value and hv")
        elif _hv(f"{where}.recurrence_single_record.hv", raw_rec.get("hv"), errors) is not None:
            recurrence = _bool(f"{where}.recurrence_single_record.value", raw_rec.get("value"), errors)
    if len(errors) > before or hv is None:
        return None
    return EntitySupport(
        name=name,
        canonical_entity=str(canonical),
        operation=str(operation),
        hv=hv,
        max_page_size=max_page,
        soft_delete_field=soft_field,
        soft_delete_nullable=nullable,
        soft_delete_resolved=resolved,
        filterable=MappingProxyType(filterable),
        custom_ops=custom_ops,
        sortable=MappingProxyType({}),
        instance_filter=instance,
        recurrence_single_record=recurrence,
    )


def _history(name: Any, body: Any, errors: list[str]) -> HistorySource | None:
    where = f"history_sources.{path_segment(name)}"
    if not isinstance(name, str) or not isinstance(body, dict) or set(body) != {"entity", "verified", "reason", "hv"}:
        errors.append(f"{where}: needs exactly entity, verified, reason and hv")
        return None
    hv = _hv(f"{where}.hv", body.get("hv"), errors)
    entity = body.get("entity")
    if entity is not None and (not isinstance(entity, str) or not BULLHORN_ENTITY_RE.fullmatch(entity)):
        errors.append(f"{where}.entity: must be null or a Bullhorn entity name")
        return None
    verified = body.get("verified")
    if not isinstance(verified, bool) or not isinstance(body.get("reason"), str) or hv is None:
        errors.append(f"{where}: verified must be a boolean and reason a string")
        return None
    return HistorySource(name=name, entity=entity, verified=verified, reason=body["reason"], hv=hv)


def packaged_document() -> dict[str, Any]:
    """A fresh copy of the parsed packaged document (tests modify copies to mark HV items unresolved)."""
    return copy.deepcopy(_parsed())


@functools.lru_cache(maxsize=1)
def _parsed() -> dict[str, Any]:
    try:
        data = yaml_strict.parse(read_packaged_text(RESOURCE))
    except YamlParseFailure as exc:
        raise SupportError([str(exc)], RESOURCE) from exc
    if not isinstance(data, dict):
        raise SupportError(["top level must be a mapping"], RESOURCE)
    return data


@functools.lru_cache(maxsize=1)
def load_query_support() -> QuerySupport:
    """Load (once) and validate the packaged query-support resource. No network."""
    return QuerySupport.from_dict(packaged_document(), RESOURCE)
