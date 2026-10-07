"""The candidate-related MCP tools: list_candidates, get_candidate,
get_candidate_files, upload_candidate_resume."""

import time

from .. import server
from ..auth import AuthenticationError
from ..bullhorn.client import BullhornAPIError
from ..crosscutting import approval, audit, dryrun, permissions


@server.mcp.tool()
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
        return server._permission_denied_message("list_candidates", decision)

    try:
        client = server.get_client()

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

        result = server.format_response(results)
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


@server.mcp.tool()
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
        return server._permission_denied_message("get_candidate", decision)

    try:
        client = server.get_client()
        result_data = client.get(entity="Candidate", entity_id=candidate_id, fields=fields)
        result = server.format_response(result_data)
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


@server.mcp.tool()
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
        return server._permission_denied_message("get_candidate_files", decision)

    try:
        client = server.get_client()
        result_data = client.get_candidate_files(candidate_id)
        result = server.format_response(result_data)
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


@server.mcp.tool()
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
        return server._permission_denied_message("upload_candidate_resume", decision)

    if dry_run:
        try:
            client = server.get_client()
            preview_data = client.describe_resume_upload(
                candidate_id=candidate_id,
                file_path=file_path,
                file_type=file_type,
                external_id=external_id,
            )
            preview = dryrun.render_preview("upload_candidate_resume", preview_data)
            result = server.format_response(preview)
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
        result = server.format_response({
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
        client = server.get_client()
        result_data = client.upload_candidate_resume(
            candidate_id=candidate_id,
            file_path=file_path,
            file_type=file_type,
            external_id=external_id,
        )
        result = server.format_response(result_data)
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
