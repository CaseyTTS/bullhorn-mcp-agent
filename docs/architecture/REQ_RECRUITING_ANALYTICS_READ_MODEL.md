# Requirement: Recruiting Activity & Analytics Read Model

| | |
|---|---|
| **Status** | Recorded 2026-10-06 as a roadmap and specification requirement. On the same day it was reconciled with the operational safe-write requirement and with the user's final, fixed phase order (`ROADMAP.md` revision 3). No phase work has started. |
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

**RA-8 Drill-back.** Every metric keeps the source Bullhorn IDs it was computed from, where applicable: candidate, job, recruiter/owner, client corporation, client contact, submission, interview/appointment, placement, and offer record/status.

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
