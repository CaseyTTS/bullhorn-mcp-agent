"""Bullhorn write transport (Phase 4B, D-4B-13).

``client.py`` is unchanged. ``EntityWriter`` sends its own requests using the
client's session (``client.auth.session``: ``rest_url`` and ``bh_rest_token``).
It retries **once** on HTTP 401 only, after ``client.auth._refresh_session()``,
which is the same mechanism ``client.py`` uses, and never retries otherwise.
Every non-200 response raises ``BullhornAPIError`` with a bounded, redacted
message (§1.5).

Verified mechanisms only (``docs/architecture/PHASE4B_HV_VERIFICATION.md``):

- ``create``: ``PUT /entity/{Entity}`` with a JSON body (HV-B1).
- ``associate``: ``PUT /entity/{Entity}/{id}/{toManyField}/{ids}`` (HV-B3).
- ``fetch_to_many``: ``GET /entity/{Entity}/{id}/{toManyField}?fields&start&count``
  (HV-B5, ``JobOrder.notes`` / ``Placement.notes`` reads). It is a read, but it
  lives here because it needs the raw transport and ``client.py`` must not change.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from ..schema.errors import truncate_text
from .errors import BullhornAPIError

MAX_ERROR_CHARS = 300
MAX_IDS_PER_CALL = 10

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}", re.ASCII)
_FIELDS_RE = re.compile(r"[A-Za-z0-9_,()\[\]]{1,1000}", re.ASCII)

MAX_ERROR_BODY_CHARS = 200_000  # kept whole so comment scrubbing never sees a cut echo (B-5)

# Secret names (case-insensitive; '-' or '_' separators allowed, e.g. "Bh-Rest-Token", "bh_rest_token").
_SECRET_NAMES = r"bh[-_]?rest[-_]?token|access[-_]?token|refresh[-_]?token|password|client[-_]?secret"
# The name, an optional closing quote (" ' \" \' %22 %27), a separator (: = %3A %3D) and an
# optional opening quote for the value.
_QUOTE = r"""(?:\\"|\\'|"|'|%22|%27)"""
_SECRET_START_RE = re.compile(r"(?i)(" + _SECRET_NAMES + r")(" + _QUOTE + r"?\s*(?::|=|%3A|%3D)\s*)(" + _QUOTE + r"?)")
# An unquoted value ends at a separator, a quote or the end of the line (it may contain spaces).
_UNQUOTED_END_RE = re.compile(r"""(?i)(?:[&,;}\]\r\n"']|%22|%26|%27|\\")""")
# Any 20+ character token-like run directly after one of the names (e.g. "BhRestToken abc...").
_SECRET_RUN_RE = re.compile(r"(?i)(" + _SECRET_NAMES + r")(\W{0,5})([A-Za-z0-9._~+/=\-]{20,})")


class WriteOutcomeUnknown(BullhornAPIError):
    """Bullhorn answered 200 but the body is not a JSON object: the write may have happened (B-4)."""


# Phase 5B (D-5B-7, P4B-8): the raw error body of a non-200 response is kept in this private,
# non-rendered attribute. It is never part of ``str()`` / ``repr()`` / ``args``; only
# ``safe_error_text`` reads it, and it scrubs the comment text before redacting secrets.
_RAW_BODY_ATTR = "_bullhorn_raw_error_body"
FRAGMENT_CHARS = 8


def _api_error(status: int, body: str) -> BullhornAPIError:
    exc = BullhornAPIError(f"API request failed: {status}")
    setattr(exc, _RAW_BODY_ATTR, body[:MAX_ERROR_BODY_CHARS])
    return exc


def raw_error_body(exc: BaseException | str) -> str | None:
    """The private raw body of an ``EntityWriter`` error (``None`` when there is none)."""
    body = getattr(exc, _RAW_BODY_ATTR, None) if isinstance(exc, BaseException) else None
    return body if isinstance(body, str) else None


def _scrub_fragments(text: str, secret: str | None) -> str:
    """Replace every run of text that contains a ``FRAGMENT_CHARS``-character piece of ``secret`` (B-5, AC-14)."""
    if not secret:
        return text
    grams: set[str] = set()
    encoded = [quote(secret, safe=""), quote_plus(secret, safe=""), html.escape(secret)]  # URL- and HTML-encoded echoes
    for variant in dict.fromkeys(_variants(secret) + encoded):
        grams.update(variant[i : i + FRAGMENT_CHARS] for i in range(len(variant) - FRAGMENT_CHARS + 1))
    if not grams:
        return text
    marker = "<comments:" + hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()[:12] + ">"
    covered = [False] * len(text)
    for i in range(len(text) - FRAGMENT_CHARS + 1):
        if text[i : i + FRAGMENT_CHARS] in grams:
            for k in range(i, i + FRAGMENT_CHARS):
                covered[k] = True
    if not any(covered):
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        if covered[i]:
            while i < len(text) and covered[i]:
                i += 1
            out.append(marker)
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _redact_values(text: str) -> str:
    out: list[str] = []
    pos = 0
    while True:
        m = _SECRET_START_RE.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos : m.end()])
        opening = m.group(3)
        if opening:
            # A quoted value (possibly with spaces) runs to the matching closing quote, else to the end of the line.
            end = text.find(opening, m.end())
            if end < 0:
                newline = re.search(r"[\r\n]", text[m.end() :])
                end = m.end() + newline.start() if newline else len(text)
        else:
            stop = _UNQUOTED_END_RE.search(text, m.end())
            end = stop.start() if stop else len(text)
        if end > m.end():
            out.append("***")
        pos = end


def redact_secrets(text: str) -> str:
    """Replace secret values (tokens, passwords) in free text with ``***``.

    Covers JSON (also backslash-escaped), query strings (also URL-encoded), header
    forms and values containing spaces (up to the closing quote, a separator or
    the end of the line).
    """
    text = _redact_values(text)
    return _SECRET_RUN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def _variants(text: str) -> list[str]:
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    ascii_escaped = json.dumps(text, ensure_ascii=True)[1:-1]
    return list(dict.fromkeys(v for v in (text, escaped, ascii_escaped) if v))


def scrub_text(text: str, secret: str | None) -> str:
    """Replace every echo of ``secret`` (whole, and each line of 8+ characters) with a hash marker (B-5)."""
    if not secret:
        return text
    marker = "<comments:" + hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()[:12] + ">"
    pieces = [secret] + [line for line in re.split(r"\r\n|\r|\n", secret) if len(line) >= 8]
    candidates: list[str] = []
    for piece in pieces:
        candidates.extend(_variants(piece))
    for candidate in sorted(set(candidates), key=len, reverse=True):
        text = text.replace(candidate, marker)
    # A cut echo at the very end of the text (8+ characters of a line's prefix).
    for candidate in candidates:
        for k in range(min(len(candidate), len(text)), 7, -1):
            if text.endswith(candidate[:k]):
                text = text[: len(text) - k] + marker
                break
    return text


def safe_error_text(exc: BaseException | str, limit: int = MAX_ERROR_CHARS, scrub: str | None = None) -> str:
    """A bounded, redacted description of an error (never the raw exception).

    ``scrub`` (the note text) is removed before anything is cut, so no partial echo survives.
    D-5B-7: (1) the raw error body, when present; (2) scrub the note text (whole, each line of
    8+ characters, and any 8+ character fragment); (3) redact secrets; (4) bound.
    """
    raw = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    body = raw_error_body(exc)
    if body is not None:
        raw = f"{raw} - {body}"
    raw = scrub_text(raw[: MAX_ERROR_BODY_CHARS + 1000], scrub)
    raw = _scrub_fragments(raw, scrub)
    # Redact before and after truncation so a cut can never expose a partial secret.
    return redact_secrets(truncate_text(redact_secrets(raw), limit))


def _check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def _check_id(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("entity ids must be ints >= 1")
    return value


class EntityWriter:
    """JSON-body requests on the client's session (no change to ``client.py``)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        auth = self.client.auth
        session = auth.session
        with httpx.Client() as http:
            response = http.request(
                method, f"{session.rest_url}{path}", params=params, json=body, headers={"BhRestToken": session.bh_rest_token}
            )
            if response.status_code == 401:
                auth._refresh_session()
                session = auth.session
                response = http.request(
                    method, f"{session.rest_url}{path}", params=params, json=body, headers={"BhRestToken": session.bh_rest_token}
                )
        if response.status_code != 200:
            # D-5B-7: the body is private; safe_error_text scrubs the note text, then redacts, then bounds.
            raise _api_error(response.status_code, response.text)
        try:
            data = response.json()
        except ValueError:
            raise WriteOutcomeUnknown("API request returned 200 but the response is not JSON") from None
        if not isinstance(data, dict):
            raise WriteOutcomeUnknown("API request returned 200 but the response is not a JSON object")
        return data

    def create(self, entity: str, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT /entity/{entity}`` with a JSON body (HV-B1). Returns the raw response object."""
        _check_name(entity, "entity name")
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        return self._send("PUT", f"/entity/{entity}", body=body)

    def associate(self, entity: str, entity_id: int, association: str, ids: list[int]) -> dict[str, Any]:
        """``PUT /entity/{entity}/{id}/{association}/{ids}`` (HV-B3)."""
        _check_name(entity, "entity name")
        _check_name(association, "association name")
        _check_id(entity_id)
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS_PER_CALL:
            raise ValueError(f"between 1 and {MAX_IDS_PER_CALL} ids are required")
        joined = ",".join(str(_check_id(i)) for i in ids)
        return self._send("PUT", f"/entity/{entity}/{entity_id}/{association}/{joined}")

    def fetch_to_many(
        self, entity: str, entity_id: int, association: str, fields: str, start: int, count: int
    ) -> dict[str, Any]:
        """``GET /entity/{entity}/{id}/{association}?fields&start&count`` (HV-B5)."""
        _check_name(entity, "entity name")
        _check_name(association, "association name")
        _check_id(entity_id)
        if not isinstance(fields, str) or not _FIELDS_RE.fullmatch(fields):
            raise ValueError("invalid fields")
        if type(start) is not int or type(count) is not int or start < 0 or not 1 <= count <= 500:
            raise ValueError("invalid paging")
        return self._send(
            "GET", f"/entity/{entity}/{entity_id}/{association}", params={"fields": fields, "start": start, "count": count}
        )
