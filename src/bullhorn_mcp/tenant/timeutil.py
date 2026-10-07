"""UTC time primitives for tenant setup (D-4A-11).

``coerce_epoch_millis_to_utc_iso`` converts a Bullhorn ``Timestamp`` value
(UNIX epoch milliseconds; HV-A3) into an ISO-8601 UTC string.

Phase 5C (D-5C-9): ``parse_bound`` (moved here from ``notes/reads.py``) is the
single date-bound parser, and ``epoch_millis_to_local_iso`` renders a timestamp
in the tenant's reporting timezone. ``tzdata`` is a runtime dependency.
"""

from __future__ import annotations

import datetime as _dt
import re
import zoneinfo
from collections.abc import Callable

from ..schema.errors import describe_value

DEFAULT_TIMEZONE = "UTC"

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
# datetime's own range: 0001-01-01T00:00:00Z .. 9999-12-31T23:59:59.999Z.
MIN_EPOCH_MILLIS = -62_135_596_800_000
MAX_EPOCH_MILLIS = 253_402_300_799_999

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", re.ASCII)
# IANA zone names: '/'-separated parts of ASCII letters, digits, '_', '+', '-'. No '.', no leading '/'.
_ZONE_RE = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*", re.ASCII)
MAX_ZONE_CHARS = 64


def coerce_epoch_millis_to_utc_iso(value: object) -> str:
    """Epoch milliseconds -> ``YYYY-MM-DDTHH:MM:SS[.mmm]Z``.

    Only a plain ``int`` is accepted (``bool``, ``float``, NaN, strings and int
    subclasses are rejected). Every rejection is a ``ValueError``.
    """
    if type(value) is not int:
        raise ValueError(f"epoch milliseconds must be an int, got {describe_value(value)}")
    if not MIN_EPOCH_MILLIS <= value <= MAX_EPOCH_MILLIS:
        raise ValueError(f"epoch milliseconds out of range: {describe_value(value)}")
    moment = _EPOCH + _dt.timedelta(milliseconds=value)
    millis = value % 1000
    base = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    return f"{base}.{millis:03d}Z" if millis else f"{base}Z"


def validate_timezone(name: object) -> str:
    """Return ``name`` when it is a resolvable timezone; else ``ValueError`` with a bounded message.

    ``"UTC"`` is always valid: it is the built-in default and needs no tz
    database (Windows hosts without ``tzdata`` cannot resolve it through
    ``zoneinfo``). Every other name must be resolvable by
    ``zoneinfo.ZoneInfo`` on this host.
    """
    if not isinstance(name, str):
        raise ValueError(f"timezone must be a string, got {describe_value(name)}")
    if name == DEFAULT_TIMEZONE:
        return name
    if len(name) > MAX_ZONE_CHARS or not _ZONE_RE.fullmatch(name):
        raise ValueError(f"unknown timezone {describe_value(name)}")
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError, TypeError, LookupError) as exc:
        raise ValueError(f"unknown timezone {describe_value(name)} ({type(exc).__name__})") from None
    return name


# ---------------------------------------------------------------------- #
# Date bounds (D-4B-15, D-5C-9): the single parser
# ---------------------------------------------------------------------- #

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?", re.ASCII)
_MS = _dt.timedelta(milliseconds=1)


class FilterError(ValueError):
    """A filter value is invalid. The message is bounded."""


def resolve_zone(name: str) -> _dt.tzinfo:
    """The reporting timezone (``UTC`` needs no tz database)."""
    if name == "UTC":
        return _dt.timezone.utc
    return zoneinfo.ZoneInfo(name)


def _ceil_ms(moment: _dt.datetime) -> int:
    delta = moment - _EPOCH
    whole = delta // _MS
    return whole if delta == whole * _MS else whole + 1


def parse_bound(
    value: object, name: str, timezone_name: str, zone: Callable[[str], _dt.tzinfo] | None = None
) -> int:
    """ISO-8601 date or datetime -> epoch milliseconds (D-4B-15, D-5C-9).

    - A date-only value is midnight in the tenant reporting timezone (``fold=0``
      for an ambiguous or nonexistent local time).
    - A datetime needs an explicit offset (or ``Z``); a naive datetime is rejected.
    - Sub-millisecond values round up, so ``[from, to)`` holds exactly for integer
      millisecond timestamps.

    ``zone`` resolves the timezone name (default ``resolve_zone``).
    """
    if not isinstance(value, str) or len(value) > 40:
        raise FilterError(f"{name} must be an ISO-8601 date or datetime string, got {describe_value(value)}")
    try:
        if _DATE_RE.fullmatch(value):
            day = _dt.date.fromisoformat(value)
            moment = _dt.datetime(day.year, day.month, day.day, tzinfo=(zone or resolve_zone)(timezone_name))
        else:
            m = _DATETIME_RE.fullmatch(value)
            if m is None:
                raise FilterError(f"{name} must be an ISO-8601 date or datetime, got {describe_value(value)}")
            if m.group(1) is None:
                raise FilterError(f"{name}: a datetime without a UTC offset is not accepted, got {describe_value(value)}")
            text = value[:-1] + "+00:00" if value.endswith("Z") else value
            moment = _dt.datetime.fromisoformat(text)
        return _ceil_ms(moment)
    except FilterError:
        raise
    except (ValueError, OverflowError, zoneinfo.ZoneInfoNotFoundError, OSError, LookupError) as exc:
        raise FilterError(f"{name} is not a valid date or datetime: {describe_value(value)} ({type(exc).__name__})") from None


def epoch_millis_to_local_iso(value: int, timezone_name: str) -> str:
    """Epoch milliseconds -> ISO-8601 in the reporting timezone, with its offset (``...+HH:MM``)."""
    coerce_epoch_millis_to_utc_iso(value)  # validates the value (a plain int in datetime's range)
    moment = (_EPOCH + _dt.timedelta(milliseconds=value)).astimezone(resolve_zone(timezone_name))
    return moment.isoformat(timespec="milliseconds")


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def format_utc(moment: _dt.datetime) -> str:
    """Datetime -> ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` (naive values are taken as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    moment = moment.astimezone(_dt.timezone.utc)
    text = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def parse_utc(value: object) -> _dt.datetime | None:
    """Parse a ``...Z`` UTC timestamp string; ``None`` when it is not one."""
    if not isinstance(value, str) or len(value) > 40 or not _ISO_RE.fullmatch(value):
        return None
    body = value[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    try:
        return _dt.datetime.strptime(body, fmt).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def normalize_timestamp(value: object) -> str | None:
    """A stored timestamp (string or YAML-parsed datetime) -> canonical UTC string, else ``None``."""
    if isinstance(value, _dt.datetime):
        try:
            return format_utc(value)
        except (OverflowError, ValueError):
            return None
    parsed = parse_utc(value)
    return None if parsed is None else format_utc(parsed)
