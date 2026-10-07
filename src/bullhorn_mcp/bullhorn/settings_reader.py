"""Read-only Bullhorn system settings transport (Phase 5B, D-5B-1).

``SettingsReader(client).get(names)`` sends ``GET /settings/{name1,name2...}``
(``docs/architecture/PHASE5B_HV_VERIFICATION.md``, HV-D1) on the client's
session, the same way ``EntityWriter`` does: ``client.py`` is unchanged, the
request is retried **once** on HTTP 401 only, after
``client.auth._refresh_session()``, and every other failure raises
``BullhornAPIError`` with a bounded, redacted message. The response body is
never included in an error.

This module issues ``GET`` requests only. Whether a setting is actually
consulted is decided by the caller (``notes.action_discovery`` guards
``commentActionList`` behind ``SETTINGS_ACTION_SOURCE_VERIFIED``).

P5B-2 (Phase 5C): the body is streamed and the read is aborted as soon as it
exceeds ``MAX_BODY_BYTES``; the JSON nesting depth is checked without recursion
before parsing (and a ``RecursionError`` is still contained); every request has
an explicit timeout.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..schema.errors import truncate_text
from .errors import BullhornAPIError
from .writes import redact_secrets

MAX_SETTING_NAMES = 10
MAX_BODY_BYTES = 256 * 1024
MAX_ERROR_CHARS = 300
MAX_JSON_DEPTH = 64
TIMEOUT_SECONDS = 15.0

_SETTING_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}", re.ASCII)


def _fail(message: str) -> BullhornAPIError:
    return BullhornAPIError(redact_secrets(truncate_text(message, MAX_ERROR_CHARS)))


class _TooLarge(Exception):
    pass


class _Encoded(Exception):
    pass


class _CappedStream(httpx.SyncByteStream):
    """Wraps a response stream: the read is aborted as soon as it exceeds ``MAX_BODY_BYTES`` (P5B-2)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __iter__(self) -> Any:
        total = 0
        for chunk in self._inner:
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise _TooLarge()
            yield chunk

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


def _cap_body(response: httpx.Response) -> None:
    """Response hook: it runs before the body is read, so the cap applies while streaming.

    The request asks for ``Accept-Encoding: identity`` and an encoded (compressed)
    response is refused, so the capped raw bytes are exactly the decoded bytes and
    no decompression can expand past the cap.
    """
    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        raise _Encoded()
    response.stream = _CappedStream(response.stream)


def _too_deep(text: str) -> bool:
    """Iterative nesting-depth check (strings are skipped), so parsing can never recurse too deeply."""
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


class SettingsReader:
    """``GET /settings/{names}`` on the client's session (read-only)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def get(self, names: list[str]) -> dict[str, Any]:
        """Return the settings response object. Raises ``ValueError`` for bad names, ``BullhornAPIError`` otherwise."""
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_SETTING_NAMES:
            raise ValueError(f"between 1 and {MAX_SETTING_NAMES} setting names are required")
        for name in names:
            if not isinstance(name, str) or not _SETTING_NAME_RE.fullmatch(name):
                raise ValueError("invalid setting name")
        path = "/settings/" + ",".join(names)
        auth = self.client.auth
        session = auth.session
        headers = {"BhRestToken": session.bh_rest_token, "Accept-Encoding": "identity"}
        try:
            with httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), event_hooks={"response": [_cap_body]}) as http:
                response = http.get(f"{session.rest_url}{path}", headers=headers)
                if response.status_code == 401:
                    auth._refresh_session()
                    session = auth.session
                    headers["BhRestToken"] = session.bh_rest_token
                    response = http.get(f"{session.rest_url}{path}", headers=headers)
        except _TooLarge:
            raise _fail(f"settings response is larger than {MAX_BODY_BYTES} bytes") from None
        except _Encoded:
            raise _fail("settings response uses an unsupported content encoding") from None
        if response.status_code != 200:
            # HV-D3: the body of a refusal is never echoed (it may describe the tenant's configuration).
            raise _fail(f"settings request failed: {response.status_code}")
        body = response.content
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("settings response is not JSON") from None
        if _too_deep(text):
            raise _fail(f"settings response is nested deeper than {MAX_JSON_DEPTH} levels")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise _fail("settings response is not JSON") from None
        if not isinstance(data, dict):
            raise _fail("settings response is not a JSON object")
        return data
