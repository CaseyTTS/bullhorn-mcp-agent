"""MCP ``TokenVerifier`` adapters (Phase 5A, D-5A-3; HV-M1/HV-M2).

* ``JwksTokenVerifier``: an asymmetric-signature JWT with required ``iss``
  (equal to an allowed issuer), ``aud`` (containing ``resource_server_url``),
  ``exp`` and ``sub``. A wrong key, a tampered payload, an expired token or
  another audience is rejected (``None``).
* ``IntrospectionTokenVerifier``: RFC 7662 introspection over ``httpx``; the
  response must be ``active``, with ``sub``, an allowed ``iss``, an ``aud`` that
  includes ``resource_server_url``, and an unexpired ``exp``.

``AccessToken.claims`` carries **only** ``iss``, the configured display claims and
the configured tenant claim. Every other claim (for example a crafted
``access_tier``) is dropped here and so can never influence identity or tier.
Neither adapter logs the token or any claim value.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

import anyio
import httpx
import jwt
from jwt import PyJWK, PyJWKClient

from mcp.server.auth.provider import AccessToken

from ..auth.secrets import CredentialSource

logger = logging.getLogger("bullhorn_mcp.identity")

LEEWAY_SECONDS = 30
JWKS_REFETCH_SECONDS = 60  # L-2: an unknown kid triggers at most one JWKS fetch per minute
MAX_TOKEN_CHARS = 16_384
MAX_CLAIM_CHARS = 512


def _kept_claims(claims: Mapping[str, Any], issuer: str, keep: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"iss": issuer}
    for name in keep:
        value = claims.get(name)
        if isinstance(value, str) and len(value) <= MAX_CLAIM_CHARS:
            out[name] = value
    return out


def _scopes(claims: Mapping[str, Any]) -> list[str]:
    raw = claims.get("scope", claims.get("scp"))
    if isinstance(raw, str):
        return [s for s in raw.split() if s][:100]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str)][:100]
    return []


def _audience_ok(aud: object, resource: str) -> bool:
    values = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    norm = resource.rstrip("/")
    return any(isinstance(v, str) and v.rstrip("/") == norm for v in values)


def _access_token(token: str, claims: Mapping[str, Any], resource: str, keep: tuple[str, ...]) -> AccessToken | None:
    sub, iss, exp = claims.get("sub"), claims.get("iss"), claims.get("exp")
    if not isinstance(sub, str) or not sub or len(sub) > MAX_CLAIM_CHARS:
        return None
    if not isinstance(iss, str) or not iss or type(exp) is not int:
        return None
    client_id = claims.get("azp", claims.get("client_id"))
    return AccessToken(
        token=token,
        client_id=client_id if isinstance(client_id, str) and client_id else "unknown",
        scopes=_scopes(claims),
        expires_at=exp,
        resource=resource,
        subject=sub,
        claims=_kept_claims(claims, iss, keep),
    )


class JwksTokenVerifier:
    """JWT/JWKS verification (PyJWT). ``jwks`` is a static key set or a ``PyJWKClient``."""

    def __init__(
        self,
        *,
        allowed_issuers: frozenset[str],
        resource_server_url: str,
        algorithms: tuple[str, ...] = ("RS256",),
        jwks: Mapping[str, Any] | None = None,
        jwks_client: Any = None,
        keep_claims: tuple[str, ...] = (),
    ) -> None:
        if (jwks is None) == (jwks_client is None):
            raise ValueError("exactly one of jwks or jwks_client is required")
        if any(a.startswith("HS") or a == "none" for a in algorithms):
            raise ValueError("only asymmetric JWT algorithms are allowed")
        self.allowed_issuers = frozenset(allowed_issuers)
        self.resource_server_url = resource_server_url
        self.algorithms = list(algorithms)
        self.keep_claims = keep_claims
        self._client = jwks_client
        self._keys: dict[str, PyJWK] = {}
        self._last_fetch = float("-inf")
        self._fetch_lock = threading.Lock()
        if jwks is not None:
            keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
            if not isinstance(keys, list) or not keys:
                raise ValueError("the JWKS has no keys")
            for item in keys:
                if isinstance(item, dict) and isinstance(item.get("kid"), str):
                    self._keys[item["kid"]] = PyJWK(item)
            if not self._keys:
                raise ValueError("every JWKS key needs a kid")

    def __repr__(self) -> str:
        return "JwksTokenVerifier(<config>)"

    def _signing_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") not in self.algorithms or not isinstance(kid, str):
            return None
        jwk = self._keys.get(kid)
        if jwk is None and self._client is not None:
            jwk = self._refetch(kid)
        return jwk.key if jwk is not None else None

    def _refetch(self, kid: str) -> PyJWK | None:
        """L-2: fetch the remote JWKS for an unknown ``kid`` at most once per ``JWKS_REFETCH_SECONDS``."""
        with self._fetch_lock:
            if kid not in self._keys:
                now = time.monotonic()
                if now - self._last_fetch < JWKS_REFETCH_SECONDS:
                    return None
                self._last_fetch = now
                jwk_set = self._client.get_jwk_set(refresh=True)
                self._keys = {k.key_id: k for k in jwk_set.keys if isinstance(k.key_id, str)}
            return self._keys.get(kid)

    def verify_sync(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            return None
        try:
            key = self._signing_key(token)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self.algorithms,
                audience=self.resource_server_url,
                options={"require": ["exp", "iss", "sub", "aud"], "verify_iss": False},
                leeway=LEEWAY_SECONDS,
            )
        except Exception:  # every verification failure is a rejection; never logged with the token
            return None
        if claims.get("iss") not in self.allowed_issuers or not _audience_ok(claims.get("aud"), self.resource_server_url):
            return None
        return _access_token(token, claims, self.resource_server_url, self.keep_claims)

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._client is None:
            return self.verify_sync(token)
        return await anyio.to_thread.run_sync(self.verify_sync, token)


class IntrospectionTokenVerifier:
    """RFC 7662 token introspection over ``httpx`` (client authentication: HTTP Basic)."""

    def __init__(
        self,
        *,
        introspection_url: str,
        client_id: str,
        client_secret: Any,
        allowed_issuers: frozenset[str],
        resource_server_url: str,
        keep_claims: tuple[str, ...] = (),
        timeout: float = 10.0,
    ) -> None:
        self.introspection_url = introspection_url
        self.client_id = client_id
        self._client_secret = client_secret  # a zero-argument callable (resolved per call, never cached)
        self.allowed_issuers = frozenset(allowed_issuers)
        self.resource_server_url = resource_server_url
        self.keep_claims = keep_claims
        self.timeout = timeout

    def __repr__(self) -> str:
        return "IntrospectionTokenVerifier(<config>)"

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.post(
                    self.introspection_url,
                    data={"token": token, "token_type_hint": "access_token"},
                    auth=(self.client_id, self._client_secret()),
                )
            if response.status_code != 200:
                return None
            data = response.json()
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("active") is not True:
            return None
        exp = data.get("exp")
        if type(exp) is not int or exp + LEEWAY_SECONDS < int(time.time()):
            return None
        if data.get("iss") not in self.allowed_issuers or not _audience_ok(data.get("aud"), self.resource_server_url):
            return None
        return _access_token(token, data, self.resource_server_url, self.keep_claims)


def build_verifier(auth: Any, creds: CredentialSource) -> Any:
    """Build the configured verifier from an ``identity.deploy.AuthConfig``."""
    v = auth.verifier
    keep = tuple(dict.fromkeys((*auth.display_claims, *((auth.tenant_claim,) if auth.tenant_claim else ()))))
    if v.kind == "jwks":
        if v.jwks_file:
            with open(v.jwks_file, encoding="utf-8") as fh:
                jwks = json.loads(fh.read(1_000_000))
            return JwksTokenVerifier(
                allowed_issuers=auth.allowed_issuers, resource_server_url=auth.resource_server_url,
                algorithms=v.algorithms, jwks=jwks, keep_claims=keep,
            )
        return JwksTokenVerifier(
            allowed_issuers=auth.allowed_issuers, resource_server_url=auth.resource_server_url,
            algorithms=v.algorithms, jwks_client=PyJWKClient(v.jwks_url, cache_keys=True), keep_claims=keep,
        )
    ref = v.client_secret_ref
    return IntrospectionTokenVerifier(
        introspection_url=v.introspection_url,
        client_id=v.client_id,
        client_secret=lambda: creds.resolve(ref),
        allowed_issuers=auth.allowed_issuers,
        resource_server_url=auth.resource_server_url,
        keep_claims=keep,
    )
