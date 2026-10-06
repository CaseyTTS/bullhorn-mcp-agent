"""Tests for the in-memory approval gate."""

import pytest

from bullhorn_mcp.crosscutting import approval
from bullhorn_mcp.crosscutting.approval import ApprovalGate


class TestApprovalGate:
    """Standalone unit tests for the ApprovalGate class itself."""

    def test_create_pending_returns_a_token(self):
        gate = ApprovalGate()

        token = gate.create_pending("upload_candidate_resume", {"candidate_id": 1})

        assert isinstance(token, str)
        assert token

    def test_create_then_confirm_returns_the_pending_entry(self):
        gate = ApprovalGate()

        token = gate.create_pending("upload_candidate_resume", {"candidate_id": 1})
        result = gate.confirm(token)

        assert result["tool"] == "upload_candidate_resume"
        assert result["payload"] == {"candidate_id": 1}

    def test_confirm_consumes_the_token(self):
        gate = ApprovalGate()
        token = gate.create_pending("tool_a", {"x": 1})

        gate.confirm(token)

        with pytest.raises(KeyError):
            gate.confirm(token)

    def test_confirm_unknown_token_raises_key_error(self):
        gate = ApprovalGate()

        with pytest.raises(KeyError):
            gate.confirm("not-a-real-token")

    def test_reject_removes_pending_entry(self):
        gate = ApprovalGate()
        token = gate.create_pending("tool_a", {"x": 1})

        gate.reject(token)

        assert gate.get(token) is None
        with pytest.raises(KeyError):
            gate.confirm(token)

    def test_reject_unknown_token_raises_key_error(self):
        gate = ApprovalGate()

        with pytest.raises(KeyError):
            gate.reject("not-a-real-token")

    def test_get_peeks_without_consuming(self):
        gate = ApprovalGate()
        token = gate.create_pending("tool_a", {"x": 1})

        peeked = gate.get(token)
        assert peeked is not None
        assert peeked["payload"] == {"x": 1}

        # Still present after peek - confirm must still succeed.
        confirmed = gate.confirm(token)
        assert confirmed["payload"] == {"x": 1}

    def test_get_unknown_token_returns_none(self):
        gate = ApprovalGate()

        assert gate.get("not-a-real-token") is None

    def test_tokens_are_unique(self):
        gate = ApprovalGate()

        token_a = gate.create_pending("tool_a", {"x": 1})
        token_b = gate.create_pending("tool_a", {"x": 2})

        assert token_a != token_b

    def test_gate_instances_do_not_share_state(self):
        gate_a = ApprovalGate()
        gate_b = ApprovalGate()

        token = gate_a.create_pending("tool_a", {"x": 1})

        assert gate_b.get(token) is None


class TestModuleLevelFunctions:
    """The module-level convenience functions mirror permissions.py's
    module-function style, backed by one default ApprovalGate."""

    def test_module_functions_share_a_default_gate(self):
        token = approval.create_pending("tool_a", {"x": 1})

        result = approval.confirm(token)

        assert result["payload"] == {"x": 1}

        with pytest.raises(KeyError):
            approval.confirm(token)

    def test_module_reject(self):
        token = approval.create_pending("tool_a", {"x": 2})

        approval.reject(token)

        assert approval.get(token) is None

    def test_module_confirm_unknown_token_raises_key_error(self):
        with pytest.raises(KeyError):
            approval.confirm("definitely-not-a-token")

    def test_module_reject_unknown_token_raises_key_error(self):
        with pytest.raises(KeyError):
            approval.reject("definitely-not-a-token")

    def test_module_get_unknown_token_returns_none(self):
        assert approval.get("definitely-not-a-token") is None
