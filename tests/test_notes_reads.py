"""Phase 4B get_notes: filter validation (AC-15), query safety (AC-16), paging/deleted (AC-18), guards (AC-19)."""

import datetime as dt
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.activity.events import make_activity_id
from bullhorn_mcp.notes import reads
from bullhorn_mcp.tools import notes as note_tools

from ._notes_helpers import make_client, valid_store
from ._tenant_helpers import CREDENTIALS, NOW, REST_URL
from ._unicode_corpus import corpus

DAY_MS = 86_400_000
OCT6_UTC_MS = int(dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc).timestamp()) * 1000


def _q(**kw):
    return reads.validate_basic(kw, "UTC")


class TestValidation:
    def test_requires_one_scope(self):
        query, errors = _q()
        assert query is None and errors[0]["code"] == "missing_scope"

    def test_two_scopes(self):
        _, errors = _q(job_id=1, candidate_id=2)
        assert errors[0]["code"] == "multiple_scopes"
        _, errors = _q(target_type="job", target_id=1, placement_id=2)
        assert errors[0]["code"] == "multiple_scopes"

    @pytest.mark.parametrize(
        "kw",
        [{"target_type": "job"}, {"target_id": 5}, {"target_type": "note", "target_id": 1}, {"job_id": 0}, {"job_id": "1"},
         {"job_id": True}, {"job_id": 1, "limit": 51}, {"job_id": 1, "limit": 0}, {"job_id": 1, "start": -1},
         {"job_id": 1, "include_deleted": "yes"}, {"job_id": 1, "author": " "}, {"job_id": 1, "action_type": ""}],
    )
    def test_invalid(self, kw):
        query, errors = _q(**kw)
        assert query is None and errors

    @pytest.mark.parametrize(
        "value", ["2026-10-06T10:00:00", "2026-10-06T10:00", "2026-10-06 10:00:00+00:00", "06/10/2026", "2026-13-01", 5]
    )
    def test_naive_or_malformed_datetime_rejected(self, value):
        query, errors = _q(job_id=1, date_from=value)
        assert query is None and errors[0]["code"] == "invalid_date"

    def test_date_only_utc(self):
        query, _ = _q(job_id=1, date_from="2026-10-06", date_to="2026-10-07")
        assert (query.date_from_ms, query.date_to_ms) == (OCT6_UTC_MS, OCT6_UTC_MS + DAY_MS)

    def test_date_only_reporting_timezone(self, monkeypatch):
        monkeypatch.setattr(reads, "_zone", lambda name: dt.timezone(dt.timedelta(hours=-5)))
        query, _ = reads.validate_basic({"job_id": 1, "date_from": "2026-10-06"}, "America/New_York")
        assert query.date_from_ms == OCT6_UTC_MS + 5 * 3_600_000

    def test_boundaries_to_the_millisecond(self):
        assert reads.parse_bound("2026-10-06T00:00:00Z", "d", "UTC") == OCT6_UTC_MS
        assert reads.parse_bound("2026-10-06T00:00:00.001Z", "d", "UTC") == OCT6_UTC_MS + 1
        assert reads.parse_bound("2026-10-06T00:00:00.0005Z", "d", "UTC") == OCT6_UTC_MS + 1
        assert reads.parse_bound("2026-10-06T02:00:00+02:00", "d", "UTC") == OCT6_UTC_MS

    def test_from_must_precede_to(self):
        _, errors = _q(job_id=1, date_from="2026-10-07", date_to="2026-10-07")
        assert errors[0]["code"] == "invalid_date"

    def test_unsupported_list(self):
        query, _ = _q(candidate_id=1, action_type="x", author="7", date_from="2026-01-01", include_deleted=True)
        assert len(reads.unsupported(query)) == 5
        query, _ = _q(job_id=1)
        assert reads.unsupported(query) == []


# ---------------------------------------------------------------------- #
# Through the tool
# ---------------------------------------------------------------------- #


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    store = valid_store(tmp_path)
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BULLHORN_SETUP_STORE", str(store.root))
    monkeypatch.setenv("BULLHORN_MCP_ACTOR", "admin@example.com")
    monkeypatch.setattr(note_tools, "_now", lambda: NOW)
    with patch.object(server, "get_client", return_value=make_client()):
        yield store


def _call(**kw):
    text = note_tools.get_notes(**kw)
    assert all(len(line) <= 10_000 for line in text.splitlines())
    assert not text.startswith("ERROR"), text
    return json.loads(text)


def _note(i, deleted=False, **extra):
    return {"id": i, "dateAdded": 1_790_000_000_000 + i, "action": "Screen Call", "comments": f"n{i}", "isDeleted": deleted,
            "personReference": {"id": 100}, "commentingPerson": {"id": 7}, "jobOrder": {"id": 200}, **extra}


class TestTool:
    def test_unknown_action_rejected(self, tool_env):
        with respx.mock(assert_all_called=False) as router:
            out = _call(job_id=200, action_type="screen")
            assert not router.calls
        assert out["status"] == "rejected" and out["errors"][0]["code"] == "unknown_action_type"
        assert out["errors"][0]["suggestions"] == ["Screen Call"]

    @pytest.mark.parametrize(
        "kw",
        [{"candidate_id": 1}, {"client_contact_id": 1}, {"client_corporation_id": 1}, {"submission_id": 1},
         {"target_type": "candidate", "target_id": 1}, {"job_id": 1, "action_type": "Screen Call"}, {"job_id": 1, "author": "7"},
         {"job_id": 1, "author": "Jane Doe"}, {"job_id": 1, "date_from": "2026-01-01"}, {"job_id": 1, "include_deleted": True}],
    )
    def test_unsupported_filter_no_request(self, tool_env, kw):
        with respx.mock(assert_all_called=False) as router:
            out = _call(**kw)
            assert not router.calls
        assert out["status"] == "unsupported_filter" and out["supported_filters"] == list(reads.SUPPORTED_FILTERS)

    @pytest.mark.parametrize(
        "evil",
        ["' OR 1=1 --", "x') OR (1=1", ")", "Screen Call' OR isDeleted=1", "*:*", "a" * 10_000, "\u202e\u0000\uffff",
         "".join(corpus()), *("Screen" + ch + "Call" for ch in corpus()[::25])],
    )
    def test_injection_never_reaches_a_request(self, tool_env, evil):
        with respx.mock(assert_all_called=False) as router:
            for kw in ({"action_type": evil}, {"author": evil}, {"date_from": evil}, {"target_type": evil, "target_id": 1}):
                text = note_tools.get_notes(job_id=200, **kw) if "target_type" not in kw else note_tools.get_notes(**kw)
                assert all(len(line) <= 10_000 for line in text.splitlines())
            assert not router.calls

    @respx.mock
    def test_job_scope_page(self, tool_env):
        route = respx.get(f"{REST_URL}/entity/JobOrder/200/notes").mock(
            return_value=httpx.Response(200, json={"data": [_note(1), _note(2, deleted=True), _note(3)]})
        )
        out = _call(job_id=200, limit=2)
        params = dict(route.calls[0].request.url.params)
        assert params == {"fields": reads.LIST_FIELDS, "start": "0", "count": "3"}
        assert out["truncated"] is True and out["next_start"] == 2
        assert [n["id"] for n in out["notes"]] == [1]
        assert out["notes"][0]["action_semantic"] == "candidate_screen"
        assert out["events"][0]["activity_id"] == make_activity_id("note_created", "note", 1)
        assert out["events"][0]["origin"] == "observed"
        assert any("deleted" in w for w in out["warnings"])

    @respx.mock
    def test_last_page(self, tool_env):
        respx.get(f"{REST_URL}/entity/Placement/9/notes").mock(return_value=httpx.Response(200, json={"data": [_note(1)]}))
        out = _call(target_type="placement", target_id=9, limit=2, start=4)
        assert out["truncated"] is False and out["next_start"] is None and out["scope"] == {"type": "placement", "id": 9}

    @respx.mock
    def test_limit_reached_exactly_is_not_truncated(self, tool_env):
        respx.get(f"{REST_URL}/entity/JobOrder/200/notes").mock(return_value=httpx.Response(200, json={"data": [_note(1), _note(2)]}))
        out = _call(job_id=200, limit=2)
        assert out["truncated"] is False and out["next_start"] is None

    @respx.mock
    def test_long_body_chunked(self, tool_env):
        respx.get(f"{REST_URL}/entity/JobOrder/200/notes").mock(
            return_value=httpx.Response(200, json={"data": [_note(1, comments="\u00e9" * 50_000)]})
        )
        body = _call(job_id=200)["notes"][0]["body"]
        assert body["length"] == 50_000 and body["truncated"] is True and sum(len(c) for c in body["text_chunks"]) == 10_000

    @respx.mock
    def test_api_error_bounded(self, tool_env):
        respx.get(f"{REST_URL}/entity/JobOrder/200/notes").mock(
            return_value=httpx.Response(500, text='{"BhRestToken":"leakme' + "x" * 40 + '"}' + "y" * 5000)
        )
        text = note_tools.get_notes(job_id=200)
        assert text.startswith("ERROR:") and "leakme" not in text and len(text) < 400

    def test_denied_without_setup(self, monkeypatch):
        for k, v in CREDENTIALS.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("BULLHORN_SETUP_STORE", raising=False)
        with respx.mock(assert_all_called=False) as router:
            out = _call(job_id=1)
            assert not router.calls
        assert out["status"] == "denied" and out["missing_requirements"] == ["state:connected_setup_required"]

    def test_validation_errors_before_gate(self, monkeypatch):
        monkeypatch.delenv("BULLHORN_SETUP_STORE", raising=False)
        out = _call(job_id=1, candidate_id=2)
        assert out["status"] == "rejected"
