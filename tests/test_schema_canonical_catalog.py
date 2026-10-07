"""Tests for the canonical (ATS-agnostic) catalog."""

import copy
import re
from importlib import resources

import pytest
import yaml

from bullhorn_mcp.schema.canonical_catalog import (
    KEY_RE,
    CanonicalCatalog,
    CanonicalField,
    load_canonical_catalog,
)
from bullhorn_mcp.schema.errors import CatalogError, UnknownCanonicalFieldError

# §4.3 minimum canonical field set (plus id and date_added on every entity).
EXPECTED_FIELDS = {
    "candidate": [
        "first_name", "last_name", "full_name", "email", "phone", "mobile", "status", "occupation",
        "skills", "source", "city", "state", "owner_id", "date_last_modified",
    ],
    "job": [
        "title", "status", "employment_type", "is_open", "num_openings", "description", "start_date",
        "salary", "pay_rate", "bill_rate", "city", "state", "client_corporation_id", "client_contact_id", "owner_id",
    ],
    "submission": ["candidate_id", "job_id", "status", "source", "sending_user_id"],
    "placement": [
        "candidate_id", "job_id", "status", "start_date", "end_date", "employment_type", "salary", "pay_rate", "bill_rate",
    ],
    "client_corporation": ["name", "status", "phone", "website", "city", "state"],
    "client_contact": [
        "first_name", "last_name", "full_name", "email", "phone", "title", "status", "client_corporation_id", "owner_id",
    ],
    "appointment": [
        "subject", "type", "description", "location", "start_at", "end_at", "candidate_id",
        "client_contact_id", "job_id", "owner_id",
    ],
    "note": ["action", "body", "person_id", "author_id", "job_id", "is_deleted"],
    "tearsheet": ["name", "description", "is_private", "owner_id"],
    "candidate_reference": [
        "candidate_id", "reference_first_name", "reference_last_name", "reference_email",
        "reference_phone", "reference_title", "company_name", "status",
    ],
}
APPROVED_ADDITIVE_ENTITIES = {"user"}


def _minimal() -> dict:
    return {
        "version": 1,
        "entities": {
            "widget": {
                "description": "x",
                "fields": {
                    "id": {"type": "id", "required": True},
                    "owner_id": {"type": "reference", "ref": "user"},
                },
            }
        },
    }


class TestPackagedCanonicalCatalog:
    def test_loads_via_importlib_resources(self):
        text = resources.files("bullhorn_mcp.mappings").joinpath("canonical_schema.yaml").read_text(encoding="utf-8")
        catalog = CanonicalCatalog.from_yaml_text(text)
        assert set(EXPECTED_FIELDS) <= set(catalog.entities)
        assert set(catalog.entities) - set(EXPECTED_FIELDS) == APPROVED_ADDITIVE_ENTITIES

    def test_exactly_the_ten_entities(self):
        catalog = load_canonical_catalog()
        assert set(EXPECTED_FIELDS) <= set(catalog.entities)
        assert set(catalog.entities) - set(EXPECTED_FIELDS) == APPROVED_ADDITIVE_ENTITIES
        assert len(catalog.entities) == 10 + len(APPROVED_ADDITIVE_ENTITIES)

    @pytest.mark.parametrize("entity", sorted(EXPECTED_FIELDS))
    def test_minimum_fields_present(self, entity):
        catalog = load_canonical_catalog()
        names = set(catalog.entity(entity).field_names)
        for required in ["id", "date_added", *EXPECTED_FIELDS[entity]]:
            assert required in names, f"{entity}.{required}"

    def test_all_keys_snake_case(self):
        catalog = load_canonical_catalog()
        for ent in catalog.entities.values():
            assert KEY_RE.match(ent.name)
            for f in ent.fields:
                assert KEY_RE.match(f.name), f"{ent.name}.{f.name}"

    def test_id_is_required_id_type(self):
        catalog = load_canonical_catalog()
        for name in catalog.entities:
            assert catalog.field(name, "id") == CanonicalField("id", "id", True, None, "Unique record identifier.")

    def test_status_fields_are_strings(self):
        catalog = load_canonical_catalog()
        for ent in catalog.entities.values():
            f = ent.get_field("status")
            if f is not None:
                assert f.type == "string"

    def test_references_have_refs(self):
        catalog = load_canonical_catalog()
        for ent in catalog.entities.values():
            for f in ent.fields:
                if f.type == "reference":
                    assert f.ref
                else:
                    assert f.ref is None

    def test_cached(self):
        assert load_canonical_catalog() is load_canonical_catalog()

    def test_lookup_api(self):
        catalog = load_canonical_catalog()
        assert catalog.has_field("candidate", "email")
        assert not catalog.has_field("candidate", "nope")
        assert not catalog.has_field("nope", "email")
        with pytest.raises(UnknownCanonicalFieldError):
            catalog.entity("nope")
        with pytest.raises(UnknownCanonicalFieldError):
            catalog.field("candidate", "nope")

    def test_no_bullhorn_names_in_canonical_yaml(self):
        """C-2: canonical_schema.yaml contains no Bullhorn entity names or camelCase field names."""
        from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog

        text = resources.files("bullhorn_mcp.mappings").joinpath("canonical_schema.yaml").read_text(encoding="utf-8")
        bh = load_bullhorn_catalog()
        for ent in bh.entities.values():
            assert not re.search(rf"\b{re.escape(ent.name)}\b", text), ent.name
            for f in ent.standard_fields:
                if f != f.lower():
                    assert f not in text, f
        canonical = load_canonical_catalog()
        for name in canonical.entities:
            assert name not in bh.entities


class TestCanonicalCatalogValidation:
    def test_minimal_valid(self):
        catalog = CanonicalCatalog.from_dict(_minimal())
        assert catalog.field("widget", "owner_id").ref == "user"

    def test_version_2_rejected(self):
        data = _minimal()
        data["version"] = 2
        with pytest.raises(CatalogError, match="unsupported version 2"):
            CanonicalCatalog.from_dict(data)

    @pytest.mark.parametrize("bad", [True, "1", 1.0, None])
    def test_non_integer_versions_rejected(self, bad):
        data = _minimal()
        data["version"] = bad
        with pytest.raises(CatalogError):
            CanonicalCatalog.from_dict(data)

    def test_missing_version_rejected(self):
        data = _minimal()
        del data["version"]
        with pytest.raises(CatalogError, match="missing required key 'version'"):
            CanonicalCatalog.from_dict(data)

    def test_unknown_type_rejected(self):
        data = _minimal()
        data["entities"]["widget"]["fields"]["colour"] = {"type": "enum"}
        with pytest.raises(CatalogError, match="unknown type 'enum'"):
            CanonicalCatalog.from_dict(data)

    def test_reference_without_ref_rejected(self):
        data = _minimal()
        del data["entities"]["widget"]["fields"]["owner_id"]["ref"]
        with pytest.raises(CatalogError, match="requires 'ref'"):
            CanonicalCatalog.from_dict(data)

    @pytest.mark.parametrize("bad_key", ["FirstName", "first-name", "1st", "_x", "firstName"])
    def test_bad_field_key_rejected(self, bad_key):
        data = _minimal()
        data["entities"]["widget"]["fields"][bad_key] = {"type": "string"}
        with pytest.raises(CatalogError, match="field key must match"):
            CanonicalCatalog.from_dict(data)

    def test_bad_entity_key_rejected(self):
        data = _minimal()
        data["entities"]["Widget"] = copy.deepcopy(data["entities"]["widget"])
        with pytest.raises(CatalogError, match="entity key must match"):
            CanonicalCatalog.from_dict(data)

    def test_three_defects_reported_together(self):
        data = _minimal()
        data["version"] = 2
        data["entities"]["widget"]["fields"]["colour"] = {"type": "enum"}
        data["entities"]["widget"]["fields"]["BadKey"] = {"type": "string"}
        with pytest.raises(CatalogError) as exc_info:
            CanonicalCatalog.from_dict(data)
        assert len(exc_info.value.errors) == 3
        joined = "\n".join(exc_info.value.errors)
        assert "unsupported version" in joined
        assert "unknown type" in joined
        assert "field key must match" in joined

    def test_yaml_text_three_defects(self):
        text = """
version: 2
entities:
  widget:
    fields:
      id: { type: id }
      colour: { type: colour }
      owner_id: { type: reference }
"""
        with pytest.raises(CatalogError) as exc_info:
            CanonicalCatalog.from_yaml_text(text)
        assert len(exc_info.value.errors) == 3

    def test_non_mapping_top_level(self):
        with pytest.raises(CatalogError):
            CanonicalCatalog.from_dict(["not", "a", "dict"])

    def test_malformed_yaml(self):
        with pytest.raises(CatalogError, match="YAML parse error"):
            CanonicalCatalog.from_yaml_text("entities: [unclosed")

    def test_unsafe_yaml_tags_rejected(self):
        """safe_load refuses arbitrary Python object tags."""
        with pytest.raises(CatalogError, match="YAML parse error"):
            CanonicalCatalog.from_yaml_text("version: !!python/object/apply:os.system ['echo hi']\n")

    def test_packaged_yaml_is_plain_data(self):
        text = resources.files("bullhorn_mcp.mappings").joinpath("canonical_schema.yaml").read_text(encoding="utf-8")
        assert isinstance(yaml.safe_load(text), dict)
