"""The six Phase 4A MCP tools: registry/schemas (AC-6), HTTP boundary (AC-16, AC-18), no samples (AC-19)."""

import json
import logging
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.config import BullhornConfig
from bullhorn_mcp.tools import setup as setup_tools

from ._tenant_helpers import CREDENTIALS, NOW, REST_URL, JOB_EXTRA, tenant_payloads

AUTH_URL = "https://auth.bullhornstaffing.com"
LOGIN_URL = "https://rest.bullhornstaffing.com"
ORIGINAL_TOOLS = {
    "connection_status", "list_jobs", "list_candidates", "get_job", "get_candidate", "get_recent_placements",
    "get_candidate_files", "upload_candidate_resume", "search_entities", "query_entities",
}
NEW_SCHEMAS = {
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
    "get_mapping_profile": {
        "properties": {
            "view": {"default": "active", "title": "View", "type": "string"},
            "version": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Version"},
            "other_version": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Other Version"},
            "entity": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Entity"},
            "search": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Search"},
        },
        "title": "get_mapping_profileArguments",
        "type": "object",
    },
    "propose_mapping_changes": {
        "properties": {
            "changes": {"items": {"additionalProperties": True, "type": "object"}, "title": "Changes", "type": "array"},
            "base_version": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Base Version"},
        },
        "required": ["changes"],
        "title": "propose_mapping_changesArguments",
        "type": "object",
    },
    "commit_mapping_changes": {
        "properties": {
            "proposal_id": {"title": "Proposal Id", "type": "string"},
            "diff_hash": {"title": "Diff Hash", "type": "string"},
            "decision": {"title": "Decision", "type": "string"},
        },
        "required": ["proposal_id", "diff_hash", "decision"],
        "title": "commit_mapping_changesArguments",
        "type": "object",
    },
    "manage_mapping_profile": {
        "properties": {
            "action": {"title": "Action", "type": "string"},
            "path": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Path"},
            "format": {"default": "v2", "title": "Format", "type": "string"},
        },
        "required": ["action"],
        "title": "manage_mapping_profileArguments",
        "type": "object",
    },
}


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


def _mock_login():
    respx.get(f"{AUTH_URL}/oauth/authorize").mock(
        return_value=httpx.Response(302, headers={"location": "https://callback.example.com?code=c1"})
    )
    respx.post(f"{AUTH_URL}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 600})
    )
    respx.get(f"{LOGIN_URL}/rest-services/login").mock(
        return_value=httpx.Response(200, json={"BhRestToken": "t", "restUrl": REST_URL})
    )


def _mock_meta(payloads, status=200, body=None):
    for entity, payload in payloads.items():
        if body is not None:
            respx.get(f"{REST_URL}/meta/{entity}").mock(return_value=httpx.Response(status, text=body))
        else:
            respx.get(f"{REST_URL}/meta/{entity}").mock(return_value=httpx.Response(status, json=payload))


@pytest.fixture
def client():
    config = BullhornConfig(client_id="id", client_secret="secret", username="user", password="pw", auth_url=AUTH_URL, login_url=LOGIN_URL)
    real = BullhornClient(BullhornAuth(config))
    with patch.object(server, "get_client", return_value=real):
        yield real


def _assert_only_meta_and_login(calls):
    assert calls, "expected HTTP calls"
    for call in calls:
        url = str(call.request.url)
        method = call.request.method
        is_login = url.startswith(f"{AUTH_URL}/oauth/") or url.startswith(f"{LOGIN_URL}/rest-services/login")
        is_meta = method == "GET" and url.startswith(f"{REST_URL}/meta/")
        assert is_login or is_meta, (method, url)
        if is_meta:
            assert call.request.url.path.split("/meta/", 1)[0] == "/rest-services/abc123"


def _assert_bounded(text):
    assert all(len(line) <= 10_000 for line in text.splitlines())


def _json(text):
    assert not text.startswith("ERROR"), text
    return json.loads(text)


def _propose(changes, **kw):
    return _json(setup_tools.propose_mapping_changes(changes=changes, **kw))


def _commit(p, decision="approve"):
    return _json(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash=p["diff_hash"], decision=decision))


class TestRegistry:
    def test_sixteen_tools(self):
        names = set(server.mcp._tool_manager._tools)
        assert ORIGINAL_TOOLS | set(NEW_SCHEMAS) <= names

    @pytest.mark.parametrize("name", list(NEW_SCHEMAS))
    def test_new_schemas(self, name):
        assert server.mcp._tool_manager._tools[name].parameters == NEW_SCHEMAS[name]


class TestEndToEnd:
    @respx.mock
    def test_full_setup_flow(self, store_dir, client):
        _mock_login()
        _mock_meta(tenant_payloads())

        status = _json(setup_tools.setup_status())
        assert status["state"] == "connected_setup_required" and status["connection_checked"] is True

        discovered = _json(setup_tools.discover_schema())
        assert "job" in discovered["entities"]
        assert {"field": "customText12", "label": "Priority"}.items() <= discovered["entities"]["job"]["custom_fields"][0].items() or any(
            f["field"] == "customText12" for f in discovered["entities"]["job"]["custom_fields"]
        )

        p = _propose([
            {"op": "init_tenant", "tenant_id": "acme", "label": "Acme"},
            {"op": "apply_verified_defaults"},
            {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"},
            {"op": "set_value_mapping", "key": "job.priority.order", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
             "bullhorn_field": "customText12", "values": ["A", "B"]},
        ])
        assert "job.primary_recruiter_id" in p["validation"]["unresolved"]
        result = _commit(p)
        assert result["status"] == "committed" and result["version"] == 1

        status = _json(setup_tools.setup_status())
        assert status["state"] == "setup_valid"
        assert status["capabilities"]["jobs.priority"] == {"ok": True, "missing": []}
        assert status["capabilities"]["jobs.primary_recruiter"]["ok"] is False

        profile = _json(setup_tools.get_mapping_profile(entity="job", search="priority"))
        assert [r["field"] for r in profile["profile"]["field_mappings"]] == ["priority"]
        history = _json(setup_tools.get_mapping_profile(view="history"))
        assert history["entries"][0]["correlation_id"] == result["correlation_id"]
        proposal = _json(setup_tools.get_mapping_profile(view="proposal", search=p["proposal_id"]))
        assert proposal["proposal"]["status"] == "committed" and "draft" not in proposal["proposal"]

        _assert_only_meta_and_login(respx.calls)

    @respx.mock
    def test_discover_only_meta_requests_and_drift(self, store_dir, client):
        _mock_login()
        _mock_meta(tenant_payloads())
        _json(setup_tools.discover_schema())
        p = _propose([{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults", "entities": ["job"]}])
        _commit(p)
        version_bytes = (store_dir / "versions" / "v000001.yaml").read_bytes()
        active_bytes = (store_dir / "active.json").read_bytes()

        respx.reset()
        respx.routes.clear()
        _mock_login()
        _mock_meta(tenant_payloads(job_extra={**JOB_EXTRA, "customText40": {}}, job_drop=("title",)))
        out = _json(setup_tools.discover_schema(entities=["job"]))
        assert out["drift_unresolved"] is True
        assert out["report"]["broken_mappings"]["items"] == [{"key": "field:job.title", "missing_sources": ["title"]}]
        assert (store_dir / "versions" / "v000001.yaml").read_bytes() == version_bytes
        assert (store_dir / "active.json").read_bytes() == active_bytes
        assert _json(setup_tools.setup_status(check_connection=False))["state"] == "setup_revalidation_required"
        for call in respx.calls:
            assert call.request.method == "GET"
            assert str(call.request.url).startswith(f"{REST_URL}/meta/")

    @respx.mock
    def test_validate_clears_drift_when_clean(self, store_dir, client):
        _mock_login()
        _mock_meta(tenant_payloads())
        _json(setup_tools.discover_schema())
        _commit(_propose([{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults", "entities": ["job"]}]))
        doc = json.loads((store_dir / "discovery" / "latest.json").read_text())
        doc["drift_unresolved"] = True
        (store_dir / "discovery" / "latest.json").write_text(json.dumps(doc))
        out = _json(setup_tools.manage_mapping_profile(action="validate"))
        assert out["drift_unresolved"] is False and out["validation"]["ok"] is True
        assert _json(setup_tools.setup_status(check_connection=False))["state"] == "setup_valid"

    @respx.mock
    def test_validate_keeps_drift_when_broken(self, store_dir, client):
        _mock_login()
        _mock_meta(tenant_payloads())
        _json(setup_tools.discover_schema())
        _commit(_propose([{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults", "entities": ["job"]}]))
        respx.routes.clear()
        _mock_login()
        _mock_meta(tenant_payloads(job_drop=("title",)))
        out = _json(setup_tools.manage_mapping_profile(action="validate"))
        assert out["drift_unresolved"] is True and "field:job.title" in out["validation"]["broken"]
        assert _json(setup_tools.setup_status(check_connection=False))["state"] == "setup_invalid"


class TestBoundaries:
    @respx.mock
    def test_thousand_fields_and_hostile_bodies_bounded(self, store_dir, client):
        _mock_login()
        payloads = tenant_payloads()
        payloads["JobOrder"]["fields"] += [
            {"name": f"customText{i}", "label": "L" * 50_000, "type": "SCALAR", "dataType": "S" * 20_000,
             "options": [{"value": "v" * 300, "label": "x"}]}
            for i in range(1, 1001)
        ]
        _mock_meta({k: v for k, v in payloads.items() if k != "Candidate"})
        respx.get(f"{REST_URL}/meta/Candidate").mock(return_value=httpx.Response(500, text="<html>" + "E" * 200_000))
        out = setup_tools.discover_schema()
        _assert_bounded(out)
        data = _json(out)
        assert len(data["entities"]["candidate"]["error"]) <= 300
        for tool_output in (
            setup_tools.setup_status(),
            setup_tools.get_mapping_profile(),
            setup_tools.get_mapping_profile(view="history"),
            setup_tools.get_mapping_profile(view="proposal"),
        ):
            _assert_bounded(tool_output)
        _assert_only_meta_and_login(respx.calls)

    @respx.mock
    def test_meta_http_error_truncated(self, store_dir, client):
        _mock_login()
        respx.get(url__startswith=f"{REST_URL}/meta/").mock(return_value=httpx.Response(503, text="Z" * 100_000))
        out = _json(setup_tools.discover_schema(entities=["job"]))
        assert len(out["entities"]["job"]["error"]) <= 300

    @respx.mock
    def test_login_failure_bounded(self, store_dir, client):
        respx.get(f"{AUTH_URL}/oauth/authorize").mock(return_value=httpx.Response(500, text="Q" * 100_000))
        status = _json(setup_tools.setup_status())
        assert status["state"] == "disconnected" and status["missing_requirements"] == ["connection:failed"]
        assert len(status["detail"]) <= 300
        out = setup_tools.discover_schema()
        assert out.startswith("ERROR:") and len(out) < 1000
        assert not (store_dir / "discovery" / "latest.json").exists()

    @respx.mock
    def test_no_samples_anywhere(self, store_dir, client):
        _mock_login()
        _mock_meta(tenant_payloads())
        outputs = [setup_tools.discover_schema()]
        p = _propose([{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults"}])
        _commit(p)
        outputs += [
            setup_tools.get_mapping_profile(),
            setup_tools.get_mapping_profile(view="version", version=1),
            setup_tools.get_mapping_profile(view="proposal", search=p["proposal_id"]),
            setup_tools.get_mapping_profile(view="history"),
        ]
        for out in outputs:
            assert '"sample"' not in out
        stored = (store_dir / "discovery" / "latest.json").read_text()
        assert '"sample"' not in stored
        for call in respx.calls:
            assert "/search/" not in str(call.request.url) and "/query/" not in str(call.request.url)
            assert "/entity/" not in str(call.request.url)


class TestErrorsAndAudit:
    def test_no_store(self, monkeypatch):
        monkeypatch.delenv("BULLHORN_SETUP_STORE", raising=False)
        for out in (
            setup_tools.discover_schema(),
            setup_tools.get_mapping_profile(),
            setup_tools.propose_mapping_changes(changes=[]),
            setup_tools.commit_mapping_changes(proposal_id="a" * 32, diff_hash="b", decision="approve"),
            setup_tools.manage_mapping_profile(action="validate"),
        ):
            assert out.startswith("ERROR:") and "BULLHORN_SETUP_STORE" in out

    def test_setup_status_without_credentials(self, monkeypatch):
        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        with patch.object(server, "get_client") as get_client:
            status = _json(setup_tools.setup_status())
        get_client.assert_not_called()
        assert status["state"] == "disconnected"

    @pytest.mark.parametrize(
        "call",
        [
            lambda: setup_tools.get_mapping_profile(view="nope"),
            lambda: setup_tools.get_mapping_profile(view="version"),
            lambda: setup_tools.get_mapping_profile(view="diff"),
            lambda: setup_tools.get_mapping_profile(view="proposal", search="../../etc"),
            lambda: setup_tools.get_mapping_profile(search="x" * 1000),
            lambda: setup_tools.manage_mapping_profile(action="destroy"),
            lambda: setup_tools.manage_mapping_profile(action="export"),
            lambda: setup_tools.propose_mapping_changes(changes=[{"op": "init_tenant", "tenant_id": "../x"}]),
            lambda: setup_tools.propose_mapping_changes(changes=[{"op": "x" * 100_000}]),
        ],
    )
    def test_bounded_error_strings(self, store_dir, call):
        out = call()
        assert out.startswith("ERROR:")
        _assert_bounded(out)
        assert len(out) < 60_000

    def test_commit_refusal_is_json(self, store_dir):
        p = _propose([{"op": "init_tenant", "tenant_id": "acme"}])
        out = _json(setup_tools.commit_mapping_changes(proposal_id=p["proposal_id"], diff_hash="0" * 64, decision="approve"))
        assert out["status"] == "refused"
        assert not (store_dir / "active.json").exists()

    def test_reject(self, store_dir):
        p = _propose([{"op": "init_tenant", "tenant_id": "acme"}])
        assert _commit(p, "reject")["status"] == "rejected"
        assert not (store_dir / "active.json").exists()

    def test_unexpected_exception_contained(self, store_dir):
        with patch.object(setup_tools.tenant_changes, "propose", side_effect=RuntimeError("secret " * 1000)):
            out = setup_tools.propose_mapping_changes(changes=[])
        assert out == "ERROR: internal error (RuntimeError)"

    def test_permission_denied(self, store_dir):
        from bullhorn_mcp.crosscutting import permissions as permissions_module

        denial = permissions_module.PermissionDecision(allowed=False, requires_approval=False, reason="no")
        with patch.object(server.permissions, "check", return_value=denial) as check:
            out = setup_tools.propose_mapping_changes(changes=[{"op": "init_tenant", "tenant_id": "acme"}])
        assert out.startswith("ERROR: permission denied for propose_mapping_changes")
        check.assert_called_once_with("propose_mapping_changes", "write")
        assert not (store_dir / "proposals").exists()

    @pytest.mark.parametrize(
        "name,call",
        [
            ("setup_status", lambda: setup_tools.setup_status(check_connection=False)),
            ("get_mapping_profile", lambda: setup_tools.get_mapping_profile()),
            ("propose_mapping_changes", lambda: setup_tools.propose_mapping_changes(changes=[{"op": "init_tenant", "tenant_id": "acme"}])),
            ("commit_mapping_changes", lambda: setup_tools.commit_mapping_changes(proposal_id="a" * 32, diff_hash="x", decision="approve")),
            ("manage_mapping_profile", lambda: setup_tools.manage_mapping_profile(action="nope")),
        ],
    )
    def test_exactly_one_audit_record(self, store_dir, caplog, name, call):
        with caplog.at_level(logging.INFO, logger="bullhorn_mcp.audit"):
            call()
        records = [r for r in caplog.records if r.name == "bullhorn_mcp.audit"]
        assert len(records) == 1 and f'"tool": "{name}"' in records[0].getMessage()


class TestExport:
    @pytest.fixture
    def committed(self, store_dir):
        _commit(_propose([{"op": "init_tenant", "tenant_id": "acme"},
                          {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"}]))
        return store_dir

    def test_export_v2_and_reimport(self, committed, tmp_path):
        target = tmp_path / "out" / "acme.yaml"
        target.parent.mkdir()
        out = _json(setup_tools.manage_mapping_profile(action="export", path=str(target)))
        assert out["version"] == 1 and target.exists()
        assert "tenant-profile/v2" in target.read_text()
        p = _propose([{"op": "import_document", "path": str(target)},
                      {"op": "set_setting", "name": "reporting_timezone", "value": "UTC"},
                      {"op": "set_field_mapping", "entity": "job", "field": "title", "target": "title"}])
        assert p["proposal_id"]

    def test_export_v1(self, committed, tmp_path):
        target = tmp_path / "acme_v1.yaml"
        _json(setup_tools.manage_mapping_profile(action="export", path=str(target), format="v1"))
        from bullhorn_mcp.schema import MappingProfile

        assert MappingProfile.load(target).entity("job").standard["priority"].to_data() == "customText12"

    @pytest.mark.parametrize("fmt", ["v3", "", "V2"])
    def test_bad_format(self, committed, tmp_path, fmt):
        out = setup_tools.manage_mapping_profile(action="export", path=str(tmp_path / "x.yaml"), format=fmt)
        assert out.startswith("ERROR:")

    def test_export_refuses_store_and_existing(self, committed, tmp_path):
        assert setup_tools.manage_mapping_profile(action="export", path=str(committed / "x.yaml")).startswith("ERROR:")
        assert setup_tools.manage_mapping_profile(
            action="export", path=str(committed / "versions" / "v000001.yaml")
        ).startswith("ERROR:")
        existing = tmp_path / "exists.yaml"
        existing.write_text("keep")
        assert setup_tools.manage_mapping_profile(action="export", path=str(existing)).startswith("ERROR:")
        assert existing.read_text() == "keep"

    def test_diff_view(self, committed):
        _commit(_propose([{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customInt3"}]))
        out = _json(setup_tools.get_mapping_profile(view="diff", version=1, other_version=2))
        assert out["diff"][-1]["old"]["target"] == "customText12" and out["diff"][-1]["new"]["target"] == "customInt3"
