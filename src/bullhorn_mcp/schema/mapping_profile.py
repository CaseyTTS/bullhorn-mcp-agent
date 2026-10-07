"""Tenant mapping profiles: load, validate, save.

A profile lives outside the package, at the path in ``BULLHORN_MAPPING_PROFILE``.
Loading never calls Bullhorn. Validation is aggregated: one ``ProfileError``
lists every problem found.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .bullhorn_catalog import RAW_NAME_RE, RawTarget, is_raw_name, parse_target
from .canonical_catalog import KEY_RE, SUPPORTED_VERSIONS, VALID_TYPES, CanonicalCatalog, load_canonical_catalog
from .errors import ProfileError, cap_errors, describe_value, path_segment, truncate_text
from .yaml_safe import YAML_PARSE_ERRORS, YamlParseFailure, safe_parse_yaml

logger = logging.getLogger("bullhorn_mcp.schema")

PROFILE_ENV_VAR = "BULLHORN_MAPPING_PROFILE"

# ``reference`` needs a ``ref`` target entity, which custom entries cannot carry.
CUSTOM_TYPES = frozenset(VALID_TYPES - {"reference"})

_TOP_KEYS = frozenset({"version", "tenant", "generated_at", "entities"})
_ENTITY_KEYS = frozenset({"standard", "custom", "unmapped_bullhorn_fields"})
_UNMAPPED_KEYS = frozenset({"field", "label", "data_type", "field_type", "options", "required", "read_only"})


@dataclass(frozen=True)
class CustomMapping:
    """A tenant-added canonical name and where it comes from."""

    target: RawTarget
    type: str = "string"

    def to_data(self) -> Any:
        data = self.target.to_data()
        if isinstance(data, dict) and self.type != "string":
            return {**data, "type": self.type}
        return data


@dataclass(frozen=True)
class UnmappedField:
    """A tenant Bullhorn field noticed during discovery but not (yet) mapped."""

    field: str
    label: str | None = None
    data_type: str | None = None
    field_type: str | None = None
    options: tuple[tuple[Any, Any], ...] | None = None
    required: bool | None = None
    read_only: bool | None = None

    def to_data(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field": self.field,
            "label": self.label,
            "data_type": self.data_type,
            "field_type": self.field_type,
        }
        if self.options is not None:
            out["options"] = [{"value": v, "label": lbl} for v, lbl in self.options]
        if self.required is not None:
            out["required"] = self.required
        if self.read_only is not None:
            out["read_only"] = self.read_only
        return out


@dataclass(frozen=True)
class EntityProfile:
    standard: Mapping[str, RawTarget] = field(default_factory=dict)
    custom: Mapping[str, CustomMapping] = field(default_factory=dict)
    unmapped_bullhorn_fields: tuple[UnmappedField, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "standard": {k: v.to_data() for k, v in self.standard.items()},
            "custom": {k: v.to_data() for k, v in self.custom.items()},
            "unmapped_bullhorn_fields": [u.to_data() for u in self.unmapped_bullhorn_fields],
        }


@dataclass(frozen=True)
class MappingProfile:
    """A validated tenant mapping profile."""

    version: int = 1
    tenant: str | None = None
    generated_at: str | None = None
    entities: Mapping[str, EntityProfile] = field(default_factory=dict)

    def entity(self, name: str) -> EntityProfile | None:
        return self.entities.get(name)

    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tenant": self.tenant,
            "generated_at": self.generated_at,
            "entities": {name: ep.to_data() for name, ep in self.entities.items()},
        }

    @classmethod
    def from_dict(cls, data: Any, canonical: CanonicalCatalog | None = None, source: str = "<dict>") -> MappingProfile:
        canonical = canonical or load_canonical_catalog()
        if not isinstance(data, dict):
            raise ProfileError(["top level must be a mapping"], source)
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

        for key in data:
            if not isinstance(key, str) or key not in _TOP_KEYS:
                errors.append(f"unknown top-level key: {describe_value(key)}")

        tenant = data.get("tenant")
        if tenant is not None and not isinstance(tenant, str):
            errors.append("tenant: must be a string")
            tenant = None

        generated_at = data.get("generated_at")
        if isinstance(generated_at, (_dt.datetime, _dt.date)):
            generated_at = generated_at.isoformat()
        elif generated_at is not None and not isinstance(generated_at, str):
            errors.append("generated_at: must be a string or timestamp")
            generated_at = None

        entities: dict[str, EntityProfile] = {}
        raw_entities = data.get("entities", {})
        if raw_entities is None:
            raw_entities = {}
        if not isinstance(raw_entities, dict):
            errors.append("entities: must be a mapping")
        else:
            for name, body in raw_entities.items():
                ep = _parse_entity_profile(name, body, canonical, errors)
                if ep is not None and isinstance(name, str):
                    entities[name] = ep

        if errors:
            raise ProfileError(errors, source)
        assert version is not None
        return cls(version=version, tenant=tenant, generated_at=generated_at, entities=entities)

    @classmethod
    def load(cls, path: str | os.PathLike[str], canonical: CanonicalCatalog | None = None) -> MappingProfile:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileError([f"file is not valid UTF-8: {truncate_text(str(exc))}"], str(p)) from exc
        try:
            data = safe_parse_yaml(text)
        except YamlParseFailure as exc:
            raise ProfileError([str(exc)], str(p)) from exc
        return cls.from_dict(data, canonical, source=str(p))

    def to_yaml_text(self) -> str:
        """Serialize to YAML and prove the text round-trips to an equal profile (F-11).

        Non-ASCII and control characters are written as YAML escapes. The text is
        re-parsed and rebuilt before it is returned; a profile that cannot be
        serialized losslessly raises ``ProfileError`` and is never written.
        This is the only YAML writer for profiles.
        """
        try:
            text = yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=False, default_flow_style=False)
        except YAML_PARSE_ERRORS as exc:
            raise ProfileError(
                [f"profile cannot be serialized ({type(exc).__name__}): {truncate_text(str(exc))}"], "<serialize>"
            ) from exc
        try:
            reparsed = MappingProfile.from_dict(safe_parse_yaml(text), source="<serialize self-check>")
        except (YamlParseFailure, ProfileError) as exc:
            raise ProfileError(["profile cannot be serialized losslessly"], "<serialize>") from exc
        if reparsed != self:
            raise ProfileError(["profile cannot be serialized losslessly"], "<serialize>")
        return text

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write atomically: temp file in the same directory, then ``os.replace``.

        The text is produced and verified first (``to_yaml_text``); if that fails,
        no temp file is created and the target is not touched.
        """
        p = Path(path)
        text = self.to_yaml_text()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, p)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _parse_entity_profile(name: Any, body: Any, canonical: CanonicalCatalog, errors: list[str]) -> EntityProfile | None:
    where = f"entities.{path_segment(name)}"
    if not isinstance(name, str) or not canonical.has_entity(name):
        errors.append(f"{where}: {describe_value(name)} is not a canonical entity")
        return None
    if body is None:
        body = {}
    if not isinstance(body, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    ok = True
    for key in body:
        if not isinstance(key, str) or key not in _ENTITY_KEYS:
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False

    canonical_fields = set(canonical.entity(name).field_names)

    standard: dict[str, RawTarget] = {}
    raw_std = body.get("standard")
    if raw_std is None:
        raw_std = {}
    if not isinstance(raw_std, dict):
        errors.append(f"{where}.standard: must be a mapping")
        ok = False
        raw_std = {}
    for cname, value in raw_std.items():
        swhere = f"{where}.standard.{path_segment(cname)}"
        if not isinstance(cname, str) or cname not in canonical_fields:
            errors.append(f"{swhere}: {describe_value(cname)} is not a canonical field of {describe_value(name)}")
            ok = False
            continue
        if cname == "id":
            # Canonical ``id`` always maps to raw ``id`` (Phase 3 review triage, F-5).
            errors.append(f"{swhere}: canonical 'id' of entity {describe_value(name)} always maps to raw 'id' and cannot be overridden")
            ok = False
            continue
        target, _, terrs = parse_target(value, swhere)
        if terrs or target is None:
            errors.extend(terrs)
            ok = False
            continue
        standard[cname] = target

    custom: dict[str, CustomMapping] = {}
    raw_custom = body.get("custom")
    if raw_custom is None:
        raw_custom = {}
    if not isinstance(raw_custom, dict):
        errors.append(f"{where}.custom: must be a mapping")
        ok = False
        raw_custom = {}
    for cname, value in raw_custom.items():
        cwhere = f"{where}.custom.{path_segment(cname)}"
        if not isinstance(cname, str) or not KEY_RE.fullmatch(cname):
            errors.append(f"{cwhere}: custom name must match {KEY_RE.pattern}")
            ok = False
            continue
        if cname in canonical_fields:
            errors.append(f"{cwhere}: custom name collides with canonical field {path_segment(name)}.{path_segment(cname)}")
            ok = False
            continue
        target, declared_type, terrs = parse_target(value, cwhere, allow_type=True)
        if terrs or target is None:
            errors.extend(terrs)
            ok = False
            continue
        ftype = "string" if declared_type is None else declared_type
        if not isinstance(ftype, str):
            errors.append(f"{cwhere}: type must be a string, got {describe_value(ftype)}")
            ok = False
            continue
        if ftype not in CUSTOM_TYPES:
            errors.append(f"{cwhere}: type {describe_value(ftype)} is not allowed (allowed: {sorted(CUSTOM_TYPES)})")
            ok = False
            continue
        custom[cname] = CustomMapping(target=target, type=ftype)

    unmapped: list[UnmappedField] = []
    raw_unmapped = body.get("unmapped_bullhorn_fields")
    if raw_unmapped is None:
        raw_unmapped = []
    if not isinstance(raw_unmapped, list):
        errors.append(f"{where}.unmapped_bullhorn_fields: must be a list")
        ok = False
        raw_unmapped = []
    for i, entry in enumerate(raw_unmapped):
        parsed = _parse_unmapped(f"{where}.unmapped_bullhorn_fields[{i}]", entry, errors)
        if parsed is None:
            ok = False
        else:
            unmapped.append(parsed)

    if not ok:
        return None
    return EntityProfile(standard=standard, custom=custom, unmapped_bullhorn_fields=tuple(unmapped))


def _opt_str(where: str, key: str, value: Any, errors: list[str]) -> tuple[bool, str | None]:
    if value is None or isinstance(value, str):
        return True, value
    errors.append(f"{where}.{key}: must be a string")
    return False, None


def _opt_bool(where: str, key: str, value: Any, errors: list[str]) -> tuple[bool, bool | None]:
    if value is None or isinstance(value, bool):
        return True, value
    errors.append(f"{where}.{key}: must be a boolean")
    return False, None


def _parse_unmapped(where: str, entry: Any, errors: list[str]) -> UnmappedField | None:
    if isinstance(entry, str):
        if not is_raw_name(entry):
            errors.append(f"{where}: {describe_value(entry)} must match {RAW_NAME_RE.pattern}")
            return None
        return UnmappedField(field=entry)
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a field name or a mapping")
        return None
    ok = True
    for key in entry:
        if not isinstance(key, str) or key not in _UNMAPPED_KEYS:
            errors.append(f"{where}: unknown key {describe_value(key)}")
            ok = False
    fname = entry.get("field")
    if not is_raw_name(fname):
        errors.append(f"{where}.field: must match {RAW_NAME_RE.pattern}")
        ok = False
    r1, label = _opt_str(where, "label", entry.get("label"), errors)
    r2, data_type = _opt_str(where, "data_type", entry.get("data_type"), errors)
    r3, field_type = _opt_str(where, "field_type", entry.get("field_type"), errors)
    r4, required = _opt_bool(where, "required", entry.get("required"), errors)
    r5, read_only = _opt_bool(where, "read_only", entry.get("read_only"), errors)
    ok = ok and r1 and r2 and r3 and r4 and r5

    options: tuple[tuple[Any, Any], ...] | None = None
    raw_options = entry.get("options")
    if raw_options is not None:
        if not isinstance(raw_options, list):
            errors.append(f"{where}.options: must be a list of {{value, label}} mappings")
            ok = False
        else:
            collected: list[tuple[Any, Any]] = []
            for j, opt in enumerate(raw_options):
                if not isinstance(opt, dict) or "value" not in opt or any(k not in ("value", "label") for k in opt):
                    errors.append(f"{where}.options[{j}]: must be a {{value, label}} mapping")
                    ok = False
                    continue
                value, label_ = opt["value"], opt.get("label")
                # F-8: option pairs are scalars, matching FieldMeta.options.
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    errors.append(f"{where}.options[{j}].value: must be a scalar, got {describe_value(value)}")
                    ok = False
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    # F-10 #4: NaN/inf would break load -> save -> load equality (AC-9).
                    errors.append(f"{where}.options[{j}].value: must be a finite number, got {describe_value(value)}")
                    ok = False
                    continue
                if label_ is not None and not isinstance(label_, str):
                    errors.append(f"{where}.options[{j}].label: must be a string, got {describe_value(label_)}")
                    ok = False
                    continue
                collected.append((value, label_))
            options = tuple(collected)
    if not ok:
        return None
    assert isinstance(fname, str)
    return UnmappedField(
        field=fname,
        label=label,
        data_type=data_type,
        field_type=field_type,
        options=options,
        required=required,
        read_only=read_only,
    )


# ---------------------------------------------------------------------- #
# Active profile resolution
# ---------------------------------------------------------------------- #

ProfileState = Literal["none", "loaded", "missing", "invalid"]


@dataclass(frozen=True)
class ProfileStatus:
    state: ProfileState
    profile: MappingProfile | None = None
    errors: tuple[str, ...] = ()
    path: Path | None = None


def resolve_profile_path() -> Path | None:
    """Return the path in ``BULLHORN_MAPPING_PROFILE``, or None when unset/blank."""
    value = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def load_active_profile(path: Path | None = None, canonical: CanonicalCatalog | None = None) -> ProfileStatus:
    """Load the active tenant profile. Never raises.

    With no ``path``, the location comes from ``BULLHORN_MAPPING_PROFILE``. A
    missing or invalid profile is reported (and logged) with ``profile=None``
    so that a broken profile can never partially apply.
    """
    try:
        resolved = path if path is not None else resolve_profile_path()
        if resolved is None:
            return ProfileStatus(state="none")
        if not resolved.is_file():
            msg = f"mapping profile not found: {truncate_text(str(resolved))}"
            logger.warning("%s; using catalog defaults only", msg)
            return ProfileStatus(state="missing", errors=(msg,), path=resolved)
        profile = MappingProfile.load(resolved, canonical)
        return ProfileStatus(state="loaded", profile=profile, path=resolved)
    except ProfileError as exc:
        # str(exc) lists at most MAX_ERROR_LINES bounded lines (see errors.py).
        logger.warning("invalid mapping profile %s; using catalog defaults only: %s", exc.source, exc)
        return ProfileStatus(state="invalid", errors=cap_errors(exc.errors), path=path or _safe_resolve())
    except Exception as exc:  # never raise: any failure means "no profile applied"
        msg = f"{type(exc).__name__}: {truncate_text(str(exc))}"
        logger.warning("could not load mapping profile; using catalog defaults only: %s", msg)
        return ProfileStatus(state="invalid", errors=(msg,), path=path or _safe_resolve())


def _safe_resolve() -> Path | None:
    try:
        return resolve_profile_path()
    except Exception:
        return None
