"""Phase 5C review triage: B-1, B-2, B-3, L-1, L-2, L-3 (T-5C-B1a..e, T-5C-B2a..d, T-5C-B3a..b, T-5C-L1..L3;
SR-33, SR-34, SR-35)."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from bullhorn_mcp.activity import service as A
from bullhorn_mcp.activity.events import tagged_json
from bullhorn_mcp.bullhorn import log_scrub
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.reads import cursor as C
from bullhorn_mcp.reads import records as R
from bullhorn_mcp.tenant import capabilities as caps
from bullhorn_mcp.tenant.profile_v2 import TenantProfileV2, load_activity_concepts, records_by_key
from bullhorn_mcp.tenant.state import effective_states
from bullhorn_mcp.tools import jobs
from bullhorn_mcp.tools import records as record_tools

from ._identity_helpers import ALICE, REST_1, TK1, caller, link
from ._phase5c_helpers import (
    DATING,
    FULL_CONFIG,
    PRIMARY_RECRUITER,
    RULE_END,
    RULE_MAPPED,
    bh_calls,
    context,
    query_route,
    tenant_store,
)
from .test_phase5c_security_tools import install_shared

SRC = Path(R.__file__).resolve().parents[1]
RANGE = {"date_from": "2026-09-01", "date_to": "2026-10-01"}
FIND_CONCEPTS = {
    "client_submission": "submission",
    "interview_scheduled": "appointment",
    "interview_completed": "appointment",
    "interview_cancelled": "appointment",
    "offer_extended": "submission",
    "offer_accepted": "submission",
    "offer_declined": "submission",
}


def without(*items):
    return [c for c in FULL_CONFIG if all(c is not i for i in items)]


def both(store, concept_id):
    find = R.find_records(context(store), {"entity": FIND_CONCEPTS[concept_id], "concept": concept_id})
    act = A.get_activity(context(store), {"concepts": [concept_id], **RANGE})["concepts"][concept_id]
    return find, act


# ---------------------------------------------------------------------- #
# B-1 / SR-35: one concept definition
# ---------------------------------------------------------------------- #


class TestB1:
    @respx.mock
    def test_b1a_missing_completion_rule(self, tmp_path):
        store = tenant_store(tmp_path, without(RULE_END))
        find, act = both(store, "interview_completed")
        assert find == {"status": "definition_missing", "missing_requirements": ["setting:interview_completion_rule"]}
        assert act["status"] == "definition_missing" and act["missing_requirements"] == find["missing_requirements"]
        assert not bh_calls()

    @respx.mock
    def test_b1b_end_passed_rule_not_renderable(self, tmp_path):
        store = tenant_store(tmp_path, FULL_CONFIG)  # FULL_CONFIG uses end_passed_not_cancelled
        out = R.find_records(context(store), {"entity": "appointment", "concept": "interview_completed"})
        assert out["status"] == "unsupported"
        assert out["unsupported"][0]["code"] == "unsupported_concept"
        assert out["unsupported"][0]["reason"] == "rule_not_renderable_as_current_state"
        assert not bh_calls()
        query_route("Appointment", [[{"id": 5, "type": "Interview", "dateEnd": 1_790_000_000_000, "parentAppointment": None}]])
        block = A.get_activity(context(store), {"concepts": ["interview_completed"], **RANGE})["concepts"]["interview_completed"]
        assert block["status"] == "ok" and len(block["events"]) == 1

    @respx.mock
    def test_b1b_mapped_state_only_renders(self, tmp_path):
        store = tenant_store(tmp_path, [*without(RULE_END), RULE_MAPPED])
        route = query_route("Appointment", [[]])
        assert R.find_records(context(store), {"entity": "appointment", "concept": "interview_completed"})["status"] == "ok"
        assert "customText1 IN ('Done')" in route.calls[0].request.url.params["where"]

    @respx.mock
    def test_b1c_missing_dating(self, tmp_path):
        store = tenant_store(tmp_path, without(DATING))
        find, act = both(store, "client_submission")
        assert find == {"status": "definition_missing", "missing_requirements": ["setting:client_submission_dating"]}
        assert act["missing_requirements"] == find["missing_requirements"]
        assert not bh_calls()

    @respx.mock
    @pytest.mark.parametrize("concept_id", sorted(FIND_CONCEPTS))
    def test_b1d_same_requirement_set(self, tmp_path, concept_id):
        store = tenant_store(tmp_path, [PRIMARY_RECRUITER])
        find, act = both(store, concept_id)
        expected = list(caps.concept_requirements(concept_id, None, {}))
        assert expected and find["status"] == "definition_missing" and act["status"] == "definition_missing"
        assert find["missing_requirements"] == act["missing_requirements"] == expected
        assert not bh_calls()

    @respx.mock
    @pytest.mark.parametrize("concept_id, entity", [("offer_pending", "submission"), ("interview_upcoming", "appointment")])
    def test_b1e_derived_states_invalid(self, tmp_path, concept_id, entity):
        store = tenant_store(tmp_path, FULL_CONFIG)
        out = R.find_records(context(store), {"entity": entity, "concept": concept_id})
        assert out["status"] == "rejected_validation" and out["errors"][0]["code"] == "invalid_concept"
        assert "get_activity" in out["errors"][0]["message"]
        assert not bh_calls()

    def test_no_local_concept_tables(self):
        text = (SRC / "reads" / "records.py").read_text(encoding="utf-8")
        assert "CONCEPT_FILTERS" not in text and "need = [" not in text
        assert 'target.kind == "ordering"' not in text  # the priority order comes from capabilities


# ---------------------------------------------------------------------- #
# B-2 / SR-33: no values and no unkeyed digests in audit or logs
# ---------------------------------------------------------------------- #


SENT = "Qzsent"
SENT_ID = 918273645
SENT_RID = 918273646
SENT_FROM = "2019-07-23"
SENT_TO = "2019-08-29"
SENT_CURSOR = "QzsentCursorBody.QzsentCursorSig"
LONG_KEY = "K" * 64


@pytest.fixture
def shared5c(tmp_path, monkeypatch):
    s = install_shared(tmp_path, monkeypatch, FULL_CONFIG)
    yield s
    deploy.reset()
    sessions.reset_caches()


@pytest.fixture
def capture(tmp_path):
    """Root capture at all levels plus the bytes of an audit file handler."""
    buf = io.StringIO()
    fmt = logging.Formatter("%(name)s|%(message)s")  # every captured line names its logger (R2-L2)
    root_handler = logging.StreamHandler(buf)
    root_handler.setLevel(0)
    root_handler.setFormatter(fmt)
    audit_path = tmp_path / "audit.log"
    file_handler = logging.FileHandler(audit_path, encoding="utf-8")
    file_handler.setLevel(0)
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    audit_logger = logging.getLogger("bullhorn_mcp.audit")
    old_root, old_audit = root.level, audit_logger.level
    root.addHandler(root_handler)
    root.setLevel(0)
    audit_logger.addHandler(file_handler)
    audit_logger.setLevel(0)
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(f"{record.name}|{record.getMessage()}")

    grab = Grab(level=0)
    root.addHandler(grab)
    yield lambda: "\n".join([buf.getvalue(), *records, audit_path.read_text(encoding="utf-8")])
    root.removeHandler(root_handler)
    root.removeHandler(grab)
    audit_logger.removeHandler(file_handler)
    file_handler.close()
    root.setLevel(old_root)
    audit_logger.setLevel(old_audit)


def _hostile_calls():
    return [
        lambda: record_tools.find_records(entity="candidate", filters=[{"field": "first_name", "op": "eq", "value": SENT}]),
        lambda: record_tools.find_records(entity="candidate", filters=[{"field": "id", "op": "in", "value": [SENT_ID]}]),
        lambda: record_tools.find_records(entity="candidate", filters=[{"field": "email", "op": "eq", "val": SENT, "values": [SENT]}]),
        lambda: record_tools.find_records(entity="candidate", filters=[{"field": "email", "op": "eq", "value": "x", LONG_KEY: SENT}]),
        lambda: record_tools.find_records(entity="candidate", filters=[{"field": SENT, "op": SENT, "value": SENT}]),
        lambda: record_tools.find_records(entity="candidate", sort={"field": "last_name", "direction": SENT}),
        lambda: record_tools.find_records(entity="candidate", sort={"field": "last_name", SENT: 1}),
        lambda: record_tools.find_records(entity="candidate", fields=["id", SENT]),
        lambda: record_tools.find_records(entity=SENT, concept=SENT),
        lambda: record_tools.find_records(entity="candidate", cursor=SENT_CURSOR),
        lambda: record_tools.get_activity(concepts=["submission_created"], scope_type="job", scope_id=SENT_ID, recruiter_id=SENT_RID),
        lambda: record_tools.get_activity(concepts=["submission_created"], date_from=SENT_FROM, date_to=SENT_TO, cursor=SENT_CURSOR),
        lambda: record_tools.get_activity(concepts=[SENT], scope_type=SENT, scope_id=SENT_ID),
    ]


SUBMITTED = [SENT, SENT_ID, SENT_RID, SENT_FROM, SENT_TO, SENT_CURSOR, LONG_KEY, [SENT], [SENT_ID]]


def _unkeyed_digests(value):
    texts = {str(value), json.dumps(value), tagged_json(value)}
    out = set()
    for t in texts:
        for algo in ("sha256", "sha1", "md5"):
            out.add(hashlib.new(algo, t.encode()).hexdigest())
    return out


class TestB2:
    @respx.mock
    def test_b2a_b2b_no_values_no_unkeyed_digests(self, shared5c, capture):
        link(shared5c.store, TK1, ALICE)
        respx.get(url__regex=r".*/query/.*").mock(return_value=httpx.Response(200, json={"data": []}))
        with caller(ALICE):
            for call in _hostile_calls():
                call()
        text = capture()
        assert '"tool": "find_records"' in text and '"tool": "get_activity"' in text and "request_hmac" in text
        for sentinel in (SENT, str(SENT_ID), str(SENT_RID), SENT_FROM, SENT_TO, "QzsentCursor", LONG_KEY):
            assert sentinel not in text, sentinel
        for value in SUBMITTED:
            for digest in _unkeyed_digests(value):
                assert digest not in text

    def test_audit_shapes(self):
        out = R.audit_args({"entity": "candidate", "filters": [{"field": "first_name", "op": "eq", "value": SENT},
                                                               {"field": "x", "op": "eq", "value": 1}, "junk"],
                            "fields": ["id", SENT], "sort": {"field": "last_name", "direction": SENT}, "cursor": SENT_CURSOR,
                            "limit": 25, "include_deleted": False, "concept": None}, "t1", "p1")
        assert out["filters"] == [{"field": "first_name", "op": "eq", "value": {"type": "string", "length": len(SENT)}}]
        assert out["fields"] == ["id"] and "sort" not in out and out["invalid_items"] == 4
        assert out["cursor"] == {"type": "string", "length": len(SENT_CURSOR)}
        act = A.audit_args({"concepts": ["job_created", SENT], "scope_type": SENT, "scope_id": SENT_ID, "limit": 50}, "t1", "p1")
        assert act["concepts"] == ["job_created"] and act["invalid_items"] == 2
        assert act["scope_id"] == {"type": "integer", "length": None} and "scope_type" not in act

    def test_b2c_request_hmac_keyed(self):
        req = {"entity": "candidate", "filters": [{"field": "first_name", "op": "eq", "value": SENT}]}
        k1, k2 = b"\x01" * 32, b"\x02" * 32
        assert C.request_hmac("t1", "p1", req, k1) == C.request_hmac("t1", "p1", copy.deepcopy(req), k1)
        assert C.request_hmac("t1", "p1", req, k1) != C.request_hmac("t1", "p1", req, k2)
        assert C.request_hmac("t1", "p1", req, k1) != C.request_hmac("t2", "p1", req, k1)

    def test_b2c_session_key_and_info(self, shared5c, monkeypatch):
        from bullhorn_mcp.identity.session_store import SessionKeys

        req = {"entity": "candidate"}
        monkeypatch.setattr(shared5c.store, "keys", SessionKeys({"a": b"\x05" * 32}, "a"), raising=False)
        first, cursor_key = C.request_hmac("t1", "p1", req), C.cursor_key()
        assert C.audit_key() != cursor_key  # its own HKDF info
        assert C.request_hmac("t1", "p1", req) == first
        monkeypatch.setattr(shared5c.store, "keys", SessionKeys({"b": b"\x06" * 32}, "b"), raising=False)
        assert C.request_hmac("t1", "p1", req) != first

    @respx.mock
    def test_provenance_request_hash_is_keyed(self, tmp_path):
        store = tenant_store(tmp_path, FULL_CONFIG)
        query_route("Candidate", [[]])
        args = {"entity": "candidate"}
        out = R.find_records(context(store), args)
        assert out["provenance"]["request_hash"] == C.provenance_hash("local", "local:tester", args)
        assert out["provenance"]["request_hash"] != C.request_hmac("local", "local:tester", args)
        assert out["provenance"]["request_hash"] not in _unkeyed_digests(tagged_json(["request/v1", args]))

    def test_b2d_grep(self):
        files = [*(SRC / "reads").glob("*.py"), SRC / "activity" / "service.py", SRC / "activity" / "derivers.py",
                 SRC / "tools" / "records.py"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            assert "hashlib.sha256(" not in text and "sha256_hex(" not in text and "hashlib" not in text, path.name
        assert not hasattr(R, "digest") and not hasattr(R, "digest_args") and not hasattr(A, "digest_args")


# ---------------------------------------------------------------------- #
# B-3 / SR-34: record ids in URL paths
# ---------------------------------------------------------------------- #


AUDIT_LOGGER = "bullhorn_mcp.audit"  # the frozen crosscutting/audit.py logger


def _is_legacy_get_job_audit(line: str) -> bool:
    """C5-1 / R2-L2: the one excluded record, matched by BOTH the audit logger name and the get_job tool."""
    name, _, message = line.partition("|")
    return name == AUDIT_LOGGER and '"tool": "get_job"' in message


def b3_leaks(text: str, sentinel: str) -> list[str]:
    """Captured lines containing ``sentinel``, except the legacy get_job audit record (P5C-3, C5-1)."""
    return [line for line in text.splitlines() if sentinel in line and not _is_legacy_get_job_audit(line)]


class TestB3:
    @respx.mock
    def test_b3a_path_ids_scrubbed(self, shared5c, capture):
        link(shared5c.store, TK1, ALICE)
        respx.get(f"{REST_1}/entity/JobOrder/{SENT_ID}/notes").mock(return_value=httpx.Response(200, json={"data": []}))
        respx.get(url__regex=rf".*/entity/JobOrder/{SENT_ID}.*").mock(return_value=httpx.Response(200, json={"data": {"id": SENT_ID}}))
        with caller(ALICE):
            out = json.loads(record_tools.get_activity(concepts=["note_created"], scope_type="job", scope_id=SENT_ID))
            jobs.get_job(job_id=SENT_ID)
        assert out["concepts"]["note_created"]["status"] == "ok"
        text = capture()
        assert b3_leaks(text, str(SENT_ID)) == []
        assert re.search(r"HTTP Request: GET https://\S+/entity/JobOrder/<id>/notes\?<query-REDACTED>", text)
        assert re.search(r"HTTP Request: GET https://\S+/entity/JobOrder/<id>", text)

    def test_r2g_exclusion_negative_control(self, capture):
        """R2-L2: a non-audit record carrying '"tool": "get_job"' and the sentinel is NOT excluded."""
        logging.getLogger(AUDIT_LOGGER).info(json.dumps({"tool": "get_job", "args": {"job_id": SENT_ID}}))
        assert b3_leaks(capture(), str(SENT_ID)) == []  # the genuine legacy record is the only exclusion
        logging.getLogger("bullhorn_mcp.other").info(json.dumps({"tool": "get_job", "args": {"job_id": SENT_ID}}))
        leaks = b3_leaks(capture(), str(SENT_ID))
        assert leaks and all(line.startswith("bullhorn_mcp.other|") for line in leaks)
        logging.getLogger(AUDIT_LOGGER).info(json.dumps({"tool": "get_activity", "args": {"scope_id": SENT_ID}}))
        assert any(line.startswith(AUDIT_LOGGER + "|") for line in b3_leaks(capture(), str(SENT_ID)))

    def test_b3b_scrub_rules(self, shared5c):
        s = log_scrub.scrub_query_strings
        assert s("GET https://h/rest-services/abc123/entity/Candidate/42 x") == "GET https://h/rest-services/abc123/entity/Candidate/<id> x"
        assert s("https://h/a/12b/7?q=1") == "https://h/a/12b/<id>?<query-REDACTED>"
        assert s("http://127.0.0.1:8080/cb?code=1") == "http://127.0.0.1:8080/cb?<query-REDACTED>"
        assert s("no url 123/456") == "no url 123/456"

    def test_b3b_local_mode_noop(self):
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: GET %s", ("https://h/entity/X/123",), None)
        before = record.getMessage()
        assert not deploy.is_shared()
        log_scrub.QueryStringScrubFilter().filter(record)
        assert record.getMessage() == before


# ---------------------------------------------------------------------- #
# Local fixes
# ---------------------------------------------------------------------- #


class TestL1:
    def test_single_concept_source(self):
        assert set(caps.ACTIVITY_CONCEPTS) == load_activity_concepts()
        text = (SRC / "tenant" / "capabilities.py").read_text(encoding="utf-8")
        assert '"job_status_changed", "candidate_status_changed"' not in text


class TestL2:
    def _doc(self, tmp_path):
        store = tenant_store(tmp_path, [PRIMARY_RECRUITER])
        doc = store.read_version(store.active_version()).to_dict()
        rec = next(r for r in doc["field_mappings"] if r["field"] == "primary_recruiter_id")
        inactive = {**copy.deepcopy(rec), "active": False, "target": "customInt9"}
        return store, doc, rec, inactive

    @pytest.mark.parametrize("inactive_first", [True, False])
    def test_parser_resolves_active(self, tmp_path, inactive_first):
        _, doc, rec, inactive = self._doc(tmp_path)
        others = [r for r in doc["field_mappings"] if r is not rec]
        doc["field_mappings"] = others + ([inactive, rec] if inactive_first else [rec, inactive])
        profile = TenantProfileV2.from_dict(doc)
        assert len([r for r in profile.field_mappings if r.field == "primary_recruiter_id"]) == 2
        assert records_by_key(profile)["field:job.primary_recruiter_id"]["active"] is True
        assert records_by_key(profile)["field:job.primary_recruiter_id"]["target"] == "customInt3"
        states, _ = effective_states(profile, None)
        active = profile.field_record("job", "primary_recruiter_id", active_only=True)
        assert active is not None and states["field:job.primary_recruiter_id"] == active.validation.state

    @pytest.mark.parametrize("inactive_first", [True, False])
    def test_import_resolves_active_and_preserves(self, tmp_path, inactive_first):
        store, doc, rec, inactive = self._doc(tmp_path)
        others = [r for r in doc["field_mappings"] if r is not rec]
        doc["field_mappings"] = others + ([inactive, rec] if inactive_first else [rec, inactive])
        doc["settings"]["reporting_timezone"] = "Europe/Paris"  # a real change, so the import is not a no-op
        path = tmp_path / "export.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        from ._tenant_helpers import propose_and_commit

        result = propose_and_commit(store, [{"op": "import_document", "path": str(path)}])
        profile = store.read_version(store.active_version())
        group = [r for r in profile.field_mappings if r.field == "primary_recruiter_id"]
        assert result["status"] == "committed"
        assert sorted(r.active for r in group) == [False, True]  # both records preserved
        active = profile.field_record("job", "primary_recruiter_id", active_only=True)
        assert active is not None and active.target.sources == ("customInt3",)
        assert records_by_key(profile)["field:job.primary_recruiter_id"]["active"] is True


class TestL3:
    def _cursor(self, key=b"k" * 32):
        binding = C.Binding("t", "p", None, "h", 1)
        return binding, C.encode(binding, "offset", 5, 1000, key=key)

    def test_canonical_round_trip(self):
        binding, cur = self._cursor()
        assert C.decode(cur, binding, 1000, key=b"k" * 32) == ("offset", 5)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda b, s: f"{b}.{s}=",
            lambda b, s: f"{b}=.{s}",
            lambda b, s: f"{b}.{s.replace('-', '+').replace('_', '/')}" if ("-" in s or "_" in s) else f"{b}.{s}+",
            lambda b, s: f"{b}.{base64.b64encode(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))).decode()}",
            lambda b, s: f"{b}.{s[:-1]}",
            lambda b, s: f"{b} .{s}",
        ],
    )
    def test_non_canonical_rejected(self, mutate):
        binding, cur = self._cursor()
        body, sig = cur.split(".")
        with pytest.raises(C.InvalidCursor):
            C.decode(mutate(body, sig), binding, 1000, key=b"k" * 32)

    def test_constant_time_string_compare(self):
        text = (SRC / "reads" / "cursor.py").read_text(encoding="utf-8")
        assert 'hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii"))' in text
