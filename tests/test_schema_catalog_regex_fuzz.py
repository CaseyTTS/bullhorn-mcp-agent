"""F-13 regression tests: catalog patterns are restricted to a fixed, linear-time grammar (R-13a..R-13d)."""

import random
import re
import time
from importlib import resources

import pytest
import yaml

from bullhorn_mcp.schema.bullhorn_catalog import (
    MAX_PATTERNS,
    PATTERN_GRAMMAR_RE,
    BullhornCatalog,
    compile_catalog_pattern,
    load_bullhorn_catalog,
)
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.errors import CatalogError

BOUND = 10_000
LISTS = ["custom_field_patterns", "sensitive_field_patterns"]


def _shipped_data() -> dict:
    text = resources.files("bullhorn_mcp.mappings").joinpath("bullhorn_standard_fields.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _catalog_text_with(list_name: str, patterns) -> str:
    data = _shipped_data()
    data[list_name] = patterns
    return yaml.safe_dump(data, sort_keys=False)


HOSTILE_PATTERNS = [
    pytest.param("a{4294967296}", id="repro_overflow"),
    pytest.param("(" * 2000 + ")" * 2000, id="repro_recursion"),
    pytest.param("(a+)+$", id="redos_nested_plus"),
    pytest.param("(a|a)*b", id="redos_alternation"),
    pytest.param("(.*)*x", id="redos_dotstar"),
    pytest.param("customText\\w+", id="word_class"),
    pytest.param("customText\\d*", id="digit_star"),
    pytest.param("customText[0-9]+", id="char_class"),
    pytest.param("(?i)ssn", id="inline_flag"),
    pytest.param(".*", id="dotstar"),
    pytest.param("", id="empty"),
    pytest.param("\\", id="backslash"),
    pytest.param("[", id="open_bracket"),
    pytest.param("a" * 65, id="too_long"),
    pytest.param("customText١", id="unicode_literal"),
    pytest.param(5, id="int"),
    pytest.param(["x"], id="list"),
    pytest.param(None, id="null"),
]


@pytest.mark.parametrize("list_name", LISTS)
@pytest.mark.parametrize("pattern", HOSTILE_PATTERNS)
def test_r13a_hostile_patterns_rejected(list_name, pattern):
    text = _catalog_text_with(list_name, [pattern])
    start = time.perf_counter()
    with pytest.raises(CatalogError) as exc_info:
        BullhornCatalog.from_yaml_text(text, load_canonical_catalog())
    assert time.perf_counter() - start < 1.0
    assert type(exc_info.value) is CatalogError
    assert len(str(exc_info.value)) < BOUND
    assert any(list_name in e for e in exc_info.value.errors)


@pytest.mark.parametrize("pattern", ["(a+)+$", "customText\\w+", "a{4294967296}"])
def test_construction_path_uses_the_same_check(pattern):
    with pytest.raises(CatalogError):
        BullhornCatalog(1, {}, (pattern,), ())
    with pytest.raises(CatalogError):
        BullhornCatalog(1, {}, (), (pattern,))


@pytest.mark.parametrize("list_name", LISTS)
def test_r13b_list_cap(list_name):
    patterns = [f"field{i}" for i in range(MAX_PATTERNS + 1)]
    with pytest.raises(CatalogError, match="at most 64 patterns"):
        BullhornCatalog.from_yaml_text(_catalog_text_with(list_name, patterns), load_canonical_catalog())
    ok = _catalog_text_with(list_name, patterns[:MAX_PATTERNS])
    BullhornCatalog.from_yaml_text(ok, load_canonical_catalog())


def _random_identifier(rng: random.Random, first: bool) -> str:
    head = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    tail = head + "0123456789"
    length = rng.randint(1 if first else 0, 12)
    if length == 0:
        return ""
    return rng.choice(head if first else tail) + "".join(rng.choice(tail) for _ in range(length - 1))


def _generated_patterns(count: int = 500):
    rng = random.Random(20261006)
    out = []
    for _ in range(count):
        prefix = _random_identifier(rng, first=True)
        has_digits = rng.random() < 0.6
        suffix = _random_identifier(rng, first=False) if has_digits else ""
        # A suffix starting with a digit would be absorbed by \d+; keep generated names unambiguous.
        suffix = suffix.lstrip("0123456789")
        pattern = prefix + ("\\d+" + suffix if has_digits else "")
        name = prefix + ("123" + suffix if has_digits else "")
        out.append((pattern, name, has_digits, prefix, suffix))
    return out


def test_r13c_grammar_soundness_generative():
    generated = _generated_patterns()
    assert len(generated) == 500
    for pattern, name, has_digits, prefix, suffix in generated:
        assert len(pattern) > 64 or PATTERN_GRAMMAR_RE.fullmatch(pattern), pattern
        if len(pattern) > 64:
            continue
        compiled, error = compile_catalog_pattern(pattern)
        assert error is None and compiled is not None, (pattern, error)
        start = time.perf_counter()
        assert compiled.fullmatch(name), (pattern, name)
        assert time.perf_counter() - start < 0.010
        if has_digits:
            assert compiled.fullmatch(prefix + "١٢٣" + suffix) is None
        else:
            assert compiled.fullmatch(name + "١") is None


def test_r13d_shipped_catalog_patterns_pass_grammar():
    data = _shipped_data()
    catalog = load_bullhorn_catalog()
    for list_name in LISTS:
        for pattern in data[list_name]:
            assert PATTERN_GRAMMAR_RE.fullmatch(pattern), pattern
            compiled, error = compile_catalog_pattern(pattern)
            assert error is None and compiled is not None and compiled.flags & re.ASCII
    assert list(catalog.custom_field_patterns) == data["custom_field_patterns"]
    assert list(catalog.sensitive_field_patterns) == data["sensitive_field_patterns"]
