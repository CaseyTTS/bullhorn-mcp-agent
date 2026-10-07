"""Phase 5B: the read-only settings transport (bullhorn/settings_reader.py, D-5B-1, HV-D1/HV-D3)."""

import re
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx

from bullhorn_mcp.bullhorn.errors import BullhornAPIError
from bullhorn_mcp.bullhorn.settings_reader import MAX_BODY_BYTES, SettingsReader

from ._notes_helpers import TOKEN, make_client
from ._tenant_helpers import REST_URL

URL = f"{REST_URL}/settings/commentActionList"
BODY = {"commentActionList": ["Test Action A", "Test Action B"]}
SOURCE = Path(__file__).resolve().parents[1] / "src" / "bullhorn_mcp" / "bullhorn" / "settings_reader.py"


class TestGet:
    @respx.mock
    def test_get_path_and_token(self):
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
        assert SettingsReader(make_client()).get(["commentActionList"]) == BODY
        req = route.calls[0].request
        assert req.method == "GET" and req.headers["BhRestToken"] == TOKEN and not req.content

    @respx.mock
    def test_several_names_comma_joined(self):
        route = respx.get(f"{REST_URL}/settings/commentActionList,currencyFormat").mock(return_value=httpx.Response(200, json={}))
        SettingsReader(make_client()).get(["commentActionList", "currencyFormat"])
        assert route.call_count == 1

    @respx.mock
    def test_retries_once_on_401(self):
        client = make_client()
        route = respx.get(URL).mock(side_effect=[httpx.Response(401), httpx.Response(200, json=BODY)])
        SettingsReader(client).get(["commentActionList"])
        assert route.call_count == 2 and client.auth._refresh_session.call_count == 1

    @respx.mock
    def test_second_401_fails(self):
        route = respx.get(URL).mock(return_value=httpx.Response(401, text="denied"))
        with pytest.raises(BullhornAPIError):
            SettingsReader(make_client()).get(["commentActionList"])
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
    def test_non_200_bounded_and_body_never_echoed(self, status):
        body = '{"errorMessage":"Test Action Secret","BhRestToken":"' + TOKEN + '"}' + "x" * 50_000
        route = respx.get(URL).mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(BullhornAPIError) as info:
            SettingsReader(make_client()).get(["commentActionList"])
        rendered = str(info.value) + repr(info.value) + repr(info.value.args)
        assert route.call_count == 1 and len(str(info.value)) <= 300
        assert "Test Action Secret" not in rendered and TOKEN not in rendered and str(status) in str(info.value)

    @respx.mock
    @pytest.mark.parametrize("body", ["not json", "[1,2]", '"ok"', "null", "<html>x</html>"])
    def test_non_object_200_fails(self, body):
        respx.get(URL).mock(return_value=httpx.Response(200, text=body))
        with pytest.raises(BullhornAPIError):
            SettingsReader(make_client()).get(["commentActionList"])

    @respx.mock
    def test_oversized_body_fails(self):
        big = '{"commentActionList": ["' + "A" * (MAX_BODY_BYTES + 10) + '"]}'
        respx.get(URL).mock(return_value=httpx.Response(200, text=big))
        with pytest.raises(BullhornAPIError) as info:
            SettingsReader(make_client()).get(["commentActionList"])
        assert len(str(info.value)) <= 300

    @pytest.mark.parametrize(
        "names",
        [[], ["comment/../x"], ["a,b"], ["../entity/Note"], [""], ["x" * 65], [5], "commentActionList", ["n"] * 11, ["a b"]],
    )
    def test_bad_names_rejected_without_http(self, names):
        with pytest.raises(ValueError):
            SettingsReader(Mock(spec=[])).get(names)


class TestSourceGreps:
    def test_only_get_requests(self):
        text = SOURCE.read_text(encoding="utf-8")
        assert re.findall(r"http\.(\w+)\(", text) == ["get", "get"]
        assert "_request" not in text and ".post(" not in text and ".put(" not in text and ".delete(" not in text
