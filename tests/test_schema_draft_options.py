"""F-10 regression tests: meta-derived options always produce a valid, round-trippable draft (R-10a..R-10e)."""

import json
from unittest.mock import Mock, PropertyMock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.bullhorn.meta import MetaDiscovery, parse_entity_meta
from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.discovery import SchemaDiscoverer, build_draft_profile
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.schema.mapping_profile import MappingProfile

# (meta options, expected draft options) - the R-10a table.
R10A = [
    ([{"value": 1}], ((1, "1"),)),
    ([{"value": True}], ((True, "true"),)),
    ([{"value": 1.5}], ((1.5, "1.5"),)),
    ([{"value": "A", "label": 7}], (("A", "7"),)),
    ([{"value": "A"}], (("A", "A"),)),
    ([{"value": "A", "label": None}], (("A", None),)),
    ([{"value": "A", "label": "Alpha"}], (("A", "Alpha"),)),
    ([{"value": 2, "label": 2.0}], ((2, "2.0"),)),
    ([{"value": "A", "label": False}], (("A", "false"),)),
]
R10A_IDS = ["int", "bool", "float", "int_label", "no_label", "null_label", "str_label", "float_label", "bool_label"]

NON_FINITE = ["NaN", "Infinity", "-Infinity"]


@pytest.fixture
def mock_auth(mock_session):
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


def _discover(mock_auth, entities):
    return SchemaDiscoverer(MetaDiscovery(BullhornClient(mock_auth)), load_canonical_catalog(), load_bullhorn_catalog()).discover(
        entities=entities
    )


def _assert_round_trips(draft, tmp_path):
    assert MappingProfile.from_dict(draft.to_dict()) == draft
    out = tmp_path / "draft.yaml"
    draft.save(out)
    assert MappingProfile.load(out) == draft


def _options_for(draft, entity, field):
    return next(u for u in draft.entity(entity).unmapped_bullhorn_fields if u.field == field).options


class TestR10aEndToEnd:
    @respx.mock
    @pytest.mark.parametrize("meta_options,expected", R10A, ids=R10A_IDS)
    def test_meta_options_to_valid_draft(self, mock_auth, mock_session, tmp_path, meta_options, expected):
        body = {"fields": [{"name": "id"}, {"name": "customText1", "label": "Region", "options": meta_options}]}
        respx.get(f"{mock_session.rest_url}/meta/Candidate").mock(return_value=httpx.Response(200, json=body))
        report = _discover(mock_auth, ["candidate"])
        draft = build_draft_profile(report, tenant="t")
        _assert_round_trips(draft, tmp_path)
        assert _options_for(draft, "candidate", "customText1") == expected


class TestR10bNonFiniteFromMeta:
    @respx.mock
    @pytest.mark.parametrize("token", NON_FINITE)
    def test_non_finite_value_drops_options(self, mock_auth, mock_session, tmp_path, token):
        raw = '{"fields":[{"name":"id"},{"name":"customText1","label":"X","options":[{"value":%s}]}]}' % token
        respx.get(f"{mock_session.rest_url}/meta/Candidate").mock(
            return_value=httpx.Response(200, content=raw.encode(), headers={"content-type": "application/json"})
        )
        report = _discover(mock_auth, ["candidate"])
        draft = build_draft_profile(report, tenant="t")
        fields = [u.field for u in draft.entity("candidate").unmapped_bullhorn_fields]
        assert "customText1" in fields
        assert _options_for(draft, "candidate", "customText1") is None
        _assert_round_trips(draft, tmp_path)


class TestR10cMultiEntity:
    @respx.mock
    def test_mixed_shapes_across_entities(self, mock_auth, mock_session, tmp_path):
        shapes = [json.dumps(opts) for opts, _ in R10A] + [f'[{{"value":{t}}}]' for t in NON_FINITE]
        entities = {"Candidate": "candidate", "JobOrder": "job", "Placement": "placement"}
        for bh in entities:
            fields = ",".join(
                '{"name":"customText%d","label":"L%d","options":%s}' % (i + 1, i, shape) for i, shape in enumerate(shapes)
            )
            raw = '{"fields":[{"name":"id"},%s]}' % fields
            respx.get(f"{mock_session.rest_url}/meta/{bh}").mock(
                return_value=httpx.Response(200, content=raw.encode(), headers={"content-type": "application/json"})
            )
        report = _discover(mock_auth, list(entities.values()))
        draft = build_draft_profile(report, tenant="t")
        for canon in entities.values():
            ep = draft.entity(canon)
            assert ep is not None
            assert len(ep.unmapped_bullhorn_fields) == len(shapes)
        for i, (_, expected) in enumerate(R10A):
            assert _options_for(draft, "job", f"customText{i + 1}") == expected
        for j in range(len(NON_FINITE)):
            assert _options_for(draft, "placement", f"customText{len(R10A) + j + 1}") is None
        _assert_round_trips(draft, tmp_path)


class TestR10dProfileNonFinite:
    @pytest.mark.parametrize("token", [".nan", ".inf", "-.inf", ".NaN", ".Inf"])
    def test_non_finite_option_value_rejected(self, tmp_path, token):
        p = tmp_path / "p.yaml"
        p.write_text(
            f"version: 1\nentities:\n  candidate:\n    unmapped_bullhorn_fields:\n"
            f"      - field: customText1\n        options:\n          - {{value: {token}}}\n",
            encoding="utf-8",
        )
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.load(p)
        assert any("must be a finite number" in e for e in exc_info.value.errors)

    def test_finite_float_round_trips(self, tmp_path):
        profile = MappingProfile.from_dict(
            {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": [{"field": "customText1", "options": [{"value": 1.5}]}]}}}
        )
        out = tmp_path / "p.yaml"
        profile.save(out)
        assert MappingProfile.load(out) == profile


class TestR10eMetaBehaviorPinned:
    def test_meta_keeps_raw_option_types(self):
        meta = parse_entity_meta("Candidate", {"fields": [{"name": "customText1", "options": [{"value": 1}]}]})
        assert meta.get_field("customText1").options == ((1, 1),)
        meta = parse_entity_meta("Candidate", {"fields": [{"name": "customText1", "options": [{"value": "A", "label": 7}]}]})
        assert meta.get_field("customText1").options == (("A", 7),)

    @respx.mock
    def test_get_entity_meta_keeps_raw_option_types(self, mock_auth, mock_session):
        body = {
            "fields": [
                {"name": "customText1", "options": [{"value": 1}]},
                {"name": "customText2", "options": [{"value": "A", "label": 7}]},
            ]
        }
        respx.get(f"{mock_session.rest_url}/meta/Candidate").mock(return_value=httpx.Response(200, json=body))
        meta = MetaDiscovery(BullhornClient(mock_auth)).get_entity_meta("Candidate")
        assert meta.get_field("customText1").options == ((1, 1),)
        assert meta.get_field("customText2").options == (("A", 7),)
