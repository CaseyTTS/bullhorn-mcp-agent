"""Audit logging for Bullhorn MCP tool invocations.

Every tool invocation is logged as one structured record via the
``bullhorn_mcp.audit`` logger. Arguments are always redacted before logging
so that credential-shaped values are never written to logs, and only a short
result summary is logged - never a full response payload.
"""

import json
import logging
from typing import Any

logger = logging.getLogger("bullhorn_mcp.audit")

# Key names (matched case-insensitively) whose values are always redacted.
_SECRET_KEYS = {
    "password",
    "client_secret",
    "access_token",
    "refresh_token",
    "bh_rest_token",
    "api_key",
    "secret",
}


def redact(data: dict) -> dict:
    """Recursively redact secret-shaped values from a dict.

    Any key matching (case-insensitively) one of the known secret key names
    has its value replaced with the literal string ``"***REDACTED***"``. All
    other keys and values - including nested dicts and lists containing
    dicts - are left untouched.
    """

    def _redact_value(value: Any) -> Any:
        if isinstance(value, dict):
            return _redact_dict(value)
        if isinstance(value, list):
            return [_redact_value(item) for item in value]
        return value

    def _redact_dict(d: dict) -> dict:
        redacted: dict = {}
        for key, value in d.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact_value(value)
        return redacted

    return _redact_dict(data)


def log_invocation(
    tool: str,
    args: dict,
    result_summary: str,
    duration_ms: float,
    success: bool = True,
) -> None:
    """Emit one structured audit log record for a tool invocation.

    ``args`` is always redacted before logging - never log a raw
    credential-shaped value under any circumstance. ``result_summary`` must
    be a short status string (e.g. ``"ok"``, ``"denied"``,
    ``"dry_run_preview"``, or ``f"error: {e}"``) - never the full tool
    output.
    """
    record = {
        "tool": tool,
        "args": redact(args),
        "duration_ms": duration_ms,
        "result": result_summary,
    }
    message = json.dumps(record, default=str)

    if success:
        logger.info(message)
    else:
        logger.warning(message)
