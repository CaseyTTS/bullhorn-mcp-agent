# Architecture Debt, Deferrals & Decisions Log

- **Owner:** the Architect.
- **Purpose:** this log records roadmap items that were not delivered, deliberate deferrals, roadmap amendments, and binding phase decisions, so that nothing is silently dropped.
- **Baseline for evidence:** commit `4d7da27`, branch `feature/bullhorn-orchestrator`.
- **Architecture plan:** `enter-planning-mode-only-binary-cherny.md` (the user's plan directory).
- **Phase numbers** follow the user-fixed `docs/architecture/ROADMAP.md` revision 3: 4A, 4B, 5, 6, 7, 8, 9 (ROADMAP-AMENDMENT-4). Every item's target was re-checked against that revision on 2026-10-06.
- **Phase 4A close-out (2026-10-06).** Closures and re-targets are recorded in the section "Phase 4A close-out" near the end of this file. Where an item below names an older target, that section supersedes it.

**Status values:**

| Status | Meaning |
|---|---|
| `OPEN-DEFERRED` | Real, not scheduled, and must not be done opportunistically. |
| `OPEN-SCHEDULED` | Assigned to a named future phase. |
| `ACCEPTED` | A binding decision or amendment. |
| `CLOSED` | Delivered, or reviewed and accepted with no action needed. |

---

## Debt

### DEBT-1: Configurable OAuth redirect trusted-origins allowlist not delivered

| Field | Value |
|---|---|
| ID | DEBT-1 |
| Origin phase | Phase 2 (module reorganization) |
| Target phase | **Phase 9 (Auth / Security Closeout)**. The user fixed this on 2026-10-06. It was previously unscheduled. |
| Status | OPEN-SCHEDULED |

**Description.** The roadmap gave Phase 2 the job of extracting the hardcoded `*.bullhornstaffing.com` redirect check into `auth/trusted_origins.py` (`TrustedOriginPolicy`). That class was meant to be a configurable allowlist whose default matches today's behavior. Phase 2 shipped without it. The redirect guard is still hardcoded.

**Evidence:**

- Architecture plan, roadmap Phase 2 bullet (line 335) and §3 layout (lines 79-82).
- No `src/bullhorn_mcp/auth/trusted_origins.py` exists at `4d7da27`.
- `src/bullhorn_mcp/auth/bullhorn_password.py:102`: `if parsed.netloc and "bullhornstaffing.com" in parsed.netloc:`. This gates the update of the regional auth URL when the redirect carries `code`.
- `src/bullhorn_mcp/auth/bullhorn_password.py:113`: `if "bullhornstaffing.com" in parsed.netloc:`. This gates whether a redirect without a code is followed.
- Related defaults that are not part of the guard: `src/bullhorn_mcp/config.py:18-19,49-50` (`auth_url` / `login_url` default to `*.bullhornstaffing.com` and can already be overridden through env).

**Note for whichever phase picks this up.** Both checks are *substring* tests on `netloc`, not suffix or host-equality tests. A host such as `bullhornstaffing.com.attacker.example` would satisfy them.

- A behavior-preserving extraction (the Phase 2 intent) would keep that semantic.
- Tightening it to a proper host-suffix match is a behavior change. It needs its own decision and tests. It must not be slipped into a "pure refactor".

---

### DEBT-2: `config/` package not delivered

| Field | Value |
|---|---|
| ID | DEBT-2 |
| Origin phase | Phase 2 (module reorganization) |
| Target phase | **Phase 9 (Auth / Security Closeout)**. The user fixed this on 2026-10-06. |
| Status | OPEN-SCHEDULED |

**Description.** The §3 layout specified a `config/` package with two modules:

- `config/env.py`: today's `config.py`, with unchanged logic.
- `config/profile_paths.py`: resolves the mapping-profile file location.

Phase 2 did not create it, so `config.py` is still a flat module.

**Interaction with Phases 3 and 4A.** Phase 3 resolves the profile path in `schema/mapping_profile.py::resolve_profile_path()` by reading `BULLHORN_MAPPING_PROFILE`. Phase 4A must decide where versioned profiles are stored (`REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` §2.5) **without** creating the `config/` package. Phase 9 then moves that resolution into `config/profile_paths.py`, with behavior preserved.

**Evidence:**

- Architecture plan §3 layout (lines 70-72) and the "Cross-cutting: config/" line (line 51).
- `src/bullhorn_mcp/config.py:1-51` is a single flat module.
- No `src/bullhorn_mcp/config/` directory exists at `4d7da27`.

---

### DEBT-3: `meta=full` / picklist-options enrichment for MetaDiscovery

| Field | Value |
|---|---|
| ID | DEBT-3 |
| Origin phase | Phase 3 (identified during work-package planning) |
| Target phase | **Phase 4A** (re-targeted from "Phase 4"). This is SB-9: picklist, status and action discovery. |
| Status | OPEN-SCHEDULED |

**Description.** `MetaDiscovery` (Phase 3) parses picklist `options` only when they are already present in the response from the unchanged `BullhornClient.get_meta()`. Today that method sends only `fields=*`. Fetching full options may need `meta=full` or a similar request parameter, to be verified (HV-1).

The user directed that Phase 3 must not modify `get_meta()` and must not add `meta=full` (Phase 3 work package, AM-1).

**Phase 4A requirements:**

- The change must be **additive**: for example, an optional keyword argument whose default reproduces today's exact request.
- It must keep every existing tool's request byte-identical.
- It must be verified before use.
- Once real option payloads flow, the R-10a to R-10c option-shape tests must be extended with captured real payloads (see NB-15).
- 4A value mappings and 4B Note action-type discovery both depend on this enrichment.

**Evidence:**

- `src/bullhorn_mcp/bullhorn/client.py:150-160`. `get_meta` sends `params = {"fields": "*"}` to `/meta/{entity}`.
- `docs/architecture/PHASE3_WORK_PACKAGE.md` §0 AM-1 and §4.7.

---

## Roadmap amendments

### ROADMAP-AMENDMENT-1: `schema/query_builder.py` moves from Phase 3 to Phase 5

| Field | Value |
|---|---|
| ID | ROADMAP-AMENDMENT-1 (implements decision D2) |
| Origin phase | Phase 3 planning, 2026-10-06 |
| Target phase | Phase 5. This is unchanged in revision 3, which lists "structured query builder" under Phase 5. |
| Status | ACCEPTED |

**Description.** `schema/query_builder.py` moves to Phase 5, next to its only consumer, the `find_*` tools.

**Evidence:**

- Architecture plan, roadmap Phase 3 bullet (line 336) and §3 layout (line 110).
- `docs/architecture/PHASE3_WORK_PACKAGE.md` §2 and Context (D2).

### ROADMAP-AMENDMENT-2: Analytics read-model block (roadmap revision 1)

| Field | Value |
|---|---|
| ID | ROADMAP-AMENDMENT-2 |
| Origin phase | Roadmap revision 1, 2026-10-06 |
| Target phase | Numbering superseded by ROADMAP-AMENDMENT-4 |
| Status | ACCEPTED. Its content is preserved and its numbering is superseded. |

**Evidence:** `REQ_RECRUITING_ANALYTICS_READ_MODEL.md`; the architecture plan's first revision section.

### ROADMAP-AMENDMENT-3: Analytics + operational safe-write reconciliation (roadmap revision 2, Architect proposal)

| Field | Value |
|---|---|
| ID | ROADMAP-AMENDMENT-3 |
| Origin phase | Roadmap revision 2, 2026-10-06 |
| Target phase | Numbering superseded by ROADMAP-AMENDMENT-4 |
| Status | ACCEPTED. Its content is preserved. The phase order it proposed (4–17) was replaced by the user's fixed order. |

**Evidence:** `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md`; `CANONICAL_ACTIVITY_VOCABULARY.md`; the architecture plan's second revision section; `ROADMAP.md` §2.3 (rev-2 → final mapping).

### ROADMAP-AMENDMENT-4: User-fixed final phase order (roadmap revision 3)

| Field | Value |
|---|---|
| ID | ROADMAP-AMENDMENT-4 |
| Origin phase | The user's final roadmap injection before Phase 4, 2026-10-06 |
| Target phase | 4A, 4B, 5, 6, 7, 8, 9 |
| Status | ACCEPTED (fixed by the user; binding) |

**Description.** The final phase order is:

| Phase | Name |
|---|---|
| 4A | Tenant Setup & Mapping Management |
| 4B | Notes / Activity Core, which includes the minimal safe-write pipeline for `create_note` |
| 5 | Expanded Recruiting Reads, including `find_*` and `query_builder` |
| 6 | Analytics / Activity Timeline, including composites and the dashboard contract |
| 7 | Broader Writes, which generalizes the pipeline and migrates the legacy upload |
| 8 | Bulk Resume |
| 9 | Auth / Security Closeout (DEBT-1, DEBT-2, OBS-1) |

Every item in this log was re-targeted against this order.

**Evidence:** `ROADMAP.md` §2 (mappings against plan §8 and against revision 2); `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md`; the architecture plan's third revision section.

---

## Decisions

### D1: Library-only canonical support in Phase 3

| Field | Value |
|---|---|
| ID | D1 |
| Origin phase | Phase 3 planning, 2026-10-06 |
| Target phase | Applies to Phase 3. Canonical exposure arrives in Phases 4A onward. |
| Status | ACCEPTED |

**Description.** Phase 3 ships the canonical schema and mapping layer as a library proven by tests only. The 10 existing MCP tools keep frozen schemas and unchanged code. Any future exposure must be through new tools or additive parameters whose default reproduces today's raw output.

**Evidence:** `docs/architecture/PHASE3_WORK_PACKAGE.md` Context (D1), §2, §3 C-1, §5, AC-2, AC-20.

### D3: Catalogs are package resources; tenant profiles are external

| Field | Value |
|---|---|
| ID | D3 |
| Origin phase | Phase 3 planning, 2026-10-06 |
| Target phase | Applies from Phase 3 onward |
| Status | ACCEPTED |

**Description.**

- **Catalogs** are versioned package resources under `src/bullhorn_mcp/mappings/`, loaded with `importlib.resources`.
- **Tenant profiles** are external, read from the path in `BULLHORN_MAPPING_PROFILE`.
- **No default path.** Phase 4A decides versioned-profile storage, within the DEBT-2 constraint.

**Evidence:** `docs/architecture/PHASE3_WORK_PACKAGE.md` Context (D3), §1, §4.5, §0 AM-6, AC-12.

### D4: One canonical activity vocabulary

| Field | Value |
|---|---|
| ID | D4 |
| Origin phase | Roadmap revision 2, 2026-10-06 (RA-9, OW-11) |
| Target phase | Implemented in 4B (event model, `note_created`), 5 (other concepts) and 6 (timeline). It binds every read, write, analytics and dashboard phase. |
| Status | ACCEPTED |

**Description.** Concepts, the event shape and the operation→concept mapping are defined only in `docs/architecture/CANONICAL_ACTIVITY_VOCABULARY.md`. No consumer defines concepts locally. Adding a concept requires a vocabulary version bump and a decision recorded here.

### D5: Safe-write pipeline ships with the first new write

| Field | Value |
|---|---|
| ID | D5 |
| Origin phase | Roadmap revision 2, 2026-10-06 (OW-6, OW-7). Re-targeted by revision 3. |
| Target phase | **4B** (the minimal pipeline, shipped together with `create_note`). **7** (generalized for all other writes). Binding on Phase 8. |
| Status | ACCEPTED |

**Description.**

- Every new write executes the safe-write pipeline (`REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` §3), with a per-operation scope. There is never one broad write permission.
- Phase 4B owns exactly the pieces that `create_note` needs: target resolution, validation, the `note.create` permission scope, idempotency, a bound preview, approval, audit with a correlation ID, and the normalized result.
- `upload_candidate_resume` is the single legacy exception until Phase 7 migrates it, with its default behavior preserved byte-identically (OWG-11).

### D6: Hard Bullhorn verification rule (HV-1)

| Field | Value |
|---|---|
| ID | D6 |
| Origin phase | User's final roadmap injection, 2026-10-06 |
| Target phase | All phases |
| Status | ACCEPTED (binding) |

**Description.** No Bullhorn entity, field, association, status, action type or write behavior is modelled from assumption. Each one is verified against authoritative Bullhorn reference material or against the connected tenant's metadata. Anything not verified is marked unresolved, surfaced in setup, and never guessed. Canonical concepts may be product-defined, but their Bullhorn mappings may not be invented. This generalizes Phase 3's AM-3.

### D7: Compact public tool surface (CT-1)

| Field | Value |
|---|---|
| ID | D7 |
| Origin phase | User's final roadmap injection, 2026-10-06 |
| Target phase | All phases from 4A onward |
| Status | ACCEPTED (binding) |

**Description.** Public MCP tools are few and parameterized: no tool per filter, entity or metric permutation. Capability lists such as RA-11 map onto parameterized tools. Internal services may be richer.

---

## Observations recorded for later triage

### OBS-1: `auth/secrets.py` missing

| Field | Value |
|---|---|
| Target phase | **Phase 9 (Auth / Security Closeout)**, fixed by the user |
| Status | OPEN-SCHEDULED |

The architecture plan's §3 layout (lines 77-78) lists `auth/secrets.py`: a `CredentialSource` Protocol plus `EnvCredentialSource`. That file is not present at `4d7da27`.

---

## Phase 3 review follow-ups (non-blocking)

**Source.** `docs/architecture/PHASE3_REVIEW_TRIAGE.md`.

**Not listed here.** The blocking findings were fixed and delivered in Phase 3, which passed in round 5:

- Round 1: fixes F-1 to F-6.
- Round 2: F-7 and F-8.
- Round 3: F-9 and F-10.
- Round 4: F-11 to F-13.

**Re-targeting.** Every item previously targeted at "Phase 4" is now targeted at **Phase 4A**.

**Evidence references.** File:line references point at the Phase 3 Builder's working tree as reviewed.

### NB-2: Snake_case typos are reported as `unresolved` instead of raising (Reviewer N2)

| Field | Value |
|---|---|
| ID | NB-2 |
| Origin phase | Phase 3 review |
| Target phase | Phase 5 (`find_*`) (unchanged) |
| Status | OPEN-SCHEDULED |

**Description.** `canonical_to_raw` sends any well-formed snake_case name that is neither canonical nor in the profile to `unresolved`, so a typo like `frist_name` does not raise. WP §4.6 is ambiguous on this point.

**Phase 5 must decide** between a strict mode and a "did you mean" hint, and must add a snake_case typo test.

**Evidence:** `src/bullhorn_mcp/schema/translator.py:127-133`.

### NB-3: Redaction residue and name-only sensitivity detection (Reviewer N3)

| Field | Value |
|---|---|
| ID | NB-3 |
| Origin phase | Phase 3 review |
| Target phase | **Phase 4A**, before sample values are exposed by the setup tools |
| Status | OPEN-SCHEDULED |

**Description.** The current behavior complies with WP §4.8. The weaknesses are these:

- **Redaction leaves context.** Values with digits keep their surrounding text.
- **Sensitivity is name-only.** Sensitive fields are identified by name pattern alone.
- **Named sensitive patterns are inert.** Only custom-pattern fields are ever sampled, so patterns such as `ssn` and `dateOfBirth` have no effect.

**Phase 4A must:**

- add label- and meta-flag-based sensitivity;
- make redaction of strings containing digits fully opaque;
- add an adversarial redaction suite;
- preserve F-12 #4 (keep masking regexes Unicode-wide).

See also NB-19.

**Evidence:** `src/bullhorn_mcp/schema/discovery.py:27-60` and `discovery.py:310`.

### NB-4: Canonical `ref:` targets are not validated against existing entities (Reviewer N4)

| Field | Value |
|---|---|
| ID | NB-4 |
| Origin phase | Phase 3 review |
| Target phase | **Phase 4A** (the canonical `user` entity is added there, per RAG-1) |
| Status | OPEN-SCHEDULED |

**Description.** The v1 catalog references `user` and `person`, which do not exist. Once 4A adds `user`, and decides whether to add or retire `person`, the loader must fail on any `ref` that is not a canonical entity.

**Evidence:** `src/bullhorn_mcp/schema/canonical_catalog.py:200-203`.

### NB-5: "Null label" warning fires when the label key is merely absent (Reviewer N5)

| Field | Value |
|---|---|
| ID | NB-5 |
| Origin phase | Phase 3 review |
| Target phase | **Phase 4A** (discovery warnings become user-visible) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must distinguish an absent label (silent fallback, or a lower-severity note) from an explicit `null` label (warning).

**Evidence:** `src/bullhorn_mcp/bullhorn/meta.py:146-148`.

### NB-6: Discovery per-entity `error` carries raw Bullhorn `response.text` (Reviewer N6)

| Field | Value |
|---|---|
| ID | NB-6 |
| Origin phase | Phase 3 review. The behavior is pre-existing in `client.py`. |
| Target phase | **Phase 4A**, before `discover_schema` output is returned through MCP |
| Status | OPEN-SCHEDULED. Re-confirmed in round 5 as unchanged and not worsened. |

**Description.** Discovery errors embed unbounded raw response bodies. Phase 4A must bound and sanitize them. `client.py` stays unchanged unless the 4A work package says otherwise.

**Evidence:** `src/bullhorn_mcp/bullhorn/client.py:49`; the discovery per-entity error capture in `src/bullhorn_mcp/schema/discovery.py`.

### NB-7: A 200 response with a non-object meta body is a warning, not a per-entity error (Reviewer N7)

| Field | Value |
|---|---|
| ID | NB-7 |
| Origin phase | Phase 3 review |
| Target phase | **Phase 4A**, before revalidation reports drive `setup_revalidation_required` and broken-mapping findings |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must define a "meta unusable" state. When it applies, the pipeline sets an error or degraded flag and suppresses the missing/broken computation, so that unusable meta cannot mass-flag every mapping as broken.

**Evidence:** `src/bullhorn_mcp/bullhorn/meta.py:114-116`; `src/bullhorn_mcp/schema/discovery.py:257`.

### NB-9: Duplicate YAML mapping keys silently keep the last value (Reviewer N9)

| Field | Value |
|---|---|
| ID | NB-9 |
| Origin phase | Phase 3 review |
| Target phase | **Phase 4A** (import and hand-edited profiles) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must reject duplicate keys inside the single `schema/yaml_safe.py` choke point, with aggregated errors, and keep C-5.

**Evidence:** `src/bullhorn_mcp/schema/yaml_safe.py` (the single `safe_load` call site after F-9).

### NB-10: `{__class__}`-style placeholders are accepted (Reviewer N10, second half)

| Field | Value |
|---|---|
| ID | NB-10 |
| Status | CLOSED (accepted, no action) |

**Description.** Rendering uses `record.get`, never `str.format`. Reopen this item as blocking if that ever changes.

**Evidence:** `src/bullhorn_mcp/schema/translator.py:222-232`.

### NB-11: Profile file I/O errors propagate as `OSError` (Reviewer round-2 N-b; Builder note 4)

| Field | Value |
|---|---|
| ID | NB-11 |
| Origin phase | Phase 3 re-review |
| Target phase | **Phase 4A** (save, import and export, and versioned storage) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must either document `OSError` as part of the contract or wrap it as `ProfileIOError(SchemaError)`. MCP tools must return a bounded, readable error. Phase 4A must test directory, nonexistent and permission-denied paths in both directions.

**Evidence:** `src/bullhorn_mcp/schema/mapping_profile.py:166-171` and `save`.

### NB-12: Unbounded interpolation in `meta.py` warnings and the sample-source exception text (Reviewer round-2 N-c; Builder note 2)

| Field | Value |
|---|---|
| ID | NB-12 |
| Origin phase | Phase 3 re-review |
| Target phase | **Phase 4A**, together with NB-6 |
| Status | OPEN-SCHEDULED. Re-confirmed in round 5 as unchanged and not worsened. |

**Description.** Phase 4A must route these messages through `describe_value`, cap the warning count, and add a long-name test.

**Evidence:** `src/bullhorn_mcp/bullhorn/meta.py:141,148,150,155,158,162,195,200,205`; `src/bullhorn_mcp/schema/discovery.py:316-317`.

### NB-13: Translator honors an `id` override on a directly constructed profile (Reviewer round-2 N-d)

| Field | Value |
|---|---|
| ID | NB-13 |
| Origin phase | Phase 3 re-review |
| Target phase | **Phase 4A** (mapping edits are the realistic bypass path) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must either make the translator ignore any `id` override or route every edit through validation, and must test direct construction.

**Evidence:** `src/bullhorn_mcp/schema/translator.py:85-90`.

### NB-14: A hostile `dict` subclass can make `raw_to_canonical` raise `KeyError`

| Field | Value |
|---|---|
| ID | NB-14 |
| Status | CLOSED (accepted, no action) |

**Description.** This is outside the threat model: JSON decoding produces only plain `dict`s. Reopen it if records are ever accepted from a non-JSON source.

**Evidence:** `src/bullhorn_mcp/schema/translator.py:211-233`.

### NB-15: Re-validate option-shape coverage against real `meta=full` payloads (Round 3 forward note)

| Field | Value |
|---|---|
| ID | NB-15 |
| Origin phase | Phase 3 re-review, round 3 |
| Target phase | **Phase 4A**, together with DEBT-3 |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must capture anonymized real option payloads, extend R-10a and R-10c with them, and confirm that the F-10 normalization covers every observed type.

**Evidence:** `src/bullhorn_mcp/schema/discovery.py:370-404`; `src/bullhorn_mcp/bullhorn/meta.py:191-208`.

### NB-16: Importing `bullhorn_mcp.schema` also initializes `server` and `tools` (Reviewer round-4 note)

| Field | Value |
|---|---|
| ID | NB-16 |
| Origin phase | Phase 3 re-review, round 4. The behavior is pre-existing (since `4d7da27`). |
| Target phase | Unscheduled. It is considered in Phase 9 if the `config/` refactor (DEBT-2) touches package initialization. |
| Status | OPEN-DEFERRED |

**Description.** `__init__.py:13` (`from . import server`) loads the server whenever any submodule is imported. AC-21 and C-2 still hold. Any fix must preserve tool behavior and the import-order safety this line exists for.

**Evidence:** `src/bullhorn_mcp/__init__.py:5-13`.

### NB-17: The serialization self-check revalidates against the packaged canonical catalog (Reviewer round-5 finding 1)

| Field | Value |
|---|---|
| ID | NB-17 |
| Origin phase | Phase 3 re-review, round 5 |
| Target phase | **Phase 4A** (versioned save and export; catalog v2) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must:

- thread the profile's own `CanonicalCatalog` through `to_yaml_text` / `save`;
- give "catalog mismatch" and "lossy serialization" distinct messages;
- add a test with a custom catalog.

This is critical now that 4A introduces catalog v2 and profile v2.

**Evidence:** `src/bullhorn_mcp/schema/mapping_profile.py:195-199`.

### NB-18: The round-trip equality is type-blind (Reviewer round-5 finding 2)

| Field | Value |
|---|---|
| ID | NB-18 |
| Origin phase | Phase 3 re-review, round 5 |
| Target phase | **Phase 4A** (versioning, diffs and export/import) |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must make the self-check, the round-trip tests and the **version diffs** (TS-5) type-aware, and add a dumper test that turns `True` into `1`.

**Evidence:** `src/bullhorn_mcp/schema/mapping_profile.py:198`.

### NB-19: `redact_sample` has fixed points, which conflict with AC-18's wording (Reviewer round-5 finding 3; grouped with NB-3)

| Field | Value |
|---|---|
| ID | NB-19 |
| Origin phase | Phase 3 re-review, round 5 |
| Target phase | **Phase 4A**, together with NB-3 |
| Status | OPEN-SCHEDULED |

**Description.** Phase 4A must:

- reword AC-18's successor criterion, or make redacted output impossible to confuse with any input (for example `<redacted:N chars>`);
- define how `None` is handled;
- decide whether `'a***@***'` should be treated as an email.

**Evidence:** `src/bullhorn_mcp/schema/discovery.py:29,33`; `PHASE3_WORK_PACKAGE.md` §4.8 and AC-18.

---

## Recruiting analytics read-model gaps in the Phase 3 catalog (RAG)

**Sources.** `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §3, `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` §2 and §4, and `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` §2.

**Phase 3 is not reopened.** All Bullhorn representations named below must satisfy **HV-1 (D6)**.

**Evidence** refers to `src/bullhorn_mcp/mappings/canonical_schema.yaml` (catalog v1) unless stated otherwise.

### RAG-1: There is no canonical `user` entity, so recruiter, owner and author IDs cannot resolve

| Field | Value |
|---|---|
| ID | RAG-1 |
| Origin phase | Roadmap revisions (RA-4, RA-7, RA-8; OW-4; TS-1 `job.primary_recruiter_id`) |
| Target phase | **4A** (canonical `user` entity, product-defined; NB-4). **4B** (recruiter identity resolution, SB-7). **5** (extended resolution). |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:32,54,65,107,123,145` (`ref: user`); `:132-133` (`ref: person`).

### RAG-2: There is no canonical `job.priority` field

| Field | Value |
|---|---|
| ID | RAG-2 |
| Origin phase | RA-5, RA-10d; OW-7; TS-1 example |
| Target phase | **4A** (canonical field plus the tenant field and value mapping; a standard mapping only if verified). **5** (reads and grouping). **7** (`update_job_priority`). |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:35-54`.

### RAG-3: The primary-recruiter association is undefined

| Field | Value |
|---|---|
| ID | RAG-3 |
| Origin phase | RA-4, RA-10c; OW-9; TS-1 (`JobOrder.owner.id` → `job.primary_recruiter_id` is the user's illustrative example) |
| Target phase | **4A** (canonical `job.primary_recruiter_id` plus its tenant mapping). **5** (reads). |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:54`.

### RAG-4: Appointment interview semantics are missing

| Field | Value |
|---|---|
| ID | RAG-4 |
| Origin phase | RA-3, RA-10b; OW-5; TS-2 |
| Target phase | **4A** (the value-mapping mechanism for classification and state). **5** (verified fields, concept derivers, verified attendee reads). **6** (multi-attendee composites). |
| Status | OPEN-SCHEDULED |

**Description.** The catalog has no interview classification, no outcome or status, no cancellation indicator, no reschedule link or history, and no to-many attendees. An unsupported state is reported as unsupported, not approximated.

**Evidence:** `canonical_schema.yaml:109-123`.

### RAG-5: Submission status history is missing, so `client_submission` cannot be dated

| Field | Value |
|---|---|
| ID | RAG-5 |
| Origin phase | RA-2, RA-10a/g |
| Target phase | **5** |
| Status | OPEN-SCHEDULED |

**Description.** Falling back to the submission's created date is open question Q-2. Candidate and job history are tracked as OWG-8.

**Evidence:** `canonical_schema.yaml:56-65`.

### RAG-6: There is no canonical offer concept or representation

| Field | Value |
|---|---|
| ID | RAG-6 |
| Origin phase | RA-6, RA-8, RA-10e; TS-2 |
| Target phase | **4A** (value mappings once a representation is verified or confirmed by the administrator). **5** (reads and offer concepts). |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml` (no offer key).

### RAG-7: The canonical placement lacks recruiter, client and submission links

| Field | Value |
|---|---|
| ID | RAG-7 |
| Origin phase | RA-7, RA-8, RA-10f |
| Target phase | **5** |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:67-80`.

### RAG-8: There is no datetime coercion or timezone normalization

| Field | Value |
|---|---|
| ID | RAG-8 |
| Origin phase | RA-10 / OW-4 date ranges. Phase 3 deliberately left coercion out of scope. |
| Target phase | **4A** (SB-4: tenant `reporting_timezone` plus an opt-in coercion primitive). **4B** (SB-5: date-range semantics, first used by `get_notes`). |
| Status | OPEN-SCHEDULED |

**Evidence:** `PHASE3_WORK_PACKAGE.md` §2.

### RAG-9: Profile v1 has no place for mapping records, value mappings or business rules

| Field | Value |
|---|---|
| ID | RAG-9 |
| Origin phase | RA-2..9; OW-3; TS-1 (mapping-record provenance), TS-2, TS-5 |
| Target phase | **4A** (profile format v2: mapping records with provenance, value mappings, business rules, settings, versioning). v1 profiles still load. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/schema/mapping_profile.py` (`_TOP_KEYS` and the version checks).

### RAG-10: The canonical `note` lacks association links and action semantics

| Field | Value |
|---|---|
| ID | RAG-10 |
| Origin phase | OW-2, OW-4, OW-5 |
| Target phase | **4B** (canonical Note representation, verified association mechanics for read and write, and the `action_semantic` derivation) |
| Status | OPEN-SCHEDULED |

**Description.** The v1 note has `person_id` (pointing to the non-existent `person`), `author_id`, `job_id`, `action`, `body` and `is_deleted`. It has no links to a client contact, client corporation, placement or submission, and it cannot represent multiple associations. How Bullhorn associates a note with several records is to be verified.

**Evidence:** `canonical_schema.yaml:125-135`; `PHASE3_UNVERIFIED_MAPPINGS.md`.

---

## Operational safe-write gaps (OWG)

**Sources.** `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` §2.2 and `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` §2.4.

**Evidence base.** The Phase 1 cross-cutting layer, as built.

### OWG-1: Note action types have no source of record and no profile slot

| Field | Value |
|---|---|
| ID | OWG-1 |
| Origin phase | OW-3; TS-2 |
| Target phase | **4A** (value-mapping mechanism and storage). **4B** (Note action discovery, validation and write enforcement). |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:130`; `mapping_profile.py` `_TOP_KEYS`.

### OWG-2: The permission layer is a default-allow stub

| Field | Value |
|---|---|
| ID | OWG-2 |
| Origin phase | Phase 1 (by design); OW-7 |
| Target phase | **4B** (a real policy check with the `note.create` scope, extended additively, preserving default outcomes for the 10 original tools). **7** (every other operation scope, plus the `raw_query` scope). **6** (read-scope restriction for the dashboard agent, on the same engine). |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/crosscutting/permissions.py:21-27`.

### OWG-3: The approval gate is unbound, never expires, executes nothing and has no tools

| Field | Value |
|---|---|
| ID | OWG-3 |
| Origin phase | Phase 1 (by design); OW-6; TS-4 |
| Target phase | **4A** (binding tokens to the diff hash, actor and expiry, for configuration changes). **4B** (the same primitive for the `create_note` preview hash, plus `list/approve/reject_pending_operation`). **7** (generalized). Persistence: Q-W6. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/crosscutting/approval.py:13-64`.

### OWG-4: The dry-run preview is generic and not bound to the commit

| Field | Value |
|---|---|
| ID | OWG-4 |
| Origin phase | Phase 1; OW-6, OW-9 |
| Target phase | **4B** (`create_note`). **7** (generalized). |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/crosscutting/dryrun.py:4-19`.

### OWG-5: Audit lacks actor, correlation, payloads, approval state, response and free-text redaction

| Field | Value |
|---|---|
| ID | OWG-5 |
| Origin phase | Phase 1; OW-10; TS-4 and TS-5 |
| Target phase | **4A** (configuration-change audit with actor, correlation ID and diff). **4B** (write audit). **7** (generalized). Free-text redaction: Q-W7. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/crosscutting/audit.py:16-24,55-81`.

### OWG-6: Each tool hand-rolls the write sequence

| Field | Value |
|---|---|
| ID | OWG-6 |
| Origin phase | Phases 1–2; OW-6 |
| Target phase | **4B** (a minimal pipeline used by `create_note`). **7** (a generalized `SafeWritePipeline`). See D5. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/tools/candidates.py:211-319`.

### OWG-7: There are no entity create, update or association primitives in the client

| Field | Value |
|---|---|
| ID | OWG-7 |
| Origin phase | Architecture plan §1 |
| Target phase | **4B** (note creation plus the verified association mechanics). **7** (generic `create()` / `update()`). Both are additive; existing requests stay byte-identical. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/bullhorn/client.py`; architecture plan §1.

### OWG-8: Candidate and job status history are not represented

| Field | Value |
|---|---|
| ID | OWG-8 |
| Origin phase | OW-5 |
| Target phase | **5** (reads, if verified, plus the `candidate_status_changed` / `job_status_changed` derivers). If no history representation is verified, these are reported as unsupported. |
| Status | OPEN-SCHEDULED |

**Evidence:** `canonical_schema.yaml:26,41`.

### OWG-9: There is no idempotency or duplicate prevention

| Field | Value |
|---|---|
| ID | OWG-9 |
| Origin phase | OW-8; SB-10 |
| Target phase | **4B** (key, ledger and the note duplicate probe). **7** (per-operation rules for every other write, including resume upload). **8** (batch). Persistence: Q-W5. |
| Status | OPEN-SCHEDULED |

**Evidence:** Absent from `crosscutting/` and from `tools/candidates.py:189-319`.

### OWG-10: There is no actor identity

| Field | Value |
|---|---|
| ID | OWG-10 |
| Origin phase | OW-7, OW-10, OW-4; TS-4 and TS-5 ("actor/agent" on versions) |
| Target phase | **4A** (needed first for configuration-change approval and version history). Q-W9 and Q-S2. |
| Status | OPEN-SCHEDULED |

**Evidence:** `crosscutting/permissions.py:21`; `crosscutting/audit.py:55-61`.

### OWG-11: `upload_candidate_resume` sits outside the safe-write pipeline

| Field | Value |
|---|---|
| ID | OWG-11 |
| Origin phase | OW-6; D5 |
| Target phase | **7** (migrate it onto the generalized pipeline with byte-identical default behavior, plus resume-upload idempotency). Q-W8. |
| Status | OPEN-SCHEDULED |

**Evidence:** `src/bullhorn_mcp/tools/candidates.py:189-196`.

---

## Phase 4A close-out (2026-10-06)

**Basis.** The Phase 4A independent review passed with no blocking findings. The gates were green: 8176 passed / 6 skipped, ruff, mypy, wheel, and 25/25 regression.

**Authority.** This table applies `PHASE4A_WORK_PACKAGE.md` §5 and the 4B package. It **supersedes** the targets shown in the entries above.

| Item | Disposition |
|---|---|
| RAG-9 | **CLOSED** (profile v2) |
| RAG-2 | **CLOSED for the canonical field and mapping part.** Reads remain in 5 and the write in 7. |
| RAG-3 | **CLOSED for the mapping part.** Reads remain in 5. |
| RAG-8 | **CLOSED for the setting and UTC-primitive part.** Date-range semantics go to 4B. Conversion of values into the reporting zone, and `tzdata`, go to 5. |
| OWG-1 | **CLOSED for the value-mapping mechanism.** Note action discovery and enforcement go to 4B. |
| OWG-3, OWG-5 | **CLOSED for the configuration-change parts.** The write parts go to 4B. |
| OWG-10 | **CLOSED** (`BULLHORN_MCP_ACTOR`) |
| NB-6, NB-12 | **CLOSED at the MCP boundary.** `meta.py` / `discovery.py` are unchanged. |
| NB-7 | **CLOSED** (`meta_unusable` suppression) |
| NB-9 | **CLOSED for 4A paths** (`tenant/yaml_strict.py`) |
| NB-11 | **CLOSED for store paths** (`SetupStoreError`) |
| NB-13 | **CLOSED by construction** (v2 → v1 only via `from_dict`) |
| NB-18 | **CLOSED for v2** (type-tagged `diff_hash`) |
| DEBT-3, NB-15 | **Re-targeted to Phase 5.** The 4B work package (D-4B-11) does not need `meta=full`, because HV-A2 verified that `options` is not limited to `meta=full`. |
| NB-3, NB-19 | **Re-targeted to Phase 5**, before any tool exposes samples. 4A exposes none (D-4A-12). |
| NB-5 | **Re-targeted to Phase 5** |
| NB-4, RAG-1 | **Re-targeted to Phase 5.** The canonical `user` entity was not added: the Phase 3 test pins 10 entities (D-4A-13). |
| NB-17 | **Re-targeted to Phase 5.** It is not triggered: 4A uses only the packaged catalog. |

## Phase 4A review follow-ups (non-blocking)

**Source.** The Phase 4A independent review, 2026-10-06.

**IDs** use the prefix `P4A-` to avoid colliding with the Phase 3 NB numbers.

| ID | Finding | Evidence | Target | Status |
|---|---|---|---|---|
| P4A-1 | `save_proposal(create=True)` is not atomic. A corrupt or empty proposal file makes `compute_setup_state` report `connected_setup_required` / `store:unusable` even when an active version exists. It fails closed. | `tenant/store.py:225-234`; `tenant/state.py:155-160` | 7 (store hardening with pipeline generalization). 4B's own `writes/` files must be written atomically from the start. | OPEN-SCHEDULED |
| P4A-2 | Concept conflicts are keyed on `(bullhorn_field, value)` without the entity, which produces false cross-entity conflicts. This matches the literal text of WP §1.3. | `tenant/profile_v2.py:424` | 5 (when concept mappings for several entities arrive) | OPEN-SCHEDULED |
| P4A-3 | `activity_concepts.yaml` has 16 IDs, while D-4A-15 says 15. The vocabulary lists 16, so this is a spec miscount. | `PHASE4A_WORK_PACKAGE.md` D-4A-15 | Corrected by a note in that file | CLOSED |
| P4A-4 | A v2 document with one active and one inactive record for the same (entity, field) is rejected, which is stricter than §1.3 intends. | `tenant/profile_v2.py:610-613` | 5 | OPEN-SCHEDULED |
| P4A-5 | A `rest_url` change is detected only with `check_connection=True`. The `discover_schema` fingerprint is never compared, and versions committed before any discovery have a null fingerprint. | `tenant/state.py:201-207` | 4B mitigation: write gating always uses a live connection check (D-4B-16). Remainder in 5. | OPEN-SCHEDULED |
| P4A-6 | The wording of the HV doc is wrong: `required` / `read_only` changes go to `changed_fields`, which **does** set `drift_unresolved` and so triggers revalidation. The behavior matches the spec; the wording does not. | `PHASE4A_HV_VERIFICATION.md` HV-A1 row | Correct the wording when that doc is next touched (5) | OPEN-SCHEDULED |
| P4A-7 | A stale `commit.lock` left after a hard kill blocks commits. The `discover_schema` / validate discovery writes are not taken under the lock, so a concurrent commit can lose a `drift_unresolved` update. | `tenant/store.py:297-309`; `tools/setup.py:232,433` vs `tenant/changes.py:654` | 7 | OPEN-SCHEDULED |
| P4A-8 | If the history append fails after `set_active`, the active version is left with no history line. | `tenant/changes.py:635-649` | 7 | OPEN-SCHEDULED |
| P4A-9 | Read-only code paths create empty store directories. | `tenant/store.py:86-96` | 7 | OPEN-SCHEDULED |
| P4A-10 | The `setup_status` detail echoes up to 300 characters of the auth error. The source `AuthenticationError("Invalid login response: {data}")` could contain `BhRestToken`. The legacy `connection_status` already echoes it unbounded, so this is not a regression. | `auth/bullhorn_password.py:201`; `tools/setup.py` (`setup_status`) | **9**, root-cause fix in the auth message, covering `connection_status` and `setup_status`. Interim: every new 4B output applies token redaction (4B §1.5). | OPEN-SCHEDULED |
| P4A-11 | Without `tzdata`, only `"UTC"` can be set on Windows. This was decided in D-4A-11. | `tenant/timeutil.py:48` | 5 (`tzdata` together with timezone conversion) | OPEN-SCHEDULED |
| P4A-12 | Import can read any readable `.yaml` / `.yml` file outside the store, and its error text may echo truncated values. | `tenant/store.py:337-386` (`check_external_path` / `read_external_text`) | 9 (security closeout: path policy / allowlist) | OPEN-SCHEDULED |

## Phase 4B review follow-ups (non-blocking)

**Source.** The Phase 4B review, 2026-10-07. The triage is in `PHASE4B_WORK_PACKAGE.md`, "4B Review Triage".

**Promoted to blocking (not listed below).** The Reviewer's NB-1 to NB-6 and the NB-9 line-ending item were promoted to blocking B-2 to B-7.

| ID | Finding | Evidence | Target | Status |
|---|---|---|---|---|
| P4B-A1-1 | `setup_status` does not list the `note_action` mapping requirement under `notes.create`. It is enforced and reported only at the point of use (Amendment A1/C2). | `tenant/capabilities.py` (state-only entries) | 7 (generalize capability requirement aggregation without breaking 4A's `test_rows`) | OPEN-SCHEDULED |
| P4B-3 | When different previews of the same payload are confirmed concurrently, the losers receive `in_doubt`, which is misleading, and their operations are consumed. Exactly one `PUT` is still made, so this is safe. | `writes/pipeline.py` (confirm path), `writes/ledger.py` | 7 (a distinct `in_progress` / `duplicate_of` status). B-3 must not make this worse. | OPEN-SCHEDULED |
| P4B-4 | `confirm_write` does not re-run the legacy `permissions.check("create_note","write")`. §1.4 stage 7 does not require it, and the check is default-allow today. | `writes/pipeline.py` (confirm path) | 7 (re-run the whole permission stage when the policy engine is generalized) | OPEN-SCHEDULED |
| P4B-5 | `get_notes` sends no `orderBy`, because the direction syntax is unverified (HV-B5), and it warns about this. "Last N notes" therefore has no guaranteed order. | `notes/reads.py` | 5 (verify the `orderBy` syntax under HV-1 together with `find_*` / `query_builder`) | OPEN-SCHEDULED |
| P4B-6 | `start` is capped at 100,000, a value the spec does not state. | `notes/reads.py` | Accepted. It is documented in the 4B HV/README notes. | CLOSED |
| P4B-7 | The 4A `parse_value_target` non-dict error message now also lists `note_action`. This is additive, and no test pins the message. | `tenant/profile_v2.py` | Accepted | CLOSED |

## Phase 4B close-out (2026-10-07)

**Basis.** The Phase 4B re-review passed with no blocking findings. All gates were green, and the regression suite passed 25/25.

**Closed by 4B** (per `PHASE4B_WORK_PACKAGE.md` §5): RAG-10; OWG-1 (Note action discovery and enforcement); OWG-2 (the `note.create` scope); the minimal parts of OWG-3, OWG-4, OWG-5, OWG-6 and OWG-9; OWG-7 (via `bullhorn/writes.py`); SB-5; SB-12.

**Partial:**
- SB-7: author resolution returns `unsupported_filter` until HV-B5 and HV-B9 are resolved.
- SB-8: name resolution moves to 5.

**Already logged above.** P4B-A1-1 is in "Phase 4B review follow-ups", targeted at 7.

**External dependency.** `create_note` is code-complete but **disabled in production** until HV-B11 is verified. See `ROADMAP.md` §5 EXT-1 and `PHASE4B_HV_VERIFICATION.md`.

| ID | Finding | Evidence | Target | Status |
|---|---|---|---|---|
| **P4B-8** | Secrets are redacted *before* comments are scrubbed. A comment containing a secret-like pattern (for example `password=Spring2024 …`) that a 400 body echoes back survives scrubbing, so partial comment text reaches the journal and audit (this violates D-4B-8 / B-5 clause 1). This is unreachable while HV-B11 is unresolved. **Fix:** scrub the comment from the raw body *before* redacting. **PRECONDITION: this must be CLOSED before `HV_B11_VERIFIED` is ever set to `True`.** | `bullhorn/writes.py:167` (redact) runs before `writes/pipeline.py:782` (scrub) | Precondition for EXT-1. It must be fixed in the same change that enables HV-B11, at the latest Phase 7. | OPEN-SCHEDULED (blocking for the EXT-1 enablement) |
| P4B-9 | Redaction misses encodings outside the B-5 list: double-escaped JSON, HTML entities (`&quot;`), double URL-encoding (`%2522%253A`), `%20` around the separator, XML `<BhRestToken>`, and an escaped quote inside a quoted value. | `bullhorn/writes.py` (redaction helper) | 9 (security closeout; together with the P4A-10 root cause) | OPEN-SCHEDULED |
| P4B-10 | A refresh after a 401 that fails with a non-`AuthenticationError` exception (for example `httpx.ConnectError`) maps to `in_doubt`, leaving the key stuck `pending` although no write occurred. | `writes/pipeline.py` (create/refresh exception mapping, near `:689-701`) | 7 (classify transport errors raised before send as `failed`) | OPEN-SCHEDULED |
| P4B-11 | A `SetupStoreError` raised from `ledger.finish` or `journal.record` after a successful `PUT` makes the tool return `ERROR`: no `committed` journal line is written and the ledger stays `pending`. This is duplicate-safe, but one transition goes unjournaled. | `writes/pipeline.py:818+` | 7 (journal-after-write durability and reconciliation) | OPEN-SCHEDULED |
| P4B-12 | In production the HV-B11 guard (stage 3) runs before the permission stage. With the scope unset, `create_note` therefore returns `rejected_validation` / `unsupported_association` rather than AC-14's literal `denied`. The `denied` path is tested with the guard off. | `writes/pipeline.py` (stage order) | Accepted: the guard order is correct, because it refuses earlier and without HTTP. Re-check AC-14 literally once EXT-1 is enabled. | CLOSED (accepted) |
