"""Tests for MetaDiscovery over the unchanged BullhornClient.get_meta()."""

from unittest.mock import Mock, PropertyMock

import httpx
import pytest
import respx

from bullhorn_mcp.auth import AuthenticationError, BullhornAuth
from bullhorn_mcp.bullhorn.client import BullhornAPIError, BullhornClient
from bullhorn_mcp.bullhorn.meta import EntityMeta, MetaDiscovery, MetaSource, parse_entity_meta


@pytest.fixture
def mock_auth(mock_session):
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


@pytest.fixture
def discovery(mock_auth):
    return MetaDiscovery(BullhornClient(mock_auth))


def _mock_meta(mock_session, entity, payload, status=200):
    return respx.get(f"{mock_session.rest_url}/meta/{entity}").mock(return_value=httpx.Response(status, json=payload))


class TestMetaDiscovery:
    def test_implements_meta_source(self, discovery):
        assert isinstance(discovery, MetaSource)

    @respx.mock
    def test_parses_fields(self, discovery, mock_session):
        _mock_meta(
            mock_session,
            "Candidate",
            {
                "entity": "Candidate",
                "label": "Candidate",
                "fields": [
                    {"name": "id", "type": "ID", "dataType": "Integer"},
                    {
                        "name": "status",
                        "type": "SCALAR",
                        "dataType": "String",
                        "label": "Status",
                        "required": True,
                        "readOnly": False,
                        "options": [{"value": "Active", "label": "Active"}, {"value": "DNU", "label": "Do Not Use"}],
                    },
                    {"name": "owner", "type": "TO_ONE", "label": "Owner", "associatedEntity": {"entity": "CorporateUser"}},
                    {"name": "dateAdded", "type": "SCALAR", "dataType": "Timestamp", "label": "Date Added", "readonly": True},
                ],
            },
        )
        meta = discovery.get_entity_meta("Candidate")
        assert isinstance(meta, EntityMeta)
        assert meta.field_names == ("id", "status", "owner", "dateAdded")
        status = meta.get_field("status")
        assert status is not None
        assert status.required is True and status.read_only is False
        assert status.options == (("Active", "Active"), ("DNU", "Do Not Use"))
        assert meta.get_field("owner").associated_entity == "CorporateUser"
        assert meta.get_field("owner").field_type == "TO_ONE"
        assert meta.get_field("dateAdded").read_only is True
        assert meta.get_field("id").options is None

    @respx.mock
    def test_request_is_unchanged_get_meta(self, discovery, mock_session):
        """AM-1: fields=* only, no meta parameter, path /meta/{entity}."""
        route = _mock_meta(mock_session, "JobOrder", {"fields": []})
        discovery.get_entity_meta("JobOrder")
        assert route.call_count == 1
        for call in respx.calls:
            req = call.request
            assert req.url.path.endswith("/meta/JobOrder")
            assert dict(req.url.params) == {"fields": "*"}
            assert "meta" not in req.url.params

    @respx.mock
    def test_cache_hits_network_once(self, discovery, mock_session):
        route = _mock_meta(mock_session, "Candidate", {"fields": [{"name": "id"}]})
        first = discovery.get_entity_meta("Candidate")
        second = discovery.get_entity_meta("Candidate")
        assert first is second
        assert route.call_count == 1
        discovery.clear_cache()
        discovery.get_entity_meta("Candidate")
        assert route.call_count == 2

    @respx.mock
    def test_api_error_propagates(self, discovery, mock_session):
        _mock_meta(mock_session, "Candidate", {"errorMessage": "boom"}, status=500)
        with pytest.raises(BullhornAPIError):
            discovery.get_entity_meta("Candidate")

    def test_auth_error_propagates(self):
        client = Mock(spec=BullhornClient)
        client.get_meta.side_effect = AuthenticationError("no token")
        with pytest.raises(AuthenticationError):
            MetaDiscovery(client).get_entity_meta("Candidate")

    def test_calls_get_meta_with_entity_only(self):
        client = Mock(spec=BullhornClient)
        client.get_meta.return_value = {"fields": []}
        MetaDiscovery(client).get_entity_meta("Note")
        client.get_meta.assert_called_once_with("Note")


class TestHostileMeta:
    @respx.mock
    @pytest.mark.parametrize(
        "payload,warning_fragment,expected_names",
        [
            ({"fields": []}, None, ()),
            ({"entity": "Candidate"}, "no 'fields'", ()),
            ({"fields": "nope"}, "not a list", ()),
            ({"fields": {"name": "id"}}, "not a list", ()),
            ({"fields": [1, "x", None, {"name": "id"}]}, "is not an object", ("id",)),
            ({"fields": [{"label": "No name"}, {"name": ""}, {"name": 5}, {"name": "id"}]}, "has no name", ("id",)),
            ({"fields": [{"name": "id", "label": "A"}, {"name": "id", "label": "B"}]}, "duplicate field 'id'", ("id",)),
            ({"fields": [{"name": "email", "label": None}]}, "null label", ("email",)),
            ({"fields": [{"name": "email", "dataType": "Quaternion"}]}, "unknown dataType 'Quaternion'", ("email",)),
            ({"fields": [{"name": "status", "options": "Active,Inactive"}]}, "malformed options", ("status",)),
            ({"fields": [{"name": "status", "options": [{"label": "no value"}]}]}, "malformed options", ("status",)),
            ({"fields": [{"name": "status", "options": [{"value": {"x": 1}}]}]}, "malformed options", ("status",)),
        ],
    )
    def test_hostile_shapes(self, mock_auth, mock_session, payload, warning_fragment, expected_names):
        _mock_meta(mock_session, "Candidate", payload)
        meta = MetaDiscovery(BullhornClient(mock_auth)).get_entity_meta("Candidate")
        assert meta.field_names == expected_names
        if warning_fragment is None:
            assert meta.warnings == ()
        else:
            assert any(warning_fragment in w for w in meta.warnings), meta.warnings

    def test_duplicate_first_wins(self):
        meta = parse_entity_meta("X", {"fields": [{"name": "id", "label": "A"}, {"name": "id", "label": "B"}]})
        assert meta.get_field("id").label == "A"

    def test_null_label_falls_back_to_name(self):
        meta = parse_entity_meta("X", {"fields": [{"name": "email", "label": None}]})
        assert meta.get_field("email").label == "email"

    def test_unknown_data_type_kept(self):
        meta = parse_entity_meta("X", {"fields": [{"name": "e", "dataType": "Quaternion"}]})
        assert meta.get_field("e").data_type == "Quaternion"

    def test_malformed_options_become_none(self):
        meta = parse_entity_meta("X", {"fields": [{"name": "s", "options": 7}]})
        assert meta.get_field("s").options is None

    @pytest.mark.parametrize("response", [None, [], "x", 5])
    def test_non_object_response(self, response):
        meta = parse_entity_meta("X", response)
        assert meta.fields == ()
        assert meta.warnings

    @respx.mock
    def test_thousand_fields(self, mock_auth, mock_session):
        fields = [{"name": f"customText{i}", "label": f"L{i}", "dataType": "String", "type": "SCALAR"} for i in range(1000)]
        route = _mock_meta(mock_session, "Candidate", {"fields": fields})
        meta = MetaDiscovery(BullhornClient(mock_auth)).get_entity_meta("Candidate")
        assert len(meta.fields) == 1000
        assert meta.warnings == ()
        assert route.call_count == 1
