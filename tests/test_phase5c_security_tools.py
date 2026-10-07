"""Phase 5C security regressions (§10, §11: SR-20..SR-26; AC-3, AC-12, AC-16; Amendment C2 T-C2a..d)."""

from __future__ import annotations

import io
import json
import logging
import shutil
import threading
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.bullhorn import log_scrub
from bullhorn_mcp.bullhorn import reads as bh_reads
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.principal import TIER2_ALLOWLIST
from bullhorn_mcp.tools import generic
from bullhorn_mcp.tools import notes as notes_tools
from bullhorn_mcp.tools import records as record_tools

from . import _identity_helpers
from ._identity_helpers import ALICE, BOB, REST_1, SVC, TK1, activate_shared, caller, link, no_caller
from ._phase5c_helpers import FULL_CONFIG, tenant_store

ALL_22 = {
    "connection_status",
    "list_jobs",
    "get_job",
    "list_candidates",
    "get_candidate",
    "get_candidate_files",
    "upload_candidate_resume",
    "get_recent_placements",
    "search_entities",
    "query_entities",
    "setup_status",
    "discover_schema",
    "get_mapping_profile",
    "propose_mapping_changes",
    "commit_mapping_changes",
    "manage_mapping_profile",
    "get_notes",
    "create_note",
    "confirm_write",
    "bullhorn_session",
    "find_records",
    "get_activity",
}
FIND_SCHEMA = {
    "properties": {
        "entity": {"title": "Entity", "type": "string"},
        "filters": {"default": [], "items": {"additionalProperties": True, "type": "object"}, "title": "Filters", "type": "array"},
        "concept": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Concept"},
        "fields": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": None, "title": "Fields"},
        "sort": {"anyOf": [{"additionalProperties": True, "type": "object"}, {"type": "null"}], "default": None, "title": "Sort"},
        "include_deleted": {"default": False, "title": "Include Deleted", "type": "boolean"},
        "limit": {"default": 25, "title": "Limit", "type": "integer"},
        "cursor": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Cursor"},
    },
    "required": ["entity"],
    "title": "find_recordsArguments",
    "type": "object",
}
_OPT_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
_OPT_STR = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
ACTIVITY_SCHEMA = {
    "properties": {
        "concepts": {"items": {"type": "string"}, "title": "Concepts", "type": "array"},
        "scope_type": {**_OPT_STR, "title": "Scope Type"},
        "scope_id": {**_OPT_INT, "title": "Scope Id"},
        "recruiter_id": {**_OPT_INT, "title": "Recruiter Id"},
        "date_from": {**_OPT_STR, "title": "Date From"},
        "date_to": {**_OPT_STR, "title": "Date To"},
        "limit": {"default": 50, "title": "Limit", "type": "integer"},
        "cursor": {**_OPT_STR, "title": "Cursor"},
    },
    "required": ["concepts"],
    "title": "get_activityArguments",
    "type": "object",
}
FORBIDDEN_PARAMS = {
    "actor",
    "user",
    "principal",
    "username",
    "password",
    "subject",
    "issuer",
    "tenant",
    "service",
    "identity",
    "token",
    "code",
    "tier",
    "access_tier",
    "where",
    "query",
    "raw",
    "raw_query",
    "order_by",
    "orderBy",
    "lucene",
    "jpql",
}
QUERY = f"{REST_1}/query/Candidate"
SENTINEL_NAME = "Zqsentinelname"
SENTINEL_EMAIL = "zq.sentinel@example.test"


# ---------------------------------------------------------------------- #
# Surface (AC-3)
# ---------------------------------------------------------------------- #


class TestSurface:
    def test_exactly_22_tools(self):
        assert set(server.mcp._tool_manager._tools) == ALL_22
        assert len(server.mcp._tool_manager._tools) == 22

    def test_schema_pins(self):
        assert server.mcp._tool_manager._tools["find_records"].parameters == FIND_SCHEMA
        assert server.mcp._tool_manager._tools["get_activity"].parameters == ACTIVITY_SCHEMA

    def test_no_raw_query_or_identity_params(self):
        for name in ("find_records", "get_activity"):
            assert not set(server.mcp._tool_manager._tools[name].parameters["properties"]) & FORBIDDEN_PARAMS

    def test_not_on_tier2_allowlist(self):
        assert not {"find_records", "get_activity"} & TIER2_ALLOWLIST


# ---------------------------------------------------------------------- #
# Shared-mode fixture with a valid tenant profile
# ---------------------------------------------------------------------- #


def install_shared(tmp_path, monkeypatch, config, **kwargs):
    """Build a tenant store in local mode, then activate shared mode and install it as tenant one's store."""
    (tmp_path / "build").mkdir()
    (tmp_path / "shared").mkdir()
    built = tenant_store(tmp_path / "build", config)
    s = activate_shared(tmp_path / "shared", monkeypatch, **kwargs)
    target = s.tenant("one").setup_store
    shutil.rmtree(target)
    shutil.copytree(built.root, target)
    bh_reads.reset_limits()
    return s


@pytest.fixture
def shared5c(tmp_path, monkeypatch):
    s = install_shared(tmp_path, monkeypatch, FULL_CONFIG)
    yield s
    deploy.reset()
    sessions.reset_caches()


def _json(text: str):
    return json.loads(text)


def _find(**kw):
    return record_tools.find_records(**{"entity": "candidate", **kw})


def _activity(**kw):
    return record_tools.get_activity(**{"concepts": ["submission_created"], "date_from": "2026-09-01", "date_to": "2026-10-01", **kw})


def _page(*ids):
    return {"data": [{"id": i, "firstName": "N", "isDeleted": False} for i in ids]}


# ---------------------------------------------------------------------- #
# SR-20: tier gating (SC-5, SC-6)
# ---------------------------------------------------------------------- #


WORKSPACE_CALLS = [
    lambda: _find(),
    lambda: _find(fields=["id"], filters=[{"field": "id", "op": "eq", "value": 1}]),
    lambda: _find(entity="submission", concept="client_submission"),
    lambda: _find(cursor="eyJ2IjoxfQ.c2ln"),
    lambda: _find(entity="Candidate; DROP", filters="garbage"),  # type: ignore[arg-type]
    lambda: _activity(),
    lambda: _activity(concepts=["note_created"], scope_type="candidate", scope_id=100, date_from=None, date_to=None),
    lambda: _activity(concepts=["client_submission", "interview_scheduled"], recruiter_id=3, cursor="x.y"),
    lambda: generic.search_entities(entity="Candidate", query="id:1"),
    lambda: generic.query_entities(entity="Candidate", where="id=1"),
    lambda: notes_tools.get_notes(job_id=1),
]


class TestTierGating:
    @respx.mock
    @pytest.mark.parametrize("call", WORKSPACE_CALLS)
    def test_workspace_only_denied_before_parsing(self, shared5c, call):
        """ALICE has no completed link: workspace_only. No Bullhorn call, no service session."""
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")), caller(ALICE):
            text = call()
        assert text.startswith("ERROR: permission denied for ") and text.endswith(": bullhorn_auth_required")
        assert not respx.calls

    @respx.mock
    def test_r_a2_rerun_with_22_tools(self, shared5c):
        """Amendment A2 R-A2a/b with 22 tools: every tool outside the Tier 2 allowlist is denied to workspace_only."""
        from .test_phase5a_security_tools import NON_ALLOWLISTED

        calls = {**NON_ALLOWLISTED, "find_records": _find, "get_activity": _activity}
        assert set(calls) | TIER2_ALLOWLIST == ALL_22
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")), caller(ALICE):
            for name, call in calls.items():
                assert call() == f"ERROR: permission denied for {name}: bullhorn_auth_required", name
        assert not respx.calls

    @respx.mock
    def test_pending_link_is_workspace_only(self, shared5c):
        rec = link(shared5c.store, TK1, ALICE)
        rec.link_id = None
        shared5c.store.put(rec)
        with caller(ALICE):
            assert _find().endswith("bullhorn_auth_required")
        assert not respx.calls

    @respx.mock
    def test_no_identity(self, shared5c):
        with no_caller():
            assert _find().endswith("identity_required") and _activity().endswith("identity_required")
        assert not respx.calls

    @respx.mock
    def test_service_principal_denied_by_service_read_list(self, shared5c):
        """The frozen 5A service read list does not include the new tools (deny by default)."""
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")), caller(SVC):
            assert _find().endswith("service_identity_read_only")
            assert _activity().endswith("service_identity_read_only")
        assert not respx.calls

    @respx.mock
    def test_linked_user_allowed(self, shared5c):
        link(shared5c.store, TK1, ALICE)
        respx.get(QUERY).mock(return_value=httpx.Response(200, json=_page(1)))
        with caller(ALICE):
            out = _json(_find())
        assert out["status"] == "ok" and out["records"][0]["source"]["id"] == 1


# ---------------------------------------------------------------------- #
# SR-21: two users' reads stay isolated (SC-1)
# ---------------------------------------------------------------------- #


class TestIsolation:
    def test_concurrent_users_use_their_own_tokens(self, shared5c):
        link(shared5c.store, TK1, ALICE, token="bh-token-ALICE-xxxxxxxx")
        link(shared5c.store, TK1, BOB, token="bh-token-BOB-yyyyyyyyy")
        seen: list[tuple[str, str]] = []
        lock = threading.Lock()

        def handler(request):
            where = request.url.params["where"]
            who = "A" if "'Aname'" in where else "B"
            with lock:
                seen.append((who, request.headers["BhRestToken"]))
            return httpx.Response(200, json=_page(1 if who == "A" else 2))

        results: dict[str, list[dict]] = {"A": [], "B": []}
        errors: list[BaseException] = []

        def worker(subject, tag, value):
            try:
                with caller(subject):
                    for _ in range(10):
                        results[tag].append(_json(_find(filters=[{"field": "first_name", "op": "eq", "value": value}])))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with respx.mock:
            respx.get(QUERY).mock(side_effect=handler)
            threads = [
                threading.Thread(target=worker, args=(ALICE, "A", "Aname")),
                threading.Thread(target=worker, args=(BOB, "B", "Bname")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        assert not errors and len(seen) == 20
        for who, token in seen:
            assert token == ("bh-token-ALICE-xxxxxxxx" if who == "A" else "bh-token-BOB-yyyyyyyyy")
        assert all(r["records"][0]["id"] == 1 for r in results["A"]) and all(r["records"][0]["id"] == 2 for r in results["B"])


# ---------------------------------------------------------------------- #
# SR-22: cursor binding (SC-3, C1-1)
# ---------------------------------------------------------------------- #


class TestCursorBinding:
    def _cursor(self, store, subject):
        with caller(subject):
            out = _json(_find(limit=1))
        assert out["next_cursor"]
        return out["next_cursor"]

    @respx.mock
    def test_other_principal(self, shared5c):
        link(shared5c.store, TK1, ALICE)
        link(shared5c.store, TK1, BOB)
        route = respx.get(QUERY).mock(return_value=httpx.Response(200, json=_page(1, 2)))
        cur = self._cursor(shared5c.store, ALICE)
        n = route.call_count
        with caller(BOB):
            out = _json(_find(limit=1, cursor=cur))
        assert out["errors"][0]["code"] == "invalid_cursor" and route.call_count == n

    @respx.mock
    def test_relink_invalidates_refresh_does_not(self, shared5c):
        rec = link(shared5c.store, TK1, ALICE, link_id="link-one-aaaaaaaa")
        route = respx.get(QUERY).mock(return_value=httpx.Response(200, json=_page(1, 2)))
        cur = self._cursor(shared5c.store, ALICE)
        rec.bh_rest_token = "bh-token-refreshed-zzzzzzzz"  # a refresh keeps the link
        shared5c.store.put(rec)
        with caller(ALICE):
            assert _json(_find(limit=1, cursor=cur))["status"] == "ok"
        link(shared5c.store, TK1, ALICE, link_id="link-two-bbbbbbbb")  # logout + re-link
        n = route.call_count
        with caller(ALICE):
            out = _json(_find(limit=1, cursor=cur))
        assert out["errors"][0]["code"] == "invalid_cursor" and route.call_count == n

    def test_cursor_key_from_session_store_keys(self, shared5c, monkeypatch):
        from bullhorn_mcp.identity.session_store import SessionKeys
        from bullhorn_mcp.reads import cursor

        keys = SessionKeys({"k1": b"\x01" * 32}, "k1")
        monkeypatch.setattr(shared5c.store, "keys", keys, raising=False)
        k1 = cursor.cursor_key()
        monkeypatch.setattr(shared5c.store, "keys", SessionKeys({"k2": b"\x02" * 32}, "k2"), raising=False)
        assert cursor.cursor_key() != k1 and len(k1) == 32 and k1 != b"\x01" * 32


class TestTenantIsolation:
    @respx.mock
    def test_same_user_two_tenants(self, tmp_path, monkeypatch):
        s = install_shared(tmp_path, monkeypatch, FULL_CONFIG, tenants=2, tenant_claim="bh_tenant")
        try:
            link(s.store, TK1, ALICE)
            link(s.store, _identity_helpers.TK2, ALICE, rest_url=_identity_helpers.REST_2)
            respx.get(QUERY).mock(return_value=httpx.Response(200, json=_page(1, 2)))
            with caller(ALICE, bh_tenant="one"):
                cur = _json(_find(limit=1))["next_cursor"]
            n = len(respx.calls)
            with caller(ALICE, bh_tenant="two"):
                out = _json(_find(limit=1, cursor=cur))
                assert out["status"] == "rejected_validation"  # tenant two: cursor from tenant one
                assert _json(_find())["status"] == "setup_required"  # tenant two has no profile
            assert len(respx.calls) == n
        finally:
            deploy.reset()
            sessions.reset_caches()


# ---------------------------------------------------------------------- #
# SR-24: no service fallback (SC-4)
# ---------------------------------------------------------------------- #


class TestNoServiceFallback:
    @respx.mock
    def test_expired_session_mid_pagination(self, shared5c):
        link(shared5c.store, TK1, ALICE)
        respx.get(QUERY).mock(side_effect=[httpx.Response(200, json=_page(*range(1, 101))), httpx.Response(401)])
        respx.post(f"{_identity_helpers.AUTH}/oauth/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")) as svc, caller(ALICE):
            out = _json(_find(limit=100, filters=[{"field": "first_name", "op": "in", "value": ["N"]}]))
            after = _find(limit=100)
        assert out["status"] == "error" and out["error"] == "bullhorn_error" and "records" not in out  # no partial results
        assert svc.call_count == 0
        assert after.endswith("bullhorn_auth_required")  # the session was deleted (5A AC-11): the caller is workspace_only

    @respx.mock
    def test_expired_session_is_workspace_only(self, shared5c):
        link(shared5c.store, TK1, ALICE, expires_in=-10)
        respx.post(f"{_identity_helpers.AUTH}/oauth/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")) as svc, caller(ALICE):
            assert _activity().endswith("bullhorn_auth_required")
        assert svc.call_count == 0 and not [c for c in respx.calls if "/query/" in str(c.request.url)]

    @respx.mock
    def test_user_enrichment_runs_as_caller(self, shared5c):
        link(shared5c.store, TK1, ALICE, token="bh-token-ALICE-xxxxxxxx")
        route = respx.get(f"{REST_1}/query/CorporateUser").mock(return_value=httpx.Response(200, json={"data": []}))
        with patch.object(sessions, "service_client", side_effect=AssertionError("service fallback")), caller(ALICE):
            assert _json(_find(entity="user"))["status"] == "ok"
        assert route.calls[0].request.headers["BhRestToken"] == "bh-token-ALICE-xxxxxxxx"


# ---------------------------------------------------------------------- #
# SR-25: provenance, secret and filter-value sentinels (SC-9, AC-16, C2)
# ---------------------------------------------------------------------- #


@pytest.fixture
def root_capture():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(0)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(0)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old)


class TestSentinels:
    @respx.mock
    def test_t_c2a_shared_mode_scan(self, shared5c, root_capture, caplog):
        token = "bh-token-SENTINELTOKEN-123456"
        link(shared5c.store, TK1, ALICE, token=token)
        respx.get(QUERY).mock(return_value=httpx.Response(200, json=_page(1)))
        respx.get(f"{REST_1}/search/Candidate").mock(return_value=httpx.Response(200, json={"data": [], "total": 0}))
        respx.get(f"{REST_1}/query/JobOrder").mock(return_value=httpx.Response(200, json={"data": []}))
        respx.get(f"{REST_1}/entity/JobOrder/200/notes").mock(return_value=httpx.Response(200, json={"data": []}))
        outputs: list[str] = []
        with caplog.at_level(0), caller(ALICE):
            outputs.append(
                _find(
                    filters=[
                        {"field": "first_name", "op": "eq", "value": SENTINEL_NAME},
                        {"field": "email", "op": "eq", "value": SENTINEL_EMAIL},
                    ]
                )
            )
            outputs.append(_find(filters=[{"field": "last_name", "op": "in", "value": [SENTINEL_NAME]}], cursor="bad.cursor"))
            outputs.append(_activity(concepts=["submission_created", "client_submission"], recruiter_id=987654321))
            outputs.append(generic.search_entities(entity="Candidate", query="id:1"))
            outputs.append(generic.query_entities(entity="JobOrder", where="id=1"))
            outputs.append(notes_tools.get_notes(job_id=200))
        records = [r.getMessage() for r in caplog.records]
        logs = "\n".join(records) + root_capture.getvalue() + caplog.text
        for sentinel in (SENTINEL_NAME, SENTINEL_EMAIL, "987654321"):
            assert sentinel not in logs
        http_lines = [m for m in records if m.startswith("HTTP Request")]
        assert any("GET https://rest99.bullhornstaffing.com/rest-services/abc123/query/Candidate?<query-REDACTED>" in m for m in http_lines)
        assert any("/search/Candidate?<query-REDACTED>" in m for m in http_lines)
        assert any("/query/JobOrder?<query-REDACTED>" in m for m in http_lines)
        assert all("?" not in m.split("?<query-REDACTED>")[-1].split(" ")[0] for m in http_lines)
        blob = "\n".join(outputs[:3])
        for forbidden in (token, "BhRestToken", REST_1, "rest99", "firstName", "isDeleted", "lastName", "/query/", " AND ", SENTINEL_EMAIL):
            assert forbidden not in blob
        assert "Zqsentinelname" not in blob

    @respx.mock
    def test_error_messages_are_redacted(self, shared5c):
        link(shared5c.store, TK1, ALICE, token="bh-token-SENTINELTOKEN-999")
        respx.get(QUERY).mock(return_value=httpx.Response(500, text=f"boom {REST_1} BhRestToken=bh-token-SENTINELTOKEN-999"))
        with caller(ALICE):
            text = _find(filters=[{"field": "first_name", "op": "eq", "value": SENTINEL_NAME}])
        out = _json(text)
        assert out == {"status": "error", "error": "bullhorn_error", "message": "ReadFailed: bullhorn_error: 500"}

    def test_t_c2b_local_mode_noop(self):
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", "https://h/x?where=a%20b"), None)
        before = record.getMessage()
        assert not deploy.is_shared()
        assert log_scrub.QueryStringScrubFilter().filter(record) is True
        assert record.getMessage() == before and record.args == ("GET", "https://h/x?where=a%20b")

    def test_t_c2b_shared_mode_scrubs_and_keeps(self, shared5c):
        record = logging.LogRecord("httpcore.http11", logging.DEBUG, __file__, 1, "GET %s", ("https://h/p/q?where=secret 'x'",), None)
        assert log_scrub.QueryStringScrubFilter().filter(record) is True
        assert record.getMessage() == "GET https://h/p/q?<query-REDACTED> 'x'" and record.args == ()
        other = logging.LogRecord("bullhorn_mcp.x", logging.INFO, __file__, 1, "https://h/p?x=1", (), None)
        log_scrub.QueryStringScrubFilter().filter(other)
        assert other.getMessage() == "https://h/p?x=1"

    def test_t_c2c_httpx_request_record_logger_name(self):
        """Pin: the installed httpx emits its URL-bearing request record on the logger named exactly ``httpx``."""
        seen: list[logging.LogRecord] = []

        class Grab(logging.Handler):
            def emit(self, record):
                seen.append(record)

        handler = Grab(level=0)
        logger = logging.getLogger("httpx")
        old = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with respx.mock:
                respx.get("https://pin.example.test/p").mock(return_value=httpx.Response(200))
                with httpx.Client() as c:
                    c.get("https://pin.example.test/p?q=1")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        urls = [r for r in seen if "pin.example.test" in r.getMessage()]
        assert urls and all(r.name == "httpx" for r in urls)

    def test_filter_installed_on_httpx_and_children(self):
        logging.getLogger("httpcore.http11")
        log_scrub.install()
        for name in ("httpx", "httpcore", "httpcore.http11"):
            assert any(isinstance(f, log_scrub.QueryStringScrubFilter) for f in logging.getLogger(name).filters)
        n = len(logging.getLogger("httpx").filters)
        log_scrub.install()
        assert len(logging.getLogger("httpx").filters) == n  # idempotent

    def test_audit_holds_no_filter_values(self):
        args = {"entity": "candidate", "filters": [{"field": "first_name", "op": "eq", "value": SENTINEL_NAME}], "cursor": "abc"}
        from bullhorn_mcp.activity import service as activity_service
        from bullhorn_mcp.reads import records

        text = json.dumps(records.audit_args(args, None, None)) + json.dumps(
            activity_service.audit_args(
                {"concepts": ["job_created"], "scope_id": 123456789, "recruiter_id": 55, "date_from": "2026-01-01", "cursor": "abc"},
                None, None,
            )
        )
        assert SENTINEL_NAME not in text and "123456789" not in text and "2026-01-01" not in text and '"abc"' not in text


# ---------------------------------------------------------------------- #
# SR-26: concept definitions fail closed (SC-12) through the tool
# ---------------------------------------------------------------------- #


class TestConceptsFailClosed:
    @respx.mock
    def test_unconfigured_tenant(self, tmp_path, monkeypatch):
        s = install_shared(tmp_path, monkeypatch, [])
        try:
            link(s.store, TK1, ALICE)
            with caller(ALICE):
                out = _json(_activity(concepts=["client_submission", "interview_scheduled", "offer_extended", "job_created"]))
                bad = _json(_find(entity="submission", concept="client_submission"))
            for block in out["concepts"].values():
                assert block["status"] == "definition_missing" and block["missing_requirements"]
            assert bad["status"] == "definition_missing"
            assert not respx.calls
        finally:
            deploy.reset()
            sessions.reset_caches()
