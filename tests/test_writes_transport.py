"""Phase 4B D-4B-13 / §1.5: the write transport (bullhorn/writes.py) and secret redaction."""

import json
from unittest.mock import Mock

import httpx
import pytest
import respx

from bullhorn_mcp.bullhorn.errors import BullhornAPIError
from bullhorn_mcp.bullhorn.writes import EntityWriter, WriteOutcomeUnknown, redact_secrets, safe_error_text

from ._notes_helpers import TOKEN, make_client
from ._tenant_helpers import REST_URL

CREATED = {"changedEntityId": 1, "changeType": "INSERT"}


class TestCreate:
    @respx.mock
    def test_put_json_body(self):
        route = respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(200, json=CREATED))
        assert EntityWriter(make_client()).create("Note", {"action": "x", "comments": "y"}) == CREATED
        req = route.calls[0].request
        assert json.loads(req.content) == {"action": "x", "comments": "y"} and req.headers["BhRestToken"] == TOKEN

    @respx.mock
    def test_retries_once_on_401(self):
        client = make_client()
        route = respx.put(f"{REST_URL}/entity/Note").mock(side_effect=[httpx.Response(401), httpx.Response(200, json=CREATED)])
        EntityWriter(client).create("Note", {})
        assert route.call_count == 2 and client.auth._refresh_session.call_count == 1

    @respx.mock
    def test_second_401_fails(self):
        route = respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(401, text="no"))
        with pytest.raises(BullhornAPIError):
            EntityWriter(make_client()).create("Note", {})
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
    def test_never_retries_otherwise(self, status):
        route = respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(status, text="x" * 50_000))
        with pytest.raises(BullhornAPIError) as exc:
            EntityWriter(make_client()).create("Note", {})
        assert route.call_count == 1 and len(safe_error_text(exc.value)) <= 300

    @respx.mock
    @pytest.mark.parametrize("body", ["not json", "[1,2]", '"ok"', "[]", "null", "<html>ok</html>"])
    def test_non_object_200_is_outcome_unknown(self, body):
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(200, text=body))
        with pytest.raises(WriteOutcomeUnknown):
            EntityWriter(make_client()).create("Note", {})

    @pytest.mark.parametrize("entity", ["Note/1", "../x", "", "Note?x=1", 5])
    def test_bad_entity_names(self, entity):
        with pytest.raises(ValueError):
            EntityWriter(Mock(spec=["auth"])).create(entity, {})


class TestAssociateAndRead:
    @respx.mock
    def test_associate_path(self):
        route = respx.put(f"{REST_URL}/entity/Note/5/candidates/1,2").mock(return_value=httpx.Response(200, json={}))
        EntityWriter(make_client()).associate("Note", 5, "candidates", [1, 2])
        assert route.call_count == 1

    @pytest.mark.parametrize("ids", [[], [0], ["1"], [True], list(range(1, 12))])
    def test_associate_bad_ids(self, ids):
        with pytest.raises(ValueError):
            EntityWriter(Mock(spec=["auth"])).associate("Note", 5, "candidates", ids)

    @respx.mock
    def test_fetch_to_many(self):
        route = respx.get(f"{REST_URL}/entity/JobOrder/7/notes").mock(return_value=httpx.Response(200, json={"data": []}))
        EntityWriter(make_client()).fetch_to_many("JobOrder", 7, "notes", "id,action", 0, 21)
        assert dict(route.calls[0].request.url.params) == {"fields": "id,action", "start": "0", "count": "21"}

    @pytest.mark.parametrize("fields", ["id,action&where=x", "id action", ""])
    def test_fetch_bad_fields(self, fields):
        with pytest.raises(ValueError):
            EntityWriter(Mock(spec=["auth"])).fetch_to_many("JobOrder", 7, "notes", fields, 0, 1)


class TestRedaction:
    @pytest.mark.parametrize(
        "text,secret",
        [
            ('{"BhRestToken":"abc123secretvalue"}', "abc123secretvalue"),
            ("BhRestToken=abc123secretvalue&x=1", "abc123secretvalue"),
            ("access_token: 'zzzSECRETzzz'", "zzzSECRETzzz"),
            ('"refresh_token" : "rrrSECRETrrr"', "rrrSECRETrrr"),
            ("password=hunter2", "hunter2"),
            ("bhresttoken " + "Q" * 40, "Q" * 40),
            ('{"bh_rest_token": "underscored-secret"}', "underscored-secret"),
        ],
    )
    def test_redacts(self, text, secret):
        out = redact_secrets(text)
        assert secret not in out and "***" in out

    def test_bounded(self):
        assert len(safe_error_text("e" * 100_000)) <= 300

    def test_truncation_does_not_expose_partial_secret(self):
        text = "x" * 270 + '"BhRestToken":"' + "S" * 60 + '"'
        assert "SSSS" not in safe_error_text(text)
