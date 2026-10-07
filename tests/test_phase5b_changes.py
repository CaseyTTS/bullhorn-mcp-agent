"""Phase 5B: discovered-action adoption, reactivation, rollback and record provenance (D-5B-2..D-5B-4, AC-8, AC-9)."""

import datetime as dt
import json
from pathlib import Path

import pytest

from bullhorn_mcp.notes import action_discovery
from bullhorn_mcp.notes.action_types import valid_set
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.tenant.changes import commit, propose
from bullhorn_mcp.tenant.profile_v2 import TenantProfileV2
from bullhorn_mcp.tenant.revalidation import DiscoverySnapshot, NoteActionSource, NoteActionSources
from bullhorn_mcp.tenant.state import effective_states

from ._tenant_helpers import NOW, NOW_TEXT, env, init_tenant, make_store, propose_and_commit, snapshot, tenant_payloads, tree_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "notes5b"
TENANT_FIXTURES = Path(__file__).parent / "fixtures" / "tenant"
OPTIONS = json.loads((FIXTURES / "note_action_options.json").read_text(encoding="utf-8"))["options"]
LONG = "Test Action " + "L" * 19  # 31 characters
ADOPT = {"op": "apply_discovered_note_actions", "values": ["Test Action A", "Test Action B"]}
ADMIN_ACTION = {
    "op": "set_value_mapping",
    "key": "note.action.admin",
    "target": {"kind": "note_action", "semantic": None},
    "bullhorn_field": "action",
    "values": ["Test Action C"],
}


def snap_with(meta=None, settings=None):
    payloads = tenant_payloads()
    for f in payloads["Note"]["fields"]:
        if f["name"] == "action":
            f["options"] = OPTIONS + [{"value": LONG, "label": LONG}]
    base = snapshot(payloads)
    srcs = {
        "meta": NoteActionSource("verified", tuple(meta)) if meta else NoteActionSource("unverifiable", warning="x"),
        "settings": NoteActionSource("verified", tuple(settings)) if settings else NoteActionSource("unresolved", warning="off"),
    }
    return DiscoverySnapshot(base.checked_at, None, base.entities, note_actions=NoteActionSources(NOW_TEXT, srcs))


def write(store, snap):
    store.write_discovery({"checked_at": snap.checked_at, "snapshot": snap.to_dict(), "report": None, "drift_unresolved": False})


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write(s, snap_with(meta=["Test Action A", "Test Action B", "Test Action C", LONG], settings=None))
    init_tenant(s)
    return s


def active(store):
    return store.read_version(store.active_version())


def note_records(profile):
    return {r.key: r for r in profile.value_mappings if r.target.kind == "note_action"}


class TestAdoption:
    def test_only_through_propose_then_commit(self, store):
        def non_proposal():
            return {k: v for k, v in tree_bytes(store.root).items() if not k.startswith("proposals")}

        before = non_proposal()
        p = propose(store, [ADOPT], now=NOW, env=env(store))
        assert non_proposal() == before  # nothing activated
        assert [d["change"] for d in p["diff"]] == ["added", "added"]
        assert all(d["new"]["discovery_source"] == "meta" and d["new"]["source"] == "discovered" for d in p["diff"])
        assert commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))["status"] == "committed"
        recs = note_records(active(store))
        assert sorted(v for r in recs.values() for v in r.values) == ["Test Action A", "Test Action B"]
        for rec in recs.values():
            assert rec.source == "discovered" and rec.target.semantic is None and rec.discovery_source == "meta"
            assert rec.active and len(rec.values) == 1
        states, _ = effective_states(active(store), store.read_discovery())
        assert valid_set(active(store), states).values == ("Test Action A", "Test Action B")

    def test_settings_provenance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(action_discovery, "SETTINGS_ACTION_SOURCE_VERIFIED", True)
        s = make_store(tmp_path)
        write(s, snap_with(meta=["Test Action A"], settings=["Test Action A", "Test Action D"]))
        init_tenant(s)
        propose_and_commit(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action A", "Test Action D"]}])
        prov = {r.values[0]: r.discovery_source for r in note_records(active(s)).values()}
        assert prov == {"Test Action A": "meta", "Test Action D": "settings"}

    def test_key_prefix(self, store):
        propose_and_commit(store, [{"op": "apply_discovered_note_actions", "values": ["Test Action A"], "key_prefix": "custom_2"}])
        assert all(k.startswith("note.action.custom_2.") for k in note_records(active(store)))

    @pytest.mark.parametrize("prefix", [None, "discovered", "job", "candidate", "priority"])
    def test_keys_stay_in_note_action_namespace(self, store, prefix):
        change = {"op": "apply_discovered_note_actions", "values": ["Test Action A"]}
        if prefix is not None:
            change["key_prefix"] = prefix
        propose_and_commit(store, [change])
        keys = list(note_records(active(store)))
        assert keys and all(k.startswith("note.action.") for k in keys)
        assert all(r.key.startswith("note.action.") for r in active(store).value_mappings)

    def test_rejected_without_commit_nothing_changes(self, store):
        p = propose(store, [ADOPT], now=NOW, env=env(store))
        assert commit(store, p["proposal_id"], p["diff_hash"], "reject", now=NOW, env=env(store))["status"] == "rejected"
        assert note_records(active(store)) == {}

    @pytest.mark.parametrize(
        "values,needle",
        [
            (["Test Action Z"], "not a value of a verified discovery source"),
            (["test action a"], "not a value of a verified discovery source"),  # case variant
            ([" Test Action A"], "not a value of a verified discovery source"),  # whitespace padded
            (["Test Action A "], "not a value of a verified discovery source"),
            (["Test \u0410ction A"], "not a value of a verified discovery source"),  # Cyrillic A (confusable)
            (["Test\u200bAction A"], "not a value of a verified discovery source"),
            ([LONG], "at most 30 characters"),  # present in the snapshot, but longer than String(30)
            ([5], "non-blank string"),
            (["   "], "non-blank string"),
            (["Test Action A", "Test Action A"], "duplicate"),
            ([], "non-empty list"),
            ("Test Action A", "non-empty list"),
            ([f"Test Action {i}" for i in range(101)], "non-empty list"),
        ],
    )
    def test_rejections(self, store, values, needle):
        before = tree_bytes(store.root)
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "apply_discovered_note_actions", "values": values}], now=NOW, env=env(store))
        assert needle in str(info.value)
        assert tree_bytes(store.root) == before

    @pytest.mark.parametrize(
        "prefix",
        ["Note.Action", "a.b.c.d", "", 5, "note..x", "note.action.", "note.action", "job.priority", "a/b", "../x",
         "Upper", "x" * 33, "1abc", "_x", "a-b", "a b"],
    )
    def test_bad_key_prefix(self, store, prefix):
        with pytest.raises(ProfileError) as info:
            change = {"op": "apply_discovered_note_actions", "values": ["Test Action A"], "key_prefix": prefix}
            propose(store, [change], now=NOW, env=env(store))
        assert "key_prefix" in str(info.value)

    @pytest.mark.parametrize("extra", [{"semantic": "candidate_screen"}, {"discovery_source": "settings"}, {"source": "discovered"}])
    def test_no_extra_keys(self, store, extra):
        with pytest.raises(ProfileError) as info:
            propose(store, [{**ADOPT, **extra}], now=NOW, env=env(store))
        assert "unknown key" in str(info.value)

    def test_no_verified_source(self, tmp_path):
        s = make_store(tmp_path)
        write(s, snap_with(meta=None, settings=None))
        init_tenant(s)
        with pytest.raises(ProfileError) as info:
            propose(s, [ADOPT], now=NOW, env=env(s))
        assert "no verified note action source" in str(info.value) and "set_value_mapping" in str(info.value)

    def test_old_snapshot_without_note_actions(self, tmp_path):
        s = make_store(tmp_path)
        s.write_discovery({"checked_at": NOW_TEXT, "snapshot": snapshot().to_dict(), "report": None, "drift_unresolved": False})
        init_tenant(s)
        with pytest.raises(ProfileError):
            propose(s, [ADOPT], now=NOW, env=env(s))

    def test_already_mapped_by_administrator(self, store):
        propose_and_commit(store, [ADMIN_ACTION])
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "apply_discovered_note_actions", "values": ["Test Action C"]}], now=NOW, env=env(store))
        assert "already mapped by 'note.action.admin'" in str(info.value)

    def test_readopting_is_no_change_and_deactivated_needs_reactivate(self, store):
        propose_and_commit(store, [ADOPT])
        with pytest.raises(ProfileError) as info:
            propose(store, [ADOPT], now=NOW, env=env(store))
        assert "no changes" in str(info.value)
        key = next(iter(note_records(active(store))))
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": key}])
        with pytest.raises(ProfileError) as info:
            propose(store, [ADOPT], now=NOW, env=env(store))
        assert "reactivate_value_mapping" in str(info.value)

    def test_forged_settings_snapshot_rejected_while_flag_off(self, tmp_path):
        """Sec-N4: a snapshot that claims settings values is not an adoption source while the flag is off."""
        s = make_store(tmp_path)
        write(s, snap_with(meta=["Test Action A"], settings=["Test Action A", "Test Action D"]))
        init_tenant(s)
        before = tree_bytes(s.root)
        with pytest.raises(ProfileError) as info:
            propose(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action D"]}], now=NOW, env=env(s))
        assert "not a value of a verified discovery source" in str(info.value)
        assert tree_bytes(s.root) == before
        write(s, snap_with(meta=None, settings=["Test Action D"]))
        with pytest.raises(ProfileError) as info:
            propose(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action D"]}], now=NOW, env=env(s))
        assert "no verified note action source" in str(info.value)

    def test_provenance_update(self, tmp_path, monkeypatch):
        monkeypatch.setattr(action_discovery, "SETTINGS_ACTION_SOURCE_VERIFIED", True)
        s = make_store(tmp_path)
        write(s, snap_with(meta=None, settings=["Test Action A"]))
        init_tenant(s)
        propose_and_commit(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action A"]}])
        write(s, snap_with(meta=["Test Action A"], settings=None))
        p = propose(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action A"]}], now=NOW, env=env(s))
        assert [d["change"] for d in p["diff"]] == ["changed"]
        assert p["diff"][0]["old"]["discovery_source"] == "settings" and p["diff"][0]["new"]["discovery_source"] == "meta"


class TestCommitGates:
    """S-5B-4: adoption, reactivation and rollback go through every 4A commit gate."""

    @pytest.fixture(params=["adopt", "reactivate", "rollback"])
    def proposal(self, request, store):
        propose_and_commit(store, [ADOPT])
        key = sorted(note_records(active(store)))[0]
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": key}])
        changes = {
            "adopt": [{"op": "apply_discovered_note_actions", "values": ["Test Action C"]}],
            "reactivate": [{"op": "reactivate_value_mapping", "key": key}],
            "rollback": [{"op": "rollback_to", "version": 2}],
        }[request.param]
        return propose(store, changes, now=NOW, env=env(store))

    def _refused(self, store, result, needle, before):
        assert result["status"] == "refused" and needle in result["reason"], result
        assert tree_bytes(store.root) == before

    def test_actor_required(self, store, proposal):
        before = tree_bytes(store.root)
        r = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=NOW, env=env(store, actor=None))
        self._refused(store, r, "BULLHORN_MCP_ACTOR", before)

    def test_admin_allowlist(self, store, proposal):
        before = tree_bytes(store.root)
        e = env(store, BULLHORN_SETUP_ADMINS="other@example.com")
        r = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=NOW, env=e)
        self._refused(store, r, "BULLHORN_SETUP_ADMINS", before)

    def test_diff_hash(self, store, proposal):
        before = tree_bytes(store.root)
        r = commit(store, proposal["proposal_id"], "0" * 64, "approve", now=NOW, env=env(store))
        self._refused(store, r, "diff_hash", before)

    def test_expiry(self, store, proposal):
        before = tree_bytes(store.root)
        r = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=NOW + dt.timedelta(hours=24), env=env(store))
        self._refused(store, r, "expired", before)

    def test_stale_base(self, store, proposal):
        propose_and_commit(store, [{**ADMIN_ACTION, "key": "note.action.other", "values": ["Test Action Q"]}])
        before = tree_bytes(store.root)
        r = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=NOW, env=env(store))
        self._refused(store, r, "stale", before)

    def test_tampered_draft(self, store, proposal):
        path = store.root / "proposals" / f"{proposal['proposal_id']}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for rec in doc["draft"]["value_mappings"]:
            rec["values"] = ["Test Action Z"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        before = tree_bytes(store.root)
        r = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=NOW, env=env(store))
        self._refused(store, r, "diff_hash", before)


class TestReactivate:
    def test_restores_identical_content_new_version(self, store):
        propose_and_commit(store, [ADOPT])
        original = note_records(active(store))
        key = sorted(original)[0]
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": key}])
        assert not note_records(active(store))[key].active
        result = propose_and_commit(store, [{"op": "reactivate_value_mapping", "key": key}])
        restored = note_records(active(store))[key]
        assert result["version"] == 4 and restored.active
        assert restored.content() == original[key].content() and restored.discovery_source == original[key].discovery_source

    @pytest.mark.parametrize("key", ["note.action.nope", 5, None])
    def test_unknown_key(self, store, key):
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "reactivate_value_mapping", "key": key}], now=NOW, env=env(store))
        assert "no value mapping" in str(info.value)

    def test_active_key(self, store):
        propose_and_commit(store, [ADMIN_ACTION])
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "reactivate_value_mapping", "key": "note.action.admin"}], now=NOW, env=env(store))
        assert "already active" in str(info.value)

    def test_rollback_restores_prior_note_action_set(self, store):
        propose_and_commit(store, [ADMIN_ACTION])  # v2
        v2 = {k: r.content() for k, r in note_records(active(store)).items()}
        propose_and_commit(store, [ADOPT, {"op": "deactivate_value_mapping", "key": "note.action.admin"}])  # v3
        assert {k: r.content() for k, r in note_records(active(store)).items()} != v2
        result = propose_and_commit(store, [{"op": "rollback_to", "version": 2}])
        assert result["version"] == 4
        assert {k: r.content() for k, r in note_records(active(store)).items()} == v2


class TestProvenanceField:
    def test_old_documents_round_trip_byte_identical(self):
        legacy_note = TenantProfileV2.from_dict(self._doc(source="administrator")).to_yaml_text()
        for text in ((TENANT_FIXTURES / "valid.yaml").read_text(encoding="utf-8"), legacy_note):
            doc = TenantProfileV2.from_yaml_text(text)
            assert "discovery_source" not in json.dumps(doc.to_dict())
            again = TenantProfileV2.from_yaml_text(doc.to_yaml_text())
            assert again == doc and again.to_yaml_text() == doc.to_yaml_text()

    def _doc(self, **rec_extra):
        rec = {"key": "note.action.a", "target": {"kind": "note_action", "semantic": None}, "bullhorn_field": "action",
               "values": ["Test Action A"], **rec_extra}
        return {"format": "tenant-profile/v2", "tenant": {"id": "acme"}, "profile_version": 1,
                "catalog_fingerprint": "0" * 64, "value_mappings": [rec]}

    @pytest.mark.parametrize("source,ds", [("discovered", "meta"), ("discovered", "settings"), ("administrator", "administrator")])
    def test_valid(self, source, ds):
        doc = TenantProfileV2.from_dict(self._doc(source=source, discovery_source=ds))
        assert doc.value_mappings[0].discovery_source == ds
        assert TenantProfileV2.from_yaml_text(doc.to_yaml_text()) == doc

    @pytest.mark.parametrize(
        "extra,needle",
        [
            ({"source": "discovered", "discovery_source": "guess"}, "is not one of"),
            ({"source": "discovered", "discovery_source": None}, "is not one of"),
            ({"source": "administrator", "discovery_source": "settings"}, "requires source 'discovered'"),
            ({"source": "discovered", "discovery_source": "administrator"}, "requires source 'administrator'"),
        ],
    )
    def test_invalid(self, extra, needle):
        with pytest.raises(ProfileError) as info:
            TenantProfileV2.from_dict(self._doc(**extra))
        assert needle in str(info.value)

    def test_only_note_action_records(self):
        doc = self._doc(source="discovered", discovery_source="meta")
        doc["value_mappings"][0].update(
            {"key": "job.priority.order", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
             "bullhorn_field": "customText12", "values": ["A"]}
        )
        with pytest.raises(ProfileError) as info:
            TenantProfileV2.from_dict(doc)
        assert "only note_action mappings carry a discovery_source" in str(info.value)
