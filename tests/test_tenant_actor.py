"""D-4A-2: the actor comes only from the environment."""

import pytest

from bullhorn_mcp.tenant.actor import resolve_actor


class TestResolveActor:
    def test_unset(self):
        res = resolve_actor({})
        assert res.actor is None and not res.allowed and "BULLHORN_MCP_ACTOR" in res.reason

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank(self, value):
        assert not resolve_actor({"BULLHORN_MCP_ACTOR": value}).allowed

    def test_set_without_admin_list(self):
        res = resolve_actor({"BULLHORN_MCP_ACTOR": " casey "})
        assert res.actor == "casey" and res.allowed

    def test_admin_list_allows(self):
        res = resolve_actor({"BULLHORN_MCP_ACTOR": "casey", "BULLHORN_SETUP_ADMINS": "alex, casey"})
        assert res.allowed

    def test_admin_list_denies(self):
        res = resolve_actor({"BULLHORN_MCP_ACTOR": "mallory", "BULLHORN_SETUP_ADMINS": "alex,casey"})
        assert res.actor == "mallory" and not res.allowed and "BULLHORN_SETUP_ADMINS" in res.reason

    @pytest.mark.parametrize("value", ["a" * 129, "x\ny", "bad\x00actor", "<script>"])
    def test_invalid_identifier(self, value):
        res = resolve_actor({"BULLHORN_MCP_ACTOR": value})
        assert not res.allowed and res.actor is None
