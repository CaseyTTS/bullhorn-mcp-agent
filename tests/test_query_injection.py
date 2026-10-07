"""Phase 5C injection corpus (AC-4, SC-8, SR-23).

For every hostile input, one of two outcomes must hold:

- the request is refused (``rejected_validation`` / ``unsupported``) with **zero** HTTP calls; or
- the rendered ``where`` tokenizes (reference JPQL tokenizer below) to the same token structure as the
  benign template, with each caller value confined to one literal token, and the other params are exact.
"""

from __future__ import annotations

import math
import re

import httpx
import pytest
import respx

from bullhorn_mcp.reads import records as R

from ._phase5c_helpers import FULL_CONFIG, context, params, tenant_store
from ._tenant_helpers import REST_URL
from ._unicode_corpus import corpus

# ---------------------------------------------------------------------- #
# Reference tokenizer for the verified /query where syntax (HV-Q2)
# ---------------------------------------------------------------------- #

_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)|(?P<str>'[^']*')|(?P<num>-?\d+)|(?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"""
    r"""|(?P<op>>=|<=|<>|=|<|>)|(?P<punct>[(),])|(?P<err>.)""",
    re.DOTALL,
)
KEYWORDS = {"AND", "OR", "NOT", "IN", "IS", "NULL"}
BOOLEANS = {"true", "false"}


def tokenize(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "ws":
            continue
        value = m.group()
        if kind == "word" and value in KEYWORDS:
            kind = "kw"
        elif kind == "word" and value in BOOLEANS:
            kind = "bool"
        out.append((kind, value))
    return out


def structure(tokens):
    """Literal tokens lose their value; everything else must match exactly."""
    return [(k, "?" if k in ("str", "num", "bool") else v) for k, v in tokens]


def test_tokenizer_sanity():
    assert tokenize("(isDeleted = false OR isDeleted IS NULL) AND firstName = 'a b' AND owner.id IN (1, 2)") == [
        ("punct", "("),
        ("word", "isDeleted"),
        ("op", "="),
        ("bool", "false"),
        ("kw", "OR"),
        ("word", "isDeleted"),
        ("kw", "IS"),
        ("kw", "NULL"),
        ("punct", ")"),
        ("kw", "AND"),
        ("word", "firstName"),
        ("op", "="),
        ("str", "'a b'"),
        ("kw", "AND"),
        ("word", "owner.id"),
        ("kw", "IN"),
        ("punct", "("),
        ("num", "1"),
        ("punct", ","),
        ("num", "2"),
        ("punct", ")"),
    ]
    assert structure(tokenize("x = 'O'Brien'")) != structure(tokenize("x = 'OBrien'"))  # a stray quote changes structure


# ---------------------------------------------------------------------- #
# Corpus
# ---------------------------------------------------------------------- #


class S(str):
    pass


class I(int):  # noqa: E742
    pass


class B(int):
    pass


LUCENE = list('+-&|!(){}[]^"~*?:\\/') + ["&&", "||"]
SQL = [
    "'",
    '"',
    "\\",
    ";",
    "--",
    "/* */",
    " OR 1=1",
    "' OR '1'='1",
    "x' OR 'a'='a",
    "1; DROP TABLE x",
    "') OR ('1'='1",
    "admin'--",
    "a' AND isDeleted = true AND 'x'='x",
    "%",
    "_",
    "%_%",
    "a%",
    "_a",
    "\\'",
    "''",
    "' '",
    "a''b",
]
UNICODE = ["‘x’", "“x”", "ʼx", "＇x", "аdmin", "Adа", "‮x", "x​y", "﻿x", "x\u0000y", "x\ry", "x\ny", "x\r\n OR 1=1", "é", "Café", "\U0001f600"]
NAMES = ["isDeleted", "owner.id", "firstName", "__class__", "__dict__", "Candidate", "lastName", "id IS NOT NULL", "1=1"]
BIG = ["x" * 10_000, "a" * 200, "a" * 201, "a " * 100, " " * 5]
BENIGN_LIKE = ["Ada", "Ada Lovelace", "a.b@c-d.e", "under_score", "123", "-1", "a  b"]
TYPED = [
    S("Ada"),
    S("x' OR 1=1"),
    I(5),
    B(1),
    True,
    False,
    1.5,
    math.nan,
    math.inf,
    -math.inf,
    None,
    0,
    -1,
    2**63,
    2**64,
    [],
    ["Ada"],
    [["Ada"]],
    {"x": 1},
    {"field": "first_name"},
    b"Ada",
    ("Ada",),
    object(),
]

STRING_VALUES = [f"a{ch}b" for ch in LUCENE] + LUCENE + SQL + UNICODE + NAMES + BIG + BENIGN_LIKE + [f"a{ch}b" for ch in corpus()]
ALL_VALUES = STRING_VALUES + TYPED


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return tenant_store(tmp_path_factory.mktemp("inj"), FULL_CONFIG)


# filter template -> (entity, bullhorn entity, filter builder, benign value)
TEMPLATES = {
    "str_eq": ("candidate", "Candidate", lambda v: [{"field": "first_name", "op": "eq", "value": v}], "BENIGN"),
    "str_in": ("candidate", "Candidate", lambda v: [{"field": "last_name", "op": "in", "value": [v, "Other"]}], "BENIGN"),
    "id_eq": ("candidate", "Candidate", lambda v: [{"field": "owner_id", "op": "eq", "value": v}], 7),
    "num_gt": ("job", "JobOrder", lambda v: [{"field": "num_openings", "op": "gt", "value": v}], 3),
    "date_gte": ("candidate", "Candidate", lambda v: [{"field": "date_added", "op": "gte", "value": v}], "2026-01-01"),
    "bool_eq": ("job", "JobOrder", lambda v: [{"field": "is_open", "op": "eq", "value": v}], True),
}


def _run(store, entity, bh, filters):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{REST_URL}/query/{bh}").mock(return_value=httpx.Response(200, json={"data": []}))
        out = R.find_records(context(store), {"entity": entity, "filters": filters})
        calls = list(router.calls)
    return out, calls


_BENIGN: dict[str, dict[str, str]] = {}


def _benign(store, name):
    if name not in _BENIGN:
        entity, bh, build, value = TEMPLATES[name]
        out, calls = _run(store, entity, bh, build(value))
        assert out["status"] == "ok" and len(calls) == 1
        _BENIGN[name] = params(calls[0])
    return _BENIGN[name]


def _check(store, name, value):
    entity, bh, build, _ = TEMPLATES[name]
    out, calls = _run(store, entity, bh, build(value))
    if out["status"] != "ok":
        assert out["status"] in ("rejected_validation", "unsupported"), out
        assert calls == []
        return "refused"
    assert len(calls) == 1
    got, benign = params(calls[0]), _benign(store, name)
    assert set(got) == {"where", "fields", "count", "start"}
    assert {k: got[k] for k in ("fields", "count", "start")} == {k: benign[k] for k in ("fields", "count", "start")}
    actual, template = tokenize(got["where"]), tokenize(benign["where"])
    assert "err" not in {k for k, _ in actual}
    assert structure(actual) == structure(template)
    if isinstance(value, str) and name in ("str_eq", "str_in"):
        assert ("str", f"'{value}'") in actual  # the value is confined to one literal token
    return "sent"


@pytest.mark.parametrize("value", ALL_VALUES, ids=lambda v: repr(v)[:30])
@pytest.mark.parametrize("template", ["str_eq", "str_in"])
def test_string_filters(store, template, value):
    _check(store, template, value)


@pytest.mark.parametrize("value", ALL_VALUES[::4] + TYPED, ids=lambda v: repr(v)[:30])
@pytest.mark.parametrize("template", ["id_eq", "num_gt", "date_gte", "bool_eq"])
def test_typed_filters(store, template, value):
    _check(store, template, value)


def test_corpus_size():
    cases = len(ALL_VALUES) * 2 + len(ALL_VALUES[::4] + TYPED) * 4
    assert len(ALL_VALUES) >= 200 and cases >= 800


def test_apostrophe_and_percent_are_refused(store):
    assert _check(store, "str_eq", "O'Brien") == "refused"
    assert _check(store, "str_eq", "100%") == "refused"
    assert _check(store, "str_eq", "Ada") == "sent"


@pytest.mark.parametrize(
    "name", NAMES + ["first_name; --", "first_name OR 1=1", S("first_name"), 1, None, ["first_name"]], ids=lambda v: repr(v)[:30]
)
def test_hostile_field_names_rejected(store, name):
    out, calls = _run(store, "candidate", "Candidate", [{"field": name, "op": "eq", "value": "Ada"}])
    assert out["status"] == "rejected_validation" and calls == []
    out, calls = _run(store, "candidate", "Candidate", [{"field": "first_name", "op": name, "value": "Ada"}])
    assert out["status"] == "rejected_validation" and calls == []


@pytest.mark.parametrize("entity", ["Candidate", "candidate; DROP", "CorporateUser", "__class__", "JobOrder", S("candidate")])
def test_hostile_entities_rejected(store, entity):
    with respx.mock(assert_all_called=False) as router:
        out = R.find_records(context(store), {"entity": entity})
        assert out["status"] == "rejected_validation" and not router.calls


@pytest.mark.parametrize(
    "filters",
    [
        [[]],
        [{"field": "first_name", "op": "eq", "value": "Ada", "where": "1=1"}],
        ["first_name = 'x'"],
        {"field": "first_name"},
        "first_name = 'Ada'",
        [{"field": "first_name", "op": "eq", "value": {"$ne": 1}}],
    ],
    ids=lambda v: repr(v)[:30],
)
def test_hostile_structures_rejected(store, filters):
    out, calls = _run(store, "candidate", "Candidate", filters)
    assert out["status"] == "rejected_validation" and calls == []
