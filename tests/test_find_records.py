"""Phase 5C ``find_records`` service (§3.1; AC-5..AC-9, AC-11, AC-12, AC-15, AC-16)."""

from __future__ import annotations

import datetime as dt

import pytest
import respx

from bullhorn_mcp.reads import records as R
from bullhorn_mcp.reads import support as sup
from bullhorn_mcp.reads.cursor import decode, encode

from ._phase5c_helpers import (
    CLIENT_SUBMISSION,
    FULL_CONFIG,
    INTERVIEW,
    INTERVIEW_CANCELLED,
    NOW,
    PLACEMENT_RECRUITER,
    bh_calls,
    commit_more,
    context,
    local_ident,
    make_client,
    params,
    query_route,
    snapshot_only_store,
    tenant_store,
    user_ident,
)
from ._tenant_helpers import make_store

SOFT = "(isDeleted = false OR isDeleted IS NULL)"
ADA = {
    "id": 1,
    "firstName": "Ada",
    "lastName": "Lovelace",
    "email": "ada@example.test",
    "status": "Active",
    "dateAdded": 1_790_000_000_000,
    "owner": {"id": 7},
    "isDeleted": False,
}


def rows(n, start=1, **extra):
    return [{"id": i, "firstName": f"N{i}", "isDeleted": False, **extra} for i in range(start, start + n)]


@pytest.fixture
def store(tmp_path):
    return tenant_store(tmp_path)


def support_with(mutate):
    doc = sup.packaged_document()
    mutate(doc)
    return sup.QuerySupport.from_dict(doc)


def run(store, args, **kw):
    return R.find_records(context(store, **kw), args)


# ---------------------------------------------------------------------- #
# Happy path, rendering and output shape
# ---------------------------------------------------------------------- #


class TestBasics:
    @respx.mock
    def test_exact_params_and_shape(self, store):
        route = query_route("Candidate", [[ADA]])
        out = run(
            store,
            {
                "entity": "candidate",
                "filters": [{"field": "first_name", "op": "eq", "value": "Ada"}, {"field": "owner_id", "op": "in", "value": [7, 8]}],
            },
        )
        assert out["status"] == "ok"
        assert params(route.calls[0]) == {
            "where": f"{SOFT} AND firstName = 'Ada' AND owner.id IN (7, 8)",
            "fields": "id,firstName,lastName,email,status,dateAdded,isDeleted,owner(id)",
            "count": "26",
            "start": "0",
        }
        rec = out["records"][0]
        assert rec == {
            "id": 1,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.test",
            "status": "Active",
            "date_added": "2026-09-21T14:13:20Z",
            "owner_id": 7,
            "source": {"system": "bullhorn", "entity": "candidate", "id": 1},
        }
        assert set(out) == {
            "status",
            "entity",
            "records",
            "count",
            "truncated",
            "complete",
            "next_cursor",
            "consistency",
            "reporting_timezone",
            "provenance",
            "warnings",
        }
        assert out["consistency"] == "offset" and out["complete"] is True and out["truncated"] is False
        assert out["next_cursor"] is None and out["reporting_timezone"] == "UTC"
        assert set(out["provenance"]) == {
            "profile_version",
            "catalog_fingerprint",
            "query_support_version",
            "vocabulary_version",
            "request_hash",
            "retrieved_at",
        }
        assert any(w.startswith("consistency: offset") for w in out["warnings"])

    @respx.mock
    def test_no_filters_uses_soft_delete_term_only(self, store):
        route = query_route("JobOrder", [[]])
        run(store, {"entity": "job"})
        assert params(route.calls[0])["where"] == SOFT

    @respx.mock
    @pytest.mark.parametrize("entity, bh", [("placement", "Placement"), ("client_corporation", "ClientCorporation")])
    def test_not_soft_deletable_entities(self, store, entity, bh):
        route = query_route(bh, [[]])
        run(store, {"entity": entity})
        assert params(route.calls[0])["where"] == "id IS NOT NULL"
        assert "isDeleted" not in params(route.calls[0])["fields"]

    @respx.mock
    def test_deleted_rows_post_filtered(self, store):
        query_route("Candidate", [[ADA, {**ADA, "id": 2, "isDeleted": True}, {**ADA, "id": 3, "isDeleted": None}]])
        out = run(store, {"entity": "candidate"})
        assert [r["id"] for r in out["records"]] == [1, 3]
        assert any("1 deleted record(s) excluded" in w for w in out["warnings"])

    @respx.mock
    def test_user_entity_full_name(self, store):
        route = query_route(
            "CorporateUser",
            [
                [
                    {
                        "id": 4,
                        "firstName": "Grace",
                        "lastName": "Hopper",
                        "email": "g@example.test",
                        "status": "Active",
                        "userDateAdded": 1_790_000_000_000,
                        "isDeleted": None,
                    }
                ]
            ],
        )
        out = run(store, {"entity": "user", "fields": ["id", "full_name", "date_added"]})
        assert out["records"][0] == {
            "id": 4,
            "full_name": "Grace Hopper",
            "date_added": "2026-09-21T14:13:20Z",
            "source": {"system": "bullhorn", "entity": "user", "id": 4},
        }
        assert params(route.calls[0])["fields"] == "id,firstName,lastName,userDateAdded,isDeleted"

    @respx.mock
    def test_text_field_only_when_requested_and_bounded(self, store):
        route = query_route("JobOrder", [[{"id": 9, "description": "d" * 2500, "isDeleted": False}]])
        out = run(store, {"entity": "job"})
        assert "description" not in out["records"][0] if out["records"] else True
        assert "description" not in params(route.calls[0])["fields"]
        out = run(store, {"entity": "job", "fields": ["id", "description"]})
        assert out["records"][0]["description"] == {"text": "d" * 2000, "truncated": True, "length": 2500}

    @respx.mock
    def test_date_filter_half_open_epoch_ms(self, store):
        route = query_route("Candidate", [[]])
        run(
            store,
            {
                "entity": "candidate",
                "filters": [
                    {"field": "date_added", "op": "gte", "value": "2026-09-01"},
                    {"field": "date_added", "op": "lt", "value": "2026-09-02T00:00:00.0005Z"},
                ],
            },
        )
        assert params(route.calls[0])["where"] == f"{SOFT} AND dateAdded >= 1788220800000 AND dateAdded < 1788307200001"

    @respx.mock
    def test_placement_submission_default_and_unmapped_recruiter(self, tmp_path):
        store = tenant_store(tmp_path, [c for c in FULL_CONFIG if c is not PLACEMENT_RECRUITER])
        route = query_route("Placement", [[{"id": 3, "jobSubmission": {"id": 44}}]])
        out = run(store, {"entity": "placement", "filters": [{"field": "submission_id", "op": "eq", "value": 44}]})
        assert params(route.calls[0])["where"] == "id IS NOT NULL AND jobSubmission.id = 44"
        assert out["records"][0]["submission_id"] == 44
        before = len(bh_calls())
        out = run(store, {"entity": "placement", "filters": [{"field": "recruiter_id", "op": "eq", "value": 5}]})
        assert out == {"status": "definition_missing", "missing_requirements": ["mapping:placement.recruiter_id"]}
        assert len(bh_calls()) == before


# ---------------------------------------------------------------------- #
# Validation, unsupported and restricted (AC-5, AC-6, AC-9)
# ---------------------------------------------------------------------- #


class TestRejections:
    @respx.mock
    @pytest.mark.parametrize(
        "args, code",
        [
            ({"entity": "Candidate"}, "invalid_value"),
            ({"entity": "note"}, "invalid_value"),
            ({"entity": "candidate", "limit": 0}, "invalid_value"),
            ({"entity": "candidate", "limit": 101}, "invalid_value"),
            ({"entity": "candidate", "limit": True}, "invalid_value"),
            ({"entity": "candidate", "include_deleted": "yes"}, "invalid_value"),
            ({"entity": "candidate", "filters": [{"field": "frist_name", "op": "eq", "value": "x"}]}, "unknown_field"),
            ({"entity": "candidate", "filters": [{"field": "firstName", "op": "eq", "value": "x"}]}, "unknown_field"),
            ({"entity": "candidate", "fields": ["isDeleted"]}, "unknown_field"),
            ({"entity": "candidate", "concept": "client_submission"}, "invalid_concept"),
            ({"entity": "submission", "concept": "job_created"}, "invalid_concept"),
            ({"entity": "candidate", "cursor": 5}, "invalid_cursor"),
        ],
    )
    def test_rejected_without_calls(self, store, args, code):
        query_route("Candidate", [[]])
        out = run(store, args)
        assert out["status"] == "rejected_validation"
        assert out["errors"][0]["code"] == code
        assert not bh_calls()

    @respx.mock
    def test_typo_suggestion(self, store):
        out = run(store, {"entity": "candidate", "filters": [{"field": "frist_name", "op": "eq", "value": "x"}]})
        assert out["errors"][0]["suggestions"] == ["first_name"]

    @respx.mock
    @pytest.mark.parametrize(
        "args, code",
        [
            ({"entity": "candidate", "filters": [{"field": "first_name", "op": "starts_with", "value": "Ad"}]}, "unsupported_operator"),
            ({"entity": "candidate", "filters": [{"field": "last_name", "op": "eq", "value": "O'Brien"}]}, "unsupported_value"),
            ({"entity": "job", "filters": [{"field": "salary", "op": "gt", "value": 1.5}]}, "unsupported_value"),
            ({"entity": "candidate", "filters": [{"field": "full_name", "op": "eq", "value": "Ada"}]}, "unsupported_filter"),
            ({"entity": "candidate", "filters": [{"field": "skills", "op": "eq", "value": "x"}]}, "unsupported_filter"),
            ({"entity": "candidate", "sort": {"field": "first_name", "direction": "asc"}}, "unsupported_sort"),
            ({"entity": "job", "sort": {"field": "priority", "direction": "desc"}}, "unsupported_sort"),
            ({"entity": "candidate", "include_deleted": True}, "unsupported_filter"),
            ({"entity": "placement", "include_deleted": True}, "unsupported_filter"),
            ({"entity": "appointment", "concept": "interview_rescheduled"}, "unsupported_concept"),
        ],
    )
    def test_unsupported_without_calls(self, store, args, code):
        query_route("Candidate", [[]])
        out = run(store, args)
        assert out["status"] == "unsupported", out
        assert code in {u["code"] for u in out["unsupported"]}
        assert not bh_calls()

    @respx.mock
    def test_one_bad_filter_blocks_the_whole_request(self, store):
        out = run(
            store,
            {
                "entity": "candidate",
                "filters": [{"field": "first_name", "op": "eq", "value": "Ada"}, {"field": "last_name", "op": "eq", "value": "O'Brien"}],
            },
        )
        assert out["status"] == "unsupported" and not bh_calls()

    @respx.mock
    def test_where_length_cap(self, store):
        values = [f"{'x' * 190}{i:03d}" for i in range(50)]
        out = run(store, {"entity": "candidate", "filters": [{"field": "first_name", "op": "in", "value": values}]})
        assert out["status"] == "unsupported" and out["unsupported"][0]["hv"] == "HV-Q12"
        assert not bh_calls()


class TestRestricted:
    @pytest.fixture
    def rstore(self, tmp_path):
        return tenant_store(
            tmp_path,
            [
                *FULL_CONFIG,
                {
                    "op": "set_field_mapping",
                    "entity": "candidate",
                    "field": "tax_note",
                    "kind": "custom",
                    "type": "string",
                    "target": "customEncryptedText1",
                },
            ],
        )

    @respx.mock
    @pytest.mark.parametrize(
        "args",
        [
            {"entity": "candidate", "filters": [{"field": "tax_note", "op": "eq", "value": "x"}]},
            {"entity": "candidate", "fields": ["id", "tax_note"]},
            {"entity": "candidate", "sort": {"field": "tax_note", "direction": "asc"}},
        ],
    )
    def test_sensitive_custom_mapping(self, rstore, args):
        query_route("Candidate", [[]])
        out = run(rstore, args)
        assert out["status"] == "rejected_validation"
        assert out["errors"][0]["code"] == "restricted_field" and out["errors"][0]["item"] == "tax_note"
        assert not bh_calls()

    @respx.mock
    def test_custom_mapping_filterable(self, tmp_path):
        store = tenant_store(
            tmp_path,
            [
                *FULL_CONFIG,
                {
                    "op": "set_field_mapping",
                    "entity": "candidate",
                    "field": "region",
                    "kind": "custom",
                    "type": "string",
                    "target": "customText3",
                },
            ],
        )
        route = query_route("Candidate", [[{"id": 1, "customText3": "North", "isDeleted": False}]])
        out = run(
            store, {"entity": "candidate", "fields": ["id", "region"], "filters": [{"field": "region", "op": "eq", "value": "North"}]}
        )
        assert params(route.calls[0])["where"] == f"{SOFT} AND customText3 = 'North'"
        assert out["records"][0]["region"] == "North"


# ---------------------------------------------------------------------- #
# Concepts and priority (§2, §3.1)
# ---------------------------------------------------------------------- #


class TestConcepts:
    @respx.mock
    def test_client_submission_current_state(self, store):
        route = query_route("JobSubmission", [[]])
        assert run(store, {"entity": "submission", "concept": "client_submission"})["status"] == "ok"
        assert params(route.calls[0])["where"] == f"{SOFT} AND status IN ('Client Submitted')"

    @respx.mock
    def test_interview_parent_only(self, store):
        parent = {"id": 1, "type": "Interview", "parentAppointment": None, "isDeleted": False}
        child = {"id": 2, "type": "Interview", "parentAppointment": {"id": 1}, "isDeleted": False}
        route = query_route("Appointment", [[parent, child]])
        out = run(store, {"entity": "appointment", "concept": "interview_scheduled"})
        assert params(route.calls[0])["where"] == f"{SOFT} AND type IN ('Interview') AND parentAppointment IS NULL"
        assert "parentAppointment" in params(route.calls[0])["fields"]
        assert [r["id"] for r in out["records"]] == [1]  # an invitee copy is never returned

    @respx.mock
    def test_interview_cancelled_needs_both_mappings(self, tmp_path):
        store = tenant_store(tmp_path, [c for c in FULL_CONFIG if c is not INTERVIEW_CANCELLED])
        out = run(store, {"entity": "appointment", "concept": "interview_cancelled"})
        assert out == {"status": "definition_missing", "missing_requirements": ["value_mapping:appointment:interview_cancelled"]}
        assert not bh_calls()

    @respx.mock
    def test_missing_mapping(self, tmp_path):
        store = tenant_store(tmp_path, [c for c in FULL_CONFIG if c not in (CLIENT_SUBMISSION, INTERVIEW)])
        assert run(store, {"entity": "submission", "concept": "client_submission"})["missing_requirements"] == [
            "value_mapping:submission:client_submission"
        ]
        assert not bh_calls()

    @respx.mock
    def test_invitee_copies_unresolved(self, store):
        support = support_with(lambda d: d["entities"]["Appointment"].pop("instance_filter"))
        out = run(store, {"entity": "appointment", "concept": "interview_scheduled"}, support=support)
        assert out["status"] == "unsupported" and out["unsupported"][0]["reason"] == "invitee_copies_unresolved"
        assert not bh_calls()

    @respx.mock
    def test_priority(self, store):
        route = query_route("JobOrder", [[{"id": 2, "customText12": "B", "isDeleted": False}]])
        out = run(
            store, {"entity": "job", "fields": ["id", "priority"], "filters": [{"field": "priority", "op": "in", "value": ["A", "B"]}]}
        )
        assert params(route.calls[0])["where"] == f"{SOFT} AND customText12 IN ('A', 'B')"
        assert out["records"][0]["priority_rank"] == 2 and out["records"][0]["priority"] == "B"

    @respx.mock
    def test_priority_value_must_be_ordered(self, store):
        out = run(store, {"entity": "job", "filters": [{"field": "priority", "op": "eq", "value": "Z"}]})
        assert out["status"] == "rejected_validation" and out["errors"][0]["code"] == "invalid_value"
        assert "['A', 'B']" in out["errors"][0]["message"]
        assert not bh_calls()

    @respx.mock
    def test_priority_unconfigured(self, tmp_path):
        store = tenant_store(tmp_path, [])
        out = run(store, {"entity": "job", "filters": [{"field": "priority", "op": "eq", "value": "A"}]})
        assert out["status"] == "definition_missing" and "mapping:job.priority" in out["missing_requirements"]
        assert not bh_calls()


# ---------------------------------------------------------------------- #
# Setup gates (D-5C-6, D-5C-7)
# ---------------------------------------------------------------------- #


class TestSetupGates:
    @respx.mock
    def test_no_profile_no_snapshot(self, tmp_path):
        out = run(make_store(tmp_path), {"entity": "candidate"})
        assert out["status"] == "setup_required" and not bh_calls()

    @respx.mock
    def test_snapshot_only(self, tmp_path):
        out = run(snapshot_only_store(tmp_path), {"entity": "candidate"})
        assert out["status"] == "setup_required" and not bh_calls()

    @respx.mock
    def test_revalidation_plain_reads_work_with_warning(self, tmp_path):
        store = tenant_store(tmp_path, drift=True)
        query_route("Candidate", [[ADA]])
        out = run(store, {"entity": "candidate"})
        assert out["status"] == "ok" and any(w.startswith("setup_revalidation_required") for w in out["warnings"])

    @respx.mock
    @pytest.mark.parametrize(
        "args",
        [
            {"entity": "submission", "concept": "client_submission"},
            {"entity": "job", "filters": [{"field": "priority", "op": "eq", "value": "A"}]},
        ],
    )
    def test_revalidation_blocks_concept_dependent(self, tmp_path, args):
        store = tenant_store(tmp_path, drift=True)
        out = run(store, args)
        assert out["status"] == "setup_revalidation_required" and not bh_calls()

    @respx.mock
    def test_rest_url_binding(self, store):
        other = make_client(rest_url="https://rest42.bullhornstaffing.com/rest-services/zzz999")
        out = run(store, {"entity": "candidate"}, client=other)
        assert out["status"] == "setup_revalidation_required"
        assert out["missing_requirements"] == ["revalidate:rest_url_changed"]
        assert not bh_calls()


# ---------------------------------------------------------------------- #
# Paging (AC-11 as amended by C3-3) and cursors (AC-12)
# ---------------------------------------------------------------------- #


class TestPaging:
    @respx.mock
    def test_truncated_and_cursor_continues(self, store):
        route = query_route("Candidate", [rows(3), rows(1, start=3)])
        args = {"entity": "candidate", "limit": 2}
        first = run(store, args)
        assert [r["id"] for r in first["records"]] == [1, 2]
        assert first["truncated"] is True and first["complete"] is False and first["next_cursor"]
        assert params(route.calls[0])["count"] == "3"
        second = run(store, {**args, "cursor": first["next_cursor"]})
        assert params(route.calls[1])["start"] == "2"
        assert [r["id"] for r in second["records"]] == [3]
        assert second["complete"] is True and second["next_cursor"] is None

    @respx.mock
    def test_page_cap(self, store):
        route = query_route("Candidate", [rows(2, isDeleted=True)])
        route.side_effect = None
        out = run(store, {"entity": "candidate", "limit": 1})
        assert route.call_count == 5
        assert out["records"] == [] and out["truncated"] is True and out["complete"] is False and out["next_cursor"]
        assert [params(c)["start"] for c in route.calls] == ["0", "2", "4", "6", "8"]

    @respx.mock
    def test_complete_only_on_short_page(self, store):
        query_route("Candidate", [rows(26)])
        out = run(store, {"entity": "candidate"})
        assert out["count"] == 25 and out["complete"] is False and out["truncated"] is True

    @respx.mock
    def test_large_limit_splits_pages(self, store):
        route = query_route("Candidate", [rows(100), rows(1, start=101)])
        out = run(store, {"entity": "candidate", "limit": 100})
        assert [params(c)["count"] for c in route.calls] == ["100", "1"]
        assert out["count"] == 100 and out["truncated"] is True


class TestCursors:
    def _first(self, store, ident=None, client=None):
        query_route("Candidate", [rows(3)])
        out = run(store, {"entity": "candidate", "limit": 2}, ident=ident, client=client)
        self.before = len(bh_calls())
        return out["next_cursor"]

    def _again(self, store, cursor, args=None, **kw):
        return run(store, {"entity": "candidate", "limit": 2, **(args or {}), "cursor": cursor}, **kw)

    def _assert_invalid(self, out):
        assert out == {
            "status": "rejected_validation",
            "errors": [{"code": "invalid_cursor", "item": "cursor", "message": "invalid_cursor"}],
        }
        assert len(bh_calls()) == self.before

    @respx.mock
    def test_tampered(self, store):
        cur = self._first(store)
        body, sig = cur.split(".")
        self._assert_invalid(self._again(store, body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig))
        self._assert_invalid(self._again(store, "x" * 3000))
        self._assert_invalid(self._again(store, "not-a-cursor"))

    @respx.mock
    def test_expired(self, store):
        cur = self._first(store)
        self._assert_invalid(self._again(store, cur, now=NOW + dt.timedelta(hours=1, seconds=1)))

    @respx.mock
    def test_other_principal(self, store):
        cur = self._first(store, ident=user_ident("alice"))
        self._assert_invalid(self._again(store, cur, ident=user_ident("bob")))

    @respx.mock
    def test_other_tenant(self, store):
        cur = self._first(store, ident=user_ident("alice", tenant="t1"))
        self._assert_invalid(self._again(store, cur, ident=user_ident("alice", tenant="t2")))

    @respx.mock
    def test_other_link(self, store):
        cur = self._first(store, ident=user_ident("alice", link="bh-link:one"))
        self._assert_invalid(self._again(store, cur, ident=user_ident("alice", link="bh-link:two")))

    @respx.mock
    def test_other_request(self, store):
        cur = self._first(store)
        self._assert_invalid(self._again(store, cur, {"filters": [{"field": "first_name", "op": "eq", "value": "Ada"}]}))
        self._assert_invalid(self._again(store, cur, {"limit": 3}))

    @respx.mock
    def test_other_profile_version(self, store):
        cur = self._first(store)
        commit_more(store, [{"op": "set_setting", "name": "reporting_timezone", "value": "America/Chicago"}])
        self._assert_invalid(self._again(store, cur))

    @respx.mock
    def test_same_link_refresh_keeps_cursor(self, store):
        ident = user_ident("alice", link="bh-link:one")
        cur = self._first(store, ident=ident)
        query_route("Candidate", [rows(1, start=3)])
        assert self._again(store, cur, ident=ident)["status"] == "ok"

    def test_payload_contents(self):
        binding = R.cursors.Binding("tenant", "principal", "bh-link:x", "h" * 64, 3)
        cur = encode(binding, "offset", 26, 1000, key=b"k" * 32)
        import base64
        import json

        body = cur.split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        assert set(payload) == {"v", "t", "p", "b", "r", "pv", "m", "pos", "iat"}
        assert "tenant" not in body and "principal" not in json.dumps(payload) and payload["pos"] == 26
        assert decode(cur, binding, 1000, key=b"k" * 32) == ("offset", 26)


# ---------------------------------------------------------------------- #
# HV guards via the support resource (AC-8)
# ---------------------------------------------------------------------- #


class TestHvGuards:
    @respx.mock
    @pytest.mark.parametrize(
        "mutate, args, code, hv",
        [
            (lambda d: d["entities"].pop("Candidate"), {"entity": "candidate"}, "unsupported_entity", "HV-Q1"),
            (lambda d: d["entities"].pop("CorporateUser"), {"entity": "user"}, "unsupported_entity", "HV-Q8"),
            (lambda d: d["entities"]["JobOrder"].__setitem__("operation", "search"), {"entity": "job"}, "unsupported_entity", "HV-Q3"),
            (
                lambda d: d["entities"]["Candidate"].__setitem__("soft_delete", {"unresolved": True, "reason": "x", "hv": "HV-Q6"}),
                {"entity": "candidate"},
                "unsupported_entity",
                "HV-Q6",
            ),
            (
                lambda d: d["syntax"]["query"].__setitem__("association_paths_verified", False),
                {"entity": "candidate", "filters": [{"field": "owner_id", "op": "eq", "value": 1}]},
                "unsupported_filter",
                "HV-Q2",
            ),
            (
                lambda d: d["syntax"]["query"].__setitem__("nested_fields_verified", False),
                {"entity": "candidate", "filters": [{"field": "owner_id", "op": "eq", "value": 1}]},
                "unsupported_filter",
                "HV-Q13",
            ),
            (
                lambda d: d["entities"]["Candidate"]["filterable"].pop("dateAdded"),
                {"entity": "candidate", "filters": [{"field": "date_added", "op": "gte", "value": "2026-01-01"}]},
                "unsupported_filter",
                "HV-Q5",
            ),
            (
                lambda d: d["entities"]["Candidate"]["filterable"].__setitem__("firstName", {"ops": ["in"], "hv": "HV-Q2"}),
                {"entity": "candidate", "filters": [{"field": "first_name", "op": "eq", "value": "x"}]},
                "unsupported_operator",
                "HV-Q2",
            ),
            (lambda d: d["syntax"]["query"].__setitem__("max_where_chars", 10), {"entity": "candidate"}, "unsupported_value", "HV-Q12"),
        ],
    )
    def test_guard(self, store, mutate, args, code, hv):
        query_route("Candidate", [[]])
        out = run(store, args, support=support_with(mutate))
        assert out["status"] == "unsupported", out
        assert out["unsupported"][0]["code"] == code and out["unsupported"][0]["hv"] == hv
        assert not bh_calls()

    @respx.mock
    def test_nested_output_omitted_when_unresolved(self, store):
        route = query_route("Candidate", [[ADA]])
        out = run(
            store,
            {"entity": "candidate"},
            support=support_with(lambda d: d["syntax"]["query"].__setitem__("nested_fields_verified", False)),
        )
        assert "owner_id" not in out["records"][0] and "owner(" not in params(route.calls[0])["fields"]
        assert any("HV-Q13" in w for w in out["warnings"])


def test_local_identity_binding():
    ctx = R.make_context(local_ident(), make_client(), {}, NOW)
    b = ctx.binding("h")
    assert b.tenant == "local" and b.principal == "local:tester" and b.link is None
