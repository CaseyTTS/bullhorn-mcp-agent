"""The connection_status MCP tool."""

import os
import time

from .. import server
from ..crosscutting import audit, permissions


@server.mcp.tool()
def connection_status() -> str:
    """Check whether Bullhorn API credentials are configured and connectivity is available."""
    start_time = time.perf_counter()
    args: dict = {}

    decision = permissions.check("connection_status")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="connection_status",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("connection_status", decision)

    required_vars = [
        "BULLHORN_CLIENT_ID",
        "BULLHORN_CLIENT_SECRET",
        "BULLHORN_USERNAME",
        "BULLHORN_PASSWORD",
    ]

    missing = [name for name in required_vars if not os.getenv(name)]

    if missing:
        result = server.format_response({
            "configured": False,
            "connected": False,
            "message": "Bullhorn credentials have not been configured yet.",
            "missing_variables": missing,
        })
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="connection_status",
            args=args,
            result_summary="ok",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    try:
        client = server.get_client()
        session = client.auth.session

        result = server.format_response({
            "configured": True,
            "connected": True,
            "message": "Successfully connected to Bullhorn.",
            "rest_url": session.rest_url,
        })
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="connection_status",
            args=args,
            result_summary="ok",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    except Exception as e:
        result = server.format_response({
            "configured": True,
            "connected": False,
            "message": f"Bullhorn connection failed: {e}",
        })
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="connection_status",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result
