"""Phase 5C query builder, renderer and query-support loader (§3.3, D-5C-3, D-5C-4, D-5C-5; AC-5, AC-6, AC-8)."""

from __future__ import annotations

import math

import pytest

from bullhorn_mcp.bullhorn import query_syntax as qs
from bullhorn_mcp.bullhorn.query_syntax import AnyOf, Literal, Predicate
from bullhorn_mcp.reads import support as sup
from bullhorn_mcp.schema import query_builder as qb
from bullhorn_mcp.schema.bullhorn_catalog import NestedField, RawField, TemplateField, load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog

CANDIDATE_FIELDS = tuple(f.name for f in load_canonical_catalog().entity("candidate").fields)
CANDIDATE_TYPES = {f.name: f.type for f in load_canonical_catalog().entity("candidate").fields}


# ---------------------------------------------------------------------- #
# Renderer: the single escaping function (§3.3 step 4)
# ---------------------------------------------------------------------- #


class TestLiteral:
    @pytest.mark.parametrize(
        "lit, text",
        [
            (Literal("int", 5), "5"),
            (Literal("int", -3), "-3"),
            (Literal("ts", 1_790_000_000_000), "1790000000000"),
            (Literal("bool", True), "true"),
            (Literal("bool", False), "false"),
            (Literal("str", "Ada Lovelace"), "'Ada Lovelace'"),
            (Literal("str", "a.b_c-d@e.f"), "'a.b_c-d@e.f'"),
        ],
    )
    def test_verified_forms(self, lit, text):
        assert qs.literal(lit) == text

    @pytest.mark.parametrize(
        "lit",
        [
            Literal("int", True),  # bool is not an int literal
            Literal("int", 1.0),
            Literal("int", 2**63),
            Literal("ts", "1790000000000"),
            Literal("bool", 1),
            Literal("str", "O'Brien"),
            Literal("str", "x" * 201),
            Literal("str", ""),
            Literal("str", "café"),
            Literal("str", 5),
        ],
    )
    def test_unverified_forms_rejected(self, lit):
        with pytest.raises(qs.UnsupportedValue):
            qs.literal(lit)

    def test_unknown_kind(self):
        with pytest.raises(qs.RenderError):
            qs.literal(Literal("float", 1.5))

    def test_str_subclass_rejected(self):
        class S(str):
            pass

        with pytest.raises(qs.UnsupportedValue):
            qs.literal(Literal("str", S("abc")))


class TestRenderWhere:
    def test_operators(self):
        clauses = [
            Predicate("id", "eq", (Literal("int", 1),)),
            Predicate("salary", "gt", (Literal("int", 2),)),
            Predicate("salary", "gte", (Literal("int", 3),)),
            Predicate("salary", "lt", (Literal("int", 4),)),
            Predicate("salary", "lte", (Literal("int", 5),)),
            Predicate("owner.id", "in", (Literal("int", 6), Literal("int", 7))),
            Predicate("email", "is_null"),
            Predicate("id", "is_not_null"),
            AnyOf((Predicate("isDeleted", "eq", (Literal("bool", False),)), Predicate("isDeleted", "is_null"))),
            Predicate("customText1", "not_in_or_null", (Literal("str", "X"),)),
        ]
        assert qs.render_where(clauses, 2000) == (
            "id = 1 AND salary > 2 AND salary >= 3 AND salary < 4 AND salary <= 5 AND owner.id IN (6, 7) AND email IS NULL"
            " AND id IS NOT NULL AND (isDeleted = false OR isDeleted IS NULL)"
            " AND (customText1 NOT IN ('X') OR customText1 IS NULL)"
        )

    def test_max_chars(self):
        clause = Predicate("id", "in", tuple(Literal("int", i) for i in range(1, 400)))
        with pytest.raises(qs.UnsupportedValue):
            qs.render_where([clause], 100)

    @pytest.mark.parametrize(
        "clause",
        [
            Predicate("id; DROP", "eq", (Literal("int", 1),)),
            Predicate("a.b.c", "eq", (Literal("int", 1),)),
            Predicate("id", "like", (Literal("str", "x"),)),
            Predicate("id", "eq", ()),
            Predicate("id", "is_null", (Literal("int", 1),)),
            Predicate("id", "in", ()),
        ],
    )
    def test_structural_errors(self, clause):
        with pytest.raises(qs.RenderError):
            qs.render_where([clause], 2000)

    def test_empty_where_is_an_error(self):
        with pytest.raises(qs.RenderError):
            qs.render_where([], 2000)


class TestRequest:
    def test_fixed_param_keys_and_no_order(self):
        endpoint, params = qs.query_request("Candidate", "id = 1", "id,firstName", 0, 26)
        assert endpoint == "/query/Candidate"
        assert set(params) == {"where", "fields", "count", "start"}
        assert "orderBy" not in params and "sort" not in params

    @pytest.mark.parametrize("entity", ["candidate", "Candidate/../x", "", "Job Order"])
    def test_entity_name_checked(self, entity):
        with pytest.raises(qs.RenderError):
            qs.query_request(entity, "id = 1", "id", 0, 1)

    def test_render_fields_nested(self):
        assert qs.render_fields([("id", ()), ("owner", ("id",)), ("address", ("city", "state"))]) == "id,owner(id),address(city,state)"

    @pytest.mark.parametrize("sel", [[("a.b", ())], [("owner", ("x.y",))], [("bad name", ())]])
    def test_render_fields_rejects(self, sel):
        with pytest.raises(qs.RenderError):
            qs.render_fields(sel)


# ---------------------------------------------------------------------- #
# Query-support resource (D-5C-3)
# ---------------------------------------------------------------------- #


class TestSupportLoader:
    def test_packaged_loads(self):
        support = sup.load_query_support()
        assert support.version == 1
        assert {e.canonical_entity for e in support.entities.values()} == set(qb.FIND_ENTITIES)
        assert all(e.operation == "query" for e in support.entities.values())  # HV-Q1 / C3-3
        assert support.syntax.string_escape_verified is False
        assert support.syntax.prefix_match_verified is False
        assert support.syntax.order_by_direction_verified is False
        assert all(not e.sortable for e in support.entities.values())  # HV-Q4
        assert support.entities["Appointment"].instance_filter == ("parentAppointment", "is_null")  # HV-Q9b
        assert not any(h.verified for h in support.history.values())  # HV-Q10

    def test_soft_delete_entries(self):
        ents = sup.load_query_support().entities
        for name in ("Candidate", "ClientContact", "JobOrder", "JobSubmission", "Appointment", "CorporateUser"):
            assert ents[name].soft_delete_field == "isDeleted"
        assert ents["JobOrder"].soft_delete_nullable and ents["CorporateUser"].soft_delete_nullable
        for name in ("Placement", "ClientCorporation"):
            assert ents[name].soft_delete_field is None and ents[name].soft_delete_resolved

    def test_entry_without_hv_fails(self):
        doc = sup.packaged_document()
        del doc["entities"]["Candidate"]["filterable"]["firstName"]["hv"]
        with pytest.raises(sup.SupportError):
            sup.QuerySupport.from_dict(doc)

    @pytest.mark.parametrize("hv", [None, "", "HV-A1", "Q2", 2])
    def test_bad_hv_ids_fail(self, hv):
        doc = sup.packaged_document()
        doc["entities"]["JobOrder"]["hv"] = hv
        with pytest.raises(sup.SupportError):
            sup.QuerySupport.from_dict(doc)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.__setitem__("extra", 1),
            lambda d: d.__setitem__("version", 2),
            lambda d: d["entities"]["Candidate"].__setitem__("operation", "lucene"),
            lambda d: d["entities"]["Candidate"]["filterable"].__setitem__("firstName", {"ops": ["like"], "hv": "HV-Q2"}),
            lambda d: d["entities"]["Candidate"]["filterable"].__setitem__("a b", {"ops": ["eq"], "hv": "HV-Q2"}),
            lambda d: d["entities"]["Candidate"].__setitem__("unknown", 1),
            lambda d: d["syntax"]["query"].__setitem__("max_where_chars", 10_000),
            lambda d: d["history_sources"]["job_status"].__setitem__("verified", "yes"),
        ],
    )
    def test_invalid_documents(self, mutate):
        doc = sup.packaged_document()
        mutate(doc)
        with pytest.raises(sup.SupportError):
            sup.QuerySupport.from_dict(doc)

    def test_unresolved_soft_delete_entry(self):
        doc = sup.packaged_document()
        doc["entities"]["Candidate"]["soft_delete"] = {"unresolved": True, "reason": "x", "hv": "HV-Q6"}
        support = sup.QuerySupport.from_dict(doc)
        assert support.entities["Candidate"].soft_delete_resolved is False


# ---------------------------------------------------------------------- #
# Validation (§3.1, §3.3 step 1; NB-2 strict mode)
# ---------------------------------------------------------------------- #


def _validate(filters):
    return qb.validate_filters(filters, CANDIDATE_FIELDS, CANDIDATE_TYPES, "UTC")


class TestValidation:
    def test_typo_suggests_from_allowlist_only(self):
        _, errors, _ = _validate([{"field": "frist_name", "op": "eq", "value": "Ada"}])
        assert errors[0].code == "unknown_field"
        assert errors[0].suggestions == ("first_name",)
        assert set(errors[0].suggestions) <= set(CANDIDATE_FIELDS)

    @pytest.mark.parametrize("name", ["firstName", "isDeleted", "owner.id", "Candidate", "__class__", "FIRST_NAME", "customText3"])
    def test_raw_and_odd_names_are_unknown(self, name):
        _, errors, _ = _validate([{"field": name, "op": "eq", "value": "x"}])
        assert errors and errors[0].code == "unknown_field"
        assert all(s in CANDIDATE_FIELDS for s in errors[0].suggestions)

    def test_unknown_filter_keys_rejected(self):
        _, errors, _ = _validate([{"field": "first_name", "op": "eq", "value": "Ada", "raw": "1=1"}])
        assert errors[0].code == "invalid_value"

    @pytest.mark.parametrize(
        "flt",
        [
            {"field": "id", "op": "eq", "value": True},
            {"field": "id", "op": "eq", "value": 0},
            {"field": "id", "op": "eq", "value": 2**63},
            {"field": "id", "op": "eq", "value": "1"},
            {"field": "id", "op": "in", "value": []},
            {"field": "id", "op": "in", "value": list(range(1, 52))},
            {"field": "id", "op": "gt", "value": 1},
            {"field": "first_name", "op": "eq", "value": ""},
            {"field": "first_name", "op": "eq", "value": "a\x00b"},
            {"field": "first_name", "op": "eq", "value": "x" * 201},
            {"field": "first_name", "op": "starts_with_x", "value": "a"},
            {"field": "first_name", "op": "is_null", "value": None},
            {"field": "first_name", "op": "eq"},
            {"field": "date_added", "op": "gte", "value": "2026-01-01T00:00:00"},
            {"field": "date_added", "op": "eq", "value": "2026-01-01"},
        ],
    )
    def test_rejections(self, flt):
        out, errors, unsupported = _validate([flt])
        assert not out and errors and not unsupported

    def test_number_values(self):
        job = load_canonical_catalog().entity("job")
        types = {f.name: f.type for f in job.fields}
        names = tuple(types)
        ok, errors, _ = qb.validate_filters([{"field": "salary", "op": "gt", "value": 5}], names, types, "UTC")
        assert ok and not errors
        for bad in (math.nan, math.inf, True, "5"):
            _, errors, _ = qb.validate_filters([{"field": "salary", "op": "gt", "value": bad}], names, types, "UTC")
            assert errors

    def test_text_fields_unsupported(self):
        job = load_canonical_catalog().entity("job")
        types = {f.name: f.type for f in job.fields}
        _, errors, unsupported = qb.validate_filters([{"field": "description", "op": "eq", "value": "x"}], tuple(types), types, "UTC")
        assert not errors and unsupported[0].code == "unsupported_filter"

    def test_bounds(self):
        assert _validate(None) == ([], [], [])
        assert _validate("x")[1][0].code == "invalid_value"
        assert _validate([{"field": "id", "op": "eq", "value": 1}] * 11)[1][0].code == "invalid_value"

    def test_nfc_normalized(self):
        out, _, _ = _validate([{"field": "first_name", "op": "eq", "value": "Café"}])
        assert out[0].value == "Café"

    def test_date_only_bound_uses_reporting_timezone(self):
        out, _, _ = qb.validate_filters(
            [{"field": "date_added", "op": "gte", "value": "2026-03-08"}], CANDIDATE_FIELDS, CANDIDATE_TYPES, "America/New_York"
        )
        assert out[0].value == 1772946000000  # 2026-03-08T00:00-05:00

    def test_sort_and_fields(self):
        assert qb.validate_sort({"field": "first_name", "direction": "asc", "x": 1}, CANDIDATE_FIELDS)[1][0].code == "invalid_value"
        assert qb.validate_sort({"field": "firstName"}, CANDIDATE_FIELDS)[1][0].code == "unknown_field"
        assert qb.validate_sort({"field": "first_name", "direction": "up"}, CANDIDATE_FIELDS)[1][0].code == "invalid_value"
        assert qb.validate_fields(["first_name", "first_name"], CANDIDATE_FIELDS) == (["first_name"], [])
        assert qb.validate_fields([], CANDIDATE_FIELDS)[1][0].code == "invalid_value"
        assert qb.validate_fields(["x"] * 31, CANDIDATE_FIELDS)[1][0].code == "invalid_value"
        assert qb.validate_fields(["lastName"], CANDIDATE_FIELDS)[1][0].code == "unknown_field"

    def test_no_starts_with_literal_path(self):
        """starts_with is in the canonical table but has no verified literal path (HV-Q2): the service makes it unsupported."""
        out, errors, _ = _validate([{"field": "first_name", "op": "starts_with", "value": "Ad"}])
        assert out and not errors  # validation passes; filter_predicate() returns unsupported_operator (tested in find_records)


# ---------------------------------------------------------------------- #
# Resolution (D-5C-5)
# ---------------------------------------------------------------------- #


class _Rec:
    def __init__(self, target, state="valid", key="field:candidate.first_name"):
        self.target = target
        self.key = key
        self.validation = type("V", (), {"state": state})()


class _Profile:
    def __init__(self, rec=None):
        self.rec = rec
        self.field_mappings = []

    def field_record(self, entity, name, active_only=True):
        return self.rec if name == "first_name" else None


def _resolver(profile=None, snapshot=frozenset({"id", "firstName", "lastName", "email"})):
    return qb.Resolver("candidate", load_canonical_catalog(), load_bullhorn_catalog(), profile, {}, snapshot)


class TestResolution:
    def test_default_needs_snapshot_source(self):
        assert isinstance(_resolver().resolve("first_name").target, RawField)
        assert _resolver(snapshot=frozenset({"id"})).resolve("first_name") is None
        assert _resolver(snapshot=None).resolve("first_name") is None

    def test_valid_profile_record_wins(self):
        res = _resolver(_Profile(_Rec(RawField("customText3")))).resolve("first_name")
        assert res.target == RawField("customText3") and res.raw_custom

    def test_invalid_profile_record_falls_back(self):
        res = _resolver(_Profile(_Rec(RawField("customText3"), state="broken"))).resolve("first_name")
        assert res.target == RawField("firstName")

    def test_sensitive_and_template(self):
        assert _resolver(_Profile(_Rec(RawField("customEncryptedText1")))).resolve("first_name").sensitive
        res = _resolver(snapshot=frozenset({"id", "firstName", "lastName"})).resolve("full_name")
        assert isinstance(res.target, TemplateField) and res.template and res.path is None

    def test_nested_path(self):
        res = _resolver(snapshot=frozenset({"id", "owner"})).resolve("owner_id")
        assert isinstance(res.target, NestedField) and res.path == "owner.id"

    def test_missing_requirement_string(self):
        assert qb.missing_requirement("job", "priority") == "mapping:job.priority"


class TestLiterals:
    def test_restricted_charset_without_escape_rule(self):
        assert isinstance(qb.literals_for("string", ["O'Brien"], False), str)
        assert qb.literals_for("string", ["Ada"], False) == (Literal("str", "Ada"),)
        assert isinstance(qb.literals_for("number", [1.5], False), str)
        assert qb.literals_for("datetime", [5], False) == (Literal("ts", 5),)
        assert qb.value_literal(True) is None and qb.value_literal("a'b") is None
