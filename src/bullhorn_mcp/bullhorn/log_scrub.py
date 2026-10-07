"""Query-string scrubbing for the HTTP library loggers (Phase 5C, Amendment C2).

``httpx`` logs every request URL at INFO. For ``/query`` that URL carries the
rendered ``where`` and so the filter values (names, emails, IDs). In
``shared`` mode, ``QueryStringScrubFilter`` replaces the query component of
every URL in a ``httpx`` / ``httpcore`` record with ``?<query-REDACTED>``, and
every all-digit path segment (a record id, 5C triage B-3) with ``<id>``, and
keeps the record (method, scheme, host and path stay visible). In ``local``
mode it changes nothing. The 5A INFO clamp and record factory are unchanged.

The filter is installed (idempotently) on the ``httpx`` and ``httpcore``
loggers and on every existing child of either when this module is imported,
and again by ``EntityReader.__init__``.

Known residual (P5A-12): records built with ``makeLogRecord``, custom
formatters and ``extra=`` fields are not covered.
"""

from __future__ import annotations

import logging
import re

LOGGER_NAMES = ("httpx", "httpcore")
# Amendment C2-1 names the placeholder "?<query-redacted>". It is upper-cased here because
# C2-2 T-C2d requires the 5A tests to pass unmodified, and T-B1a (and the 5A sentinel test)
# assert that the token "REDACTED" (case-sensitive) is present in the scrubbed record.
REDACTED_QUERY = "?<query-REDACTED>"
REDACTED_ID = "<id>"
# A URL (scheme://authority/path) with an optional query component; the query runs to whitespace, a quote or the end.
_URL_RE = re.compile(r"""((?:https?|wss?)://[^\s/?#"'<>]*)([^\s?#"'<>]*)(\?[^\s"']*)?""", re.IGNORECASE)
# Triage B-3 / R2-L1: an all-digit path segment, or comma-joined ids, is a filter value in shared mode.
_ID_SEGMENT_RE = re.compile(r"(?<=/)[0-9]+(?:,[0-9]+)*(?=/|$|\?)")  # R2-L1: comma-joined ids too


def _scrub_url(m: re.Match[str]) -> str:
    authority, path, query = m.group(1), m.group(2), m.group(3)
    return authority + _ID_SEGMENT_RE.sub(REDACTED_ID, path) + (REDACTED_QUERY if query is not None else "")


def scrub_query_strings(text: str) -> str:
    """Replace each URL's query with ``?<query-REDACTED>`` and each all-digit path segment with ``<id>``."""
    return _URL_RE.sub(_scrub_url, text)


def _applies(name: str) -> bool:
    return any(name == n or name.startswith(n + ".") for n in LOGGER_NAMES)


class QueryStringScrubFilter(logging.Filter):
    """Shared mode only: scrub URL query strings from httpx/httpcore records. Always keeps the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _applies(record.name):
            return True
        try:
            from ..identity import deploy

            shared = deploy.is_shared()
        except Exception:  # pragma: no cover - fail safe: scrub when the mode cannot be read
            shared = True
        if not shared:
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = "<unformattable log record>"
        record.msg = scrub_query_strings(message)
        record.args = ()
        return True


_FILTER = QueryStringScrubFilter()


def install() -> None:
    """Install the filter on ``httpx``, ``httpcore`` and every existing child logger (idempotent)."""
    names = set(LOGGER_NAMES)
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and _applies(name):
            names.add(name)
    for name in sorted(names):
        logger = logging.getLogger(name)
        if not any(isinstance(f, QueryStringScrubFilter) for f in logger.filters):
            logger.addFilter(_FILTER)


install()
