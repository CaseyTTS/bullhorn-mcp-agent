"""Phase 5A tool-surface security (AC-3, AC-5, AC-6, AC-12, AC-15, AC-19; SR-1, SR-3, SR-9, SR-12, SR-16, SR-19;
R-A2a, R-A2b, R-A2c, R-A3b)."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import anyio
import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.identity import sessions
from bullhorn_mcp.tenant.state import compute_setup_state
from bullhorn_mcp.tools import candidates, generic, jobs, placements, system
from bullhorn_mcp.tools import notes as notes_tools
from bullhorn_mcp.tools import session as session_tool
from bullhorn_mcp.tools import setup as setup_tools

from ._identity_helpers import (
    ADMIN,
    ALICE,
    BOB,
    CLIENT_SECRET,
    REST_1,
    SVC,
    TK1,
    caller,
    link,
    no_caller,
    pk,
)
from . import _identity_helpers
from ._tenant_helpers import CREDENTIALS

shared = _identity_helpers.shared  # fixture

ALL_20 = {
    "connection_status", "list_jobs", "get_job", "list_candidates", "get_candidate", "get_candidate_files",
    "upload_candidate_resume", "get_recent_placements", "search_entities", "query_entities",
    "setup_status", "discover_schema", "get_mapping_profile", "propose_mapping_changes", "commit_mapping_changes",
    "manage_mapping_profile", "get_notes", "create_note", "confirm_write", "bullhorn_session",
}
PHASE5C_TOOLS = {"find_records", "get_activity"}  # 5C approved additive tools (Amendment C3)
PHASE6_TOOLS = {"get_recruiting_metrics"}  # Phase 6 M1
FORBIDDEN_PARAMS = {
    "actor", "user", "principal", "username", "password", "subject", "issuer", "tenant", "service", "identity",
    "token", "code", "tier", "access_tier",
}
OP = "0" * 32
HASH = "0" * 64

# Every non-allowlisted tool (A2-2), with plausible arguments.
NON_ALLOWLISTED = {
    "connection_status": lambda: system.connection_status(),
    "list_jobs": lambda: jobs.list_jobs(),
    "get_job": lambda: jobs.get_job(job_id=1),
    "list_candidates": lambda: candidates.list_candidates(),
    "get_candidate": lambda: candidates.get_candidate(candidate_id=1),
    "get_candidate_files": lambda: candidates.get_candidate_files(candidate_id=1),
    "upload_candidate_resume": lambda: candidates.upload_candidate_resume(candidate_id=1, file_path="resume.pdf"),
    "get_recent_placements": lambda: placements.get_recent_placements(),
    "search_entities": lambda: generic.search_entities(entity="JobOrder", query="id:1"),
    "query_entities": lambda: generic.query_entities(entity="JobOrder", where="id=1"),
    "discover_schema": lambda: setup_tools.discover_schema(),
    "get_mapping_profile": lambda: setup_tools.get_mapping_profile(),
    "propose_mapping_changes": lambda: setup_tools.propose_mapping_changes(changes=[{"op": "init_tenant", "tenant_id": "acme"}]),
    "commit_mapping_changes": lambda: setup_tools.commit_mapping_changes(proposal_id=OP, diff_hash=HASH, decision="approve"),
    "manage_mapping_profile": lambda: setup_tools.manage_mapping_profile(action="validate"),
    "get_notes": lambda: notes_tools.get_notes(candidate_id=1),
    "create_note": lambda: notes_tools.create_note(target_type="candidate", target_id=1, action_type="Call", comments="x"),
    "confirm_write": lambda: notes_tools.confirm_write(operation_id=OP, preview_hash=HASH, decision="approve"),
}
WRITE_TOOLS = ("upload_candidate_resume", "propose_mapping_changes", "commit_mapping_changes", "create_note", "confirm_write")
ALL_CALLS = {
    **NON_ALLOWLISTED,
    "setup_status": lambda: setup_tools.setup_status(),
    "bullhorn_session": lambda: session_tool.bullhorn_session(),
}


def _json(text):
    return json.loads(text)


def _text(result):
    """The text of a FastMCP ``call_tool`` result (content blocks, or ``(content, structured)``)."""
    blocks = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(b, "text", "") for b in blocks)


class TestRegistry:
    def test_exactly_twenty_tools(self):
        assert set(server.mcp._tool_manager._tools) == ALL_20 | PHASE5C_TOOLS | PHASE6_TOOLS
        assert len(server.mcp._tool_manager._tools) == 20 + len(PHASE5C_TOOLS) + len(PHASE6_TOOLS)

    def test_bullhorn_session_schema_pin(self):
        assert server.mcp._tool_manager._tools["bullhorn_session"].parameters == {
            "properties": {
                "action": {"default": "status", "title": "Action", "type": "string"},
                "confirmation": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Confirmation"},
            },
            "title": "bullhorn_sessionArguments",
            "type": "object",
        }

    def test_no_identity_like_parameter_anywhere(self):  # AC-5 / SR-1 / R-A2c
        for name, tool in server.mcp._tool_manager._tools.items():
            props = {p.lower() for p in tool.parameters.get("properties", {})}
            assert not props & FORBIDDEN_PARAMS, name


class TestMissingIdentity:
    """AC-6 / SR-3: no principal -> identity_required for every tool, zero Bullhorn calls."""

    @pytest.mark.parametrize("name", sorted(ALL_CALLS))
    def test_no_token(self, shared, name):
        with respx.mock(assert_all_mocked=False) as router, no_caller():
            out = ALL_CALLS[name]()
        assert out == f"ERROR: permission denied for {name}: identity_required"
        assert not router.calls

    @pytest.mark.parametrize("name", ["get_job", "setup_status", "bullhorn_session"])
    @pytest.mark.parametrize("kw", [{"subject": None}, {"subject": ""}, {"issuer": None}, {"issuer": "https://evil.example.test"}])
    def test_subjectless_or_foreign_issuer(self, shared, name, kw):
        link(shared.store, TK1, ALICE)
        with respx.mock(assert_all_mocked=False) as router, caller(**{"subject": ALICE, **kw}):
            out = ALL_CALLS[name]()
        assert out.endswith("identity_required") and not router.calls


class TestTierGate:
    """R-A2a / SR-19: a workspace_only caller is denied every non-allowlisted tool, before any call."""

    @pytest.mark.parametrize("name", sorted(NON_ALLOWLISTED))
    def test_denied_with_zero_bullhorn_or_service_calls(self, shared, name):
        with respx.mock(assert_all_mocked=False) as router, caller(ALICE), \
                patch.object(sessions, "service_client", side_effect=AssertionError("service")) as svc, \
                patch("bullhorn_mcp.auth.bullhorn_password.BullhornAuth._get_auth_code", side_effect=AssertionError("pw")) as pw:
            out = NON_ALLOWLISTED[name]()
        assert out == f"ERROR: permission denied for {name}: bullhorn_auth_required"
        assert not router.calls and not svc.called and not pw.called

    def test_allowlisted_tools_answer_without_record_data(self, shared):
        with respx.mock(assert_all_mocked=False) as router, caller(ALICE):
            status = _json(setup_tools.setup_status())
            sess = _json(session_tool.bullhorn_session())
        assert not router.calls
        assert status["state"] == "disconnected" and status["missing_requirements"] == ["bullhorn_session"]
        assert status["identity"]["access_tier"] == "workspace_only" and status["active_version"] is None
        assert sess["access_tier"] == "workspace_only" and sess["session"] == "none"

    def test_dummy_tool_registered_later_is_denied_by_default(self, shared):  # R-A2b / SA2-5
        calls = []

        @server.mcp.tool(name="phase5a_dummy_tool")
        def dummy() -> str:
            calls.append(1)
            return "ran"

        try:
            with caller(ALICE):
                result = anyio.run(server.mcp.call_tool, "phase5a_dummy_tool", {})
            assert _text(result) == "ERROR: permission denied for phase5a_dummy_tool: bullhorn_auth_required" and not calls
            link(shared.store, TK1, ALICE)
            with caller(ALICE):
                anyio.run(server.mcp.call_tool, "phase5a_dummy_tool", {})
            assert calls == [1]
            with no_caller():
                anyio.run(server.mcp.call_tool, "phase5a_dummy_tool", {})
            assert calls == [1]
        finally:
            server.mcp._tool_manager._tools.pop("phase5a_dummy_tool", None)

    def test_gate_is_inert_in_local_mode(self):
        result = anyio.run(server.mcp.call_tool, "bullhorn_session", {})
        assert _json(_text(result))["mode"] == "local"


class TestServiceIdentity:
    """AC-12 / SR-9: service principals read only; every write tool and upload are denied."""

    @pytest.mark.parametrize("name", WRITE_TOOLS)
    def test_writes_denied(self, shared, name):
        with respx.mock(assert_all_mocked=False) as router, caller(SVC):
            out = NON_ALLOWLISTED[name]()
        assert out == f"ERROR: permission denied for {name}: service_identity_read_only" and not router.calls

    def test_export_denied_validate_allowed_by_policy(self, shared):
        with respx.mock(assert_all_mocked=False), caller(SVC):
            assert setup_tools.manage_mapping_profile(action="export", path="x.yaml").endswith("service_identity_read_only")
            assert "permission denied" not in setup_tools.manage_mapping_profile(action="validate")

    def test_service_login_and_logout_refused(self, shared):
        with caller(SVC):
            for action in ("login", "logout"):
                out = session_tool.bullhorn_session(action=action)
                assert out == "ERROR: permission denied for bullhorn_session: service_identity_read_only"
            assert _json(session_tool.bullhorn_session())["session"] == "service"


class TestSharedFilesystem:
    """D-5A-13 / AC-19 / SR-16."""

    def test_upload_denied_for_linked_user(self, shared, tmp_path):
        link(shared.store, TK1, ALICE)
        resume = tmp_path / "r.pdf"
        resume.write_bytes(b"%PDF")
        with respx.mock(assert_all_mocked=False) as router, caller(ALICE):
            for dry in (False, True):
                out = candidates.upload_candidate_resume(candidate_id=1, file_path=str(resume), dry_run=dry)
                assert out == "ERROR: permission denied for upload_candidate_resume: local_file_paths_unsupported_in_shared_mode"
        assert not router.calls

    def _admin_with_profile(self, shared):
        from ._tenant_helpers import init_tenant
        from bullhorn_mcp.tenant.store import SetupStore

        store = SetupStore(shared.tenant().setup_store)
        link(shared.store, TK1, ADMIN)
        with caller(ADMIN):
            init_tenant(store)
        return store

    def test_export_outside_exchange_dir_refused(self, shared, tmp_path):
        self._admin_with_profile(shared)
        exchange = shared.tenant().exchange_dir
        outside = tmp_path / "outside"
        outside.mkdir()
        link_dir = exchange / "link"
        try:
            link_dir.symlink_to(outside, target_is_directory=True)
        except OSError:
            link_dir = None
        candidates_ = [str(outside / "p.yaml"), str(exchange / ".." / "outside" / "p.yaml"), "relative.yaml"]
        if link_dir is not None:
            candidates_.append(str(link_dir / "p.yaml"))
        with caller(ADMIN):
            for path in candidates_:
                out = setup_tools.manage_mapping_profile(action="export", path=path)
                assert out.startswith("ERROR:") and "exchange directory" in out, (path, out)
            ok = _json(setup_tools.manage_mapping_profile(action="export", path=str(exchange / "p.yaml")))
        assert ok["action"] == "export" and (exchange / "p.yaml").exists()
        assert not (outside / "p.yaml").exists()

    def test_import_outside_exchange_dir_refused(self, shared, tmp_path):
        self._admin_with_profile(shared)
        outside = tmp_path / "evil.yaml"
        outside.write_text("version: 1\n", encoding="utf-8")
        with caller(ADMIN):
            out = setup_tools.propose_mapping_changes(changes=[{"op": "import_document", "path": str(outside)}])
        assert out.startswith("ERROR:") and "exchange directory" in out

    def test_store_comes_from_admin_config_not_env(self, shared, tmp_path, monkeypatch):
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        monkeypatch.setenv("BULLHORN_SETUP_STORE", str(decoy))
        self._admin_with_profile(shared)
        assert (shared.tenant().setup_store / "active.json").exists()
        assert not (decoy / "active.json").exists()


class TestSharedSetupState:
    """A3-2 / R-A3b / SA3-2."""

    def test_linked_user_without_env_credentials_is_connected(self, shared, monkeypatch):
        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        link(shared.store, TK1, ALICE)
        with caller(ALICE):
            state = compute_setup_state()
        assert "env:BULLHORN_CLIENT_ID" not in state.missing_requirements and state.state != "disconnected"

    def test_env_credentials_without_session_never_connected(self, shared, monkeypatch):
        for k, v in CREDENTIALS.items():
            monkeypatch.setenv(k, v)
        with caller(ALICE):
            state = compute_setup_state()
        assert state.state == "disconnected" and state.missing_requirements == ("bullhorn_session",)

    def test_another_users_session_does_not_connect(self, shared):
        link(shared.store, TK1, BOB)
        with caller(ALICE):
            assert compute_setup_state().state == "disconnected"

    def test_local_mode_unchanged(self, monkeypatch):
        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        state = compute_setup_state()
        assert state.missing_requirements[0] == "env:BULLHORN_CLIENT_ID"

    @respx.mock
    def test_get_notes_not_gated_as_disconnected(self, shared, monkeypatch):
        from ._notes_helpers import valid_store

        for k in CREDENTIALS:
            monkeypatch.delenv(k, raising=False)
        link(shared.store, TK1, ADMIN, rest_url=REST_1)
        from bullhorn_mcp.tenant.store import SetupStore

        root = shared.tenant().setup_store
        with caller(ADMIN):
            valid_store(root.parent)  # creates <tmp>/store; the tenant store is <tmp>/store-one
        # build the profile directly in the tenant store
        import shutil

        shutil.rmtree(root)
        shutil.copytree(root.parent / "store", root)
        SetupStore(root)
        respx.get(f"{REST_1}/entity/Candidate/100/notes").mock(return_value=httpx.Response(200, json={"data": []}))
        with caller(ADMIN):
            out = notes_tools.get_notes(candidate_id=100)
        assert "disconnected" not in out and "env:BULLHORN" not in out


class TestBullhornSessionTool:
    def test_local_mode(self):
        out = _json(session_tool.bullhorn_session())
        assert out["mode"] == "local" and out["access_tier"] == "local" and out["create_note_enabled_for_tenant"] is False
        assert _json(session_tool.bullhorn_session(action="login"))["reason"] == "login_unsupported_in_local_mode"
        assert _json(session_tool.bullhorn_session(action="logout"))["reason"] == "logout_unsupported_in_local_mode"

    def test_invalid_action(self, shared):
        with caller(ALICE):
            assert session_tool.bullhorn_session(action="steal").startswith("ERROR: action must be one of")

    def test_login_returns_start_url_only(self, shared):
        with caller(ALICE):
            out = _json(session_tool.bullhorn_session(action="login"))
        assert set(out) == {"login_url", "expires_at"}
        assert out["login_url"].startswith("https://mcp.example.test/oauth/bullhorn/start?login=")
        text = json.dumps(out)
        assert "state" not in text and "client_id" not in text and CLIENT_SECRET not in text

    def test_status_and_logout_for_linked_user(self, shared):
        rec = link(shared.store, TK1, ALICE)
        with caller(ALICE):
            status = _json(session_tool.bullhorn_session())
            assert status["session"] == "active" and status["access_tier"] == "bullhorn_user"
            assert status["tenant_key_hint"] == TK1[:8] and status["executing_identity_label"].startswith("bh-link:")
            assert rec.bh_rest_token not in json.dumps(status)
            assert _json(session_tool.bullhorn_session(action="logout")) == {"logged_out": True}
            assert _json(session_tool.bullhorn_session())["access_tier"] == "workspace_only"


class TestSecretSentinels:
    """AC-15 / SR-12: tokens never in tool output, logs, audit or exception text."""

    @respx.mock
    def test_sentinels_never_leak(self, shared, caplog):
        sentinel_bh = "SENTINEL-BHREST-9f8e7d"
        sentinel_refresh = "SENTINEL-REFRESH-1a2b3c"
        link(shared.store, TK1, ALICE, token=sentinel_bh, refresh=sentinel_refresh, expires_in=-10)
        respx.post("https://auth.bullhornstaffing.com/oauth/token").mock(
            return_value=httpx.Response(400, text=f"invalid_grant {sentinel_refresh} {CLIENT_SECRET}")
        )
        outputs = []
        with caplog.at_level(logging.DEBUG), caller(ALICE):
            for name in ("get_job", "setup_status", "bullhorn_session"):
                outputs.append(ALL_CALLS[name]())
            outputs.append(session_tool.bullhorn_session(action="login"))
        blob = "\n".join(outputs) + caplog.text
        for secret in (sentinel_bh, sentinel_refresh, CLIENT_SECRET, "access-alice-sub"):
            assert secret not in blob
        assert "REDACTED" in caplog.text  # the refresh URL was logged by httpx, with its secrets redacted

    def test_session_required_exception_text_is_clean(self):
        exc = sessions.BullhornSessionRequired()
        assert "token" not in str(exc).lower() or "bullhorn_session" in str(exc)
        assert repr(sessions.UserSessionAuth(None, pk(ALICE), None)) == "UserSessionAuth(<redacted>)"
