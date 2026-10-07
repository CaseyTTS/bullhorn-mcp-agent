"""Permission checks for Bullhorn MCP tool invocations.

Phase 1 ships this as a hardcoded default-allow pass-through: every tool and
operation is permitted and never requires approval. The signature is
deliberately stable so a later phase can swap in real, config-driven policy
logic without changing this function's signature or any of its callers.

Phase 5A: in ``local`` mode this is still exactly the default allow. In
``shared`` mode the decision comes from the caller's identity context
(``identity.principal``), in this order:

1. no acceptable principal -> deny ``identity_required``;
2. ``workspace_only`` caller and a tool outside the Tier 2 allowlist
   ``{bullhorn_session, setup_status}`` -> deny ``bullhorn_auth_required``
   (Amendment A2-2: deny by default, including every tool registered later);
3. ``service`` caller and a write, or any tool not on the service read list
   -> deny ``service_identity_read_only`` (D-5A-12, deny by default);
4. ``upload_candidate_resume`` -> deny ``local_file_paths_unsupported_in_shared_mode`` (D-5A-13).
"""

from dataclasses import dataclass


@dataclass
class PermissionDecision:
    """The result of a permission check for a tool invocation."""

    allowed: bool
    requires_approval: bool
    reason: str | None = None


# D-5A-12: the only tools a service principal may call (reads). Everything else is denied.
SERVICE_READ_TOOLS = frozenset(
    {
        "connection_status", "list_jobs", "get_job", "list_candidates", "get_candidate", "get_candidate_files",
        "get_recent_placements", "search_entities", "query_entities",
        "setup_status", "discover_schema", "get_mapping_profile", "manage_mapping_profile",
        "get_notes", "bullhorn_session",
    }
)
WRITE_OPERATIONS = frozenset({"write", "session"})


def _deny(reason: str) -> PermissionDecision:
    return PermissionDecision(allowed=False, requires_approval=False, reason=reason)


def _shared_check(tool: str, operation: str) -> PermissionDecision:
    from ..identity.principal import TIER2_ALLOWLIST, current_tier

    try:
        tier = current_tier()
    except Exception:  # any failure to resolve the caller fails closed
        tier = None
    if tier not in ("bullhorn_user", "workspace_only", "service"):
        return _deny("identity_required")
    if tier == "workspace_only" and tool not in TIER2_ALLOWLIST:
        return _deny("bullhorn_auth_required")
    if tier == "service" and (tool not in SERVICE_READ_TOOLS or operation in WRITE_OPERATIONS):
        return _deny("service_identity_read_only")
    if tool == "upload_candidate_resume":
        return _deny("local_file_paths_unsupported_in_shared_mode")
    return PermissionDecision(allowed=True, requires_approval=False)


def check(tool: str, operation: str = "read") -> PermissionDecision:
    """Check whether a tool invocation is permitted.

    ``local`` mode: always returns an allow decision that does not require
    approval, regardless of ``tool`` or ``operation`` (Phase 1 behaviour).
    ``shared`` mode: see the module docstring.
    """
    from ..identity import deploy

    if not deploy.is_shared():
        return PermissionDecision(allowed=True, requires_approval=False)
    return _shared_check(tool, operation)
