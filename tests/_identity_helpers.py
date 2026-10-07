"""Shared helpers for the Phase 5A identity/session tests (not a test module).

Everything here is synthetic: issuers, keys, tenants, client IDs and tokens are
made up for the tests (D-5-22). No real tenant or SSO data is used.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.roles import principal_key_for
from bullhorn_mcp.identity.session_store import MemorySessionStore, SessionRecord

ISSUER = "https://idp.example.test"
OTHER_ISSUER = "https://other-idp.example.test"
BASE = "https://mcp.example.test"
RESOURCE = BASE + "/mcp"
HOST = "mcp.example.test"
AUTH = "https://auth.bullhornstaffing.com"
LOGIN = "https://rest.bullhornstaffing.com"
# Tenant 1 uses the same restUrl as the 4A/4B helpers, so their stored profiles match its fingerprint.
REST_1 = "https://rest99.bullhornstaffing.com/rest-services/abc123"
REST_2 = "https://rest42.bullhornstaffing.com/rest-services/zzz999"
TK1 = hashlib.sha256(b"abc123").hexdigest()
TK2 = hashlib.sha256(b"zzz999").hexdigest()
CLIENT_SECRET_ENV = "TEST5A_BH_CLIENT_SECRET"
CLIENT_SECRET = "sentinel-client-secret-5a-0123456789"
SESSION_KEYS_ENV = "TEST5A_SESSION_KEYS"


def pk(subject: str, issuer: str = ISSUER) -> str:
    return principal_key_for(issuer, subject)


ALICE = "alice-sub"
BOB = "bob-sub"
ADMIN = "admin-sub"
SVC = "service-sub"


# ---------------------------------------------------------------------- #
# Keys and JWTs
# ---------------------------------------------------------------------- #

_KEY1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: Any, kid: str) -> dict[str, Any]:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    data.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return data


JWKS = {"keys": [_jwk(_KEY1, "k1")]}
WRONG_KEY = _KEY2


def make_jwt(subject: str | None = ALICE, *, issuer: str = ISSUER, aud: Any = RESOURCE, exp_delta: int = 300,
             key: Any = None, kid: str = "k1", alg: str = "RS256", **claims: Any) -> str:
    payload: dict[str, Any] = {"iss": issuer, "aud": aud, "exp": int(time.time()) + exp_delta, "iat": int(time.time())}
    if subject is not None:
        payload["sub"] = subject
    payload.update(claims)
    return jwt.encode(payload, key or _KEY1, algorithm=alg, headers={"kid": kid})


def session_keys_text(**keys: bytes) -> str:
    keys = keys or {"k1": b"\x01" * 32}
    active = list(keys)[-1]
    return json.dumps({"active": active, "keys": {k: base64.b64encode(v).decode() for k, v in keys.items()}})


# ---------------------------------------------------------------------- #
# Admin config
# ---------------------------------------------------------------------- #


def tenant_entry(root: Path, alias: str, *, sso: bool = False, service: bool = False, auth_url: str = AUTH) -> dict[str, Any]:
    store = root / f"store-{alias}"
    exchange = root / f"exchange-{alias}"
    store.mkdir(exist_ok=True)
    exchange.mkdir(exist_ok=True)
    entry: dict[str, Any] = {
        "alias": alias,
        "setup_store": str(store),
        "exchange_dir": str(exchange),
        "sso": sso,
        "bullhorn_oauth": {
            "client_id": f"client-{alias}",
            "client_secret_ref": f"env:{CLIENT_SECRET_ENV}",
            "redirect_uri": BASE + deploy.CALLBACK_PATH,
            "auth_url": auth_url,
            "login_url": LOGIN,
        },
    }
    if service:
        entry["service"] = {
            "client_id": f"svc-{alias}",
            "client_secret_ref": f"env:{CLIENT_SECRET_ENV}",
            "username_ref": "env:TEST5A_SVC_USER",
            "password_ref": "env:TEST5A_SVC_PASSWORD",
        }
    return entry


def admin_config(root: Path, *, tenants: int = 1, tenant_claim: str | None = None, sso: bool = False,
                 service: bool = False, jwks_file: str | None = None, **overrides: Any) -> dict[str, Any]:
    (root / "sessions").mkdir(exist_ok=True)
    tmap = {TK1: tenant_entry(root, "one", sso=sso, service=service)}
    if tenants > 1:
        tmap[TK2] = tenant_entry(root, "two", sso=sso, service=service)
    if jwks_file is None:
        jwks_path = root / "jwks.json"
        jwks_path.write_text(json.dumps(JWKS), encoding="utf-8")
        jwks_file = str(jwks_path)
    cfg: dict[str, Any] = {
        "mode": "shared",
        "service_base_url": BASE,
        "server": {"host": "127.0.0.1", "port": 8765, "allowed_hosts": [HOST]},
        "auth": {
            "issuer": ISSUER,
            "resource_server_url": RESOURCE,
            "allowed_issuers": [ISSUER],
            "display_claims": ["email", "name"],
            "tenant_claim": tenant_claim,
            "verifier": {"kind": "jwks", "jwks_file": jwks_file, "algorithms": ["RS256"]},
        },
        "session_store": {"dir": str(root / "sessions"), "keys_ref": f"env:{SESSION_KEYS_ENV}"},
        "tenants": tmap,
    }
    roles = {"setup_admins": [pk(ADMIN)], "write_approvers": [pk(ADMIN), pk(BOB), pk(ALICE)]}
    service = [{"issuer": ISSUER, "subject": SVC}]
    if tenants == 1:  # the single-tenant form: top-level sections apply to the only tenant (B-4)
        cfg["roles"], cfg["service_principals"] = roles, service
    else:  # several tenants: roles are configured per tenant
        for entry in tmap.values():
            entry["roles"], entry["service_principals"] = roles, service
    cfg.update(overrides)
    return cfg


@dataclass
class Shared:
    root: Path
    deployment: deploy.Deployment
    store: MemorySessionStore
    tenants: dict[str, Any] = field(default_factory=dict)

    def tenant(self, alias: str = "one") -> Any:
        t = self.deployment.tenant_by_alias(alias)
        assert t is not None
        return t


def activate_shared(root: Path, monkeypatch: pytest.MonkeyPatch, store: Any = None, **kwargs: Any) -> Shared:
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET)
    monkeypatch.setenv("TEST5A_SVC_USER", "svc-user")
    monkeypatch.setenv("TEST5A_SVC_PASSWORD", "sentinel-service-password-5a")
    dep = deploy.parse_admin_config(admin_config(root, **kwargs))
    mem = store if store is not None else MemorySessionStore()
    deploy.activate(dep, session_store=mem)
    sessions.reset_caches()
    return Shared(root, dep, mem)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A shared deployment with one tenant (``one`` = TK1) and an in-memory session store."""
    s = activate_shared(tmp_path, monkeypatch)
    yield s
    deploy.reset()
    sessions.reset_caches()


@pytest.fixture
def shared2(tmp_path, monkeypatch):
    """Two tenants (``one`` = TK1, ``two`` = TK2) selected by the ``bh_tenant`` claim."""
    s = activate_shared(tmp_path, monkeypatch, tenants=2, tenant_claim="bh_tenant")
    yield s
    deploy.reset()
    sessions.reset_caches()


def access_token(subject: str | None = ALICE, issuer: str | None = ISSUER, **claims: Any) -> AccessToken:
    c: dict[str, Any] = {} if issuer is None else {"iss": issuer}
    c.setdefault("email", f"{subject}@example.test")
    c.update(claims)
    return AccessToken(token="opaque-test-bearer", client_id="test-client", scopes=[], expires_at=int(time.time()) + 300,
                       resource=RESOURCE, subject=subject, claims=c)


@contextlib.contextmanager
def caller(subject: str | None = ALICE, issuer: str | None = ISSUER, token: AccessToken | None = None, **claims: Any):
    """Run the body as the given verified caller (the per-request contextvar the SDK sets, HV-M3)."""
    tok = token if token is not None else access_token(subject, issuer, **claims)
    reset = auth_context_var.set(AuthenticatedUser(tok))
    try:
        yield tok
    finally:
        auth_context_var.reset(reset)


@contextlib.contextmanager
def no_caller():
    reset = auth_context_var.set(None)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def link(store: Any, tenant_key: str, subject: str, *, token: str | None = None, rest_url: str = REST_1,
         expires_in: float = 600, refresh: str | None = "refresh-token-x", issuer: str = ISSUER,
         link_id: str | None = None) -> SessionRecord:
    now = time.time()
    rec = SessionRecord(
        tenant_key=tenant_key,
        principal_key=pk(subject, issuer),
        rest_url=rest_url,
        bh_rest_token=token or f"bh-token-{subject}-{tenant_key[:6]}",
        bh_expires_at=now + expires_in,
        access_token=f"access-{subject}",
        access_expires_at=now + 600,
        refresh_token=refresh,
        auth_origin=AUTH,
        created_at=now,
        last_refresh=now,
        link_id=link_id or f"link-{subject}-{tenant_key[:6]}-{time.time_ns()}",
    )
    store.put(rec)
    return rec


def tree_text(root: Path) -> str:
    """Every byte under ``root`` (text-decoded with replacement), for sentinel greps."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                out.append(Path(dirpath, name).read_bytes().decode("utf-8", "replace"))
            except OSError:
                pass
    return "\n".join(out)
