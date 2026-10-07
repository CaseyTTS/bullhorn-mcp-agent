"""AC-10..AC-13, AC-17, AC-8 (import): proposals, commits, rollback and the type-aware diff hash."""

import datetime as dt
import json
from pathlib import Path

import pytest

from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.tenant.changes import commit, compute_diff_hash, propose

from ._tenant_helpers import (
    NOW,
    env,
    init_tenant,
    make_store,
    propose_and_commit,
    snapshot,
    tenant_payloads,
    tree_bytes,
    write_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tenant"

PRIORITY = {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"}
ORDERING = {
    "op": "set_value_mapping",
    "key": "job.priority.order",
    "target": {"kind": "ordering", "entity": "job", "field": "priority"},
    "bullhorn_field": "customText12",
    "values": ["A", "B"],
}


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_snapshot(s, snapshot())
    return s


def _non_proposal_bytes(store):
    return {k: v for k, v in tree_bytes(store.root).items() if not k.startswith("proposals")}


class TestPropose:
    def test_first_change_requires_init(self, store):
        with pytest.raises(ProfileError) as info:
            propose(store, [PRIORITY], now=NOW, env=env(store))
        assert "init_tenant" in str(info.value)

    def test_writes_only_proposals(self, store):
        init_tenant(store)
        before = _non_proposal_bytes(store)
        proposals_before = set((store.root / "proposals").iterdir())
        result = propose(store, [PRIORITY, ORDERING], now=NOW, env=env(store))
        assert _non_proposal_bytes(store) == before
        new = set((store.root / "proposals").iterdir()) - proposals_before
        assert [p.name for p in new] == [f"{result['proposal_id']}.json"]

    def test_diff_old_new_and_dependencies(self, store):
        init_tenant(store, [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customInt3"}])
        result = propose(store, [PRIORITY, ORDERING], now=NOW, env=env(store))
        by_key = {d["key"]: d for d in result["diff"]}
        changed = by_key["field:job.priority"]
        assert changed["change"] == "changed"
        assert changed["old"]["target"] == "customInt3" and changed["new"]["target"] == "customText12"
        added = by_key["value:job.priority.order"]
        assert added["change"] == "added" and added["old"] is None and added["new"]["values"] == ["A", "B"]
        assert "jobs.priority" in result["dependencies"]["field:job.priority"]
        assert result["dependencies"]["value:job.priority.order"] == ["jobs.priority"]
        assert result["expires_at"] == "2026-10-07T12:00:00Z"

    def test_deactivated_vs_removed(self, store):
        init_tenant(store, [PRIORITY])
        deact = propose(store, [{"op": "deactivate_field_mapping", "entity": "job", "field": "priority"}], now=NOW, env=env(store))
        assert deact["diff"][0]["change"] == "deactivated"
        removed = propose(store, [{"op": "remove_field_mapping", "entity": "job", "field": "priority"}], now=NOW, env=env(store))
        assert removed["diff"][0]["change"] == "removed" and removed["diff"][0]["new"] is None

    def test_no_changes_refused(self, store):
        init_tenant(store, [PRIORITY])
        with pytest.raises(ProfileError):
            propose(store, [PRIORITY], now=NOW, env=env(store))

    def test_stale_base_version_refused(self, store):
        init_tenant(store)
        with pytest.raises(ProfileError) as info:
            propose(store, [PRIORITY], base_version=7, now=NOW, env=env(store))
        assert "stale" in str(info.value)

    @pytest.mark.parametrize(
        "changes",
        [
            "not a list",
            [None],
            [{"op": "explode"}],
            [{"op": "set_field_mapping", "entity": "job"}],
            [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "x", "evil": 1}],
            [{"op": "set_field_mapping", "entity": "job", "field": "id", "target": "id"}],
            [{"op": "set_setting", "name": "reporting_timezone", "value": "Nowhere/Land"}],
            [{"op": "set_setting", "name": "other", "value": "UTC"}],
            [{"op": "rollback_to", "version": 99}],
            [{"op": "rollback_to", "version": True}],
            [{"op": "init_tenant", "tenant_id": "again"}],
            [{"op": "deactivate_value_mapping", "key": "nope"}],
            [{"op": "apply_verified_defaults", "entities": ["widget"]}],
            [{"op": "import_document", "path": "../../etc/passwd"}],
            [{"op": "set_value_mapping", "key": "k", "target": {"kind": "concept", "name": "interview_completed"},
              "bullhorn_field": "type", "values": ["X"]}],  # concept target without entity
            [{"op": "set_value_mapping", "key": "k", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
              "bullhorn_field": "customText12", "values": [1.0]}],
            [{"op": "set_value_mapping", "key": "k", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
              "bullhorn_field": "customText12", "values": [True]}],
            [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "x"}] * 201,
        ],
        ids=lambda c: repr(c)[:40],
    )
    def test_invalid_changes(self, store, changes):
        init_tenant(store)
        with pytest.raises(ProfileError) as info:
            propose(store, changes, now=NOW, env=env(store))
        assert type(info.value) is ProfileError
        assert len(str(info.value)) < 60_000

    def test_conflict_reported_not_raised(self, store):
        init_tenant(store)
        base = {"op": "set_value_mapping", "entity": "appointment", "bullhorn_field": "type", "values": ["Interview"]}
        result = propose(
            store,
            [
                {**base, "key": "done", "target": {"kind": "concept", "name": "interview_completed"}},
                {**base, "key": "cancelled", "target": {"kind": "concept", "name": "interview_cancelled"}},
            ],
            now=NOW,
            env=env(store),
        )
        assert result["conflicts"] and "conflict" in result["conflicts"][0]


class TestCommit:
    def _proposal(self, store):
        init_tenant(store)
        return propose(store, [PRIORITY, ORDERING], now=NOW, env=env(store))

    def _assert_refused_unchanged(self, store, before, result, needle):
        assert result["status"] == "refused", result
        assert needle in result["reason"]
        assert tree_bytes(store.root) == before
        assert result["correlation_id"]

    def test_approve(self, store):
        p = self._proposal(store)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        assert result["status"] == "committed" and result["version"] == 2 and len(result["correlation_id"]) == 32
        assert store.active_version() == 2
        assert store.read_version(2).field_record("job", "priority").target.to_data() == "customText12"
        saved = store.load_proposal(p["proposal_id"])
        assert saved["status"] == "committed"
        again = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        assert again["status"] == "refused"

    def test_reject(self, store):
        p = self._proposal(store)
        result = commit(store, p["proposal_id"], p["diff_hash"], "reject", now=NOW, env=env(store))
        assert result["status"] == "rejected"
        assert store.active_version() == 1 and store.list_versions() == [1]
        assert store.load_proposal(p["proposal_id"])["status"] == "rejected"

    def test_wrong_hash(self, store):
        p = self._proposal(store)
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], "0" * 64, "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "diff_hash")

    def test_expired(self, store):
        p = self._proposal(store)
        before = tree_bytes(store.root)
        later = NOW + dt.timedelta(hours=24)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=later, env=env(store))
        self._assert_refused_unchanged(store, before, result, "expired")

    def test_stale(self, store):
        p = self._proposal(store)
        other = propose(store, [{"op": "set_field_mapping", "entity": "job", "field": "title", "target": "title"}], now=NOW, env=env(store))
        assert commit(store, other["proposal_id"], other["diff_hash"], "approve", now=NOW, env=env(store))["status"] == "committed"
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "stale")

    def test_actor_unset(self, store):
        p = self._proposal(store)
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store, actor=None))
        self._assert_refused_unchanged(store, before, result, "BULLHORN_MCP_ACTOR")

    def test_actor_not_admin(self, store):
        p = self._proposal(store)
        before = tree_bytes(store.root)
        e = env(store, actor="mallory", BULLHORN_SETUP_ADMINS="alex,casey")
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=e)
        self._assert_refused_unchanged(store, before, result, "BULLHORN_SETUP_ADMINS")

    def test_conflict(self, store):
        init_tenant(store)
        base = {"op": "set_value_mapping", "entity": "appointment", "bullhorn_field": "type", "values": ["Interview"]}
        p = propose(
            store,
            [
                {**base, "key": "done", "target": {"kind": "concept", "name": "interview_completed"}},
                {**base, "key": "cancelled", "target": {"kind": "concept", "name": "interview_cancelled"}},
            ],
            now=NOW,
            env=env(store),
        )
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "conflict")

    def test_broken(self, store):
        init_tenant(store)
        p = propose(
            store, [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText99"}], now=NOW, env=env(store)
        )
        assert p["validation"]["broken"] == ["field:job.priority"]
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "broken")

    def test_broken_after_meta_change(self, store):
        p = self._proposal(store)
        write_snapshot(store, snapshot(tenant_payloads(job_extra={})))  # customText12 disappeared
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "broken")

    @pytest.mark.parametrize("decision", ["APPROVE", "", None, "yes"])
    def test_bad_decision(self, store, decision):
        p = self._proposal(store)
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], decision, now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "decision")

    def test_unknown_proposal(self, store):
        init_tenant(store)
        result = commit(store, "f" * 32, "0" * 64, "approve", now=NOW, env=env(store))
        assert result["status"] == "refused"

    def test_tampered_draft(self, store):
        p = self._proposal(store)
        path = store.root / "proposals" / f"{p['proposal_id']}.json"
        data = json.loads(path.read_text())
        data["draft"]["field_mappings"][-1]["target"] = "customText1"  # edit content, keep stored diff/hash
        path.write_text(json.dumps(data))
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        self._assert_refused_unchanged(store, before, result, "does not match")

    def test_concurrent_commit_lock(self, store):
        p = self._proposal(store)
        store.acquire_commit_lock()
        before = tree_bytes(store.root)
        with pytest.raises(Exception) as info:
            commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        assert "another commit" in str(info.value)
        assert tree_bytes(store.root) == before

    def test_commit_clears_drift(self, store):
        p = self._proposal(store)
        doc = store.read_discovery()
        store.write_discovery({**doc, "drift_unresolved": True})
        commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        doc = store.read_discovery()
        assert doc["drift_unresolved"] is False and doc["last_validation"]["version"] == 2


class TestRollback:
    def test_rollback_copies_records(self, store):
        init_tenant(store, [PRIORITY])
        v1 = store.read_version(1)
        propose_and_commit(store, [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customInt3"}])
        result = propose_and_commit(store, [{"op": "rollback_to", "version": 1}])
        assert result["version"] == 3
        v3 = store.read_version(3)
        assert [r.content() for r in v3.field_mappings] == [r.content() for r in v1.field_mappings]
        assert [r.content() for r in v3.value_mappings] == [r.content() for r in v1.value_mappings]
        assert v3.settings == v1.settings
        history = store.read_history()
        assert history[-1]["action"] == "rollback" and history[-1]["version"] == 3

    def test_rollback_to_broken_refused(self, store):
        init_tenant(store, [PRIORITY])
        propose_and_commit(store, [{"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customInt3"}])
        write_snapshot(store, snapshot(tenant_payloads(job_extra={"customInt3": {}})))  # customText12 gone
        p = propose(store, [{"op": "rollback_to", "version": 1}], now=NOW, env=env(store))
        assert "field:job.priority" in p["validation"]["broken"]
        before = tree_bytes(store.root)
        result = commit(store, p["proposal_id"], p["diff_hash"], "approve", now=NOW, env=env(store))
        assert result["status"] == "refused" and "broken" in result["reason"]
        assert tree_bytes(store.root) == before


class TestDiffHash:
    def test_type_aware(self):
        def diff(v):
            return [{"key": "value:k", "change": "changed", "old": None, "new": {"values": [v]}}]

        h_int = compute_diff_hash(1, diff(1))
        assert h_int != compute_diff_hash(1, diff(True))
        assert h_int != compute_diff_hash(1, diff(1.0))
        assert h_int != compute_diff_hash(1, diff("1"))
        assert h_int == compute_diff_hash(1, diff(1))

    def test_base_version_bound(self):
        d = [{"key": "k", "change": "added", "old": None, "new": {"a": 1}}]
        assert compute_diff_hash(1, d) != compute_diff_hash(2, d)
        assert compute_diff_hash(None, d) != compute_diff_hash(0, d)

    def test_key_order_independent(self):
        a = [{"key": "k", "change": "added", "old": None, "new": {"a": 1, "b": 2}}]
        b = [{"new": {"b": 2, "a": 1}, "old": None, "change": "added", "key": "k"}]
        assert compute_diff_hash(1, a) == compute_diff_hash(1, b)


class TestDefaults:
    def test_apply_verified_defaults(self, store):
        payloads = tenant_payloads(job_drop=("clientBillRate",))
        write_snapshot(store, snapshot(payloads))
        result = propose(store, [{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults"}], now=NOW, env=env(store))
        defaults = result["defaults"]
        assert "job.title" in defaults["applied"]
        assert "job.bill_rate" in defaults["unresolved"] and "job.bill_rate" not in defaults["applied"]
        assert "job.priority" in defaults["unresolved"] and "job.primary_recruiter_id" in defaults["unresolved"]
        assert "job.priority" in result["validation"]["unresolved"]
        assert "job.primary_recruiter_id" in result["validation"]["unresolved"]
        commit(store, result["proposal_id"], result["diff_hash"], "approve", now=NOW, env=env(store))
        profile = store.read_version(1)
        title = profile.field_record("job", "title")
        assert title.source == "standard" and title.validation.state == "valid"
        assert profile.field_record("job", "bill_rate") is None
        assert profile.field_record("job", "priority") is None
        assert all(r.source == "standard" for r in profile.field_mappings)

    def test_defaults_without_snapshot_all_unresolved(self, tmp_path):
        s = make_store(tmp_path)
        result = propose(s, [{"op": "init_tenant", "tenant_id": "acme"}, {"op": "apply_verified_defaults"}], now=NOW, env=env(s))
        assert result["defaults"]["applied"] == []
        assert "job.title" in result["defaults"]["unresolved"]

    def test_defaults_keep_administrator_records(self, store):
        init_tenant(store, [{"op": "set_field_mapping", "entity": "job", "field": "title", "target": "customText12"}])
        result = propose(store, [{"op": "apply_verified_defaults", "entities": ["job"]}], now=NOW, env=env(store))
        assert "job.title" in result["defaults"]["kept_existing"]


IMPORT_EXTRA = {"customText12": {}, "customObject1s": {}, "customText5": {}}


class TestImport:
    @pytest.fixture
    def store(self, tmp_path):
        s = make_store(tmp_path)
        write_snapshot(s, snapshot(tenant_payloads(job_extra=IMPORT_EXTRA)))
        return s

    def test_import_v1(self, store):
        init_tenant(store)
        result = propose(store, [{"op": "import_document", "path": str(FIXTURES / "v1_profile.yaml")}], now=NOW, env=env(store))
        commit(store, result["proposal_id"], result["diff_hash"], "approve", now=NOW, env=env(store))
        profile = store.read_version(2)
        assert {r.field for r in profile.field_mappings} == {"priority", "title", "hot_flag", "region"}
        for rec in profile.field_mappings:
            assert rec.source == "administrator"
            assert rec.validation.state == "unvalidated"
        assert profile.field_record("job", "hot_flag").type == "integer"
        assert store.read_history()[-1]["action"] == "import"

    def test_import_v2(self, store, tmp_path):
        init_tenant(store)
        result = propose(store, [{"op": "import_document", "path": str(FIXTURES / "valid.yaml")}], now=NOW, env=env(store))
        assert all(d["new"] is None or d["new"].get("source") in (None, "administrator") for d in result["diff"])
        commit(store, result["proposal_id"], result["diff_hash"], "approve", now=NOW, env=env(store))
        profile = store.read_version(2)
        assert {r.validation.state for r in profile.field_mappings} == {"unvalidated"}

    def test_import_wrong_tenant(self, store, tmp_path):
        propose_and_commit(store, [{"op": "init_tenant", "tenant_id": "other"}])
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "import_document", "path": str(FIXTURES / "valid.yaml")}], now=NOW, env=env(store))
        assert "tenant" in str(info.value)

    @pytest.mark.parametrize("name", ["duplicate_key.yaml", "concept_conflict.yaml", "invalid_targets.yaml"])
    def test_import_invalid(self, store, name):
        init_tenant(store)
        with pytest.raises(ProfileError):
            propose(store, [{"op": "import_document", "path": str(FIXTURES / name)}], now=NOW, env=env(store))

    def test_import_from_store_refused(self, store):
        init_tenant(store)
        with pytest.raises(ProfileError) as info:
            propose(store, [{"op": "import_document", "path": str(store.version_path(1))}], now=NOW, env=env(store))
        assert "inside the setup store" in str(info.value)
