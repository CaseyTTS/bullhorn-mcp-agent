"""The list_jobs and get_job MCP tools."""

import time

from .. import server
from ..auth import AuthenticationError
from ..bullhorn.client import BullhornAPIError
from ..crosscutting import audit, permissions


@server.mcp.tool()
def list_jobs(
    query: str | None = None,
    status: str | None = None,
    limit: int = 20,
    fields: str | None = None,
) -> str:
    """List and filter job orders from Bullhorn CRM.

    Args:
        query: Lucene search query (e.g., "title:Engineer AND isOpen:1")
        status: Filter by job status
        limit: Maximum number of results (1-500, default 20)
        fields: Comma-separated fields to return

    Returns:
        JSON array of job orders

    Examples:
        - list_jobs() - Get recent jobs
        - list_jobs(query="isOpen:1") - Get open jobs
        - list_jobs(query="title:Software AND employmentType:Direct Hire", limit=10)
        - list_jobs(status="Accepting Candidates")
    """
    start_time = time.perf_counter()
    args = {"query": query, "status": status, "limit": limit, "fields": fields}

    decision = permissions.check("list_jobs")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="list_jobs",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("list_jobs", decision)

    try:
        client = server.get_client()

        # Build search query
        search_query = query or "isDeleted:0"
        if status:
            search_query = f"({search_query}) AND status:\"{status}\""

        results = client.search(
            entity="JobOrder",
            query=search_query,
            fields=fields,
            count=limit,
            sort="-dateAdded",
        )

        result = server.format_response(results)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="list_jobs",
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
            tool="list_jobs",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@server.mcp.tool()
def get_job(job_id: int, fields: str | None = None) -> str:
    """Get details for a specific job order by ID.

    Args:
        job_id: The JobOrder ID
        fields: Comma-separated fields to return (default: all common fields)

    Returns:
        JSON object with job details
    """
    start_time = time.perf_counter()
    args = {"job_id": job_id, "fields": fields}

    decision = permissions.check("get_job")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_job",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("get_job", decision)

    try:
        client = server.get_client()
        result_data = client.get(entity="JobOrder", entity_id=job_id, fields=fields)
        result = server.format_response(result_data)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_job",
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
            tool="get_job",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result
