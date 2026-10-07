"""AC-14 / AC-15: the setup state machine (every row) and per-capability gating."""

import datetime as dt

import pytest

from bullhorn_mcp.tenant.changes import propose
from bullhorn_mcp.tenant.profile_v2 import rest_url_fingerprint
from bullhorn_mcp.tenant.state import ConnectionCheck, compute_setup_state, require_capability

from ._tenant_helpers import (
    CREDENTIALS,
    FINGERPRINT,
    NOW,
    REST_URL,
    env,
    init_tenant,
    make_store,
    propose_and_commit,
    snapshot,
    write_snapshot,
)

OK = ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint(REST_URL))
CAP_MISSING = [
    "mapping:job.priority",
    "value_mapping:job.priority:ordering",
    "mapping:job.primary_recruiter_id",
]
PRIORITY = {"op": "set_field_mapping", "entity": "job", "field": "priority", "target": "customText12"}
ORDERING = {
    "op": "set_value_mapping",
    "key": "job.priority.order",
    "target": {"kind": "ordering", "entity": "job", "field": "priority"},
    "bullhorn_field": "customText12",
    "values": ["A", "B"],
}


def _valid_store(tmp_path, extra=None):
    store = make_store(tmp_path)
    write_snapshot(store, snapshot(rest_fp=OK.rest_url_fingerprint))
    init_tenant(store, extra)
    return store


def _state(e, connection=OK, catalog=FINGERPRINT):
    return compute_setup_state(e, connection, catalog_fingerprint=catalog, now=NOW)


# Each builder returns (env, connection, catalog fingerprint); parametrized over every row of §1.6.
def row1_missing_credential(tmp_path):
    e = env(make_store(tmp_path))
    del e["BULLHORN_PASSWORD"]
    return e, None, FINGERPRINT


def row1_connection_failed(tmp_path):
    return env(make_store(tmp_path)), ConnectionCheck(ok=False, error="boom"), FINGERPRINT


def row2_no_store(tmp_path):
    return env(None), OK, FINGERPRINT


def row2_store_not_directory(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    return {**env(None), "BULLHORN_SETUP_STORE": str(f)}, OK, FINGERPRINT


def row2_empty_store(tmp_path):
    return env(make_store(tmp_path), actor=None), OK, FINGERPRINT


def row3_open_proposal(tmp_path):
    store = make_store(tmp_path)
    propose(store, [{"op": "init_tenant", "tenant_id": "acme"}], now=NOW, env=env(store))
    return env(store), OK, FINGERPRINT


def row3_expired_proposal_is_row2(tmp_path):
    store = make_store(tmp_path)
    propose(store, [{"op": "init_tenant", "tenant_id": "acme"}], now=NOW - dt.timedelta(days=2), env=env(store))
    return env(store), OK, FINGERPRINT


def row4_unloadable(tmp_path):
    store = _valid_store(tmp_path)
    store.version_path(1).write_text("format: [unclosed", encoding="utf-8")
    return env(store), OK, FINGERPRINT


def row4_broken_last_validation(tmp_path):
    store = _valid_store(tmp_path, [PRIORITY])
    doc = store.read_discovery()
    doc["last_validation"] = {"version": 1, "states": {"field:job.priority": "broken"}}
    store.write_discovery(doc)
    return env(store), OK, FINGERPRINT


def row5_catalog_changed(tmp_path):
    return env(_valid_store(tmp_path)), OK, "0" * 64


def row5_rest_url_changed(tmp_path):
    return env(_valid_store(tmp_path)), ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint("https://other")), FINGERPRINT


def row5_drift(tmp_path):
    store = _valid_store(tmp_path)
    store.write_discovery({**store.read_discovery(), "drift_unresolved": True})
    return env(store), OK, FINGERPRINT


def row5_imported_unvalidated(tmp_path):
    from pathlib import Path

    store = make_store(tmp_path)
    write_snapshot(store, snapshot(rest_fp=OK.rest_url_fingerprint))
    init_tenant(store, [{"op": "import_document", "path": str(Path(__file__).parent / "fixtures" / "tenant" / "v1_title.yaml")}])
    return env(store), OK, FINGERPRINT


def row6_valid(tmp_path):
    return env(_valid_store(tmp_path)), OK, FINGERPRINT


def row6_valid_without_connection_check(tmp_path):
    return env(_valid_store(tmp_path)), None, FINGERPRINT


ROWS = [
    (row1_missing_credential, "disconnected", ["env:BULLHORN_PASSWORD"]),
    (row1_connection_failed, "disconnected", ["connection:failed"]),
    (row2_no_store, "connected_setup_required", ["env:BULLHORN_SETUP_STORE"]),
    (row2_store_not_directory, "connected_setup_required", ["store:unusable"]),
    (row2_empty_store, "connected_setup_required", ["profile:active_version", "env:BULLHORN_MCP_ACTOR"]),
    (row3_open_proposal, "setup_in_progress", ["profile:active_version"]),
    (row3_expired_proposal_is_row2, "connected_setup_required", ["profile:active_version"]),
    (row4_unloadable, "setup_invalid", ["profile:invalid", *CAP_MISSING]),
    (row4_broken_last_validation, "setup_invalid", ["broken:field:job.priority", *CAP_MISSING]),
    (row5_catalog_changed, "setup_revalidation_required", ["revalidate:catalog_changed", *CAP_MISSING]),
    (row5_rest_url_changed, "setup_revalidation_required", ["revalidate:rest_url_changed", *CAP_MISSING]),
    (row5_drift, "setup_revalidation_required", ["revalidate:drift_unresolved", *CAP_MISSING]),
    (row5_imported_unvalidated, "setup_revalidation_required", ["revalidate:unvalidated_mappings", *CAP_MISSING]),
    (row6_valid, "setup_valid", CAP_MISSING),
    (row6_valid_without_connection_check, "setup_valid", CAP_MISSING),
]


class TestStateMachine:
    @pytest.mark.parametrize("builder,state,missing", ROWS, ids=[b.__name__ for b, _, _ in ROWS])
    def test_rows(self, tmp_path, builder, state, missing):
        e, connection, catalog = builder(tmp_path)
        result = _state(e, connection, catalog)
        assert result.state == state, result
        assert list(result.missing_requirements) == missing

    def test_credential_rotation_still_valid(self, tmp_path):
        store = _valid_store(tmp_path)
        rotated = {**env(store), **{k: v + "-rotated" for k, v in CREDENTIALS.items()}}
        assert _state(rotated).state == "setup_valid"

    def test_rest_url_change_retains_mappings(self, tmp_path):
        store = _valid_store(tmp_path, [PRIORITY])
        changed = ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint("https://rest2.example/x"))
        assert _state(env(store), changed).state == "setup_revalidation_required"
        assert store.active_version() == 1
        assert store.read_version(1).field_record("job", "priority") is not None

    def test_output_fields(self, tmp_path):
        result = _state(env(_valid_store(tmp_path))).to_dict()
        assert set(result) >= {"state", "missing_requirements", "capabilities", "active_version", "open_proposals", "last_validation"}
        assert result["active_version"] == 1

    def test_validate_recorded_clears_imported(self, tmp_path):
        e, connection, catalog = row5_imported_unvalidated(tmp_path)
        from bullhorn_mcp.tenant.store import store_from_env

        store = store_from_env(e)
        doc = store.read_discovery()
        doc["last_validation"] = {"version": 1, "states": {"field:job.title": "valid"}}
        store.write_discovery(doc)
        assert _state(e, connection, catalog).state == "setup_valid"


class TestCapabilityGate:
    def test_priority_missing_exact(self, tmp_path):
        store = _valid_store(tmp_path)
        gate = require_capability("jobs.priority", env=env(store))
        assert not gate.ok
        assert list(gate.missing) == ["mapping:job.priority", "value_mapping:job.priority:ordering"]

    def test_priority_ok_while_other_unresolved(self, tmp_path):
        store = _valid_store(tmp_path)
        propose_and_commit(store, [PRIORITY, ORDERING])
        state = compute_setup_state(env(store), None, now=NOW)
        assert require_capability("jobs.priority", state).ok
        recruiter = require_capability("jobs.primary_recruiter", state)
        assert not recruiter.ok and list(recruiter.missing) == ["mapping:job.primary_recruiter_id"]
        assert require_capability("canonical_reads", state).ok

    def test_mapping_without_ordering(self, tmp_path):
        store = _valid_store(tmp_path, [PRIORITY])
        gate = require_capability("jobs.priority", env=env(store))
        assert list(gate.missing) == ["value_mapping:job.priority:ordering"]

    def test_unvalidated_mapping_not_ok(self, tmp_path):
        store = make_store(tmp_path)  # no snapshot: the record cannot be validated
        init_tenant(store, [PRIORITY, ORDERING])
        gate = require_capability("jobs.priority", env=env(store))
        assert list(gate.missing) == ["mapping:job.priority"]

    def test_canonical_reads_by_state(self, tmp_path):
        gate = require_capability("canonical_reads", env=env(None))
        assert not gate.ok and list(gate.missing) == ["state:connected_setup_required"]

    def test_unknown_capability(self):
        gate = require_capability("x" * 1000)
        assert not gate.ok and len(gate.missing[0]) < 200
