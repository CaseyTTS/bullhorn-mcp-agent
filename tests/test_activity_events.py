"""Phase 4B: the activity event model (vocabulary section 1) and deterministic activity ids."""

import re

import pytest

from bullhorn_mcp.activity.events import LINK_KEYS, ActivityEvent, make_activity_id, tagged_json


class TestActivityId:
    def test_deterministic_sha256(self):
        a = make_activity_id("note_created", "note", 5)
        assert a == make_activity_id("note_created", "note", 5)
        assert re.fullmatch(r"[0-9a-f]{64}", a)

    @pytest.mark.parametrize(
        "other",
        [("note_created", "note", 6), ("note_created", "note", "5"), ("note_created", "job", 5), ("job_created", "note", 5),
         ("note_created", "note", 5, "x"), ("note_created", "note", True)],
    )
    def test_distinct(self, other):
        assert make_activity_id("note_created", "note", 5) != make_activity_id(*other)

    def test_tagged_json_types_never_collide(self):
        values = [1, "1", True, 1.0, None, [1], {"1": 1}]
        assert len({tagged_json(v) for v in values}) == len(values)

    def test_untaggable(self):
        with pytest.raises(TypeError):
            tagged_json(object())


class TestEvent:
    def test_to_dict_has_every_link_key(self):
        event = ActivityEvent(
            activity_id="a", concept="note_created", occurred_at="2026-01-01T00:00:00Z",
            source={"canonical_entity": "note", "id": 1}, links={"note_id": 1}, origin="observed", extra_links={"job_ids": [2]},
        )
        out = event.to_dict()
        assert set(LINK_KEYS) <= set(out["links"]) and out["links"]["note_id"] == 1 and out["links"]["job_ids"] == [2]
        assert out["links"]["candidate_id"] is None
        assert set(out) == {"activity_id", "concept", "occurred_at", "state", "source", "links", "unresolved_links",
                            "attribution", "definition", "evidence", "origin"}
