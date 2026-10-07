"""F-11 regression tests: serialization fidelity and strict-JSON reports (R-11a..R-11g)."""

import json
import unicodedata
from unittest.mock import Mock, PropertyMock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.bullhorn.meta import MetaDiscovery
from bullhorn_mcp.schema import mapping_profile as mapping_profile_module
from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.discovery import SchemaDiscoverer, build_draft_profile
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.schema.mapping_profile import MappingProfile
from bullhorn_mcp.schema.translator import FieldTranslator
from tests._unicode_corpus import category_representatives, corpus, placements

RECORD = {"firstName": "A", "lastName": "B"}

POSITIONS = [
    "tenant", "generated_at", "label", "data_type", "field_type",
    "option_value", "option_label", "standard_template", "custom_template",
]
TEMPLATE_POSITIONS = {"standard_template", "custom_template"}


def _profile_dict(position: str, s: str) -> dict:
    entity: dict = {}
    data = {"version": 1, "entities": {"candidate": entity}}
    if position in ("tenant", "generated_at"):
        data[position] = s
    elif position in ("label", "data_type", "field_type"):
        entity["unmapped_bullhorn_fields"] = [{"field": "customText1", position: s}]
    elif position == "option_value":
        entity["unmapped_bullhorn_fields"] = [{"field": "customText1", "options": [{"value": s}]}]
    elif position == "option_label":
        entity["unmapped_bullhorn_fields"] = [{"field": "customText1", "options": [{"value": "A", "label": s}]}]
    elif position == "standard_template":
        entity["standard"] = {"full_name": "{firstName}" + s + "{lastName}"}
    elif position == "custom_template":
        entity["custom"] = {"t": "{firstName}" + s + "{lastName}"}
    else:  # pragma: no cover
        raise AssertionError(position)
    return data


def _rendered(profile: MappingProfile, position: str):
    rec = FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), profile).raw_to_canonical("candidate", dict(RECORD))
    return rec.fields.get("full_name") if position == "standard_template" else rec.custom.get("t")


@pytest.fixture
def mock_auth(mock_session):
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


def _json_response(body) -> httpx.Response:
    # ensure_ascii (the json default) so lone surrogates travel as \\uXXXX escapes.
    return httpx.Response(200, content=json.dumps(body).encode("ascii"), headers={"content-type": "application/json"})


# ---------------------------------------------------------------------- #
# R-11a / R-11b
# ---------------------------------------------------------------------- #


class TestR11aCorpus:
    def test_corpus_contents(self):
        cps = {ord(c) for c in corpus()}
        assert set(range(0x20)) <= cps and 0x7F in cps and set(range(0x80, 0xA0)) <= cps
        assert 0x85 in cps  # NEL
        for cp in (0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF, 0x10FFFF, 0xE0001, 0x301):
            assert cp in cps
        reps = category_representatives()
        assert len(reps) == 30
        assert {unicodedata.category(chr(c)) for c in reps} == {unicodedata.category(chr(c)) for c in cps if c in reps}


R11B_CASES = [
    pytest.param(position, placement, s, id=f"{position}-{placement}-U+{ord(ch):04X}")
    for ch in corpus()
    for placement, s in placements(ch).items()
    for position in POSITIONS
    if not (position in TEMPLATE_POSITIONS and ch in "{}")
]


@pytest.fixture(scope="module")
def shared_dir(tmp_path_factory):
    # One directory for the ~5,000 R-11b cases (per-test tmp_path creation dominates runtime).
    return tmp_path_factory.mktemp("r11b")


@pytest.mark.parametrize("position,placement,s", R11B_CASES)
def test_r11b_every_string_position_round_trips(shared_dir, position, placement, s):
    try:
        profile = MappingProfile.from_dict(_profile_dict(position, s))
    except ProfileError:
        return  # rejecting is allowed; corrupting is not
    out = shared_dir / "p.yaml"
    profile.save(out)
    reloaded = MappingProfile.load(out)
    assert reloaded == profile
    if position in TEMPLATE_POSITIONS:
        assert _rendered(reloaded, position) == _rendered(profile, position)


def test_r11b_scale():
    assert len(R11B_CASES) >= 3_500


# ---------------------------------------------------------------------- #
# R-11c
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [0, -0, 2**63, -(2**63), 10**4000, -0.0, 5e-324, 1.7976931348623157e308, True, False, None],
    ids=["0", "neg0", "2^63", "-2^63", "10^4000", "-0.0", "min_subnormal", "max_float", "true", "false", "null"],
)
def test_r11c_non_string_scalars(tmp_path, value):
    entry = {"field": "customText1", "options": [{"value": value}]}
    data = {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": [entry]}}}
    try:
        profile = MappingProfile.from_dict(data)
        out = tmp_path / "p.yaml"
        profile.save(out)
    except ProfileError:
        return
    reloaded = MappingProfile.load(out)
    assert reloaded == profile
    got = reloaded.entity("candidate").unmapped_bullhorn_fields[0].options[0][0]
    assert type(got) is type(value)


# ---------------------------------------------------------------------- #
# R-11d / R-11f / R-11g: draft path and strict-JSON reports
# ---------------------------------------------------------------------- #


def _corpus_meta_fields():
    chars = list(corpus())
    fields = [{"name": "id"}]
    for i in range(0, len(chars), 10):
        chunk = chars[i : i + 10]
        text = "".join(chunk)
        fields.append(
            {
                "name": f"customText{i // 10 + 1}",
                "label": "L" + text,
                "dataType": text,
                "type": "T" + text,
                "options": [{"value": c, "label": "x" + c + "y"} for c in chunk],
            }
        )
    # The Reviewer's explicit cases.
    fields.append({"name": "customText90", "label": "Region\u0085Code", "options": [{"value": "A", "label": "x\u0085y"}]})
    return fields


def _discover_draft(mock_auth, mock_session, fields):
    respx.get(f"{mock_session.rest_url}/meta/Candidate").mock(return_value=_json_response({"fields": fields}))
    report = SchemaDiscoverer(MetaDiscovery(BullhornClient(mock_auth)), load_canonical_catalog(), load_bullhorn_catalog()).discover(
        entities=["candidate"]
    )
    return report, build_draft_profile(report, tenant="t")


class TestR11dDraftPath:
    @respx.mock
    def test_corpus_draft_round_trips(self, mock_auth, mock_session, tmp_path):
        report, draft = _discover_draft(mock_auth, mock_session, _corpus_meta_fields())
        out = tmp_path / "draft.yaml"
        draft.save(out)
        assert MappingProfile.load(out) == draft
        unmapped = {u.field: u for u in draft.entity("candidate").unmapped_bullhorn_fields}
        assert unmapped["customText90"].label == "Region\u0085Code"
        assert unmapped["customText90"].options == (("A", "x\u0085y"),)
        # R-11f on the corpus report
        json.dumps(report.to_dict(), allow_nan=False)


class TestR11fStrictJson:
    @respx.mock
    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
    @pytest.mark.parametrize("slot", ["value", "label"])
    def test_non_finite_options_are_dropped_with_warning(self, mock_auth, mock_session, token, slot):
        option = '{"value":%s}' % token if slot == "value" else '{"value":"A","label":%s}' % token
        raw = '{"fields":[{"name":"id"},{"name":"customText1","label":"X","options":[%s]}]}' % option
        respx.get(f"{mock_session.rest_url}/meta/Candidate").mock(
            return_value=httpx.Response(200, content=raw.encode(), headers={"content-type": "application/json"})
        )
        report = SchemaDiscoverer(MetaDiscovery(BullhornClient(mock_auth)), load_canonical_catalog(), load_bullhorn_catalog()).discover(
            entities=["candidate"]
        )
        json.dumps(report.to_dict(), allow_nan=False)
        entity = report.entities[0]
        field = next(u for u in entity.custom_unmapped if u.field == "customText1")
        assert field.options is None
        assert any("non-finite" in w for w in entity.warnings)


class TestR11gReviewerRepros:
    def test_tenant_nel(self, tmp_path):
        p = MappingProfile.from_dict({"version": 1, "tenant": "acme\x85corp"})
        p.save(tmp_path / "p.yaml")
        assert MappingProfile.load(tmp_path / "p.yaml") == p

    def test_template_nel(self, tmp_path):
        p = MappingProfile.from_dict({"version": 1, "entities": {"candidate": {"custom": {"t": "{firstName}\x85{lastName}"}}}})
        p.save(tmp_path / "p.yaml")
        q = MappingProfile.load(tmp_path / "p.yaml")
        assert q == p
        assert _rendered(q, "custom_template") == _rendered(p, "custom_template") == "A\x85B"

    def test_field_and_option_label_nel(self, tmp_path):
        p = MappingProfile.from_dict(
            {
                "version": 1,
                "entities": {
                    "candidate": {
                        "unmapped_bullhorn_fields": [
                            {"field": "customText1", "label": "Region\u0085Code", "options": [{"value": "A", "label": "x\u0085y"}]}
                        ]
                    }
                },
            }
        )
        p.save(tmp_path / "p.yaml")
        assert MappingProfile.load(tmp_path / "p.yaml") == p


# ---------------------------------------------------------------------- #
# R-11e: the save self-check is live
# ---------------------------------------------------------------------- #


class TestR11eSelfCheck:
    def test_lossy_dump_is_refused_and_nothing_written(self, tmp_path, monkeypatch):
        real_dump = mapping_profile_module.yaml.safe_dump

        def lossy_dump(data, **kwargs):
            kwargs["allow_unicode"] = True
            return real_dump(data, **kwargs)

        monkeypatch.setattr(mapping_profile_module.yaml, "safe_dump", lossy_dump)
        target = tmp_path / "out" / "p.yaml"
        p = MappingProfile.from_dict({"version": 1, "tenant": "acme\x85corp"})
        with pytest.raises(ProfileError, match="cannot be serialized losslessly"):
            p.save(target)
        assert not target.exists()
        assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []

    def test_to_yaml_text_is_ascii(self):
        p = MappingProfile.from_dict({"version": 1, "tenant": "café \U0001f600 \x85 \ud800"})
        text = p.to_yaml_text()
        assert text.isascii()
