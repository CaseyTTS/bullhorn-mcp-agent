"""Tests for tenant mapping profiles."""

import logging
import os
from importlib import resources
from pathlib import Path

import pytest

from bullhorn_mcp.schema.bullhorn_catalog import NestedField, RawField, TemplateField, load_bullhorn_catalog
from bullhorn_mcp.schema.errors import ProfileError
from bullhorn_mcp.schema.mapping_profile import (
    PROFILE_ENV_VAR,
    CustomMapping,
    MappingProfile,
    UnmappedField,
    load_active_profile,
    resolve_profile_path,
)

PROFILES = Path(__file__).parent / "fixtures" / "profiles"


def _errors(name: str) -> tuple[str, ...]:
    with pytest.raises(ProfileError) as exc_info:
        MappingProfile.load(PROFILES / name)
    return exc_info.value.errors


class TestProfileLoading:
    def test_valid_profile(self):
        p = MappingProfile.load(PROFILES / "valid.yaml")
        assert p.tenant == "acme"
        assert p.generated_at is not None and p.generated_at.startswith("2026-10-06T12:00:00")
        cand = p.entity("candidate")
        assert cand is not None
        assert cand.standard == {"skills": RawField("customTextBlock2")}
        assert cand.custom["region"] == CustomMapping(RawField("customText7"), "string")
        assert cand.custom["recruiter_ref"] == CustomMapping(NestedField("owner", "id"), "integer")
        assert isinstance(cand.custom["label_line"].target, TemplateField)
        assert cand.unmapped_bullhorn_fields[0] == UnmappedField(field="customText12")
        assert cand.unmapped_bullhorn_fields[1].options == (("S", "Secret"),)

    def test_missing_version(self):
        assert any("missing required key 'version'" in e for e in _errors("missing_version.yaml"))

    def test_version_2(self):
        assert any("unsupported version 2" in e for e in _errors("version2.yaml"))

    def test_unknown_entity(self):
        assert any("'spaceship' is not a canonical entity" in e for e in _errors("unknown_entity.yaml"))

    def test_unknown_standard_key(self):
        assert any("'favourite_colour' is not a canonical field" in e for e in _errors("unknown_standard_key.yaml"))

    def test_custom_collision(self):
        assert any("collides with canonical field candidate.email" in e for e in _errors("custom_collision.yaml"))

    def test_malformed_custom_errors_aggregated(self):
        errors = _errors("malformed_custom.yaml")
        joined = "\n".join(errors)
        assert "custom.region: nested 'key'" in joined
        assert "custom.shift: unknown key 'extra'" in joined
        assert "custom.level: target must be" in joined
        assert "custom.BadName: custom name must match" in joined
        assert "custom.kind: type 'reference' is not allowed" in joined
        assert len(errors) == 5

    def test_every_rejected_template_form(self):
        errors = _errors("bad_templates.yaml")
        joined = "\n".join(errors)
        for name in ["t_dotted", "t_index", "t_conversion", "t_format", "t_empty", "t_positional", "t_unbalanced"]:
            assert f"custom.{name}:" in joined, name
        assert len(errors) == 7

    def test_non_identifier_raw_name(self):
        errors = _errors("bad_raw_name.yaml")
        assert len(errors) == 2
        assert all("must match" in e for e in errors)

    def test_invalid_yaml(self):
        assert any("YAML parse error" in e for e in _errors("invalid_yaml.yaml"))

    def test_unsafe_tag_rejected(self, tmp_path):
        p = tmp_path / "evil.yaml"
        p.write_text("version: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
        with pytest.raises(ProfileError, match="YAML parse error"):
            MappingProfile.load(p)

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {"version": 1, "entities": []},
            {"version": 1, "entities": {"candidate": []}},
            {"version": 1, "entities": {"candidate": {"standard": []}}},
            {"version": 1, "entities": {"candidate": {"custom": "x"}}},
            {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": {}}}},
            {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": [5]}}},
            {"version": 1, "entities": {"candidate": {"unmapped_bullhorn_fields": [{"field": "x", "options": "bad"}]}}},
            {"version": 1, "entities": {"candidate": {"surprise": {}}}},
            {"version": 1, "tenant": 5},
            {"version": 1, "extra": True},
            {"version": True},
        ],
    )
    def test_hostile_shapes_raise_profile_error(self, data):
        with pytest.raises(ProfileError):
            MappingProfile.from_dict(data)

    def test_empty_profile_ok(self):
        p = MappingProfile.from_dict({"version": 1})
        assert p.entities == {}


class TestRoundTrip:
    def test_load_save_load(self, tmp_path):
        original = MappingProfile.load(PROFILES / "valid.yaml")
        out = tmp_path / "nested" / "profile.yaml"
        original.save(out)
        again = MappingProfile.load(out)
        assert again == original
        assert again.to_dict() == original.to_dict()

    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        p = MappingProfile.load(PROFILES / "valid.yaml")
        target = tmp_path / "profile.yaml"
        target.write_text("old", encoding="utf-8")
        p.save(target)
        assert [x.name for x in tmp_path.iterdir()] == ["profile.yaml"]
        assert MappingProfile.load(target) == p

    def test_save_failure_keeps_original(self, tmp_path, monkeypatch):
        target = tmp_path / "profile.yaml"
        target.write_text("original", encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            MappingProfile.load(PROFILES / "valid.yaml").save(target)
        assert target.read_text(encoding="utf-8") == "original"
        assert [x.name for x in tmp_path.iterdir()] == ["profile.yaml"]

    def test_example_profile_loads_and_round_trips(self, tmp_path):
        text = resources.files("bullhorn_mcp.mappings").joinpath("profile.example.yaml").read_text(encoding="utf-8")
        src = tmp_path / "example.yaml"
        src.write_text(text, encoding="utf-8")
        p = MappingProfile.load(src)
        p.save(tmp_path / "copy.yaml")
        assert MappingProfile.load(tmp_path / "copy.yaml") == p

    def test_example_profile_uses_only_verified_or_custom_names(self, tmp_path):
        text = resources.files("bullhorn_mcp.mappings").joinpath("profile.example.yaml").read_text(encoding="utf-8")
        src = tmp_path / "example.yaml"
        src.write_text(text, encoding="utf-8")
        p = MappingProfile.load(src)
        bh = load_bullhorn_catalog()
        all_standard = {f for e in bh.entities.values() for f in e.standard_fields}
        for canon, ep in p.entities.items():
            bh_entity = bh.bullhorn_entity_for(canon)
            assert bh_entity is not None
            std = set(bh.standard_fields(bh_entity))
            targets = list(ep.standard.values()) + [c.target for c in ep.custom.values()]
            for t in targets:
                for src_name in t.sources:
                    assert src_name in std or bh.is_custom_field(src_name), f"{canon}: {src_name}"
                if isinstance(t, NestedField):
                    assert t.key in all_standard
            for u in ep.unmapped_bullhorn_fields:
                assert bh.is_custom_field(u.field)


class TestActiveProfile:
    def test_none_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        assert resolve_profile_path() is None
        status = load_active_profile()
        assert status.state == "none"
        assert status.profile is None

    def test_none_when_env_blank(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ENV_VAR, "   ")
        assert load_active_profile().state == "none"

    def test_loaded(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(PROFILES / "valid.yaml"))
        status = load_active_profile()
        assert status.state == "loaded"
        assert status.profile is not None and status.profile.tenant == "acme"

    def test_missing(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(tmp_path / "nope.yaml"))
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.schema"):
            status = load_active_profile()
        assert status.state == "missing"
        assert status.profile is None
        assert any(r.name == "bullhorn_mcp.schema" for r in caplog.records)

    def test_directory_is_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(tmp_path))
        assert load_active_profile().state == "missing"

    @pytest.mark.parametrize("name", ["version2.yaml", "malformed_custom.yaml", "invalid_yaml.yaml", "bad_templates.yaml"])
    def test_invalid(self, monkeypatch, caplog, name):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(PROFILES / name))
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.schema"):
            status = load_active_profile()
        assert status.state == "invalid"
        assert status.profile is None
        assert status.errors
        assert any(r.name == "bullhorn_mcp.schema" and r.levelno == logging.WARNING for r in caplog.records)

    def test_explicit_path_overrides_env(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(PROFILES / "version2.yaml"))
        assert load_active_profile(PROFILES / "valid.yaml").state == "loaded"

    def test_never_raises_on_unexpected_errors(self, monkeypatch, tmp_path):
        p = tmp_path / "p.yaml"
        p.write_bytes(b"\xff\xfe\x00bad")
        monkeypatch.setenv(PROFILE_ENV_VAR, str(p))
        assert load_active_profile().state == "invalid"

        def explode(*args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(MappingProfile, "load", classmethod(explode))
        monkeypatch.setenv(PROFILE_ENV_VAR, str(PROFILES / "valid.yaml"))
        status = load_active_profile()
        assert status.state == "invalid"
        assert status.profile is None
