"""Phase 4B AC-20 and D-4B-11/12: the note_action value-target kind and action-type validation."""

import pytest

from bullhorn_mcp.notes.action_types import Rejection, suggestions_for, valid_set, validate
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.tenant.changes import propose
from bullhorn_mcp.tenant.profile_v2 import TenantProfileV2, load_note_action_semantics

from ._notes_helpers import NOTE_ACTION
from ._tenant_helpers import FINGERPRINT, NOW, env, init_tenant, make_store, snapshot, write_snapshot


def _doc(*value_mappings):
    return {
        "format": "tenant-profile/v2",
        "tenant": {"id": "acme"},
        "profile_version": 1,
        "catalog_fingerprint": FINGERPRINT,
        "value_mappings": list(value_mappings),
    }


def _vm(key, values, semantic="other", **extra):
    entry = {"key": key, "target": {"kind": "note_action", "semantic": semantic}, "bullhorn_field": "action", "values": values}
    entry.update(extra)
    return entry


class TestProfileKind:
    def test_tags_packaged(self):
        assert load_note_action_semantics() == (
            "candidate_screen",
            "client_call",
            "interview_feedback",
            "follow_up",
            "email",
            "reference_check",
            "other",
        )

    def test_parse_and_round_trip(self):
        profile = TenantProfileV2.from_dict(_doc(_vm("note.action.a", ["Call"], semantic=None)))
        rec = profile.value_mappings[0]
        assert rec.entity == "note" and rec.target.kind == "note_action" and rec.target.semantic is None
        assert rec.to_dict()["target"] == {"kind": "note_action", "semantic": None}
        assert TenantProfileV2.from_yaml_text(profile.to_yaml_text()) == profile

    @pytest.mark.parametrize("semantic", ["urgent", "", 5, ["other"], "Other"])
    def test_semantic_outside_tags(self, semantic):
        with pytest.raises(ProfileError):
            TenantProfileV2.from_dict(_doc(_vm("note.action.a", ["Call"], semantic=semantic)))

    def test_shared_value_is_not_a_conflict(self):
        profile = TenantProfileV2.from_dict(_doc(_vm("note.action.a", ["Call"]), _vm("note.action.b", ["Call"], semantic="email")))
        assert len(profile.value_mappings) == 2

    @pytest.mark.parametrize(
        "extra",
        [
            {"entity": "job"},
            {"bullhorn_field": "comments"},
            {"values": [5]},
            {"values": ["x" * 31]},
            {"values": ["  "]},
            {"target": {"kind": "note_action", "semantic": "other", "name": "x"}},
        ],
    )
    def test_shape_rules(self, extra):
        with pytest.raises(ProfileError):
            TenantProfileV2.from_dict(_doc({**_vm("note.action.a", ["Call"]), **extra}))

    def test_existing_concept_kind_still_conflicts(self):
        concept = {
            "key": "a.b",
            "entity": "submission",
            "target": {"kind": "concept", "name": "client_submission"},
            "bullhorn_field": "status",
            "values": ["Sub"],
        }
        with pytest.raises(ProfileError):
            TenantProfileV2.from_dict(_doc(concept, {**concept, "key": "a.c"}))

    def test_propose_commit_flow(self, tmp_path):
        store = make_store(tmp_path)
        write_snapshot(store, snapshot())
        init_tenant(store, [NOTE_ACTION])
        rec = store.read_version(1).value_record("note.action.screen")
        assert rec.validation.state == "valid" and rec.validation.values_unverified is True

    def test_propose_rejects_bad_semantic(self, tmp_path):
        store = make_store(tmp_path)
        bad = {**NOTE_ACTION, "target": {"kind": "note_action", "semantic": "nope"}}
        with pytest.raises(ProfileError):
            propose(store, [{"op": "init_tenant", "tenant_id": "acme"}, bad], now=NOW, env=env(store))


class TestValidate:
    def _set(self, tmp_path):
        store = make_store(tmp_path)
        write_snapshot(store, snapshot())
        follow = {**NOTE_ACTION, "key": "note.action.b", "values": ["Follow Up"]}
        follow["target"] = {"kind": "note_action", "semantic": "follow_up"}
        init_tenant(store, [NOTE_ACTION, follow])
        return valid_set(store.read_version(1))

    def test_exact_value_only(self, tmp_path):
        aset = self._set(tmp_path)
        assert aset.values == ("Screen Call", "Left Message", "Follow Up")
        assert validate("Screen Call", aset) == "Screen Call"
        for bad in ("screen call", "Screen Call ", "Screen", None, 5, "x" * 10_000):
            assert isinstance(validate(bad, aset), Rejection)

    def test_suggestions(self, tmp_path):
        aset = self._set(tmp_path)
        assert suggestions_for("SCREEN CALL", aset) == ("Screen Call",)
        assert suggestions_for("follow-up", aset) == ("Follow Up",)
        assert suggestions_for("candidate screen", aset) == ("Screen Call", "Left Message")
        assert suggestions_for("zz", aset) == ()

    def test_rejection_bounded(self, tmp_path):
        rejection = validate("x" * 10_000, self._set(tmp_path))
        assert len(rejection.to_dict()["message"]) < 300

    def test_inactive_or_broken_excluded(self, tmp_path):
        store = make_store(tmp_path)
        write_snapshot(store, snapshot())
        init_tenant(store, [NOTE_ACTION])
        profile = store.read_version(1)
        assert valid_set(profile, {"value:note.action.screen": "broken"}).values == ()
        assert valid_set(None).usable is False
