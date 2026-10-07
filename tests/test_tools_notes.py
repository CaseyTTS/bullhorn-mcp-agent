"""Phase 4B MCP tools: registry (AC-6 / A1-C1), the tool boundary, audit (AC-13) and the end-to-end flow (AC-8)."""

import json
import logging
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.crosscutting import permissions
from bullhorn_mcp.crosscutting.permissions import PermissionDecision
from bullhorn_mcp.tools import notes as note_tools

from bullhorn_mcp.writes import pipeline

from ._notes_helpers import CURRENT_USER_ID, NOTE_ID, SECRET_COMMENT, make_client, mock_targets, mock_write, readback, valid_store
from ._tenant_helpers import CREDENTIALS, NOW, REST_URL

ALL_TOOLS = {
    "connection_status", "list_jobs", "list_candidates", "get_job", "get_candidate", "get_recent_placements",
    "get_candidate_files", "upload_candidate_resume", "search_entities", "query_entities",
    "setup_status", "discover_schema", "get_mapping_profile", "propose_mapping_changes", "commit_mapping_changes",
    "manage_mapping_profile",
    "get_notes", "create_note", "confirm_write",
}
APPROVED_ADDITIVE_TOOLS = {"bullhorn_session", "find_records", "get_activity"}  # Phase 5 approved additive tools (D-5-15)
_INT_OR_NULL = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
_STR_OR_NULL = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
NEW_SCHEMAS = {
    "get_notes": {
        "properties": {
            "target_type": {**_STR_OR_NULL, "title": "Target Type"},
            "target_id": {**_INT_OR_NULL, "title": "Target Id"},
            "candidate_id": {**_INT_OR_NULL, "title": "Candidate Id"},
            "job_id": {**_INT_OR_NULL, "title": "Job Id"},
            "client_corporation_id": {**_INT_OR_NULL, "title": "Client Corporation Id"},
            "client_contact_id": {**_INT_OR_NULL, "title": "Client Contact Id"},
            "placement_id": {**_INT_OR_NULL, "title": "Placement Id"},
            "submission_id": {**_INT_OR_NULL, "title": "Submission Id"},
            "action_type": {**_STR_OR_NULL, "title": "Action Type"},
            "author": {**_STR_OR_NULL, "title": "Author"},
            "date_from": {**_STR_OR_NULL, "title": "Date From"},
            "date_to": {**_STR_OR_NULL, "title": "Date To"},
            "include_deleted": {"default": False, "title": "Include Deleted", "type": "boolean"},
            "limit": {"default": 20, "title": "Limit", "type": "integer"},
            "start": {"default": 0, "title": "Start", "type": "integer"},
        },
        "title": "get_notesArguments",
        "type": "object",
    },
    "create_note": {
        "properties": {
            "target_type": {"title": "Target Type", "type": "string"},
            "target_id": {"title": "Target Id", "type": "integer"},
            "action_type": {"title": "Action Type", "type": "string"},
            "comments": {"title": "Comments", "type": "string"},
            "associations": {
                "anyOf": [{"items": {"additionalProperties": True, "type": "object"}, "type": "array"}, {"type": "null"}],
                "default": None,
                "title": "Associations",
            },
            "idempotency_key": {**_STR_OR_NULL, "title": "Idempotency Key"},
            "dry_run": {"default": True, "title": "Dry Run", "type": "boolean"},
        },
        "required": ["target_type", "target_id", "action_type", "comments"],
        "title": "create_noteArguments",
        "type": "object",
    },
    "confirm_write": {
        "properties": {
            "operation_id": {"title": "Operation Id", "type": "string"},
            "preview_hash": {"title": "Preview Hash", "type": "string"},
            "decision": {"title": "Decision", "type": "string"},
        },
        "required": ["operation_id", "preview_hash", "decision"],
        "title": "confirm_writeArguments",
        "type": "object",
    },
}


class TestRegistry:
    def test_exactly_nineteen_tools(self):
        assert set(server.mcp._tool_manager._tools) == ALL_TOOLS | APPROVED_ADDITIVE_TOOLS
        assert len(server.mcp._tool_manager._tools) == 19 + len(APPROVED_ADDITIVE_TOOLS)

    @pytest.mark.parametrize("name", list(NEW_SCHEMAS))
    def test_new_schemas(self, name):
        assert server.mcp._tool_manager._tools[name].parameters == NEW_SCHEMAS[name]


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    store = valid_store(tmp_path)
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BULLHORN_SETUP_STORE", str(store.root))
    monkeypatch.setenv("BULLHORN_MCP_ACTOR", "admin@example.com")
    monkeypatch.setenv("BULLHORN_ENABLED_WRITE_SCOPES", "note.create")
    for name in ("BULLHORN_WRITE_APPROVERS", "BULLHORN_NOTE_CREATE_MODE", "BULLHORN_SETUP_ADMINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(note_tools, "_now", lambda: NOW)
    with patch.object(server, "get_client", return_value=make_client()):
        yield store


@pytest.fixture
def hv_b11(monkeypatch):
    """HV-B11 mocked as verified, with a server-side resolver (the production default is unresolved)."""
    monkeypatch.setattr(pipeline, "HV_B11_VERIFIED", True)
    pipeline._USER_CACHE.clear()
    original = note_tools._context

    def with_user():
        ctx = original()
        ctx.current_user = lambda: CURRENT_USER_ID
        return ctx

    monkeypatch.setattr(note_tools, "_context", with_user)
    yield
    pipeline._USER_CACHE.clear()


def _json(text):
    assert all(len(line) <= 10_000 for line in text.splitlines())
    assert not text.startswith("ERROR"), text
    return json.loads(text)


class TestFlow:
    @respx.mock
    def test_preview_confirm_then_read_same_activity_id(self, tool_env, hv_b11, caplog):
        caplog.set_level(logging.INFO, logger="bullhorn_mcp.audit")
        mock_targets()
        mock_write(read=readback(job=200))
        preview = _json(note_tools.create_note("candidate", 100, "Screen Call", SECRET_COMMENT, associations=[{"type": "job", "id": 200}]))
        assert preview["status"] == "previewed"
        assert all(c.request.method == "GET" for c in respx.calls)
        result = _json(note_tools.confirm_write(preview["operation_id"], preview["preview_hash"], "approve"))
        assert result["status"] == "committed" and result["record_id"] == NOTE_ID
        respx.get(f"{REST_URL}/entity/JobOrder/200/notes").mock(
            return_value=httpx.Response(200, json={"data": [readback(job=200) | {"comments": SECRET_COMMENT}]})
        )
        read = _json(note_tools.get_notes(job_id=200))
        assert read["events"][0]["activity_id"] == result["activity"][0]["activity_id"]
        assert read["events"][0]["origin"] == "observed" and result["activity"][0]["origin"] == "written_by_mcp"
        audit_text = "".join(r.getMessage() for r in caplog.records if r.name == "bullhorn_mcp.audit")
        assert "Confidential" not in audit_text
        assert preview["correlation_id"] in audit_text

    @respx.mock
    def test_long_comment_preview_is_chunked(self, tool_env, hv_b11):
        mock_targets()
        text = "\n" * 9_999 + "x"
        out = _json(note_tools.create_note("candidate", 100, "Screen Call", text))
        comments = out["preview"]["request"]["body"]["comments"]
        assert comments["length"] == 10_000 and "".join(comments["text_chunks"]) == text

    def test_tool_audit_has_digest_not_text(self, tool_env, hv_b11, caplog):
        caplog.set_level(logging.INFO, logger="bullhorn_mcp.audit")
        with respx.mock(assert_all_called=False):
            note_tools.create_note("candidate", 100, "Nope", SECRET_COMMENT)
        records = [r.getMessage() for r in caplog.records if r.name == "bullhorn_mcp.audit"]
        assert len(records) == 1 and "Confidential" not in records[0] and '"length": 53' in records[0]

    def test_legacy_permission_denial_first(self, tool_env):
        deny = PermissionDecision(allowed=False, requires_approval=False, reason="nope")
        with patch.object(permissions, "check", return_value=deny), respx.mock(assert_all_called=False) as router:
            for out in (
                note_tools.create_note("candidate", 100, "Screen Call", "x"),
                note_tools.confirm_write("a" * 32, "0" * 64, "approve"),
                note_tools.get_notes(job_id=1),
            ):
                assert out.startswith("ERROR: permission denied")
            assert not router.calls

    @respx.mock
    def test_scope_unset_denied_via_tool(self, tool_env, hv_b11, monkeypatch):
        monkeypatch.delenv("BULLHORN_ENABLED_WRITE_SCOPES")
        mock_targets()
        out = _json(note_tools.create_note("candidate", 100, "Screen Call", "x"))
        assert out["status"] == "denied" and "scope:note.create" in out["missing_requirements"]

    def test_no_raw_exception_escapes(self, tool_env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("secret BhRestToken=abcdefghijklmnopqrstuvwxyz")

        monkeypatch.setattr(note_tools.pipeline, "create_note", boom)
        out = note_tools.create_note("candidate", 100, "Screen Call", "x")
        assert out == "ERROR: internal error (RuntimeError)"

    def test_missing_credentials_error_bounded(self, monkeypatch):
        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        with patch.object(server, "_client", None):
            out = note_tools.confirm_write("a" * 32, "0" * 64, "approve")
        assert out.startswith("ERROR:") and len(out) < 400


class TestHvB11Guard:
    @pytest.mark.parametrize("target_type", ["candidate", "client_contact"])
    def test_production_default_rejects_person_targets(self, tool_env, target_type):
        assert pipeline.HV_B11_VERIFIED is False
        with respx.mock(assert_all_called=False) as router:
            out = _json(note_tools.create_note(target_type, 100, "Screen Call", "x", dry_run=True))
            assert not router.calls
        assert out["status"] == "rejected_validation"
        assert [e["code"] for e in out["errors"]] == ["unsupported_association"]

    @respx.mock
    def test_commenting_person_from_resolver_only(self, tool_env, hv_b11):
        mock_targets()
        out = _json(note_tools.create_note("candidate", 100, "Screen Call", "x"))
        assert out["preview"]["request"]["body"]["commentingPerson"] == {"id": CURRENT_USER_ID}
        assert "commentingPerson" not in server.mcp._tool_manager._tools["create_note"].parameters["properties"]
