"""AC-16 (report part): discovery snapshots and every drift-report category, incl. NB-7 suppression."""

import json
from pathlib import Path

import pytest

from bullhorn_mcp.bullhorn.client import BullhornAPIError
from bullhorn_mcp.bullhorn.meta import parse_entity_meta
from bullhorn_mcp.schema.errors import SchemaError
from bullhorn_mcp.tenant.profile_v2 import FORMAT, TenantProfileV2, current_catalog_fingerprint
from bullhorn_mcp.tenant.revalidation import (
    DiscoverySnapshot,
    build_drift_report,
    discover,
    entity_snapshot,
    merge_snapshots,
)
from bullhorn_mcp.tenant.store import SetupStoreError

from ._tenant_helpers import NOW_TEXT, field_entry, meta_source, tenant_payloads

FIXTURES = Path(__file__).parent / "fixtures" / "tenant"
ENTITIES = ["job", "candidate", "appointment"]


def _snap(name: str) -> DiscoverySnapshot:
    payloads = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return discover(meta_source(payloads), ENTITIES, NOW_TEXT, None)


def _profile() -> TenantProfileV2:
    return TenantProfileV2.from_dict(
        {
            "format": FORMAT,
            "tenant": {"id": "acme"},
            "profile_version": 1,
            "catalog_fingerprint": current_catalog_fingerprint(),
            "field_mappings": [
                {"entity": "job", "field": "bill_rate", "target": "clientBillRate"},
                {"entity": "job", "field": "priority", "target": "customText12"},
                {"entity": "job", "field": "region", "kind": "custom", "target": "customText3"},
                {"entity": "candidate", "field": "first_name", "target": "firstName"},
                {"entity": "job", "field": "title", "target": "nowhere", "active": False},
            ],
            "value_mappings": [
                {"key": "job.priority.order", "target": {"kind": "ordering", "entity": "job", "field": "priority"},
                 "bullhorn_field": "customText12", "values": ["A", "B"]},
                {"key": "done", "entity": "appointment", "target": {"kind": "concept", "name": "interview_completed"},
                 "bullhorn_field": "type", "values": ["Interview"]},
            ],
        }
    )


@pytest.fixture
def report():
    previous, current = _snap("meta_previous.json"), _snap("meta_current.json")
    return build_drift_report(previous, merge_snapshots(previous, current), _profile(), ENTITIES)


def _items(report, category):
    return report[category]["items"]


class TestDriftReport:
    def test_new_fields(self, report):
        assert {"entity": "job", "field": "customText9"} in _items(report, "new_fields")

    def test_removed_fields(self, report):
        removed = _items(report, "removed_fields")
        assert {"entity": "job", "field": "clientBillRate"} in removed
        assert {"entity": "job", "field": "customInt7"} in removed

    def test_changed_fields(self, report):
        changed = {(c["field"]): c["attributes"] for c in _items(report, "changed_fields")}
        assert changed["customText3"] == ["label"]
        assert changed["customText12"] == ["options"]

    def test_broken_mappings_active_only(self, report):
        broken = _items(report, "broken_mappings")
        assert broken == [{"key": "field:job.bill_rate", "missing_sources": ["clientBillRate"]}]

    def test_newly_unmapped(self, report):
        assert _items(report, "newly_unmapped") == [{"entity": "job", "field": "customText9"}]

    def test_value_drift(self, report):
        drift = {d["key"]: d for d in _items(report, "value_drift")}
        assert drift["value:job.priority.order"] == {"key": "value:job.priority.order", "status": "drift", "values": ["B"]}
        assert drift["value:done"]["status"] == "unverifiable"

    def test_meta_unusable_suppresses(self, report):
        assert [m["entity"] for m in _items(report, "meta_unusable")] == ["candidate"]
        assert not any(b["key"].startswith("field:candidate.") for b in _items(report, "broken_mappings"))
        assert not any(r["entity"] == "candidate" for r in _items(report, "removed_fields"))

    def test_has_findings(self, report):
        assert report["has_findings"] is True

    def test_no_findings_when_stable(self):
        previous = _snap("meta_previous.json")
        report = build_drift_report(previous, previous, None, ENTITIES)
        assert report["has_findings"] is False

    def test_unverifiable_alone_is_not_a_finding(self):
        previous = _snap("meta_previous.json")
        profile = TenantProfileV2.from_dict(
            {**_profile().to_dict(), "field_mappings": [],
             "value_mappings": [_profile().value_mappings[1].to_dict()]}
        )
        report = build_drift_report(previous, previous, profile, ENTITIES)
        assert _items(report, "value_drift")[0]["status"] == "unverifiable"
        assert report["has_findings"] is False

    def test_first_discovery_is_baseline(self):
        current = _snap("meta_current.json")
        report = build_drift_report(None, current, None, ["job"])
        assert report["new_fields"]["count"] == 0 and report["newly_unmapped"]["count"] == 0


class TestDiscovery:
    def test_only_meta_source_used(self):
        source = meta_source(tenant_payloads())
        snap = discover(source, None, NOW_TEXT, None)
        assert "job" in snap.entities and "customText12" in snap.entity("job").fields
        called = [c.args[0] for c in source.get_entity_meta.call_args_list]
        assert len(called) == len(set(called))  # each entity fetched once

    def test_api_error_bounded(self):
        source = meta_source(tenant_payloads(), failures={"JobOrder": BullhornAPIError("API request failed: 500 - " + "x" * 50_000)})
        snap = discover(source, ["job"], NOW_TEXT, None)
        ent = snap.entity("job")
        assert ent.error is not None and len(ent.error) <= 300 and not ent.usable

    def test_invalid_entities_raise_schema_error(self):
        with pytest.raises(SchemaError):
            discover(meta_source({}), ["widget"], NOW_TEXT, None)

    def test_warnings_capped(self):
        fields = [{"name": f"customText{i}", "label": None, "type": "SCALAR"} for i in range(1, 400)]
        meta = parse_entity_meta("JobOrder", {"fields": fields})
        snap = entity_snapshot("job", "JobOrder", meta, None)
        assert len(snap.warnings) == 101 and snap.warnings[-1].startswith("... and")

    def test_hostile_meta_values_bounded(self):
        meta = parse_entity_meta(
            "JobOrder",
            {"fields": [
                field_entry("customText1", label="L" * 100_000, dataType="D" * 10_000),
                field_entry("customText2", options=[{"value": float("nan"), "label": "x"}]),
                field_entry("customText3", options=[{"value": "v" * 10_000, "label": "x"}]),
                field_entry("customText4", options=[{"value": i, "label": str(i)} for i in range(2000)]),
                {"name": "bad name!", "type": "SCALAR"},
            ]},
        )
        snap = entity_snapshot("job", "JobOrder", meta, None)
        f1 = snap.fields["customText1"]
        assert len(f1.label) <= 200 and len(f1.data_type) <= 100
        assert snap.fields["customText2"].options is None
        assert snap.fields["customText3"].options is None
        assert snap.fields["customText4"].options is None
        assert "bad name!" not in snap.fields
        assert len(json.dumps(snap.to_dict())) < 50_000

    def test_meta_unusable_flag(self):
        meta = parse_entity_meta("Candidate", {"fields": "nope"})
        assert entity_snapshot("candidate", "Candidate", meta, None).meta_unusable

    def test_required_read_only_informational(self):
        # HV-A1: required/readonly are documented as meta=full-only; a change is reported, never a broken mapping.
        prev = parse_entity_meta("JobOrder", {"fields": [field_entry("title")]})
        cur = parse_entity_meta("JobOrder", {"fields": [field_entry("title", required=True, readOnly=True)]})
        p = DiscoverySnapshot(NOW_TEXT, None, {"job": entity_snapshot("job", "JobOrder", prev, None)})
        c = DiscoverySnapshot(NOW_TEXT, None, {"job": entity_snapshot("job", "JobOrder", cur, None)})
        report = build_drift_report(p, c, None, ["job"])
        assert _items(report, "changed_fields") == [{"entity": "job", "field": "title", "attributes": ["required", "read_only"]}]
        assert report["broken_mappings"]["count"] == 0


class TestSnapshotSerialization:
    def test_round_trip(self):
        snap = _snap("meta_previous.json")
        assert DiscoverySnapshot.from_dict(json.loads(json.dumps(snap.to_dict()))) == snap

    @pytest.mark.parametrize(
        "data",
        [None, [], {}, {"checked_at": 1, "entities": {}}, {"checked_at": "x", "entities": {"job": {"fields": []}}},
         {"checked_at": "x", "entities": {"job": {"fields": {"a": {"name": "b"}}}}}],
        ids=repr,
    )
    def test_corrupt(self, data):
        with pytest.raises(SetupStoreError):
            DiscoverySnapshot.from_dict(data)

    def test_no_sample_key(self):
        text = json.dumps(_snap("meta_current.json").to_dict())
        assert '"sample"' not in text
