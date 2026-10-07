"""Bullhorn CRM MCP Server - Query jobs and candidates via natural language."""

import json
from mcp.server.fastmcp import FastMCP

from .config import BullhornConfig
from .auth import BullhornAuth
from .bullhorn.client import BullhornClient
# `approval`, `audit`, and `dryrun` are not called directly in this module
# anymore (that logic lives in tools/*.py), but they stay imported here -
# and accessible as `server.approval`, `server.audit`, `server.dryrun` - so
# that `unittest.mock.patch.object(server.permissions, "check", ...)`-style
# patches in the existing test suite keep intercepting the same shared
# module objects the tools/*.py modules call through.
from .crosscutting import approval, audit, dryrun, permissions  # noqa: F401

# Initialize MCP server
# (explicitly annotated so mypy can resolve `server.mcp`'s type from inside
# tools/*.py, across the server <-> tools circular import)
mcp: FastMCP = FastMCP(
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


# Importing `tools` registers every @mcp.tool() as a side effect. This import
# must come after everything above it is defined (mcp, get_client,
# format_response, _permission_denied_message) because each tools/*.py module
# calls back into this module via `server.<name>` at call time. See
# `src/bullhorn_mcp/__init__.py` for the other half of the order-independence
# guarantee: it imports `server` first thing, so no matter which module a
# caller touches first (`bullhorn_mcp`, `bullhorn_mcp.tools.jobs`, etc.),
# `server` is always fully initialized before anything else runs.
from . import tools  # noqa: F401,E402

# Re-export each tool function by name so `server.list_jobs(...)`-style
# direct calls (used throughout the existing test suite) keep working
# unchanged.
from .tools.system import connection_status  # noqa: F401,E402
from .tools.jobs import list_jobs, get_job  # noqa: F401,E402
from .tools.candidates import (  # noqa: F401,E402
    list_candidates,
    get_candidate,
    get_candidate_files,
    upload_candidate_resume,
)
from .tools.placements import get_recent_placements  # noqa: F401,E402
from .tools.generic import search_entities, query_entities  # noqa: F401,E402


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
