"""Tests for the Bullhorn standard-field catalog and raw-target parsing."""

import copy
import re
from importlib import resources
from pathlib import Path

import pytest
import yaml

from bullhorn_mcp.bullhorn.client import DEFAULT_FIELDS
from bullhorn_mcp.schema.bullhorn_catalog import (
    BullhornCatalog,
    NestedField,
    RawField,
    TemplateField,
    load_bullhorn_catalog,
    parse_target,
)
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.errors import CatalogError

REPO_ROOT = Path(__file__).resolve().parents[1]
FLAG_FILE = REPO_ROOT / "docs" / "architecture" / "PHASE3_UNVERIFIED_MAPPINGS.md"

# The §4.3 candidate mapping table, verbatim: (Bullhorn entity, canonical entity, canonical field, raw source).
# "a.b" = nested {field: a, key: b}; "{x} {y}" = template.
SPEC_MAPPINGS = {
    ("Candidate", "candidate"): {
        "first_name": "firstName", "last_name": "lastName", "full_name": "{firstName} {lastName}", "email": "email",
        "phone": "phone", "mobile": "mobile", "status": "status", "occupation": "occupation", "skills": "skillSet",
        "source": "source", "city": "address.city", "state": "address.state", "owner_id": "owner.id",
        "date_last_modified": "dateLastModified",
    },
    ("JobOrder", "job"): {
        "title": "title", "status": "status", "employment_type": "employmentType", "is_open": "isOpen",
        "num_openings": "numOpenings", "description": "description", "start_date": "startDate", "salary": "salary",
        "pay_rate": "payRate", "bill_rate": "clientBillRate", "city": "address.city", "state": "address.state",
        "client_corporation_id": "clientCorporation.id", "client_contact_id": "clientContact.id", "owner_id": "owner.id",
    },
    ("JobSubmission", "submission"): {
        "candidate_id": "candidate.id", "job_id": "jobOrder.id", "status": "status", "source": "source",
        "sending_user_id": "sendingUser.id",
    },
    ("Placement", "placement"): {
        "candidate_id": "candidate.id", "job_id": "jobOrder.id", "status": "status", "start_date": "dateBegin",
        "end_date": "dateEnd", "employment_type": "employmentType", "salary": "salary", "pay_rate": "payRate",
        "bill_rate": "clientBillRate",
    },
    ("ClientCorporation", "client_corporation"): {
        "name": "name", "status": "status", "phone": "phone", "website": "companyURL", "city": "address.city",
        "state": "address.state",
    },
    ("ClientContact", "client_contact"): {
        "first_name": "firstName", "last_name": "lastName", "full_name": "{firstName} {lastName}", "email": "email",
        "phone": "phone", "title": "occupation", "status": "status", "client_corporation_id": "clientCorporation.id",
        "owner_id": "owner.id",
    },
    ("Appointment", "appointment"): {
        "subject": "subject", "type": "type", "description": "description", "location": "location",
        "start_at": "dateBegin", "end_at": "dateEnd", "candidate_id": "candidateReference.id",
        "client_contact_id": "clientContactReference.id", "job_id": "jobOrder.id", "owner_id": "owner.id",
    },
    ("Note", "note"): {
        "action": "action", "body": "comments", "person_id": "personReference.id", "author_id": "commentingPerson.id",
        "job_id": "jobOrder.id", "is_deleted": "isDeleted",
    },
    ("Tearsheet", "tearsheet"): {
        "name": "name", "description": "description", "is_private": "isPrivate", "owner_id": "owner.id",
    },
    ("CandidateReference", "candidate_reference"): {
        "candidate_id": "candidate.id", "reference_first_name": "referenceFirstName",
        "reference_last_name": "referenceLastName", "reference_email": "referenceEmail",
        "reference_phone": "referencePhone", "reference_title": "referenceTitle", "company_name": "companyName",
        "status": "status",
    },
}
for _mappings in SPEC_MAPPINGS.values():
    _mappings.setdefault("id", "id")
    _mappings.setdefault("date_added", "dateAdded")


def _spec_target(text):
    if "{" in text:
        return parse_target(text, "spec")[0]
    if "." in text:
        f, k = text.split(".")
        return NestedField(f, k)
    return RawField(text)


def _flag_tables():
    """Parse the flag file into {section title: [row cells]} (header/separator rows dropped)."""
    sections: dict[str, list[list[str]]] = {}
    current = None
    for line in FLAG_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## (\d+)\. (.*)$", line)
        if m:
            current = m.group(2).strip()
            sections[current] = []
            continue
        if current and line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            sections[current].append([c.strip() for c in line.strip("|").split("|")])
    for rows in sections.values():
        if rows:
            rows.pop(0)  # header
    return sections


class TestPackagedBullhornCatalog:
    def test_loads_via_importlib_resources(self):
        text = resources.files("bullhorn_mcp.mappings").joinpath("bullhorn_standard_fields.yaml").read_text(encoding="utf-8")
        catalog = BullhornCatalog.from_yaml_text(text, load_canonical_catalog())
        assert "Candidate" in catalog.entities

    def test_cached(self):
        assert load_bullhorn_catalog() is load_bullhorn_catalog()

    def test_default_mapping_keys_are_canonical(self):
        canonical = load_canonical_catalog()
        for ent in load_bullhorn_catalog().entities.values():
            for cname in ent.default_mappings:
                assert canonical.has_field(ent.canonical_entity, cname), f"{ent.name}:{cname}"

    def test_every_raw_source_in_standard_fields(self):
        for ent in load_bullhorn_catalog().entities.values():
            for cname, target in ent.default_mappings.items():
                for src in target.sources:
                    assert src in ent.standard_fields, f"{ent.name}.{cname} -> {src}"

    @pytest.mark.parametrize("entity", sorted(DEFAULT_FIELDS))
    def test_standard_fields_superset_of_default_fields(self, entity):
        catalog = load_bullhorn_catalog()
        assert entity in catalog.entities
        std = set(catalog.standard_fields(entity))
        for name in DEFAULT_FIELDS[entity].split(","):
            assert name in std, f"{entity}.{name}"

    def test_entity_lookups(self):
        catalog = load_bullhorn_catalog()
        assert catalog.bullhorn_entity_for("job") == "JobOrder"
        assert catalog.canonical_entity_for("JobOrder") == "job"
        assert catalog.bullhorn_entity_for("nope") is None
        assert catalog.canonical_entity_for("Nope") is None
        assert dict(catalog.default_mappings("nope")) == {}

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("customText1", True), ("customText40", True), ("customTextBlock3", True), ("customInt2", True),
            ("customFloat9", True), ("customDate1", True), ("customObject12s", True), ("customEncryptedText1", True),
            ("customBillRate4", True), ("customPayRate10", True), ("customText", False), ("customObject1", False),
            ("xcustomText1", False), ("customText1x", False), ("firstName", False),
        ],
    )
    def test_is_custom_field(self, name, expected):
        assert load_bullhorn_catalog().is_custom_field(name) is expected

    @pytest.mark.parametrize(
        "name,expected",
        [("customEncryptedText3", True), ("ssn", True), ("dateOfBirth", True), ("taxID", True),
         ("customText1", False), ("email", False), ("ssnx", False)],
    )
    def test_is_sensitive(self, name, expected):
        assert load_bullhorn_catalog().is_sensitive(name) is expected

    def test_bullhorn_names_parse_as_plain_yaml(self):
        text = resources.files("bullhorn_mcp.mappings").joinpath("bullhorn_standard_fields.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        assert data["version"] == 1


class TestVerificationRecord:
    """AC-6: the AM-3 flag file and the catalog agree."""

    def test_flag_file_has_five_sections(self):
        sections = _flag_tables()
        titles = list(sections)
        assert titles[0].startswith("Sources consulted")
        assert titles[1].startswith("Omitted Bullhorn entities")
        assert titles[2].startswith("Omitted field mappings")
        assert titles[3].startswith("Omitted `standard_fields` / `custom_field_patterns` entries")
        assert titles[4].startswith("Retained-unverified sensitive patterns")
        assert "https://bullhorn.github.io/rest-api-docs/entityref.html" in FLAG_FILE.read_text(encoding="utf-8")

    def _omitted_mappings(self):
        rows = _flag_tables()["Omitted field mappings"]
        return {(r[0], r[1]) for r in rows if r and r[0] != "None"}

    def _omitted_entities(self):
        rows = _flag_tables()["Omitted Bullhorn entities"]
        return {r[0] for r in rows if r and r[0] != "None"}

    def test_every_spec_mapping_is_present_xor_flagged(self):
        catalog = load_bullhorn_catalog()
        omitted = self._omitted_mappings()
        omitted_entities = self._omitted_entities()
        for (bh, canon), mappings in SPEC_MAPPINGS.items():
            for cname, raw in mappings.items():
                flagged = (bh, cname) in omitted or bh in omitted_entities
                present = bh in catalog.entities and cname in catalog.entities[bh].default_mappings
                assert present != flagged, f"{bh}.{cname}: present={present} flagged={flagged}"
                if present:
                    assert catalog.entities[bh].canonical_entity == canon
                    assert catalog.entities[bh].default_mappings[cname] == _spec_target(raw), f"{bh}.{cname}"

    def test_no_flagged_name_in_catalog(self):
        catalog = load_bullhorn_catalog()
        for bh in self._omitted_entities():
            assert bh not in catalog.entities
        for bh, cname in self._omitted_mappings():
            if bh in catalog.entities:
                assert cname not in catalog.entities[bh].default_mappings


def _bh_minimal() -> dict:
    return {
        "version": 1,
        "custom_field_patterns": ["customText\\d+"],
        "sensitive_field_patterns": ["ssn"],
        "entities": {
            "Candidate": {
                "canonical_entity": "candidate",
                "standard_fields": ["id", "firstName", "lastName", "owner"],
                "default_mappings": {
                    "id": "id",
                    "full_name": "{firstName} {lastName}",
                    "owner_id": {"field": "owner", "key": "id"},
                },
            }
        },
    }


class TestBullhornCatalogValidation:
    def test_minimal_valid(self):
        catalog = BullhornCatalog.from_dict(_bh_minimal(), load_canonical_catalog())
        assert isinstance(catalog.default_mappings("candidate")["full_name"], TemplateField)

    def test_unknown_canonical_mapping_key(self):
        data = _bh_minimal()
        data["entities"]["Candidate"]["default_mappings"]["favourite_colour"] = "firstName"
        with pytest.raises(CatalogError, match="favourite_colour"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_source_not_in_standard_fields(self):
        data = _bh_minimal()
        data["entities"]["Candidate"]["default_mappings"]["email"] = "email"
        with pytest.raises(CatalogError, match="'email' is not listed in standard_fields"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_template_placeholder_not_in_standard_fields(self):
        data = _bh_minimal()
        data["entities"]["Candidate"]["default_mappings"]["full_name"] = "{firstName} {middleName}"
        with pytest.raises(CatalogError, match="middleName"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_unknown_canonical_entity(self):
        data = _bh_minimal()
        data["entities"]["Candidate"]["canonical_entity"] = "spaceship"
        with pytest.raises(CatalogError, match="spaceship"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_duplicate_canonical_binding(self):
        data = _bh_minimal()
        data["entities"]["Other"] = copy.deepcopy(data["entities"]["Candidate"])
        with pytest.raises(CatalogError, match="already bound"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_version_checks(self):
        data = _bh_minimal()
        data["version"] = 2
        with pytest.raises(CatalogError, match="unsupported version"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())
        del data["version"]
        with pytest.raises(CatalogError, match="missing required key 'version'"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_bad_pattern(self):
        data = _bh_minimal()
        data["custom_field_patterns"] = ["custom(Text"]
        with pytest.raises(CatalogError, match="must be an identifier optionally containing one"):  # F-13 grammar
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_errors_aggregated(self):
        data = _bh_minimal()
        data["version"] = 3
        data["custom_field_patterns"] = ["("]
        data["entities"]["Candidate"]["default_mappings"]["email"] = "email"
        with pytest.raises(CatalogError) as exc_info:
            BullhornCatalog.from_dict(data, load_canonical_catalog())
        assert len(exc_info.value.errors) == 3


class TestParseTarget:
    def test_raw(self):
        assert parse_target("firstName", "w") == (RawField("firstName"), None, [])

    def test_nested(self):
        assert parse_target({"field": "owner", "key": "id"}, "w") == (NestedField("owner", "id"), None, [])

    def test_template(self):
        target, _, errors = parse_target("{firstName} {lastName}", "w")
        assert errors == []
        assert isinstance(target, TemplateField)
        assert target.placeholders == ("firstName", "lastName")

    @pytest.mark.parametrize("bad", ["{a.b}", "{a[0]}", "{a!r}", "{a:>5}", "{}", "{0}", "{firstName", "no braces}", "plain {{literal}}"])
    def test_rejected_templates(self, bad):
        target, _, errors = parse_target(bad, "w")
        assert target is None
        assert errors

    @pytest.mark.parametrize("bad", ["first-name", "1abc", "a b", "", "owner.id"])
    def test_rejected_raw_names(self, bad):
        target, _, errors = parse_target(bad, "w")
        assert target is None and errors

    @pytest.mark.parametrize(
        "bad",
        [
            {"field": "owner"},
            {"key": "id"},
            {"field": "owner", "key": "a.b"},
            {"field": "owner", "key": "id", "type": "integer"},  # `type` only allowed in profile custom
            5,
            None,
            [],
        ],
    )
    def test_rejected_other(self, bad):
        target, _, errors = parse_target(bad, "w")
        assert target is None and errors
