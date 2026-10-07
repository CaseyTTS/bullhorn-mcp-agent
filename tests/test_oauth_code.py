"""The Bullhorn authorization-code client (Phase 5A; HV-C1/C3/C4/C8 guards)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from bullhorn_mcp.auth.oauth_code import (
    OAuthCodeClient,
    OAuthFlowError,
    RestSession,
    corp_token,
    parse_token_response,
    tenant_key_for_rest_url,
)

from ._identity_helpers import AUTH, LOGIN, REST_1, TK1

SECRET = "sentinel-oauth-client-secret"
CODE = "sentinel-auth-code-123"
REDIRECT = "https://mcp.example.test/oauth/bullhorn/callback"


def client(**kw):
    return OAuthCodeClient("cid", lambda: SECRET, REDIRECT, auth_url=kw.get("auth_url", AUTH), login_url=LOGIN)


def token_json(**over):
    data = {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 600, "token_type": "Bearer"}
    data.update(over)
    return data


class TestAuthorizeUrl:
    def test_parameters(self):
        url = client().authorize_url("state-xyz")
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{AUTH}/oauth/authorize"
        q = parse_qs(parts.query)
        assert q == {"client_id": ["cid"], "response_type": ["code"], "redirect_uri": [REDIRECT], "state": ["state-xyz"]}
        assert "password" not in url and "username" not in url and "action" not in url

    @pytest.mark.parametrize("url", ["http://auth.bullhornstaffing.com", "https://auth.bullhornstaffing.com.attacker.example"])
    def test_untrusted_auth_url_refused(self, url):
        with pytest.raises(ValueError):
            client(auth_url=url)


class TestExchange:
    @respx.mock
    def test_code_grant(self):
        route = respx.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(200, json=token_json()))
        tokens = client().exchange_code(CODE)
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in, tokens.auth_origin) == ("acc-1", "ref-1", 600, AUTH)
        q = parse_qs(urlsplit(str(route.calls[0].request.url)).query)
        assert q["grant_type"] == ["authorization_code"] and q["code"] == [CODE] and q["redirect_uri"] == [REDIRECT]
        assert "acc-1" not in repr(tokens) and "ref-1" not in repr(tokens)

    @respx.mock
    def test_refresh_grant_rotates(self):
        route = respx.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(200, json=token_json(refresh_token="ref-2")))
        tokens = client().refresh("ref-1", AUTH)
        assert tokens.refresh_token == "ref-2"
        q = parse_qs(urlsplit(str(route.calls[0].request.url)).query)
        assert q["grant_type"] == ["refresh_token"] and q["refresh_token"] == ["ref-1"]

    def test_refresh_at_untrusted_origin_refused(self):
        with respx.mock(assert_all_called=False) as router:
            with pytest.raises(OAuthFlowError, match="untrusted_auth_origin"):
                client().refresh("ref-1", "https://attacker.example")
            assert not router.calls

    @respx.mock
    def test_307_to_trusted_data_center_followed(self):
        respx.post(f"{AUTH}/oauth/token").mock(
            return_value=httpx.Response(307, headers={"location": "https://auth-west.bullhornstaffing.com/oauth/token?x=1"})
        )
        west = respx.post("https://auth-west.bullhornstaffing.com/oauth/token").mock(return_value=httpx.Response(200, json=token_json()))
        tokens = client().exchange_code(CODE)
        assert west.called and tokens.auth_origin == "https://auth-west.bullhornstaffing.com"

    @pytest.mark.parametrize(
        "location",
        [
            "https://bullhornstaffing.com.attacker.example/oauth/token",
            "http://auth-west.bullhornstaffing.com/oauth/token",
            "https://auth-west.bullhornstaffing.com/elsewhere",
        ],
    )
    def test_307_to_untrusted_target_refused(self, location):
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(307, headers={"location": location}))
            with pytest.raises(OAuthFlowError, match="untrusted_redirect"):
                client().exchange_code(CODE)
            assert len(router.calls) == 1

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"access_token": ""},
            {"access_token": 5},
            token_json(expires_in="600"),
            token_json(expires_in=True),
            token_json(expires_in=0),
            token_json(refresh_token=7),
            ["access_token"],
        ],
    )
    def test_strict_parsing_fails_closed(self, body):
        with pytest.raises(OAuthFlowError, match="token_response_invalid"):
            parse_token_response(body, AUTH)

    def test_missing_expires_in_uses_documented_ten_minutes(self):
        assert parse_token_response({"access_token": "a"}, AUTH).expires_in == 600

    @respx.mock
    def test_errors_never_echo_bodies_codes_or_secrets(self):
        respx.post(f"{AUTH}/oauth/token").mock(return_value=httpx.Response(400, text=f"bad {CODE} {SECRET}"))
        with pytest.raises(OAuthFlowError) as info:
            client().exchange_code(CODE)
        text = str(info.value) + repr(info.value)
        assert CODE not in text and SECRET not in text and "400" in text

    @respx.mock
    def test_network_error(self):
        respx.post(f"{AUTH}/oauth/token").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(OAuthFlowError, match="network_error"):
            client().exchange_code(CODE)


class TestRestLogin:
    @respx.mock
    def test_login(self):
        respx.post(f"{LOGIN}/rest-services/login").mock(
            return_value=httpx.Response(200, json={"BhRestToken": "bh-1", "restUrl": REST_1 + "/"})
        )
        rest = client().rest_login("acc-1")
        assert rest.bh_rest_token == "bh-1" and tenant_key_for_rest_url(rest.rest_url) == TK1
        assert "bh-1" not in repr(rest)

    @pytest.mark.parametrize(
        "body",
        [
            {"BhRestToken": "bh-1", "restUrl": "https://rest99.bullhornstaffing.com.attacker.example/rest-services/abc123/"},
            {"BhRestToken": "bh-1", "restUrl": "http://rest99.bullhornstaffing.com/rest-services/abc123/"},
            {"BhRestToken": "bh-1", "restUrl": "https://rest99.bullhornstaffing.com/elsewhere/abc123/"},
            {"BhRestToken": "bh-1"},
            {"restUrl": REST_1},
            {"BhRestToken": 1, "restUrl": REST_1},
        ],
    )
    @respx.mock
    def test_untrusted_or_malformed_refused(self, body):
        respx.post(f"{LOGIN}/rest-services/login").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(OAuthFlowError):
            client().rest_login("acc-1")

    @respx.mock
    def test_ping(self):
        respx.get(f"{REST_1}/ping").mock(return_value=httpx.Response(200, json={"sessionExpires": 1_900_000_000_000}))
        assert client().ping(RestSession("bh-1", REST_1)) == 1_900_000_000_000
        respx.get(f"{REST_1}/ping").mock(return_value=httpx.Response(401))
        assert client().ping(RestSession("bh-1", REST_1)) is None


class TestCorpToken:
    def test_extraction(self):
        assert corp_token(REST_1) == "abc123"
        assert corp_token(REST_1 + "/") == "abc123"
        assert corp_token("https://rest99.bullhornstaffing.com/rest-services/abc123/entity") is None
        assert corp_token("https://evil.example/rest-services/abc123/") is None
        assert corp_token(None) is None
        assert tenant_key_for_rest_url(REST_1) == TK1
