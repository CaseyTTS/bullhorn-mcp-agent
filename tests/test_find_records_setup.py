"""Phase 5C setup-side changes the reads depend on (§6, §7): D-5C-15 settings, P4A-2, P4A-4, P5B-2, P5B-4, P5B-5,
P5B-6, P5B-13, and the new capabilities in ``setup_status``."""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from bullhorn_mcp.bullhorn import settings_reader as sr
from bullhorn_mcp.notes import action_discovery
from bullhorn_mcp.tenant import capabilities as caps
from bullhorn_mcp.tenant import profile_v2
from bullhorn_mcp.tenant.changes import propose
from bullhorn_mcp.tenant.profile_v2 import ProfileError, TenantProfileV2
from bullhorn_mcp.tenant.revalidation import note_action_drift

from ._phase5c_helpers import FULL_CONFIG, PRIMARY_RECRUITER, concept, make_client, tenant_store
from ._tenant_helpers import NOW, REST_URL, env, propose_and_commit


@pytest.fixture
def store(tmp_path):
    return tenant_store(tmp_path, [PRIMARY_RECRUITER])


def active(store):
    return store.read_version(store.active_version())


class TestSettings:
    @pytest.mark.parametrize(
        "name, value",
        [("client_submission_dating", "submission_date_added"), ("interview_completion_rule", "mapped_state_only"),
         ("interview_completion_rule", "end_passed_not_cancelled")],
    )
    def test_set(self, store, name, value):
        propose_and_commit(store, [{"op": "set_setting", "name": name, "value": value}])
        profile = active(store)
        assert getattr(profile.settings, name) == value and profile.settings.to_dict()[name] == value

    @pytest.mark.parametrize(
        "name, value",
        [("client_submission_dating", "status_history"), ("client_submission_dating", "whenever"),
         ("interview_completion_rule", None), ("interview_completion_rule", 1), ("unknown_setting", "x")],
    )
    def test_rejected(self, store, name, value):
        with pytest.raises(ProfileError):
            propose(store, [{"op": "set_setting", "name": name, "value": value}], now=NOW, env=env(store))

    def test_unset_not_emitted(self, store):
        assert active(store).settings.to_dict() == {"reporting_timezone": "UTC"}

    def test_timezone_change_keeps_other_settings(self, store):
        propose_and_commit(store, [{"op": "set_setting", "name": "interview_completion_rule", "value": "mapped_state_only"}])
        propose_and_commit(store, [{"op": "set_setting", "name": "reporting_timezone", "value": "Europe/Paris"}])
        s = active(store).settings
        assert s.reporting_timezone == "Europe/Paris" and s.interview_completion_rule == "mapped_state_only"

    def test_document_round_trip_and_validation(self, store):
        propose_and_commit(store, [{"op": "set_setting", "name": "client_submission_dating", "value": "submission_date_added"}])
        doc = active(store).to_dict()
        assert TenantProfileV2.from_dict(copy.deepcopy(doc)).settings.client_submission_dating == "submission_date_added"
        doc["settings"]["client_submission_dating"] = "status_history"
        with pytest.raises(ProfileError):
            TenantProfileV2.from_dict(doc)


class TestP4A:
    def test_p4a_2_cross_entity_values_do_not_conflict(self, store):
        result = propose(store, [concept("s", "submission", "status", "client_submission", ["Shared"]),
                                 concept("p", "placement", "status", "offer_accepted", ["Shared"])], now=NOW, env=env(store))
        assert not result["conflicts"]

    def test_p4a_2_same_entity_still_conflicts(self, store):
        result = propose(store, [concept("a", "submission", "status", "client_submission", ["Same"]),
                                 concept("b", "submission", "status", "offer_accepted", ["Same"])], now=NOW, env=env(store))
        assert result["conflicts"]

    def test_p4a_4_one_active_plus_inactive_allowed(self, store):
        doc = active(store).to_dict()
        rec = next(r for r in doc["field_mappings"] if r["field"] == "primary_recruiter_id")
        doc["field_mappings"].append({**copy.deepcopy(rec), "active": False, "target": "customInt9"})
        profile = TenantProfileV2.from_dict(doc)
        assert sum(1 for r in profile.field_mappings if r.field == "primary_recruiter_id") == 2
        doc["field_mappings"][-1]["active"] = True
        with pytest.raises(ProfileError):
            TenantProfileV2.from_dict(doc)


class TestP5B:
    def test_p5b_4_reactivate_refused_when_value_is_mapped(self, store):
        a = concept("a", "submission", "status", "client_submission", ["V1"])
        propose_and_commit(store, [a, {"op": "deactivate_value_mapping", "key": "a"}])
        propose_and_commit(store, [concept("b", "submission", "status", "client_submission", ["V1", "V2"])])
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "reactivate_value_mapping", "key": "a"}], now=NOW, env=env(store))
        assert "cannot be reactivated" in str(info.value)
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": "b"}, {"op": "reactivate_value_mapping", "key": "a"}])
        assert active(store).value_record("a").active

    def test_p5b_6_never_run_reported(self):
        drift = note_action_drift(None, None)
        assert drift["sources"]["settings"] == "not_run" and "settings" in drift["source_unresolved"]

    def test_p5b_13_single_source(self):
        assert action_discovery.NOTE_ENTITY is profile_v2.NOTE_ENTITY
        assert action_discovery.NOTE_ACTION_FIELD is profile_v2.NOTE_ACTION_FIELD

    def test_p5b_5_validate_refreshes_note_actions(self, store, monkeypatch):
        from bullhorn_mcp.tools import setup as setup_tools

        monkeypatch.setenv("BULLHORN_SETUP_STORE", str(store.root))
        monkeypatch.setenv("BULLHORN_MCP_ACTOR", "admin@example.com")
        with patch.object(setup_tools, "_run_discovery", side_effect=RuntimeError("stop")) as run:
            setup_tools.manage_mapping_profile(action="validate")
        assert run.call_args.kwargs.get("note_actions") is True


URL = f"{REST_URL}/settings/commentActionList"


class TestP5B2SettingsReader:
    @respx.mock
    def test_stream_aborted_before_full_read(self):
        consumed = {"chunks": 0}

        def body():
            for _ in range(1000):
                consumed["chunks"] += 1
                yield b"x" * 4096

        respx.get(URL).mock(return_value=httpx.Response(200, stream=_Iter(body())))
        with pytest.raises(sr.BullhornAPIError) as info:
            sr.SettingsReader(make_client()).get(["commentActionList"])
        assert "larger than" in str(info.value)
        assert consumed["chunks"] < 1000

    @respx.mock
    def test_encoded_response_refused(self):
        route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"\x1f\x8b", headers={"Content-Encoding": "gzip"}))
        with pytest.raises(sr.BullhornAPIError) as info:
            sr.SettingsReader(make_client()).get(["commentActionList"])
        assert "content encoding" in str(info.value)
        assert route.calls[0].request.headers["Accept-Encoding"] == "identity"

    @respx.mock
    def test_deep_nesting_contained(self):
        respx.get(URL).mock(return_value=httpx.Response(200, text="[" * 100_000 + "]" * 100_000))
        with pytest.raises(sr.BullhornAPIError) as info:
            sr.SettingsReader(make_client()).get(["commentActionList"])
        assert "nested deeper" in str(info.value)

    @respx.mock
    def test_strings_do_not_count_as_nesting(self):
        respx.get(URL).mock(return_value=httpx.Response(200, text=json.dumps({"commentActionList": "[[[[" * 100 + '\\"'})))
        assert sr.SettingsReader(make_client()).get(["commentActionList"])["commentActionList"].startswith("[[[[")

    def test_explicit_timeout(self):
        with patch.object(sr.httpx, "Client", side_effect=RuntimeError("stop")) as client:
            with pytest.raises(RuntimeError):
                sr.SettingsReader(make_client()).get(["commentActionList"])
        assert client.call_args.kwargs["timeout"] == httpx.Timeout(sr.TIMEOUT_SECONDS)


class _Iter(httpx.SyncByteStream):
    def __init__(self, gen):
        self._gen = gen

    def __iter__(self):
        yield from self._gen


class TestCapabilities:
    def test_new_entries_state_only(self):
        assert caps.CAPABILITIES["records.find"].states == ("setup_valid", "setup_revalidation_required")
        assert caps.CAPABILITIES["records.user"].states == ("setup_valid", "setup_revalidation_required")
        for c in caps.ACTIVITY_CONCEPTS:
            req = caps.CAPABILITIES[f"activity.{c}"]
            assert not req.orderings
            assert req.field_mappings == ((("job", "primary_recruiter_id"),) if c == "job_created" else ())
        assert caps.CAPABILITIES["activity.note_created"].states == ("setup_valid", "setup_revalidation_required")
        assert caps.CAPABILITIES["activity.client_submission"].states == ("setup_valid",)

    def test_concept_requirements(self, tmp_path):
        store = tenant_store(tmp_path, FULL_CONFIG)
        profile = active(store)
        states = {r.diff_key: "valid" for r in profile.value_mappings}
        assert caps.concept_requirements("client_submission", profile, states) == ()
        assert caps.concept_requirements("client_submission", None, {}) == (
            "value_mapping:submission:client_submission", "setting:client_submission_dating")
        assert caps.concept_requirements("submission_created", None, {}) == ()

    def test_setting_key_affects_activity_capabilities(self):
        hit = caps.capabilities_for_key("setting:interview_completion_rule", [None, {"value": "x"}])
        assert hit == ["activity.interview_completed"]

    def test_setup_status_lists_details(self, tmp_path, monkeypatch):
        from bullhorn_mcp.tools import setup as setup_tools

        store = tenant_store(tmp_path, [PRIMARY_RECRUITER])
        for k, v in env(store).items():
            monkeypatch.setenv(k, v)
        status = json.loads(setup_tools.setup_status(check_connection=False))
        assert {"records.find", "records.user", "activity.client_submission"} <= set(status["capabilities"])
        assert set(status["requirement_details"]) == set(status["capabilities"])
        assert "setting:client_submission_dating" in status["requirement_details"]["activity.client_submission"]
        assert "value_mapping:submission:client_submission" in status["requirement_details"]["activity.client_submission"]
        assert not any(m.startswith(("setting:", "value_mapping:submission")) for m in status["missing_requirements"])
