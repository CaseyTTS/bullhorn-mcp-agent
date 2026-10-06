"""Tests for crosscutting permission checks."""

import inspect

from bullhorn_mcp.crosscutting import permissions


class TestCheck:
    """Phase 1's check() is a hardcoded default-allow pass-through."""

    def test_always_allows_in_phase_one(self):
        decision = permissions.check("list_jobs")

        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.reason is None

    def test_allows_regardless_of_tool_name(self):
        for tool in [
            "connection_status",
            "list_jobs",
            "list_candidates",
            "get_job",
            "get_candidate",
            "get_recent_placements",
            "get_candidate_files",
            "upload_candidate_resume",
            "search_entities",
            "query_entities",
            "some_future_tool",
        ]:
            decision = permissions.check(tool)
            assert decision.allowed is True
            assert decision.requires_approval is False

    def test_allows_regardless_of_operation(self):
        decision = permissions.check("upload_candidate_resume", operation="write")

        assert decision.allowed is True
        assert decision.requires_approval is False

    def test_default_operation_is_read(self):
        sig = inspect.signature(permissions.check)
        assert sig.parameters["operation"].default == "read"


class TestPermissionDecision:
    def test_defaults_reason_to_none(self):
        decision = permissions.PermissionDecision(allowed=True, requires_approval=False)
        assert decision.reason is None

    def test_can_carry_a_reason(self):
        decision = permissions.PermissionDecision(
            allowed=False, requires_approval=False, reason="blocked by policy"
        )
        assert decision.reason == "blocked by policy"
