"""AC-7 / AC-8: v2 profile validation, serialization and the v1 view."""

from pathlib import Path

import pytest

from bullhorn_mcp.schema import FieldTranslator, MappingProfile, load_bullhorn_catalog, load_canonical_catalog
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.tenant.profile_v2 import (
    FORMAT,
    TenantProfileV2,
    current_catalog_fingerprint,
    load_activity_concepts,
    rest_url_fingerprint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tenant"


def _load(name: str) -> TenantProfileV2:
    return TenantProfileV2.from_yaml_text((FIXTURES / name).read_text(encoding="utf-8"), source=name)


def _base(**overrides):
    doc = {
        "format": FORMAT,
        "tenant": {"id": "acme", "label": None},
        "profile_version": 1,
        "catalog_fingerprint": current_catalog_fingerprint(),
        "rest_url_fingerprint": None,
        "field_mappings": [],
        "value_mappings": [],
        "settings": {"reporting_timezone": "UTC"},
    }
    doc.update(overrides)
    return doc


class TestActivityConcepts:
    def test_verbatim_ids(self):
        assert load_activity_concepts() == frozenset(
            {
                "job_created", "job_status_changed", "candidate_status_changed", "submission_created",
                "client_submission", "interview_scheduled", "interview_rescheduled", "interview_completed",
                "interview_cancelled", "interview_upcoming", "offer_extended", "offer_accepted", "offer_declined",
                "offer_pending", "placement_created", "note_created",
            }
        )


class TestValidDocument:
    def test_loads(self):
        p = _load("valid.yaml")
        assert p.tenant_id == "acme" and p.tenant_label == "Acme Staffing"
        assert [r.key for r in p.field_mappings] == ["field:job.priority", "field:job.hot_flag"]
        assert p.value_mappings[0].entity == "job"  # implied by the ordering target
        assert p.value_mappings[1].validation.values_unverified is True  # default for value mappings

    def test_yaml_round_trip(self):
        p = _load("valid.yaml")
        again = TenantProfileV2.from_yaml_text(p.to_yaml_text())
        assert again == p

    def test_canonical_fields_added(self):
        catalog = load_canonical_catalog()
        assert catalog.field("job", "priority").type == "string"
        ref = catalog.field("job", "primary_recruiter_id")
        assert ref.type == "reference" and ref.ref == "user"
        assert load_bullhorn_catalog().default_mappings("job").get("priority") is None
        assert load_bullhorn_catalog().default_mappings("job").get("primary_recruiter_id") is None


class TestToV1:
    def test_v1_view_translates_priority(self):
        p = _load("valid.yaml")
        v1 = p.to_v1_profile()
        assert isinstance(v1, MappingProfile)
        assert MappingProfile.from_dict(v1.to_dict()) == v1
        translator = FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), v1)
        assert translator.shared_mappings("job")["priority"].to_data() == "customText12"
        assert "customText12" in translator.canonical_to_raw("job", ["priority"]).raw_fields
        rec = translator.raw_to_canonical("job", {"customText12": "A", "customObject1s": {"id": 7}})
        assert rec.fields["priority"] == "A" and rec.custom["hot_flag"] == 7

    def test_inactive_records_excluded(self):
        doc = _base(field_mappings=[{"entity": "job", "field": "priority", "target": "customText12", "active": False}])
        v1 = TenantProfileV2.from_dict(doc).to_v1_profile()
        assert v1.entity("job") is None

    def test_custom_type_carried_for_nested_only(self):
        doc = _base(
            field_mappings=[
                {"entity": "job", "field": "a_num", "kind": "custom", "target": "customInt1", "type": "integer"},
                {"entity": "job", "field": "b_ref", "kind": "custom", "target": {"field": "owner", "key": "id"}, "type": "integer"},
            ]
        )
        v1 = TenantProfileV2.from_dict(doc).to_v1_profile()
        custom = v1.entity("job").custom
        assert custom["a_num"].type == "string"  # the v1 format cannot carry a type for a raw-name target
        assert custom["b_ref"].type == "integer"


INVALID = [
    ("unknown_entity.yaml", "is not a canonical entity"),
    ("unknown_field.yaml", "is not a canonical field"),
    ("id_mapping.yaml", "always maps to raw 'id'"),
    ("two_active.yaml", "more than one active record"),
    ("invalid_targets.yaml", "target"),
    ("custom_collision.yaml", "collides with canonical field"),
    ("concept_conflict.yaml", "conflict"),
    ("unknown_concept.yaml", "is not a known activity concept"),
    ("ordering_unknown_field.yaml", "is not a canonical field"),
    ("non_finite_value.yaml", "must be a finite scalar"),
    ("duplicate_key.yaml", "duplicate mapping key"),
    ("invalid_timezone.yaml", "unknown timezone"),
]


class TestInvalidFixtures:
    @pytest.mark.parametrize("name,needle", INVALID, ids=[n for n, _ in INVALID])
    def test_raises_profile_error_only(self, name, needle):
        with pytest.raises(ProfileError) as info:
            _load(name)
        assert type(info.value) is ProfileError
        assert any(needle in e for e in info.value.errors), info.value.errors
        assert len(str(info.value)) < 50_000

    def test_invalid_targets_aggregated(self):
        with pytest.raises(ProfileError) as info:
            _load("invalid_targets.yaml")
        # every one of the ten bad targets is reported, not just the first
        assert len(info.value.errors) >= 10


HOSTILE_VALUES = [None, 1, 1.5, True, "x", [], {}, [1, [2]], {"a": {"b": None}}, "é" * 1000, float("nan"), 10**100]


class TestHostileShapes:
    @pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: repr(v)[:20])
    @pytest.mark.parametrize(
        "path",
        [
            "top", "format", "tenant", "tenant.id", "tenant.label", "profile_version", "catalog_fingerprint",
            "rest_url_fingerprint", "field_mappings", "field_mappings.0", "fm.entity", "fm.field", "fm.kind", "fm.target",
            "fm.type", "fm.source", "fm.active", "fm.validation", "fm.validation.state", "fm.created_at", "value_mappings",
            "vm.key", "vm.target", "vm.target.kind", "vm.target.name", "vm.bullhorn_field", "vm.values", "vm.values.0",
            "vm.entity", "vm.validation.values_unverified", "settings", "settings.reporting_timezone",
        ],
    )
    def test_only_profile_error(self, path, value):
        doc = _base(
            field_mappings=[
                {"entity": "job", "field": "priority", "target": "customText12", "validation": {"state": "valid"},
                 "created_at": "2026-10-06T00:00:00Z"}
            ],
            value_mappings=[
                {"key": "k", "entity": "appointment", "target": {"kind": "concept", "name": "interview_completed"},
                 "bullhorn_field": "type", "values": ["A"], "validation": {"values_unverified": True}}
            ],
            settings={"reporting_timezone": "UTC"},
            tenant={"id": "acme", "label": None},
        )
        if path == "top":
            doc = value
        elif "." not in path:
            doc[path] = value
        else:
            head, *rest = path.split(".")
            target = {"fm": doc["field_mappings"], "vm": doc["value_mappings"]}.get(head)
            if target is not None:
                node = target[0]
            elif head == "field_mappings":
                node = doc["field_mappings"]
            else:
                node = doc[head]
            for part in rest[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            last = rest[-1]
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        try:
            TenantProfileV2.from_dict(doc)
        except ProfileError as exc:
            assert type(exc) is ProfileError
            assert len(str(exc)) < 50_000


class TestFingerprints:
    def test_catalog_fingerprint_is_sha256(self):
        fp = current_catalog_fingerprint()
        assert len(fp) == 64 and int(fp, 16) >= 0

    def test_rest_url_fingerprint_hides_url(self):
        fp = rest_url_fingerprint("https://rest9.bullhornstaffing.com/rest-services/corp/")
        assert fp is not None and "corp" not in fp and len(fp) == 64
        assert rest_url_fingerprint(None) is None and rest_url_fingerprint("") is None
