# Phase 5A HV Verification: MCP SDK (HV-M) and Bullhorn OAuth / sessions (HV-C)

- **Status:** Builder-authored record for `PHASE5A_WORK_PACKAGE.md` §0 and §5 (HV-1), including Amendments A3-6 and A3-7. Dated 2026-10-07.
- **Rule:** if a Bullhorn behaviour is not documented, it is **unresolved**, and the §5 guard applies (fail closed). Nothing below is guessed.
- **Tenant verification:** none was performed. That needs an administrator on a connected tenant. Two procedures exist for it: D-5A-15 for `create_note`, and A3-6 for SSO login. No tenant values appear in this document (D-5-22).

## Outcome summary

| ID | Outcome | Effect in code |
|---|---|---|
| HV-M1 | **Verified** (source and test) | `FastMCP(token_verifier, auth)` returns 401 before any tool runs |
| HV-M2 | **Verified** (source) | `AccessToken.subject` / `.claims["iss"]` give the principal; the verifiers keep only `iss`, the display claims and the tenant claim |
| HV-M3 | **Verified** (source) | `get_access_token()` reads a per-request contextvar |
| HV-M4 | **Verified** (source) | Not relied on: shared mode is `stateless_http=True` |
| HV-M5 | **Verified** (source and test) | The OAuth routes are unauthenticated `custom_route`s that answer only in shared mode |
| HV-M6 | **Verified by test** | `get_access_token()` returns that request's token under 40 overlapping requests. The `Context.request_context.request.user` fallback is not needed. |
| HV-M7 | **Verified** (source and test); partial coverage | The SDK's `TransportSecuritySettings` Host allowlist protects `/mcp` only. `oauth_routes` repeats the Host check for the custom routes. |
| HV-C1 | **Verified** (docs) | Browser authorize flow as specified |
| HV-C2 | **Unresolved** | A3-6: `unsupported_sso` until a setup admin's verification login and an explicit `enable_sso_login` |
| HV-C3 | **Mostly verified** (docs); the response field names are not documented | Strict RFC 6749 parsing, which fails closed. The rotated refresh token is stored atomically. |
| HV-C4 | **Verified** (docs) | POST REST login, refresh on 401, `restUrl` must be a trusted `https` URL whose corpToken is the selected tenant |
| HV-C5 | **Unresolved** (same as HV-B11) | `executing_bullhorn_identity = bh-session:<sha256(tenant\|principal)>`. `commentingPerson` is never invented. The D-5A-15 procedure applies. |
| HV-C6 | **Unresolved** (no revocation endpoint documented) | Logout is local only: it deletes the `(tenant, principal)` entry |
| HV-C7 | **Unresolved** (PKCE not documented) | No PKCE. A confidential client plus a 256-bit single-use `state`; the client secret stays server-side. |
| HV-C8 | **Verified** (docs) for the 307 data-center redirect | Exchange at the configured, allowlisted host. A 307/308 is followed at most twice, only to a trusted `https` host and the same path. |
| HV-C9 | **Unresolved** (concurrent-session limits not documented) | Refresh and logout are serialised per `(tenant, principal)` lock |
| HV-C10 | **Verified** (docs): `429 Rate Limited – Wait 1 second then retry request` | No automatic retry anywhere in 5A (see the HV-C10 notes) |

---

## HV-M: MCP SDK (`mcp==1.30.0`, installed source read 2026-10-07)

| ID | Source (installed package) | Test evidence |
|---|---|---|
| HV-M1 | `mcp/server/fastmcp/server.py`: `FastMCP.__init__` takes `token_verifier`/`auth`. `streamable_http_app()` installs `AuthenticationMiddleware(BearerAuthBackend)` and `AuthContextMiddleware`, and wraps `/mcp` in `RequireAuthMiddleware`. `mcp/server/auth/middleware/bearer_auth.py`: a missing token or a verifier `None` (or an expired token, or a wrong resource when `validate_token_resource`) yields an unauthenticated scope, and `RequireAuthMiddleware` returns 401. | `tests/test_phase5a_security_http.py::TestBearerAuth::test_rejected_before_any_tool` (missing, garbage, wrong key, expired, wrong audience, wrong issuer, forged tenant alias): 401 and zero Bullhorn requests |
| HV-M2 | `mcp/server/auth/provider.py`: `AccessToken{token, client_id, scopes, expires_at, resource, subject, claims}` and `TokenVerifier.verify_token`. `bearer_auth.authorization_context` takes the issuer from `claims["iss"]`. | `tests/test_identity_verifiers.py` |
| HV-M3 | `mcp/server/auth/middleware/auth_context.py`: `auth_context_var` (a `ContextVar`) is set by `AuthContextMiddleware` around each request and reset afterwards. `get_access_token()` reads it. | Every shared-mode tool test drives identity through this contextvar (`tests/_identity_helpers.py::caller`) |
| HV-M4 | `mcp/server/streamable_http_manager.py::_handle_stateful_request`: a session is bound to `authorization_context(user)` and a different principal gets 404. | Not relied on (stateless mode) |
| HV-M5 | `server.py::custom_route`: "Routes using this decorator will not require authorization". The routes are appended outside `RequireAuthMiddleware`. | `tests/test_oauth_routes.py::test_routes_registered_on_the_server`, `test_wrong_host_and_local_mode_are_404` |
| HV-M6 | `streamable_http_manager.py::_handle_stateless_request` starts the per-request server task with `self._task_group.start(...)` from inside the request coroutine, so the task inherits that request's contextvars. | `tests/test_phase5a_security_http.py::TestHvM6::test_contextvar_is_per_request_under_interleaving`: 40 concurrent requests from 7 subjects. An async handler reads the token, sleeps, and reads it again. Every response matches its own requester, and the peak overlap is above 1. `TestConcurrentIsolation` (30 interleaved A/B `get_job` calls; every Bullhorn request carries the requester's own `BhRestToken`) |
| HV-M7 | `mcp/server/transport_security.py::TransportSecuritySettings{enable_dns_rebinding_protection, allowed_hosts, allowed_origins}` returns 421 for a Host outside the list. It is applied by the streamable-HTTP transport (the `/mcp` endpoint) only, not by `custom_route`s. | `TestBearerAuth::test_wrong_host_header_refused` (421 on `/mcp`) and `tests/test_oauth_routes.py::test_wrong_host_and_local_mode_are_404` (the custom routes' own check) |

---

## HV-C: Bullhorn (official documentation)

### Sources

- **S1, "Getting Started with REST":** https://bullhorn.github.io/Getting-Started-with-REST/ (fetched 2026-10-07; section "Complete the authorization process", "Log in to the REST API").
- **S2, REST API reference:** https://bullhorn.github.io/rest-api-docs/ (cached copy `apiref.html` from 2026-10-06, compared with the live page). It covers the sections "Authorization", "login", "Logout", "ping" and "Errors".

### HV-C1: authorization-code flow (verified)

S1, verbatim:

> `https://auth-{value_from_loginInfo}.bullhornstaffing.com/oauth/authorize?client_id={client_id}&response_type=code&redirect_uri={optional redirect_uri}&state={recommended state value}`

> If you do not include action=Login&username={username}&password={password} in the URL, you will be prompted with a login page if running in a browser

> The page is redirected to the redirect URI with a code query parameter on the URL.

> The state query parameter is recommended. The client uses this value to maintain state between the request and the callback. It should be used for preventing cross-site request forgery.

> The redirect_uri query parameter is optional. If used, the redirect_uri must match one of the redirect_uris specified in the OAuth API key.

**Implementation** (`auth/oauth_code.py::authorize_url`):
- The parameters are exactly `client_id`, `response_type=code`, `redirect_uri` and `state`.
- `action`, `username` and `password` are never sent, so the browser handles sign-in.
- `redirect_uri` is `service_base_url + /oauth/bullhorn/callback`, and `deploy.py` enforces it.
- The API-key registration of that URI is an administrator task (Q-A2).

### HV-C2: SSO / Duo (unresolved)

S1 does not mention SSO, an identity provider or MFA; it only describes "a valid Bullhorn username/password combination" on the Bullhorn login page. S2 mentions SAML in exactly one place: the CorporateUser entity example (`"samlInfo" : { "samlIdpID" : 456, "nameID" : "ssoEmail@email.com", "idpType" : 1 }`). That example describes stored user fields; it says nothing about the OAuth authorize, redirect or token behaviour (correction recorded by 5A triage L-10). Whether `/oauth/authorize` delegates to a tenant's IdP and still returns a `code` to `redirect_uri` is therefore **not documented**; the conclusion is unchanged.

**Guard (A3-6):**
- A tenant with `sso: true` refuses `bullhorn_session(login)` with `unsupported_sso` for everyone except that tenant's setup admins.
- A setup admin's login runs in **verification mode** and appends a secret-free observation to `<setup_store>/verifications/sso_login.jsonl`.
- Ordinary users' logins are enabled only by a committed `enable_sso_login {verification_id}` that names a positive observation for the current auth host and OAuth client configuration.
- A change of `auth_url` or of the client configuration re-disables it.
- **Recorded limit:** the server cannot observe the browser leg through the IdP. A record attests only the server-observable contract: a `code` with a valid `state` arrived at `redirect_uri`, and the exchange and login succeeded.
- **External dependency (EXT-2):** the user's SSO tenant needs one real admin verification login.

### HV-C3: token endpoint (mostly verified)

S1, verbatim:

> Make a POST request with the following URL ... If you specified a redirect_uri in the oauth/authorize request, you must specify the same redirect_uri in this request.
> `https://auth-{value_from_loginInfo}.bullhornstaffing.com/oauth/token?grant_type=authorization_code&code={auth_code}&client_id={client_id}&client_secret={client_secret}&redirect_uri={optional redirect_uri}`

> The access token is valid for 10 minutes. If Bullhorn has provided you with the ability to generate refresh tokens, the POST response also contains a refresh token.

> A new refresh token is returned with every new access token. The refresh token has no expiration date/time, but it does expire when a new access token and refresh token are generated.

> `https://auth-{value_from_loginInfo}.bullhornstaffing.com/oauth/token?grant_type=refresh_token&refresh_token={refresh_token}&client_id={client_id}&client_secret={client_secret}`

**Not documented:** the JSON field names of the token response. No example body is shown.

**Implementation (`parse_token_response`):**
- Fields are parsed **strictly** with the RFC 6749 §5.1 names: `access_token` (required string), `refresh_token` (optional string) and `expires_in` (optional integer from 1 to 86400; absent means the documented 600 s).
- Any other shape fails closed with `token_response_invalid`.
- POST is used, with the parameters in the query string as documented. The same `redirect_uri` is sent.
- The rotated refresh token replaces the stored one in a single atomic write (`sessions._refresh_locked`).
- Because the parameters are documented in the query string, `auth/secrets.py::RedactingFilter` redacts `code`, `state`, tokens, secrets and passwords from the `httpx`/`uvicorn` log records. It is installed by every shared deployment (AC-15).

### HV-C4: REST login and session lifetime (verified)

S1:

> `POST https://rest-{value_from_loginInfo}.bullhornstaffing.com/rest-services/login?version=*&access_token={xxxxxxxx}` ... `{ "BhRestToken" : "...", "restUrl" : "https://rest{swimlane#}.bullhornstaffing.com/rest-services/{corpToken}/" }`

> When the current session key expires, your query will return a 401 response. On this response you should perform the OAuth refresh token flow

S2 (login):

> Never assume that a REST session will not expire. Perform a ping request to return the timestamp of the REST session expiration. ... `ttl` no Session time-to-live in minutes.

S2 (ping):

> `{ "sessionExpires" : 1323449994922 }` Returns the date of the calling client's session expiration.

S1:

> Bullhorn will apply strict limits to login rates, and may block login requests that occur too frequently.

**Implementation:**
- `rest_login` uses POST. `restUrl` must be a trusted `https` URL of the form `/rest-services/{corpToken}/`.
- `tenant_key = sha256(corpToken)` must equal the selected tenant at login and at every refresh (D-5A-14). Otherwise the login is refused, or the session is deleted.
- A session is treated as valid for at most 600 s. On expiry, or on any 401 (via the unchanged `client.py` retry path), it is refreshed under the per-`(tenant, principal)` lock. That lock prevents parallel logins (the rate limit).
- `ping` is used only in SSO verification mode, to record the observed session lifetime.

### HV-C5: current-user CorporateUser ID (unresolved)

This is unchanged from HV-B11 (`PHASE4B_HV_VERIFICATION.md`). The login response carries only `BhRestToken` and `restUrl`, and ping carries only `sessionExpires`. S1 and S2 show no user ID under per-user sessions either.

**Effect:**
- `principal.HV_C5_VERIFIED = False`.
- The execution identity is the session label. `commentingPerson` is never sent from a guessed value.
- `create_note` enablement goes through the D-5A-15 admin verification procedure (A3-4). The verification write omits `commentingPerson`, whose documented default is the creating user (HV-B8), and records `commentingPerson` as read back.

### HV-C6: token revocation (unresolved)

S1 and S2 document no OAuth revocation endpoint. S2 documents only REST `GET /logout` ("Log out and invalidate your REST session"), which is not a token revocation.

**Effect:** `bullhorn_session(logout)` deletes only the caller's own `(tenant_key, principal_key)` entry. It is local only and documented as such in the tool docstring.

### HV-C7: PKCE (unresolved)

`code_challenge`, `code_verifier` and PKCE appear nowhere in S1 or S2.

**Effect:**
- No PKCE.
- A confidential client: the secret is resolved server-side from an `env:`/`file:` reference and never sent to a browser.
- `state` is 256-bit, single-use, expires after 10 minutes, and is bound to the browser by an `__Host-` cookie set at `/start`.

### HV-C8: data-center hosts (verified for the redirect)

S1 and S2:

> If you do not use the correct URLs for a user, you will receive a 307 redirect to the correct data center. Your code must be written to handle that 307 redirect.

**Effect:**
- The token exchange and REST login start at the configured `auth_url`/`login_url`. These must be trusted `https` Bullhorn origins (`TrustedOriginPolicy`, label boundary).
- A 307/308 is followed at most twice, only to a trusted `https` host and the same path. Anything else is `untrusted_redirect`.
- The origin that issued the tokens is stored (`auth_origin`) and reused for refresh, which only accepts a trusted origin.
- The browser follows the authorize-step 307 itself.

### HV-C9: concurrent sessions (unresolved)

S1 and S2 do not say whether a new login invalidates other `BhRestToken`s of the same user or client.

**Effect:** refresh, the forced refresh after a 401, and logout are serialised per `(tenant, principal)` lock. After a 401, a refresh is skipped when another call already refreshed.

### HV-C10: rate limits (verified)

S2, Errors:

> 429 Rate Limited – Wait 1 second then retry request. Repeat until successful.

**Effect in 5A:**
- Nothing is retried automatically, neither OAuth/login calls (login rates are limited, and codes are single use) nor writes. This is fail-safe.
- Bounded read back-off would require changing the frozen `client.py` request path. That is out of the 5A allowlist, so it is reported for the 5C query layer (D-5-19 lists retries and rate-limit handling).
- A 429 surfaces as the existing bounded `BullhornAPIError`.
