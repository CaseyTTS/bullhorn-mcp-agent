"""Tests for the SchemaContext facade and layer-isolation guarantees."""

import importlib
import logging
import re
import sys
from pathlib import Path

import respx

from bullhorn_mcp.schema import SchemaContext
from bullhorn_mcp.schema.mapping_profile import PROFILE_ENV_VAR

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "bullhorn_mcp"
PROFILES = Path(__file__).parent / "fixtures" / "profiles"


class TestSchemaContext:
    def test_no_profile(self, monkeypatch):
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        ctx = SchemaContext.load()
        assert ctx.profile_status.state == "none"
        assert ctx.translator.profile is None
        assert ctx.canonical.has_entity("candidate")
        assert ctx.bullhorn.bullhorn_entity_for("candidate") == "Candidate"

    def test_explicit_profile(self):
        ctx = SchemaContext.load(PROFILES / "valid.yaml")
        assert ctx.profile_status.state == "loaded"
        assert ctx.translator.canonical_to_raw("candidate", ["skills"]).raw_fields == ["id", "customTextBlock2"]

    def test_env_profile(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ENV_VAR, str(PROFILES / "valid.yaml"))
        assert SchemaContext.load().profile_status.state == "loaded"

    def test_missing_profile_never_raises(self, tmp_path):
        ctx = SchemaContext.load(tmp_path / "absent.yaml")
        assert ctx.profile_status.state == "missing"
        assert ctx.translator.profile is None

    def test_invalid_profile_output_identical_to_no_profile(self, monkeypatch, caplog, sample_candidate):
        """AC-14."""
        monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
        baseline = SchemaContext.load()
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.schema"):
            broken = SchemaContext.load(PROFILES / "malformed_custom.yaml")
        assert broken.profile_status.state == "invalid"
        assert broken.translator.profile is None
        assert caplog.records
        assert (
            broken.translator.raw_to_canonical("candidate", sample_candidate).to_dict()
            == baseline.translator.raw_to_canonical("candidate", sample_candidate).to_dict()
        )
        assert broken.translator.canonical_to_raw("candidate", None) == baseline.translator.canonical_to_raw("candidate", None)


class TestImportSafety:
    def test_importing_schema_makes_no_http_request(self, monkeypatch):
        """AC-21: fresh import under respx with no Bullhorn env vars -> zero calls."""
        import os

        for key in list(os.environ):
            if key.startswith("BULLHORN_"):
                monkeypatch.delenv(key)
        saved = {k: v for k, v in sys.modules.items() if k == "bullhorn_mcp.schema" or k.startswith("bullhorn_mcp.schema.")}
        saved_meta = sys.modules.get("bullhorn_mcp.bullhorn.meta")
        try:
            for k in saved:
                del sys.modules[k]
            sys.modules.pop("bullhorn_mcp.bullhorn.meta", None)
            with respx.mock(assert_all_called=False) as router:
                module = importlib.import_module("bullhorn_mcp.schema")
                module.SchemaContext.load()
                assert router.calls.call_count == 0
        finally:
            for k in [k for k in sys.modules if k == "bullhorn_mcp.schema" or k.startswith("bullhorn_mcp.schema.")]:
                del sys.modules[k]
            sys.modules.update(saved)
            if saved_meta is not None:
                sys.modules["bullhorn_mcp.bullhorn.meta"] = saved_meta


class TestStaticGuarantees:
    def _py_files(self, root):
        return sorted(p for p in root.rglob("*.py"))

    def test_no_unsafe_yaml_loading(self):
        """AC-3."""
        pattern = re.compile(r"yaml\.(load|unsafe_load)\(")
        for path in self._py_files(SRC):
            assert not pattern.search(path.read_text(encoding="utf-8")), path

    def test_schema_does_not_import_server_tools_or_client(self):
        """AC-4."""
        pattern = re.compile(r"from \.\.(server|tools)|bullhorn\.client|from \.\. import server")
        for path in self._py_files(SRC / "schema"):
            assert not pattern.search(path.read_text(encoding="utf-8")), path

    def test_meta_module_uses_get_meta_only(self):
        """AM-1."""
        text = (SRC / "bullhorn" / "meta.py").read_text(encoding="utf-8")
        assert "_request" not in text
        for path in self._py_files(SRC):
            assert "meta=full" not in path.read_text(encoding="utf-8"), path
