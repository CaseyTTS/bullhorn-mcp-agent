# Requirement: Tenant Setup & Mapping Management

| | |
|---|---|
| **Status** | Recorded 2026-10-06 from the user's final roadmap injection before Phase 4. This is a specification and roadmap record only. The Phase 4A work package is requested separately. |
| **Owner phase** | Phase 4A (`ROADMAP.md`) |
| **Companion docs** | `ROADMAP.md` (the authoritative phase list and ownership), `CANONICAL_ACTIVITY_VOCABULARY.md` (the shared concepts), `REQ_RECRUITING_ANALYTICS_READ_MODEL.md`, `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md`. This document does not compete with those; it holds only the setup and configuration material. |
| **Phase 3 relation** | Phase 3 (catalogs, `MappingProfile` v1, translator, `MetaDiscovery`, `SchemaDiscoverer`, `build_draft_profile`, `SchemaContext`) is complete and is **not** deconstructed. Phase 4A builds on it additively. v1 profiles keep loading, and the Phase 3 APIs keep their contracts. |

---

## 1. The requirement (faithful transcription, structured)

**TS-1: Structured first-run setup is mandatory.**

Credentials alone do not complete setup. The flow is:

```
credentials configured → Bullhorn connection verified → tenant metadata discovered → verified standard mappings applied → custom/unknown/ambiguous fields identified → administrator resolves mappings → tenant business values/workflows mapped → configuration validated → tenant profile versioned and saved → setup_complete
```

- Setup must support explicit field relationships. For example:
  - Bullhorn `JobOrder.customText12` → canonical `job.priority`;
  - Bullhorn `Candidate.customText7` → canonical `candidate.some_field`;
  - Bullhorn `JobOrder.owner.id` → canonical `job.primary_recruiter_id`.
- Mappings are structured configuration, not prompt text.
- Each mapping preserves:
  - the canonical entity;
  - the canonical field;
  - the Bullhorn entity;
  - the Bullhorn field or path;
  - the mapping type;
  - the source (`standard`, `discovered` or `administrator`);
  - the tenant;
  - the validation state;
  - a created timestamp;
  - an updated timestamp.

**TS-2: Business-value mapping is part of setup.**

Examples (illustrative only):
- `JobSubmission.status = "Presented"` → canonical `client_submission`;
- `Appointment.type/status` = a tenant value → a canonical interview event;
- `Note.action` = a tenant value → a canonical note / action meaning;
- a raw job priority value → a canonical priority meaning.

The actual Bullhorn values must be discovered or explicitly confirmed. They are never guessed.

**TS-3: Setup remains editable after the first connection.**

Supported operations:
- view, search, add, change, and remove or deactivate mappings;
- remap canonical fields;
- edit business-value mappings;
- rediscover metadata;
- compare the saved configuration against current tenant metadata;
- identify broken or stale mappings;
- validate;
- save a new profile version;
- export and import;
- roll back to a prior valid version.

The conceptual operations are `get_mapping_profile()`, `discover_schema()`, `compare_mapping_profile()`, `set_field_mapping(...)`, `remove_field_mapping(...)`, `set_value_mapping(...)`, `validate_mapping_profile()` and `save_mapping_profile()`. The public MCP surface stays compact: these are grouped behind a small setup/configuration tool family.

**TS-4: Mapping changes are controlled.**

```
proposed change → validate against Bullhorn metadata → show old mapping → show proposed new → identify conflicts/dependencies → administrator approval → save new version → activate → audit
```

An active mapping is never silently overwritten.

**TS-5: Versioning, rollback and revalidation.**

- Each version preserves: whether it is the active version, the previous versions, the changed date, the actor or agent, the mapping diff, and the validation result.
- Discovery can be rerun later. It reports newly discovered fields, removed fields, changed metadata, broken mappings, newly unmapped fields, and changed status or action values where detectable.
- Uncertain changes are never reinterpreted automatically.

**TS-6: Credentials and mappings are separate.**

- Rotating credentials must not destroy mappings.
- Changing mappings must not require recreating auth.

**TS-7: Setup status states.**

The states are `disconnected`, `connected_setup_required`, `setup_in_progress`, `setup_valid`, `setup_invalid` and `setup_revalidation_required`.

Domain agents must not operate against mappings that are known to be invalid for the capability they use. Instead, the missing setup requirements are returned explicitly.

**HV-1: Hard Bullhorn verification rule (binding, all phases).**

- No physical Bullhorn entity, field, association, status, action type or write behavior may be modelled from assumption.
- Each must be verified against one of:
  1. authoritative Bullhorn API / reference material available to the project; or
  2. the actual connected tenant's metadata or configuration.
- Anything not verified is marked **unresolved**, surfaced during setup, and never guessed.
- Canonical recruiting concepts may be product-defined. Their Bullhorn mappings may not be invented.

**CT-1: Compact tool-surface rule (binding, all phases).**

- Keep the public MCP surface small: no tool per filter, entity or metric permutation.
- Prefer parameterized domain tools, such as `find_interviews(filters...)`, `get_notes(filters...)`, `create_note(...)` and `get_recruiting_metrics(scope, filters..., metrics=[...])`.
- Internal services may be much richer.
- The earlier long `get_*` lists (RA-11) describe *required capabilities*. The public surface is their compact, parameterized form.

**SB: Required supporting behaviors.** Each has a roadmap owner in `ROADMAP.md` §1.3:

| ID | Behavior |
|---|---|
| SB-1 | Pagination and complete-result retrieval |
| SB-2 | Bullhorn result limits |
| SB-3 | Retries and rate limiting |
| SB-4 | Timezone normalization |
| SB-5 | Date-range semantics |
| SB-6 | Inactive and soft-deleted record handling |
| SB-7 | Recruiter identity resolution |
| SB-8 | Client and contact identity resolution |
| SB-9 | Picklist, status and action discovery |
| SB-10 | Duplicate prevention and idempotency |
| SB-11 | Partial failures in bulk operations |
| SB-12 | Source-record provenance |
| SB-13 | Tenant-specific rules kept separate from generic code |

---

## 2. Architect analysis (Phase 4A design inputs, not yet a work package)

### 2.1 What Phase 3 already provides and Phase 4A reuses unchanged

| Phase 3 asset | Reused for |
|---|---|
| `MetaDiscovery` (metadata-only, cached) | Discovery and rediscovery in TS-1 and TS-5 |
| `SchemaDiscoverer.discover()` (`standard_present`, `standard_missing_in_tenant`, `mappings_broken`, `custom_mapped`, `custom_unmapped`, `other_unrecognized`) | The "identify custom/unknown/ambiguous" step and the broken/stale report |
| `build_draft_profile` | Drafting the initial administrator worklist |
| The `MappingProfile` v1 validators (aggregated errors, F-1 to F-13 hardening, the `yaml_safe` choke point, lossless `to_yaml_text`) | The base layer of profile v2 |
| The `FieldTranslator` resolution order (custom > standard override > default) | Applying active mappings |
| `load_active_profile` / `SchemaContext` (never raise) | The status computation in TS-7 |

### 2.2 Mapping record model (profile format v2: additive)

Profile v1 stores only `canonical → target` per entity. TS-1 requires a **mapping record** with provenance. The design direction is a versioned **profile format v2** that adds the records without breaking v1 loading (RAG-9):

```yaml
version: 2
tenant: {id: <tenant identity>, label: ...}      # tenant identity source: Q-S1
profile_version: 7                               # monotonically increasing
field_mappings:
  - canonical: {entity: job, field: priority}
    bullhorn: {entity: JobOrder, path: customText12}   # path grammar = Phase 3 target forms
    mapping_type: raw | nested | template
    source: standard | discovered | administrator
    validation: {state: valid | unresolved | broken | stale, checked_at: ..., detail: ...}
    active: true
    created_at: ...
    updated_at: ...
value_mappings:
  - concept: client_submission                   # vocabulary concept or canonical attribute meaning
    bullhorn: {entity: JobSubmission, path: status}
    values: ["<tenant value confirmed by admin or discovered>"]
    source: discovered | administrator
    validation: {...}
    created_at: ...
    updated_at: ...
business_definitions: {...}                      # rules not expressible as value lists (vocabulary §2)
settings: {reporting_timezone: ..., ...}
```

**Notes on the model:**

- **Example paths are not facts.** `JobOrder.customText12`, the `JobSubmission.status` value "Presented", and similar paths are the user's *illustrative* examples. Any real path is admitted only when it is verified (HV-1).
- **The grammar is reused.** The mapping-path grammar reuses the Phase 3 target forms (raw name, `{rawName}` template, `{field, key}` nested) and their hardened validators. `JobOrder.owner.id` is the nested form `{field: owner, key: id}`.
- **Canonical fields are product-defined.** Canonical fields used by setup are added to the canonical catalog (catalog v2, additive). Examples are `job.priority`, `job.primary_recruiter_id` and a `user` entity. Their **Bullhorn standard mappings** are added only when verified. Otherwise they appear in setup as `unresolved`, awaiting an administrator mapping.
- **Translator compatibility.** The translator consumes the *active* field mappings, which are equivalent to the v1 `standard`/`custom` semantics. v1 profiles are migrated on load into an in-memory v2 view with `source: administrator` and `validation: unknown`. They are never rewritten on disk without an explicit save.

### 2.3 Setup state machine (TS-7)

| State | Entered when |
|---|---|
| `disconnected` | Credentials are missing, or the connection fails. |
| `connected_setup_required` | The connection is verified, but there is no active valid profile. |
| `setup_in_progress` | A draft exists and is not yet validated and activated. |
| `setup_valid` | The active version validated against the current metadata. |
| `setup_invalid` | The active version failed validation, for example because of broken required mappings. |
| `setup_revalidation_required` | Any of these happened: the catalog version changed, rediscovery found differences, the profile was imported, or the time since the last validation exceeded a policy threshold (Q-S4). |

**Capability gating.** Each capability (for example `notes.read`, `notes.create`, `analytics.client_submissions`) declares the mappings and definitions it requires. Its tools call one gate function, which returns `{ok, missing_requirements}`. A capability whose required mappings are invalid refuses to run and lists exactly what is missing (TS-7).

### 2.4 Controlled change workflow (TS-4)

```
propose change → validate (against cached/fresh tenant meta + catalog + dependency graph) → diff (old vs new, with dependent capabilities and value mappings listed) → approval (admin; the Phase 1 approval gate is extended with token-to-diff-hash binding, actor and expiry) → save new version (immutable) → activate (atomic pointer switch, hot reload) → audit (actor, correlation ID, diff, validation result)
```

- This reuses the same approval and audit primitives that the Phase 4B write pipeline uses, so it is built once.
- **Never a silent overwrite.** Any change to an active mapping creates a new version. The prior version stays retrievable.

### 2.5 Versioning, rollback and revalidation (TS-5)

- **Storage.** Immutable version files plus an active-version pointer, written with the Phase 3 atomic-write discipline. The location is decided in the 4A work package; it relates to DEBT-2, which stays in Phase 9 per the user.
- **Rollback.** Rollback activates a prior version after revalidating it against current metadata. A prior version that is now invalid can be activated only with an explicit administrator override, which is audited.
- **Revalidation report.** Discovery is rerun and diffed against the meta snapshot stored with the active version. The report covers:
  - new fields;
  - removed fields;
  - changed metadata (type and label);
  - broken mappings;
  - newly unmapped fields;
  - changed picklist, status or action values, where detectable (needs DEBT-3).

  Nothing is reinterpreted automatically: every finding becomes a proposed change awaiting the administrator.

### 2.6 Credential separation (TS-6)

Credentials stay in the environment / `BullhornConfig`. The profile is keyed by **tenant identity**, not by credentials, so a credential rotation that resolves to the same tenant keeps its mappings. How tenant identity is derived is to be verified (Q-S1); for example, a corporation identifier from the REST login response.

### 2.7 Compact setup tool family (CT-1)

The proposed public surface is about six tools. The final names and grouping are decided in the 4A work package.

| Tool | Purpose |
|---|---|
| `setup_status` | The TS-7 state plus missing requirements per capability. |
| `discover_schema` | Discovery or rediscovery, metadata-only by default. Returns the diff against the active version. |
| `get_mapping_profile` | View, search and history (`view`, `search`, `versions`, `diff`). |
| `propose_mapping_changes` | A batch of set/remove/deactivate operations on field and value mappings. It validates, diffs and returns an approval token. |
| `commit_mapping_changes` | Approve, save a version and activate (or reject). |
| `manage_mapping_profile` | `validate`, `export`, `import` (which forces revalidation) and `rollback`. |

The conceptual operations in TS-3 map onto these tools as parameters.

`connection_status` (one of the 10 original tools) gains its setup state only additively. Its existing fields are unchanged.

### 2.8 Phase 4A boundaries and hand-offs

| Item | Owner |
|---|---|
| Generic setup, the mapping-record model, value-mapping mechanics, versioning, rollback, revalidation, status states, capability gating, the controlled-change workflow, actor identity, the tenant timezone setting and datetime coercion primitive, picklist/value discovery (DEBT-3) | **4A** |
| Note action-type *discovery and validation for writes*, built on the 4A value-mapping mechanism | **4B** |
| The value mappings for submissions, interviews and offers, entered through the 4A mechanism once Phase 5 adds the needed canonical fields and reads | **Phase 5**. Each catalog growth triggers `setup_revalidation_required`. |

## 3. Open questions for the user

| ID | Question |
|---|---|
| Q-S1 | **Tenant identity.** What should key a tenant profile? For example, a Bullhorn corporation identifier obtained at login (to be verified), or an administrator-assigned tenant name. |
| Q-S2 | **Who is an administrator?** How is the approving administrator for mapping changes identified: a configured list of actors, or anyone holding a `setup.admin` scope? |
| Q-S3 | **Version retention.** How many prior profile versions should be kept: all of them, the last N, or time-bounded? |
| Q-S4 | **Revalidation trigger.** Should revalidation be required only on events (catalog upgrade, import, detected drift), or also on a schedule, for example after N days? |
| Q-S5 | **Deactivation.** Is a "deactivated" mapping (kept, but not applied) required as distinct from removal? The recommendation is yes, for auditability. |
| Q-S6 | **Partial setup.** May capabilities whose own requirements are satisfied operate while other capabilities are still `setup_in_progress`? The recommendation is yes, through per-capability gating. Or must the whole tenant reach `setup_valid` first? |
