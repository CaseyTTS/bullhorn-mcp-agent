"""F-12 regression tests: identifier character-set consistency and Unicode-wide masking (R-12a..R-12e)."""

import functools
import json
import re
import unicodedata
from unittest.mock import Mock, PropertyMock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.bullhorn.meta import MetaDiscovery
from bullhorn_mcp.schema import bullhorn_catalog as bullhorn_catalog_module
from bullhorn_mcp.schema import canonical_catalog as canonical_catalog_module
from bullhorn_mcp.schema import discovery as discovery_module
from bullhorn_mcp.schema import errors as errors_module
from bullhorn_mcp.schema.bullhorn_catalog import RAW_NAME_RE, load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.discovery import SchemaDiscoverer, build_draft_profile, redact_sample
from bullhorn_mcp.schema.mapping_profile import MappingProfile
from tests._unicode_corpus import corpus

BASES = [
    "customText{}", "customTextBlock{}", "customInt{}", "customFloat{}", "customDate{}",
    "customObject{}s", "customEncryptedText{}", "customBillRate{}", "customPayRate{}",
]


@functools.lru_cache(maxsize=1)
def unicode_digit_ones() -> tuple[str, ...]:
    """Every non-ASCII Nd code point whose decimal value is 1: one per digit block."""
    return tuple(
        chr(cp) for cp in range(0x80, 0x110000) if unicodedata.category(chr(cp)) == "Nd" and unicodedata.decimal(chr(cp)) == 1
    )


NFKC_ONES = ["¹", "①", "⑴", "１", "\U0001d7cf", "₁"]  # superscript, circled, ..., subscript one


@functools.lru_cache(maxsize=1)
def hostile_names() -> tuple[str, ...]:
    names: dict[str, None] = {}
    for base in BASES:
        valid = base.format("1")
        prefix, rest = "custom", valid[len("custom"):]
        for d in unicode_digit_ones():
            names.setdefault(base.format(d), None)
        for ch in corpus():
            names.setdefault(valid + ch, None)
            names.setdefault(ch + valid, None)
            names.setdefault(prefix + ch + rest, None)
        for one in NFKC_ONES:
            n = base.format(one)
            if unicodedata.normalize("NFKC", n) != n:
                names.setdefault(n, None)
    for n in [
        "сustomText1",  # Cyrillic es
        "custоmText1",  # Cyrillic o
        "customΤext1",  # Greek Tau
        "customTеxt1",  # Cyrillic ie
        "ｃｕｓｔｏｍＴｅｘｔ１",  # fullwidth customText1
        "customText١", "customText１", "customObject٢s", "customInt\U0001d7d9",
    ]:
        names.setdefault(n, None)
    return tuple(names)


def _expected_custom(n: str) -> bool:
    catalog = load_bullhorn_catalog()
    return n.isascii() and RAW_NAME_RE.fullmatch(n) is not None and any(
        re.fullmatch(p, n, re.ASCII) for p in catalog.custom_field_patterns
    )


@pytest.fixture
def mock_auth(mock_session):
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


def _json_response(body) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body).encode("ascii"), headers={"content-type": "application/json"})


# ---------------------------------------------------------------------- #
# R-12a / R-12b
# ---------------------------------------------------------------------- #


class TestR12Classification:
    def test_r12a_corpus_shape(self):
        assert len(unicode_digit_ones()) >= 60
        names = hostile_names()
        assert "customText١" in names and "сustomText1" in names
        assert sum(1 for n in names if RAW_NAME_RE.fullmatch(n) is None) > 3_000

    def test_r12b_is_custom_field_property(self):
        catalog = load_bullhorn_catalog()
        mismatches = [n for n in hostile_names() if catalog.is_custom_field(n) != _expected_custom(n)]
        assert mismatches == []
        for n in hostile_names():
            if RAW_NAME_RE.fullmatch(n) is None:
                assert catalog.is_custom_field(n) is False

    def test_every_identifier_regex_is_ascii(self):
        catalog = load_bullhorn_catalog()
        for compiled in catalog._custom_res + catalog._sensitive_res:
            assert compiled.flags & re.ASCII, compiled.pattern
        for compiled in (
            bullhorn_catalog_module.RAW_NAME_RE,
            bullhorn_catalog_module.BULLHORN_ENTITY_RE,
            bullhorn_catalog_module.TEMPLATE_RE,
            bullhorn_catalog_module.PATTERN_GRAMMAR_RE,
            canonical_catalog_module.KEY_RE,
            errors_module._PLAIN_SEGMENT,
        ):
            assert compiled.flags & re.ASCII, compiled.pattern

    def test_masking_regexes_are_unicode_wide(self):
        for compiled in (discovery_module._DIGIT_RE, discovery_module._EMAIL_RE):
            assert not compiled.flags & re.ASCII


# ---------------------------------------------------------------------- #
# R-12c / R-12d: end to end
# ---------------------------------------------------------------------- #


def _meta_body(names):
    fields = [{"name": "id"}, {"name": "customText1"}, {"name": "customText3", "label": "Clearance"}]
    fields += [{"name": n, "label": f"L{i}"} for i, n in enumerate(names)]
    return {"fields": fields}


class TestR12EndToEnd:
    @respx.mock
    def test_r12c_hostile_names_never_reach_the_draft(self, mock_auth, mock_session, tmp_path):
        names = list(hostile_names())
        invalid = {n for n in names if RAW_NAME_RE.fullmatch(n) is None}
        catalog = load_bullhorn_catalog()
        for bh in catalog.entities:
            respx.get(f"{mock_session.rest_url}/meta/{bh}").mock(return_value=_json_response(_meta_body(names)))
        requested: list[str] = []

        def sample_source(entity, fields):
            requested.extend(fields)
            return {}

        report = SchemaDiscoverer(MetaDiscovery(BullhornClient(mock_auth)), load_canonical_catalog(), catalog).discover(
            include_sample_values=True, sample_source=sample_source
        )
        json.dumps(report.to_dict(), allow_nan=False)
        assert requested and all(RAW_NAME_RE.fullmatch(n) for n in requested)
        for entity in report.entities:
            assert entity.error is None
            other = {o.field for o in entity.other_unrecognized}
            assert invalid <= other
            elsewhere = (
                set(entity.standard_present)
                | {m.field for m in entity.custom_mapped}
                | {u.field for u in entity.custom_unmapped}
            )
            assert not (invalid & elsewhere)
            name_warnings = [w for w in entity.warnings if "not valid raw field names" in w]
            assert len(name_warnings) == 1
            assert len(name_warnings[0]) < 2_000

        draft = build_draft_profile(report, tenant="t")
        out = tmp_path / "draft.yaml"
        draft.save(out)
        assert MappingProfile.load(out) == draft
        for canon in (e.canonical_entity for e in report.entities):
            fields = {u.field for u in draft.entity(canon).unmapped_bullhorn_fields}
            assert {"customText1", "customText3"} <= fields
            assert not (fields & invalid)

    @respx.mock
    def test_r12d_reviewer_repro(self, mock_auth, mock_session, tmp_path):
        repro = ["customText١", "customText１", "customObject٢s", "customInt\U0001d7d9"]
        catalog = load_bullhorn_catalog()
        for bh in catalog.entities:
            respx.get(f"{mock_session.rest_url}/meta/{bh}").mock(return_value=_json_response(_meta_body(repro)))
        report = SchemaDiscoverer(MetaDiscovery(BullhornClient(mock_auth)), load_canonical_catalog(), catalog).discover()
        draft = build_draft_profile(report, tenant="t")
        assert set(draft.entities) == {e.canonical_entity for e in report.entities}
        assert len(draft.entities) == len(catalog.entities)
        draft.save(tmp_path / "d.yaml")
        assert MappingProfile.load(tmp_path / "d.yaml") == draft
        for ep in draft.entities.values():
            assert {u.field for u in ep.unmapped_bullhorn_fields} == {"customText1", "customText3"}


# ---------------------------------------------------------------------- #
# R-12e: masking stays Unicode-wide
# ---------------------------------------------------------------------- #


class TestR12eMasking:
    @pytest.mark.parametrize("raw", ["١٢٣٤٥٦٧", "１２３４５", "call ٠٥٥١٢٣٤٥٦٧"])
    def test_non_ascii_digits_are_masked(self, raw):
        out = redact_sample(raw)
        digits = {c for c in raw if unicodedata.category(c) == "Nd"}
        assert not any(c in out for c in digits)
        assert out != raw

    def test_every_corpus_character_with_digits_is_changed(self):
        for ch in corpus():
            for v in (ch + "12345", "a" + ch + "١٢٣٤", "12" + ch + "34"):
                assert redact_sample(v) != v, repr(v)
