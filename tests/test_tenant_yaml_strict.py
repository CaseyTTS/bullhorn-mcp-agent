"""NB-9 / AC-7: strict YAML parsing (duplicate keys at any depth; hostile corpus -> documented error only)."""

import pytest

from bullhorn_mcp.schema.yaml_safe import YamlParseFailure
from bullhorn_mcp.tenant import yaml_strict

BOUND = 2_000


class TestDuplicateKeys:
    @pytest.mark.parametrize(
        "text",
        [
            "a: 1\na: 2\n",
            "outer:\n  inner: 1\n  inner: 2\n",
            "list:\n  - {x: 1, x: 2}\n",
            "deep:\n  - - - {k: 1}\n      - {k: 1, k: 2}\n",
            "1: a\n0x1: b\n",  # same int via two spellings
            "true: a\nTrue: b\n",
            "1: a\n1.0: b\n",  # dict would silently merge these
            "'a': 1\n\"a\": 2\n",
            "? a\n: 1\n? a\n: 2\n",
            "x: &anchor {k: 1, k: 2}\n",
        ],
        ids=["top", "nested", "in_list", "deep", "int_spellings", "bool_spellings", "int_float", "quoted", "explicit", "anchored"],
    )
    def test_rejected(self, text):
        with pytest.raises(YamlParseFailure) as info:
            yaml_strict.parse(text)
        assert "duplicate mapping key" in str(info.value)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("a: 1\nb: 2\n", {"a": 1, "b": 2}),
            ("a: {x: 1}\nb: {x: 1}\n", {"a": {"x": 1}, "b": {"x": 1}}),
            ("'1': a\n1: b\n", {"1": "a", 1: "b"}),
            ("base: &b {x: 1}\nother:\n  <<: *b\n  y: 2\n", {"base": {"x": 1}, "other": {"x": 1, "y": 2}}),
            ("x: &a [1, 2]\ny: *a\n", {"x": [1, 2], "y": [1, 2]}),
            ("", None),
        ],
    )
    def test_accepted(self, text, expected):
        assert yaml_strict.parse(text) == expected


HOSTILE = [
    "!!python/object/apply:os.system ['echo hi']",
    "!!python/name:os.system",
    "a: !!python/object:builtins.dict {}",
    "[" * 5000 + "]" * 5000,
    "{" * 5000 + "}" * 5000,
    "- " * 3000 + "x",
    "a: &a [*a]",
    "a: *undefined",
    "a: b: c",
    "\t- x",
    "%YAML 9.9\n---\na: 1",
    "--- 1\n--- 2\n",
    "x: !!binary '%%%'",
    "x: !!timestamp 'not a time'",
    "x: !!int 'abc'",
    "x: !!float 'abc'",
    "x: !!set {a: 1, a: 2}",
    "x: !!omap [{a: 1}, b]",
    "x: !!pairs [1]",
    "? [a, b]\n: 1",
    "? {a: 1}\n: 1",
    "x: " + "9" * 10_000,
    "﻿﻿a: 1",
    "a: '\\ud800'",
    'a: "\\x"',
    "a: !<tag:evil.example,2026:x> 1",
    "&a a: &a b",
    "x: !!str [1]",
    "a: |\n bad\n  indent\n wrong: 1",
]


class TestHostileCorpus:
    def test_corpus_size(self):
        assert len(HOSTILE) >= 20

    @pytest.mark.parametrize("text", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
    def test_only_documented_error(self, text):
        try:
            yaml_strict.parse(text)
        except YamlParseFailure as exc:  # anything else propagates and fails the test
            assert type(exc) is YamlParseFailure
            assert len(str(exc)) < BOUND

    @pytest.mark.parametrize("value", [None, b"a: 1", 5, ["a: 1"]], ids=repr)
    def test_non_text(self, value):
        with pytest.raises(YamlParseFailure):
            yaml_strict.parse(value)

    def test_oversized(self):
        with pytest.raises(YamlParseFailure):
            yaml_strict.parse("a: '" + "x" * (yaml_strict.MAX_YAML_CHARS + 1) + "'")
