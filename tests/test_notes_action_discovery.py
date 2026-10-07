"""Phase 5B: note action discovery sources, the HV guard, drift and adoption helpers (D-5B-1, D-5B-5)."""

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import AuthenticationError
from bullhorn_mcp.notes import action_discovery
from bullhorn_mcp.notes.action_discovery import (
    discover_note_actions,
    discovered_key,
    meta_source,
    note_action_requirements,
    settings_source,
    verified_sources_by_value,
)
from bullhorn_mcp.notes.action_types import REQUIREMENT
from bullhorn_mcp.tenant.profile_v2 import VALUE_KEY_RE, TenantProfileV2
from bullhorn_mcp.tenant.revalidation import (
    DiscoverySnapshot,
    NoteActionSource,
    NoteActionSources,
    build_drift_report,
    merge_snapshots,
    note_action_drift,
)
from bullhorn_mcp.tenant.store import SetupStoreError

from ._notes_helpers import TOKEN, make_client
from ._tenant_helpers import NOW_TEXT, REST_URL, env, init_tenant, make_store, snapshot, tenant_payloads, write_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "notes5b"
OPTIONS = json.loads((FIXTURES / "note_action_options.json").read_text(encoding="utf-8"))["options"]
SETTINGS_BODY = json.loads((FIXTURES / "settings_comment_action_list.json").read_text(encoding="utf-8"))
SETTINGS_URL = f"{REST_URL}/settings/commentActionList"


def note_payloads(options=None, drop_action=False):
    payloads = tenant_payloads()
    fields = [f for f in payloads["Note"]["fields"] if not (drop_action and f["name"] == "action")]
    for f in fields:
        if f["name"] == "action" and options is not None:
            f["options"] = options
    payloads["Note"]["fields"] = fields
    return payloads


def nrec(key, values, active=True, source="administrator", discovery_source=None):
    rec = {"key": key, "target": {"kind": "note_action", "semantic": None}, "bullhorn_field": "action",
           "values": values, "source": source, "active": active}
    if discovery_source:
        rec["discovery_source"] = discovery_source
    return rec


def profile_with(*records):
    doc = {
        "format": "tenant-profile/v2",
        "tenant": {"id": "acme", "label": None},
        "profile_version": 1,
        "catalog_fingerprint": "0" * 64,
        "value_mappings": list(records),
    }
    return TenantProfileV2.from_dict(doc)


def sources(meta=None, settings=None):
    out = {}
    for name, values in (("meta", meta), ("settings", settings)):
        if values is None:
            out[name] = NoteActionSource("unresolved" if name == "settings" else "unverifiable")
        else:
            out[name] = NoteActionSource("verified", tuple(values))
    return NoteActionSources(checked_at=NOW_TEXT, sources=out)


@pytest.fixture
def settings_on(monkeypatch):
    monkeypatch.setattr(action_discovery, "SETTINGS_ACTION_SOURCE_VERIFIED", True)


class TestMetaSource:
    def test_verified_from_options(self):
        src = meta_source(snapshot(note_payloads(OPTIONS)))
        assert src.status == "verified" and src.values == ("Test Action A", "Test Action B", "Test Action C")

    def test_values_kept_exactly_and_deduplicated(self):
        opts = [
            {"value": " Test Action A", "label": "x"},
            {"value": "Test Action A", "label": "y"},
            {"value": "Test Action A", "label": "z"},
        ]
        assert meta_source(snapshot(note_payloads(opts))).values == (" Test Action A", "Test Action A")

    @pytest.mark.parametrize(
        "payloads",
        [
            note_payloads(None),  # HV-A2: options absent
            note_payloads([]),
            note_payloads([{"value": 5, "label": "five"}]),
            note_payloads([{"value": "   ", "label": "blank"}]),
            note_payloads(drop_action=True),
        ],
    )
    def test_unverifiable(self, payloads):
        src = meta_source(snapshot(payloads))
        assert src.status == "unverifiable" and src.values == () and src.warning

    def test_note_entity_failed_or_absent(self):
        assert meta_source(snapshot(errors={"Note": "boom"})).status == "unverifiable"
        assert meta_source(DiscoverySnapshot(NOW_TEXT, None, {})).status == "unverifiable"


class TestSettingsGuard:
    def test_production_default_is_off(self):
        assert action_discovery.SETTINGS_ACTION_SOURCE_VERIFIED is False

    def test_off_never_touches_the_client(self):
        client = Mock(spec=[])  # any attribute access (e.g. .auth) would raise
        with respx.mock(assert_all_called=False) as router:
            src = settings_source(client)
            assert not router.calls
        assert src.status == "unresolved" and src.values == ()

    def test_discover_reports_settings_unresolved(self):
        with respx.mock(assert_all_called=False) as router:
            found = discover_note_actions(snapshot(note_payloads(OPTIONS)), Mock(spec=[]), NOW_TEXT)
            assert not router.calls
        assert found.sources["settings"].status == "unresolved"
        assert note_action_drift(found, None)["source_unresolved"] == ["settings"]


class TestSettingsVerifiedShape:
    @respx.mock
    def test_values_discovered(self, settings_on):
        route = respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=SETTINGS_BODY))
        src = settings_source(make_client())
        assert src.status == "verified" and src.values == ("Test Action A", "Test Action B", "Test Action D")
        assert route.calls[0].request.method == "GET"

    @respx.mock
    @pytest.mark.parametrize(
        "body",
        [
            {"commentActionList": "Test Action A,Test Action B"},  # a delimited string is not the verified shape
            {"commentActionList": [1, 2]},
            {"commentActionList": ["Test Action A", None]},
            {"commentActionList": [""]},
            {"commentActionList": ["x" * 201]},
            {"commentActionList": []},
            {"commentActionList": None},
            {"commentActionList": {"a": 1}},
            {"commentActionList": [f"Test Action {i}" for i in range(501)]},
            {"otherSetting": ["Test Action A"]},
            {},
        ],
    )
    def test_malformed_is_unverifiable(self, settings_on, body):
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=body))
        src = settings_source(make_client())
        assert src.status == "unverifiable" and src.values == () and 0 < len(src.warning) <= 300

    @respx.mock
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
    def test_non_200_is_unverifiable_and_bounded(self, settings_on, status):
        body = '{"errorMessage":"Test Action Private","BhRestToken":"' + TOKEN + '"}' + "Z" * 100_000
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(status, text=body))
        src = settings_source(make_client())
        assert src.status == "unverifiable" and len(src.warning) <= 300
        assert TOKEN not in src.warning and "Test Action Private" not in src.warning

    @respx.mock
    @pytest.mark.parametrize("text", ["not json", "[1]", "A" * 300_000], ids=["not-json", "array", "oversized"])
    def test_bad_200_bodies(self, settings_on, text):
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(200, text=text))
        assert settings_source(make_client()).status == "unverifiable"

    @respx.mock
    def test_transport_and_auth_errors_never_raise(self, settings_on):
        respx.get(SETTINGS_URL).mock(side_effect=httpx.ConnectError("down"))
        assert settings_source(make_client()).status == "unverifiable"
        client = make_client()
        client.auth._refresh_session.side_effect = AuthenticationError("refresh failed BhRestToken=" + TOKEN)
        respx.get(SETTINGS_URL).mock(return_value=httpx.Response(401))
        src = settings_source(client)
        assert src.status == "unverifiable" and TOKEN not in src.warning


class TestDrift:
    def test_sets(self):
        profile = profile_with(
            nrec("note.action.a", ["Test Action A"]),
            nrec("note.action.z", ["Test Action Z"]),
            nrec("note.action.b", ["Test Action B"], active=False),
        )
        drift = note_action_drift(sources(meta=["Test Action A", "Test Action B", "Test Action C"]), profile)
        assert drift["new_values"] == ["Test Action C"]
        assert drift["stale_values"] == ["Test Action Z"]
        assert drift["reactivatable"] == ["Test Action B"] and drift["reactivatable_keys"] == ["note.action.b"]
        assert drift["unverifiable"] == [] and drift["source_unresolved"] == ["settings"]

    def test_union_of_verified_sources(self, settings_on):
        profile = profile_with(nrec("note.action.d", ["Test Action D"]))
        drift = note_action_drift(sources(meta=["Test Action A"], settings=["Test Action D"]), profile)
        assert drift["stale_values"] == [] and drift["new_values"] == ["Test Action A"] and drift["source_unresolved"] == []

    def test_no_verified_source_is_unverifiable_not_stale(self):
        profile = profile_with(nrec("note.action.a", ["Test Action A"]))
        for srcs in (None, sources()):
            drift = note_action_drift(srcs, profile)
            assert drift["unverifiable"] == ["Test Action A"] and drift["stale_values"] == [] and drift["new_values"] == []

    def test_exact_strings_no_normalisation(self):
        profile = profile_with(nrec("note.action.a", ["test action a"]))
        drift = note_action_drift(sources(meta=["Test Action A"]), profile)
        assert drift["stale_values"] == ["test action a"] and drift["new_values"] == ["Test Action A"]

    def test_active_value_also_in_deactivated_record_is_not_reactivatable(self):
        profile = profile_with(nrec("note.action.a", ["Test Action A"]), nrec("note.action.old", ["Test Action A"], active=False))
        assert note_action_drift(sources(meta=["Test Action A"]), profile)["reactivatable"] == []

    def test_report_findings_only_for_stale(self):
        stale = profile_with(nrec("note.action.z", ["Test Action Z"]))
        fresh = profile_with(nrec("note.action.a", ["Test Action A"]))
        snap = DiscoverySnapshot(NOW_TEXT, None, snapshot().entities, note_actions=sources(meta=["Test Action A", "Test Action C"]))
        before = stale.to_dict()
        report = build_drift_report(snap, snap, stale, list(snap.entities))
        assert report["has_findings"] is True and report["note_action_drift"]["stale_values"] == ["Test Action Z"]
        assert stale.to_dict() == before  # never mutated
        clean = build_drift_report(snap, snap, fresh, list(snap.entities))
        assert clean["has_findings"] is False and clean["note_action_drift"]["new_values"] == ["Test Action C"]

    def test_bounded(self):
        values = [f"Test Action {i}" for i in range(450)]
        drift = note_action_drift(sources(meta=values), None)
        assert len(drift["new_values"]) == 200 and drift["new_values_truncated"] == 250


class TestSnapshotStorage:
    def test_round_trip(self):
        snap = DiscoverySnapshot(NOW_TEXT, None, snapshot().entities, note_actions=sources(meta=["Test Action A"]))
        data = json.loads(json.dumps(snap.to_dict()))
        assert DiscoverySnapshot.from_dict(data) == snap

    def test_old_snapshot_unchanged(self):
        snap = snapshot()
        assert "note_actions" not in snap.to_dict()
        assert DiscoverySnapshot.from_dict(snap.to_dict()).note_actions is None

    @pytest.mark.parametrize(
        "bad",
        [
            [],
            {"checked_at": NOW_TEXT},
            {"checked_at": NOW_TEXT, "sources": {"other": {"status": "verified", "values": [], "warning": None}}},
            {"checked_at": NOW_TEXT, "sources": {"meta": {"status": "guessed", "values": [], "warning": None}}},
            {"checked_at": NOW_TEXT, "sources": {"meta": {"status": "verified", "values": [5], "warning": None}}},
            {"checked_at": NOW_TEXT, "sources": {"meta": {"status": "unresolved", "values": ["x"], "warning": None}}},
            {"checked_at": NOW_TEXT, "sources": {"meta": {"status": "verified", "values": ["x"] * 501, "warning": None}}},
        ],
    )
    def test_corrupt_is_store_error(self, bad):
        data = snapshot().to_dict()
        data["note_actions"] = bad
        with pytest.raises(SetupStoreError):
            DiscoverySnapshot.from_dict(data)

    def test_merge_keeps_previous_note_actions(self):
        prev = DiscoverySnapshot(NOW_TEXT, None, snapshot().entities, note_actions=sources(meta=["Test Action A"]))
        cur = DiscoverySnapshot("2026-10-07T00:00:00Z", None, {})
        assert merge_snapshots(prev, cur).note_actions == prev.note_actions
        newer = sources(meta=["Test Action B"])
        assert merge_snapshots(prev, DiscoverySnapshot(NOW_TEXT, None, {}, note_actions=newer)).note_actions == newer


class TestAdoptionHelpers:
    @pytest.mark.parametrize(
        "value", ["Test Action A", "test action a", "1st Call", "Ünïcødé Action", "---", "Test \u0410ction A", "x" * 30]
    )
    def test_key_shape(self, value):
        key = discovered_key("discovered", value)
        assert VALUE_KEY_RE.fullmatch(key) and key == discovered_key("discovered", value)
        assert key.startswith("note.action.discovered.")

    def test_keys_distinct_for_variants(self):
        variants = ["Test Action A", "test action a", "Test Action A ", "Test \u0410ction A", "Test-Action-A"]
        assert len({discovered_key("p", v) for v in variants}) == len(variants)

    @pytest.mark.parametrize("prefix", ["", "Upper", "a.b", "a/b", "x" * 33, "1x", None, "note.action"])
    def test_key_prefix_rule(self, prefix):
        with pytest.raises(ValueError):
            discovered_key(prefix, "Test Action A")

    def test_first_verified_source_wins(self, settings_on):
        assert verified_sources_by_value(sources(meta=["A1"], settings=["A1", "D1"])) == {"A1": "meta", "D1": "settings"}

    def test_settings_ignored_while_flag_off(self):
        """Sec-N4: a snapshot claiming settings: verified is ignored while HV-D1..D3 are unresolved."""
        assert verified_sources_by_value(sources(meta=["A1"], settings=["A1", "D1"])) == {"A1": "meta"}
        assert verified_sources_by_value(sources(settings=["D1"])) == {}
        assert verified_sources_by_value(None) == {} and verified_sources_by_value(sources()) == {}


class TestRequirements:
    def test_no_store_or_no_version(self, tmp_path):
        assert note_action_requirements({}) == [REQUIREMENT]
        store = make_store(tmp_path)
        assert note_action_requirements(env(store)) == [REQUIREMENT]

    def test_usable_and_deactivated(self, tmp_path):
        store = make_store(tmp_path)
        write_snapshot(store, snapshot())
        init_tenant(store)
        assert note_action_requirements(env(store)) == [REQUIREMENT]
        from ._tenant_helpers import propose_and_commit

        propose_and_commit(store, [{"op": "set_value_mapping", **{k: v for k, v in nrec("note.action.a", ["Test Action A"]).items()
                                                                   if k not in ("source", "active")}}])
        assert note_action_requirements(env(store)) == []
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": "note.action.a"}])
        assert note_action_requirements(env(store)) == [REQUIREMENT]
