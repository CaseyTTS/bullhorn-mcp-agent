"""UTC time primitives for tenant setup (D-4A-11).

``coerce_epoch_millis_to_utc_iso`` converts a Bullhorn ``Timestamp`` value
(UNIX epoch milliseconds; HV-A3) into an ISO-8601 UTC string. Conversion into
the tenant's reporting timezone is Phase 5 and is not done here.
"""

from __future__ import annotations

import datetime as _dt
import re
import zoneinfo

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
