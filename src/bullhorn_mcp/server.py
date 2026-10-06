"""Bullhorn CRM MCP Server - Query jobs and candidates via natural language."""

import json
import os
import time
from mcp.server.fastmcp import FastMCP

from .config import BullhornConfig
from .auth import BullhornAuth, AuthenticationError
from .client import BullhornClient, BullhornAPIError
from .crosscutting import approval, audit, dryrun, permissions
from datetime import datetime, timedelta, timezone

# Initialize MCP server
mcp = FastMCP(
    "Bullhorn CRM",
    instructions="Query Bullhorn CRM data - jobs, candidates, and placements",
)

# Global client instance (initialized on first use)
_client: BullhornClient | None = None


def get_client() -> BullhornClient:
    """Get or create the Bullhorn API client."""
    global _client
    if _client is None:
        config = BullhornConfig.from_env()
        auth = BullhornAuth(config)
        _client = BullhornClient(auth)
    return _client


def format_response(data: list | dict) -> str:
    """Format API response as readable JSON."""
    return json.dumps(data, indent=2, default=str)


def _permission_denied_message(tool: str, decision: permissions.PermissionDecision) -> str:
    """Build the denial string returned when a permission check disallows a call."""
    reason = f": {decision.reason}" if decision.reason else ""
    return f"ERROR: permission denied for {tool}{reason}"


@mcp.tool()
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
        return _permission_denied_message("connection_status", decision)

    required_vars = [
        "BULLHORN_CLIENT_ID",
        "BULLHORN_CLIENT_SECRET",
        "BULLHORN_USERNAME",
        "BULLHORN_PASSWORD",
    ]

    missing = [name for name in required_vars if not os.getenv(name)]

    if missing:
        result = format_response({
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
        client = get_client()
        session = client.auth.session

        result = format_response({
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
        result = format_response({
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


@mcp.tool()
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
        return _permission_denied_message("list_jobs", decision)

    try:
        client = get_client()

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

        result = format_response(results)
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


@mcp.tool()
def list_candidates(
    query: str | None = None,
    status: str | None = None,
    limit: int = 20,
    fields: str | None = None,
) -> str:
    """List and filter candidates from Bullhorn CRM.

    Args:
        query: Lucene search query (e.g., "lastName:Smith" or "skillSet:Python")
        status: Filter by candidate status
        limit: Maximum number of results (1-500, default 20)
        fields: Comma-separated fields to return

    Returns:
        JSON array of candidates

    Examples:
        - list_candidates() - Get recent candidates
        - list_candidates(query="skillSet:Python") - Find Python developers
        - list_candidates(query="lastName:Smith AND status:Active")
        - list_candidates(status="Active", limit=50)
    """
    start_time = time.perf_counter()
    args = {"query": query, "status": status, "limit": limit, "fields": fields}

    decision = permissions.check("list_candidates")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="list_candidates",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return _permission_denied_message("list_candidates", decision)

    try:
        client = get_client()

        # Build search query
        search_query = query or "isDeleted:0"
        if status:
            search_query = f"({search_query}) AND status:\"{status}\""

        results = client.search(
            entity="Candidate",
            query=search_query,
            fields=fields,
            count=limit,
            sort="-dateAdded",
        )

        result = format_response(results)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="list_candidates",
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
            tool="list_candidates",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@mcp.tool()
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
        return _permission_denied_message("get_job", decision)

    try:
        client = get_client()
        result_data = client.get(entity="JobOrder", entity_id=job_id, fields=fields)
        result = format_response(result_data)
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


@mcp.tool()
def get_candidate(candidate_id: int, fields: str | None = None) -> str:
    """Get details for a specific candidate by ID.

    Args:
        candidate_id: The Candidate ID
        fields: Comma-separated fields to return (default: all common fields)

    Returns:
        JSON object with candidate details
    """
    start_time = time.perf_counter()
    args = {"candidate_id": candidate_id, "fields": fields}

    decision = permissions.check("get_candidate")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_candidate",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return _permission_denied_message("get_candidate", decision)

    try:
        client = get_client()
        result_data = client.get(entity="Candidate", entity_id=candidate_id, fields=fields)
        result = format_response(result_data)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_candidate",
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
            tool="get_candidate",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@mcp.tool()
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
        return _permission_denied_message("get_recent_placements", decision)

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
        client = get_client()

        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        start_ms = int(start_date.timestamp() * 1000)

        results = client.query(
            entity="Placement",
            where=f"dateAdded >= {start_ms}",
            fields=None,
            count=limit,
            order_by="-dateAdded",
        )

        result = format_response(results)
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


@mcp.tool()
def get_candidate_files(candidate_id: int) -> str:
    """List file attachments for a Bullhorn Candidate."""
    start_time = time.perf_counter()
    args = {"candidate_id": candidate_id}

    decision = permissions.check("get_candidate_files")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_candidate_files",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return _permission_denied_message("get_candidate_files", decision)

    try:
        client = get_client()
        result_data = client.get_candidate_files(candidate_id)
        result = format_response(result_data)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_candidate_files",
            args=args,
            result_summary="ok",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    except (AuthenticationError, BullhornAPIError, ValueError) as e:
        result = f"ERROR: {e}"
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="get_candidate_files",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@mcp.tool()
def upload_candidate_resume(
    candidate_id: int,
    file_path: str,
    file_type: str = "SAMPLE",
    external_id: str = "Portfolio",
    dry_run: bool = False,
) -> str:
    """Upload a local resume file to a Bullhorn Candidate.

    Args:
        candidate_id: The Candidate ID
        file_path: Local filesystem path to the resume file
        file_type: Bullhorn file type classification (default: "SAMPLE")
        external_id: Bullhorn external ID for the file (default: "Portfolio")
        dry_run: If True, validate inputs and return a preview of what would
            be uploaded without performing the upload

    Returns:
        JSON object with the upload result, or - when dry_run is True - a
        preview of what would have been uploaded.
    """
    start_time = time.perf_counter()
    args = {
        "candidate_id": candidate_id,
        "file_path": file_path,
        "file_type": file_type,
        "external_id": external_id,
        "dry_run": dry_run,
    }

    decision = permissions.check("upload_candidate_resume", operation="write")
    if not decision.allowed:
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="upload_candidate_resume",
            args=args,
            result_summary="denied",
            duration_ms=duration_ms,
            success=False,
        )
        return _permission_denied_message("upload_candidate_resume", decision)

    if dry_run:
        try:
            client = get_client()
            preview_data = client.describe_resume_upload(
                candidate_id=candidate_id,
                file_path=file_path,
                file_type=file_type,
                external_id=external_id,
            )
            preview = dryrun.render_preview("upload_candidate_resume", preview_data)
            result = format_response(preview)
            duration_ms = (time.perf_counter() - start_time) * 1000
            audit.log_invocation(
                tool="upload_candidate_resume",
                args=args,
                result_summary="dry_run_preview",
                duration_ms=duration_ms,
                success=True,
            )
            return result

        except (AuthenticationError, BullhornAPIError, ValueError) as e:
            result = f"ERROR: {e}"
            duration_ms = (time.perf_counter() - start_time) * 1000
            audit.log_invocation(
                tool="upload_candidate_resume",
                args=args,
                result_summary=f"error: {e}",
                duration_ms=duration_ms,
                success=False,
            )
            return result

    if decision.requires_approval:
        token = approval.create_pending(
            "upload_candidate_resume",
            {
                "candidate_id": candidate_id,
                "file_path": file_path,
                "file_type": file_type,
                "external_id": external_id,
            },
        )
        result = format_response({
            "pending_approval": True,
            "approval_token": token,
            "message": "This operation requires approval before it will be executed.",
        })
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="upload_candidate_resume",
            args=args,
            result_summary="pending_approval",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    try:
        client = get_client()
        result_data = client.upload_candidate_resume(
            candidate_id=candidate_id,
            file_path=file_path,
            file_type=file_type,
            external_id=external_id,
        )
        result = format_response(result_data)
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="upload_candidate_resume",
            args=args,
            result_summary="ok",
            duration_ms=duration_ms,
            success=True,
        )
        return result

    except (AuthenticationError, BullhornAPIError, ValueError) as e:
        result = f"ERROR: {e}"
        duration_ms = (time.perf_counter() - start_time) * 1000
        audit.log_invocation(
            tool="upload_candidate_resume",
            args=args,
            result_summary=f"error: {e}",
            duration_ms=duration_ms,
            success=False,
        )
        return result


@mcp.tool()
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
        return _permission_denied_message("search_entities", decision)

    try:
        client = get_client()

        results = client.search(
            entity=entity,
            query=query,
            fields=fields,
            count=limit,
        )

        result = format_response(results)
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


@mcp.tool()
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
        return _permission_denied_message("query_entities", decision)

    try:
        client = get_client()

        results = client.query(
            entity=entity,
            where=where,
            fields=fields,
            count=limit,
            order_by=order_by,
        )

        result = format_response(results)
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


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
