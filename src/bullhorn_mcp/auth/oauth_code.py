"""Bullhorn OAuth 2.0 authorization-code client (Phase 5A, D-5A-4).

Verified mechanisms only (``docs/architecture/PHASE5A_HV_VERIFICATION.md``):

- HV-C1: ``GET {auth}/oauth/authorize?client_id&response_type=code&redirect_uri&state``.
  The browser is sent there; this server never sees a password or an MFA code.
- HV-C3: ``POST {auth}/oauth/token`` with the ``authorization_code`` grant (with
  the same ``redirect_uri``) and the ``refresh_token`` grant, parameters in the
  query string as documented. The response field names are not shown in the
  Bullhorn documentation, so the RFC 6749 §5.1 names are parsed **strictly**:
  anything unexpected fails closed. Refresh tokens rotate (a new one with every
  access token), so the caller must store the returned one.
- HV-C4: ``POST {rest}/rest-services/login?version=*&access_token`` returns
  ``BhRestToken`` and ``restUrl`` (``.../rest-services/{corpToken}/``).
- HV-C8: a 307/308 to another data center is followed (at most twice) **only**
  to an ``https`` host trusted by ``TrustedOriginPolicy`` and only to the same
  endpoint path; everything else is refused.
- HV-C6 (revocation) and HV-C7 (PKCE) are undocumented: neither is used.
- HV-C10: nothing here is retried (logins are rate limited; codes are single use).

No error message ever contains a response body, a code, a token or a secret.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .bullhorn_password import AuthenticationError
from .trusted_origins import DEFAULT_POLICY, TrustedOriginPolicy

DEFAULT_ACCESS_TOKEN_SECONDS = 600  # HV-C3: "The access token is valid for 10 minutes."
MAX_EXPIRES_IN = 86_400
MAX_TOKEN_CHARS = 8192
MAX_REDIRECT_HOPS = 2
_CORP_TOKEN_RE = re.compile(r"[A-Za-z0-9]{1,64}", re.ASCII)
_REST_PATH_RE = re.compile(r"/rest-services/([A-Za-z0-9]{1,64})/?", re.ASCII)


class OAuthFlowError(AuthenticationError):
    """A bounded, secret-free reason code for an OAuth/login failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason[:80]
        super().__init__(f"Bullhorn authorization failed ({self.reason})")


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int
    auth_origin: str  # the https origin that issued the tokens (used again for refresh)


@dataclass(frozen=True)
class RestSession:
    bh_rest_token: str = field(repr=False)
    rest_url: str = field(repr=False)


def corp_token(rest_url: object, policy: TrustedOriginPolicy = DEFAULT_POLICY) -> str | None:
    """The corpToken of a trusted ``https://rest*.bullhornstaffing.com/rest-services/{corpToken}/`` URL."""
    if not policy.is_trusted_url(rest_url):
        return None
    assert isinstance(rest_url, str)
    parts = urlsplit(rest_url)
    if parts.query:
        return None
    m = _REST_PATH_RE.fullmatch(parts.path)
    return m.group(1) if m and _CORP_TOKEN_RE.fullmatch(m.group(1)) else None


def tenant_key_for_rest_url(rest_url: object, policy: TrustedOriginPolicy = DEFAULT_POLICY) -> str | None:
    """``sha256(corpToken)`` (D-5A-14), or ``None`` for an untrusted or malformed ``restUrl``."""
    token = corp_token(rest_url, policy)
    return hashlib.sha256(token.encode("ascii")).hexdigest() if token is not None else None


def _token_string(data: dict[str, Any], name: str, required: bool) -> str | None:
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_CHARS or not value.isprintable():
        raise OAuthFlowError("token_response_invalid")
    return value


def parse_token_response(data: object, auth_origin: str) -> TokenSet:
    """Strict RFC 6749 §5.1 parsing (HV-C3). Fails closed on any unexpected shape."""
    if not isinstance(data, dict):
        raise OAuthFlowError("token_response_invalid")
    access = _token_string(data, "access_token", True)
    refresh = _token_string(data, "refresh_token", False)
    raw = data.get("expires_in", DEFAULT_ACCESS_TOKEN_SECONDS)
    if type(raw) is not int or not 1 <= raw <= MAX_EXPIRES_IN:
        raise OAuthFlowError("token_response_invalid")
    assert access is not None
    return TokenSet(access, refresh, raw, auth_origin)


class OAuthCodeClient:
    """One Bullhorn OAuth client (one tenant's API key)."""

    def __init__(
        self,
        client_id: str,
        client_secret: Callable[[], str],
        redirect_uri: str,
        auth_url: str = "https://auth.bullhornstaffing.com",
        login_url: str = "https://rest.bullhornstaffing.com",
        policy: TrustedOriginPolicy = DEFAULT_POLICY,
        timeout: float = 20.0,
    ) -> None:
        auth_origin = policy.origin(auth_url)
        login_origin = policy.origin(login_url)
        if auth_origin is None or login_origin is None:
            raise ValueError("auth_url and login_url must be trusted https Bullhorn URLs")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.auth_origin = auth_origin
        self.login_origin = login_origin
        self.policy = policy
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"OAuthCodeClient(auth_origin={self.auth_origin!r})"

    # ------------------------------------------------------------------ #

    def authorize_url(self, state: str) -> str:
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri, "state": state}
        return f"{self.auth_origin}/oauth/authorize?{urlencode(params)}"

    def _post(self, origin: str, path: str, params: dict[str, str]) -> tuple[httpx.Response, str]:
        """POST with query parameters; follow a 307/308 only to a trusted https origin and the same path."""
        current = origin
        with httpx.Client(follow_redirects=False, timeout=self.timeout) as client:
            for _ in range(MAX_REDIRECT_HOPS + 1):
                try:
                    response = client.post(f"{current}{path}", params=params)
                except httpx.HTTPError:
                    raise OAuthFlowError("network_error") from None
                if response.status_code not in (307, 308):
                    return response, current
                location = response.headers.get("location", "")
                target = self.policy.origin(location)
                if target is None or urlsplit(location).path != path:
                    raise OAuthFlowError("untrusted_redirect")
                current = target
        raise OAuthFlowError("too_many_redirects")

    def _token_request(self, params: dict[str, str], origin: str) -> TokenSet:
        response, used = self._post(origin, "/oauth/token", params)
        if response.status_code != 200:
            raise OAuthFlowError(f"token_endpoint_status_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise OAuthFlowError("token_response_invalid") from None
        return parse_token_response(data, used)

    def exchange_code(self, code: str) -> TokenSet:
        """The authorization-code grant (HV-C3), at the configured auth host (HV-C8)."""
        if not isinstance(code, str) or not code or len(code) > MAX_TOKEN_CHARS or not code.isprintable():
            raise OAuthFlowError("invalid_code")
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self._client_secret(),
            "redirect_uri": self.redirect_uri,
        }
        return self._token_request(params, self.auth_origin)

    def refresh(self, refresh_token: str, auth_origin: str | None = None) -> TokenSet:
        """The refresh-token grant (HV-C3). ``auth_origin`` must itself be trusted."""
        origin = self.policy.origin(auth_origin) if auth_origin else self.auth_origin
        if origin is None:
            raise OAuthFlowError("untrusted_auth_origin")
        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self._client_secret(),
        }
        return self._token_request(params, origin)

    def rest_login(self, access_token: str) -> RestSession:
        """REST login (HV-C4): ``BhRestToken`` and a trusted ``restUrl``."""
        response, _ = self._post(self.login_origin, "/rest-services/login", {"version": "*", "access_token": access_token})
        if response.status_code != 200:
            raise OAuthFlowError(f"login_status_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise OAuthFlowError("login_response_invalid") from None
        if not isinstance(data, dict):
            raise OAuthFlowError("login_response_invalid")
        token = data.get("BhRestToken")
        rest_url = data.get("restUrl")
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS or not token.isprintable():
            raise OAuthFlowError("login_response_invalid")
        if corp_token(rest_url, self.policy) is None:
            raise OAuthFlowError("untrusted_rest_url")
        assert isinstance(rest_url, str)
        return RestSession(token, rest_url)

    def ping(self, rest: RestSession) -> int | None:
        """``GET {restUrl}ping`` -> ``sessionExpires`` (epoch ms), or ``None`` (HV-C4). Never raises."""
        url = rest.rest_url if rest.rest_url.endswith("/") else rest.rest_url + "/"
        try:
            with httpx.Client(follow_redirects=False, timeout=self.timeout) as client:
                response = client.get(f"{url}ping", headers={"BhRestToken": rest.bh_rest_token})
            data = response.json() if response.status_code == 200 else None
        except Exception:  # informational only: any failure means "unknown"
            return None
        value = data.get("sessionExpires") if isinstance(data, dict) else None
        return value if type(value) is int and value > 0 else None
