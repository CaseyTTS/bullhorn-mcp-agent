# Bullhorn Recruiting Orchestrator MCP: Authoritative Phased Roadmap

| | |
|---|---|
| **Owner** | Architect |
| **Revision** | 3 (FINAL before Phase 4), 2026-10-06. The **phase order is fixed by the user** and is binding. |
| **Requirements reconciled** | `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` (TS, HV-1, CT-1, SB-1..13); `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` (RA-1..13); `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` (OW-0..13). |
| **Shared vocabulary** | `CANONICAL_ACTIVITY_VOCABULARY.md` is the single definition of the activity concepts and of the operation→concept mapping. |
| **Base** | Plan `enter-planning-mode-only-binary-cherny.md` §8, amended by ROADMAP-AMENDMENT-1..4 (`DEFERRED_DEBT.md`). |
| **Precedence** | This revision supersedes the phase numbering of plan §8 and of roadmap revisions 1 and 2. All plan §8 scope commitments are preserved (§2). Completed Phases 0–3 are not deconstructed. |

## Standing rules

These rules come from plan §8 and are unchanged:

- Every phase ends fully green (`pytest`, `ruff` and `mypy` all pass).
- The 10 original tools stay behaviorally and schema-identical.
- Every phase runs Architect work package → Builder → independent Reviewer.
- Every phase is independently shippable.
- **Sub-phases.** The Architect may split a phase into lettered sub-phases at work-package time (as 4A/4B already are). Ownership stays with the parent phase number.

## Binding constraints (recorded in the plan's revision sections)

1. **Layering (RA-12):**

   ```
   Bullhorn raw data → canonical recruiting model → normalized activity definitions → analytics/read tools → dashboard/agent
   ```

2. **Safe-write pipeline (OW-6, final order):**

   ```
   intent → canonical operation → target resolution → validation → permission → idempotency/duplicate check → dry-run/preview → approval policy → Bullhorn write → audit → normalized result
   ```

   - No new write exists outside this pipeline, and no arbitrary raw Bullhorn write is ever accepted.
   - Phase 4B owns the **minimal** pipeline that `create_note` needs. Phase 7 generalizes it.
   - The legacy `upload_candidate_resume` is the single documented exception until it is migrated in Phase 7, with its default behavior preserved.

3. **Controlled configuration change (TS-4):**

   ```
   propose → validate → diff → admin approval → new version → activate → audit
   ```

   Phase 4A owns this workflow. It shares its approval and audit primitives with the write pipeline.

4. **One vocabulary (RA-9, OW-11).** No consumer defines activity concepts locally.

5. **HV-1, hard Bullhorn verification.** No Bullhorn entity, field, association, status, action type or write behavior is modelled from assumption. Each is verified against authoritative reference material or against the connected tenant's metadata. Anything unverified is marked unresolved, surfaced in setup, and never guessed.

6. **CT-1, compact tool surface.** Public tools are few and parameterized. Capability lists such as RA-11 map onto them; they are not one tool per permutation.

7. **TS-7 capability gating.** A capability whose required mappings are invalid refuses to run and returns its missing setup requirements.

---

## 1. Ownership

### 1.1 Recruiting analytics (RA-13) and operational (OW-12) items, de-duplicated

| # | Responsibility | Requested by | Owning phase | Contributing phases |
|---|---|---|---|---|
| 1 | Tenant setup / field mapping, including business-value mappings | OW-12.1, RA-13.1, TS-1..7 | **4A** | 5 (adds value mappings for the concepts it introduces, through the 4A mechanism) |
| 2 | Note action-type discovery, mapping and validation | OW-12.2 | **4B** | 4A (provides the value-mapping mechanism) |
| 3 | Expanded entity reads | RA-13.2, OW-12.3 | **5** | 4B (Note reads) |
| 4 | Normalized activity / event definitions | RA-13.3 | **5** | 4B (event model and `note_created`) |
| 5 | Activity-timeline normalization | OW-12.4 | **6** | |
| 6 | Note creation | OW-12.5 | **4B** | |
| 7 | Generalized safe-write infrastructure | OW-12.6 | **7** | 4B (minimal pipeline) |
| 8 | Write-specific (per-operation) permissions | OW-12.7 | **4B** for `note.create` | 7 (all other operations, plus the `raw_query` scope) |
| 9 | Duplicate / idempotency safeguards | OW-12.8 | **4B** for notes | 7 (generalized), 8 (resume batch) |
| 10 | Aggregation / metrics tools | RA-13.4, OW-12.9 | **6** | |
| 11 | Downstream dashboard-agent consumption | RA-13.5, OW-12.10 | **6** (final deliverable of Phase 6) | |

### 1.2 Placement of other committed capabilities

| Capability | Phase |
|---|---|
| `find_*` structured query tools and `schema/query_builder.py` (ROADMAP-AMENDMENT-1) | **5** |
| Composites (`candidate_360`, `job_360`) | **6**. They consume the Phase 5 reads and the Phase 6 timeline. |
| The OW-13 end-to-end acceptance scenario | **6**. The timeline is required for "recent activity"; the note-write half is already provable at the end of 4B. |

### 1.3 Required supporting behaviors (SB-1..13)

| ID | Behavior | Owning phase | Notes |
|---|---|---|---|
| SB-1 | Pagination / complete-result retrieval | **5** | 4B applies an interim rule: explicit `truncated` / `next` markers on `get_notes`. |
| SB-2 | Bullhorn result limits | **5** | Owned with SB-1. The limits themselves are verified (HV-1). |
| SB-3 | Retries / rate limiting | **5** | Client-level, additive, ahead of the heavy reads. Today's single 401 retry is unchanged. |
| SB-4 | Timezone normalization | **4A** | The tenant `reporting_timezone` setting plus an opt-in datetime coercion primitive (RAG-8). |
| SB-5 | Date-range semantics | **4B** | Defined once, at the first date-filtered read (`get_notes`), and reused by Phases 5 and 6. |
| SB-6 | Inactive / soft-deleted records | **5** | 4B handles it for notes. |
| SB-7 | Recruiter identity resolution | **4B** | Author filters and identity cards. It builds on the 4A canonical `user` entity, and Phase 5 extends it. |
| SB-8 | Client / contact identity resolution | **4B** | Note targets and identity cards. Phase 5 extends it. |
| SB-9 | Picklist / status / action discovery | **4A** | DEBT-3. 4B uses it for Note action types. |
| SB-10 | Duplicate prevention / idempotency | **4B** for notes | 7 (generalized), 8 (resume). |
| SB-11 | Partial failures in bulk operations | **8** | 4B covers multi-association partial failure. |
| SB-12 | Source-record provenance | **4B** | The activity event's `source`, `links` and `origin`, plus write-result provenance. Mapping provenance is 4A. Metric drill-back is 6. |
| SB-13 | Tenant-specific rules kept separate from generic code | **4A** | Every tenant rule lives in the versioned profile; code stays generic. Binding on all later phases. |

## 2. Phase sequence and renumbering

### 2.1 Final sequence

| Phase | Name | State |
|---|---|---|
| 0 | Role scaffolding & foundation | COMPLETE (`9262810`) |
| 1 | Cross-cutting scaffolding | COMPLETE (`0930bf0`) |
| 2 | Module reorganization | COMPLETE (`4d7da27`); DEBT-1 and DEBT-2 carried |
| 3 | Canonical schema & mapping layer | COMPLETE (passed review, round 5) |
| **4A** | Tenant Setup & Mapping Management | COMPLETE (review PASS, 2026-10-06) |
| **4B** | Notes / Activity Core | COMPLETE (re-review PASS, 2026-10-07). `create_note` is disabled in production pending external dependency **EXT-1** (§5). |
| **5** | Expanded Recruiting Reads | |
| **6** | Analytics / Activity Timeline | |
| **7** | Broader Writes | |
| **8** | Bulk Resume | |
| **9** | Auth / Security Closeout | |

### 2.2 Original plan §8 → final

| Plan §8 phase | Final phase(s) |
|---|---|
| 0–3 | 0–3 (unchanged) |
| 4 First-run setup flow | **4A** (expanded by TS-1..7) |
| 5 Entity coverage expansion | **4B** (Note reads; `add_note` is now `create_note`; the first `create()` primitive). **5** (reads of submissions, appointments, tearsheets, clients and references; `find_*`; `query_builder`). **7** (`create_submission`, `create_appointment`, `add_to_tearsheet`, generic `create()` / `update()`). |
| 6 Composite tools | **6** |
| 7 Write-path hardening | **4B** (minimal enforcement for notes). **7** (generalized per-operation permissions, the `raw_query` scope, migration of the legacy upload). **9** (re-running the security matrices and the approval-bypass tests). |
| 8 Batch resume import | **8** |
| 9 Auth hardening & retrospective | **9** |
| (new) Analytics | **5** (definitions and reads) and **6** (timeline, metrics, dashboard) |

### 2.3 Interim roadmap revision 2 (Architect proposal, superseded) → final

| Rev-2 phase | Final phase(s) |
|---|---|
| 4 Setup | **4A** |
| 5 Reads + find | **5** (Note reads move to **4B**) |
| 6 Activity model (library) | **4B** (event model and `note_created`); **5** (all other concept derivers) |
| 7 Business-definition setup | **4A** (mechanism and business values); **4B** (Note action types) |
| 8 Safe-write + `create_note` | **4B** (minimal); **7** (generalized) |
| 9 Activity read tools | **5** (entity-level activity reads); **6** (timeline) |
| 10 Metrics | **6** |
| 11 Dashboard contract | **6** |
| 12 Submission/interview writes | **7** |
| 13 Record/status/placement writes | **7** |
| 14 Composites | **6** |
| 15 Write hardening | **7** and **9** |
| 16 Batch resume | **8** |
| 17 Auth hardening | **9** |

Roadmap revision 1 (analytics only) is fully superseded by revision 2 and therefore by this table.

### 2.4 Older references

The Phase 3 documents use the original plan numbering. Map those references as follows:

| Reference in older documents | Means in the final roadmap |
|---|---|
| "Phase 4" (setup) | Phase 4A |
| "Phase 5" (`find_*` / `query_builder`) | Phase 5 |
| "Phase 5" (create/update payloads) | Phase 4B (notes) or Phase 7 |
| "Phase 6" (composites / to-many associations) | Phase 6 |

Every Phase 4 item in `DEFERRED_DEBT.md` is re-targeted to 4A (§4).

## 3. Phases

### Phase 4A: Tenant Setup & Mapping Management (owns rows 1, SB-4, SB-9 and SB-13)

**Scope**, fixed by the user: TS-1 to TS-7 in full.

- first-run setup state;
- metadata discovery;
- verified standard mappings;
- review of unmapped and custom fields;
- field-mapping edits;
- value and business-rule mappings;
- profile import and export;
- validate and finalize;
- versioning;
- a rollback-ready versioned design;
- rediscovery and revalidation;
- the setup-required and status states;
- capability gating.

**Design inputs:** `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` §2.

- Profile format v2 (mapping records with provenance), additive over Phase 3 v1.
- Canonical catalog v2 subset: product-defined canonical fields that setup must expose, for example `job.priority`, `job.primary_recruiter_id` and a canonical `user` entity (RAG-1 to RAG-3). Their Bullhorn standard mappings are included only if verified (HV-1).
- The controlled-change workflow, with approval tokens bound to the diff, the actor and an expiry.
- Actor identity (OWG-10).
- Audit with correlation IDs for configuration changes.
- Picklist/value enrichment (DEBT-3).
- The tenant timezone setting and the datetime coercion primitive (RAG-8).
- A compact setup tool family (CT-1).
- An additive setup state on `connection_status`.

**Debt targeted** (§4): DEBT-3, NB-3, NB-4, NB-5, NB-6, NB-7, NB-9, NB-11, NB-12, NB-13, NB-15, NB-17, NB-18, NB-19, RAG-1 (canonical part), RAG-2 (canonical and mapping part), RAG-3 (mapping part), RAG-8, RAG-9, OWG-1 (mechanism), OWG-3 (configuration-approval part), OWG-5 (configuration-audit part), OWG-10.

**Out of scope:** Bullhorn record writes (4B), Note action-type enforcement (4B), entity reads beyond what discovery needs (5), and DEBT-2's `config/` package (9).

### Phase 4B: Notes / Activity Core (owns rows 2, 6, the note parts of 8 and 9, SB-5, SB-7, SB-8 and SB-12)

**Scope**, fixed by the user:

- Note reads;
- the canonical Note representation;
- tenant Note action-type discovery and validation;
- safe Note creation;
- verified associations;
- dry-run, approval, permissions and audit.

**Public tools (CT-1):**

- `get_notes(filters…)`. Filters: target entity, candidate, job, client corporation, client contact, placement, submission, action type, author and date range, with an interim pagination marker.
- `create_note(target_type, target_id, action_type, comments, associations=None, dry_run=…)`. Whether `dry_run` defaults to on is open question Q-W2.

**Minimal safe-write pipeline**, owned here and generalized in Phase 7:

- the canonical operation `create_note`;
- target resolution with identity cards (read-before-write);
- validation, including action type against the 4A value mappings, with rejection plus valid or mapped alternatives;
- the `note.create` permission scope on a real policy check (the existing `permissions.check` signature, extended additively; default outcomes for the 10 original tools preserved);
- an idempotency key and a note duplicate probe;
- a dry-run preview bound to the commit;
- approval, reusing the 4A token binding;
- the Bullhorn create, plus the verified association mechanics (RAG-10, OWG-7), multi-association where permitted, orphan prevention and read-back;
- audit with an actor and a correlation ID;
- a normalized result containing the `note_created` event.

**Activity core:** the vocabulary event model (`CANONICAL_ACTIVITY_VOCABULARY.md` §1) and the `note_created` deriver. This is the foundation that Phase 5 extends.

**Debt targeted:** RAG-10, OWG-1 (discovery and enforcement), OWG-2 (`note.create` part), OWG-3 / OWG-4 / OWG-5 / OWG-6 / OWG-9 (minimal parts), OWG-7 (note create and associations).

### Phase 5: Expanded Recruiting Reads (owns rows 3 and 4, SB-1, SB-2, SB-3 and SB-6)

**Scope**, fixed by the user: JobSubmission and client submissions, interview instances, recruiter jobs by job-created date, priority, offers, placements, activity, and the structured query builder.

**Catalog and setup:**

- Canonical and Bullhorn catalog growth, verified under HV-1: submission/candidate/job status history, appointment interview fields, offer representation, placement links, and interview attendees where verified (RAG-4..7, OWG-8).
- Entering the new value mappings through the 4A mechanism. Catalog growth triggers `setup_revalidation_required`.

**Concept derivers:** every concept in the vocabulary except the timeline merge. That is `job_created`, `submission_created`, `client_submission`, the interview concepts, the offer concepts, `placement_created`, `candidate_status_changed` and `job_status_changed`.

**Public tools (CT-1)** are parameterized `find_*` / read tools. Examples:
- `find_records(entity, filters)`;
- `find_submissions(filters, client_only=…)`;
- `find_interviews(filters)`;
- `find_jobs(filters)`, covering a recruiter and created-date range, and priority;
- `find_offers(filters)`;
- `find_placements(filters)`;
- `get_activity(filters)`, the entity-level activity.

These cover RA-10a–f. The final names are set in the Phase 5 work package.

**Also:** `schema/query_builder.py` (ROADMAP-AMENDMENT-1), NB-2, pagination and complete-result retrieval, Bullhorn limits, retries and rate limiting, and soft-delete handling.

**Out of scope:** any write, metrics and the timeline.

### Phase 6: Analytics / Activity Timeline (owns rows 5, 10 and 11)

**Scope**, fixed by the user: the canonical activity timeline and aggregation/metrics.

**Public tools:**
- `get_activity_timeline(scope, filters)`, the cross-entity timeline for a job, candidate, client corporation or contact (OW-5);
- `get_recruiting_metrics(scope, filters, metrics=[…])`, covering recruiter, job, client and team metrics, the funnel and priority grouping (RA-10d/g).

**Requirements:**
- Every metric is computed only from vocabulary events.
- Every result carries drill-back IDs or a handle, plus the definition versions.

**Also:**
- Composites (`candidate_360`, `job_360`), including multi-attendee joins with partial-failure tests.
- The dashboard-agent consumption contract: versioned output schemas, contract tests, and a reference agent restricted server-side to Phase 5–6 tools.
- The OW-13 end-to-end acceptance test.

**Possible sub-split:** 6a (timeline and composites) and 6b (metrics and the dashboard contract).

### Phase 7: Broader Writes (owns row 7, plus the generalized parts of rows 8 and 9)

**Scope**, fixed by the user: additional controlled recruiting writes. All of them run on the generalized pipeline, each with its own scope, idempotency rule and dry-run/live tests:

- `create_candidate` / `update_candidate`;
- `create_submission` / `update_submission`;
- `update_submission_status`;
- `create_interview` / `update_interview`;
- `update_candidate_status`;
- `update_job_status`;
- `update_job_priority`;
- `create_placement` / `update_placement`;
- `associate_candidate_with_job`;
- `add_to_tearsheet`;
- `update_note`, only if Bullhorn permits it (to be verified).

**Also:**
- The generalized `SafeWritePipeline` (extracted from 4B), a generic `create()` / `update()`, and the full per-operation policy, including the `raw_query` scope for `search_entities` / `query_entities`.
- Migrating `upload_candidate_resume` onto the pipeline with byte-identical default behavior and resume-upload idempotency (OWG-11).

**Public surface (CT-1):** writes may be grouped into a few parameterized tools, for example `update_record_status(entity, id, status)`, rather than one tool per field. This is decided in the work package.

### Phase 8: Bulk Resume (owns SB-11)

**Scope:** the existing commitment, preserved. `import_resume_batch` (up to 10 resumes: parse → resolve/dedupe → field mapping → preview), then `commit_resume_batch` (the approved subset only, with a per-record result).

**Built on:** the Phase 7 candidate writes and pipeline, Phase 5 find, the batch approval tokens extending the 4A/4B gate, and resume-upload idempotency.

### Phase 9: Auth / Security Closeout

**Scope:**
- DEBT-1 (`auth/trusted_origins.py`, including the substring-vs-host-suffix decision);
- DEBT-2 (the `config/` package);
- OBS-1 (`auth/secrets.py`);
- re-running the full auth-failure, injection and approval-bypass matrices against every tool;
- the process retrospective;
- the mypy ratchet.

NB-16 is considered here if the `config/` refactor touches package initialization.

## 4. Deferred-debt targets (final; see `DEFERRED_DEBT.md`)

| Phase | Items |
|---|---|
| 4A | DEBT-3, NB-3, NB-4, NB-5, NB-6, NB-7, NB-9, NB-11, NB-12, NB-13, NB-15, NB-17, NB-18, NB-19, RAG-1 (canonical `user`), RAG-2 (canonical field and mapping), RAG-3 (mapping), RAG-8, RAG-9, OWG-1 (mechanism), OWG-3 (configuration approvals), OWG-5 (configuration audit), OWG-10 |
| 4B | RAG-10, OWG-1 (Note actions), OWG-2 (`note.create`), OWG-3 / OWG-4 / OWG-5 / OWG-6 / OWG-9 (minimal), OWG-7 (note create and associations) |
| 5 | NB-2, RAG-1 (resolution extension), RAG-2 (reads), RAG-3 (reads), RAG-4, RAG-5, RAG-6, RAG-7, OWG-8 |
| 6 | RAG-4 (multi-attendee composites) |
| 7 | RAG-2 (`update_job_priority`), OWG-2 (generalized, plus `raw_query`), OWG-3 / OWG-4 / OWG-5 / OWG-6 / OWG-7 / OWG-9 (generalized), OWG-11 |
| 8 | OWG-9 (resume batch) |
| 9 | DEBT-1, DEBT-2, OBS-1; NB-16 (conditional) |
| Closed | NB-10, NB-14 |

The 4A and 4B close-outs re-target several of these items. `DEFERRED_DEBT.md` is authoritative for current targets.

## 5. External dependencies

### EXT-1: HV-B11, the logged-in user's id, blocks `create_note` in production

| | |
|---|---|
| **What is blocked** | `create_note` is **code-complete (4B) but disabled in production**. `HV_B11_VERIFIED = False` in `writes/pipeline.py`. |
| **Why** | Bullhorn documents NoteEntity auto-creation only when both `commentingPerson` and `personReference` are sent. 4B has no **documented** mechanism to obtain the authenticated user's CorporateUser id for `commentingPerson` (`PHASE4B_HV_VERIFICATION.md` HV-B4, HV-B11). |
| **What unblocks it** | The mechanism must be verified against the Bullhorn reference or against a connected tenant, and the verification recorded in `PHASE4B_HV_VERIFICATION.md`. |
| **Preconditions before setting `HV_B11_VERIFIED=True`** | (1) That verification is recorded. (2) `DEFERRED_DEBT.md` **P4B-8** is closed: comments are scrubbed from the raw error body before redaction. |
| **Owner** | The user or tenant administrator supplies the evidence. Any phase may then flip the flag through a short Architect-approved change with tests. |
| **Impact on later phases** | The OW-13 acceptance test's note-write half (Phase 6) depends on EXT-1. If EXT-1 is still open, it runs with the flag mocked on and is reported as "blocked externally". |
