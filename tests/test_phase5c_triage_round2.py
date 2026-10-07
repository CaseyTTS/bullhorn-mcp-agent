"""Phase 5C review triage, round 2: R2-B1 (T-5C-R2a..d, SR-36), R2-L1 (T-5C-R2e), R2-L3 (T-5C-R2h) and
R2-T1 (``test_phase5c_triage_concept_predicate_parity``). R2-L2 (T-5C-R2g) is in ``test_phase5c_triage_fixes.py``."""

from __future__ import annotations

import ast
import base64
import json
import random
from pathlib import Path

import httpx
import pytest
import respx

from bullhorn_mcp.activity import service as A
from bullhorn_mcp.reads import cursor as C
from bullhorn_mcp.reads import records as R
from bullhorn_mcp.reads import support as sup
from bullhorn_mcp.tools import records as record_tools

from . import _phase5c_helpers as H
from ._identity_helpers import ALICE, BOB, REST_1, TK1, caller, link
from .test_phase5c_triage_fixes import b3_leaks, capture, shared5c  # noqa: F401 - fixtures

SRC = Path(R.__file__).resolve().parents[1]
SECRET = "Hiddenname"


def _audit(text: str, tool: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        name, _, message = line.partition("|")
        if name == "bullhorn_mcp.audit" and f'"tool": "{tool}"' in message:
            out.append(json.loads(message))
    return out


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _cursor_values(cursor: str | None) -> list[str]:
    if not cursor:
        return []
    body = cursor.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return [cursor, *_strings(payload)]


def _page(*ids):
    return {"data": [{"id": i, "lastName": "N", "isDeleted": False} for i in ids]}


# ---------------------------------------------------------------------- #
# R2-B1 / SR-36
# ---------------------------------------------------------------------- #


class TestR2B1:
    @respx.mock
    def test_r2a_oracle_same_tenant(self, shared5c, capture):  # noqa: F811
        link(shared5c.store, TK1, ALICE)
        link(shared5c.store, TK1, BOB)
        respx.get(url__regex=r".*/query/.*").mock(return_value=httpx.Response(200, json=_page(1, 2)))
        args = {"entity": "candidate", "filters": [{"field": "last_name", "op": "eq", "value": SECRET}], "limit": 1}
        with caller(BOB):
            record_tools.find_records(**args)
        bob_hmac = _audit(capture(), "find_records")[-1]["args"]["request_hmac"]
        with caller(ALICE):
            alice_out = json.loads(record_tools.find_records(**args))
        visible = _strings(alice_out) + _cursor_values(alice_out.get("next_cursor"))
        assert alice_out["status"] == "ok" and alice_out["next_cursor"]
        assert bob_hmac not in visible and bob_hmac not in json.dumps(alice_out)
        alice_hmac = _audit(capture(), "find_records")[-1]["args"]["request_hmac"]
        assert alice_hmac != bob_hmac
        for guess in ("Smithxxxxx", SECRET, "Otherguess"):  # the oracle of the review no longer exists
            with caller(ALICE):
                o = json.loads(record_tools.find_records(entity="candidate", filters=[{"field": "last_name", "op": "eq", "value": guess}]))
            assert o["provenance"]["request_hash"] != bob_hmac

    @respx.mock
    def test_r2b_audit_hmac_never_in_output(self, shared5c, capture):  # noqa: F811
        link(shared5c.store, TK1, ALICE)
        respx.get(url__regex=r".*/query/.*").mock(return_value=httpx.Response(200, json=_page(1, 2)))
        with caller(ALICE):
            outs = [
                json.loads(record_tools.find_records(entity="candidate", limit=1)),
                json.loads(record_tools.get_activity(concepts=["submission_created"], scope_type="job", scope_id=5, limit=1)),
                json.loads(record_tools.find_records(entity="candidate", sort={"field": "last_name", "direction": "asc"})),
            ]
        hmacs = {a["args"]["request_hmac"] for tool in ("find_records", "get_activity") for a in _audit(capture(), tool)}
        assert len(hmacs) == 3 and all(hmacs)  # each record is captured three times (stream, handler, audit file)
        for out in outs:
            visible = _strings(out) + _cursor_values(out.get("next_cursor"))
            blob = json.dumps(out)
            for h in hmacs:
                assert h not in visible and h not in blob
        assert outs[0]["provenance"]["request_hash"] not in hmacs

    def test_r2c_stability_and_binding(self):
        req = {"entity": "candidate", "filters": [{"field": "last_name", "op": "eq", "value": SECRET}]}
        k = b"\x07" * 32
        base = C.request_hmac("t1", "alice", req, k)
        assert base == C.request_hmac("t1", "alice", json.loads(json.dumps(req)), k)
        assert base != C.request_hmac("t1", "bob", req, k)
        assert base != C.request_hmac("t2", "alice", req, k)
        assert C.request_hmac("t1", "alice", req) != C.provenance_hash("t1", "alice", req)  # separate keys
        assert C.audit_key() != C.provenance_key() != C.cursor_key() != C.audit_key()

    def test_r2c_cursor_r_is_not_the_audit_or_provenance_value(self):
        binding = C.Binding("t1", "alice", "bh-link:x", C.provenance_hash("t1", "alice", {"entity": "candidate"}), 1)
        cur = C.encode(binding, "offset", 2, 1000)
        values = _cursor_values(cur)
        assert C.request_hmac("t1", "alice", {"entity": "candidate"}) not in values
        assert binding.request_hash not in values  # r is keyed with the cursor key over principal and link

    def test_r2d_grep(self):
        infos = ["bullhorn-mcp/cursor/v1", "bullhorn-mcp/audit-digest/v2", "bullhorn-mcp/provenance/v1"]
        text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
        for info in infos:
            assert text.count(info) == 1, info
        assert "bullhorn-mcp/audit-digest/v1" not in text
        # request_hmac is produced only inside the audit helpers; no response builder references it.
        allowed = {"request_id", "audit_args"}
        for path in (SRC / "reads" / "records.py", SRC / "activity" / "service.py", SRC / "tools" / "records.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef) and fn.name not in allowed:
                    names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                    names |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                    consts = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                    assert "request_hmac" not in names and "request_hmac" not in consts, (path.name, fn.name)
                    assert "request_id" not in names or fn.name in ("audit_args",), (path.name, fn.name)


# ---------------------------------------------------------------------- #
# R2-L1: comma-joined ids
# ---------------------------------------------------------------------- #


class TestR2L1:
    @respx.mock
    def test_r2e_comma_joined_ids(self, shared5c, capture):  # noqa: F811
        import logging

        import httpx as _httpx

        ids = "918273001,918273002"
        respx.get(url__regex=r".*/entity/Note/.*").mock(return_value=httpx.Response(200, json={"data": []}))
        with _httpx.Client() as client:  # a 5B association-path request, logged by httpx itself
            client.get(f"{REST_1}/entity/Note/918273000/candidates/{ids}?fields=id")
        logging.getLogger("httpcore.http11").debug("GET %s", f"{REST_1}/entity/Note/918273000/candidates/{ids}")
        text = capture()
        for sentinel in ("918273000", "918273001", "918273002"):
            assert b3_leaks(text, sentinel) == []
        assert "/entity/Note/<id>/candidates/<id>?<query-REDACTED>" in text

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://h/a/1,2,3", "https://h/a/<id>"),
            ("https://h/a/1,2/b?x=1", "https://h/a/<id>/b?<query-REDACTED>"),
            ("https://h/a/1,x", "https://h/a/1,x"),
            ("https://h/a/,1", "https://h/a/,1"),
            ("https://h/a/12b", "https://h/a/12b"),
        ],
    )
    def test_pattern(self, shared5c, url, expected):  # noqa: F811
        from bullhorn_mcp.bullhorn.log_scrub import scrub_query_strings

        assert scrub_query_strings(url) == expected


# ---------------------------------------------------------------------- #
# R2-L3: exact HV labels on every guard
# ---------------------------------------------------------------------- #


def _support(mutate):
    doc = sup.packaged_document()
    mutate(doc)
    return sup.QuerySupport.from_dict(doc)


FIND_GUARDS = [
    (lambda d: d["entities"].pop("Candidate"), {"entity": "candidate"}, "unsupported_entity", "HV-Q1"),
    (lambda d: d["entities"]["Candidate"]["filterable"].__setitem__("firstName", {"ops": ["in"], "hv": "HV-Q2"}),
     {"entity": "candidate", "filters": [{"field": "first_name", "op": "eq", "value": "x"}]}, "unsupported_operator", "HV-Q2"),
    (lambda d: None, {"entity": "candidate", "filters": [{"field": "first_name", "op": "starts_with", "value": "x"}]},
     "unsupported_operator", "HV-Q2"),
    (lambda d: None, {"entity": "candidate", "filters": [{"field": "last_name", "op": "eq", "value": "O'Brien"}]},
     "unsupported_value", "HV-Q2"),
    (lambda d: d["entities"]["JobOrder"].__setitem__("operation", "search"), {"entity": "job"}, "unsupported_entity", "HV-Q3"),
    (lambda d: None, {"entity": "candidate", "sort": {"field": "last_name", "direction": "asc"}}, "unsupported_sort", "HV-Q4"),
    (lambda d: d["entities"]["Candidate"]["filterable"].pop("dateAdded"),
     {"entity": "candidate", "filters": [{"field": "date_added", "op": "gte", "value": "2026-01-01"}]}, "unsupported_filter", "HV-Q5"),
    (lambda d: d["entities"]["Candidate"].__setitem__("soft_delete", {"unresolved": True, "reason": "x", "hv": "HV-Q6"}),
     {"entity": "candidate"}, "unsupported_entity", "HV-Q6"),
    (lambda d: None, {"entity": "candidate", "include_deleted": True}, "unsupported_filter", "HV-Q6"),
    (lambda d: d["entities"].pop("CorporateUser"), {"entity": "user"}, "unsupported_entity", "HV-Q8"),
    (lambda d: None, {"entity": "appointment", "concept": "interview_rescheduled"}, "unsupported_concept", "HV-Q9"),
    (lambda d: d["entities"]["Appointment"].pop("instance_filter"), {"entity": "appointment", "concept": "interview_scheduled"},
     "unsupported_concept", "HV-Q9b"),
    (lambda d: d["syntax"]["query"].__setitem__("max_where_chars", 10), {"entity": "candidate"}, "unsupported_value", "HV-Q12"),
    (lambda d: d["syntax"]["query"].__setitem__("nested_fields_verified", False),
     {"entity": "candidate", "filters": [{"field": "owner_id", "op": "eq", "value": 1}]}, "unsupported_filter", "HV-Q13"),
]


@pytest.fixture(scope="module")
def guard_store(tmp_path_factory):
    return H.tenant_store(tmp_path_factory.mktemp("g"), [*H.FULL_CONFIG[:-1], H.RULE_MAPPED])


class TestR2L3:
    @respx.mock
    @pytest.mark.parametrize("mutate, args, code, hv", FIND_GUARDS, ids=[f"{g[2]}-{g[3]}" for g in FIND_GUARDS])
    def test_r2h_find_guard_labels(self, guard_store, mutate, args, code, hv):
        out = R.find_records(H.context(guard_store, support=_support(mutate)), args)
        assert out["status"] == "unsupported", out
        assert [(u["code"], u["hv"]) for u in out["unsupported"]] == [(code, hv)]
        assert not H.bh_calls()

    @respx.mock
    @pytest.mark.parametrize(
        "concept, args, code, hv",
        [
            ("interview_rescheduled", {"date_from": "2026-09-01", "date_to": "2026-10-01"}, "unsupported_concept", "HV-Q9"),
            ("interview_cancelled", {"date_from": "2026-09-01", "date_to": "2026-10-01"}, "unsupported_filter", "HV-Q9"),
            ("job_status_changed", {"date_from": "2026-09-01", "date_to": "2026-10-01"}, "unsupported_concept", "HV-Q10"),
            ("candidate_status_changed", {"date_from": "2026-09-01", "date_to": "2026-10-01"}, "unsupported_concept", "HV-Q10"),
            ("note_created", {"scope_type": "candidate", "scope_id": 1}, "unsupported_filter", "HV-B5"),
        ],
    )
    def test_r2h_activity_guard_labels(self, guard_store, concept, args, code, hv):
        block = A.get_activity(H.context(guard_store), {"concepts": [concept], **args})["concepts"][concept]
        assert block["status"] == "unsupported" and {(u["code"], u["hv"]) for u in block["unsupported"]} == {(code, hv)}
        assert not H.bh_calls()


# ---------------------------------------------------------------------- #
# R2-T1: current-state predicate parity
# ---------------------------------------------------------------------- #


PL_OFFER = H.concept("pl.offer", "placement", "status", "offer_extended", ["Offered"])
PL_ACC = H.concept("pl.acc", "placement", "status", "offer_accepted", ["Acc"])
BASE = [H.PRIMARY_RECRUITER, H.PRIORITY, H.PRIORITY_ORDER, H.APPOINTMENT_STATUS, H.PLACEMENT_RECRUITER, H.PLACEMENT_CLIENT]
OPTIONAL = {"INT": H.INTERVIEW, "DONE": H.INTERVIEW_DONE, "CANC": H.INTERVIEW_CANCELLED, "CS": H.CLIENT_SUBMISSION,
            "OE": H.OFFER_EXTENDED, "OA": H.OFFER_ACCEPTED, "OD": H.OFFER_DECLINED, "PLO": PL_OFFER, "PLA": PL_ACC, "DATING": H.DATING}
FIND = {"client_submission": ["submission"], "interview_scheduled": ["appointment"], "interview_completed": ["appointment"],
        "interview_cancelled": ["appointment"], "offer_extended": ["submission", "placement"],
        "offer_accepted": ["submission", "placement"], "offer_declined": ["submission", "placement"]}
BH = {"submission": "JobSubmission", "placement": "Placement", "appointment": "Appointment"}
SCOPE_TERMS = ("candidateReference.id = 1", "candidate.id = 1")


def _configs():
    rng = random.Random(7)
    keys = list(OPTIONAL)
    out = [(keys, None), (keys, "MAPPED"), (keys, "END"), ([], None)]
    for _ in range(24):
        out.append(([k for k in keys if rng.random() < 0.6], rng.choice([None, "MAPPED", "END"])))
    return out


def _terms(where: str) -> set[str]:
    return set(where.split(" AND "))


def _wheres(calls, bh: str) -> list[str]:
    return [c.request.url.params["where"] for c in calls if c.request.url.path.endswith(f"/query/{bh}")]


@pytest.mark.parametrize("selected, rule", _configs(), ids=lambda v: str(v)[:40])
def test_phase5c_triage_concept_predicate_parity(tmp_path, selected, rule):
    changes = BASE + [OPTIONAL[k] for k in selected]
    changes += [H.RULE_MAPPED] if rule == "MAPPED" else [H.RULE_END] if rule == "END" else []
    store = H.tenant_store(tmp_path, changes)
    row = [{"id": 1, "dateAdded": 1_700_000_000_000, "isDeleted": False, "status": "x", "type": "Interview", "customText1": "Done",
            "dateEnd": 1_600_000_000_000, "dateBegin": 1_600_000_000_000, "parentAppointment": None}]
    for concept, entities in FIND.items():
        with respx.mock:
            for bh in BH.values():
                H.query_route(bh, [row])
            act = A.get_activity(H.context(store), {"concepts": [concept], "scope_type": "candidate", "scope_id": 1})
            block = act["concepts"][concept] if act.get("status") == "ok" else act
            act_calls = list(respx.calls)
        for entity in entities:
            with respx.mock:
                for bh in BH.values():
                    H.query_route(bh, [row])
                found = R.find_records(H.context(store), {"entity": entity, "concept": concept})
                find_calls = list(respx.calls)
            label = (selected, rule, concept, entity)
            if found["status"] != "ok":
                assert not find_calls, label
            if block["status"] == "definition_missing":
                assert found == {"status": "definition_missing", "missing_requirements": block["missing_requirements"]}, label
            elif block["status"] == "ok":
                if concept == "interview_completed" and rule == "END":
                    assert found["unsupported"][0]["reason"] == "rule_not_renderable_as_current_state", label
                    continue
                if found["status"] == "definition_missing":  # an offer mapped only on the other entity
                    assert concept.startswith("offer_") and found["missing_requirements"] == [f"value_mapping:{entity}:{concept}"], label
                    continue
                assert found["status"] == "ok", label
                f_where = _wheres(find_calls, BH[entity])[0]
                a_wheres = _wheres(act_calls, BH[entity])
                assert a_wheres, label
                # The same current-state predicate: get_activity adds only its scope term.
                a_terms = _terms(a_wheres[0])
                assert _terms(f_where) <= a_terms, label
                assert all(t in SCOPE_TERMS for t in a_terms - _terms(f_where)), label
            else:
                assert found["status"] != "ok", label
