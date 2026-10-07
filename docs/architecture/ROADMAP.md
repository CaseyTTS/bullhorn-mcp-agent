# Bullhorn Recruiting Orchestrator MCP: Authoritative Phased Roadmap

| | |
|---|---|
| **Owner** | Architect |
| **Revision** | 3 (2026-10-06), plus the **Phase 5 approval** (2026-10-07; `PHASE5_PROPOSAL.md`, binding decisions D-5-1..D-5-25). The phase order is fixed by the user. |
| **Requirements reconciled** | `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` (TS, HV-1, CT-1, SB-1..13); `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` (RA-1..13, and §8, the two-tier model); `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` (OW-0..13); the Phase 5 user decisions (D-5-n). |
| **Shared vocabulary** | `CANONICAL_ACTIVITY_VOCABULARY.md` is the single definition of the activity concepts and of the operation→concept mapping. |
| **Base** | Plan `enter-planning-mode-only-binary-cherny.md` §8, amended by ROADMAP-AMENDMENT-1..4 (`DEFERRED_DEBT.md`) and the approved Phase 5 amendments PA-1..PA-6. |
| **Precedence** | This file supersedes the phase numbering of plan §8 and of earlier roadmap revisions. All plan §8 scope commitments are preserved. Completed phases are not deconstructed. |
| **Current state** | **Phase 5 COMPLETE.** **Phase 6 M1 COMPLETE** (2026-10-07; both reviews PASS, plus the M1-B fix and its re-check). The public tool count is **23**. The next Phase 6 slices are listed in §3. |

## Standing rules

- **Gates.** Every phase ends fully green: `pytest`, `ruff` and `mypy` all pass.
- **The 10 original tools stay schema-identical, and their inputs and outputs are backward compatible.** Under D-5-2 they run under the calling user's Bullhorn session when used interactively.
- **Review harness (from Phase 5, D-5-23):**

  ```
  Architect → Builder → Independent Reviewer → Security & Identity Reviewer → final gates
  ```

  Any phase touching auth, authorization, sessions, identity, tenant isolation, service accounts, secrets or redirects, or consequential writes needs **both** reviewers to PASS (`docs/process/SECURITY_REVIEWER.md`).
- **Shippable phases.** Every phase is independently shippable.
- **Sub-phases.** The Architect may split a phase into lettered sub-phases or milestones. Ownership stays with the parent number.
- **Scope freeze (D-5-25).** Within a phase or milestone, only findings that make approved behaviour correct, safe, consistent or regression-protected are fixed. Everything else is deferred.

## Binding constraints

1. **Layering (RA-12):**

   ```
   Bullhorn raw data → canonical recruiting model → normalized activity definitions → analytics/read tools → dashboard/agent
   ```

2. **Safe-write pipeline (OW-6):**

   ```
   intent → canonical operation → target resolution → validation → permission → idempotency/duplicate check → dry-run/preview → approval policy → Bullhorn write → audit → normalized result
   ```

   - Phase 4B owns the minimal pipeline and Phase 7 generalizes it.
   - The legacy `upload_candidate_resume` is the single exception until Phase 7.
   - Preview ownership follows D-5-10: no cross-user confirm by default, and execution identity always stays with the requester.

3. **Controlled configuration change (TS-4):**

   ```
   propose → validate → diff → admin approval → new version → activate → audit
   ```

4. **One vocabulary (RA-9, OW-11).**

5. **HV-1, hard Bullhorn verification.** Never assume Bullhorn behaviour. Verify it against the official reference or the connected tenant; anything unresolved fails closed.

6. **CT-1 / D-5-21, compact tool surface.** Internally rich and externally compact. Never consolidate where that would weaken a security, permission, approval, audit or tool-selection boundary.

7. **TS-7 capability gating.**

8. **Identity (D-5-3, D-5-7, D-5-17).**
   - A **shared remote HTTP server** is the production model, and there is no process-global auth state for interactive users.
   - The identity chain is: workspace caller → authenticated MCP principal → per-user Bullhorn session → Bullhorn user.
   - `initiating_principal`, `executing_bullhorn_identity`, `approver` and `service_identity` stay distinct.
   - No tool takes an identity parameter.
   - The service identity is limited to unattended reads; unattended writes are default-denied (D-5-11).

9. **Privacy (D-5-22).** No tenant-specific data (credentials, profiles, mappings, workflow values, SSO configuration, Note actions or business rules) is ever committed to the repo.

10. **Two-tier data access (D-5-24; binding for 5C and Phase 6; single source `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8).**
    - **Tier 1** (`bullhorn_user`, meaning the caller has a valid linked Bullhorn session) gets record-level and aggregate data.
    - **Tier 2** (`workspace_only`) gets only approved, de-identified aggregates computed through the read-only service identity, with:
      - cohort thresholds and complementary suppression;
      - differencing defences;
      - approved geography levels;
      - anonymous client segments;
      - a Tier 2 output allowlist.
    - Every record-level tool is denied to Tier 2. `get_recruiting_metrics` enforces the tier automatically.
    - Authorization and de-identification happen in the server before anything is returned to the model, never through prompting.

---

## 1. Ownership

### 1.1 Recruiting analytics (RA-13) and operational (OW-12) items

| # | Responsibility | Owning phase | Contributing |
|---|---|---|---|
| 1 | Tenant setup / field mapping, including business-value mappings | **4A** | 5B (Note actions), 5C (new value mappings) |
| 2 | Note action-type discovery, mapping and validation | **4B** | 4A (mechanism); **5B** (completion; done) |
| 3 | Expanded entity reads | **5C** (done) | 4B (Note reads) |
| 4 | Normalized activity / event definitions | **5C** (done) | 4B (event model and `note_created`) |
| 5 | Activity-timeline normalization | **6** (as a `get_activity` scope, D-5-5) | |
| 6 | Note creation | **4B**. Production enablement is via the 5A procedure (EXT-1). | |
| 7 | Generalized safe-write infrastructure | **7** | 4B |
| 8 | Write-specific permissions | **4B** (`note.create`) | 7 |
| 9 | Duplicate / idempotency safeguards | **4B** | 7, 8 |
| 10 | Aggregation / metrics, including the **Tier 2** de-identified interface | **6** (single `get_recruiting_metrics`; **M1 done**) | |
| 11 | Dashboard-agent consumption | **6** | |
| 12 | **Per-user Bullhorn auth / sessions / identity**, including the `access_tier` hook | **5A** (done) | 9 (re-running the security matrices) |
| 13 | **Two-tier gating of record-level tools** (D-5-24) | **5C** (done) | 5A (hook), 6 (Tier 2 metrics; M1 done) |

### 1.2 Placement of other committed capabilities

| Capability | Phase |
|---|---|
| `find_records` (replacing the roadmap's `find_*` family) and `schema/query_builder.py` | **5C** (done) |
| Composites (`candidate_360`, `job_360`) | **6** |
| OW-13 end-to-end acceptance test | **6**. Its note-write half depends on EXT-1 (§5). |

### 1.3 Required supporting behaviors (SB-1..13)

| ID | Behavior | Owner |
|---|---|---|
| SB-1 | Pagination / complete-result retrieval | **5C** (done; offset only while HV-Q4 is unresolved) |
| SB-2 | Bullhorn result limits | **5C** (done) |
| SB-3 | Retries / rate limiting | **5C** (done for new reads; legacy path P5A-2 → 9) |
| SB-4 | Timezone normalization | **4A** |
| SB-5 | Date-range semantics | **4B** |
| SB-6 | Inactive / soft-deleted records | **5C** (done; 4B for notes) |
| SB-7 | Recruiter identity resolution | **4B** (partial) → **5A** (canonical `user`) / **5C** (CorporateUser binding; done) |
| SB-8 | Client / contact identity resolution | **4B** → **5C** (done) |
| SB-9 | Picklist / status / action discovery | **4A** → **5B** (Note actions) |
| SB-10 | Duplicate prevention / idempotency | **4B** → 7, 8 |
| SB-11 | Partial failures in bulk operations | **8** |
| SB-12 | Source-record provenance | **4B** → 5C (done), 6 (Tier 1 only; Tier 2 never receives provenance) |
| SB-13 | Tenant rules kept separate from generic code | **4A** |

## 2. Phase sequence

### 2.1 Sequence

| Phase | Name | State | Tools |
|---|---|---|---|
| 0–3 | Foundation, cross-cutting, reorganization, schema | COMPLETE | 10 |
| 4A | Tenant Setup & Mapping Management | COMPLETE (2026-10-06) | 16 |
| 4B | Notes / Activity Core | COMPLETE (2026-10-07) | 19 |
| **5B** | Note-Action Setup Completion | **COMPLETE** (both reviews PASS, 2026-10-07) | 19 |
| **5A** | Identity & Sessions | **COMPLETE** (both re-reviews PASS, 2026-10-07) | 20 (+`bullhorn_session`) |
| **5C** | Expanded Recruiting Reads + Streamlining + two-tier gating | **COMPLETE** (Round 2 re-reviews PASS, 2026-10-07) | 22 (+`find_records`, `get_activity`) |
| **Phase 5 overall** | 5B + 5A + 5C | **COMPLETE** | 22 |
| **6 — M1** | `get_recruiting_metrics`: period counts and stage ratios, two-tier, live Bullhorn | **COMPLETE** (both reviews PASS; M1-B closed-periods fix), 2026-10-07. Work package: `PHASE6_M1_WORK_PACKAGE.md` | **23** (+`get_recruiting_metrics`) |
| 6 — next milestones | See §3 | Not started | 23 (no new tools planned) |
| 7 | Broader Writes | | |
| 8 | Bulk Resume | | |
| 9 | Auth / Security Closeout | | |

**Earlier mappings.** The mappings from plan §8 and from roadmap revision 2 are unchanged (see `DEFERRED_DEBT.md`, ROADMAP-AMENDMENT-2..4). The approved PA-6 splits Phase 5 into 5B → 5A → 5C.

## 3. Phases

### Phases 4A and 4B (COMPLETE)

See `PHASE4A_WORK_PACKAGE.md` and `PHASE4B_WORK_PACKAGE.md`.

### Phase 5B, 5A and 5C (COMPLETE)

See `PHASE5B_WORK_PACKAGE.md`, `PHASE5A_WORK_PACKAGE.md` and `PHASE5C_WORK_PACKAGE.md`, including their amendments and triages.

### Phase 6: Analytics / Activity Timeline (PA-5 and D-5-24 applied)

**M1 (COMPLETE).** `get_recruiting_metrics` with:
- a fixed metric catalog of 5 counts and 3 within-period ratios;
- month and quarter periods;
- no dimensions.

The tiers:
- **Tier 1** gets exact values under the caller's own session.
- **Tier 2** is computed under the service identity through the 5C internal services. It gets closed periods only, aligned cells, no margins, `k`-suppression (default 10), the quarter rule, and an exact output allowlist.

**Next milestones (not started; each needs its own work package).** The order follows `DEFERRED_DEBT.md` and `ANALYTICS_PIPELINE_RECOMMENDATIONS.md`:

1. **M2: hardening and settings.**
   - P6-1 and P6-2 (`set_setting` for integer settings);
   - P6-4 (a per-tenant cap on service-identity reads);
   - P6-6..P6-11;
   - the Phase 6 cleanups P5C-9..P5C-13.
2. **M3: coarse dimensions for Tier 1.** Dimensions such as recruiter or priority, applied first to Tier 1.
   - Tier 2 dimensions (geography, client segments with the dominance rule, rounding, and the query budget, Q-T1..T5) only after a separate user decision.
3. **M4: analytics store** (only on measured need). It follows the pipeline recommendations: derived canonical events, small closed `dateAdded` backfill windows, no dlt in the first slice, and Tier 2 reading only through an aggregate view. It closes P6-3 by freezing snapshots of closed cells.
   - **Prerequisite: the user's decision on Q-P1** (see `DEFERRED_DEBT.md`, "Phase 6 M1 close-out"). Until then, Tier 1 stays live under the caller's session.
4. **Later:**
   - the timeline as a `get_activity` scope;
   - composites;
   - the dashboard-agent contract;
   - the OW-13 end-to-end test.

The Security & Identity Reviewer's two-tier attack list (REQ §8.6) is blocking for every milestone.

### Phase 7: Broader Writes

**Scope:**
- the remaining writes, all on the generalized pipeline, each with its own scope, idempotency rule and dry-run/live tests;
- the `raw_query` scope;
- migrating `upload_candidate_resume` with byte-identical defaults (OWG-11).

The scope is otherwise unchanged.

### Phase 8: Bulk Resume

Unchanged.

### Phase 9: Auth / Security Closeout (reduced by PA-1)

- DEBT-2 (the `config/` package);
- re-running the full auth-failure, injection, approval-bypass and **multi-user isolation** matrices;
- logging, redaction and error-body hardening (see `DEFERRED_DEBT.md` for the 35 Phase 9 items);
- the process retrospective;
- the mypy ratchet and `ruff format` (P5C-14).

## 4. Deferred-debt targets

`DEFERRED_DEBT.md`, section "Phase 6 M1 close-out: updated open debt by target", is authoritative.

| Target | Open items |
|---|---|
| Phase 6 | 16 |
| Phase 7 | 19 |
| Phase 8 | 0 separate (only OWG-9's batch part, counted under Phase 7) |
| Phase 9 | 35 |
| Pre-production / external gates | 2 (P5A-7; P5A-16, which also covers P6-4) |
| Waiting on an HV item (fails closed meanwhile) | 4 (NB-15, RAG-5, OWG-8, P4B-5) |
| **Total** | **76** |

**Open user decision:** **Q-P1**, whether Tier 1 aggregates may come from a store built under the service identity (`DEFERRED_DEBT.md`). It must be decided before M4.

## 5. External dependencies and the pre-production gate

### EXT-1: `create_note` production enablement

| | |
|---|---|
| **Blocking HV** | HV-B11 / HV-C5 is **unresolved**: no documented way to obtain the current user's CorporateUser ID. |
| **State** | `create_note` is code-complete. It is disabled in every tenant until that tenant has a positive verification verdict. P4B-8 is closed (5B). |
| **Resolution path (implemented in 5A, D-5A-15 / A3-4)** | A setup admin of the tenant, with a linked Bullhorn session, runs the **admin-controlled one-note verification procedure**: authorize, preview and confirm exactly one Note on a designated test record. The read-back verdict is recorded. A positive verdict enables person targets, as long as the `rest_url` fingerprint is unchanged. |
| **Before enablement** | Re-assess P5B-1 / P5B-14 (residual scrub encodings). On an SSO tenant, EXT-2 comes first. |

### EXT-2: SSO / Duo tenant login enablement

| | |
|---|---|
| **Blocking HV** | HV-C2 is **unresolved**: Bullhorn's SSO/Duo behaviour in the OAuth code flow is undocumented. **The user's own tenant uses SSO with Duo.** |
| **State** | On an SSO tenant, ordinary users' logins are refused (`unsupported_sso`) until the tenant is enabled. |
| **Resolution path (implemented in 5A, A3-6)** | A setup admin performs one real **verification login** through the deployment's HTTPS callback. Only server-observable results are recorded, with no secrets. The admin then explicitly commits `enable_sso_login`. Re-verification is required if the auth host or the OAuth client configuration changes. |
| **Prerequisites** | **Fix P5A-7 first:** a failed ping must record a negative observation. A deployed shared server with a public HTTPS base URL (Q-A2). The callback URI must be registered on the tenant's Bullhorn API key (HV-C1). |

### Pre-production gate (P5A-16)

Before the first production shared deployment, re-assess:
- the redaction and DoS items P5A-10..P5A-14;
- P5C-2 (logger children created later);
- P5C-3 (the legacy audit records raw query text and path IDs);
- P5C-16 (the cursor / audit key silently falling back to a per-process secret);
- P6-4 (the service-identity read load from Tier 2).

Single-worker operation is enforced (5A L-5), and DEBUG logging is unsupported in shared mode.

### 5.1 Unresolved HV items across Phase 5, and their fail-closed effect

| HV item | Sub-phase | Unresolved aspect | Fail-closed effect |
|---|---|---|---|
| HV-D1..D3 | 5B | `/settings/commentActionList` behaviour | `SETTINGS_ACTION_SOURCE_VERIFIED=False`. The settings source is unreachable. Note actions come only from meta options plus admin-adopted mappings. |
| HV-C2 | 5A | SSO / Duo in the code flow | `unsupported_sso` for ordinary users on an SSO tenant, until EXT-2. |
| HV-C3 (partial) | 5A | Token-response field names | Strict RFC 6749 parsing. Any deviation means login fails. |
| HV-C5 = HV-B11 | 5A / 4B | Current-user CorporateUser ID | `create_note` is disabled until there is a positive verification verdict (EXT-1). `commentingPerson` is omitted (the default is the creator). |
| HV-C6 | 5A | Token revocation | Logout is local-only: the session is deleted on the server, and Bullhorn tokens expire naturally. |
| HV-C7 | 5A | PKCE | No PKCE: a confidential client plus `state`. |
| HV-C9 | 5A | Concurrent-session limits | Logins and refreshes are serialized per principal. |
| HV-B5 | 4B (inherited) | Note query / filter / ordering mechanics | `get_notes` works only on the job and placement scopes. Other scopes and the action / author / date filters return `unsupported_filter`. Results are in server order. |
| HV-B9 | 4B (inherited) | CorporateUser lookup by name for the `author` filter | `author` returns `unsupported_filter`. |
| HV-Q2 (partial) | 5C | `LIKE` and string escaping | No `starts_with`. String values are limited to `[A-Za-z0-9 ._@-]`; anything else, including apostrophes, returns `unsupported_value`. |
| HV-Q3 | 5C | `/search` Lucene index names | `/search` is not used; there is no effect, because `/query` covers all 8 entities. |
| HV-Q4 | 5C | `/query` `orderBy` direction | Offset paging only, with a consistency warning. Any `sort` returns `unsupported_sort`. P4B-5 stays open. |
| HV-Q6 (partial) | 5C | Whether `/query` returns soft-deleted rows | `include_deleted=true` returns `unsupported_filter`. The exclusion is query plus post-filter. A NULL `isDeleted` means "not deleted" (interpretation HV-Q6-I). |
| HV-Q7 (partial) | 5C | `Retry-After` | Not parsed. Fixed backoff: base 1 s, cap 8 s, at most 2 retries, a 30 s budget. |
| HV-Q9 (partial) | 5C | Appointment status, cancellation and reschedule fields; placement client and recruiter fields | `interview_rescheduled` returns `unsupported_concept`. `appointment.status`, `placement.client_corporation_id` and `placement.recruiter_id` are tenant-mapped only; when unmapped, the links are null and listed in `unresolved_links`. A recurring series is not expanded. |
| HV-Q9b | 5C | Parent-appointment link for invitee copies | See `PHASE5C_HV_VERIFICATION.md`. If unresolved, every `interview_*` concept returns `unsupported_concept` (`invitee_copies_unresolved`), and so do the M1 interview metrics. |
| HV-Q10 | 5C | Status-history sources | `client_submission` is dated only by `submission_date_added`. `job_status_changed` / `candidate_status_changed` return `unsupported_concept`. RAG-5 / OWG-8 stay open. |
