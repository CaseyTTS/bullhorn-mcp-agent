"""Phase 5C ``get_activity`` service (§2, §3.2; AC-7, AC-9, AC-10, AC-14, AC-16)."""

from __future__ import annotations

import datetime as dt
import zoneinfo

import httpx
import pytest
import respx

from bullhorn_mcp.activity import service as A
from bullhorn_mcp.notes import reads as note_reads
from bullhorn_mcp.reads import support as sup
from bullhorn_mcp.tenant.timeutil import epoch_millis_to_local_iso, parse_bound

from ._phase5c_helpers import (
    CLIENT_SUBMISSION,
    DATING,
    FULL_CONFIG,
    INTERVIEW,
    INTERVIEW_CANCELLED,
    INTERVIEW_DONE,
    OFFER_ACCEPTED,
    OFFER_DECLINED,
    OFFER_EXTENDED,
    PLACEMENT_RECRUITER,
    PRIMARY_RECRUITER,
    RULE_END,
    RULE_MAPPED,
    bh_calls,
    concept,
    context,
    params,
    query_route,
    tenant_store,
)
from ._tenant_helpers import REST_URL, make_store

SOFT = "(isDeleted = false OR isDeleted IS NULL)"
RANGE = {"date_from": "2026-09-01", "date_to": "2026-10-01"}
FROM_MS, TO_MS = 1788220800000, 1790812800000
TS = 1_790_000_000_000


def without(*items):
    return [c for c in FULL_CONFIG if all(c is not i for i in items)]


def run(store, args, **kw):
    return A.get_activity(context(store, **kw), args)


@pytest.fixture
def store(tmp_path):
    return tenant_store(tmp_path)


def support_with(mutate):
    doc = sup.packaged_document()
    mutate(doc)
    return sup.QuerySupport.from_dict(doc)


# ---------------------------------------------------------------------- #
# Validation
# ---------------------------------------------------------------------- #


class TestValidation:
    @respx.mock
    @pytest.mark.parametrize(
        "args, code",
        [
            ({"concepts": []}, "invalid_concept"),
            ({"concepts": ["x"] * 9}, "invalid_concept"),
            ({"concepts": ["meeting_held"], **RANGE}, "invalid_concept"),
            ({"concepts": ["job_created", "job_created"], **RANGE}, "invalid_concept"),
            ({"concepts": ["job_created"]}, "invalid_value"),
            ({"concepts": ["job_created"], "date_from": "2026-09-01"}, "invalid_value"),
            ({"concepts": ["job_created"], "scope_type": "job"}, "invalid_value"),
            ({"concepts": ["job_created"], "scope_type": "note", "scope_id": 1}, "invalid_value"),
            ({"concepts": ["job_created"], "scope_type": "job", "scope_id": True}, "invalid_value"),
            ({"concepts": ["job_created"], "recruiter_id": 0, **RANGE}, "invalid_value"),
            ({"concepts": ["job_created"], "date_from": "2026-09-01T00:00:00", "date_to": "2026-10-01"}, "invalid_value"),
            ({"concepts": ["job_created"], "date_from": "2026-10-01", "date_to": "2026-09-01"}, "invalid_value"),
            ({"concepts": ["job_created"], "date_from": "2025-01-01", "date_to": "2026-01-03"}, "range_too_wide"),
            ({"concepts": ["job_created"], "limit": 201, **RANGE}, "invalid_value"),
            ({"concepts": ["job_created"], "cursor": 1, **RANGE}, "invalid_cursor"),
            ({"concepts": ["job_created"], "cursor": "bogus.cursor", **RANGE}, "invalid_cursor"),
        ],
    )
    def test_rejected_without_calls(self, store, args, code):
        out = run(store, args)
        assert out["status"] == "rejected_validation" and code in {e["code"] for e in out["errors"]}, out
        assert not bh_calls()

    @respx.mock
    def test_scoped_range_may_exceed_366_days(self, store):
        query_route("JobOrder", [[]])
        out = run(store, {"concepts": ["job_created"], "scope_type": "job", "scope_id": 5, "date_from": "2020-01-01",
                          "date_to": "2026-01-01"})
        assert out["status"] == "ok"

    @respx.mock
    def test_setup_required(self, tmp_path):
        out = run(make_store(tmp_path), {"concepts": ["submission_created"], **RANGE})
        assert out["status"] == "setup_required" and not bh_calls()


# ---------------------------------------------------------------------- #
# AC-7: every §2 row fails closed with zero record requests
# ---------------------------------------------------------------------- #


MISSING_CASES = [
    ("client_submission", without(CLIENT_SUBMISSION), ["value_mapping:submission:client_submission"]),
    ("client_submission", without(DATING), ["setting:client_submission_dating"]),
    ("interview_scheduled", without(INTERVIEW), ["value_mapping:appointment:interview_scheduled"]),
    ("interview_completed", without(RULE_END), ["setting:interview_completion_rule"]),
    ("interview_completed", without(INTERVIEW_DONE), ["value_mapping:appointment:interview_completed"]),
    ("interview_completed", without(INTERVIEW_CANCELLED), ["value_mapping:appointment:interview_cancelled"]),
    ("interview_cancelled", without(INTERVIEW_CANCELLED), ["value_mapping:appointment:interview_cancelled"]),
    ("interview_upcoming", without(INTERVIEW_CANCELLED), ["value_mapping:appointment:interview_cancelled"]),
    ("interview_upcoming", without(INTERVIEW), ["value_mapping:appointment:interview_scheduled"]),
    ("offer_extended", without(OFFER_EXTENDED), ["value_mapping:submission|placement:offer_extended"]),
    ("offer_accepted", without(OFFER_ACCEPTED), ["value_mapping:submission|placement:offer_accepted"]),
    ("offer_declined", without(DATING), ["setting:client_submission_dating"]),
    ("offer_pending", without(OFFER_ACCEPTED, OFFER_DECLINED), ["value_mapping:submission|placement:offer_accepted|offer_declined"]),
    ("job_created", without(PRIMARY_RECRUITER), ["mapping:job.primary_recruiter_id"]),
]


class TestFailClosed:
    @respx.mock
    @pytest.mark.parametrize("concept_id, config, missing", MISSING_CASES, ids=[f"{c}-{m[0]}" for c, _, m in MISSING_CASES])
    def test_definition_missing(self, tmp_path, concept_id, config, missing):
        store = tenant_store(tmp_path, config)
        out = run(store, {"concepts": [concept_id], **RANGE})
        block = out["concepts"][concept_id]
        assert block["status"] == "definition_missing" and block["missing_requirements"] == missing
        assert block["events"] == [] and not bh_calls()

    @respx.mock
    @pytest.mark.parametrize(
        "concept_id, reason, hv",
        [
            ("interview_rescheduled", None, "HV-Q9"),
            ("job_status_changed", "status_history_unresolved", "HV-Q10"),
            ("candidate_status_changed", "status_history_unresolved", "HV-Q10"),
        ],
    )
    def test_unsupported_concepts(self, store, concept_id, reason, hv):
        block = run(store, {"concepts": [concept_id], **RANGE})["concepts"][concept_id]
        assert block["status"] == "unsupported" and block["unsupported"][0]["code"] == "unsupported_concept"
        assert block["unsupported"][0]["hv"] == hv and (reason is None or block["unsupported"][0]["reason"] == reason)
        assert not bh_calls()

    @respx.mock
    def test_status_history_dating_unsupported(self, store, monkeypatch):
        """A (forged) ``status_history`` setting is unsupported while HV-Q10 is unresolved."""
        from dataclasses import replace

        from bullhorn_mcp.reads import records as R

        original = R.load_tenant

        def forged(ctx):
            original(ctx)
            ctx.profile = replace(ctx.profile, settings=replace(ctx.profile.settings, client_submission_dating="status_history"))

        monkeypatch.setattr(A, "load_tenant", forged)
        out = A.get_activity(context(store), {"concepts": ["client_submission"], **RANGE})
        block = out["concepts"]["client_submission"]
        assert block["status"] == "unsupported" and block["unsupported"][0]["reason"] == "status_history_unresolved"
        assert not bh_calls()

    @respx.mock
    def test_revalidation_blocks_concepts_but_not_notes(self, tmp_path):
        store = tenant_store(tmp_path, drift=True)
        respx.get(f"{REST_URL}/entity/JobOrder/5/notes").mock(return_value=httpx.Response(200, json={"data": []}))
        out = run(store, {"concepts": ["submission_created", "client_submission", "note_created"], "scope_type": "job", "scope_id": 5})
        assert out["concepts"]["submission_created"]["status"] == "setup_revalidation_required"
        assert out["concepts"]["client_submission"]["status"] == "setup_revalidation_required"
        assert out["concepts"]["note_created"]["status"] == "ok"
        assert all("/query/" not in str(c.request.url) for c in bh_calls())

    @respx.mock
    def test_status_history_setting_rejected_at_validation(self, tmp_path):
        from bullhorn_mcp.tenant.changes import propose
        from bullhorn_mcp.tenant.profile_v2 import ProfileError

        from ._tenant_helpers import NOW, env

        store = tenant_store(tmp_path)
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "set_setting", "name": "client_submission_dating", "value": "status_history"}], now=NOW, env=env(store))
        assert "HV-Q10" in str(info.value)


# ---------------------------------------------------------------------- #
# Derivation (AC-10, D-5C-14, provenance)
# ---------------------------------------------------------------------- #


APPT = {"id": 5, "type": "Interview", "customText1": None, "dateAdded": TS, "dateBegin": TS + 3_600_000,
        "dateEnd": TS + 7_200_000, "owner": {"id": 3}, "candidateReference": {"id": 1}, "jobOrder": {"id": 2},
        "parentAppointment": None, "isDeleted": False}


class TestInterviews:
    @respx.mock
    def test_only_classified_parent_appointments(self, store):
        child = {**APPT, "id": 6, "parentAppointment": {"id": 5}}
        route = query_route("Appointment", [[APPT, child]])
        sub_route = query_route("JobSubmission", [[{"id": 9, "status": "Interview", "dateAdded": TS, "isDeleted": False}]])
        out = run(store, {"concepts": ["interview_scheduled"], **RANGE})
        events = out["concepts"]["interview_scheduled"]["events"]
        assert [e["source"] for e in events] == [{"canonical_entity": "appointment", "id": 5}]  # one per parent
        assert params(route.calls[0])["where"] == (
            f"{SOFT} AND parentAppointment IS NULL AND type IN ('Interview') AND dateAdded >= {FROM_MS} AND dateAdded < {TO_MS}"
        )
        assert sub_route.call_count == 0  # a submission status never produces an interview
        assert any(w.startswith("recurrence_not_expanded") for w in out["warnings"])

    @respx.mock
    def test_submission_status_named_interview_is_not_an_interview(self, tmp_path):
        store = tenant_store(tmp_path, [*without(INTERVIEW), concept("sub.iv", "submission", "status", "client_submission", ["Interview"])])
        out = run(store, {"concepts": ["interview_scheduled"], **RANGE})
        assert out["concepts"]["interview_scheduled"]["status"] == "definition_missing" and not bh_calls()

    @respx.mock
    def test_completed_mapped_state_only(self, tmp_path):
        store = tenant_store(tmp_path, [*without(RULE_END), RULE_MAPPED])
        route = query_route("Appointment", [[{**APPT, "customText1": "Done"}]])
        out = run(store, {"concepts": ["interview_completed"], **RANGE})
        assert params(route.calls[0])["where"] == (
            f"{SOFT} AND parentAppointment IS NULL AND type IN ('Interview') AND customText1 IN ('Done')"
            f" AND dateEnd >= {FROM_MS} AND dateEnd < {TO_MS}"
        )
        evt = out["concepts"]["interview_completed"]["events"][0]
        assert evt["definition"]["settings"] == {"interview_completion_rule": "mapped_state_only"}

    @respx.mock
    def test_completed_end_passed(self, store):
        route = query_route("Appointment", [[APPT]])
        run(store, {"concepts": ["interview_completed"], **RANGE})
        now_ms = 1791288000000  # NOW
        assert params(route.calls[0])["where"] == (
            f"{SOFT} AND parentAppointment IS NULL AND dateEnd < {now_ms} AND type IN ('Interview')"
            f" AND (customText1 NOT IN ('Cancelled') OR customText1 IS NULL) AND dateEnd >= {FROM_MS} AND dateEnd < {TO_MS}"
        )

    @respx.mock
    def test_cancelled_has_no_dating(self, store):
        out = run(store, {"concepts": ["interview_cancelled"], **RANGE})
        block = out["concepts"]["interview_cancelled"]
        assert block["status"] == "unsupported" and block["unsupported"][0]["code"] == "unsupported_filter" and not bh_calls()
        query_route("Appointment", [[{**APPT, "customText1": "Cancelled"}]])
        block = run(store, {"concepts": ["interview_cancelled"], "scope_type": "job", "scope_id": 2})["concepts"]["interview_cancelled"]
        assert block["status"] == "ok" and block["events"][0]["state"] == "cancelled"

    @respx.mock
    def test_upcoming_state_uses_read_time(self, store):
        route = query_route("Appointment", [[APPT]])
        out = run(store, {"concepts": ["interview_upcoming"], "scope_type": "candidate", "scope_id": 1})
        assert "dateBegin >= 1791288000000" in params(route.calls[0])["where"]
        assert "candidateReference.id = 1" in params(route.calls[0])["where"]
        evt = out["concepts"]["interview_upcoming"]["events"][0]
        assert evt["state"] == "upcoming" and evt["occurred_at"] == "2026-10-06T12:00:00Z"
        assert evt["evidence"]["defining_at"] == "2026-09-21T15:13:20Z"

    @respx.mock
    def test_invitee_copies_unresolved(self, store):
        support = support_with(lambda d: d["entities"]["Appointment"].pop("instance_filter"))
        query_route("JobSubmission", [[]])
        out = run(store, {"concepts": ["interview_scheduled", "submission_created"], **RANGE}, support=support)
        assert out["concepts"]["submission_created"]["status"] == "ok"
        assert out["concepts"]["interview_scheduled"]["unsupported"][0]["reason"] == "invitee_copies_unresolved"
        assert all("/Appointment" not in str(c.request.url) for c in bh_calls())


class TestEvents:
    @respx.mock
    def test_event_shape_and_dates(self, tmp_path):
        store = tenant_store(tmp_path, [*FULL_CONFIG, {"op": "set_setting", "name": "reporting_timezone", "value": "America/Chicago"}])
        route = query_route("JobSubmission", [[{"id": 9, "status": "Client Submitted", "dateAdded": TS, "sendingUser": {"id": 3},
                                                "candidate": {"id": 1}, "jobOrder": {"id": 2}, "isDeleted": False}]])
        out = run(store, {"concepts": ["client_submission"], **RANGE})
        # Date-only bounds are local midnight in the reporting timezone (CDT, -05:00).
        assert params(route.calls[0])["where"].endswith(f"dateAdded >= {FROM_MS + 5 * 3_600_000} AND dateAdded < {TO_MS + 5 * 3_600_000}")
        assert out["reporting_timezone"] == "America/Chicago"
        evt = out["concepts"]["client_submission"]["events"][0]
        assert set(evt) == {"activity_id", "concept", "occurred_at", "state", "source", "links", "unresolved_links", "attribution",
                            "definition", "evidence", "origin", "occurred_at_local"}
        assert evt["occurred_at"] == "2026-09-21T14:13:20Z" and evt["occurred_at_local"] == "2026-09-21T09:13:20.000-05:00"
        assert evt["origin"] == "observed" and evt["source"] == {"canonical_entity": "submission", "id": 9}
        assert evt["links"]["recruiter_id"] == 3 and evt["links"]["candidate_id"] == 1 and evt["links"]["submission_id"] == 9
        assert evt["attribution"]["rule"] == "attr.client_submission.v1" and evt["attribution"]["field"] == "submission.sending_user_id"
        assert evt["definition"]["mappings"] == ["sub.client"]
        assert evt["evidence"]["matched"] == [{"mapping": "sub.client", "concept": "client_submission", "value": "Client Submitted"}]

    @respx.mock
    def test_unresolved_links_reported_never_dropped(self, tmp_path):
        store = tenant_store(tmp_path, without(PLACEMENT_RECRUITER))
        query_route("Placement", [[{"id": 4, "dateAdded": TS, "candidate": {"id": 1}, "jobOrder": {"id": 2}, "jobSubmission": {"id": 9},
                                    "customInt2": 77}]])
        evt = run(store, {"concepts": ["placement_created"], **RANGE})["concepts"]["placement_created"]["events"][0]
        assert evt["links"]["recruiter_id"] is None and "recruiter_id" in evt["unresolved_links"]
        assert evt["links"]["client_corporation_id"] == 77 and evt["links"]["submission_id"] == 9
        assert evt["attribution"]["resolved"] is False

    @respx.mock
    def test_recruiter_filter_unsupported_only_for_that_concept(self, tmp_path):
        store = tenant_store(tmp_path, without(PLACEMENT_RECRUITER))
        sub = query_route("JobSubmission", [[]])
        out = run(store, {"concepts": ["placement_created", "submission_created"], "recruiter_id": 3, **RANGE})
        assert out["concepts"]["placement_created"]["status"] == "unsupported"
        assert out["concepts"]["submission_created"]["status"] == "ok"
        assert "sendingUser.id = 3" in params(sub.calls[0])["where"]
        assert all("/Placement" not in str(c.request.url) for c in bh_calls())

    @respx.mock
    def test_job_created_attribution(self, store):
        route = query_route("JobOrder", [[{"id": 2, "dateAdded": TS, "customInt3": 11, "owner": {"id": 3}, "isDeleted": False}]])
        out = run(store, {"concepts": ["job_created"], "recruiter_id": 11, **RANGE})
        assert "customInt3 = 11" in params(route.calls[0])["where"]
        evt = out["concepts"]["job_created"]["events"][0]
        assert evt["links"]["recruiter_id"] == 11 and evt["attribution"]["field"] == "job.primary_recruiter_id"

    @respx.mock
    def test_offer_on_submission_only(self, store):
        sub = query_route("JobSubmission", [[{"id": 9, "status": "Offer Extended", "dateAdded": TS, "isDeleted": False}]])
        out = run(store, {"concepts": ["offer_pending"], **RANGE})
        evt = out["concepts"]["offer_pending"]["events"][0]
        assert evt["state"] == "pending" and "status IN ('Offer Extended')" in params(sub.calls[0])["where"]
        assert all("/Placement" not in str(c.request.url) for c in bh_calls())

    @respx.mock
    def test_unscoped_scope_unsupported_for_concept(self, store):
        out = run(store, {"concepts": ["job_created"], "scope_type": "candidate", "scope_id": 1})
        assert out["concepts"]["job_created"]["status"] == "unsupported" and not bh_calls()


class TestNotes:
    @respx.mock
    def test_note_created_delegates_to_note_reads(self, store):
        route = respx.get(f"{REST_URL}/entity/JobOrder/5/notes").mock(return_value=httpx.Response(200, json={"data": [
            {"id": 77, "dateAdded": TS, "action": "Screen Call", "isDeleted": False, "commentingPerson": {"id": 3}}]}))
        out = run(store, {"concepts": ["note_created"], "scope_type": "job", "scope_id": 5})
        block = out["concepts"]["note_created"]
        assert route.call_count == 1 and block["status"] == "ok"
        evt = block["events"][0]
        assert evt["concept"] == "note_created" and evt["occurred_at_local"] and evt["attribution"]["rule"] == "attr.note_created.v1"

    @respx.mock
    @pytest.mark.parametrize("extra", [{"scope_type": "candidate", "scope_id": 1}, {"scope_type": "job", "scope_id": 5, "recruiter_id": 3},
                                       {"scope_type": "job", "scope_id": 5, **RANGE}])
    def test_note_filters_unsupported(self, store, extra):
        block = run(store, {"concepts": ["note_created"], **extra})["concepts"]["note_created"]
        assert block["status"] == "unsupported" and block["unsupported"][0]["code"] == "unsupported_filter" and not bh_calls()

    def test_single_note_path(self):
        assert A.NOTE_SCOPES == tuple(note_reads.SUPPORTED_SCOPES)


# ---------------------------------------------------------------------- #
# Paging (SC-11) and cursor
# ---------------------------------------------------------------------- #


class TestPaging:
    @respx.mock
    def test_per_concept_page_cap_and_cursor(self, store):
        rows = [{"id": i, "dateAdded": TS, "isDeleted": True} for i in range(1, 3)]
        route = query_route("JobSubmission", [rows])
        out = run(store, {"concepts": ["submission_created"], "limit": 1, **RANGE})
        assert route.call_count == 5
        block = out["concepts"]["submission_created"]
        assert block["truncated"] is True and block["complete"] is False and out["next_cursor"]
        nxt = run(store, {"concepts": ["submission_created"], "limit": 1, **RANGE, "cursor": out["next_cursor"]})
        assert params(route.calls[5])["start"] == "10" and nxt["status"] == "ok"

    @respx.mock
    def test_cursor_bound_to_request(self, store):
        query_route("JobSubmission", [[{"id": i, "dateAdded": TS, "isDeleted": False} for i in range(1, 4)]])
        out = run(store, {"concepts": ["submission_created"], "limit": 1, **RANGE})
        n = len(bh_calls())
        bad = run(store, {"concepts": ["submission_created"], "limit": 2, **RANGE, "cursor": out["next_cursor"]})
        assert bad["status"] == "rejected_validation" and len(bh_calls()) == n


# ---------------------------------------------------------------------- #
# Dates (AC-14)
# ---------------------------------------------------------------------- #


def _expected(day: str, zone: str, fold: int) -> int:
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=zoneinfo.ZoneInfo(zone), fold=fold).timestamp() * 1000)


class TestDates:
    @pytest.mark.parametrize(
        "zone, day",
        [
            ("America/Havana", "2026-03-08"),  # DST starts at 00:00: local midnight does not exist (gap)
            ("America/Santiago", "2026-09-06"),  # DST starts at 24:00 on Saturday: midnight does not exist (gap)
            ("America/Havana", "2026-11-01"),  # DST ends at 01:00 -> 00:00: midnight is ambiguous (overlap)
            ("Asia/Beirut", "2026-10-25"),  # DST ends at 00:00 -> 23:00 the day before: overlap around midnight
        ],
    )
    def test_dst_edges_use_fold_zero(self, zone, day):
        assert parse_bound(day, "date_from", zone) == _expected(day, zone, 0)

    def test_tzdata_present_and_transitions_real(self):
        assert _expected("2026-03-08", "America/Havana", 0) != _expected("2026-03-08", "America/Havana", 1)
        assert _expected("2026-11-01", "America/Havana", 0) != _expected("2026-11-01", "America/Havana", 1)

    @pytest.mark.parametrize("value", ["2026-09-01T00:00:00", "2026-09-01T00:00", "20260901", "2026-02-30", "x" * 41])
    def test_rejected(self, value):
        with pytest.raises(ValueError):
            parse_bound(value, "date_from", "UTC")

    def test_millisecond_edges(self):
        assert parse_bound("2026-09-01T00:00:00.000Z", "d", "UTC") == FROM_MS
        assert parse_bound("2026-09-01T00:00:00.000001Z", "d", "UTC") == FROM_MS + 1  # rounds up: [from, to) stays exact
        assert parse_bound("2026-09-01T00:00:00.001+00:00", "d", "UTC") == FROM_MS + 1

    def test_local_rendering(self):
        assert epoch_millis_to_local_iso(TS, "Asia/Kolkata") == "2026-09-21T19:43:20.000+05:30"
        assert epoch_millis_to_local_iso(TS, "UTC") == "2026-09-21T14:13:20.000+00:00"

    def test_single_parser(self):
        import inspect

        from bullhorn_mcp.notes import reads

        assert "_DATETIME_RE" not in inspect.getsource(reads)
        assert reads.parse_bound("2026-09-01", "d", "UTC") == parse_bound("2026-09-01", "d", "UTC")
