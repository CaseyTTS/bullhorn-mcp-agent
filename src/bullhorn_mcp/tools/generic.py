"""The generic entity MCP tools: search_entities, query_entities."""

import time

from .. import server
from ..auth import AuthenticationError
from ..bullhorn.client import BullhornAPIError
from ..crosscutting import audit, permissions


@server.mcp.tool()
def search_entities(
    entity: str,
    query: str,
    limit: int = 20,
    fields: str | None = None,
) -> str:
    """Search any Bullhorn entity type using Lucene query syntax.

    Args:
        entity: Entity type (JobOrder, Candidate, Placement, ClientCorporation, ClientContact, etc.)
        query: Lucene search query
        limit: Maximum number of results (1-500, default 20)
        fields: Comma-separated fields to return

    Returns:
        JSON array of matching entities

    Examples:
        - search_entities(entity="Placement", query="status:Approved")
        - search_entities(entity="ClientCorporation", query="name:Acme*")
        - search_entities(entity="JobSubmission", query="jobOrder.id:12345")
    """
    start_time = time.perf_counter()
    args = {"entity": entity, "query": query, "limit": limit, "fields": fields}

    decision = permissions.check("search_entities")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="search_entities",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("search_entities", decision)

    try:
        client = server.get_client()

        results = client.search(
            entity=entity,
            query=query,
            fields=fields,
            count=limit,
        )

        result = server.format_response(results)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="search_entities",
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
            tool="search_entities",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@server.mcp.tool()
def query_entities(
    entity: str,
    where: str,
    limit: int = 20,
    fields: str | None = None,
    order_by: str | None = None,
) -> str:
    """Query Bullhorn entities using SQL-like WHERE syntax.

    Args:
        entity: Entity type (JobOrder, Candidate, etc.)
        where: WHERE clause (e.g., "salary > 100000 AND status='Active'")
        limit: Maximum number of results (1-500, default 20)
        fields: Comma-separated fields to return
        order_by: Sort order (e.g., "-dateAdded" for newest first)

    Returns:
        JSON array of matching entities

    Examples:
        - query_entities(entity="JobOrder", where="salary > 100000")
        - query_entities(entity="Candidate", where="status='Active'", order_by="-dateAdded")
    """
    start_time = time.perf_counter()
    args = {
        "entity": entity,
        "where": where,
        "limit": limit,
        "fields": fields,
        "order_by": order_by,
    }

    decision = permissions.check("query_entities")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="query_entities",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return server._permission_denied_message("query_entities", decision)

    try:
        client = server.get_client()

        results = client.query(
            entity=entity,
            where=where,
            fields=fields,
            count=limit,
            order_by=order_by,
        )

        result = server.format_response(results)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="query_entities",
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
            tool="query_entities",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result
