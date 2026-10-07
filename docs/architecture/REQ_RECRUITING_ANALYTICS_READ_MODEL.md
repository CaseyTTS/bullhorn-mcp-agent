# Requirement: Recruiting Activity & Analytics Read Model

| | |
|---|---|
| **Status** | Recorded 2026-10-06 as a roadmap and specification requirement. On the same day it was reconciled with the operational safe-write requirement and with the user's final, fixed phase order (`ROADMAP.md` revision 3). No phase work has started. **§8 (the two-tier data access model) was added on 2026-10-07 and is binding** (D-5-24). **§8.8 (analytics authorization policy, D-6-1..D-6-4; 2026-10-07) is binding and supersedes any conflicting text.** |
| **Source** | A user requirement relayed by the coordinator on 2026-10-06, restated and merged in the user's final roadmap injection. §1 transcribes it faithfully. §2 onward is Architect analysis. |
| **Shared vocabulary** | `CANONICAL_ACTIVITY_VOCABULARY.md` is the single definition of the activity concepts and the event shape. This document refers to it and does not redefine them. |
| **Related** | `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` (tenant value mappings, HV-1, CT-1, SB-1..13) and `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` |
| **Ownership** | `ROADMAP.md` §1 |
| **Phase 3** | Not reopened. Catalog gaps are logged as RAG-1..10 in `DEFERRED_DEBT.md`. |

**HV-1 applies to every Bullhorn name in this document.** Any Bullhorn representation that the Phase 3 catalog has not already verified is only a *candidate*. Before use it must be verified against authoritative reference material or against the connected tenant's metadata. If it cannot be verified, it is marked unresolved and surfaced in setup. It is never guessed.

---

## 1. The requirement (faithful transcription, structured)

**RA-1 Purpose.** The MCP exposes a complete, normalized read model of recruiting activity. Dashboard and analytics agents can then compute operational statistics without reinterpreting raw Bullhorn data.

**RA-2 Client submissions.**
- A client submission is a business event defined by the tenant. It is not every generic JobSubmission.
- The tenant defines which status, field or workflow constitutes "submitted to client". This is a value mapping made during setup (TS-2).

**RA-3 Interviews.**
- An interview is an individual occurrence linked to a candidate and a job.
- Each interview is scheduled, upcoming, completed, cancelled, rescheduled or tagged, wherever the verified tenant/API model supports that state.
- An interview is never inferred from a candidate merely reaching an interview status.

**RA-4 Recruiter jobs.**
- By default, jobs are attributed by job-created date plus primary recruiter.
- The primary recruiter / owner relationship is preserved.

**RA-5 Job priority.** `job.priority` is exposed as a canonical field, whether its Bullhorn source is a standard field or a custom one.

**RA-6 Offers.** Offer states are extended, accepted, declined and pending/open. They are discovered and mapped during tenant setup.

**RA-7 Placements.** Each placement keeps its relationships to candidate, job, recruiter, client and date.

**RA-8 Drill-back.** Every metric keeps the source Bullhorn IDs it was computed from, where applicable: candidate, job, recruiter/owner, client corporation, client contact, submission, interview/appointment, placement, and offer record/status. *Tier 2 callers never receive these IDs (§8).*

**RA-9 Activity concepts.**
- The concepts are `job_created`, `client_submission`, `interview_scheduled`, `interview_completed`, `interview_cancelled`, `offer_extended`, `offer_accepted`, `offer_declined`, `placement_created` and `note_created`.
- They are defined once, in the MCP domain/configuration layer, in `CANONICAL_ACTIVITY_VOCABULARY.md`.

**RA-10 Capabilities.**

| ID | Capability |
|---|---|
| RA-10a | Client submissions by recruiter, job, client, candidate and date range |
| RA-10b | Interview instances by recruiter, job, client, candidate and date range |
| RA-10c | Jobs created in a range for a primary recruiter |
| RA-10d | Jobs grouped or filtered by priority |
| RA-10e | Offers by status and date range |
| RA-10f | Placements by recruiter, job, client and date range |
| RA-10g | Funnel: submission → interview → offer → placement |

**RA-11 Capability inventory.**
- The capabilities are named `get_job_submissions`, `get_client_submissions`, `get_interviews`, `get_job_interviews`, `get_candidate_interviews`, `get_recruiter_jobs`, `get_jobs_by_priority`, `get_offers`, `get_recruiting_activity`, `get_recruiter_metrics`, `get_job_metrics`, `get_client_metrics` and `get_team_metrics`.
- Under **CT-1** these names describe *capabilities*. The public MCP surface is a compact set of parameterized tools: Phase 5 `find_*` / `get_activity`, and Phase 6 `get_activity_timeline` and `get_recruiting_metrics(scope, filters, metrics=[…])`.

**RA-12 Layering (binding).**

```
Bullhorn raw data → canonical recruiting model → normalized activity definitions → analytics/read tools → dashboard/agent
```

**RA-13 Ownership.** The roadmap names the owning phase for each of the following:
1. tenant setup of business definitions;
2. expanded reads;
3. activity definitions;
4. metrics;
5. dashboard consumption.

## 2. Activity concepts

The concepts are defined in `CANONICAL_ACTIVITY_VOCABULARY.md`:
- §1 the event shape;
- §2 the concepts, their timestamps, their rules and the phase that implements each deriver;
- §3 the write-operation → concept mapping.

Analytics-specific notes:
- **RA-3 is enforced by design.** Interview concepts derive only from appointment occurrences. A submission that reaches an "interview" status without an interview appointment is never counted as an interview. Phase 6 may report such cases as a separately labelled diagnostic.
- **Per-concept decisions are made when the derivers are built.** Each concept needs a timestamp source, an attribution rule, re-submission handling and soft-delete handling. These are decided in Phase 5 (the derivers) and recorded as tenant rules through the 4A setup mechanism.

## 3. Coverage matrix: what the Phase 3 catalog supports, and its gaps

| Item | Already supported (catalog v1) | Gaps → owning phase (see `DEFERRED_DEBT.md`) |
|---|---|---|
| RA-2 | `submission.{id, date_added, candidate_id, job_id, status, source, sending_user_id}`. The client is reached via the job. | Status history: RAG-5 → 5. Value mapping: RAG-9 → 4A. `user`: RAG-1 → 4A/5. |
| RA-3 | `appointment.{id, date_added, type, start_at, end_at, candidate_id, client_contact_id, job_id, owner_id}` | Interview fields, outcome, cancel/reschedule and attendees: RAG-4 → 5/6. Value mappings → 4A mechanism. |
| RA-4 | `job.{id, date_added, owner_id}` | `job.primary_recruiter_id` mapping: RAG-3 → 4A (mapping), 5 (reads). `user`: RAG-1. |
| RA-5 | — | `job.priority`: RAG-2 → 4A (canonical field and mapping), 5 (reads), 7 (write). |
| RA-6 | — | Offer representation: RAG-6 → 4A (value mappings), 5 (reads and concepts). |
| RA-7 | `placement.{id, date_added, candidate_id, job_id, status, start_date, end_date, …}` | Recruiter, client and submission links: RAG-7 → 5. |
| RA-8 | Most entity IDs | `user` (RAG-1); offer reference (RAG-6). |
| RA-10 date ranges | Datetime fields | Coercion and timezone: RAG-8 → 4A (SB-4). Date-range semantics: SB-5 → 4B. |

## 4. Tenant business values and rules

These live in the 4A profile, format v2. They are entered as **value mappings and rules** using the 4A mechanism (`REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` §2.2), discovered or confirmed during setup, and never guessed.

| Mapping / rule | Purpose |
|---|---|
| `client_submission` | Status values or a rule, plus the dating rule (Q-2) |
| `interview` | Classification and state mappings (Q-4) |
| `offer` | Representation and state values |
| `job.primary_recruiter_id` | Field mapping (Q-3) |
| `job.priority` | Field mapping, plus ordered values (Q-5) |
| `attribution.<concept>` | Recruiter attribution per concept (Q-8) |
| `placement` | Exclusions (Q-7) |
| `settings.reporting_timezone` | Reporting timezone, plus period convention (Q-6) |
| `teams` | Team definitions (Q-9) |
| `note.action_types` | Allowed note action types (OW-3) |

## 5. Drill-back requirements (binding on Phases 5 and 6)

These apply to **Tier 1 only** (§8).

1. Every event carries the links defined in vocabulary §1. A link is never silently omitted.
2. Every metric returns the contributing `activity_id`s, or a re-executable handle that reproduces them. Any truncation is explicit.
3. Every event and every metric reports the profile version and the rule that produced it.
4. IDs are canonical. That means Bullhorn IDs passed through the non-overridable `id → id` mapping. Raw Bullhorn field names never appear in the output.
5. Any new Bullhorn representation must pass HV-1 verification.

## 6. Open questions for the user

| # | Question |
|---|---|
| Q-1 | *Resolved by the user's fixed phase order (`ROADMAP.md` revision 3).* |
| Q-2 | If submission status history is unavailable, may a client submission be dated by the submission's created date? |
| Q-3 | Is the job's primary recruiter its **owner**, or some other assignment? This is answered through the 4A mapping, but a default suggestion is needed. |
| Q-4 | May an interview count as completed simply because its time has passed and it was not cancelled? |
| Q-5 | Where does the job priority value come from, and what are its values and their order? |
| Q-6 | What is the reporting timezone, and are periods end-inclusive? |
| Q-7 | Which placement statuses should be excluded from counts? |
| Q-8 | For each metric, which recruiter gets the credit? |
| Q-9 | What defines a team? |
| Q-10 | Is on-demand computation acceptable, or is a cache or snapshot store needed? A store would be a new component. |
| Q-11 | Should the dashboard agent be denied raw tools? The proposal is yes, enforced server-side in Phase 6 using the policy engine. Phase 4B introduces that engine and Phase 7 generalizes it. |

## 7. Non-goals

This document makes no source changes and no test changes, starts no phase work, and does not cover a dashboard UI.

---

## 8. Two-tier data access model (binding; D-5-24; the single source)

**Status.** This is a user requirement of 2026-10-07. It is **binding for 5C** (record-level tier gating) and for **Phase 6** (the Tier 2 aggregate interface and de-identification). 5A provides only the identity hook (`PHASE5A_WORK_PACKAGE.md` Amendment A2).

**Single source.** Other documents reference this section and must not restate it.

**Supersession.** §8.8 (D-6-1..D-6-4) supersedes any conflicting text in §8.1–§8.5, in particular the definition of who receives Tier 2.

### 8.1 Tiers (TT-1)

| Tier | Who | May receive |
|---|---|---|
| **Tier 1, `bullhorn_user`** | An authenticated workspace principal **with a valid linked Bullhorn session** (5A) | Record-level and aggregate Bullhorn data, according to their own Bullhorn permissions and MCP policy: candidates, jobs, submissions, interviews, notes, placements, clients, and approved writes |
| **Tier 2, `workspace_only`** | An authenticated workspace principal **without** a valid linked Bullhorn session (*superseded by D-6-2..D-6-4: Tier 2 analytics now requires an explicit grant*) | **Only** approved, de-identified aggregate, historical or statistical results, computed through the read-only service identity |

**Tier derivation (TT-2).**
- The tier comes **only** from the authenticated identity context (5A), never from tool arguments or the model.
- A Bullhorn session that is linked but expired or invalid means `workspace_only` until the user re-links. Under D-6-3, this state on its own no longer grants any analytics.
- Service principals and local-mode callers are separate modes. They are not Tier 2 users.

### 8.2 What Tier 2 may and must never receive

**May receive (TT-3).** Only metrics and dimensions that the admin has approved:
- historical job counts and trends;
- jobs by broad geography (state, metro or region);
- job family and type statistics;
- counts of submissions, interviews, offers and placements;
- funnel and conversion rates;
- time-to-submit and time-to-fill;
- aggregate operational throughput;
- anonymized client concentration, industry and segment statistics;
- other admin-approved aggregate business metrics.

**Must never receive (TT-4):**
- candidate names or any identifying candidate information;
- resumes or contact information;
- candidate-level histories or notes;
- candidate IDs or any record-level drill-down;
- named client companies;
- client-contact information;
- raw submission, interview or placement records;
- raw Bullhorn search or query capability;
- exact addresses;
- source or provenance IDs that could reconstruct restricted records.

**Service-identity boundary (TT-5).**
- The service identity may read underlying records **internally** to compute an approved metric.
- Tier 2 receives **only** the policy-filtered aggregate.
- Authorization and de-identification happen in the server **before** anything is returned to the model. The system never relies on prompting the LLM.
- Tier 2 output is validated against a **Tier 2 output schema allowlist**: aggregate values, the dimension labels from approved levels, `suppressed` flags and metric metadata. Anything else is rejected before return.

### 8.3 Tool availability by tier (TT-6)

| Tools | Tier 1 | Tier 2 |
|---|---|---|
| Legacy 10, `find_records`, `get_activity`, `get_notes`, `create_note`, `confirm_write`, `search_entities`, `query_entities` | yes (per policy) | **denied** (`bullhorn_auth_required`), with zero Bullhorn calls and no service fallback |
| `get_recruiting_metrics` (Phase 6) | yes, full (with drill-back) | **yes**, the primary Tier 2 interface; tier enforced automatically (*D-6-2..D-6-4: only for principals holding the analytics grant*) |
| `bullhorn_session` | yes | yes (so the user can link their Bullhorn account) |
| `setup_status` | yes | yes (status only, no record data) |
| Setup / admin tools | admin role only | admin role only |

**Parameter manipulation (TT-7).** The permission layer must make it impossible for a Tier 2 caller to obtain raw records by manipulating tool parameters. For example, `get_recruiting_metrics` with a filter narrowed to one record, `group_by` set to an ID or name, or a request for drill-back must not leak record data.

### 8.4 Re-identification controls (TT-8; implemented in Phase 6)

1. **Allowlisted metrics and dimensions only.** Tier 2 queries are composed from an admin-approved metric × dimension × time-bucket catalog. There are no free-form filters, no record-level dimensions, and no `group_by` on any identifier, name, or exact or free-text field.
2. **Minimum cohort threshold `k`.** It is configurable per tenant, with a conservative default. A result cell with a cohort ≥ `k` is allowed. A cell below `k` is **suppressed** with `insufficient_aggregate_population`.
   - **Complementary suppression** is applied, so a suppressed cell cannot be recovered from totals or margins.
   - A separate client-level threshold `k_client` applies to any client-segment statistic.
3. **Differencing and repeated-query defences.**
   - **Overlap check.** For each tenant, the server records the cohort definitions of answered Tier 2 queries for a window. A query whose cohort differs from an answered query's cohort by fewer than `k` members is refused or suppressed.
   - **Coarsening.** Time buckets have a minimum width, and geography is limited to approved levels.
   - **Rounding.** Counts and rates are rounded or bucketed as configured.
   - **Budget.** There is a per-principal query budget, with an audit trail.
4. **Geography.** Only the approved aggregation levels: state, metro and region. They are derived server-side from approved mappings. City, ZIP, address and coordinates are never returned to Tier 2.
5. **Clients.** Results are reported only as anonymous categories, industries or segments, or as concentration percentages, with a **dominance rule**: a segment in which a single client exceeds a configured share is suppressed. Company names and IDs are never returned.
6. **Provenance.** Tier 2 results carry the metric definition version and the policy version, and **no** source IDs or `activity_id`s.

### 8.5 Phase ownership

| Phase | Delivers |
|---|---|
| **5A** | The identity hook only: `access_tier` in the identity context, and fail-closed record-level access for `workspace_only` with no service fallback (Amendment A2). No analytics logic. |
| **5C** | TT-6 gating for `find_records` / `get_activity` and for every record-level tool, with Security & Identity tests. No Tier 2 metrics. |
| **Phase 6** | `get_recruiting_metrics` with TT-3..TT-8: the metric/dimension catalog, `k` / `k_client` thresholds, complementary suppression, differencing defences, geography and client anonymization, the Tier 2 output schema allowlist, and the service-identity internal computation path. |

### 8.6 Security & Identity Reviewer blocking tests (Phase 6, plus 5C for gating)

The reviewer must attack each of these:
- candidate re-identification;
- client re-identification;
- cohort-threshold bypass, including via complements and margins;
- repeated-query and differencing attacks;
- requests for restricted IDs or provenance;
- overly precise geographic queries;
- service-identity raw-data leakage;
- non-Bullhorn (Tier 2) users invoking record-level tools;
- parameter manipulation to obtain raw records;
- *(D-6-3)* a logged-out, expired or unlinked user without the analytics grant obtaining any analytics.

### 8.7 Open questions (Phase 6 work package)

| ID | Question |
|---|---|
| Q-T1 | The default `k` (proposed: 10) and `k_client` (proposed: 5). |
| Q-T2 | The dominance threshold (proposed: suppress a segment where one client is more than 50% of the segment). |
| Q-T3 | The source of the metro and region definitions. |
| Q-T4 | The initial approved Tier 2 metric catalog. |
| Q-T5 | The differencing-history retention window and the query budget. |

### 8.8 Analytics authorization policy (binding user decisions D-6-1..D-6-4, 2026-10-07)

**Status.** These decisions are binding, and **supersede** all conflicting text in this document, in `PHASE5_PROPOSAL.md` (D-5-24), in `PHASE6_M1_WORK_PACKAGE.md` and in `ROADMAP.md`.

| ID | Decision |
|---|---|
| **D-6-1** | **Tier 1 analytics stays within the caller's own Bullhorn permissions by default.** Analytics for a Bullhorn-linked user is computed under that user's session, as in M1. Any future store-backed Tier 1 aggregate must not exceed what that user can see in Bullhorn, unless D-6-2 applies. |
| **D-6-2** | **Broader, tenant-wide analytics requires a separately granted Workspace analytics permission** (the "analytics grant"). This covers any aggregate beyond the caller's own Bullhorn visibility, including every service-identity Tier 2 aggregate. The grant is per tenant, explicit and admin-controlled. |
| **D-6-3** | **Logging out of Bullhorn must not itself grant Tier 2 analytics access.** Being unlinked, expired, pending or logged out never implies any analytics entitlement. Such a caller without the grant is **denied** analytics, with zero Bullhorn calls and zero service-identity calls. |
| **D-6-4** | **Tier 2 access comes from trusted Workspace or admin authorization only**, independent of the Bullhorn link state. The trusted source is server-side admin configuration, or a claim in the verified token. It is never a tool argument, a model output or the user's own link or unlink action. |

**What this supersedes.**
- §8.1's definition of Tier 2 as any principal without a linked session.
- §8.3's "yes" for every Tier 2 caller.
- The P6-5 acceptance in `DEFERRED_DEBT.md`.
- The M1 behaviour (`PHASE6_M1_WORK_PACKAGE.md` §3) under which any `workspace_only` caller, including a logged-out Bullhorn user, receives Tier 2 aggregates.

**Conformance gap (P6-12).** The current M1 code does **not** conform: it serves Tier 2 aggregates to any `workspace_only` caller.
- This is **pre-production required**.
- It **blocks any shared-mode deployment** that exposes `get_recruiting_metrics`.
- Local mode is unaffected, because it has no Tier 2.
- Until it is fixed, a shared deployment must not run M1. An interim mitigation is acceptable: for example, no service principal configured, which already gives `unavailable`.

**Q-P1 is resolved by default.** Store-backed tenant-wide aggregates are available **only** to principals holding the analytics grant. Any other Tier 1 user is served live under their own session, or from a store filtered to their own Bullhorn visibility; the latter requires HV.
