"""The get_recent_placements MCP tool."""

import time
from datetime import datetime, timedelta, timezone

from .. import server
from ..auth import AuthenticationError
from ..bullhorn.client import BullhornAPIError
from ..crosscutting import audit, permissions


@server.mcp.tool()
def get_recent_placements(days: int = 30, limit: int = 100) -> str:
    """Get recent Bullhorn placements from the last X days."""
    start_time = time.perf_counter()
    args = {"days": days, "limit": limit}

    decision = permissions.check("get_recent_placements")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_recent_placements",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("get_recent_placements", decision)

    if days < 1 or days > 365:
        result = "ERROR: days must be between 1 and 365"
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_recent_placements",
            args=args,
            result_summary=result,
            duration_ms=duration_ms,
            success=False,
        )
        return result

    if limit < 1 or limit > 500:
        result = "ERROR: limit must be between 1 and 500"
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_recent_placements",
            args=args,
            result_summary=result,
            duration_ms=duration_ms,
            success=False,
        )
        return result

    try:
        client = server.get_client()

        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        start_ms = int(start_date.timestamp() * 1000)

        results = client.query(
            entity="Placement",
            where=f"dateAdded >= {start_ms}",
            fields=None,
            count=limit,
            order_by="-dateAdded",
        )

        result = server.format_response(results)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_recent_placements",
            args=args,
            result_summary="ok",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    except (AuthenticationError, BullhornAPIError) as e:
        result = f"ERROR: {e}"
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_recent_placements",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result
