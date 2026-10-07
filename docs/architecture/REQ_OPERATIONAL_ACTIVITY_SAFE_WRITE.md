# Requirement: Operational Activity & Safe Write Layer

| | |
|---|---|
| **Status** | Recorded on 2026-10-06 as a roadmap and specification requirement. It was reconciled with the final user-fixed phase order (`ROADMAP.md` revision 3) the same day. No phase work has started. |
| **Source** | The user's second requirement, relayed by the coordinator on 2026-10-06. The final roadmap injection refines it in three places: the `create_note` signature, the pipeline order and the compact tool surface. §1 transcribes both faithfully. §2 onward is Architect analysis. |
| **Shared model** | `CANONICAL_ACTIVITY_VOCABULARY.md` (OW-5, OW-11). It is referenced here, not redefined. |
| **Related** | `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md`, which covers action-type value mappings, the configuration approval/audit primitives and HV-1. `REQ_RECRUITING_ANALYTICS_READ_MODEL.md`. |
| **Ownership** | `ROADMAP.md` §1 |

**HV-1 applies.** Every Bullhorn representation named in this document must be **verified against the Bullhorn reference or the tenant's metadata** before use. If it cannot be verified, it is marked unresolved, surfaced in setup, and never guessed. This covers:

- the Note entity's fields;
- the association mechanics, for example `personReference`, `jobOrder`, `clientContacts`, `placements`, `jobSubmissions`, or a separate note-to-entity link entity;
- whether multi-entity association is allowed;
- the source of the action-type list, for example an `action` picklist in meta or a settings value;
- the Note update and delete policy.

Phase 3 verified only these Note names: `id`, `dateAdded`, `action`, `comments`, `personReference.id`, `commentingPerson.id`, `jobOrder.id` and `isDeleted`.

---

## 1. The requirement (faithful transcription, structured)

**OW-0 — Scope.** The MCP must support controlled recruiter-operational **writes**, not only reads and analytics. The first mandatory write beyond resume upload is **Bullhorn Note creation and retrieval**. It must come with proper entity associations, action-type validation, permissions, dry-run, approval and audit.

**OW-1 — Notes are first-class.** An agent instruction can create a note against any supported entity. Example intents:

- "Add a note to Job 4370."
- "Add this note to candidate 12345."
- "Add a Client Call note to this job."
- "Document this interview feedback against the candidate and job."
- "Add a Candidate Screen note to this candidate."

The canonical capability is `create_note(target_type, target_id, action_type, comments, associations=None)`. The second requirement's sketch had `additional_associations=None, dry_run=True`; the final injection uses `associations=None`. The Architect may refine the exact external schema.

**OW-2 — Associations.**

- Supported targets, where Bullhorn allows them: Candidate, JobOrder, ClientContact, ClientCorporation, Placement and JobSubmission.
- The Note **and** its associations must both be created. An orphan Note is a failure.
- Multi-record association is used where it is permitted. For example, one interview-feedback note is linked to Candidate 12345, Job 4370 and Client Contact 678, rather than written as three duplicate notes.

**OW-3 — Note action types are tenant configuration.**

- Action types are discovered or mapped. They are never hardcoded and never guessed. Values such as Candidate Screen, Client Call, Interview, Follow Up, Email and Reference Check are examples only.
- An invalid action type is rejected. The rejection returns the valid tenant values or the mapped alternatives. The system never substitutes one silently.

**OW-4 — Note and activity reads.**

- Notes can be read and filtered by target entity, candidate, job, client, contact, placement, submission, action type, author, and date or date range.
- Example prompts:
  - "Show me the last five notes on this candidate."
  - "What happened on Job 4370 this week?"
  - "Summarize activity on this account."
  - "Show all Client Call notes from September."
  - "Show all notes John added to this job."

**OW-5 — Normalized activity timeline.** The timeline covers note created, client submission, interview scheduled / rescheduled / completed / cancelled, offer extended / accepted / declined, placement created, candidate status change and job status change. Front ends must never merge raw entities themselves.

**OW-6 — Safe-write framework.** The pipeline order is the one given in the final injection:

```
intent → canonical operation → target resolution → validation → permission → dry-run/preview → approval policy → Bullhorn write → audit → normalized result
```

- Arbitrary raw Bullhorn writes are never accepted.
- Every write returns:
  - the operation;
  - the Bullhorn entity type;
  - the created or updated record ID;
  - the associated records;
  - an audit or correlation ID;
  - warnings and partial failures.

**OW-7 — Anticipated writes.**

- The operations: Candidate create/update; JobSubmission create/update; submission status update; interview/appointment create/update; Candidate status update; Job status update; Job priority update; Placement create/update; Candidate↔Job association; resume upload; Note creation; and Note update if Bullhorn permits it.
- Each operation has its **own** permission and policy. There is never one broad "write" permission.

**OW-8 — Idempotency and duplicate prevention.** This covers duplicate notes, duplicate JobSubmissions, duplicate interviews, repeated resume uploads and repeated status updates. Protection uses keys, duplicate checks or pre-write verification, wherever feasible.

**OW-9 — Read-before-write.** Before a consequential write, the target is resolved and identifying information is returned, for example "Job 4370 / <job title> / <client corporation> / Primary recruiter: <name>". A write is never based solely on an ID supplied by the LLM. This step is configurable for trusted automation.

**OW-10 — Audit.** Each audit record captures:

- the user or agent;
- the operation;
- the entity and entity ID;
- the before/after state or the proposed payload;
- the approval state;
- the timestamp;
- the Bullhorn response;
- success or failure.

Sensitive content follows the redaction rules.

**OW-11 — One canonical model** shared by reads, writes, analytics and dashboards.

**OW-12 — Roadmap ownership** of these 10 items: setup/mapping, Note action discovery, expanded reads, timeline normalization, Note creation, safe-write infrastructure, write-specific permissions, idempotency, metrics, and dashboard consumption.

**OW-13 — Acceptance scenario.** "Pull Job 4370, show me the recent candidate activity, summarize what has happened, then add a Client Call note saying the manager wants two more candidates by Friday." The MCP must:

1. resolve the job;
2. retrieve the activity;
3. validate the action type;
4. construct the Note and its associations;
5. enforce permissions and approval;
6. preview it;
7. write it;
8. audit it;
9. return the Note ID.

**CT-1 (from the final injection) — Compact tool surface.** The public tools are parameterized, for example `get_notes(filters…)` and `create_note(…)`, rather than one tool per filter or entity.

---

## 2. Architect analysis: the Phase 1 cross-cutting layer as built, and its gaps

### 2.1 What exists

| Component | As built | Evidence |
|---|---|---|
| Permissions | `check(tool, operation="read")` always allows and never requires approval. Its signature is deliberately stable. | `src/bullhorn_mcp/crosscutting/permissions.py:12-27` |
| Approval | An in-memory `ApprovalGate`: `create_pending`, `confirm`, `reject`, `get`. It has no producers today. | `crosscutting/approval.py:13-64` |
| Dry-run | `render_preview(tool, would_execute)` | `crosscutting/dryrun.py:4-19` |
| Audit | `log_invocation(tool, args, result_summary, duration_ms, success)`, with key-name redaction. | `crosscutting/audit.py:13-81` |
| The single write | `upload_candidate_resume` hand-codes permission → dry-run → approval → upload → audit. Its defaults are frozen: `dry_run=False`. | `src/bullhorn_mcp/tools/candidates.py:189-319` |
| Client writes | None, apart from the resume upload. | Architecture plan §1 |

### 2.2 Gaps

The gaps are tracked as OWG items in `DEFERRED_DEBT.md`, each with its owning phase.

| Gap | Needed | Phase |
|---|---|---|
| No shared pipeline (OWG-6) | A minimal pipeline for `create_note`, then a generalized `SafeWritePipeline` | 4B (minimal), 7 (general) |
| No canonical operations | An operation catalog (vocabulary §3) | 4B (`create_note`), 7 (all others) |
| No per-operation scopes, actor or policy (OWG-2, OWG-10) | Scopes such as `note.create` on a real policy check, plus actor identity | Actor: 4A. `note.create`: 4B. All other scopes and `raw_query`: 7 |
| No read-before-write | Target resolution with identity cards | 4B, generalized in 7 |
| No idempotency (OWG-9) | A key, a ledger and duplicate probes | 4B (notes), 7 (general), 8 (batch) |
| Approval is unbound (OWG-3) | Tokens bound to the preview or diff hash, the actor and an expiry; confirmation executes the bound operation | 4A (configuration changes) and 4B (writes), sharing one primitive; persistence per Q-W6 |
| Dry-run is generic (OWG-4) | The preview equals the committed object, identified by a preview hash | 4B, generalized in 7 |
| Audit is shallow (OWG-5) | Actor, correlation ID, payloads, approval state, Bullhorn response, free-text redaction (Q-W7) | 4A (configuration), 4B (writes), 7 (general) |
| No create or association primitives (OWG-7) | Note create plus the verified association mechanics | 4B; generic create/update in 7 |
| Legacy upload sits outside the pipeline (OWG-11) | Migrate it, keeping byte-identical defaults | 7 |

## 3. The safe-write pipeline (binding)

### 3.1 Stages (final order)

The minimal version is implemented in **4B**. Phase **7** generalizes it.

1. **Intent → canonical operation.** The pipeline accepts canonical arguments only; raw Bullhorn names are never accepted.
2. **Target resolution (read-before-write).** Every referenced record is fetched, and an identity card is produced for it. A missing or ambiguous target stops the pipeline. Policy per operation: `target_confirmation` is `required`, `preview_only` (the default) or `off`. `off` is for trusted automation, is configured explicitly, and is audited.
3. **Validation.** Covers the schema, the business values (for example the action type against the 4A/4B value mappings, with rejection and alternatives) and the verified association mechanics. Errors are aggregated.
4. **Permission.** The per-operation scope is checked for the actor. The result is allow, deny, or allow-with-approval.
5. **Idempotency / duplicate check.** The pipeline checks the key and runs any operation-specific probe. On a hit it returns `duplicate` together with the original result.
6. **Dry-run / preview.** The preview contains the exact planned payloads, the association plan, the identity cards and the idempotency verdict, and it carries a preview hash. The `create_note` default is open question Q-W2.
7. **Approval policy.** If approval is required, the pipeline returns a token bound to the preview hash, the actor and an expiry. Confirming the token executes exactly the previewed operation.
8. **Bullhorn write.** The primary record is written first, then its associations. If an association fails, the pipeline either compensates or returns an explicit `partially_committed` result. No orphan is ever left silently. The write is then read back to verify it.
9. **Audit.** An audit record is written at each transition (preview, approval requested or decided, commit, failure). All records share one correlation ID.
10. **Normalized result** (§3.2).

The idempotency check (stage 5) sits between permission and dry-run. It was implied by OW-8, and the final injection's pipeline does not exclude it.

### 3.2 Normalized write result

The result object has these fields:

- `operation`: the operation and its version.
- `status`: one of `previewed`, `pending_approval`, `committed`, `partially_committed`, `rejected_validation`, `denied`, `duplicate`, `failed`.
- `entity_type`: the canonical entity type, plus the Bullhorn type for diagnostics.
- `record_id`
- `associations`: the associated records, each with its own status.
- `targets`: the identity cards.
- `activity`: the vocabulary events produced, with `origin: written_by_mcp`.
- `correlation_id`
- `approval_token`: present only when the status is `pending_approval`.
- `warnings` and `errors`: bounded in size.

### 3.3 Legacy write

`upload_candidate_resume` is the one documented exception (decision D5). It moves onto the pipeline in Phase 7, and its default behavior must stay byte-identical (Q-W8).

## 4. Notes

### 4.1 Action types

- **Discovery** happens in 4A/4B. It uses the 4A value-mapping mechanism and the DEBT-3 picklist enrichment.
  - The tenant values are discovered or entered manually, and confirmed by an administrator.
  - Each value can carry an optional semantic tag (vocabulary §2.1).
  - The values are stored as profile value mappings.
- **Enforcement** happens in 4B.
  - The value matches if it equals an enabled tenant value exactly; a matching display label resolves to its value.
  - Anything else is rejected with `rejected_validation`. The rejection lists the valid values and, where the input matches a tag, the mapped suggestions.
  - The system never substitutes a value automatically.

### 4.2 Associations (4B, verified under HV-1)

- **Supported targets**: candidate, job, client contact, client corporation, placement and submission.
- **Multiple associations** are made where Bullhorn permits them. Otherwise the call fails with an explicit error; the fallback is open question Q-W4.
- **Orphan check**: after writing, the pipeline reads the note back to confirm no orphan was created.

### 4.3 Reads (4B)

- `get_notes(filters…)` filters on: target or entity, candidate, job, client, contact, placement, submission, action type, author (SB-7 identity resolution) and date range (SB-5 semantics). It supports ordering and limits for "last N" requests.
- The tool pages results itself, using interim markers until the Phase 5 pagination work (SB-1).
- Soft-deleted notes are handled explicitly (SB-6, note part).

### 4.4 Timeline (Phase 6)

`note_created` events are included in `get_activity_timeline`.

## 5. Idempotency

The 4B framework covers notes. Phase 7 generalizes it, and Phase 8 applies it to batches.

- **The key.** The caller may supply a key. Otherwise the pipeline derives one from the operation, the targets, a normalized payload hash and the actor, within a configurable time window.
  - The same key with the same payload returns the original result.
  - The same key with a different payload is rejected.
- **Probes.** These are best-effort, and their feasibility is to be verified.

  | Operation | Probe |
  |---|---|
  | `create_note` | A note with the same targets, action, author and comments exists within the window. |
  | `create_submission` | An existing candidate–job submission. |
  | `create_interview` | The same candidate, job and start time. |
  | Status updates | Already at the target value: a no-op, which is reported. |
  | Resume | The same file hash is already attached. |

- **Ledger persistence** is open question Q-W5.

## 6. Write inventory, scopes and phases

Every operation has its own scope (OW-7). The scope names are proposals, to be fixed in the 4B and 7 work packages.

| Operation | Proposed scope | Phase |
|---|---|---|
| `create_note` | `note.create` | 4B |
| `create_submission` / `update_submission` | `submission.create` / `submission.update` | 7 |
| `update_submission_status` | `submission.status.update` | 7 |
| `create_interview` / `update_interview` | `interview.create` / `interview.update` | 7 |
| `associate_candidate_with_job` | `candidate_job.associate` | 7 |
| `update_candidate_status` | `candidate.status.update` | 7 |
| `update_job_status` | `job.status.update` | 7 |
| `update_job_priority` | `job.priority.update` | 7 |
| `create_candidate` / `update_candidate` | `candidate.create` / `candidate.update` | 7 |
| `create_placement` / `update_placement` | `placement.create` / `placement.update` | 7 |
| `add_to_tearsheet` | `tearsheet.member.add` | 7 |
| `update_note` (only if Bullhorn permits) | `note.update` | 7 (optional) |
| `upload_resume` | `candidate.file.upload` | legacy tool migrated in 7; batch use in 8 |

## 7. The OW-13 acceptance scenario, decomposed

| Step | Capability | Phase |
|---|---|---|
| Resolve Job 4370 | `get_job` (existing) and the identity card | today / 4B |
| Recent candidate activity on the job | `get_notes` (4B) is enough for notes; `get_activity_timeline` (6) gives the full picture | 4B / 6 |
| Summarize | The front-end agent's own reasoning | agent |
| Validate "Client Call" | Tenant action values | 4A mechanism, 4B enforcement |
| Write the note: associations, permission, approval, preview, write, audit, return the ID | `create_note` on the minimal pipeline | 4B |

**Acceptance tests.** The note-write half is acceptance-tested at the end of 4B. The full scenario, including the cross-entity timeline, is acceptance-tested in Phase 6.

## 8. Open questions for the user

| ID | Question |
|---|---|
| Q-W1 | Default posture for new write scopes. The recommendation is denied-until-enabled, except `note.create`, which would be allowed with preview on. |
| Q-W2 | Should `create_note` default to `dry_run=True`, or to preview-then-confirm? |
| Q-W3 | Do you accept the proposed note semantic tags (vocabulary §2.1)? |
| Q-W4 | If multi-association is not permitted, should the request be rejected, or should the MCP offer one note per target with explicit approval? |
| Q-W5 | Is an in-memory idempotency ledger acceptable, or must it be persistent? |
| Q-W6 | Should pending approvals (for writes and configuration changes) survive a restart? |
| Q-W7 | How should note `comments` appear in audit records: verbatim, as a hash plus length, or truncated? |
| Q-W8 | Is it acceptable for the legacy `upload_candidate_resume` to stay outside the pipeline until Phase 7? |
| Q-W9 | Where does actor identity come from: a static deployment identity, a per-call parameter, or the MCP client session? How should a name like "John" resolve to a Bullhorn user? |
