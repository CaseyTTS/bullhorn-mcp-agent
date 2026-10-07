"""F-9 regression tests: every YAML parse failure becomes only the documented error (R-9a..R-9c)."""

import pytest

from bullhorn_mcp.schema import yaml_safe
from bullhorn_mcp.schema.bullhorn_catalog import BullhornCatalog
from bullhorn_mcp.schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from bullhorn_mcp.schema.errors import CatalogError, ProfileError
from bullhorn_mcp.schema.mapping_profile import MappingProfile

BOUND = 10_000

TAGS = ["bool", "int", "float", "timestamp", "binary", "null", "str", "seq", "map", "set", "omap", "pairs"]
COLLECTION_TAGS = {"seq", "map", "set", "omap", "pairs"}
HOSTILE = [
    "", "_", "-", ":", "0x", "0b", "0o", ".", "maybe", "abc", "9" * 5000, "1e999999", "é中🙂",
    "[1, [2]]", "{a: 1}", "!!python/object:os.system",
]
HOSTILE_IDS = [
    "empty", "underscore", "dash", "colon", "0x", "0b", "0o", "dot", "maybe", "abc", "5000_digits", "1e999999",
    "unicode", "nested_list_text", "map_text", "python_tag_text",
]

PROFILE_POSITIONS = {
    "version": "version: {X}\n",
    "tenant": "version: 1\ntenant: {X}\n",
    "unmapped_label": (
        "version: 1\nentities:\n  candidate:\n    unmapped_bullhorn_fields:\n"
        "      - field: customText1\n        label: {X}\n"
    ),
}
CATALOG_POSITIONS = {
    "version": "version: {X}\nentities:\n  widget:\n    fields:\n      id: {{type: id}}\n",
    "entity_description": "version: 1\nentities:\n  widget:\n    description: {X}\n    fields:\n      id: {{type: id}}\n",
    "field_type": "version: 1\nentities:\n  widget:\n    fields:\n      id:\n        type: {X}\n",
}


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _tagged(tag: str, value: str) -> str:
    """Emit ``value`` under an explicit core tag: quoted scalar, or a flow collection for collection tags."""
    q = _quoted(value)
    if tag not in COLLECTION_TAGS:
        return f"!!{tag} {q}"
    return {
        "seq": f"!!seq [{q}, [{q}]]",
        "map": f"!!map {{{q}: {q}}}",
        "set": f"!!set {{{q}}}",
        "omap": f"!!omap [{{{q}: 1}}, {q}]",
        "pairs": f"!!pairs [{{{q}: 1}}, [{q}]]",
    }[tag]


def _load(loader: str, text: str, tmp_path):
    if loader == "profile":
        p = tmp_path / "p.yaml"
        p.write_text(text, encoding="utf-8")
        return MappingProfile.load(p)
    if loader == "canonical":
        return CanonicalCatalog.from_yaml_text(text)
    return BullhornCatalog.from_yaml_text(text, load_canonical_catalog())


def _assert_only_documented(loader: str, text: str, tmp_path):
    expected = ProfileError if loader == "profile" else CatalogError
    try:
        _load(loader, text, tmp_path)
    except expected as exc:  # any other exception type propagates and fails the test
        assert type(exc) is expected
        assert len(str(exc)) < BOUND


CASES = [
    pytest.param(loader, pos_name, template, tag, value, id=f"{loader}-{pos_name}-{tag}-{vid}")
    for loader, positions in (("profile", PROFILE_POSITIONS), ("canonical", CATALOG_POSITIONS), ("bullhorn", CATALOG_POSITIONS))
    for pos_name, template in positions.items()
    for tag in TAGS
    for value, vid in zip(HOSTILE, HOSTILE_IDS)
]


@pytest.mark.parametrize("loader,position,template,tag,value", CASES)
def test_r9a_generative_cross_product(tmp_path, loader, position, template, tag, value):
    _assert_only_documented(loader, template.format(X=_tagged(tag, value)), tmp_path)


def test_r9a_case_count():
    assert len(CASES) >= 12 * 16 * 2 * 3


REVIEWER_REPROS = ["!!bool maybe", "!!bool ''", "!!timestamp abc", "!!int ''", "!!int _", "!!int '-'", "!!float ''", "!!float _"]


@pytest.mark.parametrize("loader", ["profile", "canonical", "bullhorn"])
@pytest.mark.parametrize("repro", REVIEWER_REPROS)
def test_r9b_reviewer_repros(tmp_path, loader, repro):
    text = f"version: 1\ntenant: {repro}\n"
    expected = ProfileError if loader == "profile" else CatalogError
    with pytest.raises(expected) as exc_info:
        _load(loader, text, tmp_path)
    assert type(exc_info.value) is expected
    assert len(str(exc_info.value)) < BOUND
    if repro in ("!!bool maybe", "!!timestamp abc", "!!int _"):
        assert any(e.startswith("YAML parse error (") for e in exc_info.value.errors)


@pytest.mark.parametrize("loader", ["profile", "canonical", "bullhorn"])
def test_r9c_memory_error_propagates(tmp_path, monkeypatch, loader):
    def boom(text):
        raise MemoryError("simulated")

    monkeypatch.setattr(yaml_safe.yaml, "safe_load", boom)
    with pytest.raises(MemoryError):
        _load(loader, "version: 1\n", tmp_path)


def test_parse_errors_tuple_is_explicit():
    assert Exception not in yaml_safe.YAML_PARSE_ERRORS
    assert BaseException not in yaml_safe.YAML_PARSE_ERRORS
    assert not any(issubclass(MemoryError, t) for t in yaml_safe.YAML_PARSE_ERRORS)
    assert not any(issubclass(KeyboardInterrupt, t) for t in yaml_safe.YAML_PARSE_ERRORS)
