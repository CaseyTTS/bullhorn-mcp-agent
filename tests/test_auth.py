"""Tests for Bullhorn authentication."""

import time
import pytest
import httpx
import respx
from bullhorn_mcp.auth import BullhornAuth, BullhornSession, AuthenticationError


class TestBullhornSession:
    """Tests for BullhornSession class."""

    def test_session_creation(self):
        """Test creating a session."""
        session = BullhornSession(
            bh_rest_token="token123",
            rest_url="https://rest99.bullhornstaffing.com/rest-services/abc/",
            expires_at=time.time() + 600,
        )

        assert session.bh_rest_token == "token123"
        assert session.rest_url == "https://rest99.bullhornstaffing.com/rest-services/abc/"

    def test_session_expiry(self):
        """Test session expiry tracking."""
        # Session that expires in the future
        future_session = BullhornSession(
            bh_rest_token="token",
            rest_url="https://rest.example.com/",
            expires_at=time.time() + 600,
        )
        assert future_session.expires_at > time.time()

        # Session that has expired
        expired_session = BullhornSession(
            bh_rest_token="token",
            rest_url="https://rest.example.com/",
            expires_at=time.time() - 100,
        )
        assert expired_session.expires_at < time.time()


class TestBullhornAuth:
    """Tests for BullhornAuth class."""

    @respx.mock
    def test_full_auth_flow(self, sample_config):
        """Test complete authentication flow."""
        # Mock authorization endpoint - returns redirect with code
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=auth_code_123"},
            )
        )

        # Mock token endpoint
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "access_token_123",
                    "refresh_token": "refresh_token_123",
                    "expires_in": 600,
                },
            )
        )

        # Mock REST login endpoint
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "bh_rest_token_123",
                    "restUrl": "https://rest99.bullhornstaffing.com/rest-services/abc/",
                },
            )
        )

        auth = BullhornAuth(sample_config)
        session = auth.session

        assert session.bh_rest_token == "bh_rest_token_123"
        assert session.rest_url == "https://rest99.bullhornstaffing.com/rest-services/abc/"
        assert session.expires_at > time.time()

    @respx.mock
    def test_auth_code_error(self, sample_config):
        """Test handling of OAuth error response."""
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?error=invalid_client&error_description=Invalid%20client"},
            )
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "invalid_client" in str(exc_info.value)

    @respx.mock
    def test_token_exchange_failure(self, sample_config):
        """Test handling of token exchange failure."""
        # Mock successful auth code
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=auth_code_123"},
            )
        )

        # Mock failed token exchange
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                400,
                json={"error": "invalid_grant"},
            )
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "Token exchange failed" in str(exc_info.value)

    @respx.mock
    def test_rest_login_failure(self, sample_config):
        """Test handling of REST login failure."""
        # Mock successful auth code and token
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=auth_code_123"},
            )
        )
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 600},
            )
        )

        # Mock failed REST login
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "REST login failed" in str(exc_info.value)

    @respx.mock
    def test_session_caching(self, sample_config):
        """Test that session is cached and reused."""
        # Set up mocks
        auth_route = respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=code123"},
            )
        )
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 600},
            )
        )
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "cached_token",
                    "restUrl": "https://rest.example.com/",
                },
            )
        )

        auth = BullhornAuth(sample_config)

        # First access - should trigger auth
        session1 = auth.session
        assert auth_route.call_count == 1

        # Second access - should use cached session
        session2 = auth.session
        assert auth_route.call_count == 1  # Still 1, no new auth

        assert session1.bh_rest_token == session2.bh_rest_token

    @respx.mock
    def test_regional_redirect_307(self, sample_config):
        """Test handling of 307 redirect to regional Bullhorn server.

        When Bullhorn redirects to a regional server (307), we follow it.
        The regional URL is only captured when the redirect containing
        the auth code is to a Bullhorn domain.
        """
        # First request gets 307 redirect to regional server
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-apac.bullhornstaffing.com/oauth/authorize?client_id=test"},
            )
        )

        # Regional server returns auth code in redirect to external callback
        # (This is the typical OAuth flow - redirect to registered callback URL)
        respx.get("https://auth-apac.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=regional_code_123"},
            )
        )

        # Token exchange uses original auth URL (regional URL not captured
        # because callback.example.com is not a Bullhorn domain)
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "regional_access_token",
                    "refresh_token": "regional_refresh_token",
                    "expires_in": 600,
                },
            )
        )

        # REST login
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "bh_token_regional",
                    "restUrl": "https://rest-apac.bullhornstaffing.com/rest-services/abc/",
                },
            )
        )

        auth = BullhornAuth(sample_config)
        session = auth.session

        assert session.bh_rest_token == "bh_token_regional"
        # Regional URL is not set when callback is to external domain
        assert auth._regional_auth_url is None

    @respx.mock
    def test_regional_redirect_with_bullhorn_callback(self, sample_config):
        """Test that regional URL is captured when redirect contains Bullhorn domain."""
        # Redirect directly to regional Bullhorn domain with auth code
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://auth-apac.bullhornstaffing.com/callback?code=regional_code_123"},
            )
        )

        # Token exchange uses regional URL
        respx.post("https://auth-apac.bullhornstaffing.com/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "regional_access_token",
                    "refresh_token": "regional_refresh_token",
                    "expires_in": 600,
                },
            )
        )

        # REST login
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "bh_token_regional",
                    "restUrl": "https://rest-apac.bullhornstaffing.com/rest-services/abc/",
                },
            )
        )

        auth = BullhornAuth(sample_config)
        session = auth.session

        assert session.bh_rest_token == "bh_token_regional"
        assert auth._regional_auth_url == "https://auth-apac.bullhornstaffing.com"

    @respx.mock
    def test_auth_code_in_redirect_without_regional_domain(self, sample_config):
        """Test extracting auth code from non-Bullhorn callback URL."""
        # Auth code returned in redirect to external callback URL
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://myapp.example.com/callback?code=external_code_456"},
            )
        )

        # Token exchange (uses original auth URL since no regional redirect)
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "access_token",
                    "refresh_token": "refresh_token",
                    "expires_in": 600,
                },
            )
        )

        # REST login
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "bh_token",
                    "restUrl": "https://rest.bullhornstaffing.com/rest-services/abc/",
                },
            )
        )

        auth = BullhornAuth(sample_config)
        session = auth.session

        assert session.bh_rest_token == "bh_token"
        # Regional URL should not be set when callback is to external domain
        assert auth._regional_auth_url is None

    @respx.mock
    def test_refresh_token_uses_regional_url(self, sample_config):
        """Test that token refresh uses regional URL when set."""
        # Initial auth with regional redirect
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://auth-emea.bullhornstaffing.com/callback?code=code123"},
            )
        )

        # Token exchange at regional URL
        respx.post("https://auth-emea.bullhornstaffing.com/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "initial_token",
                    "refresh_token": "refresh_123",
                    "expires_in": 600,
                },
            )
        )

        # REST login
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={
                    "BhRestToken": "bh_token",
                    "restUrl": "https://rest.example.com/",
                },
            )
        )

        auth = BullhornAuth(sample_config)
        _ = auth.session  # Trigger initial auth

        # Verify regional URL was captured
        assert auth._regional_auth_url == "https://auth-emea.bullhornstaffing.com"

        # Now test refresh - should use regional URL
        refresh_route = respx.post("https://auth-emea.bullhornstaffing.com/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "refreshed_token",
                    "refresh_token": "new_refresh",
                    "expires_in": 600,
                },
            )
        )

        # Force a refresh
        auth._refresh_access_token()

        # Verify refresh was called at regional URL
        assert refresh_route.called
        assert auth._access_token == "refreshed_token"

    @respx.mock
    def test_no_auth_code_in_response(self, sample_config):
        """Test error when no auth code is returned."""
        # Response with no code parameter
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(200, text="Login page HTML")
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "Failed to get auth code" in str(exc_info.value)

    @respx.mock
    def test_redirect_to_untrusted_domain_without_code(self, sample_config):
        """Test that a redirect to a non-Bullhorn host with no code is not followed.

        If the authorize endpoint redirects somewhere that isn't a
        bullhornstaffing.com host and the redirect contains no auth code
        (and no OAuth error), the loop must not follow it (the `else: break`
        branch) and should raise AuthenticationError instead of chasing an
        arbitrary redirect target.
        """
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://not-bullhorn.example.com/login"},
            )
        )

        # If the (untrusted) redirect were followed, this route would be hit.
        untrusted_route = respx.get("https://not-bullhorn.example.com/login").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=should_not_be_used"},
            )
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "Failed to get auth code" in str(exc_info.value)
        assert not untrusted_route.called

    @respx.mock
    def test_max_redirects_exhausted(self, sample_config):
        """Test that exhausting max_redirects (5) without a terminal response raises.

        If every hop is a redirect to a Bullhorn domain with no auth code
        and no error, the loop runs out of attempts and falls through to
        raise AuthenticationError based on the last response's status code.
        """
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop1.bullhornstaffing.com/oauth/authorize"},
            )
        )
        respx.get("https://auth-hop1.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop2.bullhornstaffing.com/oauth/authorize"},
            )
        )
        respx.get("https://auth-hop2.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop3.bullhornstaffing.com/oauth/authorize"},
            )
        )
        respx.get("https://auth-hop3.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop4.bullhornstaffing.com/oauth/authorize"},
            )
        )
        respx.get("https://auth-hop4.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop5.bullhornstaffing.com/oauth/authorize"},
            )
        )
        respx.get("https://auth-hop5.bullhornstaffing.com/oauth/authorize").mock(
            return_value=httpx.Response(
                307,
                headers={"location": "https://auth-hop6.bullhornstaffing.com/oauth/authorize"},
            )
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "Failed to get auth code" in str(exc_info.value)
        assert "307" in str(exc_info.value)

    @respx.mock
    def test_rest_login_missing_token(self, sample_config):
        """Test error when REST login returns incomplete data."""
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://callback.example.com?code=code123"},
            )
        )
        respx.post(f"{sample_config.auth_url}/oauth/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 600},
            )
        )

        # REST login returns incomplete data
        respx.get(f"{sample_config.login_url}/rest-services/login").mock(
            return_value=httpx.Response(
                200,
                json={"someOtherField": "value"},  # Missing BhRestToken and restUrl
            )
        )

        auth = BullhornAuth(sample_config)

        with pytest.raises(AuthenticationError) as exc_info:
            _ = auth.session

        assert "Invalid login response" in str(exc_info.value)
