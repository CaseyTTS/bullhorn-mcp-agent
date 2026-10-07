# Phase 5 Proposal: Identity, Note-Action Setup Completion, and Expanded Reads (with Tool-Surface Streamlining)

| | |
|---|---|
| **Status** | **APPROVED by the user, 2026-10-07.** Decisions D-5-1..D-5-25 in §0 are **binding**. Where §0 and the analysis in §1–§10 disagree, §0 wins. |
| **Author** | Architect. Drafted 2026-10-07 and approved the same day. |
| **Baseline** | Phases 0–4B are committed (`d5330fb`) and are not reopened. The amendments PA-1..PA-6 are approved as recorded in §0. |
| **Sub-phase work packages** | 5B: `PHASE5B_WORK_PACKAGE.md` (PASS). 5A: `PHASE5A_WORK_PACKAGE.md` (PASS). 5C: `PHASE5C_WORK_PACKAGE.md`. |
| **HV-1 applies** | Every Bullhorn OAuth, SSO, session or endpoint behaviour is **to be verified** against https://bullhorn.github.io/rest-api-docs/ and Bullhorn OAuth/SSO documentation, or against a connected tenant. Unverified behaviour is marked **unresolved** and fails closed. |

---

## 0. Binding user decisions (2026-10-07)

| ID | Decision (substance as given by the user) |
|---|---|
| D-5-1 | **PA-1 approved.** Secure credential/session storage and the redirect allowlist move from Phase 9 into **5A** (OBS-1 and DEBT-1), as prerequisites for per-user auth. |
| D-5-2 | **PA-2 approved.** When used interactively, the original 10 tools execute under the **calling user's** authenticated Bullhorn session. Their inputs and backward-compatible behaviour are unchanged. |
| D-5-3 | **PA-3 approved.** The audit actor, administrator identity and approver identity derive from the **authenticated caller plus administrator configuration**. They never come from env-var identity assertions. |
| D-5-4 | **PA-4 approved conditionally.** `/settings/commentActionList` may become a Note-action discovery source **only after** its behaviour is verified against authoritative Bullhorn documentation or the connected tenant. If it cannot be verified, it fails closed and the administrator-configured action mappings are kept. |
| D-5-5 | **PA-5 approved.** The Phase 6 timeline becomes part of `get_activity`, and future metrics go into a single `get_recruiting_metrics`. Internal modularity is preserved. |
| D-5-6 | **PA-6 approved.** The order is **5B** (Note-action setup completion) → **5A** (per-user auth/session) → **5C** (expanded reads). |
| D-5-7 | **Deployment.** Design for a **shared remote HTTP MCP server** used by the whole workspace. An admin installs and configures it once, and each individual user (for example a ChatGPT user) authenticates to Bullhorn individually. Local development may remain possible. Production assumes many users share one server, so **there is no process-global Bullhorn auth state for interactive users**, and every request resolves the authenticated caller to *that caller's* Bullhorn session. |
| D-5-8 | **SSO.** The user's own tenant uses Bullhorn SSO with Duo MFA. Production auth must **not** be built around a local callback on each machine; prefer an **HTTPS callback endpoint owned by the MCP service deployment**. The abstraction stays generic so non-SSO tenants can use standard Bullhorn OAuth. Exact Bullhorn OAuth/SSO behaviour is verified before implementation. **The LLM or host never collects Bullhorn passwords or Duo codes.** `bullhorn_session(status\|login\|logout)` returns or directs the user to the proper Bullhorn auth flow. |
| D-5-9 | **One-note tenant verification test approved, only as an explicit administrator-controlled procedure.** See the requirements immediately after this table. |
| D-5-10 | **No cross-user confirm by default.** A preview belongs to the initiating principal, the Bullhorn execution identity and the proposed operation. The same user confirms their own preview, and other ordinary users cannot. A distinct approver workflow may exist in future, but only as an explicit approval role, and it **must not change the Bullhorn execution identity to the approver** (requester = Casey, approver = manager, execution identity = Casey) unless a specifically configured service workflow says otherwise. |
| D-5-11 | **Service identity.** It may run unattended **reads** (analytics, monitoring, discovery, other non-consequential operations). **Unattended writes are default-denied.** Future service writes are allowed only as individually whitelisted operations, each with explicit admin configuration, an operation-specific permission, audit, idempotency and an approval policy. |
| D-5-12 | **Token storage.** See the requirements immediately after this table. |
| D-5-13 | **Canonical `user` entity approved.** Make the narrowly required test change: the Phase 3 test changes from pinning exactly 10 entities to "the original 10 preserved, plus explicitly approved additive canonical entities". It must not be weakened in any other way. The canonical `user` is ATS-agnostic. It represents recruiter ownership, activity author, the Bullhorn CorporateUser mapping, the initiating-principal mapping and audit attribution. (The coordinator treats regression case 11's "10 entities" label as covered by this approved change.) |
| D-5-14 | **Legacy tools are not deprecated or removed in Phase 5.** They remain backward-compatible compatibility and escape-hatch tools under appropriate permissions. Any deprecation is a separate, future approved decision. |
| D-5-15 | **Tool surface.** Keep the existing 19 tools. Add `bullhorn_session`, `find_records` and `get_activity`, for **22 after Phase 5**. Phase 6 adds `get_recruiting_metrics`, for **23**. Do **not** add the 13 separate analytics/read tools unless a concrete capability cannot be represented clearly and safely through the consolidated tools. `find_records` is the structured, domain-safe read path. `search_entities` / `query_entities` remain compatibility escape hatches. |
| D-5-16 | **5B required behaviour.** See the requirements immediately after this table. |
| D-5-17 | **5A identity model.** The chain is: workspace caller identity → authenticated MCP principal → per-user Bullhorn session lookup → authenticated Bullhorn user → Bullhorn operation. `initiating_principal`, `executing_bullhorn_identity`, `approver` and `service_identity` are kept **distinct**. **No MCP tool accepts an arbitrary identity parameter.** |
| D-5-18 | **`create_note` enablement.** Revisit HV-B11 under per-user sessions. Using authoritative docs and/or the approved tenant verification procedure (D-5-9), determine whether the authenticated Bullhorn identity can be resolved to the ID required for Note attribution and associations. If it is verified, enable `create_note`; if not, it remains production-disabled. **Do not invent the linkage.** |
| D-5-19 | **5C scope** (after 5A is green). See the requirements immediately after this table. |
| D-5-20 | **Shared-server isolation is a BLOCKING security requirement** that the Reviewer must attack. See the attack list immediately after this table. |
| D-5-21 | **Tool-surface rule.** Internally rich, externally compact. Never consolidate where that weakens security boundaries, permission differences, approval semantics, audit clarity or the model's ability to choose the right tool. Each sub-phase Reviewer flags unnecessary tool growth and duplicated domain logic. |
| D-5-22 | **Privacy.** No tenant-specific data from the user's company is committed to the public repo: no credentials, tenant profile, field mappings, workflow values, SSO configuration, Note actions, workbook data or business rules. **Process:** each sub-phase runs Architect → Builder → a fresh Reviewer. The gates for each sub-phase are full pytest, ruff, mypy, packaging/resource checks where applicable, the 25/25 regression suite, and the new Phase 5 regression/security cases. If Bullhorn documentation or tenant verification blocks a capability, it fails closed, is documented precisely, and work continues with what is safe. Do not begin Phase 6. No commit or push happens until all Phase 5 work and gates are complete. |
| D-5-23 | **Harness (user directive, binding from Phase 5).** The order is Architect → Builder → Independent Reviewer → **Security & Identity Reviewer** (`docs/process/SECURITY_REVIEWER.md`) → final gates. Every Phase 5 sub-phase requires **both** reviewers to PASS: 5B (consequential writes and secrets), 5A (the full minimum attack set, as blocking ACs) and 5C (session/tenant isolation of reads, and provenance). Each sub-phase work package carries a "Security & Identity Review" section. |
| D-5-24 | **Two-tier data access model** (user requirement, 2026-10-07; binding for 5C and Phase 6). **Tier 1**: Bullhorn-authenticated users get record-level and aggregate data. **Tier 2**: workspace-only users get only approved, de-identified aggregate results computed through the read-only service identity. The full requirement and controls are defined **once**, in `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8: cohort thresholds, complementary suppression, differencing defences, approved geography levels, anonymous client segments, the Tier 2 output allowlist, record-level tools denied to Tier 2, `get_recruiting_metrics` as the primary Tier 2 interface, and the blocking security tests. **5A** adds only the identity hook (`PHASE5A_WORK_PACKAGE.md` Amendment A2). **5C** gates every record-level tool by tier. **Phase 6** implements Tier 2. |
| D-5-25 | **Phase 5 scope is FROZEN** (user directive, 2026-10-07; binding for every remaining triage). See the rule immediately after this table. |

**D-5-9 requirements (one-note tenant verification test).** The procedure must:
- run only on an explicit admin action;
- show a preview before execution;
- use a clearly identified test target, preferably a designated test record;
- make exactly one intentional Note write;
- verify the resulting Note ID and associations;
- record the verified tenant behaviour;
- never run silently during ordinary setup, and never create repeated test Notes.

Success may satisfy the remaining `create_note` enablement requirement for that tenant, if the Architect determines that the observed behaviour is sufficient and repeatable.

**D-5-12 requirements (token storage).**
- **Production** uses a server-side, secure, per-user token/session store with:
  - encryption at rest;
  - strict tenant and user isolation;
  - expiry handling;
  - refresh and re-authentication;
  - revocation and logout;
  - no tokens in logs or audit;
  - no credentials in the repo.
- Storage sits behind an **abstract storage interface**. The OS credential store is only an optional adapter for local development.

**D-5-16 requirements (5B behaviour).**
- **First setup:** discover Note action values where they are verified → the admin reviews and adopts the mappings → save them to the tenant profile → expose the setup requirement and status.
- **Later:** view, add, remap, deactivate, reactivate, rediscover, compare against the current Bullhorn configuration, identify stale mappings, propose, approve, and version/rollback.
- **Invariants:**
  - Unknown actions fail closed, and `create_note` never invents an action.
  - The chain raw `Note.action` ↔ tenant configuration ↔ canonical action meaning is preserved.
  - **P4B-8 (comment scrubbing before redaction) is completed in 5B**, before Note writes can be enabled.

**D-5-19 requirements (5C scope).**
- `find_records` and `get_activity` support:
  - JobSubmission;
  - tenant-defined client submissions;
  - individual interview instances;
  - jobs by primary recruiter and job-created date;
  - job priority;
  - offers;
  - placements;
  - notes and activity;
  - the relevant candidate, job and client relationships.
- They are backed by:
  - a structured, safe query builder;
  - pagination and complete-result handling;
  - Bullhorn limits;
  - retries and rate-limit handling;
  - date/time semantics;
  - provenance and source IDs;
  - inactive/deleted filtering.
- Business meaning comes from tenant configuration. `find_records` never guesses what a client submission, interview, offer or priority means.

**D-5-20 attack list (shared-server isolation).** The Reviewer must attack each of these:
- User A receives User B's session.
- A's token is used for B's operation.
- A logout invalidates another user's session.
- Session cache keys collide.
- Sessions collide across tenants.
- The audit actor and the execution identity do not match.
- The server falls back to the service account unexpectedly.
- The LLM spoofs a principal.
- Sessions are stale or expired.
- Many users make requests concurrently.

**D-5-25 rule (scope freeze).**

**Fix now (blocking)** a finding that is required to make **approved** Phase 5 behaviour correct, safe, internally consistent or regression-protected. That covers:
- permission mismatches;
- session-isolation defects;
- cross-user or cross-tenant leakage;
- unintended service fallback;
- query inconsistencies between approved tools;
- pagination errors;
- date/time filtering errors;
- incorrectly implemented Bullhorn relationship or association behaviour;
- setup-state inconsistencies;
- audit or identity attribution defects;
- sensitive-data leakage in logs, errors or tool output;
- retry or rate-limit defects;
- regressions to the original tools;
- Tier 1 / Tier 2 authorization-boundary defects;
- tool-surface drift that violates the compact design;
- missing regression tests for approved behaviour or security boundaries.

**Defer** (to `DEFERRED_DEBT.md` / Phase 6+, then continue) anything that is:
- an enhancement, redesign, optimization or cleanup;
- a new architecture idea or a new analytics capability;
- an additional tool;
- an optional refactor;
- a new setup concept;
- a redesigned auth abstraction;
- a future convenience feature;
- broader Phase 6 functionality.

**Never promote** deferrable items to blocking.

**Objective.**
1. Resolve the current 5C blockers.
2. Complete both reviewer passes.
3. Fix only blocking findings.
4. Re-run the gates.
5. Produce the final Phase 5 report.
6. Stop at **READY FOR COMMIT/PUSH**.

Do not begin Phase 6.

---

## 1. Summary of the approved structure

| Order | Sub-phase | Scope | Tools added | Tool total |
|---|---|---|---|---|
| 1 | **5B, Note-Action Setup Completion** | D-5-16, D-5-4, P4B-8 | none | **19** |
| 2 | **5A, Identity & Sessions** | D-5-1/2/3/7/8/10/11/12/13/17/18/20. This includes the D-5-9 tenant verification procedure and the `create_note` enablement decision. | `bullhorn_session` | **20** |
| 3 | **5C, Expanded Reads** | D-5-19, the original roadmap Phase 5 items, and the Phase-5 debt | `find_records`, `get_activity` | **22** |

Phase 6 then adds `get_recruiting_metrics`, for **23**. The timeline is a `get_activity` scope (D-5-5).

5C may split into 5C-i (catalog, query builder, pagination/limits/retries) and 5C-ii (concept derivers and tools) at work-package time.

**Review harness (D-5-23).** Each sub-phase runs Builder → Independent Reviewer → Security & Identity Reviewer → gates. Both reviewers are required for 5B, 5A and 5C.

**Two-tier model (D-5-24).** 5A adds the identity hook, 5C does the record-level tier gating, and Phase 6 implements Tier 2. See `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8.

**Scope freeze (D-5-25).** From 2026-10-07, every triage applies the D-5-25 rule.

## 2. Dependencies

```
5B ── (4A/4B only; HV-D1..D3 for the /settings source) ─────────────┐
5A ── (HV-C1..C8; admin config; secure store; HTTPS callback) ───────┼──> 5C ──> 6 ──> 7 ──> 8 ──> 9
create_note enablement ── 5A identity + (HV-C5 or D-5-9 probe) + P4B-8 closed (5B)
```

## 3. Public MCP tool surface after Phase 5 (approved: 22)

| # | Tool | Status | Purpose | Key params |
|---|---|---|---|---|
| 1 | `connection_status` | legacy-kept | Credential and connectivity check | — |
| 2 | `list_jobs` | legacy-kept | Raw job listing | `query`, `status`, `limit`, `fields` |
| 3 | `get_job` | legacy-kept | Raw job by ID | `job_id`, `fields` |
| 4 | `list_candidates` | legacy-kept | Raw candidate listing | `query`, `status`, `limit`, `fields` |
| 5 | `get_candidate` | legacy-kept | Raw candidate by ID | `candidate_id`, `fields` |
| 6 | `get_candidate_files` | legacy-kept | Files on a candidate | `candidate_id` |
| 7 | `upload_candidate_resume` | legacy-kept (joins the pipeline in Phase 7) | Upload a resume | existing |
| 8 | `get_recent_placements` | legacy-kept | Recent placements | existing |
| 9 | `search_entities` | legacy-kept, raw escape hatch | Raw Lucene search | existing |
| 10 | `query_entities` | legacy-kept, raw escape hatch | Raw JPQL query | existing |
| 11 | `setup_status` | kept (4A); 5B adds per-capability requirement detail to the output; 5A adds session status | Setup state | `check_connection` |
| 12 | `discover_schema` | kept (4A); 5B adds the verified action-value source internally, with no schema change | Meta-only discovery and drift | `entities` |
| 13 | `get_mapping_profile` | kept (4A) | View, search, history, diff and proposals | existing |
| 14 | `propose_mapping_changes` | kept (4A); 5B adds propose-ops | Dry-run of configuration changes | existing |
| 15 | `commit_mapping_changes` | kept (4A) | Approve and activate a change | existing |
| 16 | `manage_mapping_profile` | kept (4A) | Validate and export | existing |
| 17 | `get_notes` | kept (4B) | Note reads | existing |
| 18 | `create_note` | kept (4B); production-disabled until D-5-18 is satisfied | Preview a note write | existing |
| 19 | `confirm_write` | kept (4B) | Confirm or reject one's **own** preview (D-5-10) | existing |
| 20 | `bullhorn_session` | **new (5A)** | `status` / `login` (directs the user to the Bullhorn auth flow; never takes passwords or MFA codes) / `logout` | `action` |
| 21 | `find_records` | **new (5C)** | Structured, domain-safe read over the canonical entities | `entity`, `filters[]`, `fields?`, `sort?`, `limit`, `cursor` |
| 22 | `get_activity` | **new (5C)** | Vocabulary events by concept, scope, recruiter and date range; the timeline scope comes in Phase 6 | `concepts[]`, `scope?`, `recruiter_id?`, `date_from?`, `date_to?`, `limit`, `cursor` |

## 4. Consolidation and separation (approved)

**Consolidated.**
- The RA-11 read capabilities and the roadmap's `find_*` family become `find_records` + `get_activity`.
- The Phase 6 timeline becomes `get_activity`, and the Phase 6 metrics become `get_recruiting_metrics`.
- Auth `status` / `login` / `logout` become one tool, `bullhorn_session`.

**Kept separate for safety** (D-5-21):

| Pair / group | Why it stays separate |
|---|---|
| `propose_mapping_changes` / `commit_mapping_changes` | Two-step approval of configuration changes |
| `create_note` / `confirm_write` | The preview hash is bound to the commit, and only the owner confirms (D-5-10) |
| Raw `search_entities` / `query_entities` vs `find_records` | Different permission scopes and different safety properties |
| Admin setup tools vs domain tools | Different role gating |
| `bullhorn_session` vs domain tools | Authentication is host- and user-level |
| Legacy tools vs new tools | Frozen schemas |

## 5. EXT-1 / HV-B11 under per-user sessions (approved approach)

**What per-user auth fixes.** Attribution: the `commentingPerson` default becomes the real user.

**What it does not fix by itself.** The CorporateUser ID is still unknown. The documented `login` response carries only `BhRestToken` and `restUrl`.

**What 5A must do (D-5-18):**
1. **Check the documentation.** Re-search the authoritative docs for a current-user ID mechanism (HV-C5).
2. **Otherwise, run the tenant procedure.** Run the D-5-9 admin-controlled one-note verification, under the admin's own session, against a designated test record.
3. **Enable only on sufficient evidence.** Enable `create_note` for a tenant only if (1) or (2) yields sufficient, repeatable evidence and P4B-8 is closed (5B). Otherwise it stays disabled. The linkage is never invented.

## 6. HV-1 items

| Sub-phase | IDs | Covers |
|---|---|---|
| 5B | HV-D1..D3 | `/settings/commentActionList` (see `PHASE5B_WORK_PACKAGE.md`) |
| 5A | HV-C1..C8 | OAuth authorization-code flow, SSO/Duo redirect behaviour, per-user REST login and session TTL, revocation, current-user ID, concurrent-session limits, rate limits |
| 5C | Per-entity HV items | Fields, associations, history and query syntax |

## 7. Open questions remaining after approval

| ID | Question |
|---|---|
| Q-A1 | **Shared-server transport authentication.** Which MCP host(s) must be supported first, and how does each convey an authenticated workspace principal? This determines the 5A principal-verification adapter. It is asked when the 5A work package is written. |
| Q-A2 | **Bullhorn OAuth client registration.** Who registers the HTTPS callback URI with Bullhorn for the shared deployment, and what is the deployment's public base URL? This is needed for HV-C1 and the redirect allowlist. |
