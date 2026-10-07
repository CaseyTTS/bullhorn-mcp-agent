"""Bullhorn OAuth 2.0 authentication handler."""

import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from ..config import BullhornConfig
from . import trusted_origins


@dataclass
class BullhornSession:
    """Active Bullhorn API session."""

    bh_rest_token: str
    rest_url: str
    expires_at: float  # Unix timestamp


class BullhornAuth:
    """Handles Bullhorn OAuth 2.0 authentication flow.

    Flow:
    1. Get authorization code (using username/password)
    2. Exchange auth code for access token
    3. REST login to get BhRestToken
    4. Use BhRestToken for API calls
    5. Refresh when tokens expire
    """

    def __init__(self, config: BullhornConfig):
        self.config = config
        self._session: BullhornSession | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0
        self._regional_auth_url: str | None = None  # Set if redirected to regional server

    @property
    def session(self) -> BullhornSession:
        """Get current session, refreshing if needed."""
        if self._session is None or time.time() >= self._session.expires_at - 60:
            self._refresh_session()
        return cast(BullhornSession, self._session)

    def _refresh_session(self) -> None:
        """Refresh the API session."""
        # If we have a refresh token, try to use it
        if self._refresh_token and time.time() < self._token_expires_at - 60:
            try:
                self._refresh_access_token()
            except Exception:
                # Refresh failed, do full auth
                self._full_auth()
        else:
            self._full_auth()

        # Now get REST session
        self._rest_login()

    def _full_auth(self) -> None:
        """Perform full OAuth authentication."""
        # Step 1: Get authorization code
        auth_code = self._get_auth_code()

        # Step 2: Exchange for access token
        self._exchange_auth_code(auth_code)

    def _get_auth_code(self) -> str:
        """Get authorization code using username/password."""
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "action": "Login",
            "username": self.config.username,
            "password": self.config.password,
        }

        url = f"{self.config.auth_url}/oauth/authorize?{urlencode(params)}"

        with httpx.Client(follow_redirects=False) as client:
            # May need to follow regional redirects (307 to auth-apac, auth-emea, etc.)
            max_redirects = 5
            for _ in range(max_redirects):
                response = client.get(url)

                if response.status_code not in (301, 302, 303, 307, 308):
                    break

                location = response.headers.get("location", "")
                if not location:
                    break

                parsed = urlparse(location)
                query_params = parse_qs(parsed.query)

                # Check if this redirect contains the auth code
                if "code" in query_params:
                    # Only update regional URL if redirected to a Bullhorn domain
                    if parsed.netloc and trusted_origins.is_trusted_bullhorn_url(parsed):
                        self._regional_auth_url = f"{parsed.scheme}://{parsed.netloc}"
                    return query_params["code"][0]

                # Check for OAuth errors
                if "error" in query_params:
                    error = query_params.get("error", ["unknown"])[0]
                    error_desc = query_params.get("error_description", [""])[0]
                    raise AuthenticationError(f"OAuth error: {error} - {error_desc}")

                # Only follow redirects to Bullhorn domains (regional servers)
                if trusted_origins.is_trusted_bullhorn_url(parsed):
                    url = location
                else:
                    # Non-Bullhorn redirect without code - something's wrong
                    break

            raise AuthenticationError(
                f"Failed to get auth code. Status: {response.status_code}"
            )

    def _exchange_auth_code(self, auth_code: str) -> None:
        """Exchange authorization code for access token."""
        auth_url = self._regional_auth_url or self.config.auth_url
        url = f"{auth_url}/oauth/token"
        params = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }

        with httpx.Client() as client:
            response = client.post(url, params=params)

            if response.status_code != 200:
                raise AuthenticationError(
                    f"Token exchange failed: {response.status_code} - {response.text}"
                )

            data = response.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token")

            # Access tokens expire in 10 minutes (600 seconds)
            expires_in = data.get("expires_in", 600)
            self._token_expires_at = time.time() + expires_in

    def _refresh_access_token(self) -> None:
        """Refresh the access token using refresh token."""
        if not self._refresh_token:
            raise AuthenticationError("No refresh token available")

        auth_url = self._regional_auth_url or self.config.auth_url
        url = f"{auth_url}/oauth/token"
        params = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }

        with httpx.Client() as client:
            response = client.post(url, params=params)

            if response.status_code != 200:
                raise AuthenticationError(
                    f"Token refresh failed: {response.status_code}"
                )

            data = response.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)

            expires_in = data.get("expires_in", 600)
            self._token_expires_at = time.time() + expires_in

    def _rest_login(self) -> None:
        """Perform REST login to get BhRestToken."""
        if not self._access_token:
            raise AuthenticationError("No access token available")

        url = f"{self.config.login_url}/rest-services/login"
        params = {
            "version": "*",
            "access_token": self._access_token,
        }

        with httpx.Client() as client:
            response = client.get(url, params=params)

            if response.status_code != 200:
                raise AuthenticationError(
                    f"REST login failed: {response.status_code} - {response.text}"
                )

            data = response.json()

            if "BhRestToken" not in data or "restUrl" not in data:
                raise AuthenticationError(f"Invalid login response: {data}")

            # Session typically valid for 10 minutes
            self._session = BullhornSession(
                bh_rest_token=data["BhRestToken"],
                rest_url=data["restUrl"],
                expires_at=time.time() + 600,
            )


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    pass
