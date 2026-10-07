"""Tests for SchemaDiscoverer, redact_sample and build_draft_profile."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, PropertyMock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornClient
from bullhorn_mcp.bullhorn.meta import EntityMeta, MetaDiscovery, MetaSource, parse_entity_meta
from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog
from bullhorn_mcp.schema.canonical_catalog import load_canonical_catalog
from bullhorn_mcp.schema.discovery import SchemaDiscoverer, build_draft_profile, redact_sample
from bullhorn_mcp.schema.errors import SchemaError
from bullhorn_mcp.schema.mapping_profile import MappingProfile

FIXTURES = Path(__file__).parent / "fixtures"
CANDIDATE_META = json.loads((FIXTURES / "meta" / "candidate_tenant.json").read_text(encoding="utf-8"))


@pytest.fixture
def mock_auth(mock_session):
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


def _profile() -> MappingProfile:
    return MappingProfile.from_dict(
        {
            "version": 1,
            "tenant": "acme",
            "entities": {
                "candidate": {
                    "standard": {"skills": "customTextBlock2"},  # absent in tenant meta -> broken
                    "custom": {"region": "customText7", "ghost": "customText99"},
                }
            },
        }
    )


def _discoverer(meta_source, profile=None):
    return SchemaDiscoverer(meta_source, load_canonical_catalog(), load_bullhorn_catalog(), profile)


def _route_all_meta(mock_session, failing=()):
    """Mock every catalog entity's /meta call; Candidate returns the tenant fixture."""
    routes = {}
    for bh in load_bullhorn_catalog().entities:
        if bh in failing:
            resp = httpx.Response(500, json={"errorMessage": "boom"})
        elif bh == "Candidate":
            resp = httpx.Response(200, json=CANDIDATE_META)
        else:
            resp = httpx.Response(200, json={"entity": bh, "fields": [{"name": "id", "type": "ID", "dataType": "Integer"}]})
        routes[bh] = respx.get(f"{mock_session.rest_url}/meta/{bh}").mock(return_value=resp)
    return routes


class TestMetadataOnly:
    @respx.mock
    def test_default_discover_only_calls_meta(self, mock_auth, mock_session):
        """AC-16."""
        routes = _route_all_meta(mock_session)
        report = _discoverer(MetaDiscovery(BullhornClient(mock_auth)), _profile()).discover()
        assert len(report.entities) == len(load_bullhorn_catalog().entities)
        assert all(r.call_count == 1 for r in routes.values())
        assert len(respx.calls) == len(routes)
        rest_path = httpx.URL(mock_session.rest_url).path
        for call in respx.calls:
            path = call.request.url.path[len(rest_path):]
            assert path.startswith("/meta/"), path
            for forbidden in ("/search", "/query", "/entity"):
                assert forbidden not in path
            assert "meta" not in call.request.url.params

    @respx.mock
    def test_samples_require_both_flags(self, mock_auth, mock_session):
        _route_all_meta(mock_session)
        sample_source = Mock(return_value={"customText3": "secret"})
        d = _discoverer(MetaDiscovery(BullhornClient(mock_auth)))
        d.discover(include_sample_values=False, sample_source=sample_source)
        d.discover(include_sample_values=True, sample_source=None)
        sample_source.assert_not_called()
        assert all(c.request.url.path.split("/")[-2] == "meta" for c in respx.calls)


class TestDiscoveryClassification:
    """AC-17."""

    @respx.mock
    def test_fixture_tenant(self, mock_auth, mock_session):
        _route_all_meta(mock_session, failing=("JobOrder",))
        report = _discoverer(MetaDiscovery(BullhornClient(mock_auth)), _profile()).discover()
        cand = report.entity("candidate")
        assert cand is not None and cand.error is None

        assert cand.standard_missing_in_tenant == ("mobile",)
        assert "firstName" in cand.standard_present and "mobile" not in cand.standard_present

        unmapped = {u.field: u for u in cand.custom_unmapped}
        assert unmapped["customText3"].appears_configured is True
        assert unmapped["customText3"].label == "Clearance Level"
        assert unmapped["customText3"].options == (("S", "Secret"), ("TS", "Top Secret"))
        assert unmapped["customText9"].appears_configured is False
        assert unmapped["customEncryptedText1"].sensitive is True
        assert "customText7" not in unmapped

        mapped = {m.field: m for m in cand.custom_mapped}
        assert mapped["customText7"].canonical_fields == ("region",)

        broken = {b.canonical_field: b for b in cand.mappings_broken}
        assert broken["ghost"].origin == "profile_custom"
        assert broken["ghost"].missing_sources == ("customText99",)
        assert broken["skills"].origin == "profile_standard"
        assert broken["mobile"].origin == "catalog"

        assert [o.field for o in cand.other_unrecognized] == ["middleName"]

        job = report.entity("JobOrder")
        assert job is not None and job.error is not None and "BullhornAPIError" in job.error
        others = [e for e in report.entities if e.bullhorn_entity != "JobOrder"]
        assert all(e.error is None for e in others)
        assert len(others) == len(load_bullhorn_catalog().entities) - 1

        json.dumps(report.to_dict())  # JSON-safe

    def test_entity_selection(self):
        source = Mock(spec=MetaSource)
        source.get_entity_meta.return_value = EntityMeta("X", "X", ())
        d = _discoverer(source)
        report = d.discover(entities=["candidate", "JobOrder", "Candidate"])
        assert [e.bullhorn_entity for e in report.entities] == ["Candidate", "JobOrder"]
        assert [c.args for c in source.get_entity_meta.call_args_list] == [("Candidate",), ("JobOrder",)]
        with pytest.raises(SchemaError):
            d.discover(entities=["spaceship"])
        with pytest.raises(SchemaError):
            d.discover(entities="candidate")  # type: ignore[arg-type]

    def test_any_exception_is_per_entity(self):
        def meta(entity):
            if entity == "Note":
                raise RuntimeError("weird")
            return parse_entity_meta(entity, {"fields": [{"name": "id"}]})

        source = Mock(spec=MetaSource)
        source.get_entity_meta.side_effect = meta
        report = _discoverer(source).discover()
        assert report.entity("note").error == "RuntimeError: weird"
        assert sum(1 for e in report.entities if e.error) == 1

    def test_meta_warnings_surface(self):
        source = Mock(spec=MetaSource)
        source.get_entity_meta.return_value = parse_entity_meta("Candidate", {"fields": [1]})
        report = _discoverer(source).discover(entities=["candidate"])
        assert report.entities[0].warnings


class TestRedaction:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("john.smith@example.com", "a***@***"),
            ("555-123-4567", "***-***-****"),
            ("ID 12345 X", "ID ***** X"),
            ("abc", "***"),
            ("ab", "***"),
            ("", "***"),
            ("Secret", "Se***"),
            ("Top Secret", "To***"),
            (42, "<number>"),
            (3.5, "<number>"),
            (Decimal("1.5"), "<number>"),
            (1704067200000, "<number>"),
            (True, "<boolean>"),
            (False, "<boolean>"),
            (dt.date(2026, 1, 1), "<date>"),
            (dt.datetime(2026, 1, 1, 12), "<date>"),
            ({"id": 1}, "<object>"),
            ([1, 2], "<list>"),
        ],
    )
    def test_table(self, raw, expected):
        assert redact_sample(raw) == expected
        assert redact_sample(raw) != raw


class TestSampling:
    """AC-18."""

    def _source(self):
        return parse_entity_meta(
            "Candidate",
            {
                "fields": [
                    {"name": "customText1", "label": "Personal Email"},
                    {"name": "customText2", "label": "Alt Phone"},
                    {"name": "customText3", "label": "Clearance"},
                    {"name": "customText4", "label": "Tier"},
                    {"name": "customInt1", "label": "Years"},
                    {"name": "customEncryptedText1", "label": "Badge"},
                    {"name": "customDate1", "label": "Cleared On"},
                ]
            },
        )

    def test_samples_redacted_and_sensitive_never_requested(self):
        raw = {
            "customText1": "jane.doe@example.com",
            "customText2": "(555) 987-6543",
            "customText3": "Top Secret",
            "customText4": "A1",
            "customInt1": 12,
            "customEncryptedText1": "SHOULD-NEVER-BE-REQUESTED",
            "customDate1": 1704067200000,
        }
        requested: list = []

        def sample_source(entity, fields):
            requested.append((entity, list(fields)))
            return {k: v for k, v in raw.items() if k in fields}

        meta_source = Mock(spec=MetaSource)
        meta_source.get_entity_meta.return_value = self._source()
        report = _discoverer(meta_source).discover(entities=["candidate"], include_sample_values=True, sample_source=sample_source)
        assert len(requested) == 1
        entity, fields = requested[0]
        assert entity == "Candidate"
        assert "customEncryptedText1" not in fields
        assert all(not load_bullhorn_catalog().is_sensitive(f) for f in fields)

        samples = {u.field: u.sample for u in report.entities[0].custom_unmapped}
        assert samples["customEncryptedText1"] is None
        assert samples["customText1"] == "a***@***"
        assert samples["customText2"] == "(***) ***-****"
        assert samples["customText3"] == "To***"
        assert samples["customText4"] == "***"
        assert samples["customInt1"] == "<number>"
        assert samples["customDate1"] == "<number>"
        for name, value in samples.items():
            if value is not None:
                assert value != raw[name]
        dumped = json.dumps(report.to_dict())
        for secret in ("jane.doe", "987-6543", "Top Secret", "SHOULD-NEVER"):
            assert secret not in dumped

    def test_sample_source_failure_is_a_warning(self):
        meta_source = Mock(spec=MetaSource)
        meta_source.get_entity_meta.return_value = self._source()
        source = Mock(side_effect=RuntimeError("nope"))
        report = _discoverer(meta_source).discover(entities=["candidate"], include_sample_values=True, sample_source=source)
        assert report.entities[0].error is None
        assert any("sample fetch failed" in w for w in report.entities[0].warnings)

    def test_sample_source_not_called_when_disabled(self):
        meta_source = Mock(spec=MetaSource)
        meta_source.get_entity_meta.return_value = self._source()
        source = Mock()
        report = _discoverer(meta_source).discover(entities=["candidate"], sample_source=source)
        source.assert_not_called()
        assert all(u.sample is None for u in report.entities[0].custom_unmapped)


class TestDraftProfile:
    """AC-19."""

    def _report(self):
        meta_source = Mock(spec=MetaSource)

        def meta(entity):
            if entity == "Candidate":
                return parse_entity_meta("Candidate", CANDIDATE_META)
            if entity == "Note":
                raise RuntimeError("down")
            return parse_entity_meta(entity, {"fields": [{"name": "id"}, {"name": "customText2", "label": "Zed"}, {"name": "customText1"}]})

        meta_source.get_entity_meta.side_effect = meta
        return _discoverer(meta_source, _profile()).discover()

    def test_deterministic_and_valid(self):
        report = self._report()
        existing = _profile()
        a = build_draft_profile(report, "acme", existing)
        b = build_draft_profile(report, "acme", existing)
        assert a == b
        assert a.to_dict() == b.to_dict()
        assert MappingProfile.from_dict(a.to_dict()) == a

    def test_preserves_custom_and_overrides_only(self):
        existing = _profile()
        draft = build_draft_profile(self._report(), "acme", existing)
        cand = draft.entity("candidate")
        assert cand.custom == existing.entity("candidate").custom
        assert cand.standard == existing.entity("candidate").standard  # overrides only, no catalog defaults
        assert "first_name" not in cand.standard
        assert draft.entity("job").standard == {}

    def test_unmapped_sorted(self):
        draft = build_draft_profile(self._report(), "acme", _profile())
        names = [u.field for u in draft.entity("candidate").unmapped_bullhorn_fields]
        assert names == sorted(names)
        assert names == ["customEncryptedText1", "customInt4", "customText3", "customText9"]
        job_names = [u.field for u in draft.entity("job").unmapped_bullhorn_fields]
        assert job_names == ["customText1", "customText2"]
        clearance = next(u for u in draft.entity("candidate").unmapped_bullhorn_fields if u.field == "customText3")
        assert clearance.label == "Clearance Level"
        assert clearance.options == (("S", "Secret"), ("TS", "Top Secret"))

    def test_without_existing(self):
        draft = build_draft_profile(self._report(), "new-tenant")
        assert draft.tenant == "new-tenant"
        assert draft.generated_at is None
        assert all(not ep.custom and not ep.standard for ep in draft.entities.values())

    def test_errored_entity_keeps_existing_unmapped(self):
        existing = MappingProfile.from_dict(
            {"version": 1, "entities": {"note": {"unmapped_bullhorn_fields": ["customText5"]}}}
        )
        draft = build_draft_profile(self._report(), "acme", existing, generated_at="2026-10-06T00:00:00+00:00")
        assert [u.field for u in draft.entity("note").unmapped_bullhorn_fields] == ["customText5"]
        assert draft.generated_at == "2026-10-06T00:00:00+00:00"
