"""Tests for FieldTranslator (canonical <-> raw)."""

import re

import pytest

from bullhorn_mcp.schema.bullhorn_catalog import BullhornCatalog, load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.errors import SchemaError, UnknownCanonicalFieldError
from bullhorn_mcp.schema.mapping_profile import MappingProfile
from bullhorn_mcp.schema.translator import CanonicalRecord, FieldTranslator


_RAW_FIELD_CHARS = re.compile(r"[A-Za-z0-9_]+")


@pytest.fixture(autouse=True)
def _raw_fields_are_plain_identifiers(monkeypatch):
    """R-3e: every FieldResolution produced in these tests has only [A-Za-z0-9_] raw fields."""
    original = FieldTranslator.canonical_to_raw

    def checked(self, entity, fields=None):
        res = original(self, entity, fields)
        for raw in res.raw_fields:
            assert _RAW_FIELD_CHARS.fullmatch(raw), f"unsafe raw field {raw!r}"
        return res

    monkeypatch.setattr(FieldTranslator, "canonical_to_raw", checked)


def _profile(entities: dict) -> MappingProfile:
    return MappingProfile.from_dict({"version": 1, "tenant": "t", "entities": entities})


@pytest.fixture
def translator():
    return FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), None)


@pytest.fixture
def profiled():
    profile = _profile(
        {
            "candidate": {
                "standard": {"skills": "customTextBlock2", "email": "customText1"},
                "custom": {
                    "region": "customText7",
                    "badge": "{firstName}-{customInt2}",
                    "recruiter_ref": {"field": "owner", "key": "id", "type": "integer"},
                },
            }
        }
    )
    return FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), profile)


class TestCanonicalToRaw:
    def test_template_expands_to_placeholders(self, translator):
        res = translator.canonical_to_raw("candidate", ["full_name"])
        assert res.bullhorn_entity == "Candidate"
        assert res.raw_fields == ["id", "firstName", "lastName"]
        assert res.unresolved == []

    def test_nested_requests_outer_field(self, translator):
        res = translator.canonical_to_raw("candidate", ["owner_id", "city", "state"])
        assert res.raw_fields == ["id", "owner", "address"]

    def test_id_always_included_and_deduped(self, translator):
        res = translator.canonical_to_raw("job", ["title", "id", "title", "city", "state"])
        assert res.raw_fields == ["id", "title", "address"]
        assert res.raw_fields.count("id") == 1
        assert res.fields_param == "id,title,address"

    def test_empty_list_still_has_id(self, translator):
        assert translator.canonical_to_raw("job", []).raw_fields == ["id"]

    def test_none_means_all_mapped(self, translator):
        res = translator.canonical_to_raw("candidate", None)
        assert res.raw_fields[0] == "id"
        assert set(res.raw_fields) == {
            "id", "dateAdded", "firstName", "lastName", "email", "phone", "mobile", "status", "occupation",
            "skillSet", "source", "address", "owner", "dateLastModified",
        }
        assert len(res.raw_fields) == len(set(res.raw_fields))
        assert res.unresolved == []

    def test_override_order(self, profiled):
        # standard override beats the catalog default
        assert profiled.canonical_to_raw("candidate", ["skills"]).raw_fields == ["id", "customTextBlock2"]
        assert profiled.canonical_to_raw("candidate", ["email"]).raw_fields == ["id", "customText1"]
        # custom resolves
        assert profiled.canonical_to_raw("candidate", ["region"]).raw_fields == ["id", "customText7"]
        assert profiled.canonical_to_raw("candidate", ["badge"]).raw_fields == ["id", "firstName", "customInt2"]
        # untouched defaults still apply
        assert profiled.canonical_to_raw("candidate", ["phone"]).raw_fields == ["id", "phone"]

    def test_none_includes_custom_and_overrides(self, profiled):
        res = profiled.canonical_to_raw("candidate", None)
        assert "customTextBlock2" in res.raw_fields and "skillSet" not in res.raw_fields
        assert "customText1" in res.raw_fields and "email" not in res.raw_fields
        assert "customText7" in res.raw_fields

    def test_unmapped_custom_name_is_unresolved(self, translator, profiled):
        res = translator.canonical_to_raw("candidate", ["first_name", "region"])
        assert res.raw_fields == ["id", "firstName"]
        assert res.unresolved == ["region"]
        res = profiled.canonical_to_raw("job", ["title", "region"])
        assert res.unresolved == ["region"]

    @pytest.mark.parametrize("bad", ["firstName", "First Name", "1abc", "", "a.b"])
    def test_unknown_field_raises(self, translator, bad):
        with pytest.raises(UnknownCanonicalFieldError):
            translator.canonical_to_raw("candidate", [bad])

    def test_unknown_entity_raises(self, translator):
        with pytest.raises(UnknownCanonicalFieldError):
            translator.canonical_to_raw("Candidate", ["id"])
        with pytest.raises(UnknownCanonicalFieldError):
            translator.canonical_to_raw("spaceship", None)

    def test_hostile_field_lists(self, translator):
        with pytest.raises(SchemaError):
            translator.canonical_to_raw("candidate", "first_name")  # type: ignore[arg-type]
        with pytest.raises(SchemaError):
            translator.canonical_to_raw("candidate", [1])  # type: ignore[list-item]

    def test_unbound_entity(self):
        """An entity with no Bullhorn binding resolves to nothing and never raises (§4.3)."""
        data = {
            "version": 1,
            "entities": {
                "Candidate": {
                    "canonical_entity": "candidate",
                    "standard_fields": ["id", "firstName"],
                    "default_mappings": {"id": "id", "first_name": "firstName"},
                }
            },
        }
        bh = BullhornCatalog.from_dict(data, load_canonical_catalog())
        t = FieldTranslator(load_canonical_catalog(), bh, None)
        assert bh.bullhorn_entity_for("tearsheet") is None
        res = t.canonical_to_raw("tearsheet", ["name", "owner_id"])
        assert res.bullhorn_entity is None
        assert res.raw_fields == []
        assert res.unresolved == ["name", "owner_id"]
        # standard field with no default mapping on a bound entity
        res = t.canonical_to_raw("candidate", ["first_name", "email"])
        assert res.raw_fields == ["id", "firstName"]
        assert res.unresolved == ["email"]
        rec = t.raw_to_canonical("tearsheet", {"id": 1, "name": "x"})
        assert rec.fields == {}
        assert rec.unmapped == {"id": 1, "name": "x"}
        assert "name" in rec.missing


class TestRawToCanonical:
    def test_basic(self, translator, sample_candidate):
        rec = translator.raw_to_canonical("candidate", sample_candidate)
        assert isinstance(rec, CanonicalRecord)
        assert rec.fields["first_name"] == "John"
        assert rec.fields["full_name"] == "John Smith"
        assert rec.fields["date_added"] == 1704067200000  # no coercion
        assert rec.custom == {}

    def test_null_becomes_none(self, translator):
        rec = translator.raw_to_canonical("candidate", {"id": 1, "email": None})
        assert "email" in rec.fields and rec.fields["email"] is None
        assert "email" not in rec.missing

    def test_absent_key_goes_to_missing_not_fields(self, translator):
        rec = translator.raw_to_canonical("candidate", {"id": 1})
        assert "email" in rec.missing
        assert "email" not in rec.fields

    def test_unconsumed_keys_kept_verbatim(self, translator):
        obj = {"nested": [1, {"a": 2}]}
        record = {"id": 1, "firstName": "A", "middleName": "Q", "customText9": obj, "owner": {"firstName": "x"}}
        rec = translator.raw_to_canonical("candidate", record)
        assert rec.unmapped == {"middleName": "Q", "customText9": obj, "owner": {"firstName": "x"}}
        assert rec.unmapped["customText9"] is obj
        assert "owner_id" in rec.missing
        # every key is accounted for: consumed or unmapped
        consumed = {"id", "firstName"}
        assert set(record) == consumed | set(rec.unmapped)

    def test_template_with_missing_part(self, translator):
        rec = translator.raw_to_canonical("candidate", {"id": 1, "firstName": "John"})
        assert "full_name" in rec.missing
        assert "full_name" not in rec.fields

    def test_template_with_null_part(self, translator):
        rec = translator.raw_to_canonical("candidate", {"id": 1, "firstName": "John", "lastName": None})
        assert "full_name" in rec.missing
        assert rec.fields["last_name"] is None

    @pytest.mark.parametrize("outer", [None, 5, "str", [1, 2], {"other": 1}])
    def test_nested_on_non_dict_goes_to_missing(self, translator, outer):
        rec = translator.raw_to_canonical("candidate", {"id": 1, "owner": outer})
        assert "owner_id" in rec.missing
        assert "owner_id" not in rec.fields
        assert rec.unmapped == {"owner": outer}

    def test_nested_success(self, translator):
        rec = translator.raw_to_canonical("job", {"id": 1, "owner": {"id": 9, "firstName": "x"}, "address": {"city": "Austin"}})
        assert rec.fields["owner_id"] == 9
        assert rec.fields["city"] == "Austin"
        assert "state" in rec.missing
        # F-4: unconsumed sibling sub-keys survive as a residual dict.
        assert rec.unmapped == {"owner": {"firstName": "x"}}

    @pytest.mark.parametrize("hostile", [None, [], "x", 5, ({"id": 1},)])
    def test_hostile_record_raises_schema_error(self, translator, hostile):
        with pytest.raises(SchemaError):
            translator.raw_to_canonical("candidate", hostile)

    def test_hostile_values_never_raise_key_or_type_error(self, translator, profiled):
        weird = {
            "id": object(),
            "firstName": {"x": 1},
            "lastName": [1],
            "owner": [],
            "address": "not-a-dict",
            1: "int key",
            None: "none key",
            "customInt2": float("nan"),
        }
        for t in (translator, profiled):
            rec = t.raw_to_canonical("candidate", weird)
            assert rec.unmapped[1] == "int key"
            assert rec.unmapped[None] == "none key"

    def test_custom_fields_surface_separately(self, profiled):
        rec = profiled.raw_to_canonical(
            "candidate",
            {"id": 1, "firstName": "Jo", "customText7": "NE", "customInt2": 42, "owner": {"id": 3}, "customText1": "e@x"},
        )
        assert rec.custom == {"region": "NE", "badge": "Jo-42", "recruiter_ref": 3}
        assert "region" not in rec.fields
        assert rec.fields["email"] == "e@x"
        assert rec.fields["owner_id"] == 3

    def test_custom_missing(self, profiled):
        rec = profiled.raw_to_canonical("candidate", {"id": 1})
        assert {"region", "badge", "recruiter_ref"} <= set(rec.missing)

    def test_many(self, translator):
        out = translator.raw_to_canonical_many("candidate", [{"id": 1}, {"id": 2}])
        assert [r.fields["id"] for r in out] == [1, 2]

    @pytest.mark.parametrize("hostile", [None, {"id": 1}, "x", ({"id": 1},)])
    def test_many_requires_list(self, translator, hostile):
        with pytest.raises(SchemaError):
            translator.raw_to_canonical_many("candidate", hostile)

    def test_many_hostile_element(self, translator):
        with pytest.raises(SchemaError):
            translator.raw_to_canonical_many("candidate", [{"id": 1}, "bad"])

    def test_to_dict(self, translator):
        d = translator.raw_to_canonical("candidate", {"id": 1, "zzz": 2}).to_dict()
        assert d["fields"] == {"id": 1}
        assert d["unmapped"] == {"zzz": 2}
        assert d["entity"] == "candidate" and d["bullhorn_entity"] == "Candidate"


class TestProfileFallback:
    """AC-14."""

    def test_no_profile_uses_catalog_defaults(self, sample_candidate):
        t = FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), None)
        rec = t.raw_to_canonical("candidate", sample_candidate)
        assert rec.fields == {
            "id": 67890,
            "date_added": 1704067200000,
            "first_name": "John",
            "last_name": "Smith",
            "full_name": "John Smith",
            "email": "john.smith@example.com",
            "phone": "555-1234",
            "status": "Active",
            "occupation": "Software Developer",
        }
        assert rec.unmapped == {}
