# Phase 5A Work Package: Identity & Sessions (Per-User Bullhorn Auth on a Shared HTTP MCP Server)

- **Status:** **COMPLETE** (both re-reviews PASS, 2026-10-07; see "5A Close-out" at the end). Binding Architect specification, 2026-10-07. **Amendments A1, A2, A3 and A4, and the "5A Review Triage"** (at the end of this document) are binding. Where an amendment or the triage disagrees with the base text, the later text wins.
- **Governing decisions:** `PHASE5_PROPOSAL.md` §0: D-5-1, D-5-2, D-5-3, D-5-7, D-5-8, D-5-9, D-5-10, D-5-11, D-5-12, D-5-13, D-5-17, D-5-18, D-5-20, D-5-21, D-5-22, D-5-23 and D-5-24 (the hook only).
- **Baseline:** 5B HEAD. 5B must be green before 5A starts.
- **Tool count:** 19 → **20** (adds `bullhorn_session`).
- **Reviewers:** both the Independent Reviewer and the Security & Identity Reviewer are required (§9).

---

## 0. MCP SDK facts this design relies on (`mcp==1.30.0`, read from the installed source)

These are the HV-M items. The Builder re-verifies each one with a test.

| ID | Fact | Source |
|---|---|---|
| HV-M1 | `FastMCP(..., token_verifier=TokenVerifier, auth=AuthSettings(issuer_url, resource_server_url, required_scopes, validate_token_resource))`. On streamable-HTTP this installs `BearerAuthBackend` and `RequireAuthMiddleware`. A missing or invalid bearer token gets a **401 before any tool runs**. | `mcp/server/fastmcp/server.py:163-247, 1003-1044`; `mcp/server/auth/middleware/bearer_auth.py:49-141` |
| HV-M2 | `TokenVerifier.verify_token(token) -> AccessToken \| None`. `AccessToken` has `client_id`, `scopes`, `expires_at`, `resource`, `subject` (the RFC 7662/9068 `sub`, "unique only per issuer") and `claims` (for example `iss`). | `mcp/server/auth/provider.py:40-47, 97-115` |
| HV-M3 | `get_access_token()` reads a contextvar that `AuthContextMiddleware` sets **per HTTP request**. | `mcp/server/auth/middleware/auth_context.py:10-48` |
| HV-M4 | Stateful streamable-HTTP binds each MCP session to its creator `(client_id, issuer, subject)` and rejects a request from a different principal. | `mcp/server/streamable_http_manager.py:275-328` |
| HV-M5 | `custom_route(path, methods)` registers **unauthenticated** HTTP routes, which is intended for OAuth callbacks. | `server.py:721-746` |
| HV-M6 | **To verify by test:** inside a tool handler, under `stateless_http=True`, `get_access_token()` returns *that request's* token, including under concurrent requests. If it does not, the design falls back to `Context.request_context.request.user`. If neither works, shared mode is not shippable, and the Builder must stop and escalate. | — |

## 1. Decisions (D-5A-n)

### 1.1 Deployment and caller authentication

| ID | Decision |
|---|---|
| D-5A-1 | **Deployment modes.** These are set in an out-of-repo admin config file named by `BULLHORN_ADMIN_CONFIG` (YAML, read via `tenant/yaml_strict`).<br>**`local`** (the default when no admin config exists): stdio only. It behaves exactly as today. It is the legacy test mode.<br>**`shared`**: streamable-HTTP only, with `stateless_http=True` (a fresh transport per request, so no cross-request state). An MCP `token_verifier` and `AuthSettings` are **mandatory**.<br>Startup **refuses**: `shared` without auth configuration; `local` with any HTTP transport; and `shared` on stdio. Production is never silently `local`. |
| D-5A-2 | **Caller principal.** In `shared` mode the principal comes from `get_access_token()` (HV-M3/M6). It is `principal_key = sha256(issuer, subject)`, and `display` comes from configured claims such as `email` and `name`.<br>A missing token, `subject` or issuer, or an issuer not on the admin allowlist, → **fail closed** (`identity_required`). `client_id` alone is **never** a user identity.<br>In `local` mode, the principal is `local:<OS user>`. |
| D-5A-3 | **Token verifier adapters.** **JWT/JWKS:** issuer, audience = `resource_server_url`, signature and expiry are all required, and `validate_token_resource=True`. **RFC 7662 introspection** uses `httpx`. The admin selects one in config. Which host or IdP is the first target remains open question Q-A1, and both adapters ship. |

### 1.2 Bullhorn authentication and sessions

| ID | Decision |
|---|---|
| D-5A-4 | **Bullhorn per-user auth: the authorization-code flow.** Bullhorn is the authentication authority. The server **never** receives passwords or MFA/Duo codes. See the flow below. |
| D-5A-5 | **Legacy password-grant path.** It is retained only as **(a)** `local` mode's client (today's behaviour, unchanged) and **(b)** the **service identity** adapter in `shared` mode. It runs only for principals listed in `service_principals` in the admin config, and only **reads** (D-5-11).<br>Its redirect guard (`auth/bullhorn_password.py:102,113`) is replaced by `TrustedOriginPolicy`. The default allowlist is the host-suffix `.bullhornstaffing.com` **with a label boundary**, so `bullhornstaffing.com.attacker.example` is now rejected. This closes DEBT-1.<br>This is an approved security behaviour change (D-5-1). Legacy redirect-guard tests must still pass unmodified. If any of them depends on substring semantics, **stop and escalate**. |
| D-5A-6 | **Session store** (D-5-12). The interface is `SessionStore.get/put/delete(tenant_key, principal_key)`, plus `put_login(state)` / `take_login(state)` for pending logins. Stored items:<br>• the refresh token;<br>• the access token and its expiry;<br>• the `BhRestToken` and its expiry;<br>• `rest_url`;<br>• `bullhorn_user_ref` (if resolvable; HV-C5);<br>• `created_at` and `last_refresh`.<br>**Production adapter `EncryptedFileSessionStore`:** one file per `(tenant_key, principal_key)`. Each file is encrypted with **AES-256-GCM** (`cryptography`), using **AAD = `tenant_key \| principal_key \| schema_version`**. That binds the ciphertext to its owner: a swapped file fails to decrypt and the read fails closed.<br>**Key source:** `BULLHORN_SESSION_KEYS` (env or secret file), a key-ID → 32-byte key map with an active key ID. This supports rotation. A missing key means `shared` mode refuses to start.<br>**Writes** are atomic and owner-only (`0600` where the OS supports it). The directory is never inside the 4A setup store.<br>**Dev adapters:** `MemorySessionStore` (tests) and an optional OS-keyring adapter, which is local-only and refused in `shared` mode. |
| D-5A-7 | **Approved new runtime dependencies** (`pyproject.toml`): `cryptography>=42` (AES-GCM for the session store) and `PyJWT>=2.8` (JWT/JWKS verification for D-5A-3). Both are widely used and maintained. `cryptography` is already the de-facto Python primitive library, and `PyJWT` uses it. No other dependency is added. |
| D-5A-8 | **Session isolation** (replaces `server._client`). `server.get_client()` keeps its name and signature.<br>In `local` mode its body is unchanged (the process-global `_client`, exactly as today).<br>In `shared` mode it delegates to `identity.sessions.resolve_client()`, which proceeds as follows:<br>• it reads the principal (D-5A-2);<br>• it chooses the user session, or a service session only for service principals;<br>• it loads the session from the store;<br>• it refreshes it if expired, under a per-`(tenant, principal)` lock;<br>• it returns a **per-call** `BullhornClient` bound to that session's auth.<br>A **process-global** client is never used in `shared` mode. A user with no session raises `BullhornSessionRequired`. The legacy tools already convert that into their `ERROR:` / `connected: false` paths. There is **no fallback** to service credentials. |

**The D-5A-4 flow, step by step:**

1. `bullhorn_session(action="login")` creates a pending login. It records:
   - `state`: 32 random bytes;
   - a PKCE verifier, **only if HV-C7 is verified**;
   - `principal_key` and `tenant_key`;
   - creation time and expiry (10 minutes);
   - a single-use flag.

   It returns `https://<service-base>/oauth/bullhorn/start?login=<id>`.
2. `/oauth/bullhorn/start` (a `custom_route`) redirects the browser to the Bullhorn authorize URL (HV-C1), using `redirect_uri = https://<service-base>/oauth/bullhorn/callback` (a registered, allowlisted HTTPS URL).
3. Bullhorn runs its own SSO/Duo (HV-C2) and redirects to `/oauth/bullhorn/callback?code&state`. The callback:
   - validates `state` (it must be known, unexpired and unused, and is consumed atomically);
   - exchanges the code at the verified token endpoint and host (HV-C3/C8);
   - performs the REST login (HV-C4);
   - then shows a **confirmation page**. The page names the workspace principal (`display`) that will be linked, and the Bullhorn identity if it can be resolved. It has a CSRF-protected POST "Link this Bullhorn account".

   **Only after that POST** is the session stored under `(tenant_key, principal_key)`. This mitigates login-CSRF and account-linking attacks, where a victim might otherwise complete an attacker-initiated login.
4. Every failure renders a generic page and logs a bounded, redacted reason. Codes and tokens are never echoed.
5. **Refresh** uses the refresh grant (HV-C3). If refresh fails, the session is deleted and `BullhornSessionRequired` is raised.
6. **Logout** deletes **only** that `(tenant_key, principal_key)` entry, and calls the revocation endpoint only if HV-C6 is verified.
7. **Expiry** fails closed and is never silently extended past the refresh-token lifetime.

### 1.3 Roles, ownership and scoping

| ID | Decision |
|---|---|
| D-5A-9 | **Identity model** (D-5-17). Every tool invocation carries an `IdentityContext{initiating_principal, tenant_key, executing_bullhorn_identity, mode: user\|service\|local}`. `approver` is set only by `confirm_write` / `commit_mapping_changes`. `service_identity` is set only for service principals.<br>`executing_bullhorn_identity` is the session's `bullhorn_user_ref` if HV-C5 is verified. Otherwise it is `bh-session:<sha256(tenant_key\|principal_key)>`, never a guessed user ID.<br>No tool accepts an identity argument. |
| D-5A-10 | **Roles** (PA-3). The admin config has `roles.setup_admins` and `roles.write_approvers`, listing principal keys or `(issuer, subject)` pairs. `tenant/actor.py` derives the actor from the `IdentityContext`.<br>`BULLHORN_MCP_ACTOR`, `BULLHORN_SETUP_ADMINS` and `BULLHORN_WRITE_APPROVERS` are honoured **only in `local` mode**, as back-compat aliases. In `shared` mode they are ignored, and a startup warning is logged if they are set. |
| D-5A-11 | **Preview ownership** (D-5-10). A pending write records `{initiating_principal, tenant_key, executing_bullhorn_identity, op hash}`. `confirm_write` requires the **same** principal, tenant and execution identity. A different principal gets `denied`, even if they hold `write_approvers`, because there is no cross-user confirm in 5A. The same rule applies to `commit_mapping_changes`, for proposals created by a different principal. The exception is setup admins confirming their own proposals; that is unchanged. |
| D-5A-12 | **Service identity** (D-5-11). Service principals may call **read** tools only. These are denied for them: `create_note`, `confirm_write`, `propose_mapping_changes`, `commit_mapping_changes`, `manage_mapping_profile(export)`, `upload_candidate_resume`, and every Phase 7 write. Enforcement is in `crosscutting/permissions.check()` (§2), using the identity context. |
| D-5A-13 | **Shared-mode filesystem tools.** In `shared` mode, `upload_candidate_resume` (it reads a server-local `file_path`) is **denied** for everyone, with reason `local_file_paths_unsupported_in_shared_mode`, until Phase 7/8 adds an upload channel. `manage_mapping_profile(export)` and `import_document` paths must resolve **inside** the tenant's admin-configured `exchange_dir`, or they are refused. This also closes P4A-12 for shared mode. |
| D-5A-14 | **Scoping.** Tenants are declared in the admin config as `tenants: {tenant_key: {setup_store, exchange_dir, bullhorn_oauth: {client_id, client_secret_ref, redirect_uri}}}`. `tenant_key = sha256(corpToken from rest_url)` is checked against config at login. A session whose `rest_url` tenant is not configured is refused.<br>The 4A setup store is **tenant-level** (shared by that tenant's users).<br>The 4B ledger key, pending operations and journal lines add `tenant_key`, `initiating_principal` and `executing_bullhorn_identity`, and the ledger directory is per tenant.<br>Session data is **user-level**, in the session store only. |
| D-5A-15 | **`create_note` enablement** (D-5-18 / D-5-9). See the steps below. |
| D-5A-16 | **Canonical `user` entity** (D-5-13). Added to `canonical_schema.yaml` with these fields: `id` (id), `date_added`, `first_name`, `last_name`, `full_name`, `email`, `status`. It has **no** Bullhorn catalog binding in 5A; the CorporateUser binding and reads arrive in 5C. 5A uses it as the attribution shape. NB-4 `ref` validation is now satisfiable for `user`. The approved test change is in §2. |

**The D-5A-15 steps:**

1. **Check the documentation.** Re-check HV-B11 / HV-C5 against the docs under per-user sessions. If a documented current-user ID exists, record it, send `commentingPerson` from the session, and set the flag.
2. **Otherwise, the admin verification procedure** (implemented in 5A, because it needs only 5A identity plus the existing 4A and 4B mechanisms):
   - **(a) Authorize.** A setup admin proposes the op `authorize_note_write_verification {target_type ∈ {candidate, client_contact}, target_id, action_type}` and commits it. The target must be one the admin designates as a test record. The commit records the authorization: who, which target, single-use, expiring after 24 hours.
   - **(b) Preview.** That **same admin principal** calls `create_note` on exactly that target and action. The HV-B11 guard is lifted **only** for that exact `(tenant, principal, target, action)` match, and only once. The admin sees the normal preview.
   - **(c) Confirm and write.** `confirm_write` executes **exactly one** Note write (with a fixed ledger key `verification:note:v1:<tenant>`). The write is read back, including `entities`.
   - **(d) Record.** The verdict is recorded append-only in `<setup_store>/verifications/note_write.jsonl`, with:
     - the Note ID;
     - the associations;
     - whether a NoteEntity is present;
     - `commentingPerson` as read back;
     - who, when and the correlation ID.
   - **(e) Never repeated or automatic.** A second authorization is refused while any verdict exists, unless the admin proposes and commits `reset_note_write_verification`. The procedure never runs automatically.
3. **Enablement rule.**
   - `create_note` person targets are enabled for a tenant **iff** a positive verdict exists (a NoteEntity is present for the `personReference` target), **and** P4B-8 is closed (5B), **and** `rest_url_fingerprint` is unchanged since the verdict. A fingerprint change means revalidation.
   - **Architect determination:** one positive, read-back-verified observation is sufficient and repeatable for that tenant. The behaviour is server-side and deterministic per tenant configuration, and a drift trigger re-requires verification.
   - A negative verdict keeps `create_note` disabled for that tenant.

## 2. Pre-existing files: exact allowlist (case 10 is scoped to this)

| File | Phase | Exact nature (additive except as stated) |
|---|---|---|
| `src/bullhorn_mcp/server.py` | protected | **(a)** One import line for `identity.deploy`. **(b)** `FastMCP(...)` construction adds `**deploy.fastmcp_auth_kwargs()`, which is `{}` in `local` mode. **(c)** `get_client()`: in `shared` mode, an early return delegating to `identity.sessions.resolve_client()`. The existing `local` body is unchanged. **(d)** `main()`: transport selection via `deploy.run_args()`; `local` → `mcp.run()` exactly as today. No other change. `_client` remains (local mode). |
| `src/bullhorn_mcp/tools/__init__.py` | protected | **One added line** after the 4B line: `from . import session  # noqa: F401` |
| `src/bullhorn_mcp/auth/bullhorn_password.py` | protected | Lines 102 and 113: replace the substring tests with `trusted_origins.is_trusted_bullhorn_host(parsed.netloc)`, plus one import line. Nothing else. (The triage's L-7 may change the same two expressions once more.) |
| `src/bullhorn_mcp/crosscutting/permissions.py` | protected | `check(tool, operation)` keeps its signature. In `local` mode it behaves as today (default allow). In `shared` mode it consults the identity context: no principal → deny; service principal and a write tool → deny; `upload_candidate_resume` → deny (D-5A-13); and the Amendment A2 tier gate. |
| `pyproject.toml` | — | Add `cryptography>=42` and `PyJWT>=2.8` to the runtime dependencies. Nothing else. |
| `src/bullhorn_mcp/mappings/canonical_schema.yaml` | Phase 3 | Add the `user` entity (D-5A-16). |
| `tests/test_schema_canonical_catalog.py` | Phase 3 test | **An approved existing-test edit** (D-5-13). In `test_loads_via_importlib_resources` (line 68) and `test_exactly_the_ten_entities` (lines 72–73), replace the equality and count with:<br>`APPROVED_ADDITIVE_ENTITIES = {"user"}` (module constant)<br>`assert set(EXPECTED_FIELDS) <= set(catalog.entities)`<br>`assert set(catalog.entities) - set(EXPECTED_FIELDS) == APPROVED_ADDITIVE_ENTITIES`<br>`assert len(catalog.entities) == 10 + len(APPROVED_ADDITIVE_ENTITIES)`<br>Test names are unchanged and no other line changes. (Amendments A3-0 and A4-1 approve further test edits.) |
| `src/bullhorn_mcp/bullhorn/client.py` | protected | **Amendment A1-1 only**: the two expressions given there, inside the frozen 5B helpers. |
| 4A/4B modules (not protected) | 4A/4B | `tenant/actor.py` (identity-context actor), `tenant/store.py` (`exchange_dir` restriction; per-tenant roots), `tools/setup.py` (`setup_status` adds a session/identity block to its output, including `access_tier`; exchange-dir checks), `writes/pipeline.py`, `writes/policy.py`, `writes/ledger.py`, `writes/journal.py` (D-5A-11/14/15), `tenant/changes.py` (verification ops), `tenant/revalidation.py` (Amendment A1-2 only), `tenant/state.py` (Amendment A3-2 only). |

**Must not change:** `config.py`, `auth/__init__.py`, `auth/base.py`, `bullhorn/client.py` beyond A1-1, the legacy `tools/*.py` modules (`jobs`, `candidates`, `placements`, `generic`, `system`), every other test, and the catalogs other than as stated.

**Legacy tests (`test_server.py` and so on) must pass UNMODIFIED.** They run in `local` mode, which is the default with no admin config, so their behaviour is unchanged. No exception is requested. If any fails, stop and escalate.

## 3. New files

```
src/bullhorn_mcp/identity/__init__.py
src/bullhorn_mcp/identity/deploy.py          # admin config load/validate (yaml_strict), mode, startup refusals, fastmcp_auth_kwargs, run_args
src/bullhorn_mcp/identity/principal.py       # IdentityContext (incl. access_tier, A2), principal from access token / local OS user, fail-closed rules
src/bullhorn_mcp/identity/verifiers.py       # JwksTokenVerifier, IntrospectionTokenVerifier (TokenVerifier protocol)
src/bullhorn_mcp/identity/session_store.py   # SessionStore ABC, EncryptedFileSessionStore (AES-GCM+AAD), MemorySessionStore, KeyringSessionStore(local only)
src/bullhorn_mcp/identity/sessions.py        # resolve_client(), per-(tenant,principal) refresh lock, BullhornSessionRequired
src/bullhorn_mcp/identity/roles.py           # setup_admins / write_approvers / service_principals from admin config
src/bullhorn_mcp/auth/trusted_origins.py     # TrustedOriginPolicy (DEBT-1)
src/bullhorn_mcp/auth/secrets.py             # CredentialSource abstraction (OBS-1) used by both auth adapters
src/bullhorn_mcp/auth/oauth_code.py          # authorization-code client: authorize URL, code exchange, refresh, revoke(if verified), REST login
src/bullhorn_mcp/identity/oauth_routes.py    # custom_route /oauth/bullhorn/start, /callback, /confirm (CSRF), generic error pages
src/bullhorn_mcp/tools/session.py            # bullhorn_session tool
tests/test_identity_*.py, tests/test_oauth_*.py, tests/test_session_store*.py, tests/test_phase5a_security_*.py
docs/architecture/PHASE5A_HV_VERIFICATION.md # HV-M1..M6, HV-C1..C10 (Builder)
```

## 4. Tool: `bullhorn_session` (tool 20)

**Signature:** `bullhorn_session(action: str = "status")`, with `action ∈ {status, login, logout}`. It takes **no other parameters**. (Superseded by triage B-2: an action `complete_link` and a parameter `confirmation` are added.)

| Action | Returns |
|---|---|
| `status` | `{mode, principal_display, tenant_key_hint, session: active\|expired\|none\|service, expires_at, executing_identity_label, access_tier, create_note_enabled_for_tenant}` |
| `login` | `{login_url, expires_at}`. Refused in `local` mode, where the env password grant is used. |
| `logout` | `{logged_out: true}`. Removes only the caller's own session for the current tenant. |

No action ever returns tokens, codes or secrets.

## 5. HV-C items (Bullhorn; record in `PHASE5A_HV_VERIFICATION.md`; unresolved → fail closed)

| ID | Mechanism | Guard if unresolved |
|---|---|---|
| HV-C1 | `/oauth/authorize` code flow: `client_id`, `response_type=code`, `redirect_uri`, `state` parameters; redirect-URI registration and matching rules | `shared` user mode disabled (login refused); `local` mode only |
| HV-C2 | SSO tenants: `/oauth/authorize` delegates to the IdP (SSO / Duo) and returns a `code` to `redirect_uri` | Login refused with `unsupported_sso` for SSO tenants. Verification against a connected tenant by an admin is allowed (HV-1 rule 2). |
| HV-C3 | `/oauth/token`: `authorization_code` and `refresh_token` grants; response fields (`access_token`, `refresh_token`, `expires_in`); refresh-token rotation | Same as HV-C1 |
| HV-C4 | `/rest-services/login` with a per-user `access_token` → `BhRestToken`, `restUrl`; session TTL | Same as HV-C1 |
| HV-C5 | Current-user CorporateUser ID (= HV-B11) | Use the D-5A-15 procedure |
| HV-C6 | Token revocation endpoint | Logout is local-only and documented as such |
| HV-C7 | PKCE support (`code_challenge` / S256) | No PKCE: a confidential client plus state, with the client secret kept server-side |
| HV-C8 | Regional / data-center auth hosts during the code flow: which host to exchange at, and how swimlane redirects work | Exchange only at the host verified by HV-C1/C8, using the allowlist; otherwise refuse |
| HV-C9 | Concurrent-session limits (does a new login invalidate other `BhRestToken`s for the same user or client?) | Serialize per principal and document |
| HV-C10 | Rate limits / 429 | Bounded back-off on reads; **no** automatic retry of writes |

## 6. Acceptance criteria (mechanically checkable)

### Gates

- **AC-1.** Full `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` exit 0. The 25/25 regression suite passes, with case 10 scoped to the §2 allowlist and case 11 per D-5-13. The wheel builds with the new modules.
- **AC-2.** `git diff <5B-head> --name-status` shows only `A` entries plus `M` for the §2 files. The test diff is exactly the approved `test_schema_canonical_catalog.py` hunk, the Amendment A3-0 and A4-1 hunks, plus new tests.
- **AC-3.** The registry has exactly 20 tools. The legacy, 4A and 4B schema pins pass unmodified. A new test pins `bullhorn_session`'s schema (a single `action` parameter; amended by triage B-2 to `action` and `confirmation`).

### Modes and startup

- **AC-4.** With no admin config, every legacy test passes unmodified and `server._client` local behaviour is unchanged. `shared` mode without a verifier, issuer or session keys refuses to start; a test asserts the exception. `local` mode with streamable-HTTP refuses to start.

### Identity and isolation (blocking; these mirror `SECURITY_REVIEWER.md`)

- **AC-5.** No registered tool schema has any of these properties: `actor`, `user`, `principal`, `username`, `password`, `subject`, `issuer`, `tenant`, `service`, `identity`, `token`, `code`, `tier`, `access_tier`.
- **AC-6.** A missing bearer token gives a 401 before any tool runs. A token with no `subject`, or from an issuer not on the allowlist, makes every tool return `identity_required`, with **zero** Bullhorn HTTP calls.
- **AC-7.** A forged, expired or wrong-audience JWT is rejected by the verifier (JWKS fixture keys; a wrong key, a tampered payload, `exp` in the past, a different `aud`).
- **AC-8.** In a concurrent harness with N ≥ 20 interleaved requests from users A and B in the same tenant (`stateless_http`), every Bullhorn request carries the requesting user's own `BhRestToken`, asserted per request through respx. No request uses the other user's token, and none uses a process-global client.
- **AC-9.** The same user in two tenants gets two independent sessions. A token or session for tenant 1 is never used for tenant 2. A `rest_url` whose tenant is not configured is refused at login.
- **AC-10.** A's `logout` deletes only `(tenant, A)`. B's and A's other-tenant sessions remain valid.
- **AC-11.** An expired session with a failing refresh gives `BullhornSessionRequired` and deletes the session. **No** service-credential request is made (respx asserts that no password-grant endpoint is called).
- **AC-12.** A user without a session gets an error. The service identity is never used implicitly. A service principal calling any write tool, or `upload_candidate_resume`, gets `denied`.
- **AC-13.** `confirm_write` by B on A's preview gives `denied`, even if B is in `write_approvers`. A confirm under a changed execution identity or tenant gives `denied`. `commit_mapping_changes` follows the same ownership rule.
- **AC-14.** The audit, journal and ledger records of a write carry `initiating_principal`, `tenant_key` and `executing_bullhorn_identity`, and these equal the requester's.

### Secrets, store, redirects and paths

- **AC-15.** Tokens, auth codes, client secrets and session keys never appear in any of the following. This is checked by grep over captured output and by a sentinel-token test:
  - tool output;
  - `caplog`;
  - audit / journal;
  - 4A store files;
  - exception `str` / `repr`.
- **AC-16.** Session store files contain no plaintext token (a byte search for the sentinel finds nothing). A file copied from (T, A) to (T, B) fails to decrypt and the read fails closed. Rotating to a new active key ID still decrypts old files, while a removed key fails closed.
- **AC-17.** At the callback:
  - an unknown, expired, reused or mismatched `state` → a generic error with no session stored;
  - `code` and `state` are never echoed;
  - the session is stored only after the CSRF-protected confirm POST;
  - a confirm POST without a valid CSRF token, or for another login, is rejected.
- **AC-18.** `TrustedOriginPolicy` accepts the configured Bullhorn hosts at a label boundary. It rejects `bullhornstaffing.com.attacker.example`, `evilbullhornstaffing.com`, `http://` (non-TLS) and userinfo tricks. The legacy redirect-guard tests pass unmodified.
- **AC-19.** In `shared` mode, `upload_candidate_resume` → denied. Export or import paths outside `exchange_dir`, including through symlinks and `..`, → refused.

### `create_note` and the canonical entity

- **AC-20.** `create_note` stays disabled unless HV-C5 is verified or a positive verification verdict exists, **and** P4B-8 is closed. The verification procedure enforces all of the following, each tested:
  - admin-only;
  - preview first;
  - an exactly matching target;
  - a single write (a second attempt is refused);
  - the verdict is recorded;
  - a fingerprint change re-disables `create_note`;
  - a negative verdict keeps it disabled.
- **AC-21.** The canonical catalog has 11 entities, `user` included. The approved test hunk is the only Phase 3 test change. `ref: user` now validates.

## 7. Out of scope

- `find_records` / `get_activity`, and the CorporateUser binding for `user` (5C).
- Any Tier 2 analytics, the metric catalog, thresholds or de-identification (Phase 6). 5A delivers only the A2 hook.
- Cross-user approver workflows (a future explicit role).
- Service-identity writes.
- A DB-backed session store (the interface allows adding one later).
- Bulk or remote upload channels.
- DEBT-2's `config/` package (Phase 9).

## 8. Privacy (D-5-22)

The admin config, tenants, OAuth client IDs and secrets, session keys and verification verdicts all live **outside the repo**. Tests use synthetic issuers, keys and tenants only.

## 9. Security & Identity Review: REQUIRED (blocking)

The Security & Identity Reviewer must independently reproduce **every** item in the 5A minimum attack set of `docs/process/SECURITY_REVIEWER.md`. Each is blocking:

| Attack | Maps to |
|---|---|
| A cannot receive or use B's session | AC-8 |
| A cannot confirm B's preview | AC-13 |
| Same tenant, different users stay isolated | AC-8 |
| Same user, different tenants stay isolated | AC-9 |
| Logout scope | AC-10 |
| Expired sessions fail closed | AC-11 |
| Missing identity fails closed | AC-6 |
| Forged identity fails closed | AC-6, AC-7 |
| No implicit service fallback | AC-11, AC-12 |
| LLM arguments cannot override identity | AC-5 |
| Tokens never visible | AC-15, AC-16 |
| Concurrent requests never cross sessions | AC-8 |

The reviewer must also attack:
- callback / state / CSRF forgery and login-CSRF account linking (AC-17);
- open redirects and the allowlist (AC-18);
- session-store file swap and key rotation (AC-16);
- cache-key collisions: `principal_key` built from `(issuer, subject)`, never from `client_id` or display;
- audit-actor vs execution-identity mismatches (AC-14);
- the verification procedure's single-write guarantee (AC-20);
- path escape in shared mode (AC-19);
- Amendment A1 (R-A1a, R-A1b), Amendment A2 (SA2-1..SA2-5), Amendment A3 (SA3-1..SA3-6), Amendment A4 (SA4-1) and the triage items (T-B1..T-B4).

**Process:** Builder → fresh Independent Reviewer → fresh Security & Identity Reviewer → gates. If either reviewer fails the sub-phase: Architect triage → fix → a fresh instance of each affected reviewer → gates. No commit until all of Phase 5 passes.

## 10. New Phase 5 security-regression cases for the harness

| ID | Case |
|---|---|
| SR-1 | No identity-like parameter appears in any tool schema (AC-5). |
| SR-2 | Startup refusals for misconfigured modes (AC-4). |
| SR-3 | Missing or subject-less principal → no Bullhorn call (AC-6). |
| SR-4 | Forged, expired or wrong-audience token rejected (AC-7). |
| SR-5 | Two-user concurrent isolation (AC-8). |
| SR-6 | Same user across two tenants (AC-9). |
| SR-7 | Logout scope (AC-10). |
| SR-8 | Expired session → no service fallback (AC-11). |
| SR-9 | Service principal write denial, including `upload_candidate_resume` (AC-12). |
| SR-10 | Cross-principal confirm/commit denial (AC-13). |
| SR-11 | Identity triple in audit, journal and ledger (AC-14). |
| SR-12 | Secret sentinel never leaks (AC-15). |
| SR-13 | Session-file swap and key rotation (AC-16). |
| SR-14 | Callback state, CSRF and replay (AC-17). |
| SR-15 | Trusted-origin label boundary (AC-18). |
| SR-16 | Shared-mode path confinement (AC-19). |
| SR-17 | Verification procedure single-write and enablement rule (AC-20). |
| SR-18 | Legacy `local`-mode byte-identical behaviour: the existing legacy suite, unmodified. |
| SR-19 | Tier gate: a `workspace_only` caller is denied every non-allowlisted tool with zero Bullhorn or service calls, and newly registered tools are denied by default (Amendment A2). |

---

## Amendment A1 (2026-10-07): two 5B follow-ups folded in (minimal)

This amendment folds in two items from `DEFERRED_DEBT.md` "Phase 5B close-out": P5B-10 and P5B-11. Both are hardening of the shared-server attack surface. They are folded into 5A so they land before multi-user exposure.

### A1-1: exact `type()` checks in `bullhorn/client.py` (P5B-10 / Sec-N-1)

5B froze the `client.py` hunk by content hash. A1-1 changes **exactly two expressions** inside the helpers that 5B added. No other line of `client.py` may change.

| Helper | Expression now | Replace with |
|---|---|---|
| `_check_entity` | `if not isinstance(entity, str) or _ENTITY_NAME_RE.fullmatch(entity) is None:` | `if type(entity) is not str or _ENTITY_NAME_RE.fullmatch(entity) is None:` |
| `_check_entity_id` | `if isinstance(entity_id, bool) or not isinstance(entity_id, int):` | `if type(entity_id) is not int:` |

**Why.** A `str` or `int` subclass can override `__format__`, `__str__` or `__eq__`, and so inject a path segment at the f-string. Accepting only the exact builtin types closes that, and `bool` is excluded by construction. Valid callers are unaffected: pydantic passes plain `str` and `int`.

**Case 10.** `client.py` is added to the §2 allowlist as "the frozen 5B hunk with exactly these two expressions changed". The coordinator re-freezes the hash.

**Tests (R-A1a).** A `str` subclass whose `__format__` and `__str__` return `"../settings/x"`, and an `int` subclass whose `__format__` returns `"1/../../settings"`, are each rejected, with zero requests. R-B1a..e still pass unmodified.

### A1-2: the settings flag filter applies to drift too (P5B-11 / Ind-NB-1 / Sec-N-4)

`tenant/revalidation.py` is added to the §2 4A/4B-module row. In `note_action_drift`, the `settings` source is treated as **absent** whenever `SETTINGS_ACTION_SOURCE_VERIFIED` is `False`. This is the same rule as the 5B fix to `verified_sources_by_value`, applied wherever `sources.verified()` is consulted for drift.

**Tests (R-A1b).** A forged or stale snapshot claiming `settings: verified` with values, with the flag off, gives these results:
- the `settings` values do not appear in `new_values`;
- `stale_values` is computed from verified non-`settings` sources only;
- the 5B drift tests still pass unmodified, **except** the one test approved in Amendment A4-1.

**Review.** The Security & Identity Reviewer adds R-A1b to its 5A attack list.

---

## Amendment A2 (2026-10-07): two-tier hook (D-5-24) — identity only, no analytics logic

**Why it is needed.** The base 5A design already fails closed for a caller without a Bullhorn session, and it never falls back to the service identity. However, two things are not yet guaranteed:

- **No tier is exposed.** The identity context exposes no *tier* that 5C and Phase 6 can gate on.
- **Denial depends on each tool's error path.** Today a session-less caller is denied only because each tool happens to error. It is not denied **by policy, by default, for every tool, including tools added later**.

**Single source.** The two-tier model itself is defined in `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8 and is not restated here.

### A2-1: the `access_tier` value

`IdentityContext` gains `access_tier ∈ {bullhorn_user, workspace_only, service, local}`. It is computed only by `identity.principal` / `identity.sessions`, at request time:

| Mode / caller | `access_tier` |
|---|---|
| `local` mode | `local` (today's behaviour) |
| Service principal | `service` |
| Shared-mode principal **with** a valid linked Bullhorn session for that tenant (present, and unexpired or successfully refreshed) | `bullhorn_user` |
| Any other authenticated principal | `workspace_only` |

The tier is **never** derived from tool arguments, claims chosen by the client, or model output.

### A2-2: deny-by-default tier gate

This lives in `crosscutting/permissions.check()` (already on the §2 allowlist).

- In `shared` mode, a `workspace_only` caller may invoke **only** the tools in the explicit Tier 2 allowlist, `{bullhorn_session, setup_status}`.
- **Every other tool is denied**, with reason `bullhorn_auth_required`, **before** any Bullhorn call or service-session resolution. That includes the legacy 10, the setup/admin tools, the notes tools, and **any tool registered later**.
- Phase 6 adds `get_recruiting_metrics` to the Tier 2 allowlist, together with its de-identification layer. Nothing else is added without an Architect decision.
- `setup_status` for a `workspace_only` caller returns state and tier only, with no record data.

### A2-3: no service fallback for `workspace_only`

In 5A, the service session is resolved **only** for `access_tier = service`. A `workspace_only` caller never causes a service-session resolution, under any code path. The Phase 6 internal Tier 2 computation path is out of scope here.

### A2-4: exposure without parameters

`bullhorn_session(status)` and the `setup_status` output include `access_tier`. Neither tool gains a parameter.

### A2-5: files

No new files beyond §3. The allowlisted files are unchanged except as described above: `identity/principal.py`, `identity/sessions.py`, `crosscutting/permissions.py` (already allowlisted) and `tools/setup.py` (output only).

### A2-6: tests (R-A2a..e)

| ID | Test |
|---|---|
| R-A2a | A `workspace_only` principal invoking each of the 18 non-allowlisted tools gets `denied` / `bullhorn_auth_required`. respx records **zero** Bullhorn requests and **zero** password-grant / service-session resolutions. |
| R-A2b | A dummy tool registered in the test (not in the allowlist) is denied for `workspace_only` by default. |
| R-A2c | `access_tier` cannot be influenced by tool arguments. AC-5 is extended with `tier` and `access_tier`. Crafted token claims such as `access_tier` or `bullhorn_linked` are ignored. |
| R-A2d | Expiring a linked session flips the next request to `workspace_only`. Re-linking gives `bullhorn_user`. A tier computed for one request is never cached across principals. |
| R-A2e | Concurrent A (`bullhorn_user`) and B (`workspace_only`) requests: B is denied every record tool, and A's results never reach B. |

### A2-7: Security & Identity attack items (blocking, added to §9)

| ID | Attack |
|---|---|
| SA2-1 | A non-Bullhorn (`workspace_only`) user invokes record-level tools, legacy or new. |
| SA2-2 | Tier escalation via tool arguments or forged claims. |
| SA2-3 | Service-identity fallback for `workspace_only`. |
| SA2-4 | Tier cache confusion across principals, tenants or concurrent requests. |
| SA2-5 | A new tool bypasses the deny-by-default gate. |

**Regression harness.** Add **SR-19** (§10).

**Naming note.** The user's labels `workspace_analytics` and `service_identity` are the same tiers as `workspace_only` and `service` here; the names follow `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8.

---

## Amendment A3 (2026-10-07): Builder pre-implementation rulings

The 5A Builder stopped before implementing, on one spec conflict and five gaps. These rulings are binding. A3 also adds a tenant-level SSO enablement procedure, so that SSO tenants have a documented path to enablement that does not rely on guessing.

### A3-0: the 19-tool pins (BLOCKER). Approved test edits.

Registering tool 20 contradicts two existing exact-count pins. Shared-mode-only registration is **rejected**: it would make the registry mode-dependent and break AC-3. Two minimal hunks are **approved**. They follow the D-5-13 additive pattern. In each file, nothing else changes and test names are unchanged.

| File | Approved hunk |
|---|---|
| `tests/test_tools_notes.py` (committed in 4B) | Insert after the `ALL_TOOLS` set (after line 27): `APPROVED_ADDITIVE_TOOLS = {"bullhorn_session"}  # Phase 5 approved additive tools (D-5-15)`.<br>Line 85 becomes `assert set(server.mcp._tool_manager._tools) == ALL_TOOLS \| APPROVED_ADDITIVE_TOOLS`.<br>Line 86 becomes `assert len(server.mcp._tool_manager._tools) == 19 + len(APPROVED_ADDITIVE_TOOLS)`. |
| `tests/test_phase5b_setup.py` (5B, uncommitted) | Add one import line, `from .test_tools_notes import APPROVED_ADDITIVE_TOOLS`.<br>Line 117 becomes `assert len(server.mcp._tool_manager._tools) == 19 + len(APPROVED_ADDITIVE_TOOLS)`. |

Further requirements:
- A **new** 5A test pins the exact 20-name set.
- 5C may change only the `APPROVED_ADDITIVE_TOOLS` line, to add `find_records` and `get_activity`. Recorded in `PHASE5C_WORK_PACKAGE.md`.
- Case 10 freezes these hunks.
- No other existing test may be edited. Any other failing pin means **stop and escalate**.

### A3-1: per-request tenant selection in shared mode

The Builder's proposal is **approved, with these rules**:

**Admin-config fields:**
- `auth.tenant_claim` is an optional claim name.
- Each `tenants[tk]` has a unique `alias`.

**Selection rule:**

| Configuration | Result |
|---|---|
| Exactly one tenant is configured, and `tenant_claim` is not set | That tenant |
| More than one tenant is configured, and `tenant_claim` is not set | **Startup refused** |
| `tenant_claim` is set | The claim value must be a string present in the **verified** token's claims, and must equal exactly one `alias` |
| The claim is missing, not a string, or matches no alias | `identity_required`, with zero Bullhorn calls |

**Constraints:**
- The claim is IdP-asserted, because the token is verified. It is never a tool argument.
- `tenant_key` stays `sha256(corpToken)`. Login still checks that the session's `rest_url` tenant equals the selected tenant (D-5A-14).
- A principal linked in several tenants holds independent sessions, one per `(tenant_key, principal_key)`. Each request uses only the tenant its token selects.

**Tests (R-A3a):**
- Each row of the selection table.
- A token for tenant 1 never reaches tenant 2's session, store or ledger.
- A forged alias claim in an unverified token gives 401 (HV-M1).

### A3-2: setup-state gate in shared mode. Option (b).

`tenant/state.py` is added to the allowlist, for this change only.

`compute_setup_state` row 1 ("credentials present") behaves differently by mode:

| Mode | Row 1 is satisfied by |
|---|---|
| `local` | The four `CREDENTIAL_ENV_VARS`. This is byte-identical to today. |
| `shared` | The caller's resolved session for the selected tenant: `access_tier ∈ {bullhorn_user, service}`. The env vars are ignored in this mode. |

- A `workspace_only` caller reaches only `setup_status`. It reports state `disconnected`, with `missing_requirements: ["bullhorn_session"]` and no record data.
- `tools/notes.py` is **not** modified. It keeps passing `os.environ`, and `state.py` decides by mode, so `get_notes`, `create_note` and `setup_status` stay consistent.
- Option (a) is rejected, because it would force shared credentials into env.
- Option (c) is rejected, because `get_notes` would then be inconsistent.
- Store, actor and approver resolution is identity-aware inside the already-allowlisted modules (`tenant/store.py`, `tenant/actor.py`).

**Tests (R-A3b):**
- Shared mode with no credential env vars and a linked user gives `setup_valid` (given a valid store). `get_notes` is not gated as disconnected.
- Shared mode with the env vars set, but no session for the caller, never counts as connected.
- All 4A, 4B and 5B state tests pass unmodified (local mode).

### A3-3: `commit_mapping_changes` ownership. Confirmed.

| Mode | Rule |
|---|---|
| `shared` | A commit by any principal other than the proposal's creator gives `denied`. This applies even to setup admins and approvers. A setup admin committing their **own** proposal is unchanged. |
| `local` | Legacy behaviour; no ownership check. |

**Tests:** part of AC-13.

### A3-4: the D-5A-15 verification procedure. Confirmed, with these precisions.

**Who and where:**
- Shared mode only. `local` mode refuses both ops with `unsupported_in_local_mode`.
- The caller must be a setup admin with `access_tier = bullhorn_user`. A `service` caller is denied, because this is a write.

**Exact target match:**
- `target_type`, `target_id` and `action_type` must all be the same as authorized.
- There must be **no** associations, including no `job` association.
- A mismatch gives the normal HV-B11 guard rejection.

**The verification write:**
- It **omits `commentingPerson`**. HV-B8: the default is the creating user. HV-C5 is unresolved.
- The read-back records `commentingPerson` as Bullhorn set it.

**Ops:**
- `authorize_note_write_verification` and `reset_note_write_verification` are each a **single-op proposal**.
- The diff entry is the synthetic `verification:note_write`, and the normal 4A binding applies (diff hash, actor, expiry).
- The ops have their own commit branch. It writes **no profile version**. It writes:
  - the authorization (or reset) record under `<setup_store>/verifications/`;
  - a history/audit line with the correlation ID.
- Mixing a verification op with any other op in one proposal is rejected.

**Tests:** AC-20 covers this, plus each precision above.

### A3-5: admin-config schema. Approved, with constraints.

`identity/deploy.py` defines and validates these keys: `mode`, `service_base_url`, `server` (`host`, `port`, `allowed_hosts`), `auth` (`issuer`, `resource_server_url`, `allowed_issuers`, `display_claims`, `tenant_claim`, verifier kind and config), `session_store` (`dir`, `keys_ref`), `roles`, `service_principals`, and `tenants[tk]` (`alias`, `setup_store`, `exchange_dir`, `bullhorn_oauth`, `sso`, service credential refs). (Triage B-4 moves `roles` and `service_principals` under each tenant.)

| Area | Constraint |
|---|---|
| Parsing | Parsed with `tenant/yaml_strict`. Unknown keys, duplicate keys and wrong types are rejected, and the startup error lists every problem, bounded. |
| Secrets | Secrets are given **by reference only**, as `env:NAME` or `file:/abs/path`. A literal secret value under a `*_ref`, `client_secret` or `keys` key **refuses startup**. |
| Base URL | `service_base_url` must be `https://`. |
| Allowed hosts | `server.allowed_hosts` is required in shared mode. It is passed to the SDK's transport-security (DNS-rebinding) settings, if the installed SDK provides them; the Builder verifies this as HV-M7. |
| Host binding | `host` defaults to `127.0.0.1`. |
| Documentation | Documented in the `identity/deploy.py` module docstring, plus a synthetic `docs/deployment/admin_config.example.yaml` that contains no tenant data (D-5-22). |

### A3-6: SSO tenant enablement procedure (new; in 5A scope)

**Background.** HV-C2 is undocumented, and the user's own tenant uses SSO with Duo. A3-6 provides an admin-controlled, observation-based path to enablement, analogous to D-5A-15.

1. **Default state.** A tenant with `sso: true` starts with `sso_login: unverified`. While unverified, `bullhorn_session(login)` gives `unsupported_sso` for everyone **except** setup admins of that tenant.
2. **Verification login.** A setup admin's `login` runs the normal D-5A-4 flow in **verification mode**:
   - The flow goes through the deployment's HTTPS callback.
   - The admin authenticates in the browser with the IdP and Duo. The server never sees credentials or codes.
   - The server records observations **append-only** in `<setup_store>/verifications/sso_login.jsonl`. **No tokens, codes or claims are recorded.** The record contains:
     - timestamps;
     - whether the callback received a `code` with a valid `state`;
     - the token-exchange host, as a host only, and whether it passed `TrustedOriginPolicy`;
     - the exchange result and the REST-login result;
     - the observed access-token and session lifetimes;
     - who, and the correlation ID.
   - On success, the admin may link the session through the normal confirm page.
3. **Explicit enablement.** Logins are enabled for ordinary users only when the admin proposes and commits `enable_sso_login {verification_id}`. This uses the A3-4 single-op pattern, and the ID must refer to a positive record. `disable_sso_login` reverses it.
4. **Re-verification.** It is required when the auth host, or the OAuth client configuration, changes.
5. **Negative or failed observations** keep the tenant disabled.
6. **Recorded limit.** The browser leg through the IdP is not observable by the server. The record attests only the server-observable contract: a code arrives at `redirect_uri` with `state`, and the exchange and login succeed.

**Tests (R-A3c):**
- An unverified SSO tenant refuses ordinary users' logins.
- An admin verification login (respx-simulated callback) writes a record containing no secrets.
- Enablement requires a positive record ID.
- A change of auth host re-disables.
- None of these flows runs automatically.

**External dependency.** Production use on the user's SSO tenant requires a real admin verification login against that tenant (EXT-2). This is recorded in `ROADMAP.md` §5.

### A3-7: HV findings reported by the Builder (recorded; the HV doc is authoritative)

| ID | Status | 5A consequence |
|---|---|---|
| HV-C1 | Verified: the authorize parameters; `redirect_uri` must match the API-key list; `state` is recommended; the `auth-{loginInfo}` host; a 307 for the wrong DC | Flow as specified |
| HV-C2 | **Unresolved** (SSO/Duo undocumented) | A3-6 procedure; `unsupported_sso` until it is enabled |
| HV-C3 | Mostly verified: both grants; a 10-minute access token; rotating refresh tokens. Response field names are undocumented. | Strict RFC 6749 response parsing. Store the rotated refresh token atomically. |
| HV-C4 | Verified: REST login; 401 on expiry; `/ping` `sessionExpires`; login is rate limited | Refresh on 401. Serialize logins per principal. |
| HV-C5 | Unresolved | D-5A-15 / A3-4 path |
| HV-C6 | Undocumented | Logout is local-only and documented as such |
| HV-C7 | Undocumented | No PKCE: a confidential client plus `state` |
| HV-C9 | Undocumented | Serialize per principal |
| HV-C10 | 429, with a wait of 1 s | Bounded back-off, reads only |
| HV-M1..M5 | Confirmed | — |
| HV-M6 | Pending | The mandatory concurrency test (AC-8) |
| HV-M7 | New: SDK transport-security / allowed-hosts support | If absent, enforce the Host header check in `identity/deploy.py` middleware, or **stop and escalate** if that is not possible |

### A3-8: allowlist delta, and Security & Identity attack items

**Allowlist delta.** The following are added to the §2 allowlist:
- `tenant/state.py` (A3-2);
- `tenant/changes.py` (the ops of A3-4 and A3-6);
- the two A3-0 test hunks;
- `docs/deployment/admin_config.example.yaml` (new; synthetic).

**Security & Identity attack items (blocking):**

| ID | Attack |
|---|---|
| SA3-1 | Tenant confusion via the claim: missing, forged in an unverified token, or ambiguous; multiple tenants without a claim. |
| SA3-2 | Shared-mode setup state is satisfied by env credentials without a caller session, or by another user's session. |
| SA3-3 | A cross-principal commit of a mapping proposal. |
| SA3-4 | Abuse of the verification procedure: a non-admin, a service caller, local mode, a mismatched target, an association, a second write, or mixing with other ops. |
| SA3-5 | SSO enablement bypass: an ordinary user logs in on an unverified tenant; enablement without a positive record; secrets in `sso_login.jsonl`; failure to re-disable after a host change. |
| SA3-6 | Admin-config secret literals accepted; a non-HTTPS base URL; a missing `allowed_hosts` / DNS-rebinding check. |

**Regression harness.** Add **SR-27** (tenant-claim selection, SA3-1) and **SR-28** (SSO enablement gate, SA3-5). SR-20..26 are reserved by 5C.

---

## Amendment A4 (2026-10-07): the A1-2 vs 5B drift-test conflict, and Builder close-out notes

### A4-1: `test_union_of_verified_sources` (BLOCKER). Approved test edit.

**The conflict.** `tests/test_notes_action_discovery.py::TestDrift::test_union_of_verified_sources` is a 5B test, still uncommitted. It asserts the union of meta and settings sources while `SETTINGS_ACTION_SOURCE_VERIFIED=False`. That is exactly the behaviour P5B-11 identified as the defect. A1-2 is the binding security rule, so the test's flag-off expectation is wrong, and its "pass unmodified" clause in A1-2 is superseded for this one test.

**Approved hunk.** One line, line 206:
- from: `def test_union_of_verified_sources(self):`
- to: `def test_union_of_verified_sources(self, settings_on):`

Nothing else changes. The test then checks the union with the flag on, which is its intent. R-A1b covers the flag-off case (`stale_values == ["Test Action D"]`, and `settings` values absent from `new_values`).

**Case 10.** Freezes this hunk. No other existing test may be edited.

**Security & Identity attack item:**

| ID | Attack |
|---|---|
| SA4-1 | Confirm that no remaining test or code path counts an unverified `settings` source in drift or adoption while the flag is off. |

### A4-2: Builder notes. Accepted, and recorded for the 5A close-out.

**Accepted, no ruling needed:**

| Area | Note |
|---|---|
| OAuth URLs in logs | In shared mode, a `RedactingFilter` is installed on the `httpx`, `httpcore` and `uvicorn` loggers, because `httpx` logs full request URLs at INFO and those can carry OAuth codes and secrets. The Security & Identity Reviewer includes these loggers in the AC-15 sentinel scan. (Found insufficient for `httpcore` child loggers; see triage B-1.) |
| `create_note` verdict | A positive verdict enables only the person **target**. Person-type associations stay behind the HV-B11 guard. |
| `setup_status` | The identity block appears in shared mode only; local mode output is byte-identical. |
| `server.py` import | The import line is `from .identity import deploy, sessions`. That is one import line, consistent with §2(a). |

**New debt, logged in `DEFERRED_DEBT.md`:**

| ID | Item | Target |
|---|---|---|
| P5A-1 | In local mode, the legacy password-grant request URLs, which carry credentials in the query string, are still logged by `httpx` at INFO. This is pre-existing. | Phase 9, together with P4A-10 and P4B-9 |
| P5A-2 | HV-C10 bounded back-off is not applied to the legacy `client.py` request path, because `client.py` is frozen. | 5C: D-5C-12 covers all new reads. The legacy path stays Phase 9, unless a legacy-behaviour decision is taken. |

---

## 5A Review Triage (2026-10-07)

**Inputs:**
- The Independent Reviewer returned **PASS**, with no blocking findings.
- The Security & Identity Reviewer returned **FAIL**: one blocking finding (B-1) and nine non-blocking ones.
- Independent observations Ind-1..8 were also reported.

**Architect promotions:**

| Finding | Promoted to | Why |
|---|---|---|
| Sec NB-1 | **B-2** | It defeats "User A cannot receive or use User B's Bullhorn session" (D-5-20). It was reproduced. |
| Sec NB-2 | **B-3** | It defeats the binding between requester and execution identity (D-5-10, AC-13). |
| Sec NB-3 | **B-4** | It breaks tenant isolation of privilege (D-5-20), and contradicts A3-6, which says "setup admins of that tenant". |
| Sec NB-4 | **Folded into B-1** | A DEBUG log of `login_url` feeds B-2. |

Nothing is demoted.

**Every fix below is local, and the Builder does all of them in one round.** The tests are named `test_phase5a_triage_*`; every T-test is new and blocking.

### B-1 (blocking): log redaction is bypassed by child loggers (AC-15)

**Fix (shared mode only; local mode is unchanged).**

**(a) Global record factory.**
- At shared-mode startup, `identity/deploy.py` installs a `logging.setLogRecordFactory` wrapper. For **every** record, it replaces `record.msg` with the redacted, fully formatted `getMessage()` and sets `record.args = ()`.
- It reuses the single existing redaction function in `auth/secrets.py`; no second pattern table is created.
- The patterns cover at least:
  - the query/form keys `code`, `state`, `login`, `access_token`, `refresh_token`, `client_secret`, `password` and `username`;
  - `BhRestToken`;
  - `Authorization` / `Bearer` values.

**(b) Logger level clamp.**
- These loggers, **and every existing child logger under them** (enumerated from `logging.root.manager.loggerDict` at startup), are clamped to a level of at least `INFO`: `httpcore`, `httpx`, `h11`, `h2`, `hpack`, `uvicorn`, `sse_starlette` and `mcp`.
- This also resolves Sec NB-4, since DEBUG dumps of responses are no longer emitted.

**(c) No reliance on per-logger filters.** Correctness must not depend on per-logger filters; the existing `RedactingFilter` may remain.

**(d) Documentation.** The admin config docs state that DEBUG logging is unsupported in shared mode.

**Tests:**

| ID | Test |
|---|---|
| T-B1a | `httpcore.http11` at DEBUG logs a 302 `Location: ...?code=SENTINEL&state=SENTINEL2`. Neither sentinel appears in `caplog` or in a `StreamHandler` attached to the root logger. |
| T-B1b | A child logger created **after** startup (`httpcore.newchild`) is redacted too. |
| T-B1c | The `httpx` INFO request line with `password=SENTINEL` and `client_secret=SENTINEL` is redacted. |
| T-B1d | After startup, the clamped loggers have an effective level of at least INFO, including a child that was explicitly set to DEBUG before startup. |
| T-B1e | `sse_starlette` / `mcp` DEBUG records with a `login_url` or record payload are not emitted. |
| T-B1f | In local mode, no factory is installed and the logger levels are untouched. P5A-1 remains open. |

### B-2 (blocking): account-linking hijack through a forwarded `login_url` (D-5-20)

**Fix: bind the completion to the initiating principal with an out-of-band confirmation code.**

**Tool change.** `bullhorn_session(action: str = "status", confirmation: str | None = None)`.
- `action ∈ {status, login, complete_link, logout}`.
- `confirmation` is required for `complete_link`, and must be absent for every other action (`rejected_validation` otherwise).
- AC-3 is amended so that the schema pin is exactly these two parameters.
- `confirmation` is not an identity parameter, so AC-5 is unaffected.

**Flow:**
1. **Confirm page.** The CSRF-protected confirm POST no longer activates the session. Instead:
   - It stores a **pending link**, encrypted, under `(tenant_key, principal_key)` of the **pending login's initiating principal**. The pending link holds the token material and has a TTL of 10 minutes.
   - It displays a one-time **confirmation code**: 8 characters of base32 `[A-Z2-7]`, 40 bits from `secrets`.
   - Only a keyed hash of the code is stored.
   - The page names the workspace principal (`display`), and warns: "Only continue if you are this person. Enter this code yourself in your assistant. Never give it to anyone."
2. **Completion.** `bullhorn_session(complete_link, confirmation=…)` succeeds only if all of the following hold:
   - the caller's `(tenant_key, principal_key)` holds the pending link;
   - the comparison is constant-time and the code matches;
   - the link is unexpired;
   - fewer than 5 failed attempts have been made. The 5th failure destroys the pending link.

   On success, the session becomes active, the B-3 `link_id` is minted, and the pending link is deleted.
3. **Before completion,** the caller's tier remains `workspace_only`.
4. **Secrecy.**
   - The code is never returned by any tool.
   - It is never logged (B-1 patterns include `confirmation`).
   - It is never stored in clear.
5. **Status.** `status` reports only `pending_link: true|false`.

**Tests:**

| ID | Test |
|---|---|
| T-B2a | Reproduce the hijack: Alice's browser completes Mallory's `login_url` and confirms. Mallory then calls `get_job` → `bullhorn_auth_required`, zero Bullhorn calls. Mallory's `complete_link` without the code fails. |
| T-B2b | A correct code submitted by a **different** principal (Alice herself via her own MCP identity, or a third user) does not activate Mallory's link, and does not link anything to the submitter. |
| T-B2c | 5 wrong codes destroy the pending link. An expired pending link is rejected. A code cannot be reused. |
| T-B2d | The legitimate flow works: the same person initiates, completes in the browser, and submits the code → `bullhorn_user`. |
| T-B2e | The code is absent from every tool output, from `caplog` and from the store plaintext (sentinel scan). |
| T-B2f | The schema pin is exactly `{action, confirmation}`, and `confirmation` is rejected for any action other than `complete_link`. |

### B-3 (blocking): execution identity does not distinguish re-linked accounts (AC-13)

**Fix.**
- **Minting.** Every completed link mints a `link_id` (`secrets.token_urlsafe(16)`), stored in the session.
  - Refresh preserves it.
  - Logout, and any later re-link, replace it.
- **The label.** While HV-C5 is unresolved, `executing_bullhorn_identity` becomes `bh-link:<sha256(tenant_key|principal_key|link_id)>`. If HV-C5 is ever verified, both `bullhorn_user_ref` and the link hash are recorded and compared.
- **Where it is carried.** The pending write, ledger, journal and audit all carry the label.
- **Confirm.** `confirm_write` requires exact equality of the label. A mismatch gives `denied`, reason `execution_identity_changed`.
- **Verification procedure.** Its authorization (A3-4) is bound to the same label.

**Tests:**

| ID | Test |
|---|---|
| T-B3a | Preview under link X, then logout and re-link (Y), then confirm → `denied` / `execution_identity_changed`. Zero `PUT` requests. |
| T-B3b | A refresh between preview and confirm leaves the `link_id` unchanged, so the confirm succeeds. |
| T-B3c | Journal and audit lines from link X and link Y have different labels. |
| T-B3d | A verification authorization made under link X cannot be used after a re-link. |

### B-4 (blocking, promoted): roles and service principals are global, not per tenant

**Fix.**
- **Admin-config schema.** `roles {setup_admins, write_approvers}` and `service_principals` move under `tenants[tk]`.
- **Global sections.** The top-level `roles` / `service_principals` are accepted **only** when exactly one tenant is configured, and then apply to that tenant. Otherwise startup is refused.
- **Evaluation.** Every role and service-principal check is evaluated against the request's selected tenant (A3-1).

**Tests:**

| ID | Test |
|---|---|
| T-B4a | A setup admin of T1 is denied the following in T2: `propose_mapping_changes`, `commit_mapping_changes`, the verification ops, `enable_sso_login`, and the admin SSO login exception. |
| T-B4b | Multiple tenants with top-level `roles` → startup refused. |
| T-B4c | A service principal of T1 has no service tier in T2 (it is `workspace_only` there). |

### Local non-blocking fixes (same round)

| ID | Source | Fix | Test |
|---|---|---|---|
| L-1 | Sec NB-5 | `oauth_routes.confirm` reads at most 4 KB, streamed, and rejects anything larger before buffering. | T-L1 |
| L-2 | Sec NB-5 | Re-fetch the JWKS for an unknown `kid` at most once per 60 s per issuer. | T-L2 |
| L-3 | Sec NB-5 | Prune expired pending logins and pending links on every `put` / `take` and at startup. Per-principal locks live in a bounded structure (LRU or weak-valued). | T-L3 |
| L-4 | Sec NB-6 | Startup check: refuse to start if the longest session-file path for `session_store.dir` would exceed the platform limit (Windows: 240 characters). | T-L4 |
| L-5 | Sec NB-8 | Absolute session lifetime: `session_store.max_session_days`, default 30, maximum 90, counted from link completion. After it, the session is deleted and the user must re-link. Shared mode refuses more than one worker process (in-process refresh lock); this is documented. | T-L5 |
| L-6 | Ind-2 | The verification ledger key includes a reset generation: `verification:note:v1:<tenant>:<generation>`. Committing `reset_note_write_verification` increments the generation. | T-L6 |
| L-7 | Sec NB-7 | In `bullhorn_password.py`, change only the two approved expressions (lines 102 and 113) to `trusted_origins.is_trusted_bullhorn_url(parsed)`, which requires `https` **and** a trusted host. **Condition:** the legacy redirect-guard tests must pass unmodified. If any of them fails, revert to the netloc check and log P5A-5 instead. **No escalation is needed.** | T-L7 |
| L-8 | Ind-5 | One path-containment helper (drop `deploy._inside` in favour of `store.is_within`, or the reverse). One tier-check helper in `identity/principal.py`, used by `state.py`, `setup.py` and `permissions.py`. | Existing tests |
| L-9 | Ind-6 | Restore CRLF line endings in `tenant/changes.py`. | `git diff --check`; byte check |
| L-10 | Ind-8 | Correct the HV doc: the S2 CorporateUser example contains `samlInfo` / `samlIdpID`. The conclusion is unchanged. | Doc |

### Deferred or accepted (logged in `DEFERRED_DEBT.md`)

| ID | Disposition |
|---|---|
| Sec NB-9 | P5A-3. Phase 9, together with P4A-10. |
| Ind-1 | P5A-4. Phase 9 legacy decision. `bullhorn_session(status)` and `setup_status` are authoritative in shared mode. |
| Ind-3 | P5A-6. **Accepted.** The service `BullhornAuth` is cached per `tenant_key`, for a single non-human identity, and is never reachable from a non-`service` tier. The D-5A-8 wording is clarified accordingly. |
| P5A-5 | Conditional on L-7. |

### Re-review and regression

**Reviews:**
- A **fresh Security & Identity Reviewer** reruns the full 5A attack set, plus T-B1..T-B4.
- A **fresh Independent Reviewer** reviews the fix diff: the tool schema and the flow change.
- Then the gates.

**Case 10:** it freezes the updated `bullhorn_password.py` expressions, if L-7 is applied. No other protected-file change is permitted.

**New regression cases:**

| ID | Case |
|---|---|
| SR-29 | Child-logger redaction and level clamp (B-1). |
| SR-30 | Link-completion binding (B-2). |
| SR-31 | `link_id` execution-identity binding (B-3). |
| SR-32 | Per-tenant roles (B-4). |

**Re-review (2026-10-07): PASS.** The fresh Independent Reviewer and the fresh Security & Identity Reviewer both passed, with no blocking findings. The protected diffs, including the L-7 expressions, are frozen for case 10. L-7 was applied, so P5A-5 is not opened.

---

## 5A Close-out (2026-10-07)

### Accepted deviations

The Independent Reviewer judged these spec-consistent. They are recorded as intended.

| ID | Deviation | Reason |
|---|---|---|
| CO-1 | In shared mode, `propose_mapping_changes` requires a setup admin **of the selected tenant**. | Needed for T-B4a. No capability is lost, because only setup admins can commit. |
| CO-2 | For `uvicorn.access`, B-1's record factory redacts **each argument** instead of setting `args=()`. | `AccessFormatter` unpacks `record.args`. Redaction is still enforced. |
| CO-3 | The JWKS re-fetch throttle (L-2) is per verifier instance, not per issuer. | Only one verifier is allowed per deployment, so the two are equivalent. |

### Follow-ups

These are logged in `DEFERRED_DEBT.md` as P5A-7..P5A-17, with their targets. One documentation item is stale: the docstring in `auth/trusted_origins.py`, which is code. It is left for the next Builder touch and logged as P5A-17.

### External dependencies

Both are in `ROADMAP.md` §5:
- **EXT-1:** `create_note` enablement, now through the D-5A-15 verification procedure with P4B-8 closed.
- **EXT-2:** SSO login enablement.
