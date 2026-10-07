"""Phase 5B through the setup tools: the HV guard, discovery, drift and setup_status (AC-3, AC-5..AC-7, AC-11, AC-12)."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.config import BullhornConfig
from bullhorn_mcp.notes import action_discovery
from bullhorn_mcp.notes.action_types import REQUIREMENT
from bullhorn_mcp.tenant.state import compute_setup_state
from bullhorn_mcp.tools import setup as setup_tools

from ._tenant_helpers import CREDENTIALS, NOW, REST_URL, tenant_payloads, tree_bytes
from .test_tools_notes import APPROVED_ADDITIVE_TOOLS

AUTH_URL = "https://auth.bullhornstaffing.com"
LOGIN_URL = "https://rest.bullhornstaffing.com"
FIXTURES = Path(__file__).parent / "fixtures" / "notes5b"
OPTIONS = json.loads((FIXTURES / "note_action_options.json").read_text(encoding="utf-8"))["options"]
SETTINGS_BODY = json.loads((FIXTURES / "settings_comment_action_list.json").read_text(encoding="utf-8"))
SETTINGS_URL = f"{REST_URL}/settings/commentActionList"

# The 4A pins (tests/test_tools_setup.py NEW_SCHEMAS), repeated here: 5B must not change them (AC-3).
PINNED = {
    "setup_status": {
        "properties": {"check_connection": {"default": True, "title": "Check Connection", "type": "boolean"}},
        "title": "setup_statusArguments",
        "type": "object",
    },
    "discover_schema": {
        "properties": {
            "entities": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": None, "title": "Entities"}
        },
        "title": "discover_schemaArguments",
        "type": "object",
    },
}
IDENTITY_PARAMS = {"actor", "user", "user_id", "userid", "identity", "as_user", "on_behalf_of", "impersonate", "corporate_user"}


def note_payloads(options=OPTIONS):
    payloads = tenant_payloads()
    for f in payloads["Note"]["fields"]:
        if f["name"] == "action" and options is not None:
            f["options"] = options
    return payloads


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BULLHORN_SETUP_STORE", str(root))
    monkeypatch.setenv("BULLHORN_MCP_ACTOR", "admin@example.com")
    monkeypatch.delenv("BULLHORN_SETUP_ADMINS", raising=False)
    monkeypatch.setattr(setup_tools, "_now", lambda: NOW)
    return root


@pytest.fixture
def client():
    config = BullhornConfig(client_id="id", client_secret="secret", username="user", password="pw", auth_url=AUTH_URL, login_url=LOGIN_URL)
    real = BullhornClient(BullhornAuth(config))
    with patch.object(server, "get_client", return_value=real):
        yield real


@pytest.fixture
def settings_on(monkeypatch):
    monkeypatch.setattr(action_discovery, "SETTINGS_ACTION_SOURCE_VERIFIED", True)


def _mock_login():
    respx.get(f"{AUTH_URL}/oauth/authorize").mock(
        return_value=httpx.Response(302, headers={"location": "https://callback.example.com?code=c1"})
    )
    respx.post(f"{AUTH_URL}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 600})
    )
    respx.get(f"{LOGIN_URL}/rest-services/login").mock(return_value=httpx.Response(200, json={"BhRestToken": "t", "restUrl": REST_URL}))


def _mock_meta(payloads):
    for entity, payload in payloads.items():
        respx.get(f"{REST_URL}/meta/{entity}").mock(return_value=httpx.Response(200, json=payload))


def _json(text):
    assert not text.startswith("ERROR"), text
    return json.loads(text)


def _commit(changes):
    p = _json(setup_tools.propose_mapping_changes(changes=changes))
    return _json(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision="approve"))


def _settings_calls():
    return [c for c in respx.calls if "/settings" in str(c.request.url)]


def _profile_bytes(store_dir):
    return {k: v for k, v in tree_bytes(store_dir).items() if k.startswith("versions") or k == "active.json"}


class TestRegistry:
    def test_nineteen_tools(self):
        assert len(server.mcp._tool_manager._tools) == 19 + len(APPROVED_ADDITIVE_TOOLS)

    @pytest.mark.parametrize("name", list(PINNED))
    def test_setup_tool_schemas_equal_4a_pins(self, name):
        assert server.mcp._tool_manager._tools[name].parameters == PINNED[name]

    def test_no_identity_parameter_anywhere(self):
        for name, tool in server.mcp._tool_manager._tools.items():
            params = {p.lower() for p in tool.parameters.get("properties", {})}
            assert not params & IDENTITY_PARAMS, name


class TestGuardOff:
    @respx.mock
    @pytest.mark.parametrize("entities", [None, ["note"], ["Note"], ["note", "job"]])
    def test_no_settings_request(self, store_dir, client, entities):
        _mock_login()
        _mock_meta(note_payloads())
        settings = respx.get(url__regex=r".*/settings.*").mock(return_value=httpx.Response(200, json=SETTINGS_BODY))
        out = _json(setup_tools.discover_schema(entities=entities))
        assert settings.call_count == 0 and not _settings_calls()
        drift = out["report"]["note_action_drift"]
        assert drift["source_unresolved"] == ["settings"] and drift["sources"] == {"meta": "verified", "settings": "unresolved"}
        assert drift["new_values"] == ["Test Action A", "Test Action B", "Test Action C"]
        stored = json.loads((store_dir / "discovery" / "latest.json").read_text())
        assert stored["snapshot"]["note_actions"]["sources"]["settings"] == {
            "status": "unresolved", "values": [], "warning": "HV-D1..D3 unresolved: /settings/commentActionList is not consulted"
        }

    @respx.mock
    def test_no_settings_request_from_any_tool(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        respx.get(url__regex=r".*/settings.*").mock(return_value=httpx.Response(200, json=SETTINGS_BODY))
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action A"]}])
        for out in (
            setup_tools.setup_status(),
            setup_tools.discover_schema(entities=["settings"]),
            setup_tools.discover_schema(entities=["commentActionList"]),
            setup_tools.manage_mapping_profile(action="validate"),
            setup_tools.get_mapping_profile(),
            setup_tools.propose_mapping_changes(changes=[{"op": "apply_discovered_note_actions", "values": ["Test Action D"]}]),
        ):
            assert isinstance(out, str)
        assert not _settings_calls()

    @respx.mock
    def test_settings_value_is_never_adopted_while_unresolved(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"}])
        out = setup_tools.propose_mapping_changes(changes=[{"op": "apply_discovered_note_actions", "values": ["Test Action D"]}])
        assert out.startswith("ERROR:") and "not a value of a verified discovery source" in out

    @respx.mock
    def test_administrator_mappings_untouched(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([
            {"op": "init_tenant", "tenant_id": "acme"},
            {"op": "set_value_mapping", "key": "note.action.admin", "target": {"kind": "note_action", "semantic": None},
             "bullhorn_field": "action", "values": ["Test Action A"]},
        ])
        before = _profile_bytes(store_dir)
        setup_tools.discover_schema()
        assert _profile_bytes(store_dir) == before


class TestGuardOnMockedVerifiedShape:
    @respx.mock
    def test_settings_values_discovered(self, store_dir, client, settings_on):
        _mock_login()
        _mock_meta(note_payloads(options=None))
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=SETTINGS_BODY))
        out = _json(setup_tools.discover_schema())
        drift = out["report"]["note_action_drift"]
        assert drift["sources"] == {"meta": "unverifiable", "settings": "verified"}
        assert drift["new_values"] == ["Test Action A", "Test Action B", "Test Action D"]
        assert all(c.request.method == "GET" for c in respx.calls if str(c.request.url).startswith(REST_URL))
        assert len(_settings_calls()) == 1
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action D"]}])
        recs = _json(setup_tools.get_mapping_profile())["profile"]["value_mappings"]
        assert [(r["values"], r["discovery_source"], r["source"]) for r in recs] == [(["Test Action D"], "settings", "discovered")]

    @respx.mock
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(403, text='{"errorMessage":"Test Action Hidden"}'),
            httpx.Response(500, text="E" * 300_000),
            httpx.Response(200, json={"commentActionList": "Test Action A;Test Action B"}),
            httpx.Response(200, json=[1, 2]),
            httpx.Response(200, text="A" * 300_000),
        ],
        ids=["403", "500-huge", "delimited", "non-object", "oversized"],
    )
    def test_bad_settings_are_unverifiable_never_raise_never_adopt(self, store_dir, client, settings_on, response):
        _mock_login()
        _mock_meta(note_payloads(options=None))
        respx.get(SETTINGS_URL).mock(return_value=response)
        text = setup_tools.discover_schema()
        out = _json(text)
        drift = out["report"]["note_action_drift"]
        assert drift["sources"]["settings"] == "unverifiable" and len(drift["warnings"]["settings"]) <= 300
        assert "Test Action Hidden" not in text and drift["new_values"] == []
        assert not (store_dir / "versions").exists()

    @respx.mock
    def test_discovery_never_mutates_profile(self, store_dir, client, settings_on):
        _mock_login()
        _mock_meta(note_payloads())
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=SETTINGS_BODY))
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action A", "Test Action D"]}])
        before = _profile_bytes(store_dir)
        setup_tools.discover_schema()
        setup_tools.discover_schema(entities=["note"])
        assert _profile_bytes(store_dir) == before


class TestDriftThroughTools:
    @respx.mock
    def test_stale_value_requires_revalidation_and_nothing_changes(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action A", "Test Action B"]}])
        assert _json(setup_tools.setup_status(check_connection=False))["state"] == "setup_valid"
        before = _profile_bytes(store_dir)

        respx.routes.clear()
        _mock_login()
        _mock_meta(note_payloads(options=[{"value": "Test Action A", "label": "A"}, {"value": "Test Action E", "label": "E"}]))
        out = _json(setup_tools.discover_schema(entities=["note"]))
        drift = out["report"]["note_action_drift"]
        assert drift["stale_values"] == ["Test Action B"] and drift["new_values"] == ["Test Action E"]
        assert out["drift_unresolved"] is True
        assert _json(setup_tools.setup_status(check_connection=False))["state"] == "setup_revalidation_required"
        assert _profile_bytes(store_dir) == before

    @respx.mock
    def test_meta_without_options_is_unverifiable_not_stale(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action A"]}])
        respx.routes.clear()
        _mock_login()
        _mock_meta(note_payloads(options=None))
        drift = _json(setup_tools.discover_schema(entities=["note"]))["report"]["note_action_drift"]
        assert drift["unverifiable"] == ["Test Action A"] and drift["stale_values"] == []

    @respx.mock
    def test_reactivatable(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"},
                 {"op": "apply_discovered_note_actions", "values": ["Test Action A"]}])
        key = _json(setup_tools.get_mapping_profile())["profile"]["value_mappings"][0]["key"]
        _commit([{"op": "deactivate_value_mapping", "key": key}])
        drift = _json(setup_tools.discover_schema(entities=["note"]))["report"]["note_action_drift"]
        assert drift["reactivatable"] == ["Test Action A"] and drift["reactivatable_keys"] == [key]
        _commit([{"op": "reactivate_value_mapping", "key": key}])
        assert _json(setup_tools.get_mapping_profile())["profile"]["value_mappings"][0]["active"] is True


class TestSetupStatusDetails:
    @respx.mock
    def test_requirement_details(self, store_dir, client):
        _mock_login()
        _mock_meta(note_payloads())
        setup_tools.discover_schema()
        _commit([{"op": "init_tenant", "tenant_id": "acme"}])
        status = _json(setup_tools.setup_status(check_connection=False))
        assert status["requirement_details"]["notes.create"] == [REQUIREMENT]
        assert set(status["requirement_details"]) == set(status["capabilities"])
        assert status["missing_requirements"] == list(compute_setup_state(os.environ, None, now=NOW).missing_requirements)
        assert REQUIREMENT not in status["missing_requirements"]  # the 4A aggregation is unchanged
        assert all(not m.startswith("state:") for items in status["requirement_details"].values() for m in items)

        _commit([{"op": "apply_discovered_note_actions", "values": ["Test Action A"]}])
        status = _json(setup_tools.setup_status(check_connection=False))
        assert status["requirement_details"]["notes.create"] == []
        assert status["missing_requirements"] == list(compute_setup_state(os.environ, None, now=NOW).missing_requirements)

    def test_without_store_or_credentials(self, monkeypatch):
        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("BULLHORN_SETUP_STORE", raising=False)
        status = _json(setup_tools.setup_status())
        assert status["state"] == "disconnected" and status["requirement_details"]["notes.create"] == [REQUIREMENT]
        expected = compute_setup_state(os.environ, None).to_dict()
        assert {k: v for k, v in status.items() if k != "requirement_details"} == expected


SENTINEL = "SentinelPayload5B"


class TestNoPayloadEcho:
    """Sec-N2: discovery warnings carry only the type and length of a bad /settings item, never its content."""

    @respx.mock
    @pytest.mark.parametrize(
        "body",
        [
            {"commentActionList": SENTINEL},
            {"commentActionList": {"k": SENTINEL}},
            {"commentActionList": ["Test Action A", {"x": SENTINEL}]},
            {"commentActionList": [SENTINEL * 20]},
            {"commentActionList": [SENTINEL, 5]},
            {"commentActionList": [SENTINEL, "   "]},
        ],
        ids=["string", "object", "nested", "over-long", "mixed", "blank"],
    )
    def test_sentinel_never_echoed(self, store_dir, client, settings_on, body, caplog):
        caplog.set_level("DEBUG")
        _mock_login()
        _mock_meta(note_payloads(options=None))
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=body))
        out = setup_tools.discover_schema()
        drift = _json(out)["report"]["note_action_drift"]
        assert drift["sources"]["settings"] == "unverifiable" and drift["warnings"]["settings"]
        stored = (store_dir / "discovery" / "latest.json").read_text(encoding="utf-8")
        status = setup_tools.setup_status(check_connection=False)
        for text in (out, stored, status, caplog.text):
            assert SENTINEL not in text
