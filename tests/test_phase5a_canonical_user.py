"""The canonical ``user`` entity (D-5A-16 / D-5-13; AC-21)."""

from __future__ import annotations

from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog

USER_FIELDS = ["id", "date_added", "first_name", "last_name", "full_name", "email", "status"]


def test_user_entity_fields():
    catalog = load_canonical_catalog()
    assert len(catalog.entities) == 11
    assert list(catalog.entity("user").field_names) == USER_FIELDS
    assert catalog.entity("user").get_field("id").type == "id"


def test_every_ref_user_now_resolves():
    catalog = load_canonical_catalog()
    refs = {f.ref for e in catalog.entities for f in catalog.entity(e).fields if f.type == "reference"}
    assert "user" in refs and "user" in catalog.entities


def test_user_binds_to_corporate_user_from_5c():
    assert load_bullhorn_catalog().bullhorn_entity_for("user") == "CorporateUser"  # retargeted by 5C Amendment C3 (D-5C-16; HV-Q8 verified)
