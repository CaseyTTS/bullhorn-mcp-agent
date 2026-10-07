"""Phase 4B AC-17: NoteRecord.from_bullhorn on hostile records, links, and note_created events."""

import math

import pytest

from bullhorn_mcp.activity.events import make_activity_id
from bullhorn_mcp.notes.model import LINK_TYPES, NoteRecord

GOOD = {
    "id": 10,
    "dateAdded": 1_700_000_000_123,
    "action": "Screen Call",
    "comments": "hello",
    "isDeleted": False,
    "personReference": {"id": 100},
    "commentingPerson": {"id": 7},
    "jobOrder": {"id": 200},
    "candidates": {"total": 2, "data": [{"id": 101}, {"id": 100}]},
    "clientContacts": [{"id": 300}],
    "placements": {"total": 0, "data": []},
    "jobOrders": {"total": 1, "data": [{"id": 201}]},
}


class TestParse:
    def test_good_record(self):
        rec, warnings = NoteRecord.from_bullhorn(GOOD, {"Screen Call": "candidate_screen"})
        assert warnings == []
        d = rec.to_dict()
        assert d["date_added"] == "2023-11-14T22:13:20.123Z"
        assert d["action_semantic"] == "candidate_screen"
        assert d["links"] == {
            "candidate_ids": [100, 101],
            "job_ids": [200, 201],
            "client_contact_ids": [300],
            "client_corporation_ids": [],
            "placement_ids": [],
            "submission_ids": [],
        }
        assert d["unresolved_links"] == ["client_corporation_ids", "submission_ids"]
        assert (d["person_id"], d["author_id"], d["is_deleted"]) == (100, 7, False)

    def test_unread_links_unresolved(self):
        rec, _ = NoteRecord.from_bullhorn({"id": 1, "jobOrder": {"id": 5}})
        assert set(rec.unresolved_links) == set(LINK_TYPES)
        assert rec.links["job_ids"] == (5,)

    def test_unmapped_action_semantic_null(self):
        rec, _ = NoteRecord.from_bullhorn({"id": 1, "action": "Other"}, {"Screen Call": "candidate_screen"})
        assert rec.action_semantic is None and rec.action_type == "Other"


HOSTILE = [
    None, [], "x", 5, {}, {"id": None}, {"id": "10"}, {"id": True}, {"id": 0}, {"id": -1}, {"id": 2**80}, {"id": 1.0},
    {"id": math.nan},
    {"id": 1, "dateAdded": math.nan}, {"id": 1, "dateAdded": "2020-01-01"}, {"id": 1, "dateAdded": 10**30},
    {"id": 1, "action": 5}, {"id": 1, "comments": {"x": 1}}, {"id": 1, "isDeleted": "yes"},
    {"id": 1, "personReference": 5}, {"id": 1, "personReference": {"id": "5"}}, {"id": 1, "commentingPerson": []},
    {"id": 1, "candidates": None}, {"id": 1, "candidates": "x"}, {"id": 1, "candidates": [1, None, "a", {"id": "2"}]},
    {"id": 1, "candidates": {"data": None, "total": 3}}, {"id": 1, "clientContacts": {"data": [{"id": math.inf}]}},
    {"id": 1, "candidates": {"total": 5000, "data": [{"id": i} for i in range(1, 1001)]}},
    {"id": 1, "candidates": [{"id": i} for i in range(1, 2001)]},
    {"id": 1, "jobOrders": {"data": [[1]]}, "jobOrder": {"id": 3}},
]


class TestHostile:
    @pytest.mark.parametrize("raw", HOSTILE)
    def test_never_raises(self, raw):
        rec, warnings = NoteRecord.from_bullhorn(raw)
        assert isinstance(warnings, list) and len(warnings) <= 20
        if rec is not None:
            rec.to_dict()
            rec.to_event()

    def test_thousand_associations(self):
        rec, warnings = NoteRecord.from_bullhorn({"id": 1, "candidates": [{"id": i} for i in range(1, 2001)]})
        assert len(rec.links["candidate_ids"]) == 1000 and any("first 1000" in w for w in warnings)

    def test_null_to_many_is_unresolved(self):
        rec, warnings = NoteRecord.from_bullhorn({"id": 1, "candidates": None})
        assert "candidate_ids" in rec.unresolved_links and warnings


class TestEvent:
    def test_same_activity_id_for_both_origins(self):
        rec, _ = NoteRecord.from_bullhorn(GOOD)
        observed, written = rec.to_event("observed").to_dict(), rec.to_event("written_by_mcp").to_dict()
        assert observed["activity_id"] == written["activity_id"] == make_activity_id("note_created", "note", 10)
        assert (observed["origin"], written["origin"]) == ("observed", "written_by_mcp")
        assert observed["occurred_at"] == "2023-11-14T22:13:20.123Z"
        assert observed["source"] == {"canonical_entity": "note", "id": 10}
        assert observed["links"]["note_id"] == 10 and observed["links"]["author_id"] == 7
        assert observed["links"]["candidate_ids"] == [100, 101]

    def test_bad_origin(self):
        rec, _ = NoteRecord.from_bullhorn(GOOD)
        with pytest.raises(ValueError):
            rec.to_event("guessed")
