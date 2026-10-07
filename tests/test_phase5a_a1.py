"""Amendment A1: exact type checks in client.py (R-A1a) and the settings filter applied to drift (R-A1b)."""

from __future__ import annotations

import pytest
import respx

from bullhorn_mcp.bullhorn.errors import BullhornAPIError
from bullhorn_mcp.notes import action_discovery
from bullhorn_mcp.tenant.revalidation import NoteActionSource, NoteActionSources, note_action_drift

from ._notes_helpers import make_client
from .test_notes_action_discovery import nrec, profile_with


class EvilStr(str):
    def __format__(self, spec):
        return "../settings/x"

    def __str__(self):
        return "../settings/x"


class EvilInt(int):
    def __format__(self, spec):
        return "1/../../settings"

    def __str__(self):
        return "1/../../settings"


class TestExactTypes:
    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.search(EvilStr("JobOrder"), "id:1"),
            lambda c: c.query(EvilStr("JobOrder"), "id=1"),
            lambda c: c.get(EvilStr("JobOrder"), 1),
            lambda c: c.get_meta(EvilStr("JobOrder")),
            lambda c: c.get("JobOrder", EvilInt(1)),
            lambda c: c.get("JobOrder", True),
        ],
        ids=["search-str", "query-str", "get-str", "meta-str", "get-int", "get-bool"],
    )
    def test_subclasses_rejected_with_zero_requests(self, call):
        with respx.mock(assert_all_mocked=False) as router:
            with pytest.raises(BullhornAPIError):
                call(make_client())
        assert not router.calls

    def test_plain_types_still_accepted(self):
        with respx.mock(assert_all_mocked=False) as router:
            router.get(url__regex=r".*/entity/JobOrder/1.*").respond(200, json={"data": {"id": 1}})
            assert make_client().get("JobOrder", 1) == {"id": 1}


def _sources(meta, settings_values):
    return NoteActionSources(
        "2026-10-07T00:00:00Z",
        {"meta": NoteActionSource("verified", tuple(meta)), "settings": NoteActionSource("verified", tuple(settings_values))},
    )


class TestDriftIgnoresUnverifiedSettings:
    def test_forged_settings_snapshot_is_absent_while_flag_off(self):
        assert action_discovery.SETTINGS_ACTION_SOURCE_VERIFIED is False
        profile = profile_with(nrec("note.action.a", ["Meta A"]), nrec("note.action.s", ["Only In Settings"]))
        drift = note_action_drift(_sources(["Meta A"], ["Only In Settings", "New From Settings"]), profile)
        assert "New From Settings" not in drift["new_values"]
        assert drift["stale_values"] == ["Only In Settings"]  # computed from the verified meta source only
        assert drift["sources"]["settings"] == "unresolved"

    def test_flag_on_restores_the_union(self, monkeypatch):
        monkeypatch.setattr(action_discovery, "SETTINGS_ACTION_SOURCE_VERIFIED", True)
        profile = profile_with(nrec("note.action.s", ["Only In Settings"]))
        drift = note_action_drift(_sources(["Meta A"], ["Only In Settings", "New From Settings"]), profile)
        assert drift["stale_values"] == [] and set(drift["new_values"]) == {"Meta A", "New From Settings"}
