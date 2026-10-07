"""Deployment modes and the admin config (Phase 5A, D-5A-1, Amendment A3-5).

The admin config is an **out-of-repo** YAML file named by ``BULLHORN_ADMIN_CONFIG``,
parsed with ``tenant/yaml_strict`` (duplicate keys rejected). Unknown keys and
wrong types are rejected; startup fails with every problem listed (bounded).

Modes
-----
* ``local`` (the default when ``BULLHORN_ADMIN_CONFIG`` is unset): stdio only,
  exactly today's behaviour (the env password grant, ``server._client``).
* ``shared``: streamable-HTTP only, ``stateless_http=True``, with a mandatory MCP
  ``token_verifier`` + ``AuthSettings``. Every tool call resolves the calling
  principal and *that* principal's Bullhorn session.

Startup refuses: ``shared`` without auth configuration or session keys; ``local``
with an HTTP transport (``BULLHORN_MCP_TRANSPORT``); ``shared`` on stdio; a literal
secret where a reference is required; a non-``https`` ``service_base_url``;
``shared`` without ``server.allowed_hosts``.

Schema (``shared``)::

    mode: shared
    service_base_url: https://mcp.example.test          # public https base, no path
    server:
      host: 127.0.0.1                                   # default 127.0.0.1
      port: 8000                                        # default 8000
      allowed_hosts: [mcp.example.test]                 # required; Host-header allowlist (HV-M7)
    auth:
      issuer: https://idp.example.test                  # AuthSettings.issuer_url
      resource_server_url: https://mcp.example.test/mcp # the token audience
      allowed_issuers: [https://idp.example.test]       # principals from other issuers are refused
      required_scopes: []                               # optional
      display_claims: [email, name]                     # optional (shown, never trusted for identity)
      tenant_claim: null                                # optional (Amendment A3-1)
      verifier:                                         # exactly one kind
        kind: jwks                                      # jwks | introspection
        jwks_url: https://idp.example.test/jwks.json    # or jwks_file: /abs/path.json
        algorithms: [RS256]                             # asymmetric algorithms only
        # kind: introspection -> introspection_url, client_id, client_secret_ref
    session_store:
      dir: /abs/path/sessions                           # never inside a tenant setup store
      keys_ref: env:BULLHORN_SESSION_KEYS               # {"active": id, "keys": {id: base64(32 bytes)}}
      max_session_days: 30                              # absolute lifetime from link completion (max 90)
    tenants:
      <tenant_key = sha256(corpToken), 64 hex>:
        alias: acme                                     # unique
        setup_store: /abs/path/store                    # the 4A setup store (tenant level)
        exchange_dir: /abs/path/exchange                # export/import paths must resolve inside
        sso: false                                      # true: SSO tenant (Amendment A3-6)
        bullhorn_oauth:
          client_id: <API key client id>
          client_secret_ref: env:NAME | file:/abs/path
          redirect_uri: https://mcp.example.test/oauth/bullhorn/callback
          auth_url: https://auth.bullhornstaffing.com   # optional, must be trusted https
          login_url: https://rest.bullhornstaffing.com  # optional, must be trusted https
        service:                                        # optional: the read-only service identity
          client_id: <client id>
          client_secret_ref: env:NAME
          username_ref: env:NAME
          password_ref: env:NAME
        roles:                                          # per tenant (5A triage B-4)
          setup_admins: [<principal key> | {issuer: ..., subject: ...}]
          write_approvers: [...]
        service_principals: [...]                       # this tenant's read-only service identity callers

Top-level ``roles`` / ``service_principals`` are accepted only when exactly one
tenant is configured (they then apply to that tenant); otherwise startup is refused.
Every role and service-principal check uses the request's selected tenant.

A ``local`` config contains only ``mode: local``.

Operational constraints (shared mode): run exactly **one** worker process (the
refresh/link locks are in-process; ``WEB_CONCURRENCY``/``UVICORN_WORKERS`` > 1 is
refused), and **DEBUG logging is unsupported**: a global log-record factory redacts
secrets and the HTTP/MCP library loggers are clamped to INFO (5A triage B-1).

Secrets are references only (``env:NAME`` / ``file:/abs/path``, ``auth/secrets.py``).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..auth.secrets import (
    DEFAULT_SOURCE,
    CredentialSource,
    SecretRef,
    SecretRefError,
    install_log_redaction,
    is_reference,
    uninstall_log_redaction,
)
from ..auth.trusted_origins import DEFAULT_POLICY
from .roles import Roles, parse_principal_list

logger = logging.getLogger("bullhorn_mcp.identity")

ADMIN_CONFIG_ENV_VAR = "BULLHORN_ADMIN_CONFIG"
TRANSPORT_ENV_VAR = "BULLHORN_MCP_TRANSPORT"
LEGACY_IDENTITY_ENV_VARS = ("BULLHORN_MCP_ACTOR", "BULLHORN_SETUP_ADMINS", "BULLHORN_WRITE_APPROVERS")
CALLBACK_PATH = "/oauth/bullhorn/callback"
MAX_PROBLEMS = 50
_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ALIAS_RE = re.compile(r"[a-z][a-z0-9_\-]{0,31}", re.ASCII)
_CLAIM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,63}", re.ASCII)
_HOST_RE = re.compile(r"[A-Za-z0-9.\-]{1,253}(?::[0-9]{1,5})?", re.ASCII)
_ASYMMETRIC_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"})
_SECRET_KEYS = ("client_secret", "keys", "password", "secret", "token")


class DeploymentError(Exception):
    """Startup refused. The message lists every problem (bounded) and never contains a secret."""

    def __init__(self, problems: list[str] | str) -> None:
        items = [problems] if isinstance(problems, str) else list(problems)
        self.problems = tuple(p[:300] for p in items[:MAX_PROBLEMS])
        extra = f"\n  - ... and {len(items) - MAX_PROBLEMS} more" if len(items) > MAX_PROBLEMS else ""
        super().__init__("admin config refused:\n" + "\n".join(f"  - {p}" for p in self.problems) + extra)


# ---------------------------------------------------------------------- #
# Model
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifierConfig:
    kind: str  # "jwks" | "introspection"
    jwks_url: str | None = None
    jwks_file: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    introspection_url: str | None = None
    client_id: str | None = None
    client_secret_ref: SecretRef | None = None


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    resource_server_url: str
    allowed_issuers: frozenset[str]
    verifier: VerifierConfig
    required_scopes: tuple[str, ...] = ()
    display_claims: tuple[str, ...] = ()
    tenant_claim: str | None = None


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret_ref: SecretRef
    redirect_uri: str
    auth_url: str = "https://auth.bullhornstaffing.com"
    login_url: str = "https://rest.bullhornstaffing.com"


@dataclass(frozen=True)
class ServiceCredentials:
    client_id: str
    client_secret_ref: SecretRef
    username_ref: SecretRef
    password_ref: SecretRef


@dataclass(frozen=True)
class TenantConfig:
    tenant_key: str
    alias: str
    setup_store: Path
    exchange_dir: Path
    oauth: OAuthClientConfig
    sso: bool = False
    service: ServiceCredentials | None = None
    roles: Roles = field(default_factory=Roles)

    @property
    def hint(self) -> str:
        return self.tenant_key[:8]


@dataclass(frozen=True)
class Deployment:
    mode: str  # "local" | "shared"
    service_base_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ()
    auth: AuthConfig | None = None
    session_dir: Path | None = None
    session_keys_ref: SecretRef | None = None
    session_max_days: int = 30
    tenants: Mapping[str, TenantConfig] = field(default_factory=dict)

    @property
    def shared(self) -> bool:
        return self.mode == "shared"

    def tenant(self, tenant_key: str | None) -> TenantConfig | None:
        return self.tenants.get(tenant_key) if tenant_key is not None else None

    def tenant_by_alias(self, alias: str) -> TenantConfig | None:
        matches = [t for t in self.tenants.values() if t.alias == alias]
        return matches[0] if len(matches) == 1 else None


LOCAL = Deployment(mode="local")


# ---------------------------------------------------------------------- #
# Parsing
# ---------------------------------------------------------------------- #


class _Parser:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def mapping(self, where: str, value: object, required: set[str], optional: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.err(f"{where}: must be a mapping")
            return {}
        for key in value:
            if not isinstance(key, str) or (key not in required and key not in optional):
                if isinstance(key, str) and key in _SECRET_KEYS:
                    self.err(f"{where}.{key}: a literal secret is not allowed; use a *_ref (env:NAME or file:/abs/path)")
                else:
                    self.err(f"{where}: unknown key {str(key)[:40]!r}")
        for key in sorted(required):
            if key not in value:
                self.err(f"{where}: missing required key {key!r}")
        return value

    def string(self, where: str, value: object, max_len: int = 1024) -> str | None:
        if not isinstance(value, str) or not value or len(value) > max_len or not value.isprintable():
            self.err(f"{where}: must be a non-empty string of at most {max_len} characters")
            return None
        return value

    def https_url(self, where: str, value: object, *, path_ok: bool = True) -> str | None:
        text = self.string(where, value, 2048)
        if text is None:
            return None
        try:
            parts = urlsplit(text)
        except ValueError:
            parts = None
        bad = parts is None or parts.scheme != "https" or not parts.netloc
        if bad or parts is None or parts.username or parts.password or parts.fragment or parts.query:
            self.err(f"{where}: must be an https:// URL without credentials, query or fragment")
            return None
        if not path_ok and parts.path not in ("", "/"):
            self.err(f"{where}: must not have a path")
            return None
        return text.rstrip("/") if not path_ok else text

    def ref(self, where: str, value: object) -> SecretRef | None:
        if not is_reference(value):
            self.err(f"{where}: a secret must be a reference (env:NAME or file:/abs/path); literal values refuse startup")
            return None
        assert isinstance(value, str)
        return SecretRef(value)

    def abs_dir(self, where: str, value: object) -> Path | None:
        text = self.string(where, value, 1024)
        if text is None:
            return None
        path = Path(text)
        if not path.is_absolute():
            self.err(f"{where}: must be an absolute path")
            return None
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            self.err(f"{where}: directory does not exist")
            return None
        if not resolved.is_dir():
            self.err(f"{where}: is not a directory")
            return None
        return resolved

    def str_list(self, where: str, value: object, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            self.err(f"{where}: must be a list of non-empty strings")
            return ()
        if pattern is not None:
            for v in value:
                if not pattern.fullmatch(v):
                    self.err(f"{where}: {v[:60]!r} is not valid")
        return tuple(value)


def _parse_verifier(p: _Parser, raw: object) -> VerifierConfig | None:
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind == "jwks":
        v = p.mapping("auth.verifier", raw, {"kind"}, {"jwks_url", "jwks_file", "algorithms"})
        url = p.https_url("auth.verifier.jwks_url", v["jwks_url"]) if "jwks_url" in v else None
        jfile = p.string("auth.verifier.jwks_file", v["jwks_file"]) if "jwks_file" in v else None
        if (url is None) == (jfile is None):
            p.err("auth.verifier: exactly one of jwks_url or jwks_file is required")
        if jfile is not None and not Path(jfile).is_absolute():
            p.err("auth.verifier.jwks_file: must be an absolute path")
        algs = p.str_list("auth.verifier.algorithms", v.get("algorithms", ["RS256"])) or ("RS256",)
        bad = [a for a in algs if a not in _ASYMMETRIC_ALGS]
        if bad:
            p.err("auth.verifier.algorithms: only asymmetric algorithms are allowed")
        return VerifierConfig("jwks", jwks_url=url, jwks_file=jfile, algorithms=algs)
    if kind == "introspection":
        v = p.mapping("auth.verifier", raw, {"kind", "introspection_url", "client_id", "client_secret_ref"}, set())
        return VerifierConfig(
            "introspection",
            introspection_url=p.https_url("auth.verifier.introspection_url", v.get("introspection_url")),
            client_id=p.string("auth.verifier.client_id", v.get("client_id")),
            client_secret_ref=p.ref("auth.verifier.client_secret_ref", v.get("client_secret_ref")),
        )
    p.err("auth.verifier.kind: must be 'jwks' or 'introspection'")
    return None


def _parse_auth(p: _Parser, raw: object) -> AuthConfig | None:
    a = p.mapping(
        "auth",
        raw,
        {"issuer", "resource_server_url", "allowed_issuers", "verifier"},
        {"required_scopes", "display_claims", "tenant_claim"},
    )
    if not a:
        return None
    issuer = p.https_url("auth.issuer", a.get("issuer"))
    resource = p.https_url("auth.resource_server_url", a.get("resource_server_url"))
    allowed = p.str_list("auth.allowed_issuers", a.get("allowed_issuers"))
    if not allowed:
        p.err("auth.allowed_issuers: at least one issuer is required")
    verifier = _parse_verifier(p, a.get("verifier"))
    claim = a.get("tenant_claim")
    if claim is not None and (not isinstance(claim, str) or not _CLAIM_RE.fullmatch(claim)):
        p.err("auth.tenant_claim: must be null or a claim name")
        claim = None
    if issuer is None or resource is None or verifier is None:
        return None
    return AuthConfig(
        issuer=issuer,
        resource_server_url=resource,
        allowed_issuers=frozenset(allowed),
        verifier=verifier,
        required_scopes=p.str_list("auth.required_scopes", a.get("required_scopes")),
        display_claims=p.str_list("auth.display_claims", a.get("display_claims"), _CLAIM_RE),
        tenant_claim=claim,
    )


def _parse_tenant(p: _Parser, tk: object, raw: object, base_url: str | None) -> TenantConfig | None:
    where = f"tenants.{str(tk)[:16]}"
    if not isinstance(tk, str) or not _HEX64_RE.fullmatch(tk):
        p.err(f"{where}: a tenant key must be sha256(corpToken) as 64 lowercase hex characters")
        return None
    t = p.mapping(
        where, raw, {"alias", "setup_store", "exchange_dir", "bullhorn_oauth"}, {"sso", "service", "roles", "service_principals"}
    )
    if not t:
        return None
    alias = t.get("alias")
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        p.err(f"{where}.alias: must match {_ALIAS_RE.pattern}")
        alias = None
    o = p.mapping(f"{where}.bullhorn_oauth", t.get("bullhorn_oauth"), {"client_id", "client_secret_ref", "redirect_uri"},
                  {"auth_url", "login_url"})
    redirect = p.https_url(f"{where}.bullhorn_oauth.redirect_uri", o.get("redirect_uri"))
    if redirect is not None and base_url is not None and redirect != base_url + CALLBACK_PATH:
        p.err(f"{where}.bullhorn_oauth.redirect_uri: must be service_base_url + {CALLBACK_PATH}")
    urls = {}
    for name, default in (("auth_url", "https://auth.bullhornstaffing.com"), ("login_url", "https://rest.bullhornstaffing.com")):
        value = o.get(name, default)
        if DEFAULT_POLICY.origin(value) is None or DEFAULT_POLICY.origin(value) != str(value).rstrip("/"):
            p.err(f"{where}.bullhorn_oauth.{name}: must be a trusted https Bullhorn origin")
        urls[name] = str(value).rstrip("/")
    client_id = p.string(f"{where}.bullhorn_oauth.client_id", o.get("client_id"), 256)
    secret = p.ref(f"{where}.bullhorn_oauth.client_secret_ref", o.get("client_secret_ref"))
    sso = t.get("sso", False)
    if not isinstance(sso, bool):
        p.err(f"{where}.sso: must be true or false")
        sso = False
    service = None
    if "service" in t:
        s = p.mapping(f"{where}.service", t["service"], {"client_id", "client_secret_ref", "username_ref", "password_ref"}, set())
        sid = p.string(f"{where}.service.client_id", s.get("client_id"), 256)
        refs = [p.ref(f"{where}.service.{n}", s.get(n)) for n in ("client_secret_ref", "username_ref", "password_ref")]
        if sid is not None and all(r is not None for r in refs):
            service = ServiceCredentials(sid, refs[0], refs[1], refs[2])  # type: ignore[arg-type]
    has_roles = "roles" in t or "service_principals" in t
    roles = _parse_roles(p, where + ".", t.get("roles"), t.get("service_principals")) if has_roles else Roles()
    store = p.abs_dir(f"{where}.setup_store", t.get("setup_store"))
    exchange = p.abs_dir(f"{where}.exchange_dir", t.get("exchange_dir"))
    if None in (alias, redirect, client_id, secret, store, exchange):
        return None
    assert alias and redirect and client_id and secret and store and exchange
    return TenantConfig(
        tenant_key=tk,
        alias=alias,
        setup_store=store,
        exchange_dir=exchange,
        oauth=OAuthClientConfig(client_id, secret, redirect, urls["auth_url"], urls["login_url"]),
        sso=sso,
        service=service,
        roles=roles,
    )


def _parse_roles(p: _Parser, prefix: str, raw_roles: object, raw_service: object) -> Roles:
    r = p.mapping(f"{prefix}roles", raw_roles if raw_roles is not None else {}, set(), {"setup_admins", "write_approvers"})
    roles = Roles(
        setup_admins=parse_principal_list(f"{prefix}roles.setup_admins", r.get("setup_admins"), p.errors),
        write_approvers=parse_principal_list(f"{prefix}roles.write_approvers", r.get("write_approvers"), p.errors),
        service_principals=parse_principal_list(f"{prefix}service_principals", raw_service, p.errors),
    )
    if roles.service_principals & (roles.setup_admins | roles.write_approvers):
        p.err(f"{prefix}service_principals: a service principal cannot also be a setup admin or write approver")
    return roles


# L-4: the longest file name the session store creates below its directory
# (``pending/<kind>-<64 hex>.bin``, ``pending/.taken-<32 hex>.bin``, temp files), plus a margin.
SESSION_PATH_SUFFIX_CHARS = len("/pending/plink-") + 64 + len(".bin") + 16
MAX_PATH_CHARS = 240 if os.name == "nt" else 4000
MAX_SESSION_DAYS = 90


def parse_admin_config(data: object) -> Deployment:
    """Validate a parsed admin config. Raises ``DeploymentError`` listing every problem."""
    p = _Parser()
    if not isinstance(data, dict):
        raise DeploymentError("the admin config must be a mapping")
    mode = data.get("mode")
    if mode == "local":
        p.mapping("<root>", data, {"mode"}, set())
        if p.errors:
            raise DeploymentError(p.errors)
        return LOCAL
    if mode != "shared":
        raise DeploymentError("mode: must be 'local' or 'shared'")
    from dataclasses import replace

    d = p.mapping(
        "<root>",
        data,
        {"mode", "service_base_url", "server", "auth", "session_store", "tenants"},
        {"roles", "service_principals"},
    )
    base = p.https_url("service_base_url", d.get("service_base_url"), path_ok=False)
    s = p.mapping("server", d.get("server"), {"allowed_hosts"}, {"host", "port"})
    host = s.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        p.err("server.host: must be a non-empty string")
        host = "127.0.0.1"
    port = s.get("port", 8000)
    if type(port) is not int or not 1 <= port <= 65535:
        p.err("server.port: must be an integer from 1 to 65535")
        port = 8000
    allowed_hosts = p.str_list("server.allowed_hosts", s.get("allowed_hosts"), _HOST_RE)
    if not allowed_hosts:
        p.err("server.allowed_hosts: at least one Host header value is required in shared mode (DNS-rebinding protection)")
    auth = _parse_auth(p, d.get("auth"))
    ss = p.mapping("session_store", d.get("session_store"), {"dir", "keys_ref"}, {"max_session_days"})
    session_dir = p.abs_dir("session_store.dir", ss.get("dir"))
    keys_ref = p.ref("session_store.keys_ref", ss.get("keys_ref"))
    max_days = ss.get("max_session_days", 30)
    if type(max_days) is not int or not 1 <= max_days <= MAX_SESSION_DAYS:
        p.err(f"session_store.max_session_days: must be an integer from 1 to {MAX_SESSION_DAYS}")
        max_days = 30
    if session_dir is not None and len(str(session_dir)) + SESSION_PATH_SUFFIX_CHARS > MAX_PATH_CHARS:
        p.err(f"session_store.dir: too long; session file paths would exceed {MAX_PATH_CHARS} characters")
    tenants: dict[str, TenantConfig] = {}
    raw_tenants = d.get("tenants")
    if not isinstance(raw_tenants, dict) or not raw_tenants:
        p.err("tenants: at least one tenant is required")
    else:
        for tk, raw in raw_tenants.items():
            tenant = _parse_tenant(p, tk, raw, base)
            if tenant is not None:
                tenants[tenant.tenant_key] = tenant
    if "roles" in d or "service_principals" in d:
        # B-4: global role sections only with exactly one tenant, and then they apply to it.
        top = _parse_roles(p, "", d.get("roles"), d.get("service_principals"))
        if not isinstance(raw_tenants, dict) or len(raw_tenants) != 1:
            p.err(
                "roles/service_principals: top-level sections are allowed only with exactly one tenant; "
                "configure them under tenants.<key> instead"
            )
        elif tenants:
            only = next(iter(tenants.values()))
            if only.roles != Roles():
                p.err("roles/service_principals: configure them either at the top level or under the tenant, not both")
            else:
                tenants[only.tenant_key] = replace(only, roles=top)
    aliases = [t.alias for t in tenants.values()]
    if len(set(aliases)) != len(aliases):
        p.err("tenants: every alias must be unique")
    if auth is not None and auth.tenant_claim is None and len(tenants) > 1:
        p.err("auth.tenant_claim: required when more than one tenant is configured (Amendment A3-1)")
    from ..tenant.store import is_within  # L-8: the single path-containment helper

    if session_dir is not None:
        for t in tenants.values():
            if is_within(session_dir, t.setup_store) or is_within(t.setup_store, session_dir):
                p.err(f"session_store.dir: must not overlap tenant {t.alias}'s setup store")
    for t in tenants.values():
        if is_within(t.exchange_dir, t.setup_store) or is_within(t.setup_store, t.exchange_dir):
            p.err(f"tenants.{t.alias}.exchange_dir: must not overlap the setup store")
    if p.errors or auth is None or base is None or session_dir is None or keys_ref is None:
        raise DeploymentError(p.errors or ["shared mode requires auth, service_base_url and session_store"])
    return Deployment(
        mode="shared",
        service_base_url=base,
        host=host,
        port=port,
        allowed_hosts=allowed_hosts,
        auth=auth,
        session_dir=session_dir,
        session_keys_ref=keys_ref,
        session_max_days=max_days,
        tenants=tenants,
    )


def load_admin_config(path: str | os.PathLike[str]) -> Deployment:
    from ..tenant import yaml_strict  # local import: the tenant package is heavy

    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(yaml_strict.MAX_YAML_CHARS + 1)
    except OSError as exc:
        raise DeploymentError(f"{ADMIN_CONFIG_ENV_VAR}: cannot read the admin config ({type(exc).__name__})") from None
    try:
        data = yaml_strict.parse(text)
    except yaml_strict.YamlParseFailure as exc:
        raise DeploymentError(f"admin config: {str(exc)[:200]}") from None
    return parse_admin_config(data)


# ---------------------------------------------------------------------- #
# The active deployment (process state)
# ---------------------------------------------------------------------- #


@dataclass
class _Active:
    deployment: Deployment
    session_store: Any = None
    verifier: Any = None
    credentials: CredentialSource = field(default_factory=lambda: DEFAULT_SOURCE)


_ACTIVE: _Active | None = None


def activate(deployment: Deployment, *, session_store: Any = None, verifier: Any = None,
             credentials: CredentialSource | None = None) -> None:
    """Install a deployment (startup, or a test). A ``shared`` deployment needs a session store."""
    global _ACTIVE
    if deployment.shared:
        if session_store is None:
            raise DeploymentError("shared mode requires a session store")
        if getattr(session_store, "local_only", False):
            raise DeploymentError("the OS-keyring session store is local-only and refused in shared mode")
    if deployment.shared:
        install_log_redaction()  # AC-15: no code, state, token or secret in HTTP logs
    _ACTIVE = _Active(deployment, session_store, verifier, credentials or DEFAULT_SOURCE)


def reset() -> None:
    """Back to the implicit default (local mode; also used by tests)."""
    global _ACTIVE
    _ACTIVE = None
    uninstall_log_redaction()  # local mode is unchanged: no record factory, no level clamps


def current() -> Deployment:
    return _ACTIVE.deployment if _ACTIVE is not None else LOCAL


def is_shared() -> bool:
    return _ACTIVE is not None and _ACTIVE.deployment.shared


def session_store() -> Any:
    if _ACTIVE is None or _ACTIVE.session_store is None:
        raise DeploymentError("no session store is configured")
    return _ACTIVE.session_store


def credentials() -> CredentialSource:
    return _ACTIVE.credentials if _ACTIVE is not None else DEFAULT_SOURCE


def _build_shared_runtime(dep: Deployment, creds: CredentialSource) -> tuple[Any, Any]:
    from .session_store import EncryptedFileSessionStore, SessionKeys, SessionStoreError
    from .verifiers import build_verifier

    assert dep.session_keys_ref is not None and dep.session_dir is not None and dep.auth is not None
    try:
        keys = SessionKeys.from_text(creds.resolve(dep.session_keys_ref))
        store = EncryptedFileSessionStore(dep.session_dir, keys)
        store.prune()  # L-3: drop stale pending logins/links left by a previous run
    except (SecretRefError, SessionStoreError) as exc:
        raise DeploymentError(f"session_store: {exc}") from None
    try:
        verifier = build_verifier(dep.auth, creds)
    except (ValueError, OSError, SecretRefError) as exc:
        raise DeploymentError(f"auth.verifier: {str(exc)[:200]}") from None
    return store, verifier


def startup(env: Mapping[str, str] | None = None, creds: CredentialSource | None = None) -> Deployment:
    """Load ``BULLHORN_ADMIN_CONFIG`` (if set), validate it and install the runtime. Raises ``DeploymentError``."""
    env = os.environ if env is None else env
    raw = env.get(ADMIN_CONFIG_ENV_VAR, "")
    path = raw.strip() if isinstance(raw, str) else ""
    if not path:
        reset()
        return LOCAL
    dep = load_admin_config(path)
    if not dep.shared:
        activate(dep)
        return dep
    creds = creds or DEFAULT_SOURCE
    store, verifier = _build_shared_runtime(dep, creds)
    activate(dep, session_store=store, verifier=verifier, credentials=creds)
    ignored = [n for n in LEGACY_IDENTITY_ENV_VARS if (env.get(n) or "").strip()]
    if ignored:
        logger.warning("shared mode: %s are local-mode aliases and are ignored", ", ".join(ignored))
    return dep


def fastmcp_auth_kwargs(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``FastMCP(...)`` keyword arguments: ``{}`` in local mode; auth + stateless HTTP in shared mode."""
    dep = startup(env, CredentialSource(env) if env is not None else None)
    if not dep.shared:
        return {}
    return shared_fastmcp_kwargs(dep, _ACTIVE.verifier if _ACTIVE is not None else None)


def shared_fastmcp_kwargs(dep: Deployment, verifier: Any) -> dict[str, Any]:
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings

    if not dep.shared or dep.auth is None or verifier is None or dep.service_base_url is None:
        raise DeploymentError("shared mode requires a token verifier and auth settings")
    return {
        "token_verifier": verifier,
        "auth": AuthSettings(
            issuer_url=dep.auth.issuer,  # type: ignore[arg-type]
            resource_server_url=dep.auth.resource_server_url,  # type: ignore[arg-type]
            required_scopes=list(dep.auth.required_scopes) or None,
            validate_token_resource=True,
        ),
        "stateless_http": True,
        "host": dep.host,
        "port": dep.port,
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(dep.allowed_hosts),
            allowed_origins=[dep.service_base_url],
        ),
    }


def run_args(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``mcp.run(**run_args())``: ``{}`` (stdio) in local mode; streamable-HTTP in shared mode."""
    env = os.environ if env is None else env
    raw = env.get(TRANSPORT_ENV_VAR, "")
    transport = raw.strip() if isinstance(raw, str) else ""
    if is_shared():
        for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
            value = (env.get(name) or "").strip()
            if value and value != "1":
                raise DeploymentError(f"shared mode runs exactly one worker process ({name} must be 1)")
        install_log_redaction()  # re-apply the level clamps right before serving
        if transport and transport != "streamable-http":
            raise DeploymentError("shared mode runs only on streamable-http (stdio and sse are refused)")
        return {"transport": "streamable-http"}
    if transport and transport != "stdio":
        raise DeploymentError("local mode runs only on stdio; HTTP transports require mode: shared")
    return {}
