"""Credential sources (Phase 5A, OBS-1).

A secret is configured **by reference only**: ``env:NAME`` (an environment
variable) or ``file:/absolute/path`` (the whole file, trailing newline
stripped). A literal value is never accepted where a reference is required.

``CredentialSource`` resolves a reference at the moment it is needed and
never caches the value. Error messages name the reference *kind* only, never
the value. ``install_log_redaction`` (installed by a shared deployment) keeps
secret query parameters out of HTTP client/server logs.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}", re.ASCII)
MAX_SECRET_BYTES = 64_000


class SecretRefError(ValueError):
    """A secret reference is malformed or cannot be resolved. The message never contains a secret."""


def is_reference(value: object) -> bool:
    """Is ``value`` a well-formed ``env:NAME`` or ``file:/abs/path`` reference?"""
    if not isinstance(value, str):
        return False
    if value.startswith("env:"):
        return _ENV_NAME_RE.fullmatch(value[4:]) is not None
    if value.startswith("file:"):
        path = value[5:]
        return bool(path) and "\x00" not in path and Path(path).is_absolute()
    return False


@dataclass(frozen=True)
class SecretRef:
    """A validated reference. ``repr`` shows only the reference, never the value."""

    ref: str

    def __post_init__(self) -> None:
        if not is_reference(self.ref):
            raise SecretRefError("a secret must be given as env:NAME or file:/absolute/path")

    @property
    def kind(self) -> str:
        return self.ref.split(":", 1)[0]


@dataclass
class CredentialSource:
    """Resolves ``SecretRef`` values from the environment or from files."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def resolve(self, ref: SecretRef | str) -> str:
        sref = ref if isinstance(ref, SecretRef) else SecretRef(ref)
        if sref.kind == "env":
            value = self.env.get(sref.ref[4:], "")
            if not isinstance(value, str) or not value:
                raise SecretRefError(f"secret reference ({sref.kind}) is not set")
            return value
        path = Path(sref.ref[5:])
        try:
            with open(path, "rb") as fh:
                data = fh.read(MAX_SECRET_BYTES + 1)
        except OSError as exc:
            raise SecretRefError(f"secret reference ({sref.kind}) cannot be read ({type(exc).__name__})") from None
        if len(data) > MAX_SECRET_BYTES:
            raise SecretRefError(f"secret reference ({sref.kind}) is too large")
        try:
            text = data.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise SecretRefError(f"secret reference ({sref.kind}) is not UTF-8") from None
        if not text:
            raise SecretRefError(f"secret reference ({sref.kind}) is empty")
        return text


DEFAULT_SOURCE = CredentialSource()


# ---------------------------------------------------------------------- #
# Log redaction (AC-15; 5A triage B-1)
# ---------------------------------------------------------------------- #
#
# Bullhorn documents its OAuth and login parameters in the *query string*
# (HV-C1/C3/C4); ``httpx``/``httpcore`` log request URLs and response headers,
# and an ASGI server's access log records the OAuth callback URL (``code``,
# ``state``). ``redact_query`` is the single redaction function. In shared mode
# ``install_log_redaction`` installs a global log-record factory that replaces
# every record's message with its redacted, fully formatted text (so no
# per-logger filter or child logger can be bypassed) and clamps the HTTP/MCP
# library loggers to at least INFO. DEBUG logging is unsupported in shared mode.

SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "code", "state", "access_token", "refresh_token", "id_token", "client_secret", "password", "username",
        "bhresttoken", "token", "login", "csrf", "link", "confirmation", "__host-bhmcp_login",
    }
)
_QUERY_PARAM_RE = re.compile(r"(^|[?&;\s,(\[{'\"])([A-Za-z0-9_.\-]+)=([^&;#\s\"',)\]}]*)")
_HEADER_RE = re.compile(
    r"(?i)(bhresttoken|proxy-authorization|authorization|set-cookie|cookie)(['\"]?\s*[:=,]\s*(?-i:b)?['\"]?\s*)((?:bearer|basic)\s+)?([^\s'\",)\]}]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=\-]{6,})")
_JSON_RE = re.compile(
    r'(?i)("(?:access_token|refresh_token|id_token|client_secret|password|confirmation|code|state|login_url)"\s*:\s*")([^"]*)(")'
)
REDACTED = "REDACTED"


def redact_query(text: str) -> str:
    """Redact every sensitive value in ``text``: query/form keys, token headers, bearer values, JSON fields."""

    def query(m: re.Match[str]) -> str:
        name = m.group(2)
        return f"{m.group(1)}{name}={REDACTED}" if name.lower() in SENSITIVE_QUERY_PARAMS else m.group(0)

    text = _QUERY_PARAM_RE.sub(query, text)
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{REDACTED}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _JSON_RE.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)


class RedactingFilter(logging.Filter):
    """A per-logger filter (defence in depth only; the record factory is authoritative)."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


_FILTER = RedactingFilter()
REDACTED_LOGGERS = ("httpx", "httpcore", "uvicorn.access", "uvicorn.error")
CLAMPED_LOGGERS = ("httpcore", "httpx", "h11", "h2", "hpack", "uvicorn", "sse_starlette", "mcp")
_ORIGINAL_FACTORY: Any = None
_ORIGINAL_LEVELS: dict[str, int] = {}


def _redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    return _redact_record(_ORIGINAL_FACTORY(*args, **kwargs))


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Replace the message with its redacted, fully formatted text and drop the arguments."""
    if getattr(record, "_bhmcp_redacted", False):
        return record
    record._bhmcp_redacted = True
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        # uvicorn's AccessFormatter unpacks (client, method, full_path, http_version, status); the full
        # path carries the whole query string, so each argument is redacted on its own.
        record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in record.args)
        return record
    try:
        message = record.getMessage()
    except Exception:
        message = "<unformattable log record>"
    record.msg = redact_query(message)
    record.args = ()
    if record.exc_info:
        try:
            record.exc_text = redact_query(logging.Formatter().formatException(record.exc_info))
        except Exception:
            record.exc_text = "<exception>"
    return record


def clamp_logger_levels() -> None:
    """Every clamped logger, and every existing child, gets an effective level of at least INFO."""
    names = set(CLAMPED_LOGGERS)
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and any(name.startswith(p + ".") for p in CLAMPED_LOGGERS):
            names.add(name)
    for name in names:
        logger = logging.getLogger(name)
        clamp = logger.level < logging.INFO if name in CLAMPED_LOGGERS else logging.NOTSET < logger.level < logging.INFO
        if clamp:  # a top-level NOTSET is clamped too: never inherit DEBUG from the root
            _ORIGINAL_LEVELS.setdefault(name, logger.level)
            logger.setLevel(logging.INFO)


def install_log_redaction() -> None:
    """Shared mode: global record factory + level clamps (+ the per-logger filter). Idempotent."""
    global _ORIGINAL_FACTORY
    if _ORIGINAL_FACTORY is None:
        _ORIGINAL_FACTORY = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_redacting_factory)
    clamp_logger_levels()
    for name in REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


def log_redaction_installed() -> bool:
    return _ORIGINAL_FACTORY is not None


def uninstall_log_redaction() -> None:
    """Tests only: restore the original record factory and logger levels, and remove the filters."""
    global _ORIGINAL_FACTORY
    if _ORIGINAL_FACTORY is not None:
        logging.setLogRecordFactory(_ORIGINAL_FACTORY)
        _ORIGINAL_FACTORY = None
    for name, level in _ORIGINAL_LEVELS.items():
        logging.getLogger(name).setLevel(level)
    _ORIGINAL_LEVELS.clear()
    for name in REDACTED_LOGGERS:
        logging.getLogger(name).removeFilter(_FILTER)
