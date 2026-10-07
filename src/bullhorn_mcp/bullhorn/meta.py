"""Entity metadata discovery over the existing ``BullhornClient.get_meta()``.

``MetaDiscovery`` calls ``client.get_meta(entity)`` exactly as it exists: no
extra arguments, no subclassing, no lower-level request calls. Picklist
``options`` are parsed only when already present in that response (fetching
them with extra request parameters is deferred to Phase 4, DEBT-3).

Malformed metadata never raises: every problem becomes a warning. API and
authentication errors from the client propagate to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

KNOWN_DATA_TYPES = frozenset(
    {
        "Integer",
        "BigDecimal",
        "Double",
        "String",
        "Boolean",
        "Timestamp",
        "byte[]",
        "Address",
        "Address1",
        "AddressWithoutCountry",
        "LoginRestrictions",
    }
)


@dataclass(frozen=True)
class FieldMeta:
    name: str
    label: str
    data_type: str | None
    field_type: str | None
    required: bool
    read_only: bool
    options: tuple[tuple[Any, Any], ...] | None
    associated_entity: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "data_type": self.data_type,
            "field_type": self.field_type,
            "required": self.required,
            "read_only": self.read_only,
            "options": None if self.options is None else [{"value": v, "label": lbl} for v, lbl in self.options],
            "associated_entity": self.associated_entity,
        }


@dataclass(frozen=True)
class EntityMeta:
    entity: str
    label: str
    fields: tuple[FieldMeta, ...]
    warnings: tuple[str, ...] = ()

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def get_field(self, name: str) -> FieldMeta | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None


@runtime_checkable
class MetaSource(Protocol):
    """Anything that can describe an entity's tenant fields."""

    def get_entity_meta(self, entity: str) -> EntityMeta: ...


class _MetaClient(Protocol):
    def get_meta(self, entity: str) -> dict[str, Any]: ...


class MetaDiscovery:
    """``MetaSource`` backed by ``BullhornClient.get_meta``; caches per entity."""

    def __init__(self, client: _MetaClient) -> None:
        self._client = client
        self._cache: dict[str, EntityMeta] = {}

    def get_entity_meta(self, entity: str) -> EntityMeta:
        cached = self._cache.get(entity)
        if cached is not None:
            return cached
        response = self._client.get_meta(entity)
        meta = parse_entity_meta(entity, response)
        self._cache[entity] = meta
        return meta

    def clear_cache(self) -> None:
        self._cache.clear()


def _as_bool(value: Any) -> bool:
    return value is True


def parse_entity_meta(entity: str, response: Any) -> EntityMeta:
    """Parse a ``/meta/{entity}`` response. Never raises on bad shapes."""
    warnings: list[str] = []
    if not isinstance(response, dict):
        warnings.append(f"meta response is not an object ({type(response).__name__}); no fields parsed")
        return EntityMeta(entity=entity, label=entity, fields=(), warnings=tuple(warnings))

    label = response.get("label")
    if not isinstance(label, str) or not label:
        label = entity

    raw_fields = response.get("fields")
    if raw_fields is None:
        warnings.append("meta response has no 'fields'")
        raw_fields = []
    elif not isinstance(raw_fields, list):
        warnings.append(f"meta 'fields' is not a list ({type(raw_fields).__name__})")
        raw_fields = []

    fields: list[FieldMeta] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_fields):
        if not isinstance(entry, dict):
            warnings.append(f"fields[{i}] is not an object ({type(entry).__name__}); skipped")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            warnings.append(f"fields[{i}] has no name; skipped")
            continue
        if name in seen:
            warnings.append(f"duplicate field {name!r} at fields[{i}]; first occurrence kept")
            continue
        seen.add(name)

        flabel = entry.get("label")
        if not isinstance(flabel, str) or not flabel:
            if flabel is None:
                warnings.append(f"field {name!r} has a null label; using the name")
            elif "label" in entry:
                warnings.append(f"field {name!r} has an invalid label; using the name")
            flabel = name

        data_type = entry.get("dataType")
        if data_type is not None and not isinstance(data_type, str):
            warnings.append(f"field {name!r} has a non-string dataType; kept as text")
            data_type = str(data_type)
        if isinstance(data_type, str) and data_type not in KNOWN_DATA_TYPES:
            warnings.append(f"field {name!r} has unknown dataType {data_type!r}; kept as-is")

        field_type = entry.get("type")
        if field_type is not None and not isinstance(field_type, str):
            warnings.append(f"field {name!r} has a non-string type; ignored")
            field_type = None

        read_only_raw = entry.get("readOnly", entry.get("readonly"))
        options = _parse_options(name, entry.get("options"), warnings) if "options" in entry else None

        assoc = entry.get("associatedEntity")
        associated: str | None = None
        if isinstance(assoc, dict) and isinstance(assoc.get("entity"), str):
            associated = assoc["entity"]
        elif isinstance(assoc, str):
            associated = assoc

        fields.append(
            FieldMeta(
                name=name,
                label=flabel,
                data_type=data_type,
                field_type=field_type,
                required=_as_bool(entry.get("required")),
                read_only=_as_bool(read_only_raw),
                options=options,
                associated_entity=associated,
            )
        )

    return EntityMeta(entity=entity, label=label, fields=tuple(fields), warnings=tuple(warnings))


def _parse_options(name: str, raw: Any, warnings: list[str]) -> tuple[tuple[Any, Any], ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        warnings.append(f"field {name!r} has malformed options; ignored")
        return None
    out: list[tuple[Any, Any]] = []
    for opt in raw:
        if not isinstance(opt, dict) or "value" not in opt:
            warnings.append(f"field {name!r} has malformed options; ignored")
            return None
        value = opt["value"]
        label = opt.get("label", value)
        if isinstance(value, (dict, list)) or isinstance(label, (dict, list)):
            warnings.append(f"field {name!r} has malformed options; ignored")
            return None
        out.append((value, label))
    return tuple(out)
