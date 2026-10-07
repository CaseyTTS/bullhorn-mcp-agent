"""Meta-snapshot validation of v2 records, incl. the HV-A2 guard (options may be missing -> values_unverified)."""

from bullhorn_mcp.tenant.profile_v2 import FORMAT, TenantProfileV2, current_catalog_fingerprint
from bullhorn_mcp.tenant.validation import IMPORTED_DETAIL, evaluate

from ._tenant_helpers import snapshot, tenant_payloads


def _profile(field_mappings=(), value_mappings=()):
    return TenantProfileV2.from_dict(
        {
            "format": FORMAT,
            "tenant": {"id": "acme"},
            "profile_version": 1,
            "catalog_fingerprint": current_catalog_fingerprint(),
            "field_mappings": list(field_mappings),
            "value_mappings": list(value_mappings),
        }
    )


ORDER = {
    "key": "job.priority.order",
    "target": {"kind": "ordering", "entity": "job", "field": "priority"},
    "bullhorn_field": "customText12",
}


class TestValueMappings:
    def test_options_absent_means_values_unverified(self):
        snap = snapshot(tenant_payloads(job_extra={"customText12": {}}))
        result = evaluate(_profile(value_mappings=[{**ORDER, "values": ["A"]}]), snap)
        state = result.states["value:job.priority.order"]
        assert state.state == "valid" and state.values_unverified is True
        assert result.values_unverified == ("value:job.priority.order",)
        assert result.ok  # a warning, not an error

    def test_options_present_and_matching(self):
        result = evaluate(_profile(value_mappings=[{**ORDER, "values": ["A", "B"]}]), snapshot())
        assert result.states["value:job.priority.order"].values_unverified is False
        assert result.values_unverified == ()

    def test_value_not_in_options(self):
        result = evaluate(_profile(value_mappings=[{**ORDER, "values": ["A", "Z"]}]), snapshot())
        state = result.states["value:job.priority.order"]
        assert state.values_unverified is True and "'Z'" in state.detail

    def test_type_aware_option_match(self):
        snap = snapshot(tenant_payloads(job_extra={"customText12": {"options": [{"value": 1, "label": "one"}]}}))
        result = evaluate(_profile(value_mappings=[{**ORDER, "values": ["1"]}]), snap)
        assert result.states["value:job.priority.order"].values_unverified is True

    def test_field_absent_is_broken(self):
        snap = snapshot(tenant_payloads(job_extra={}))
        result = evaluate(_profile(value_mappings=[{**ORDER, "values": ["A"]}]), snap)
        assert result.broken == ("value:job.priority.order",) and not result.ok


class TestFieldMappings:
    def test_states(self):
        profile = _profile(
            field_mappings=[
                {"entity": "job", "field": "priority", "target": "customText12"},
                {"entity": "job", "field": "title", "target": "nope"},
                {"entity": "job", "field": "status", "target": "nope", "active": False},
                {"entity": "candidate", "field": "first_name", "target": "firstName"},
            ]
        )
        snap = snapshot(errors={"Candidate": "BullhornAPIError: 500"})
        result = evaluate(profile, snap)
        assert result.states["field:job.priority"].state == "valid"
        assert result.states["field:job.title"].state == "broken"
        assert result.states["field:job.status"].state == "broken"
        assert result.broken == ("field:job.title",)  # inactive records never block
        assert result.states["field:candidate.first_name"].state == "unresolved"
        assert "candidate.first_name" in result.unresolved

    def test_no_snapshot_is_unvalidated(self):
        result = evaluate(_profile(field_mappings=[{"entity": "job", "field": "priority", "target": "customText12"}]), None)
        assert result.states["field:job.priority"].state == "unvalidated"
        assert result.unvalidated == ("field:job.priority",)

    def test_imported_stay_unvalidated_unless_broken(self):
        pending = {"state": "unvalidated", "detail": IMPORTED_DETAIL}
        profile = _profile(
            field_mappings=[
                {"entity": "job", "field": "priority", "target": "customText12", "validation": pending},
                {"entity": "job", "field": "title", "target": "nope", "validation": pending},
            ]
        )
        result = evaluate(profile, snapshot(), keep_unvalidated={"field:job.priority", "field:job.title"})
        assert result.states["field:job.priority"].state == "unvalidated"
        assert result.states["field:job.title"].state == "broken"
