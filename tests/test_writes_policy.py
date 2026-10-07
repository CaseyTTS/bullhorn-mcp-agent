"""Phase 4B D-4B-1/2/3: write-scope policy (default off), actor and approvers."""

import pytest

from bullhorn_mcp.crosscutting import permissions
from bullhorn_mcp.writes.policy import check_scope, direct_mode, enabled_scopes

ACTOR = "a@b.c"


class TestScope:
    def test_default_off(self):
        decision = check_scope("note.create", {"BULLHORN_MCP_ACTOR": ACTOR})
        assert not decision.allowed and decision.missing == ("scope:note.create",)

    @pytest.mark.parametrize("raw", ["", " ", "note.read", "Note.Create", "note.create.all", "*"])
    def test_only_exact_scope_enables(self, raw):
        assert "note.create" not in enabled_scopes({"BULLHORN_ENABLED_WRITE_SCOPES": raw})

    def test_enabled(self):
        decision = check_scope("note.create", {"BULLHORN_ENABLED_WRITE_SCOPES": "x, note.create", "BULLHORN_MCP_ACTOR": ACTOR})
        assert decision.allowed and decision.actor == ACTOR

    @pytest.mark.parametrize("actor", [None, "", "  ", "bad\nactor", "x" * 200])
    def test_actor_required(self, actor):
        e = {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create"}
        if actor is not None:
            e["BULLHORN_MCP_ACTOR"] = actor
        assert "env:BULLHORN_MCP_ACTOR" in check_scope("note.create", e).missing

    def test_setup_admins_does_not_apply(self):
        e = {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create", "BULLHORN_MCP_ACTOR": ACTOR, "BULLHORN_SETUP_ADMINS": "z@b.c"}
        assert check_scope("note.create", e).allowed

    def test_approvers(self):
        e = {"BULLHORN_ENABLED_WRITE_SCOPES": "note.create", "BULLHORN_MCP_ACTOR": ACTOR, "BULLHORN_WRITE_APPROVERS": "z@b.c"}
        assert check_scope("note.create", e).allowed  # a preview is not an approval
        assert check_scope("note.create", e, approving=True).missing == ("approver:BULLHORN_WRITE_APPROVERS",)
        assert check_scope("note.create", {**e, "BULLHORN_WRITE_APPROVERS": "z@b.c,a@b.c"}, approving=True).allowed

    @pytest.mark.parametrize("raw,expected", [("direct", True), (" direct ", True), ("Direct", False), ("", False), ("preview", False)])
    def test_direct_mode(self, raw, expected):
        assert direct_mode({"BULLHORN_NOTE_CREATE_MODE": raw}) is expected

    def test_legacy_permissions_unchanged(self):
        decision = permissions.check("create_note", "write")
        assert decision.allowed and not decision.requires_approval
