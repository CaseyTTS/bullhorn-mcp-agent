# Canonical Recruiting Activity Vocabulary (single source of truth)

| | |
|---|---|
| **Status** | Architect specification, 2026-10-06. Aligned with roadmap revision 3, the final user-fixed phase order. Not yet implemented. |
| **Authority** | This is the **only** place where recruiting activity concepts, their event shape, and the mapping from write operations to activity concepts are defined. |
| **Referenced by** | `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` (RA-9), `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` (OW-5, OW-11), `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` (TS-2 value mappings), and `ROADMAP.md`. Reads, writes, analytics and dashboard consumers must not define these concepts themselves (decision D4). |

**Implementation owners** (`ROADMAP.md`):

| Phase | What it implements |
|---|---|
| 4B | The event model (§1) and `note_created` |
| 5 | Every other concept deriver (§2) |
| 6 | Timeline merging and aggregation over events |
| 4A | The tenant value mappings and business rules that every *config-required* concept depends on (through the 4A value-mapping mechanism); 4B and 5 extend them |

## Conventions

**Bullhorn names (HV-1).** Concepts are product-defined. Any Bullhorn entity, field, status, workflow or association named in this document, beyond what the Phase 3 catalog already verified, is a *candidate representation*.
- Each one must be verified against authoritative Bullhorn reference material or against the connected tenant's metadata before use.
- Anything unverified is marked **unresolved** and surfaced in setup. It is never guessed.

**Terms used below:**

| Term | Meaning |
|---|---|
| **event** | Something that happened at a point in time. |
| **state** | A value computed at read time. |
| **config-required** | No event is emitted until the tenant mapping or business rule exists and is valid. Until then the gap is reported as `definition_missing`, together with the TS-7 missing-setup requirements. |
| **built-in** | A tenant-independent default exists. |

---

## 1. The activity event (one shape for every concept)

| Field | Meaning |
|---|---|
| `activity_id` | A deterministic ID derived from (concept, source entity, source record ID, occurrence discriminator). |
| `concept` | A concept from §2. |
| `occurred_at` | The canonical timestamp for the concept (§2). It is normalized to UTC. The display timezone is the tenant's `reporting_timezone` (4A, SB-4). |
| `state` | For stateful concepts: the normalized state at read time. |
| `source` | `{canonical_entity, id}` of the source record. |
| `links` | `candidate_id`, `job_id`, `recruiter_id`, `owner_id`, `author_id`, `client_corporation_id`, `client_contact_id`, `submission_id`, `appointment_id`, `placement_id`, `note_id`, `offer_ref`. A link that does not apply is `null`. A link that could not be resolved goes in `unresolved_links`. Links are never silently dropped. |
| `attribution` | The business rule that chose `recruiter_id`. |
| `definition` | The profile version and rule ID used. |
| `evidence` | The canonical field(s) and value(s) that matched the rule. |
| `origin` | `observed` (read from Bullhorn) or `written_by_mcp` (also returned by a safe-write result). Both carry the same meaning and the same `activity_id`. |

## 2. Concepts (v1)

| Concept | Kind | Source canonical entity | Timestamp | Rule | Default | Deriver phase |
|---|---|---|---|---|---|---|
| `job_created` | event | `job` | `job.date_added` | One event per job. The recruiter is `job.primary_recruiter_id`, as mapped in 4A. | Dating is built-in. The recruiter mapping is config-required. | 5 |
| `job_status_changed` | event | `job` + status history (to be verified) | status-change time | One event per transition. Reported as unsupported if no history representation is verified. | built-in if history exists | 5 |
| `candidate_status_changed` | event | `candidate` + status history (to be verified) | status-change time | As above. | built-in if history exists | 5 |
| `submission_created` | event | `submission` | `submission.date_added` | Every job submission. This is **not** a client submission. | built-in | 5 |
| `client_submission` | event | `submission` + status history | When the tenant rule first became true | The tenant's "submitted to client" value mapping or rule (TS-2). | config-required | 5 |
| `interview_scheduled` | event | `appointment` | Creation time; the scheduled-for time is `start_at` | Appointments classified as interviews by tenant value mappings. Never inferred from submission statuses. | config-required | 5 |
| `interview_rescheduled` | event | `appointment` + reschedule link or history (to be verified) | Change time | Only where it is verified. Otherwise reported as unsupported. | config-required / possibly unsupported | 5 |
| `interview_completed` | event | `appointment` | Outcome time or `end_at`, per the rule | The tenant state mapping or rule. | config-required | 5 |
| `interview_cancelled` | event | `appointment` | Cancellation time, per the rule | The tenant state mapping or rule. | config-required | 5 |
| `interview_upcoming` | state | `appointment` | read time | An interview whose `start_at` is after now and that is not cancelled. | derived | 5 |
| `offer_extended` / `offer_accepted` / `offer_declined` | event | Per the tenant offer mapping | State-change time (history) | Tenant value mappings (RA-6, TS-2). | config-required | 5 |
| `offer_pending` | state | as above | read time | Extended, and not yet accepted or declined. | derived | 5 |
| `placement_created` | event | `placement` | `placement.date_added` | One event per placement, with an optional status exclusion. | built-in | 5 |
| `note_created` | event | `note` | `note.date_added` | One event per note. `action_type` is the tenant value. `action_semantic` is the mapped tag (§2.1). Links come from the verified associations. | built-in | 4B |

**Adding a concept** requires a vocabulary version bump and a decision recorded in `DEFERRED_DEBT.md` (Decisions).

### 2.1 Note action semantics (optional canonical tags)

Tenant action values are authoritative, and are discovered or confirmed in setup (TS-2, OW-3). The vocabulary defines **optional** semantic tags so that agents can be offered suggestions and analytics can group notes across tenants. The proposed tags are pending user confirmation (Q-W3):

| Tag | Meaning |
|---|---|
| `candidate_screen` | A screening conversation with a candidate. |
| `client_call` | A conversation with a client contact. |
| `interview_feedback` | Feedback recorded about an interview. |
| `follow_up` | A follow-up action. |
| `email` | Email correspondence that was logged. |
| `reference_check` | A reference check. |
| `other` | Explicitly mapped, with no specific semantic meaning. |

How the tags are used:
- A tag is never sent to Bullhorn.
- An unmapped action value is valid and has `action_semantic = null`.
- Tags are used only for suggestions and grouping.

## 3. Write operations → activity concepts

Every write operation produces events with `origin: written_by_mcp`. When the same records are later read from Bullhorn, they produce the same `activity_id`s with `origin: observed`.

| Canonical operation | Produces | Owning phase |
|---|---|---|
| `create_note` | `note_created` | 4B |
| `create_submission` | `submission_created`, plus `client_submission` if the tenant rule matches | 7 |
| `update_submission_status` | `client_submission` and offer events, if the tenant rules match | 7 |
| `create_interview` / `update_interview` | Interview events, per the rules | 7 |
| `associate_candidate_with_job` | `submission_created`, if it is represented as a submission (to be verified) | 7 |
| `update_candidate_status` | `candidate_status_changed` | 7 |
| `update_job_status` | `job_status_changed` | 7 |
| `update_job_priority` | none (attribute change) | 7 |
| `create_candidate` / `update_candidate` | none in v1 | 7 (also used by Phase 8) |
| `create_placement` / `update_placement` | `placement_created` (on create) | 7 |
| `add_to_tearsheet` | none in v1 | 7 |
| `upload_resume` (legacy `upload_candidate_resume`) | none in v1 | migrated in 7; batch use in 8 |
| `update_note` | none in v1; only if Bullhorn permits (to be verified) | 7 (optional) |
