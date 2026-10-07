"""Regression tests for the Phase 3 review triage fixes F-1..F-6 (R-1a..R-6c)."""

import copy
import logging
import time

import pytest

from bullhorn_mcp.schema.bullhorn_catalog import BullhornCatalog, TemplateField, load_bullhorn_catalog, parse_target
from bullhorn_mcp.schema.canonical_catalog import CanonicalCatalog, load_canonical_catalog
from bullhorn_mcp.schema.errors import (
    MAX_ERROR_LINES,
    CatalogError,
    ProfileError,
    UnknownCanonicalFieldError,
    describe_value,
)
from bullhorn_mcp.schema.mapping_profile import PROFILE_ENV_VAR, MappingProfile, load_active_profile
from bullhorn_mcp.schema.translator import FieldTranslator

BOUND = 10_000
HOSTILE_VALUES = [[1], {"a": 1}, 5, True, None]


def _translator(profile=None):
    return FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), profile)


def _bomb_yaml(placement: str) -> str:
    """A ~400-byte, 9-level YAML alias bomb; ``placement`` is YAML using ``*a8``."""
    lines = ['a0: &a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for i in range(1, 9):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 9) + "]")
    lines.append(placement)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# F-1
# ---------------------------------------------------------------------- #


class TestF1HostileTypes:
    def test_r1a_profile_reports_tenant_and_type(self):
        data = {"version": 1, "tenant": 5, "entities": {"candidate": {"custom": {"a": {"field": "owner", "key": "id", "type": ["x"]}}}}}
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.from_dict(data)
        joined = "\n".join(exc_info.value.errors)
        assert "tenant: must be a string" in joined
        assert "custom.a: type must be a string" in joined
        assert len(exc_info.value.errors) == 2

    def test_r1b_canonical_catalog_reports_both(self):
        data = {
            "version": 1,
            "entities": {"widget": {"fields": {"id": {"type": "id"}, "a": {"type": ["x"]}, "BadKey": {"type": "string"}}}},
        }
        with pytest.raises(CatalogError) as exc_info:
            CanonicalCatalog.from_dict(data)
        joined = "\n".join(exc_info.value.errors)
        assert "type must be a string" in joined
        assert "field key must match" in joined
        assert len(exc_info.value.errors) == 2

    def test_r1b_bullhorn_catalog_unhashable_values(self):
        data = {
            "version": [1],
            "custom_field_patterns": [{"a": 1}],
            "entities": {"Candidate": {"canonical_entity": ["candidate"], "standard_fields": ["id"]}},
        }
        with pytest.raises(CatalogError) as exc_info:
            BullhornCatalog.from_dict(data, load_canonical_catalog())
        assert len(exc_info.value.errors) == 3

    @staticmethod
    def _base():
        return {
            "version": 1,
            "tenant": "t",
            "generated_at": "2026-10-06",
            "entities": {
                "candidate": {
                    "standard": {"skills": "customTextBlock2"},
                    "custom": {"region": {"field": "owner", "key": "id", "type": "integer"}},
                    "unmapped_bullhorn_fields": [
                        {"field": "customText1", "label": "L", "data_type": "String", "field_type": "SCALAR",
                         "options": [{"value": "a", "label": "A"}], "required": False, "read_only": False}
                    ],
                }
            },
        }

    @staticmethod
    def _inject(data, position, value):
        cand = data["entities"]["candidate"]
        if position in ("version", "tenant", "generated_at", "entities"):
            data[position] = value
        elif position == "entity_value":
            data["entities"]["candidate"] = value
        elif position == "standard_key":
            cand["standard"] = {value: "customTextBlock2"}
        elif position == "standard_value":
            cand["standard"]["skills"] = value
        elif position == "custom_key":
            cand["custom"] = {value: "customText7"}
        elif position == "custom_value":
            cand["custom"]["region"] = value
        elif position in ("custom_type", "custom_field", "custom_nested_key"):
            cand["custom"]["region"][{"custom_type": "type", "custom_field": "field", "custom_nested_key": "key"}[position]] = value
        elif position == "unmapped_list":
            cand["unmapped_bullhorn_fields"] = value
        elif position == "unmapped_entry":
            cand["unmapped_bullhorn_fields"] = [value]
        elif position.startswith("unmapped."):
            cand["unmapped_bullhorn_fields"][0][position.split(".", 1)[1]] = value
        else:  # pragma: no cover
            raise AssertionError(position)

    POSITIONS = [
        "version", "tenant", "generated_at", "entities", "entity_value",
        "standard_key", "standard_value", "custom_key", "custom_value",
        "custom_type", "custom_field", "custom_nested_key",
        "unmapped_list", "unmapped_entry",
        "unmapped.field", "unmapped.label", "unmapped.data_type", "unmapped.field_type",
        "unmapped.options", "unmapped.required", "unmapped.read_only",
    ]

    @pytest.mark.parametrize("position", POSITIONS)
    @pytest.mark.parametrize("value", HOSTILE_VALUES, ids=["list", "dict", "int", "bool", "null"])
    def test_r1c_only_profile_error_escapes(self, position, value):
        if position.endswith("_key") and position != "custom_nested_key" and isinstance(value, (list, dict)):
            pytest.skip("unhashable values cannot be mapping keys")
        data = self._base()
        self._inject(data, position, copy.deepcopy(value))
        try:
            MappingProfile.from_dict(data)
        except ProfileError:
            pass

    def test_r1d_load_active_profile_invalid(self, tmp_path, monkeypatch):
        p = tmp_path / "p.yaml"
        p.write_text(
            "version: 1\ntenant: 5\nentities:\n  candidate:\n    custom:\n      a: {field: owner, key: id, type: [x]}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        status = load_active_profile()
        assert status.state == "invalid"
        assert status.profile is None


# ---------------------------------------------------------------------- #
# F-2
# ---------------------------------------------------------------------- #


class TestF2BoundedMessages:
    def test_describe_value(self):
        assert describe_value("abc") == "'abc'"
        assert len(describe_value("x" * 10_000)) <= 80
        assert describe_value("x" * 10_000).endswith("...")
        assert describe_value(None) == "None"
        assert describe_value(True) == "True"
        assert describe_value(5) == "5"
        assert describe_value(1.5) == "1.5"
        assert describe_value(10**5000) == "<int>"
        assert describe_value([1] * 10_000) == "<list>"
        assert describe_value({"a": 1}) == "<dict>"

    def _assert_bounded_profile(self, tmp_path, monkeypatch, caplog, placement):
        p = tmp_path / "bomb.yaml"
        p.write_text(_bomb_yaml(placement), encoding="utf-8")
        assert p.stat().st_size < 600
        start = time.perf_counter()
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.load(p)
        assert len(str(exc_info.value)) < BOUND
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.schema"):
            status = load_active_profile()
        assert status.state == "invalid"
        assert caplog.records
        for record in caplog.records:
            assert len(record.getMessage()) < BOUND
        assert all(len(e) < BOUND for e in status.errors)
        assert time.perf_counter() - start < 2.0

    def test_r2a_bomb_under_version(self, tmp_path, monkeypatch, caplog):
        self._assert_bounded_profile(tmp_path, monkeypatch, caplog, "version: *a8")

    @pytest.mark.parametrize(
        "placement",
        [
            "version: 1\ntenant: *a8",
            "version: 1\nentities: {candidate: {custom: {foo: {field: owner, key: id, type: *a8}}}}",
            "version: 1\nentities: {candidate: {unmapped_bullhorn_fields: [*a8]}}",
            "version: 1\nentities: {candidate: {unmapped_bullhorn_fields: [{field: customText1, label: *a8}]}}",
        ],
        ids=["tenant", "custom_type", "unmapped_entry", "unmapped_entry_field"],
    )
    def test_r2b_bomb_elsewhere(self, tmp_path, monkeypatch, caplog, placement):
        self._assert_bounded_profile(tmp_path, monkeypatch, caplog, placement)

    def test_r2c_catalog_loaders(self):
        text = _bomb_yaml("version: *a8\nentities: {}")
        start = time.perf_counter()
        with pytest.raises(CatalogError) as exc_info:
            CanonicalCatalog.from_yaml_text(text)
        assert len(str(exc_info.value)) < BOUND
        with pytest.raises(CatalogError) as exc_info:
            BullhornCatalog.from_yaml_text(text, load_canonical_catalog())
        assert len(str(exc_info.value)) < BOUND
        assert time.perf_counter() - start < 2.0

    def test_r2d_many_defects_aggregated_and_capped(self, tmp_path, monkeypatch, caplog):
        custom = {f"Bad{i}": "customText1" for i in range(500)}
        data = {"version": 1, "entities": {"candidate": {"custom": custom}}}
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.from_dict(data)
        assert len(exc_info.value.errors) == 500  # aggregated, nothing lost
        message_lines = str(exc_info.value).splitlines()
        assert len(message_lines) == 1 + MAX_ERROR_LINES + 1
        assert message_lines[-1].strip() == "- ... and 400 more"

        import yaml

        p = tmp_path / "many.yaml"
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.schema"):
            status = load_active_profile()
        assert status.state == "invalid"
        assert len(status.errors) == MAX_ERROR_LINES + 1
        assert status.errors[-1] == "... and 400 more"
        for record in caplog.records:
            assert len(record.getMessage().splitlines()) <= MAX_ERROR_LINES + 3


# ---------------------------------------------------------------------- #
# F-3
# ---------------------------------------------------------------------- #


def _profile(entity_body):
    return {"version": 1, "entities": {"candidate": entity_body}}


class TestF3TrailingNewline:
    def test_r3a_custom_name_with_newline(self):
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.from_dict(_profile({"custom": {"email\n": "customText1"}}))
        assert any("custom name must match" in e for e in exc_info.value.errors)

    @pytest.mark.parametrize(
        "target",
        ["firstName\n", {"field": "owner\n", "key": "id"}, {"field": "owner", "key": "id\n"}, "{firstName\n} {lastName}"],
    )
    def test_r3b_targets_with_newline(self, target):
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_profile({"custom": {"x": target}}))
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_profile({"standard": {"skills": target}}))

    def test_r3b_unmapped_field_with_newline(self):
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_profile({"unmapped_bullhorn_fields": ["customText1\n"]}))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d["entities"].__setitem__("candidate\n", d["entities"].pop("widget")),
            lambda d: d["entities"]["widget"]["fields"].__setitem__("first_name\n", {"type": "string"}),
            lambda d: d["entities"]["widget"]["fields"]["owner_id"].__setitem__("ref", "user\n"),
        ],
        ids=["entity_key", "field_key", "ref"],
    )
    def test_r3c_canonical_catalog(self, mutate):
        data = {
            "version": 1,
            "entities": {"widget": {"fields": {"id": {"type": "id"}, "owner_id": {"type": "reference", "ref": "user"}}}},
        }
        mutate(data)
        with pytest.raises(CatalogError):
            CanonicalCatalog.from_dict(data)

    def _bh(self):
        return {
            "version": 1,
            "entities": {
                "Candidate": {
                    "canonical_entity": "candidate",
                    "standard_fields": ["id", "firstName"],
                    "default_mappings": {"id": "id", "first_name": "firstName"},
                }
            },
        }

    def test_r3c_bullhorn_entity_key(self):
        data = self._bh()
        data["entities"]["Candidate\n"] = data["entities"].pop("Candidate")
        with pytest.raises(CatalogError, match="entity name must match"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_r3c_standard_fields_entry(self):
        data = self._bh()
        data["entities"]["Candidate"]["standard_fields"].append("firstName\n")
        with pytest.raises(CatalogError, match="must match"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_r3d_translator_rejects_newline_name(self):
        with pytest.raises(UnknownCanonicalFieldError):
            _translator().canonical_to_raw("candidate", ["first_name\n"])

    @pytest.mark.parametrize("raw", ["firstName\n", "a\n", "x\r\n"])
    def test_raw_targets_never_accept_newlines(self, raw):
        target, _, errors = parse_target(raw, "w")
        assert target is None and errors


# ---------------------------------------------------------------------- #
# F-4
# ---------------------------------------------------------------------- #

R4A_RECORD = {
    "id": 1,
    "owner": {"id": 5, "firstName": "A", "lastName": "B"},
    "address": {"city": "X", "state": "Y", "zip": "1", "address1": "2 Main"},
    "foo": [1],
}


def _leaves_conserved(record, rec, translator, entity):
    """Every top-level key, and each sub-key of nested-touched dicts, appears in fields/custom/unmapped."""
    shared = translator.shared_mappings(entity)
    custom = translator.custom_mappings(entity)
    targets = list(shared.values()) + list(custom.values())
    nested_outers = {t.field for t in targets if hasattr(t, "key")}
    produced = {**rec.fields, **rec.custom}
    produced_values = list(produced.values())
    for key, value in record.items():
        if key in nested_outers and isinstance(value, dict):
            residual = rec.unmapped.get(key, {})
            for sub_key, sub_value in value.items():
                in_residual = sub_key in residual and residual[sub_key] is sub_value
                assert in_residual or any(v is sub_value for v in produced_values), f"{key}.{sub_key} dropped"
        else:
            in_unmapped = key in rec.unmapped and rec.unmapped[key] is value
            consumed = any(v is value for v in produced_values) or any(
                key in getattr(t, "placeholders", ()) for t in targets
            )
            assert in_unmapped or consumed, f"{key} dropped"


class TestF4NestedResidual:
    def test_r4a(self):
        rec = _translator().raw_to_canonical("candidate", copy.deepcopy(R4A_RECORD))
        assert rec.fields["owner_id"] == 5
        assert rec.fields["city"] == "X"
        assert rec.fields["state"] == "Y"
        assert rec.unmapped == {"owner": {"firstName": "A", "lastName": "B"}, "address": {"zip": "1", "address1": "2 Main"}, "foo": [1]}
        assert list(rec.unmapped["address"]) == ["zip", "address1"]  # order preserved
        assert rec.to_dict()["unmapped"] == rec.unmapped

    def test_r4b_fully_consumed_outer_absent(self):
        rec = _translator().raw_to_canonical("candidate", {"id": 1, "owner": {"id": 5}})
        assert rec.fields["owner_id"] == 5
        assert "owner" not in rec.unmapped

    def test_r4c_non_dict_outer(self):
        rec = _translator().raw_to_canonical("candidate", {"id": 1, "owner": 7})
        assert "owner_id" in rec.missing
        assert rec.unmapped["owner"] == 7

    def test_whole_consumption_wins(self):
        profile = MappingProfile.from_dict(_profile({"custom": {"owner_raw": "owner"}}))
        rec = _translator(profile).raw_to_canonical("candidate", {"id": 1, "owner": {"id": 5, "firstName": "A"}})
        assert rec.fields["owner_id"] == 5
        assert rec.custom["owner_raw"] == {"id": 5, "firstName": "A"}
        assert "owner" not in rec.unmapped

    def test_r4d_conservation(self, sample_candidate):
        t = _translator()
        for record in (copy.deepcopy(R4A_RECORD), sample_candidate):
            rec = t.raw_to_canonical("candidate", record)
            _leaves_conserved(record, rec, t, "candidate")


# ---------------------------------------------------------------------- #
# F-5
# ---------------------------------------------------------------------- #


class TestF5IdNotOverridable:
    def test_r5a_standard_id_rejected(self, tmp_path, sample_candidate):
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.from_dict(_profile({"standard": {"id": "customText1"}}))
        assert any("candidate" in e and "'id'" in e for e in exc_info.value.errors)

        p = tmp_path / "id.yaml"
        p.write_text("version: 1\nentities:\n  candidate:\n    standard:\n      id: customText1\n", encoding="utf-8")
        status = load_active_profile(p)
        assert status.state == "invalid"
        assert status.profile is None
        broken = FieldTranslator(load_canonical_catalog(), load_bullhorn_catalog(), status.profile)
        record = {**sample_candidate, "customText1": "not-the-id"}
        assert broken.raw_to_canonical("candidate", record).to_dict() == _translator().raw_to_canonical("candidate", record).to_dict()

    def test_r5b_catalog_id_must_be_raw_id(self):
        data = {
            "version": 1,
            "entities": {
                "Candidate": {
                    "canonical_entity": "candidate",
                    "standard_fields": ["id", "owner"],
                    "default_mappings": {"id": {"field": "owner", "key": "id"}},
                }
            },
        }
        with pytest.raises(CatalogError, match="canonical 'id' must map to exactly the raw field 'id'"):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    def test_shipped_catalog_ids_are_raw_id(self):
        for ent in load_bullhorn_catalog().entities.values():
            assert ent.default_mappings["id"].to_data() == "id"

    def test_r5c_custom_id_collides(self):
        with pytest.raises(ProfileError, match="collides with canonical field candidate.id"):
            MappingProfile.from_dict(_profile({"custom": {"id": "customText1"}}))


# ---------------------------------------------------------------------- #
# F-6
# ---------------------------------------------------------------------- #

NEW_REJECTS = ["{a:}", "{a!}", "{ a}", "{a }", "{a}{", "}{a}"]
OLD_REJECTS = ["{a.b}", "{a[0]}", "{a!r}", "{a:>5}", "{}", "{0}"]


class TestF6StrictTemplates:
    @pytest.mark.parametrize("template", NEW_REJECTS + OLD_REJECTS)
    def test_r6ab_rejected_in_catalog(self, template):
        data = {
            "version": 1,
            "entities": {
                "Candidate": {
                    "canonical_entity": "candidate",
                    "standard_fields": ["id", "a", "firstName"],
                    "default_mappings": {"id": "id", "full_name": template},
                }
            },
        }
        with pytest.raises(CatalogError):
            BullhornCatalog.from_dict(data, load_canonical_catalog())

    @pytest.mark.parametrize("template", NEW_REJECTS + OLD_REJECTS)
    def test_r6ab_rejected_in_profile(self, template):
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_profile({"standard": {"full_name": template}}))
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_profile({"custom": {"t": template}}))

    def test_r6c_accepted(self):
        target, _, errors = parse_target("{firstName} {lastName}", "w")
        assert errors == [] and isinstance(target, TemplateField)
        target, _, errors = parse_target("{{x}} {firstName}", "w")
        assert errors == [] and isinstance(target, TemplateField)
        assert target.placeholders == ("firstName",)

        profile = MappingProfile.from_dict(_profile({"custom": {"t": "{{x}} {firstName}"}}))
        rec = _translator(profile).raw_to_canonical("candidate", {"firstName": "A"})
        assert rec.custom["t"] == "{x} A"


# ---------------------------------------------------------------------- #
# Round 2: F-7 (parse stage raises only the documented error)
# ---------------------------------------------------------------------- #

HUGE_INT = "9" * 5000
DEEP_SEQUENCE = "[" * 5000 + "]" * 5000
DEEP_MAPPING = "{a: " * 3000 + "1" + "}" * 3000


def _cause_chain_has(exc: BaseException, kind: type) -> bool:
    seen = exc.__cause__
    while seen is not None:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__
    return False


def _profile_text(kind: str) -> str:
    return {
        "huge_int_version": f"version: {HUGE_INT}\n",
        "huge_int_tenant": f"version: 1\ntenant: {HUGE_INT}\n",
        "deep_sequence": f"version: 1\ntenant: {DEEP_SEQUENCE}\n",
        "deep_mapping": f"version: 1\ntenant: {DEEP_MAPPING}\n",
    }[kind]


class TestF7ParseStage:
    @pytest.mark.parametrize("kind", ["huge_int_version", "huge_int_tenant"])
    def test_r7a_huge_int_profile(self, tmp_path, monkeypatch, kind):
        p = tmp_path / "p.yaml"
        p.write_text(_profile_text(kind), encoding="utf-8")
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.load(p)
        assert type(exc_info.value) is ProfileError
        assert len(str(exc_info.value)) < BOUND
        # F-9 superseded F-7 #1's message format.
        assert any(e.startswith("YAML parse error (ValueError):") for e in exc_info.value.errors)
        assert _cause_chain_has(exc_info.value, ValueError)
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        assert load_active_profile().state == "invalid"

    @pytest.mark.parametrize("kind", ["deep_sequence", "deep_mapping"])
    def test_r7b_deep_nesting_profile(self, tmp_path, monkeypatch, kind):
        p = tmp_path / "p.yaml"
        p.write_text(_profile_text(kind), encoding="utf-8")
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.load(p)
        assert type(exc_info.value) is ProfileError
        assert len(str(exc_info.value)) < BOUND
        assert exc_info.value.errors == ("YAML nesting too deep",)
        assert _cause_chain_has(exc_info.value, RecursionError)
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        assert load_active_profile().state == "invalid"

    @pytest.mark.parametrize(
        "text,prefix",
        [
            (f"version: {HUGE_INT}\nentities: {{}}\n", "YAML parse error (ValueError):"),
            (f"version: 1\nentities: {DEEP_SEQUENCE}\n", "YAML nesting too deep"),
            (f"version: 1\nentities: {DEEP_MAPPING}\n", "YAML nesting too deep"),
        ],
        ids=["huge_int", "deep_sequence", "deep_mapping"],
    )
    def test_r7c_catalog_from_yaml_text(self, text, prefix):
        for load in (
            lambda: CanonicalCatalog.from_yaml_text(text),
            lambda: BullhornCatalog.from_yaml_text(text, load_canonical_catalog()),
        ):
            with pytest.raises(CatalogError) as exc_info:
                load()
            assert type(exc_info.value) is CatalogError
            assert len(str(exc_info.value)) < BOUND
            assert exc_info.value.errors[0].startswith(prefix)

    # Three loaders x {huge int, deep sequence, deep mapping}, plus invalid UTF-8 for the profile only.
    R7D_CASES = [
        (loader, hostile)
        for loader in ("profile", "canonical", "bullhorn")
        for hostile in ("huge_int", "deep_sequence", "deep_mapping")
    ] + [("profile", "invalid_utf8")]

    @pytest.mark.parametrize("loader,hostile", R7D_CASES)
    def test_r7d_only_documented_error(self, tmp_path, loader, hostile):
        text = {
            "huge_int": f"version: {HUGE_INT}\n",
            "deep_sequence": f"version: 1\ntenant: {DEEP_SEQUENCE}\n",
            "deep_mapping": f"version: 1\ntenant: {DEEP_MAPPING}\n",
        }.get(hostile)
        expected = ProfileError if loader == "profile" else CatalogError
        with pytest.raises(Exception) as exc_info:
            if loader == "profile":
                p = tmp_path / "p.yaml"
                if text is None:
                    p.write_bytes(b"version: 1\ntenant: \xff\xfe\n")
                else:
                    p.write_text(text, encoding="utf-8")
                MappingProfile.load(p)
            elif loader == "canonical":
                CanonicalCatalog.from_yaml_text(text)
            else:
                BullhornCatalog.from_yaml_text(text, load_canonical_catalog())
        assert type(exc_info.value) is expected


# ---------------------------------------------------------------------- #
# Round 2: F-8 (profile option values/labels are scalars)
# ---------------------------------------------------------------------- #


def _option_bomb_profile(slot: str) -> str:
    parts = ['&a0 ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for i in range(1, 9):
        parts.append(f"&a{i} [" + ",".join([f"*a{i - 1}"] * 9) + "]")
    bomb = "[" + ", ".join(parts) + "]"
    option = f"{{value: {bomb}, label: x}}" if slot == "value" else f"{{value: 1, label: {bomb}}}"
    return f"version: 1\nentities: {{candidate: {{unmapped_bullhorn_fields: [{{field: customText1, options: [{option}]}}]}}}}\n"


def _unmapped(entry):
    return {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": [entry]}}}


class TestF8ScalarOptions:
    @pytest.mark.parametrize("slot", ["value", "label"], ids=["r8a_value", "r8b_label"])
    def test_r8ab_option_alias_bomb(self, tmp_path, monkeypatch, slot):
        p = tmp_path / "opt_bomb.yaml"
        p.write_text(_option_bomb_profile(slot), encoding="utf-8")
        assert p.stat().st_size < 700
        start = time.perf_counter()
        with pytest.raises(ProfileError) as exc_info:
            MappingProfile.load(p)
        assert len(str(exc_info.value)) < BOUND
        assert any(f"options[0].{slot}" in e for e in exc_info.value.errors)
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        status = load_active_profile()
        assert status.state == "invalid"
        assert status.profile is None
        assert time.perf_counter() - start < 2.0

    @pytest.mark.parametrize(
        "options",
        [[{"value": [1]}], [{"value": {"a": 1}}], [{"value": 1, "label": ["x"]}]],
        ids=["value_list", "value_dict", "label_list"],
    )
    def test_r8c_container_options_rejected(self, options):
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_unmapped({"field": "customText1", "options": options}))

    @pytest.mark.parametrize(
        "options",
        [[{"value": 1}], [{"value": "A", "label": "Alpha"}], [{"value": True}], [{"value": None, "label": None}]],
        ids=["int", "str_with_label", "bool", "nulls"],
    )
    def test_r8c_scalar_options_load_and_round_trip(self, tmp_path, options):
        profile = MappingProfile.from_dict(_unmapped({"field": "customText1", "options": options}))
        out = tmp_path / "p.yaml"
        profile.save(out)
        assert MappingProfile.load(out) == profile

    @pytest.mark.parametrize("key", ["field", "label", "data_type", "field_type", "required", "read_only"])
    def test_r8d_non_option_keys_reject_lists(self, key):
        entry = {"field": "customText1", key: ["x"]}
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(_unmapped(entry))
