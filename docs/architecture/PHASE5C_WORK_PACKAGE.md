# Phase 5C Work Package: Expanded Recruiting Reads, Streamlining and Tier Gating

| | |
|---|---|
| **Status** | **COMPLETE** (Round 2 re-reviews PASS by both reviewers, 2026-10-07). Binding Architect specification, 2026-10-07. **Amendments C1–C5, the "5C Review Triage" and "5C Review Triage — Round 2"** (at the end of this document) are binding. Where an amendment or a triage disagrees with the base text, the later text wins. C2 supersedes C1-2. C3 records the HV outcomes and pre-approves every known deviation. C4 ratifies two build-time deviations. C5 ratifies two fix-round details. The scope is frozen under D-5-25. |
| **Governing decisions** | `PHASE5_PROPOSAL.md` §0: D-5-2, D-5-5, D-5-11, D-5-14, D-5-15, D-5-19, D-5-20, D-5-21, D-5-22, D-5-23, D-5-24, D-5-25. `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` (RA-2..RA-10, §5 drill-back, §8 two-tier). `CANONICAL_ACTIVITY_VOCABULARY.md` (single concept source, D4). HV-1 (D6). CT-1 (D7). |
| **Baseline** | 5A HEAD. 5A must be green (both reviews PASS, gates green) before 5C starts. 5C builds on the 5A identity/session API as specified in `PHASE5A_WORK_PACKAGE.md`, including Amendments A1–A4 and the 5A Review Triage. In particular, the tenant is selected per request by A3-1. |
| **Tool count** | 20 → **22** (`find_records`, `get_activity`). No other tool is added or changed. |
| **Reviewers** | Independent Reviewer **and** Security & Identity Reviewer, both required (§10). |
| **Do not begin Phase 6** | No metrics, no aggregation, no grouping, no timeline merge, and no Tier 2 data path. |

---

## 0. Ground rules (apply to every item below)

| ID | Rule |
|---|---|
| G-1 | **HV-1.** Every Bullhorn behaviour that 5C uses must be verified against https://bullhorn.github.io/rest-api-docs/ (including the Entity Reference) or against the connected tenant, and recorded in `PHASE5C_HV_VERIFICATION.md` (§5). This covers every entity, field, operator, query syntax, escaping rule, paging/limit, ordering, date representation, soft-delete behaviour, association and rate-limit behaviour. If an item is unresolved, the capability that depends on it returns a structured `unsupported_*` response (§3.4) and makes **zero** Bullhorn calls for that capability. Nothing is approximated. |
| G-2 | **Business meaning comes only from tenant configuration.** `client_submission`, interview classification and state, offer states and job priority are derived **only** from active, valid tenant value mappings and settings (§2). A missing, invalid or drift-affected definition returns `definition_missing` / `setup_revalidation_required`. Nothing is guessed. |
| G-3 | **No raw query text from model input.** Neither tool accepts Lucene, JPQL, raw Bullhorn entity names or raw Bullhorn field names. Filters are a structured AST over **canonical** names, validated against allowlists and rendered by one builder (§3). |
| G-4 | **Reads run as the caller.** Every Bullhorn request goes through `server.get_client()`, which in shared mode delegates to the 5A `identity.sessions.resolve_client()`. The service session is used **only** when the caller's `access_tier` is `service` (D-5-11, Amendment A2-3). No code path in 5C resolves a service session for a `bullhorn_user`, `workspace_only` or `local` caller. That includes "enrichment" lookups, such as CorporateUser names. |
| G-5 | **Tier gating (D-5-24, REQ §8 TT-6).** `find_records` and `get_activity` are record-level tools. They are **not** added to the Amendment A2 Tier 2 allowlist, so the A2 deny-by-default gate denies them to a `workspace_only` caller before any argument parsing or Bullhorn call. 5C adds no Tier 2 code. |
| G-6 | **Compact surface (D-5-15, D-5-21).** Internally rich, externally two tools. Legacy tools are unchanged (D-5-14). |
| G-7 | **Privacy (D-5-22).** The repo contains no tenant-specific values, mappings, field names, workflow values or captured payloads. Fixtures are synthetic and modelled only on documented shapes. |

## 1. Decisions (D-5C-n)

| ID | Decision |
|---|---|
| D-5C-1 | **One sub-phase, built in order.** The Builder delivers these steps within a single review cycle:<br>(i) the HV verification doc;<br>(ii) the query-support resource, query builder, `EntityReader` transport, paging/limits/retries and cursor;<br>(iii) catalog growth and debt items;<br>(iv) the concept derivers;<br>(v) the two tools.<br>If step (i) leaves a capability unresolved, the later steps implement that capability's `unsupported_*` guard, not the capability itself. |
| D-5C-2 | **New read transport; `client.py` untouched.** `bullhorn/reads.py::EntityReader(client)` follows the 4B `EntityWriter` pattern. It uses the passed client's `auth.session` (`rest_url`, `BhRestToken`) and issues only `GET` requests built from builder output. In shared mode the client is the per-call 5A client, so the 401 refresh path is the 5A session refresh. There is no fallback to any other credential. |
| D-5C-3 | **HV-verified query-support resource.** A new packaged resource, `mappings/bullhorn_query_support.yaml` (version 1), loaded via `importlib.resources`, is the **only** place where 5C records Bullhorn query mechanics. Per entity it records:<br>• the operation (`query` or `search`);<br>• the filterable raw fields and their allowed operators;<br>• the sortable fields and the direction syntax;<br>• the maximum page size;<br>• the soft-delete field, or "not soft-deletable";<br>• the HV ID that verified each entry.<br>An entry without an HV ID fails to load. The Bullhorn names of the history entities that derivers use (RAG-5, OWG-8) also live here, under `history_sources`, each with its HV ID. |
| D-5C-4 | **NB-2 settled: strict mode.** The query builder resolves canonical names strictly. An unknown name, including a snake_case typo such as `frist_name`, returns `rejected_validation` / `unknown_field`. It offers up to 3 suggestions, drawn **only** from that entity's allowed canonical field names (`difflib`, cutoff 0.75). The Phase 3 translator is not modified. |
| D-5C-5 | **Field resolution order (never guessed).** A canonical `(entity, field)` resolves as follows:<br>(1) the active tenant profile's mapping record, if its state is `valid`; else<br>(2) the packaged default mapping, **only if** its raw source is present in the tenant's latest discovery snapshot; else<br>`definition_missing` with `mapping:<entity>.<field>`.<br>Template targets (for example `full_name`) are output-only; they are never filterable or sortable (`unsupported_filter`). A field whose raw source matches `sensitive_field_patterns` is never filtered, sorted or returned (`restricted_field`). |
| D-5C-6 | **Setup-state gate.**<br>• Plain record reads use the existing 4A capability `canonical_reads`. It is allowed in `setup_valid` and in `setup_revalidation_required`, where a warning is added.<br>• Anything that depends on a concept mapping, an ordering or a 5C setting requires `setup_valid`, and otherwise returns `setup_revalidation_required`. That covers the `concept` parameter, priority filters, and every `get_activity` concept except `note_created`.<br>• With no active profile and no discovery snapshot, the result is `setup_required`. |
| D-5C-7 | **`rest_url` binding (closes the P4A-5 remainder).** Before any read, the active profile's recorded `rest_url_fingerprint`, if set, must equal the fingerprint of the caller's session `rest_url`. A mismatch returns `setup_revalidation_required`, with zero record reads. |
| D-5C-8 | **Inactive vs deleted (SB-6).** Soft-deleted records are excluded by default (`include_deleted=false`). The exclusion uses the verified soft-delete field in the query **and** a defensive post-filter.<br>`include_deleted=true` is honoured only for entities whose soft-delete semantics are verified; otherwise it returns `unsupported_filter`. An entity whose soft-delete semantics are unresolved is `unsupported_entity` (reason `deleted_semantics_unresolved`).<br>"Inactive" (for example a closed job, or a disabled user) is an ordinary canonical field. It is never filtered implicitly. |
| D-5C-9 | **Timezone and dates (RAG-8 remainder, P4A-11).**<br>• `tzdata` is added as a runtime dependency.<br>• `notes/reads.py::parse_bound` moves to `tenant/timeutil.py`, the single source. `notes/reads.py` re-imports it, and its behaviour is byte-identical.<br>• Ranges are half-open, `[from, to)`.<br>• A date-only bound is local midnight in the tenant's `reporting_timezone`, using `fold=0` for ambiguous or nonexistent times.<br>• A datetime bound must carry an offset or `Z`. Naive datetimes are rejected.<br>• Output timestamps are UTC ISO-8601 with `Z`. Events also carry `occurred_at_local` in the reporting timezone. The response states the `reporting_timezone`. |
| D-5C-10 | **Paging and complete results (SB-1, SB-2).**<br>• Keyset pagination (`id` ascending, `id > last_id`) is used when HV verifies, for that entity, `id` range filtering and ascending order.<br>• Otherwise offset pagination is used, with `consistency: "offset"` and a warning.<br>• Each tool call fetches at most `limit + 1` records, across at most 5 Bullhorn page requests.<br>• `complete` is true only when the result set is provably exhausted.<br>• Truncation is always explicit (`truncated`, `next_cursor`). |
| D-5C-11 | **Cursor.** An opaque `base64url(payload).base64url(HMAC-SHA256)`.<br>• **Payload:** version; `sha256(tenant_key)`; `sha256(principal_key)`; the hash of the normalized request (excluding `cursor`); the active profile version; the paging mode and position; `issued_at`.<br>• **Expiry:** 1 hour.<br>• **Key:** derived by HKDF-SHA256 from the 5A active session key, with `info="bullhorn-mcp/cursor/v1"`. Local mode uses a random per-process key.<br>• **Rejection:** any mismatch, tampering or expiry → `rejected_validation` / `invalid_cursor`, with zero Bullhorn calls.<br>• The payload never contains query text, raw field names, or any record data other than the last-seen ID. |
| D-5C-12 | **Retries and rate limits (SB-3).** Reads only; no write path is touched.<br>• `GET` responses 429 and 503 are retried at most 2 times (3 attempts), with exponential backoff and full jitter (base 0.5 s, cap 8 s).<br>• `Retry-After` is honoured only if HV-Q7 verifies its form, and only up to 30 s.<br>• 500, 502 and 504 are retried at most once.<br>• The total wall-clock budget per tool call is 30 s. When it is exceeded, the call returns `error` / `rate_limited` and discards partial results.<br>• At most 2 Bullhorn requests are in flight per `(tenant_key, principal_key)`. |
| D-5C-13 | **Provenance (SB-12, RA-8, REQ §5; Tier 1 only).**<br>• Every record carries `source: {system: "bullhorn", entity: <canonical entity>, id}`.<br>• Every event carries the vocabulary §1 fields `source`, `links`, `attribution`, `definition` and `evidence`.<br>• The response carries `provenance: {profile_version, catalog_fingerprint, query_support_version, vocabulary_version, request_hash, retrieved_at}`.<br>**Never** returned: `rest_url`; the corp token; `BhRestToken`; raw Bullhorn field or entity names; the rendered query string; and other callers' identifiers beyond the Bullhorn record links themselves. |
| D-5C-14 | **Attribution defaults (product-defined, canonical-level).**<br>• `job_created` → `job.primary_recruiter_id` (config-required).<br>• `submission_created` and `client_submission` → `submission.sending_user_id`.<br>• Interview concepts → `appointment.owner_id`.<br>• Offer concepts → the attribution of the entity that carries the offer mapping: `sending_user_id` for a submission; `placement.recruiter_id` for a placement, only if it is mapped.<br>• `placement_created` → `placement.recruiter_id` if it is mapped; otherwise `recruiter_id: null`, listed in `unresolved_links`.<br>`attribution` names the rule ID (`attr.<concept>.v1`). Tenant-specific attribution overrides (Q-8) are Phase 6. A `recruiter_id` filter on a concept whose attribution field is unresolved returns `unsupported_filter` for that concept. |
| D-5C-15 | **Two new tenant settings.** They are added to the profile v2 `settings` and set through the existing `set_setting` op.<br>• `client_submission_dating ∈ {status_history, submission_date_added}` answers Q-2 per tenant.<br>• `interview_completion_rule ∈ {mapped_state_only, end_passed_not_cancelled}` answers Q-4 per tenant.<br>**There is no default.** While a setting is unset, the concepts that depend on it return `definition_missing` with `setting:<name>`. `status_history` is accepted only if HV-Q10 verifies the history source. |
| D-5C-16 | **`user` binding (completes RAG-1 / NB-4; SB-7).** If HV-Q8 verifies the CorporateUser fields and its read operation, `bullhorn_standard_fields.yaml` gains a `CorporateUser` section bound to canonical `user`, with default mappings only for verified names. `find_records(entity="user")` then becomes the recruiter-identity lookup. If HV-Q8 is unresolved, `user` is `unsupported_entity` and recruiter IDs remain plain IDs. |
| D-5C-17 | **Debt dispositions in 5C:** see §7. The Architect updates `DEFERRED_DEBT.md` at close. |

## 2. Business concepts from tenant configuration (fail-closed)

**Mapping mechanism.** All concept mappings use the existing 4A `value_mappings`, with `target: {kind: concept, name}` on `(entity, bullhorn_field, value)`. Priority uses `target: {kind: ordering}`.

**Availability rule.** A concept is **available** only when every part of its requirement is met. Otherwise the concept returns the listed failure and nothing is derived.

| Concept / feature | Requirement (all must hold) | When missing / invalid | When the Bullhorn mechanism is unresolved |
|---|---|---|---|
| `submission_created` | `canonical_reads`; the submission entity is supported | `setup_required` | `unsupported_concept` |
| `client_submission` | • at least one active, valid concept mapping `→ client_submission` on a `submission` field<br>• setting `client_submission_dating`<br>• `setup_valid` | `definition_missing` (`value_mapping:submission:client_submission`, `setting:client_submission_dating`) | `status_history` dating with HV-Q10 unresolved → `unsupported_concept` (reason `status_history_unresolved`) |
| `interview_scheduled` (instances) | • at least one active, valid concept mapping `→ interview_scheduled` on an `appointment` field (the classification)<br>• `setup_valid` | `definition_missing` | `unsupported_concept` |
| `interview_completed` / `interview_cancelled` | • the classification above<br>• a mapping `→ interview_completed` / `→ interview_cancelled` on an `appointment` field<br>• for `interview_completed` only: `interview_completion_rule` set; `end_passed_not_cancelled` also requires the cancelled mapping | `definition_missing` | `unsupported_concept` |
| `interview_upcoming` (state) | The classification **and** the cancelled mapping | `definition_missing` (`value_mapping:appointment:interview_cancelled`) | `unsupported_concept` |
| `interview_rescheduled` | A verified reschedule representation (HV-Q9) | — | `unsupported_concept` (expected) |
| `offer_extended` / `offer_accepted` / `offer_declined` | • at least one mapping `→` that concept on a `submission` or `placement` field<br>• dating per the `client_submission_dating` rules, applied to that entity | `definition_missing` | `unsupported_concept` |
| `offer_pending` (state) | The `offer_extended` mapping, plus at least one of accepted / declined | `definition_missing` | `unsupported_concept` |
| `job_created` | A valid `job.primary_recruiter_id` mapping (the `jobs.primary_recruiter` capability) | `definition_missing` (`mapping:job.primary_recruiter_id`) | — |
| `placement_created` | `canonical_reads`; placement is supported | `setup_required` | `unsupported_concept` |
| `job_status_changed` / `candidate_status_changed` | A verified history source (HV-Q10) | — | `unsupported_concept` (reason `status_history_unresolved`) |
| `note_created` | As for `get_notes` (4B): the verified scopes `job` and `placement` only | As for `get_notes` | Other scopes → `unsupported_filter` |
| Priority filter / `priority_rank` output | The `jobs.priority` capability (field mapping plus ordering) | `definition_missing` (`mapping:job.priority`, `value_mapping:job.priority:ordering`) | — |

**Concept-state rule.**
- Interviews are derived **only** from appointments that the tenant mapping classifies as interviews (RA-3). A submission status never produces an interview.
- Each appointment record is one instance.
- The server never expands recurring or series appointments. If HV-Q9 shows that Bullhorn returns a series as one record, events carry the warning `recurrence_not_expanded`.

## 3. Tool contracts

### 3.1 `find_records` (tool 21)

```
find_records(
  entity: str,                          # one of FIND_ENTITIES (below)
  filters: list[Filter] = [],           # 0..10, AND-combined
  concept: str | None = None,           # optional: restrict to records whose CURRENT value matches a tenant concept mapping
  fields: list[str] | None = None,      # canonical field names, 1..30; default = DEFAULT_FIELDS[entity]
  sort: Sort | None = None,             # {field, direction: "asc"|"desc"}
  include_deleted: bool = False,
  limit: int = 25,                      # 1..100
  cursor: str | None = None,
) -> dict
Filter = {field: str, op: str, value?: ...}
```

**Entities.**
- `FIND_ENTITIES = {candidate, job, submission, placement, client_corporation, client_contact, appointment, user}`.
- Each entity is available only if its query-support entry is verified; otherwise `unsupported_entity`.
- `note`, `tearsheet` and `candidate_reference` are **not** `find_records` entities in 5C. Notes stay on `get_notes`, and the other two are outside D-5-19.

**`concept`.**
- It must be a concept whose source entity equals `entity` and whose kind is mapping-based (`client_submission`, `interview_*`, `offer_*`). Otherwise the result is `rejected_validation` / `invalid_concept`.
- It is a **current-state** filter, rendered as `field IN (mapped values)`. It is not an event query.

**Operator allowlist by canonical type.** The query-support entry for the resolved raw field may restrict it further.

| Canonical type | Operators | Value |
|---|---|---|
| `id`, `reference` | `eq`, `in` | An `int` from 1 to 2^63−1 (exact `type() is int`); `in` takes 1–50 values |
| `string` | `eq`, `in`, `is_null`; `starts_with` only if HV-Q2/Q3 verify prefix semantics and escaping | A `str` of 1–200 characters, with no control characters, NFC-normalized; `in` takes 1–50 values |
| `boolean` | `eq` | An exact `bool` |
| `integer`, `number` | `eq`, `in`, `gt`, `gte`, `lt`, `lte` | An exact `int` or a finite `float` |
| `datetime`, `date` | `gte`, `lt`, `is_null` | An ISO-8601 bound, per D-5C-9 |
| `text`, `list` | none (`unsupported_filter`) | — |

**Priority.**
- `job.priority` filter values must be among the tenant's ordering-mapped values. Otherwise the result is `invalid_value`, listing the allowed values.
- The output adds `priority_rank`.
- Sorting by `priority` is `unsupported_sort` in 5C. Rank-ordered grouping is Phase 6.

**Text fields.** They are returned only when requested explicitly. Each is bounded to 2,000 characters and returned as `{text, truncated, length}`.

**Sort.**
- With no sort requested, results are paged by `id` ascending (keyset or offset; D-5C-10).
- Any other sort requires a verified sortable field and direction syntax (HV-Q4). Otherwise `unsupported_sort`.

**Result:** `{status, entity, records: [{...canonical fields, source}], count, truncated, complete, next_cursor, consistency, reporting_timezone, provenance, warnings[≤50]}`.

### 3.2 `get_activity` (tool 22)

```
get_activity(
  concepts: list[str],                  # 1..8 concept IDs from activity_concepts.yaml
  scope_type: str | None = None,        # candidate|job|client_corporation|client_contact|placement|submission
  scope_id: int | None = None,          # required iff scope_type
  recruiter_id: int | None = None,      # canonical user id; filters on the concept's attribution field (D-5C-14)
  date_from: str | None = None,         # D-5C-9 bounds; filters on the concept's occurred_at (state concepts: their defining timestamp)
  date_to: str | None = None,
  limit: int = 50,                      # 1..200, per concept
  cursor: str | None = None,
) -> dict
```

**Required inputs.** At least one of the following is required:
- `scope_type` together with `scope_id`; or
- both `date_from` and `date_to`.

An unscoped range may not exceed 366 days (`rejected_validation` / `range_too_wide`).

**Per-concept evaluation.**
- Each concept is evaluated independently and returns its own block: `{status: ok|unsupported|definition_missing|setup_revalidation_required, events[], truncated, complete, unsupported[], missing_requirements[]}`.
- One concept failing never widens or alters another.
- If a concept cannot honour a filter, **that concept** is `unsupported`. The filter is never silently dropped.

**Events.**
- Events use the vocabulary §1 shape through the existing `activity/events.py` (`make_activity_id`, `ActivityEvent`), with `origin: "observed"`.
- Within a concept, events are ordered by source `id` ascending. There is no cross-concept merge: the timeline is Phase 6 (D-5-5).

**`note_created`** delegates to the verified scopes of `notes/reads.py`. There is no second note-reading path.

**Result:** `{status, concepts: {<id>: block}, next_cursor, reporting_timezone, provenance, warnings[≤50]}`.

### 3.3 Query builder (`schema/query_builder.py`) and renderer (`bullhorn/query_syntax.py`)

1. **Validate** the request. Types must be exact. Unknown keys in a `Filter` or `Sort` object are rejected. Bounds are as in §3.1 / §3.2.
2. **Resolve** each canonical field to a raw target from the profile or catalog, per D-5C-5. A nested `{field, key}` target renders as the verified association path (HV-Q13).
3. **Check** the raw field and operator against the query-support entry. Otherwise `unsupported_filter` / `unsupported_operator`.
4. **Render** to the verified syntax of that entity's operation.
   - Model-supplied values appear **only** as literals, each produced by the single escaping function for that syntax:
     - ints via `str(int)` on an exact `int`;
     - booleans as the verified literal;
     - timestamps as verified epoch milliseconds (or another verified format);
     - strings via the verified escape rule (HV-Q2 / HV-Q3).
   - **If a syntax's string escaping rule is unresolved, its string values are limited to `^[A-Za-z0-9 ._@-]{1,200}$`.** Anything else returns `unsupported_value`.
   - Field names, operators and structure come only from the allowlists, never from input.
5. **Emit** `(endpoint, params)`.
   - The `params` keys are a fixed set: `where` or `query`, `fields`, `count`, `start`, and `orderBy` or `sort`.
   - `fields` is built only from resolved raw names.

`search_entities` and `query_entities` remain the raw escape hatches (D-5-15) and are unchanged.

### 3.4 Status vocabulary (shared by both tools)

| Status | Meaning / payload |
|---|---|
| `ok` | — |
| `rejected_validation` | `errors[]`: `unknown_field`, `invalid_value`, `invalid_operator`, `invalid_concept`, `invalid_cursor`, `range_too_wide`, `restricted_field`, … |
| `unsupported` | `unsupported[]`: `{code: unsupported_entity\|unsupported_filter\|unsupported_operator\|unsupported_sort\|unsupported_value\|unsupported_concept, item, reason, hv}` |
| `definition_missing` | `missing_requirements[]` (TS-7 strings) |
| `setup_required` | — |
| `setup_revalidation_required` | — |
| `error` | Bounded and redacted via `safe_error_text`; `rate_limited` / `bullhorn_error` |

- Gate denials (`denied`, `bullhorn_auth_required`, `identity_required`) come from 5A, unchanged.
- Every non-`ok` status except `error` is reached with zero Bullhorn record requests.

## 4. Catalog growth (additive; verified names only)

| File | Addition |
|---|---|
| `canonical_schema.yaml` | Exactly four fields:<br>• `placement.client_corporation_id` (ref `client_corporation`)<br>• `placement.submission_id` (ref `submission`)<br>• `placement.recruiter_id` (ref `user`, tenant-mapped)<br>• `appointment.status` (string, tenant-configured)<br>Nothing else. No Bullhorn names (C-2). |
| `bullhorn_standard_fields.yaml` | • The `CorporateUser` section (D-5C-16).<br>• Default mappings for the new placement fields, **only** where HV-Q9 verifies the raw source.<br>• `placement.recruiter_id` and `appointment.status` get **no** default; they are tenant-mapped. |
| `mappings/bullhorn_query_support.yaml` | New (D-5C-3). |

## 5. HV items (record in `docs/architecture/PHASE5C_HV_VERIFICATION.md`; unresolved → guard)

| ID | Mechanism to verify | Guard if unresolved |
|---|---|---|
| HV-Q1 | For each `FIND_ENTITIES` entity: the supported read operation (`/search` vs `/query`) and the response shape (`data`, `total`, `start`, `count`) | `unsupported_entity` |
| HV-Q2 | The `/query` `where` grammar:<br>• operators (`=`, `<>`, `<`, `>=`, `IN`, `IS NULL`, prefix `LIKE`);<br>• literal forms (string, int, boolean, timestamp);<br>• association paths (`owner.id`);<br>• string escaping;<br>• the maximum `where` length | • Per operator: `unsupported_operator`<br>• Strings: the restricted charset (§3.3 step 4)<br>• No association path: `unsupported_filter` |
| HV-Q3 | `/search` Lucene:<br>• per-entity index field names for each filterable field;<br>• range syntax and inclusivity;<br>• date format;<br>• reserved-character escaping;<br>• prefix wildcard | • Per field: `unsupported_filter`<br>• Strings: the restricted charset |
| HV-Q4 | • Paging: the maximum `count`, `start` limits, whether `total` is present<br>• Ordering: the `/search` `sort` direction syntax and the `/query` `orderBy` direction syntax (closes P4B-5 if verified)<br>• Tie-break stability | Offset paging plus a warning; `unsupported_sort` |
| HV-Q5 | How datetime and date-only fields are represented, per entity (epoch ms UTC or otherwise) | The field is `unsupported_filter`, and its output is omitted with a warning |
| HV-Q6 | • Soft-delete per entity: whether `isDeleted` is present, and whether `/search` and `/query` return deleted rows<br>• How CorporateUser enabled/inactive is represented | `unsupported_entity` (`deleted_semantics_unresolved`) |
| HV-Q7 | 429 / 503 behaviour, the `Retry-After` form, concurrency limits (reuses the 5A HV-C9/C10 evidence) | Backoff without `Retry-After`; the fixed caps of D-5C-12 |
| HV-Q8 | CorporateUser fields (`id`, `dateAdded`, `firstName`, `lastName`, `email`, status/enabled) and its read operation (HV-B9 partial) | `user` is `unsupported_entity` |
| HV-Q9 | • Placement → submission, client corporation and recruiter fields (RAG-7)<br>• Appointment fields for classification, status/cancellation, reschedule linkage, recurrence/series representation and attendees (RAG-4) | The field is absent from the catalog; the concept is `unsupported_concept` |
| HV-Q10 | Status-history sources for submissions (RAG-5), candidates and jobs (OWG-8): the entity, fields, query mechanism and ordering | `status_history` dating and `*_status_changed` → `unsupported_concept` |
| HV-Q11 | What happens when the caller's Bullhorn role cannot read a requested field or record (an error, or omission) | Any such response is treated as `error` / `bullhorn_error`. It is never retried under another identity. |
| HV-Q12 | The maximum URL and `fields` length | The builder rejects requests that exceed a conservative fixed cap: 2,000 characters of `where`/`query`, and 30 fields |
| HV-Q13 | The syntax for selecting nested sub-fields of to-one associations in `fields` (generalizing HV-B6) | Nested fields are not returned; filters on them are `unsupported_filter` |

## 6. Pre-existing files: exact allowlist (regression case 10 is scoped to this)

| File | Protected? | Exact nature of the change |
|---|---|---|
| `src/bullhorn_mcp/tools/__init__.py` | **protected** | **One added line** after the 5A line: `from . import records  # noqa: F401`. Nothing else. |
| `src/bullhorn_mcp/mappings/canonical_schema.yaml` | **protected (Phase 3)** | **Exactly four added field lines** (§4): three under `placement.fields` and one under `appointment.fields`. Nothing else. |
| `src/bullhorn_mcp/mappings/bullhorn_standard_fields.yaml` | **protected (Phase 3)** | Additive only:<br>• a `CorporateUser` entity section;<br>• under `Placement`, `standard_fields` / `default_mappings` lines for sources verified by HV-Q9.<br>No existing line changes. Every added name cites an HV-Q ID in `PHASE5C_HV_VERIFICATION.md`. |
| `pyproject.toml` | **protected** | Add `tzdata>=2024.1` to the runtime dependencies. Nothing else. |
| `tests/test_tools_notes.py` | **protected (approved test edit)** | **Only** the `APPROVED_ADDITIVE_TOOLS` line introduced by 5A Amendment A3-0 changes, to `APPROVED_ADDITIVE_TOOLS = {"bullhorn_session", "find_records", "get_activity"}  # Phase 5 approved additive tools (D-5-15)`. Nothing else, in this file or in `tests/test_phase5b_setup.py`, which imports the constant. |
| `src/bullhorn_mcp/tenant/timeutil.py` | 4A | Receives `parse_bound`, moved from `notes/reads.py` (D-5C-9). |
| `src/bullhorn_mcp/notes/reads.py` | 4B | Imports `parse_bound` from `tenant/timeutil.py`. Behaviour is byte-identical. |
| `src/bullhorn_mcp/tenant/profile_v2.py` | 4A | • P4A-2: the concept-conflict key includes the entity.<br>• P4A-4: one active plus inactive records for one `(entity, field)` are allowed.<br>• The two D-5C-15 settings are added to `_SETTINGS_KEYS`, with closed-enum validation.<br>• P5B-13: the single source for `NOTE_ENTITY` / `NOTE_ACTION_FIELD`. |
| `src/bullhorn_mcp/notes/action_discovery.py` | 5B | P5B-13: import the constants from `tenant/profile_v2.py`. |
| `src/bullhorn_mcp/tenant/changes.py` | 4A/5B/5A | P5B-4: `reactivate_value_mapping` refuses when another active record exists for the same value. It must not disturb the 5A verification and SSO ops. |
| `src/bullhorn_mcp/tools/setup.py` | 4A | P5B-5: validate refreshes the `note_actions` sources together with the Note meta. `setup_status` lists the new capabilities. |
| `src/bullhorn_mcp/tenant/capabilities.py` | 4A | Additive capability entries: `records.find`, `activity.<concept>` (per §2) and `records.user`. |
| `src/bullhorn_mcp/tenant/revalidation.py` | 4A/5B/5A | P5B-6: `settings` is reported as `not_run`, and listed in `source_unresolved`, when discovery has never run. It must not disturb Amendment A1-2. |
| `src/bullhorn_mcp/bullhorn/settings_reader.py` | 5B | P5B-2: a streamed size cap (aborting before the full read), a recursion-safe parse, and an explicit timeout. |
| `src/bullhorn_mcp/activity/events.py` | 4B | Additive only, if needed. The existing `activity_id` derivation does not change. |
| `docs/architecture/PHASE4A_HV_VERIFICATION.md` | doc | P4A-6: the wording fix on the HV-A1 row. |

**Must not change:**
- `server.py`, `config.py`, `auth/*`, `bullhorn/client.py`, `bullhorn/meta.py`, `bullhorn/writes.py`.
- `crosscutting/*`, including `permissions.py`. The A2 gate already denies the new tools to Tier 2. If the Builder finds that a change is required, **stop and escalate**.
- `identity/*` and `tenant/state.py` (5A). Consume only their public API. (C2 withdraws the C1-2 exception, so there is no exception.)
- `schema/translator.py` and every other Phase 3 module.
- The legacy `tools/*.py`.
- `activity_concepts.yaml`.
- **Every existing test**, apart from the single `APPROVED_ADDITIVE_TOOLS` line above and the C3-1 hunks.

If any existing test fails because of an allowlisted change (for example, a pinned capability list or settings-key set), **stop and escalate**. Do not edit the test.

**New files:**

```
src/bullhorn_mcp/mappings/bullhorn_query_support.yaml
src/bullhorn_mcp/schema/query_builder.py        # AST, validation, strict resolution (NB-2), allowlists
src/bullhorn_mcp/bullhorn/query_syntax.py       # renderers + single escaping function per verified syntax
src/bullhorn_mcp/bullhorn/reads.py              # EntityReader: GET /query, /search; retries, rate limits, paging
src/bullhorn_mcp/bullhorn/log_scrub.py          # Amendment C2: shared-mode query-string scrub for httpx/httpcore records
src/bullhorn_mcp/reads/__init__.py
src/bullhorn_mcp/reads/support.py               # query-support loader (HV IDs mandatory)
src/bullhorn_mcp/reads/cursor.py                # D-5C-11
src/bullhorn_mcp/reads/records.py               # find_records service (takes IdentityContext + client explicitly)
src/bullhorn_mcp/activity/derivers.py           # one deriver per concept (§2), reusing activity/events.py
src/bullhorn_mcp/activity/service.py            # get_activity orchestration (per-concept blocks)
src/bullhorn_mcp/tools/records.py               # find_records + get_activity tools (thin)
docs/architecture/PHASE5C_HV_VERIFICATION.md    # HV-Q1..Q13 (Builder)
tests/test_query_builder*.py, tests/test_query_injection*.py, tests/test_entity_reader*.py,
tests/test_find_records*.py, tests/test_get_activity*.py, tests/test_phase5c_security_*.py
```

The internal services (`reads/records.py`, `activity/service.py`) take the `IdentityContext` and the per-call client as explicit arguments. They never call a module-level or default client. Phase 6 will reuse them internally.

## 7. Debt dispositions

| Item | 5C disposition |
|---|---|
| NB-2 | Closed by D-5C-4, plus the typo test. |
| RAG-1 / NB-4 (binding), SB-7 | D-5C-16. |
| RAG-2, RAG-3 (reads) | The priority and `primary_recruiter_id` filters (§3.1); `job_created`. |
| RAG-4 (the Phase 5 part), RAG-5, RAG-6, RAG-7, OWG-8 | §2, §4, HV-Q9/Q10. Each fails closed where unresolved. |
| RAG-8 remainder, P4A-11 | D-5C-9. |
| SB-1, SB-2, SB-3, SB-6, SB-8, SB-12 | D-5C-10, D-5C-12, D-5C-8, `find_records` on `client_corporation` / `client_contact`, and D-5C-13. |
| P4B-5 | HV-Q4. Closed only if the `orderBy` direction is verified; otherwise it stays open, with its warning. |
| P4A-2, P4A-4, P4A-5 remainder, P4A-6 | §6 / D-5C-7. |
| P5B-2, P5B-4, P5B-5, P5B-6, P5B-13 | §6. |
| P5A-2 (new reads) | D-5C-12. |
| DEBT-3 | **Close as not required.** HV-A2 verified that options are not limited to `meta=full`, so 5C needs no `get_meta` change. |
| NB-15 | **Re-defer (OPEN-DEFERRED).** Real option payloads cannot be committed (D-5-22). Revisit only if a discrepancy in the captured shape is reported. |
| NB-3, NB-19, NB-5, NB-17 | Not triggered in 5C: no samples are exposed, meta warnings are unchanged, and only the packaged catalog is used. Re-target to Phase 9. |

## 8. Acceptance criteria (mechanically checkable)

### Gates and surface

- **AC-1.** `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` exit 0. The regression suite passes 25/25, with case 10 scoped to §6. The Phase 5 SR cases pass: 5A's SR-1..19 and SR-27..32, plus the §11 cases. The wheel builds and includes `bullhorn_query_support.yaml`.
- **AC-2.** `git diff <5A-head> --name-status` shows only `A` entries, plus `M` for the §6 files. The test diff is the `A` entries, plus the one-line `APPROVED_ADDITIVE_TOOLS` change and the C3-1 hunks. The diff of `canonical_schema.yaml` is exactly the four lines of §4.
- **AC-3.** The registry has exactly 22 tools, and a new test pins the exact 22-name set. All earlier schema pins pass unmodified, including `bullhorn_session`'s `{action, confirmation}`. New tests pin both new schemas exactly as in §3.1 / §3.2 (parameter names, types and defaults). No schema contains a raw-query or identity-like parameter (5A's AC-5, extended to the new tools).

### Query safety

- **AC-4.** An injection corpus of at least 200 cases is run. It includes:
  - every Lucene reserved character;
  - `'`, `"`, `\`, `;`, `--`, `/* */` and ` OR 1=1`;
  - Unicode quotes and homoglyphs;
  - NUL and CR/LF;
  - `%` and `_`;
  - strings of 10,000 characters;
  - raw field names such as `isDeleted` and `owner.id`, camelCase names and dunder names;
  - `str`, `int` and `bool` subclasses;
  - NaN and Inf;
  - nested lists and dicts.

  For every case, one of two outcomes must hold:
  - the request is rejected with zero HTTP calls; or
  - the rendered `where` / `query` parameter tokenizes to the **same token structure** as the benign template, with the value confined to one literal token. The tests include a reference tokenizer for each syntax, and respx asserts the exact params.
- **AC-5.** A raw Bullhorn field name, a raw entity name, or any field not in the entity's canonical allowlist is rejected with `unknown_field`. Suggestions come only from the allowlist. `frist_name` yields the suggestion `first_name` (NB-2).
- **AC-6.** Sensitive-pattern fields cannot be filtered, sorted or returned (`restricted_field`). This includes tenant-mapped custom fields that match `sensitive_field_patterns`.

### Fail-closed business meaning and HV

- **AC-7.** For every row of §2:
  - with the requirement absent, the result is the listed status and `missing_requirements` strings, with zero Bullhorn record requests;
  - with a drift-affected profile (`setup_revalidation_required`), concept-dependent requests return that status, while plain `find_records` still works, with a warning.
- **AC-8.** For every HV-Q item, a test marks the item unresolved (through its flag or support entry) and asserts the listed guard, with zero calls for that capability. `PHASE5C_HV_VERIFICATION.md` has a verdict and evidence for each of HV-Q1..Q13.
- **AC-9.** A `find_records` request with any unsupported filter, operator or sort returns `unsupported` and makes **no** request; a filter is never dropped. In `get_activity`, an unsupported filter affects only that concept's block.
- **AC-10.** Interviews:
  - a submission whose status maps to nothing interview-related never yields an interview event;
  - only classified appointments yield interview events;
  - there is one event per appointment record (as amended by C3-3: per parent appointment).

### Paging, limits, retries, dates and deletion

- **AC-11.** In a fixture with interleaved inserts, keyset paging returns each record exactly once across pages. Offset mode sets `consistency: "offset"`. `complete` is true only on exhaustion. There are at most 5 page requests per call. (Amended by C3-3: offset only.)
- **AC-12.** Each of these cursors gives `invalid_cursor`, with zero calls: tampered; expired; from another principal; from another tenant; from another request; from another profile version; issued under another Bullhorn link (C1-1).
- **AC-13.** With time mocked:
  - 429/503 are retried at most 2 times, with backoff;
  - `Retry-After` is honoured, up to 30 s, only when verified;
  - the 30 s budget is enforced;
  - at most 2 requests are in flight per principal;
  - 5C code never retries a write (grep: `bullhorn/reads.py` issues only `GET`).
- **AC-14.** Date bounds:
  - a date-only bound is local midnight in the `reporting_timezone`;
  - DST-gap and DST-overlap dates are tested in two non-UTC zones, on Windows and Linux, with `tzdata` present;
  - naive datetimes are rejected;
  - `[from, to)` holds at millisecond edges;
  - output is UTC `Z` plus `occurred_at_local`;
  - the `notes/reads.py` tests pass unmodified.
- **AC-15.** Deleted records:
  - they are excluded by default, and both the query term and the post-filter are tested;
  - `include_deleted=true` is honoured only where verified;
  - unresolved soft-delete semantics give `unsupported_entity`.

### Provenance

- **AC-16.**
  - Every record has `source`.
  - Every event has the vocabulary §1 fields, and its `links` are never silently dropped (`unresolved_links` is used instead).
  - The `provenance` block is present.
  - A sentinel scan of outputs, warnings, errors, `caplog` and audit finds none of: `rest_url`, the corp token, `BhRestToken`, raw Bullhorn field or entity names, or rendered query text.
  - In shared mode, **filter values** are also absent from logs (C2).

## 9. Out of scope

- `get_recruiting_metrics`, and any Tier 2 path, threshold or de-identification (Phase 6).
- Timeline merge and composites (Phase 6).
- Grouping and aggregation, including priority-ranked ordering.
- Multi-attendee interview composites.
- Tenant attribution overrides (Q-8) and team definitions (Q-9).
- Placement status exclusions (Q-7).
- New writes.
- The `raw_query` scope (Phase 7).
- Changes to legacy tools.
- NB-3, NB-19, NB-5 and NB-17.
- The 5A follow-ups P5A-7..P5A-17.

## 10. Security & Identity Review: REQUIRED (blocking)

The Security & Identity Reviewer independently attacks each item. Each is blocking.

| ID | Attack | Expected / maps to |
|---|---|---|
| SC-1 | **Session isolation of reads:** concurrent `find_records` / `get_activity` from A and B (`bullhorn_user`, same tenant), with N ≥ 20 interleaved requests | Each Bullhorn request carries the requester's own `BhRestToken` (respx, per request). There is no shared record cache. |
| SC-2 | **Tenant isolation:** the same user in two tenants, with the tenant selected by the A3-1 claim. The profile, query-support state, cursor and session never cross tenants. A service principal of T1 has no service tier in T2. | AC-12; per-tenant profile only; D-5C-7; 5A B-4 |
| SC-3 | **Cursor replay** across principals, tenants, requests and Bullhorn links | AC-12 |
| SC-4 | **No service fallback** when: the user session expires mid-pagination; derivers make "enrichment" lookups (CorporateUser); a 401 refresh fails; the link is pending (not yet completed) | `BullhornSessionRequired`, or the tier flips to `workspace_only` → denied. Zero service-session resolutions (G-4). |
| SC-5 | **Tier gating (the REQ §8.6 items that apply in 5C).** A `workspace_only` caller invokes `find_records`, `get_activity`, `get_notes`, `search_entities`, `query_entities` and every legacy read, with every parameter combination: concept filters, narrowed scopes, `fields` that include IDs or names, and cursors issued to a Tier 1 user. | `bullhorn_auth_required` before argument parsing, with zero Bullhorn calls and zero service calls. Re-run Amendment A2's R-A2a/b with 22 tools. |
| SC-6 | **Requests for restricted IDs or provenance** by Tier 2, for example `fields=["id"]`, a scope on a known candidate ID, or a cursor that carries an ID | Denied as in SC-5; nothing is echoed |
| SC-7 | **Parameter manipulation for escalation:** tier- or identity-like arguments, extra unknown keys in `Filter` / `Sort`, raw names, `concept` on the wrong entity | Rejected (AC-3, AC-5) |
| SC-8 | **Injection** (Lucene / JPQL, escaping, Unicode, type subclasses) | AC-4 |
| SC-9 | **Provenance, secret and filter-value leakage** in outputs, errors, logs, audit and cursors | AC-16; cursor payload contents as in D-5C-11 / C1-1; C2 |
| SC-10 | **Respect for Bullhorn permissions:** a field or record that the user's Bullhorn role cannot read | An error is returned. It is never retried under another identity (HV-Q11). |
| SC-11 | **Resource exhaustion:** the maximum number of filters, `in` sizes and `limit`; page fan-out; 366-day ranges; 8 concepts | Caps are enforced: at most 5 pages per `find_records` call, and a per-concept page cap of 5 in `get_activity` |
| SC-12 | **Guessed business meaning:** remove or invalidate each mapping and setting in §2 | AC-7 |

## 11. New security-regression cases

| ID | Case |
|---|---|
| SR-20 | `find_records` / `get_activity` are denied to `workspace_only` callers with zero calls (SC-5, SC-6). |
| SR-21 | Two concurrent users' reads stay isolated (SC-1). |
| SR-22 | Cursor binding, including the link binding (SC-3). |
| SR-23 | The token structure is unchanged across the injection corpus (SC-8). |
| SR-24 | No service fallback during reads (SC-4). |
| SR-25 | Provenance, secret and filter-value sentinel scan (SC-9, C2). |
| SR-26 | Concept definitions fail closed (SC-12). |

## 12. Reviewer notes (both reviewers)

Flag any of the following as findings:
- any tool beyond the two new ones;
- any parameter beyond those in §3.1 / §3.2;
- a second date parser, concept registry, activity-ID scheme or note-reading path;
- a table of Bullhorn names outside `bullhorn_standard_fields.yaml` / `bullhorn_query_support.yaml`;
- concept logic that bypasses `tenant/capabilities.py`;
- a query string assembled outside `bullhorn/query_syntax.py`;
- a Bullhorn request made outside `bullhorn/reads.py` (for new code) or the existing 4B paths;
- a filter silently dropped or approximated.

**Process:**
- The order is Builder → fresh Independent Reviewer → fresh Security & Identity Reviewer → gates.
- Failures go to Architect triage → fix → fresh affected reviewer(s) → gates.
- No commit until all of Phase 5 passes.
- Do not begin Phase 6.

---

## Amendment C1 (2026-10-07): 5A outcomes that 5C must honour

5A closed with two-step account linking (B-2), a per-link execution identity (B-3), per-tenant roles and service principals (B-4), a setup-admin gate on `propose_mapping_changes` (CO-1), and a shared-mode logging record factory (B-1). This amendment records their consequences for 5C. The amendment's text is already reflected in §6, AC-1, AC-3, AC-12, AC-16, SC-2..4, SC-9 and SR-22/25.

### C1-1: cursors are bound to the Bullhorn link

**Change.** In shared mode, the D-5C-11 cursor payload adds `sha256(executing_bullhorn_identity)`, which is the 5A B-3 `bh-link:` label. A logout followed by a re-link (a new `link_id`) invalidates outstanding cursors with `invalid_cursor`. A refresh does not, because the `link_id` is unchanged.

**Why.** A position obtained under one Bullhorn account must not continue under another.

**Test.** Part of AC-12 / SR-22.

### C1-2: filter values must not reach logs in shared mode (**superseded by C2**)

The requirement stands: in shared mode, filter values must never reach logs. C1-2's mechanism, raising the `httpx` / `httpcore` clamp in `identity/deploy.py` to WARNING, is **withdrawn**: the clamp lives in `auth/secrets.py`, and a 5A test pins the INFO request line. See C2.

### C1-3: tiers, roles and the service identity per tenant

- **Service tier.** `access_tier = service` is evaluated against the **selected tenant's** `service_principals` (5A B-4). Service reads through `find_records` / `get_activity` run only under that tenant's service session. Covered by SC-2 / SC-4. (Superseded by C4-2: these tools are denied to `service` in Phase 5.)
- **Pending link.** A caller whose link is pending (B-2 not yet completed) is `workspace_only`, so the A2 gate denies both new tools.
- **Settings.** Setting the D-5C-15 values (`set_setting`) requires a setup admin **of the selected tenant**, through the normal propose/commit flow (CO-1, A3-3). 5C adds no new admin path.

### C1-4: tool surface

- `bullhorn_session`'s schema (`{action, confirmation}`) is frozen; 5C must not change it.
- The 22-name registry pin includes it.

### C1-5: refresh during paging

The 5C 401 path uses the 5A session refresh, which preserves the `link_id`. If the refresh fails mid-call, the tool returns `error` with no partial results. The session is then deleted per AC-11 of 5A. There is no retry under another identity.

---

## Amendment C2 (2026-10-07): mechanism for keeping filter values out of logs (supersedes C1-2)

**Ruling.** Option (b) is generalized as the coordinator suggested.
- **Option (a) is rejected.** It would change frozen 5A code (`auth/*`) and a 5A test.
- **Option (b) as proposed, a contextvar tied to `EntityReader`, is rejected** because it covers only one call site.

### C2-1: what is added

A new file, `src/bullhorn_mcp/bullhorn/log_scrub.py`, provides `QueryStringScrubFilter` (a `logging.Filter`).

**What the filter does to a record:**
- It applies only to records whose logger name is `httpx`, `httpcore`, or a child of either.
- It applies only while deployment mode is `shared`. It reads the mode through the 5A public API on each call, so in `local` mode it does nothing and the record is unchanged.
- It takes the fully formatted message (`getMessage()`). The 5A factory has already redacted it and set `args=()`.
- It replaces the query component of every URL in that message with `?<query-redacted>`, keeping the method, scheme, host and path. The query component runs from `?` to the first whitespace, quote or end of message. (C4-1 changes the placeholder to `?<query-REDACTED>`; triage B-3 adds redaction of numeric path segments.)
- It then sets `msg` to the result and `args=()`, and **always returns `True`**. The record is kept, so the 5A test T-B1a/c, which expects the "HTTP Request" line, still passes.

**Where it is installed:**
- It is installed idempotently on the `httpx` and `httpcore` loggers, and on every child logger of either that exists at that moment.
- It is installed when `bullhorn/log_scrub.py` is imported, from `tools/records.py` (which loads at server startup through the approved `tools/__init__.py` line).
- It is installed again, idempotently, in `EntityReader.__init__`.
- Because of this, it covers **every** request in shared mode: legacy `search_entities` / `query_entities`, `get_notes` / 4B to-many reads, 5B settings reads, and the new `EntityReader`.

**Files unchanged.** No existing file or test changes. `auth/*`, `identity/*`, the INFO clamp and T-B1a..f are untouched. The §6 "must not change" list stands with no exception.

### C2-2: tests (blocking; SR-25)

| ID | Test |
|---|---|
| T-C2a | In shared mode, capture logging from the root logger at level 0 (all levels), plus a root `StreamHandler`. Run `find_records` (filter values: a sentinel name and a sentinel email), `search_entities`, `query_entities` and `get_notes`, each through real httpx client code with respx transports. **No** captured record's `getMessage()`, and no handler output, contains either sentinel. The "HTTP Request" lines are still present, showing the method and path. |
| T-C2b | In local mode, the httpx record message is byte-identical to the unfiltered message, and the filter is a no-op. P5A-1 is unchanged. |
| T-C2c | **Pin.** The installed httpx emits its URL-bearing request record on the logger named exactly `httpx`. If a future httpx changes this, the test fails, and the Builder must **stop and escalate**. This guards against the child-logger bypass class seen in 5A B-1. |
| T-C2d | The 5A tests T-B1a..f pass unmodified. |

### C2-3: residual risk

Records created through `makeLogRecord`, custom formatters and `extra=` fields can bypass the filter. This is the same class as P5A-12, and it stays there (Phase 9, plus the P5A-16 pre-production gate). Reviewers treat it as known, not new.

---

## Amendment C3 (2026-10-07): the user-binding pin, pre-approved Builder plans, HV outcomes, and the complete test-pin list

**Purpose.** This amendment is final for 5C scope. Every known deviation is pre-approved below, so the Builder can finish without stopping again. The only remaining stop condition is anything **not** listed here.

### C3-1: the only approved existing-test edits in 5C

The Architect scanned the tests for pins of pre-5C state: `_in_5a`, `_in_5b`, tool counts of 19 or 20, exact tool sets, capability sets, `_SETTINGS_KEYS` and catalog entity sets. Exactly three hunks are approved. Test names may change only where stated.

| # | File | Approved hunk |
|---|---|---|
| 1 | `tests/test_tools_notes.py` | As in §6: only the `APPROVED_ADDITIVE_TOOLS` line, which becomes `{"bullhorn_session", "find_records", "get_activity"}`. |
| 2 | `tests/test_phase5a_canonical_user.py` | **The blocker; option (a) is approved, because HV-Q8 is verified.** Lines 24–25 become:<br>`def test_user_binds_to_corporate_user_from_5c():`<br>`    assert load_bullhorn_catalog().bullhorn_entity_for("user") == "CorporateUser"  # retargeted by 5C Amendment C3 (D-5C-16; HV-Q8 verified)`<br>No other line changes. `test_user_entity_fields` (exactly 11 entities, the exact `user` field list) still passes unmodified, because 5C adds no canonical entity and no `user` field. |
| 3 | `tests/test_phase5a_security_tools.py` | `TestRegistry.test_exactly_twenty_tools` pins 20 names. Two changes:<br>(i) Insert one line immediately after the `ALL_20` definition: `PHASE5C_TOOLS = {"find_records", "get_activity"}  # 5C approved additive tools (Amendment C3)`.<br>(ii) Lines 95–96 become:<br>`assert set(server.mcp._tool_manager._tools) == ALL_20 \| PHASE5C_TOOLS`<br>`assert len(server.mcp._tool_manager._tools) == 20 + len(PHASE5C_TOOLS)`<br>The test name is unchanged. `NON_ALLOWLISTED` is **not** edited: the new tools' tier-gate coverage comes from the new 5C tests (SR-20). |

**Standing rule for the rest of 5C.**
- Any other existing test that fails is a **stop and escalate**. Do not edit it.
- The case-10 freeze includes these three hunks.

### C3-2: the Builder's plans, pre-approved as stated

**Capabilities (`tenant/capabilities.py`):**
- New `CAPABILITIES` entries are `records.find`, `records.user` and `activity.<concept>`.
- They carry **state requirements only**. `activity.job_created` also carries the existing `("job", "primary_recruiter_id")` field mapping, which de-duplicates, so the 4A aggregation and `CAP_MISSING` pins stay unchanged.
- Concept-mapping and setting requirements (`value_mapping:<entity>:<concept>`, `setting:<name>`) are evaluated by a **new** function in `capabilities.py`.
- They are surfaced only in `setup_status.requirement_details` and in the tools' `missing_requirements`, **never** in the 4A top-level list. This follows the 5B `notes.create` precedent.
- `test_phase5b_setup.py:302` (requirement_details keys equal the capabilities keys) must still hold.

**`parse_bound` move (D-5C-9):**
- There is one parser, in `tenant/timeutil.py`, which takes a zone-resolver argument.
- `notes/reads.py` keeps `_zone`, and binds the resolver **at call time**, so `tests/test_notes_reads.py::test_date_only_reporting_timezone`, which monkeypatches `reads._zone`, passes unmodified.
- `tzdata` is now installed.

**P4A-2 / P4A-4 and the settings:**
- The existing fixtures `two_active.yaml` and `concept_conflict.yaml` remain rejected. That is correct: they test two **active** records and a **same-entity** conflict.
- The new settings are emitted in profile output only when they are set, so existing profile byte pins are unchanged.

### C3-3: HV outcomes and their binding consequences

These are recorded in `PHASE5C_HV_VERIFICATION.md`, which is authoritative for the evidence.

**HV-Q1, read operation.**
- Outcome: `/query` is verified for all 8 `FIND_ENTITIES`.
- Consequence: **5C uses `/query` only.** The Lucene renderer is **not built**: no dead code, and the query-support `operation` is always `query`. AC-4 needs only the JPQL reference tokenizer; the Lucene-reserved characters remain in the corpus as values.

**HV-Q3, `/search`.**
- Outcome: unresolved.
- Consequence: no impact, because of HV-Q1.

**HV-Q2, `where` grammar.**
- Outcome: partly verified. These are verified: the operators `=`, `<>`, `<`, `<=`, `>`, `>=`, `IN` and `IS [NOT] NULL`; to-one paths; boolean literals; epoch-ms timestamps; `'x'` string literals. `LIKE` and string escaping are unresolved.
- Consequences:
  - There is no `starts_with`.
  - String values are limited to the restricted charset `^[A-Za-z0-9 ._@-]{1,200}$`; anything else returns `unsupported_value`. Names containing an apostrophe are therefore unsupported. This is accepted, and documented in the HV doc.
  - `is_null` may also render `IS NOT NULL`, internally only, for the soft-delete term.

**HV-Q4, ordering.**
- Outcome: unresolved; the `/query` `orderBy` direction is undocumented.
- Consequences:
  - **Offset paging only**, with `consistency: "offset"` and a warning. **No `orderBy` is sent.**
  - Any `sort` returns `unsupported_sort`.
  - P4B-5 stays open.
  - `complete` is true when a page returns fewer than `count` rows.
  - **AC-11 is amended:** the keyset assertions apply only if HV-Q4 is later verified. The offset-mode assertions (the warning, the 5-page cap, the `complete` rule) are required.
  - "Ordered by source `id` ascending" in §3.2 becomes "in server order; the order is not guaranteed", stated in the warning.

**HV-Q6, soft delete.**
- Outcome:
  - `isDeleted` exists on Candidate, ClientContact, JobOrder, JobSubmission, Appointment and CorporateUser. It is nullable on JobOrder and CorporateUser.
  - ClientCorporation and Placement are not soft-deletable: ClientCorporation is immutable, and Placement has no field.
  - Whether `/query` returns soft-deleted rows is undocumented.
- Consequences:
  - **Exclusion term:** `(isDeleted = false OR isDeleted IS NULL)`, plus a post-filter that drops `isDeleted is True`.
  - **Architect interpretation HV-Q6-I:** a NULL `isDeleted` means "not deleted". It is an unset boolean flag, and treating it as deleted would hide live jobs and users. This is recorded as an interpretation in the HV doc.
  - `include_deleted=true` returns `unsupported_filter` for every entity.

**HV-Q7, rate limits.**
- Outcome: the documented wait after a 429 is 1 s, and there is no `Retry-After`.
- Consequences:
  - The D-5C-12 backoff **base becomes 1.0 s**; the cap stays 8 s.
  - `Retry-After` is never parsed.

**HV-Q8, CorporateUser.**
- Outcome: verified.
- Consequences:
  - The `CorporateUser` section's default mappings are:
    - `id: id`;
    - `date_added: userDateAdded`;
    - `first_name: firstName`;
    - `last_name: lastName`;
    - `full_name: "{firstName} {lastName}"`;
    - `email: email`;
    - `status: status`.
  - `enabled` and `isDeleted` are query-support only, not canonical.

**HV-Q9, Placement and Appointment.**
- Placement:
  - `placement.submission_id` has the default `{field: jobSubmission, key: id}`.
  - `placement.client_corporation_id` and `placement.recruiter_id` are **tenant-mapped, with no default**. When unmapped, the event links are `null` and listed in `unresolved_links`.
- Appointment:
  - `appointment.status` is tenant-mapped. There is no native status or cancellation field.
  - A recurring series is one record, so events carry `recurrence_not_expanded`.
  - There is no reschedule link, so `interview_rescheduled` is `unsupported_concept`.
- **New rule C3-3a (Appointment child copies).** Bullhorn creates a child Appointment per invitee. Interview instances are **parent appointments only**, filtered by the verified parent-link field `IS NULL`. The Builder verifies that field's name as **HV-Q9b**; if it is unresolved, every `interview_*` concept is `unsupported_concept` (reason `invitee_copies_unresolved`). Children are never counted. Attendee composites are Phase 6. AC-10 is read as "one event per parent appointment".

**HV-Q10, status history.**
- Outcome: unresolved.
- Consequences:
  - `client_submission_dating` accepts **only** `submission_date_added`; `status_history` is rejected at validation.
  - `job_status_changed` / `candidate_status_changed` return `unsupported_concept`.

### C3-4: no further stops expected

Everything above is binding. The Builder proceeds to completion. The only escalation triggers are:
- a failing existing test not listed in C3-1;
- any need to touch a "must not change" file;
- an HV result that contradicts C3-3.

---

## Amendment C4 (2026-10-07): ratification of two build-time deviations, and the Builder notes

### C4-1: the placeholder is `?<query-REDACTED>`. Ratified.

The C2-1 literal `?<query-redacted>` is replaced by `?<query-REDACTED>`.

**Why.** The 5A tests below assert the case-sensitive marker `REDACTED`. C2's T-C2d requires them to pass unmodified, and with the lowercase marker they fail:
- `test_phase5a_triage_b1.py::test_t_b1a_child_logger_records_are_redacted_even_at_warning`;
- `test_phase5a_security_tools.py::TestSecretSentinels::test_sentinels_never_leak`.

**Impact.** The change is cosmetic, and the behaviour is unchanged. T-C2a..d stand with the new literal.

### C4-2: the service tier is denied `find_records` / `get_activity` in Phase 5. Accepted fail-closed.

**What happens.** The frozen `crosscutting/permissions.py` `SERVICE_READ_TOOLS` does not list the two new tools. Its deny-by-default rule therefore returns `service_identity_read_only` to a `service` caller. This is pinned by a test.

**Ruling.**
- This is **accepted** for Phase 5. No frozen-file change is approved.
- It is fail-closed, and nothing in Phase 5 needs unattended record-level reads through these tools.
- C1-3's first bullet is superseded accordingly.

**Phase 6.**
- Phase 6 needs the service identity for the Tier 2 internal computation. It should call the internal services (`reads/records.py`, `activity/service.py`) **directly**, with an explicit service `IdentityContext`, rather than through the record-level tools.
- Phase 6 decides whether `SERVICE_READ_TOOLS` should ever include them. This is logged as **P5C-1** (target 6) at the 5C close-out.

### C4-3: Builder notes. Accepted, logged at the 5C close-out.

| ID | Note | Disposition |
|---|---|---|
| P5C-2 | The C2 filter attaches only to the `httpx` / `httpcore` child loggers that exist at install time (at import, and at each `EntityReader` construction). Children created later are not covered. | P5A-12 class. Phase 9, plus the P5A-16 pre-production gate. T-C2c pins that httpx emits on `httpx` itself. |
| P5C-3 | The legacy tools' own audit records (`crosscutting/audit.py`, frozen) still contain their raw query arguments (`search_entities` / `query_entities`), and therefore possible PII. | Phase 9 (audit free-text redaction, Q-W7). Added to the P5A-16 pre-production gate. |
| — | `interview_completed` uses `end_at` as its timestamp. | **Ratified.** Vocabulary §2 allows "outcome time or `end_at`, per the rule", and no outcome-time field is verified (HV-Q9). The rule ID records it. |
| — | `find_records(concept="interview_upcoming")` returns `invalid_concept`. | **Ratified.** `concept` accepts only mapping-based concepts; `interview_upcoming` is a derived state concept, available only through `get_activity`. |
| — | HV-Q6-I (a NULL `isDeleted` means not deleted) is recorded as an interpretation. | Acknowledged (C3-3). |

---

## 5C Review Triage (2026-10-07)

**Inputs:**
- The Independent Reviewer returned **FAIL**, with one blocking finding (Ind B-1).
- The Security & Identity Reviewer returned **FAIL**, with one blocking finding (Sec B-1).
- Non-blocking: Sec N-1..N-5 and Ind N-1..N-8.

**Architect promotion:** Sec N-1 is promoted to **B-3**, for C2 consistency, since a `scope_id` is a filter value.

**Nothing is demoted.**

**Every fix below is local.** They touch only 5C-new files or files on the §6 allowlist, and the Builder does them all in one round. Tests are named `test_phase5c_triage_*`; every T-test is new and blocking.

### B-1 (blocking; Ind B-1): `find_records(concept=…)` bypasses the concept definitions (G-2, §2, AC-7, §12)

**Fix.**
1. **One definition.** `reads/records.py` must obtain a concept's requirements **only** from `tenant/capabilities.concept_requirements`, the single source that `get_activity` also uses. The local `need=[...]` construction, and any local copy of concept rules, are deleted.
2. **Same requirement set.** `find_records` and `get_activity` apply the **same** requirement set to the same concept. In particular:
   - a missing `interview_completion_rule` for `interview_completed` gives `definition_missing` with `setting:interview_completion_rule`;
   - a missing `client_submission_dating` for `client_submission` gives `definition_missing` with `setting:client_submission_dating`. This is for consistency: one concept definition, whether or not the setting affects current state.
3. **The time-based rule.** When `interview_completion_rule = end_passed_not_cancelled`, `find_records(concept="interview_completed")` returns `unsupported` / `unsupported_concept`, reason `rule_not_renderable_as_current_state`, with zero calls. `get_activity` keeps computing it. `find_records` never applies the mapped-state definition against the tenant's rule.
4. **`concept` scope (folds in Ind N-4, and clarifies §3.1).** The concepts `find_records` accepts are exactly:
   - `client_submission`;
   - `interview_scheduled`, `interview_completed` (subject to point 3) and `interview_cancelled`;
   - `offer_extended`, `offer_accepted` and `offer_declined`.

   The derived state concepts `interview_upcoming` and `offer_pending` give `rejected_validation` / `invalid_concept`, with a message pointing to `get_activity` (consistent with C4-3).

**Tests:**

| ID | Test |
|---|---|
| T-5C-B1a | Mappings present but no `interview_completion_rule` → `find_records` and `get_activity` both return `definition_missing` with the same `missing_requirements`. Zero `/query` requests. |
| T-5C-B1b | `end_passed_not_cancelled` → `find_records` returns `unsupported_concept` (`rule_not_renderable_as_current_state`) with zero calls, while `get_activity` returns events. |
| T-5C-B1c | Missing `client_submission_dating` → both tools return `definition_missing`. |
| T-5C-B1d | Parametrized over every concept: the requirement set `find_records` uses equals the one `get_activity` uses (both from `concept_requirements`). |
| T-5C-B1e | `offer_pending` and `interview_upcoming` in `find_records` → `invalid_concept`. |

### B-2 (blocking; Sec B-1): filter values reach the audit and logs through unkeyed digests and unknown keys (C2, AC-16, SC-9)

**Fix (`reads/records.py`, `activity/service.py`, `tools/records.py`).**

**1. What audit and log records of the two new tools may contain.** Only these:

| Item | Content |
|---|---|
| Names | The tool name, the entity, and the concept / concepts. |
| Filter fields | **Only after validation:** allowlisted canonical field names and operator names. |
| Values | For each value, only its **type and length**, never the value and never a digest. This covers filter values, `date_from` / `date_to`, `scope_id`, `recruiter_id` and the `cursor`. |
| Rejected input | Anything that failed validation or is not on an allowlist (an unknown key in a `Filter` or `Sort`, an invalid `scope_type`, an invalid sort direction, invalid `fields` entries, an unknown concept) is recorded only as a count, such as `{"invalid_items": N}`. **The key or value text is never recorded.** |
| Request identifier | A **keyed** `request_hmac`: HMAC-SHA256 with a key derived by HKDF from the 5A active session key, using its own `info="bullhorn-mcp/audit-digest/v1"`. Local mode uses a per-process random key. |

**2. The provenance `request_hash`.** The response's `provenance.request_hash` uses the same keyed HMAC, so that no unkeyed hash of request values exists anywhere. (Superseded by Round 2, R2-B1: the provenance value must **not** equal the audit HMAC.)

**3. Removal.** The `digest` / `digest_args` helpers are removed. No unkeyed `sha256(value)` remains anywhere in 5C code (grep-tested).

**Tests:**

| ID | Test |
|---|---|
| T-5C-B2a | In shared mode, with root capture at all levels plus the audit file bytes: sentinels are placed in every value position (`value`, `date_from`, `date_to`, `scope_id`, `recruiter_id`, `cursor`), in unknown-key **names and values** (`val`, `values`, a 64-character ASCII key), in the sort direction, `fields`, `scope_type` and `concepts`. None of them appears. |
| T-5C-B2b | For each submitted value, its unkeyed `sha256` hex does not appear in the logs or the audit (an enumeration-resistance check). |
| T-5C-B2c | `request_hmac` differs between two session keys and between two tenants for an identical request. It is stable for a repeated identical request under the same key. |
| T-5C-B2d | Grep: no `hashlib.sha256(` is applied to request values in `reads/`, `activity/service.py` or `tools/records.py`. |

### B-3 (blocking; promoted from Sec N-1): record IDs in URL paths are logged in shared mode

**Ruling.** In shared mode, record IDs in URL paths are filter values under C2, so they must not reach logs.

**Fix (in `bullhorn/log_scrub.py` only).** For the same records and mode as C2-1, the filter also replaces every all-digit path segment of each URL with `<id>`.
- For example, `/entity/JobOrder/123/notes` becomes `/entity/JobOrder/<id>/notes`.
- This covers the `note_created` path and, as a side effect, the legacy `get_job` / `get_candidate` paths. No legacy code changes.
- Local mode is unchanged (T-C2b).

**Tests:**

| ID | Test |
|---|---|
| T-5C-B3a | In shared mode, `get_activity(concepts=["note_created"], scope_type="job", scope_id=<sentinel digits>)` and legacy `get_job(job_id=<sentinel>)` leave no sentinel in any captured record. The "HTTP Request" line still shows the method and the redacted path. (Amended by C5-1: the frozen legacy `get_job` audit line is excluded.) |
| T-5C-B3b | T-C2a..d and T-B1a..f pass unmodified. |

### Local fixes (required in the same round; non-blocking in origin)

| ID | Source | Fix | Test |
|---|---|---|---|
| L-1 | Ind N-3 | `tenant/capabilities.py`'s `ACTIVITY_CONCEPTS` is derived from `profile_v2.load_activity_concepts()` (one source, `activity_concepts.yaml`), not hard-coded. `records.py` keeps no `CONCEPT_FILTERS` / priority-order duplicate; it uses `capabilities` (`CONCEPT_RULES`, `evaluate_capability`) through B-1. | T-5C-L1: `set(ACTIVITY_CONCEPTS) == load_activity_concepts()`, plus the grep test for the removed duplicates |
| L-2 | Ind N-5 | P4A-4 collision. When one active and one or more inactive records share an `(entity, field)`, every lookup (`effective_states`, `records_by_key`, and v2 import at `changes.py:511`) resolves to the **active** record, whatever the order. Import preserves all records. No active mapping can be dropped by record order. (Detailed by C5-2.) | T-5C-L2: the inactive-after-active and active-after-inactive orders, in both the parser and the import, give an identical effective mapping |
| L-3 | Sec N-3 | Cursor signature hygiene. Decode, re-encode canonically, and compare the canonical **string** in constant time. Non-canonical base64 (redundant padding, alternative alphabet) gives `invalid_cursor`. | T-5C-L3 |

### Deferred or accepted (logged in `DEFERRED_DEBT.md` at the 5C close-out)

| ID | Source | Disposition |
|---|---|---|
| P5C-1 | Sec N-5 / Ind N-2 | Service tier denied the new tools (C4-2). Target 6. |
| P5C-2 | Builder note | The C2 filter misses child loggers created later (C4-3). Phase 9, plus the P5A-16 gate. |
| P5C-3 | Sec N-2 | The legacy audit records the raw `where` / `query` text (frozen; C4-3). Extended by C5-1 to legacy path-ID arguments. Phase 9, plus the P5A-16 gate. |
| P5C-4 | Sec N-4 / Ind N-8 (semaphore) | Race in the per-key semaphore eviction from the LRU above 10,000 keys (`bullhorn/reads.py:64-76`). Same class as P5A-13; Phase 9. |
| P5C-5 | Ind N-8 (response size) | `EntityReader` has no streamed response-size cap; `count` is at most 100. Phase 9, reusing the P5B-2 streamed-cap pattern. |
| P5C-6 | Ind N-6 | The error string for an unknown setting changed (`changes.py:321`); no test pins it. Accepted, CLOSED. |
| P5C-7 | Ind N-7 | The `tests/_phase5c_helpers.py` name falls outside the §6 patterns. Harmless; accepted, CLOSED. |
| P5C-8 | C5-2 | Inactive duplicates are dropped without a warning when their primary is deactivated or removed. Phase 7 (store / commit hardening). |
| P5C-9 | Round 2, Ind N-2 | The interview / offer current-state predicate is built in two places (`records.py` from `CONCEPT_RULES.groups`; `derivers._source` from hard-coded groups), and `evaluate_capability` repeats the `ordering_values` loop. Parity is pinned by R2-T1. Consolidation is deferred to Phase 6, where `get_activity` is extended. |
| P5C-10 | Round 2, Ind N-3 | `records.py` special-cases `interview_rescheduled`, duplicating `derivers.UNSUPPORTED_CONCEPTS`. Behaviour is identical, so this is cleanup only. Phase 6. |
| — | Ind N-1 | Ruled in C4-1; CLOSED. |

### Re-review and regression

**Reviews:**
- A **fresh Independent Reviewer** reviews B-1 and L-1..L-3.
- A **fresh Security & Identity Reviewer** reviews B-2 and B-3, re-runs SC-9 / SR-25, and does a fresh pass over SC-1..12.
- Then the gates.

**Case 10:** no protected-file change is introduced.

**New regression cases:**

| ID | Case |
|---|---|
| SR-33 | Audit and log records contain no request values and no unkeyed digests (B-2). |
| SR-34 | Path-ID scrub in shared mode (B-3). |
| SR-35 | `find_records` and `get_activity` use the same concept definition (B-1). |

---

## Amendment C5 (2026-10-07): ratification of two fix-round details

### C5-1: T-5C-B3a's legacy audit-line exclusion. Ratified.

**The constraint.** The frozen legacy `get_job` writes `job_id` into its own audit record, through the frozen `crosscutting/audit.py`. No permitted change can remove it.

**The exclusion.** T-5C-B3a may exclude **exactly that one record**: the legacy `get_job` audit line. The test must:
- identify the excluded record by its logger and tool name, not by a broad pattern;
- assert that every other captured record is free of the sentinel, which includes every httpx line, showing `<id>`, and the new tools' audit records.

**P5C-3 is extended.** It now covers **legacy path-ID arguments** (`job_id`, `candidate_id` and similar) in the legacy audit records, as well as the raw `where` / `query` text. It stays Phase 9, plus the P5A-16 pre-production gate.

### C5-2: L-2 retention of inactive duplicates. Ratified, with one debt item.

**The ratified rules:**
- An inactive duplicate is kept **only** while the primary record for its `(entity, field)` slot is active.
- When the primary is deactivated or removed, the duplicate is dropped. Otherwise the P4A-4 parser would reject a slot with no active record.
- Diffs are keyed by record key. An import that differs only by an extra inactive duplicate is therefore "no changes".

**Why this is acceptable.** Inactive duplicates have no effect on behaviour; they carry history only. No active mapping can be lost.

**Debt.** The drop is silent: no warning appears in the proposal or import output. This is logged as **P5C-8**, Phase 7 (store / commit hardening, together with P4A-7 and P4A-8). No change is needed this round.

---

## 5C Review Triage — Round 2 (2026-10-07; D-5-25 applied)

**Inputs:**
- The Security & Identity re-review returned **FAIL**, with one blocking finding (SB-1) and two non-blocking (N-1, N-2).
- The Independent re-review returned **PASS**, with no blocking findings and the gates green. It raised non-blocking Ind N-1..N-5, which are folded in below.

**D-5-25 classification:**

| Finding | Classification | Disposition |
|---|---|---|
| SB-1 | Sensitive-data leakage, in scope | **Fix now** (R2-B1) |
| Sec N-1 = Ind N-4 | Required to make approved behaviour consistent (triage B-3: record IDs in URL paths must not reach shared-mode logs), in scope | **Fix now** (R2-L1) |
| Sec N-2 | Permitted by B-2 | No action |
| Ind N-1 | A regression-protection gap: the C5-1 test exclusion is less precise than ratified. In scope. | **Fix now** (R2-L2) |
| Ind N-5 | A correctness defect in approved output: the HV-Q5 guard cites the wrong HV ID in the `hv` field of §3.4. In scope. | **Fix now** (R2-L3) |
| Ind N-2 | Duplicated logic. Parity is verified, so this is cleanup. The missing parity regression test is in scope. | **Add test now** (R2-T1); consolidation **deferred**: P5C-9 |
| Ind N-3 | Duplicated special case with identical behaviour; cleanup | **Deferred**: P5C-10 |

### R2-B1 (blocking; SB-1): the audit HMAC can be computed from model-visible values

**The problem.** `provenance.request_hash` and the cursor's `r` field are byte-identical to the audit `request_hmac`, and the HMAC does not cover the principal. Any linked user in the same tenant can therefore compute the audit digest of any request of their choosing by calling the tool, and use it to confirm a colleague's filter values from the audit log.

**Fix (`reads/records.py`, `activity/service.py`, `reads/cursor.py`, `tools/records.py`). Apply both of the reviewer's measures.**

1. **Audit HMAC (`request_hmac`):**
   - Use its own HKDF key: `info="bullhorn-mcp/audit-digest/v2"`, derived from the 5A active session key. Local mode uses a per-process random key.
   - The message is `tenant_key | principal_key | normalized_request`.
   - The value appears **only** in audit and log records. It **never** appears in any tool output: not in provenance, not in a cursor, not in an error.
2. **Provenance `request_hash`:**
   - Use a separate HKDF key: `info="bullhorn-mcp/provenance/v1"`.
   - The message is `tenant_key | principal_key | normalized_request`.
   - It is model-visible, but it is a different value from the audit HMAC.
3. **Cursor request-binding field `r`:**
   - Use the cursor key (`info="bullhorn-mcp/cursor/v1"`, D-5C-11).
   - The message also covers the principal and the link (C1-1).
   - It is never equal to the audit HMAC.
4. **Key separation.** No two of these three values share a key. Each HKDF `info` string appears exactly once in the code, which is grep-tested.

**Tests:**

| ID | Test |
|---|---|
| T-5C-R2a | Bob runs `find_records(... last_name eq "Hiddenname")`. Alice, a linked principal in the same tenant, runs the identical request. Every value in Alice's provenance, cursor and output differs from Bob's audit `request_hmac`. Alice's own audit `request_hmac` also differs from Bob's. |
| T-5C-R2b | For any single request, the audit `request_hmac` is not equal to that request's `provenance.request_hash`, nor to the cursor's `r` field, nor to any other string in the tool output. The test scans the whole JSON output. |
| T-5C-R2c | The same principal, the same request and the same key give a stable audit HMAC. A different principal or a different tenant gives a different one. |
| T-5C-R2d | Grep: each of the three HKDF `info` strings is defined exactly once. `reads/records.py`, `activity/service.py` and `tools/records.py` contain no code path that copies `request_hmac` into the response. |

### R2-L1 (required; Sec N-1 = Ind N-4): comma-joined IDs in URL path segments (B-3 consistency)

**Fix (in `bullhorn/log_scrub.py` only).**
- The path-ID pattern becomes `(?<=/)[0-9]+(?:,[0-9]+)*(?=/|$|\?)`.
- Each matching segment becomes `<id>`. For example, `.../entity/Note/<id>/candidates/918273001,918273002` becomes `.../entity/Note/<id>/candidates/<id>`.
- Shared mode only; local mode is unchanged.

**Tests:**

| ID | Test |
|---|---|
| T-5C-R2e | In shared mode, a 5B association-path request logged by httpx with comma-joined sentinel IDs leaves no sentinel in any captured record. |
| T-5C-R2f | T-5C-B3a/b, T-C2a..d and T-B1a..f pass unmodified. |

### R2-L2 (required; Ind N-1): T-5C-B3a exclusion precision

**Fix.** T-5C-B3a's exclusion must match the legacy `get_job` audit record by **both** of these, exactly as C5-1 requires:
- the audit logger name (the frozen `crosscutting/audit.py` logger);
- `"tool": "get_job"`.

No other record may be excluded.

**Test.** T-5C-R2g is a negative control: a non-audit record that contains `"tool": "get_job"` and the sentinel, injected through a different logger, must make T-5C-B3a's assertion fail. This shows that the exclusion is not broader than intended.

### R2-L3 (required; Ind N-5): the HV label on the HV-Q5 guard

**Fix.** The `unsupported_*` entry that the HV-Q5 (date representation) guard emits must carry `"hv": "HV-Q5"`, not `"HV-Q2"`. The Builder also audits every `unsupported_*` emission in the 5C code, and confirms that each `hv` value matches the HV item whose guard it is (§5).

**Test.** T-5C-R2h is parametrized over the AC-8 guard tests: each asserts the exact `hv` label.

### R2-T1 (required regression test; Ind N-2): current-state predicate parity

**Fix.** Make the Independent Reviewer's parity check (940 comparisons, 0 mismatches) a committed test, `test_phase5c_triage_concept_predicate_parity`. For every mapping-based concept, and a generated set of tenant mappings and settings, the current-state predicate that `find_records` uses must equal the one that `get_activity`'s deriver uses.

**Deferred.** Consolidating the two code paths is P5C-9 (Phase 6).

### Re-review

**Reviews:**
- A fresh Security & Identity Reviewer reviews R2-B1 and R2-L1, and re-runs SC-9 / SR-25 / SR-33.
- The Independent Reviewer passed. A brief Independent check of the R2 diff (R2-L2, R2-L3, R2-T1) is sufficient.
- Then the gates.

**Case 10:** unchanged.

**New regression case SR-36:** no model-visible value equals, or is derivable as, the audit HMAC, across principals (R2-B1).

**Re-review Round 2 (2026-10-07): PASS.**
- The Independent Round-2 check and the fresh Security & Identity Round-2 re-review both passed, with no blocking findings.
- 5C is **COMPLETE**.
- The non-blocking findings of these reviews are logged as P5C-11..P5C-17 in `DEFERRED_DEBT.md` ("Phase 5C close-out"). They are deferrable under D-5-25.
