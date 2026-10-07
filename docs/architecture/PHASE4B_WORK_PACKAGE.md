# Phase 4B Work Package: Notes / Activity Core

- **Status:** Architect specification, 2026-10-06. The decisions are made here; the Builder implements them. **Amendment A1** (at the end of this document) is binding and overrides AC-6 and §1.6 where they conflict.
- **Inputs:** `REQ_OPERATIONAL_ACTIVITY_SAFE_WRITE.md` (OW-0..13), `CANONICAL_ACTIVITY_VOCABULARY.md` (§1, §2 `note_created`, §2.1 tags), `ROADMAP.md` revision 3 (Phase 4B), and `DEFERRED_DEBT.md`.
- **Baseline:** the committed Phase 4A HEAD.
- **Reused as-is:**
  - 4A `tenant` package: `store_from_env`, `SetupStore`, `resolve_actor`, `compute_setup_state`, `require_capability`, `TenantProfileV2`, the value mappings, the discovery snapshot, and `timeutil`;
  - Phase 3 `schema`: catalogs, `FieldTranslator`, `describe_value`/`truncate_text`;
  - Phase 1 `crosscutting`: `permissions.check`, `audit.log_invocation`.
- **Approach:** new packages and new tools. Existing modules change only through the exact additive edits listed in §1.2.

---

## 0. Decisions (D-4B-n); binding for the Builder, revisable by the user

| ID | Question | Decision (conservative default) |
|---|---|---|
| D-4B-1 | Q-W1 default write posture | Scope `note.create` is **disabled until enabled**. It is enabled only when `BULLHORN_ENABLED_WRITE_SCOPES` (comma list) contains `note.create`. `crosscutting/permissions.py` is **not** changed. The legacy `permissions.check(tool, op)` is still called first, so default outcomes for the 10 original tools are unchanged. The scope policy is a new 4B module. |
| D-4B-2 | Q-W2 dry-run / approval | `create_note(dry_run=True)` is the default. Writing is **two-step**: (1) the preview returns `operation_id` + `preview_hash`; (2) `confirm_write(operation_id, preview_hash, "approve")` executes exactly the previewed write. A single-call write (`dry_run=False`) is allowed only when `BULLHORN_NOTE_CREATE_MODE=direct` (trusted automation; it is audited). Otherwise `dry_run=False` is refused with an instruction to use preview → confirm. |
| D-4B-3 | Approver | The actor comes from `BULLHORN_MCP_ACTOR` (4A `resolve_actor`); a write is refused if it is unset. If `BULLHORN_WRITE_APPROVERS` is set, the confirming actor must be in that list. |
| D-4B-4 | Q-W3 semantic tags | Adopt the 7 proposed tags (vocabulary §2.1) as the packaged resource `mappings/note_action_semantics.yaml` (`version: 1`). Tags are suggestions and grouping only; a tag is never sent to Bullhorn. |
| D-4B-5 | Q-W4 multi-association fallback | **Reject** any request containing an association type that is not verified (§4) or not permitted. Never fall back to one note per target. No partial requests. |
| D-4B-6 | Q-W5 idempotency ledger | **Persistent**, under the 4A store as `writes/ledger/<key>.json`, created with `open(..., "x")` (exclusive, so it works across processes). Derived-key window: 24 h. A caller-supplied key is kept for 7 days. An entry left `pending` after a crash is treated as **in-doubt**: the write is refused with `in_doubt` and must be verified manually. It is never retried automatically. |
| D-4B-7 | Q-W6 pending ops | Persistent: `writes/pending/<operation_id>.json` (written atomically), expiring after 30 min. |
| D-4B-8 | Q-W7 note text in audit | Audit and the journal store `comments_sha256` + `comments_length` only, never the text. The preview returned to the caller contains the text, because the text is the caller's own input. |
| D-4B-9 | Q-W8 legacy upload | `upload_candidate_resume` stays outside the pipeline until Phase 7 (D5). |
| D-4B-10 | Q-W9 actor / author | The actor comes from the environment only. The `author` filter accepts a Bullhorn user **ID**, or a name resolved through the verified CorporateUser lookup (HV-B9). An ambiguous name, or one with no match, returns a choice list. Names are never guessed. If HV-B9 is unresolved, only IDs are accepted. |
| D-4B-11 | Action-type source | Sources are (a) `/meta/Note` `options` on the `action` field, as already captured by the 4A discovery snapshot (HV-A2: options may be present or missing), and (b) values entered by an administrator through the 4A propose/commit flow. No `/settings` endpoint is used in 4B. **DEBT-3 (`meta=full`) is not needed** (HV-A2: `options` is not limited to `meta=full`). DEBT-3 and NB-15 are re-deferred to Phase 5. |
| D-4B-12 | Action-type storage | A new 4A value-target kind, `{kind: note_action, semantic: <tag or null>}`, on `bullhorn_field: action` of the Note entity. This kind is **exempt** from the concept-conflict rule. The union of active `note_action` values is the tenant's valid set. |
| D-4B-13 | Write transport | A **new** module, `bullhorn/writes.py`. **`client.py` is unchanged.** `EntityWriter(client)` sends a JSON-body request using `client.auth.session` (`rest_url`, `bh_rest_token`). It retries once on HTTP 401 only, after `client.auth._refresh_session()` (the same mechanism `client.py` uses). It never retries otherwise. Any non-200 response raises `BullhornAPIError` with a bounded and redacted message (§1.5). |
| D-4B-14 | Read-back / orphan | After the create, the note is read back with its verified associations. A mismatch gives status `partially_committed` with per-association status. Zero associations gives status `failed_orphan`. Phase 4B performs **no deletes and no compensation**. Every outcome is journaled and audited. |
| D-4B-15 | Date-range semantics (SB-5) | `date_from` is inclusive and `date_to` is exclusive. Each accepts ISO-8601 date or datetime. A date-only value means midnight in the tenant `reporting_timezone` (via `zoneinfo`, already validated by 4A). A datetime without an offset is rejected. The values are converted to epoch milliseconds (HV-A3) for the query. |
| D-4B-16 | Write gating | `notes.create` requires state `setup_valid`, computed with `check_connection=True`. Revalidation-required, invalid and in-progress states all block writes. This also mitigates 4A review finding P4A-5. `notes.read` requires `setup_valid` or `setup_revalidation_required`. |

**Escalation:** none. Every unverified Bullhorn mechanism is guarded (§4) instead of being assumed.

## 1. In scope

### 1.1 New files

```
src/bullhorn_mcp/bullhorn/writes.py          # EntityWriter (D-4B-13): create(entity, body); associate(...) only if HV-B3 verified
src/bullhorn_mcp/activity/__init__.py
src/bullhorn_mcp/activity/events.py          # ActivityEvent (vocabulary §1), make_activity_id(concept, entity, id, discriminator="")
src/bullhorn_mcp/notes/__init__.py
src/bullhorn_mcp/notes/model.py              # NoteRecord + from_bullhorn(raw) + to_event() -> note_created
src/bullhorn_mcp/notes/action_types.py       # valid set from active profile; validate(input) -> value | Rejection(valid, suggestions)
src/bullhorn_mcp/notes/reads.py              # filter validation, verified query building, pagination markers
src/bullhorn_mcp/writes/__init__.py
src/bullhorn_mcp/writes/policy.py            # scope policy (D-4B-1/3)
src/bullhorn_mcp/writes/identity.py          # target resolution → identity cards
src/bullhorn_mcp/writes/ledger.py            # idempotency (D-4B-6)
src/bullhorn_mcp/writes/pipeline.py          # minimal SafeWritePipeline for create_note (§1.4)
src/bullhorn_mcp/writes/journal.py           # writes/journal.jsonl + audit bridge
src/bullhorn_mcp/tools/notes.py              # 3 MCP tools (§2)
src/bullhorn_mcp/mappings/note_action_semantics.yaml
tests/test_notes_*.py, tests/test_writes_*.py, tests/test_activity_events.py, tests/test_tools_notes.py, tests/fixtures/notes/...
docs/architecture/PHASE4B_HV_VERIFICATION.md # Builder-authored (§4)
```

### 1.2 Pre-existing files that may change (additive only; nothing else may change)

| File | Exact change |
|---|---|
| `src/bullhorn_mcp/tools/__init__.py` (Phase 2; protected) | **One added line**, immediately after the 4A line `from . import setup  # noqa: F401`: `from . import notes  # noqa: F401` |
| `src/bullhorn_mcp/tenant/profile_v2.py` (4A) | Add the `note_action` value-target kind: parse, `to_dict` and validation of `semantic` against `note_action_semantics.yaml`. Exempt the kind from `find_value_conflicts`. Existing kinds behave identically, and all 4A tests pass unmodified. |
| `src/bullhorn_mcp/tenant/capabilities.py` (4A) | Add the registry entries `notes.read` and `notes.create` (§1.6). Existing entries are unchanged. |
| `src/bullhorn_mcp/tenant/validation.py` (4A) | **Only if needed** to evaluate `note_action` values against the Note `action` options (`values_unverified` semantics as in 4A). Additive only. |

**Must not change:** `client.py`, `server.py`, `crosscutting/*`, `auth/*`, `config.py`, `schema/*.py`, the catalog YAMLs, the other `tools/*.py` files (including `tools/setup.py`), `pyproject.toml`, and every existing test. If an existing test fails, the Builder **stops and escalates**.

### 1.3 Canonical Note and the `note_created` event

`NoteRecord` fields:

| Field | Content |
|---|---|
| `id` | |
| `date_added` | ISO UTC, from `coerce_epoch_millis_to_utc_iso` |
| `action_type` | The tenant value |
| `action_semantic` | Tag or `null` |
| `body` | The text |
| `author_id` | |
| `person_id` | |
| `is_deleted` | |
| `links` | `{candidate_ids, job_ids, client_contact_ids, client_corporation_ids, placement_ids, submission_ids}`, each a sorted list of ints |
| `unresolved_links` | |

**Field sources.** Use the Phase 3 verified fields (`action`, `comments`, `dateAdded`, `isDeleted`, `personReference.id`, `commentingPerson.id`, `jobOrder.id`) plus the HV-B2-verified to-many associations. A link type whose association is unresolved is listed in `unresolved_links` and is never silently omitted.

**Parsing.** `from_bullhorn` never raises `KeyError`/`TypeError` on hostile shapes. Malformed records become warnings.

**`to_event()`** produces vocabulary §1 with:
- `concept="note_created"`;
- `activity_id = make_activity_id("note_created", "note", id)`, as `sha256` hex, deterministic;
- `occurred_at = date_added`;
- `source={note,id}`;
- `links` as above, plus `note_id` and `author_id`;
- `origin` = `observed` or `written_by_mcp`.

The same note yields the same `activity_id` regardless of origin.

### 1.4 Minimal safe-write pipeline (`create_note`; final stage order)

1. **Canonical op.** `CreateNoteOp(target_type, target_id, action_type, comments, associations, idempotency_key)`.
   - `target_type` and every `associations[].type` must be in {`candidate`, `job`, `client_contact`, `client_corporation`, `placement`, `submission`}.
   - IDs must be ints ≥ 1. There are at most 10 associations.
   - `comments` must be `str`, 1..`MAX_COMMENT_CHARS` (10,000, or the HV-B7 verified limit if that is lower). Control characters other than `\n\r\t` are rejected. Text is **never modified**.
2. **Target resolution.** For each target, `client.get(entity, id, fields=<Phase 3 verified standard fields>)` returns an identity card:
   - job: id, title, status, client_corporation_id, owner_id, plus `primary_recruiter_id` if the 4A mapping is active and valid;
   - candidate: id, name, status, owner_id;
   - contact: id, name, client_corporation_id;
   - corporation: id, name;
   - placement / submission: id, candidate_id, job_id, status.

   A missing target, a deleted target or an API error **stops** the pipeline (`rejected_target`).
3. **Validation.**
   - Action type via `notes/action_types.validate` (D-4B-11/12). Unknown → `rejected_validation` with `valid_values` (≤100) and `suggestions` (tenant values whose tag or label matches case-insensitively). **Never substitute.**
   - Every association type must be verified (§4). Otherwise → `rejected_validation` with `unsupported_association` (D-4B-5).
   - Errors are aggregated.
4. **Permission.** `permissions.check("create_note", "write")`, then `policy.check_scope("note.create", actor)` (D-4B-1/3) and the capability gate `notes.create` (D-4B-16). A failure → `denied`, with the missing requirements listed.
5. **Idempotency.**
   - The key is the caller's `idempotency_key`, or `sha256(type-tagged JSON of {op, sorted targets, action value, comments_sha256, actor})` within the window.
   - Ledger hit committed → `duplicate`, plus the original `record_id`. Ledger hit pending → `in_doubt`.
   - **Duplicate probe** (HV-B5): query for notes with the same personReference/job, the same action and `dateAdded` within 24 h, then compare `comments` by hash. If the probe mechanism is unresolved, skip it with a warning.
6. **Preview.**
   - Contents: the exact JSON body for `PUT /entity/Note` (HV-B1), the association plan, the identity cards, the idempotency verdict and `requires_confirmation`.
   - `preview_hash = sha256(type-tagged canonical JSON of the preview without timestamps)`.
   - The preview is stored in `writes/pending/<operation_id>.json`.
7. **Approval.** `confirm_write(operation_id, preview_hash, decision)`:
   - It is **refused** for any of: hash mismatch; operation expired; already consumed; actor missing or not allowed (D-4B-3); scope disabled since the preview; gate no longer ok.
   - It **re-runs stages 2–5**. If any identity card or the idempotency verdict changed, it is refused with `stale_preview`.
   - `reject` marks the operation rejected and writes nothing.
8. **Write.**
   - Create the ledger entry with exclusive create (`pending`). Then `EntityWriter.create("Note", body)`. Then add the associations, either inside the create body or via the to-many endpoint, whichever is verified (HV-B2/B3). Then read back (HV-B6).
   - Ledger becomes `committed` with `record_id`. If the Bullhorn create fails, the ledger becomes `failed` (the key may be reused).
9. **Audit.** For each transition (`previewed`, `confirmed`/`rejected`, `committed`, `partially_committed`, `failed`, `failed_orphan`):
   - `audit.log_invocation` with comments replaced by hash and length;
   - one `writes/journal.jsonl` line holding `correlation_id` (shared from preview to commit), actor, operation, entity, `record_id`, targets, approval state, a Bullhorn response summary (`changedEntityId`/`changeType` or the error, bounded) and the outcome.
10. **Normalized result** (OW-6). Fields: `operation`, `status`, `entity_type: {canonical: note, bullhorn: Note}`, `record_id`, `associations[{type, id, status}]`, `targets`, `activity: [note_created event, origin written_by_mcp]`, `correlation_id`, `operation_id`/`preview_hash` (preview only), and bounded `warnings`/`errors`.

### 1.5 Output safety

- **Errors.** Every Bullhorn error text in a result, journal or audit entry goes through `describe_value`/`truncate_text` (≤300 characters). It is also redacted: values of keys or patterns `BhRestToken`, `access_token`, `refresh_token` and `password`, and any 20+ character token-like run following those names, become `***`.
- **Strings.** No result string exceeds 10,000 characters per line. No raw exception escapes a tool. `SchemaError`, `SetupStoreError`, `BullhornAPIError` and `AuthenticationError` all become a bounded `ERROR:` string.

### 1.6 Reads (`get_notes`) and capabilities

**Filters.**
- Exactly one primary scope: `target_type` + `target_id`, **or** one of `candidate_id`, `job_id`, `client_corporation_id`, `client_contact_id`, `placement_id`, `submission_id`. Combinations are AND'ed only where the verified query supports it.
- Optional filters: `action_type` (validated as in §1.4 stage 3; an unknown value is rejected), `author` (D-4B-10), `date_from`/`date_to` (D-4B-15) and `include_deleted=False`.
- A filter whose mechanism is unresolved in §4 returns `unsupported_filter`, listing the supported ones. It is never approximated.

**Query construction.** Queries are built only from validated typed values: ints, epoch-ms ints, and tenant-valid action strings with `'` escaped. No caller string is concatenated raw. Ordering is `-dateAdded`.

**Paging.** `limit` 1..50 (default 20), `start ≥ 0`. The result is `{notes:[NoteRecord.to_dict()], events:[note_created], next_start|null, truncated: bool, warnings}`.

**Capabilities** (added to the 4A registry):
- `notes.read`: requires state `setup_valid` or `setup_revalidation_required`.
- `notes.create`: requires `notes.read`, state `setup_valid`, and at least one active, valid (or `values_unverified`) `note_action` value mapping.

## 2. Public MCP tool surface (CT-1): 3 new tools; the existing 16 are unchanged

| Tool | Signature (JSON-schema-typed) | Notes |
|---|---|---|
| `get_notes` | `(target_type: str\|None=None, target_id: int\|None=None, candidate_id: int\|None=None, job_id: int\|None=None, client_corporation_id: int\|None=None, client_contact_id: int\|None=None, placement_id: int\|None=None, submission_id: int\|None=None, action_type: str\|None=None, author: str\|None=None, date_from: str\|None=None, date_to: str\|None=None, include_deleted: bool=False, limit: int=20, start: int=0)` | R |
| `create_note` | `(target_type: str, target_id: int, action_type: str, comments: str, associations: list[dict]\|None=None, idempotency_key: str\|None=None, dry_run: bool=True)`; `associations[] = {type: str, id: int}` | Preview by default |
| `confirm_write` | `(operation_id: str, preview_hash: str, decision: str)`; `decision ∈ {approve, reject}` | Generic. Phase 7 reuses it for other operations. |

## 3. Out of scope

- Other writes (Phase 7). Note update or delete. Compensation or deletion of orphans.
- Bulk note creation. Name-based contact or corporation resolution (Phase 5 `find_*`).
- The cross-entity timeline (Phase 6). The other concept derivers (Phase 5).
- `meta=full` / DEBT-3. Any `/settings` endpoint. Changes to `permissions.py` or `approval.py`.
- Fixing 4A review findings P4A-1, 2, 4, 6–12, except where §0 states a mitigation.

## 4. HV-1: Bullhorn mechanisms 4B depends on

The Builder verifies each item against https://bullhorn.github.io/rest-api-docs/ and `entityref.html` and records it in `PHASE4B_HV_VERIFICATION.md` (source, quote, verdict, guard). An unresolved item is **guarded by disabling the dependent feature** (refuse with `unsupported_*`), never by guessing.

| ID | Mechanism | Guard if unresolved |
|---|---|---|
| HV-B1 | Create an entity via `PUT /entity/Note` with a JSON body; response shape (`changedEntityId`, `changeType`, error body) | `create_note` disabled entirely (`unsupported_operation`); report to the Architect |
| HV-B2 | The Note entity's association fields (for example `personReference` to-one; `candidates`, `clientContacts`, `jobOrders`/`jobOrder`, `placements`, `jobSubmissions`, `corporations`, or a `NoteEntity` link entity) and which can be set **in the create body** | That association type is disabled |
| HV-B3 | The to-many association endpoint (for example `PUT /entity/Note/{id}/{association}/{ids}`) **if** create-body association is not supported | That association type is disabled |
| HV-B4 | Whether a note is visible on a target record only via `NoteEntity` (and how that is created) | If it is required and unverified, the target type is disabled |
| HV-B5 | Querying notes: `/query/Note` and/or `/search/Note` where-syntax for associations, `action`, `commentingPerson`, `dateAdded`, `isDeleted`; `orderBy`; `count`/`start` limits | That filter (or the duplicate probe) is disabled |
| HV-B6 | Read-back `GET /entity/Note/{id}?fields=...` including to-many sub-field syntax (for example `candidates(id)`) | Read-back verifies the to-one fields only, with a warning |
| HV-B7 | Note `comments` maximum length and type | Use the 10,000-character cap |
| HV-B8 | `commentingPerson` on create: whether it is set automatically or required | Do not send it. If it is required and unverified, `create_note` is disabled. |
| HV-B9 | CorporateUser lookup by name (entity, fields, query) for the `author` filter | Author accepts IDs only |
| HV-B10 | The Note `action` field's `options` presence in `/meta/Note` | Admin-entered values only (`values_unverified`) |

## 5. Deferred-debt disposition (the Architect updates `DEFERRED_DEBT.md` at phase close)

**Closed in 4B:**

| Item | Scope |
|---|---|
| RAG-10 | canonical Note |
| OWG-1 | action discovery / enforcement |
| OWG-2 | the `note.create` scope (new policy module) |
| OWG-3, OWG-4, OWG-5, OWG-6, OWG-9 | minimal parts |
| OWG-7 | via `bullhorn/writes.py`, without touching `client.py` |
| SB-5, SB-12 | in full |
| SB-7 | ID + verified name lookup |

**Re-deferred:**

| Item | New target |
|---|---|
| DEBT-3, NB-15 | 5 (not needed, per HV-A2) |
| SB-8 name resolution | 5 |
| P4A-5 remainder | 5 |

## 6. Acceptance criteria (each mechanically checkable)

**Gates**

- **AC-1.** `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` each exit 0.
- **AC-2.** The regression suite passes 25/25. Case 10's allowlist gains exactly the one `tools/__init__.py` line from §1.2.
- **AC-3.** `git diff <4A-head> --name-status` shows only `A` entries plus `M` for the files in §1.2. The `tools/__init__.py` diff is exactly one added line. `git diff <4A-head> -- src/bullhorn_mcp/bullhorn/client.py src/bullhorn_mcp/crosscutting src/bullhorn_mcp/server.py` is empty.
- **AC-4.** The wheel contains `bullhorn_mcp/mappings/note_action_semantics.yaml` and the new packages.
- **AC-5.** These grep checks hold:
  - `safe_load` has exactly 1 hit;
  - `yaml.(load|unsafe_load)(` has 0 hits;
  - `grep -rn "client\._request\|\.put(\|\.post(\|\.delete(" src/bullhorn_mcp` finds no new hits outside `bullhorn/writes.py`. Existing `client.py` lines are unchanged.
- **AC-6.** `TestMCPToolSchemasUnchanged` and the 4A tool-pinning test pass unmodified. The registry holds 19 tools, and a new test pins the 3 new names and schemas.

**Pipeline and safety**

- **AC-7.** `create_note` with default `dry_run` makes **no** write request: respx records only `GET` calls. It returns `operation_id` and `preview_hash` and writes nothing except `writes/pending/`.
- **AC-8.** `confirm_write` is refused, and no `PUT`/`POST` is recorded, in each of these cases:
  - wrong hash;
  - expired (simulated clock);
  - already consumed;
  - actor unset;
  - actor not in `BULLHORN_WRITE_APPROVERS`;
  - scope disabled after the preview;
  - setup state ≠ `setup_valid`;
  - a target changed between preview and confirm (`stale_preview`).

  An approved confirm issues exactly one create `PUT` (plus the verified association calls) and returns `committed` with `record_id` and a `note_created` event whose `activity_id` equals the one derived later via `get_notes` for the same note.
- **AC-9.** These are all rejected before any write:
  - an unknown action type, with `valid_values` and `suggestions` listed and the action never substituted (a test asserts the request body is never built);
  - an unsupported association;
  - more than 10 associations;
  - an over-limit comment;
  - a comment containing control characters;
  - a non-int ID or an ID ≤ 0.
- **AC-10.** For idempotency:
  - The same key with the same payload, after commit, returns `duplicate` with the original `record_id` and no second `PUT`.
  - The same key with a different payload is rejected.
  - A leftover `pending` ledger entry yields `in_doubt`.
  - Two concurrent confirms of the same operation produce exactly one `PUT`. The test uses threads, and the exclusive create decides the winner.
- **AC-11.** For read-back:
  - All associations present → `committed`.
  - One association missing → `partially_committed` with per-association status.
  - None present → `failed_orphan`.

  No DELETE is ever issued.
- **AC-12.** `BULLHORN_NOTE_CREATE_MODE` unset with `dry_run=False` → refused. With `direct`, it commits in one call, and the journal records `mode: direct`.
- **AC-13.** Audit records and journal lines never contain the comment text. They contain the hash and the length. They share one `correlation_id` from preview to commit. With an error body containing `"BhRestToken":"abc123..."`, the token never appears in any output, audit record or journal line.
- **AC-14.** With `BULLHORN_ENABLED_WRITE_SCOPES` unset, `create_note` (preview or direct) → `denied`. The 10 legacy tools' outcomes are unchanged (the existing tests pass).

**Reads and model**

- **AC-15.** `get_notes` filter validation:
  - Neither a primary scope nor an ID → error.
  - Two primary scopes → error.
  - An unknown `action_type` → rejected.
  - A naive datetime → rejected.
  - A date-only bound is converted with the reporting timezone: `UTC` in the test, plus one monkeypatched zone.
  - `[from, to)` boundaries are correct to the millisecond.
  - Each unresolved-mechanism filter → `unsupported_filter`.
- **AC-16.** Query construction is safe. A parametrized injection corpus (quotes, `OR 1=1`, `)`, Unicode, 10,000-character strings) in every string filter never reaches the query unvalidated: it is either rejected, or appears only as a validated tenant action value with `'` escaped. The test asserts on the exact `where` sent through respx.
- **AC-17.** `NoteRecord.from_bullhorn` handles a hostile-record corpus (missing keys, null to-many, non-dict associations, string IDs, NaN, 1,000 associations) without raising. Unresolved link types appear in `unresolved_links`.
- **AC-18.** `get_notes` excludes deleted notes by default, and `limit` > 50 is refused. `truncated` / `next_start` are correct for a page that reaches the limit.

**Verification**

- **AC-19.** `PHASE4B_HV_VERIFICATION.md` lists HV-B1..B10, each with a source, a quote and a verdict. For every unresolved item, a test proves that the guard refuses the request: `unsupported_*`, and no HTTP call to the unverified mechanism.
- **AC-20.** The `note_action` kind: `semantic` outside the packaged tag list → `ProfileError`. Two `note_action` mappings sharing a value do **not** produce a concept conflict. All existing 4A tests pass unmodified.

**Process.** The Builder hands off to a fresh Reviewer. The Reviewer gets only this package, the diff and the raw gate outputs. Focus areas:
- confirm-bypass and hash or replay attacks;
- concurrent confirms;
- in-doubt handling;
- action-type substitution;
- association spoofing;
- query injection;
- token or comment leakage;
- orphan reporting.

**Commit message:** `Phase 4B: add notes and activity core with minimal safe-write pipeline`.

---

## Amendment A1 (2026-10-06): Architect rulings on the two Builder-reported spec conflicts

These rulings are binding. They override AC-6, §1.2 ("every existing test") and §1.6 where they conflict.

### C1: the tool-count pin

**Problem.** AC-6 requires 19 tools, but 4A's `TestRegistry.test_sixteen_tools` asserts exactly 16. The two requirements are contradictory.

**Ruling: option (a).** Exactly **one** 4A test edit is allowed: `tests/test_tools_setup.py::TestRegistry::test_sixteen_tools` (lines 151–155). The edit:
- changes the name-set assertion from equality to **superset**: `ORIGINAL_TOOLS | set(NEW_SCHEMAS) <= names`;
- removes the `len == 16` assertion;
- keeps the test name unchanged;
- changes no other line of that file.

**Why this is safe.** The 4A guarantees remain:
- all 16 tools are still present;
- `test_new_schemas` still pins the 4A schemas exactly, and is not edited;
- `TestMCPToolSchemasUnchanged` is not edited.

**New 4B test.** It pins the **exact** set of 19 names, plus the 3 new schemas, and it replaces the count pin.

**Reviewer check.** `git diff <4A-head> -- tests/` shows only `A` entries, plus exactly that hunk in `tests/test_tools_setup.py`.

**AC-6 now reads:** "`TestMCPToolSchemasUnchanged` and `test_new_schemas` pass unmodified; `test_sixteen_tools` is edited only as A1/C1 states; a new test pins exactly 19 names and the 3 new schemas."

### C2: the `notes.create` mapping requirement

**Ruling: option (a).**
- **Registry entries.** The `notes.read` and `notes.create` entries in `tenant/capabilities.py` carry **state-only** requirements:
  - `notes.read`: `setup_valid` or `setup_revalidation_required`.
  - `notes.create`: `setup_valid`.
- **Where the mapping requirement is enforced.** "At least one active, usable `note_action` mapping" is enforced in `writes/pipeline.py`, in both the permission stage (§1.4 stage 4) and the `confirm_write` gate.
- **How it is reported.** It is reported as an explicit missing requirement, `value_mapping:note.action:note_action`, in the `denied` result. This satisfies TS-7 ("return missing setup requirements explicitly") at the point of use.
- **No change** to `tenant/state.py` or to `tests/test_tenant_state.py`.
- **Known limitation.** `setup_status` will not list the `note_action` requirement under `notes.create`. This is accepted for 4B and recorded as follow-up P4B-A1-1, targeted at Phase 7, when capability aggregation is generalized.

**New tests.** They must show that:
- the 4A `TestStateMachine::test_rows` passes unmodified;
- `create_note` and `confirm_write` return `denied` with the exact `value_mapping:note.action:note_action` item when no usable `note_action` mapping exists.

---

## 4B Review Triage (2026-10-07)

**Verdict.** Review FAIL. The Reviewer raised 1 blocking finding. The Architect **promotes 6 more**, because each one violates a binding clause. The other review findings are non-blocking and are logged in `DEFERRED_DEBT.md` (P4B-*). Every gate and every other AC passed.

**What "local" means.** "Local" = the fix stays inside the 4B files, changes no architecture, and needs no new pre-existing-file edits. For each fix, the regression tests are new tests, or edits to **4B-created** tests only.

| ID | Source | Why blocking | Local? |
|---|---|---|---|
| B-1 | Reviewer | Violates A1/C2 | yes |
| B-2 | NB-1, promoted | Violates OW-2 (no orphan) and D-4B-14; §4 HV-B4 guard | **no**: it needs HV verification and may disable a target type |
| B-3 | NB-2, promoted | Violates OW-8 and D-4B-6 (duplicate prevention): two `PUT`s are possible | yes (internal to the ledger) |
| B-4 | NB-3, promoted | Duplicate risk: a `200` response means the write may have happened. Contradicts the HV record. | yes |
| B-5 | NB-4 + NB-5, promoted | Violates D-4B-8 (comment text never in audit/journal) and the AC-13 token rule | yes |
| B-6 | NB-6, promoted | Violates D-4B-6 (a stuck pending entry is not a crash, so it must not leave the key in-doubt forever) and stage 9 (every transition journaled) | yes |
| B-7 | NB-9 (line endings), promoted | Violates §1.2 / AC-3 "additive only": converting CRLF to LF rewrites every line of two 4A files | yes |

### B-1: `confirm_write` gate status

**Fix.**
- When no usable `note_action` mapping exists, `confirm_write` returns `status: "denied"`, not `refused`.
- It keeps `reason` and `missing_requirements`, which must contain exactly `value_mapping:note.action:note_action`.
- Relevant code: `writes/pipeline.py:620-623`.

**Test.** Update `test_writes_pipeline.py::TestConfirm::test_note_action_mapping_removed_after_preview` (a 4B test) to assert `denied` and that exact missing item. Add the same assertion for direct mode.

### B-2: NoteEntity / `commentingPerson` (HV-B4/B8)

**Background.** The reference ties NoteEntity auto-creation to sending **both** `commentingPerson` and `personReference`. 4B currently omits `commentingPerson`, so notes on person targets may not appear on those records.

**Fix.**
1. **Add HV-B11 to `PHASE4B_HV_VERIFICATION.md`.** It records a *documented* mechanism for obtaining the authenticated user's CorporateUser ID.
   - **If verified:** send `commentingPerson: {id: <that id>}` on every create. Cache it per session; never accept it from the caller.
   - **If unresolved:** disable every target or association type whose record visibility depends on NoteEntity auto-creation (at least `candidate` and `client_contact` via `personReference`). These return `unsupported_association`, per the §4 guard. Record this in the HV document.
2. **Read-back.** If the read-back shows an expected NoteEntity is absent for a target, that target's association status is `note_entity_absent`, and the overall status is **`partially_committed`**, never `committed` (D-4B-14). If no target is linked or visible at all, the status is `failed_orphan`.

**Tests.**
- With HV-B11 verified (mocked), the create body contains `commentingPerson.id` equal to the resolved user. That ID never comes from tool arguments.
- With HV-B11 unresolved (guard on), a candidate or client_contact target is rejected with `unsupported_association`, and no `PUT` is sent.
- A read-back fixture without a NoteEntity results in `partially_committed` with `note_entity_absent`.

### B-3: ledger supersede race

**Required property.** For any key, at most **one** `Verdict(new)` can be issued per generation, under any interleaving.

**Fix (mandated mechanism).**
- Replace the read-check-`os.replace` supersede at `writes/ledger.py:194-206` with **generation-numbered exclusive create**.
- Entries become `ledger/<key>.g<N>.json`, and the current entry is the highest `N`.
- Superseding a `failed` or expired generation `N` means creating `g<N+1>` with `open(..., "x")`. Exactly one racer succeeds.
- A losing racer re-reads, sees `pending`, and returns `in_doubt` (or `in_progress`; see P4B-3). It never returns `new`.
- The ledger must never use `os.replace` or `os.rename` onto a live key path.

**Tests.**
- The Reviewer's deterministic interleaving: a prior failed entry, then two writers. Exactly one gets `new`, and respx records exactly one `PUT`.
- A 50-iteration threaded stress test with the same assertion.
- `grep -n "os.replace\|os.rename" src/bullhorn_mcp/writes/ledger.py` returns 0 hits.

### B-4: a `200` response with a non-JSON or non-object body

**Fix.**
- In `bullhorn/writes.py:98-103`, a `200` response whose body is not a JSON object raises a distinct `WriteOutcomeUnknown` error.
- The pipeline maps it to `status: "in_doubt"`. The ledger entry **stays `pending`**, so the key is not reusable, and a journal line is written with outcome `in_doubt`.
- The HV record and the code must then agree.

**Tests.**
- `200` with body `"ok"`, `[]` or `null` gives `in_doubt`, with the ledger still pending.
- A retry with the same key returns `in_doubt`, and no second `PUT` is sent.

### B-5: redaction and comment scrubbing

**Fix.** Before any Bullhorn error text is written to a result, the journal or an audit record:

1. **Comment scrubbing.**
   - Remove every occurrence of `op.comments`, and of each line of it that is 8 or more characters long, matched case-sensitively.
   - Replace each occurrence with `<comments:sha256-prefix>`.
2. **Token-redaction forms.** Extend redaction to cover:
   - URL-encoded forms (`BhRestToken%22%3A%22…`, `%3D`);
   - backslash-escaped JSON (`\"BhRestToken\":\"…\"`);
   - header forms (`Bh-Rest-Token:`, `BhRestToken=`), case-insensitive;
   - values containing spaces up to the closing quote, separator or end of line.

**Tests.**
- A parametrized corpus covering each form, with token values that contain spaces. The token never appears in the outputs, the journal or audit captured with `caplog`.
- An error body that echoes the full comment text, and lines of it. The comment text never appears in the journal or audit. The returned result may echo the text only inside `preview` (D-4B-8).

### B-6: `AuthenticationError` during create

**Fix.**
- In `pipeline.py:689-701`, catch `AuthenticationError` raised from the session or refresh before any request completes.
- The ledger entry becomes `failed`, so the key is reusable: no write was accepted.
- The pipeline writes a journal line with outcome `failed` and a redacted, bounded reason, and returns `status: "failed"`.
- Any other unexpected exception after the `PUT` was sent gives `in_doubt`, as in B-4.

**Tests.**
- A refresh failure gives `failed`; the ledger entry is not pending; a journal line is present; and a later identical write succeeds with a new ledger generation.

### B-7: line endings

**Fix.**
- Restore the original line endings in `tenant/profile_v2.py` and `tenant/capabilities.py`.
- `git diff <4A-head> --ignore-cr-at-eol` and plain `git diff <4A-head>` must show the **same** hunk set for those two files, and each hunk must be additive only.

**Check.** `git diff <4A-head> --stat -- src/bullhorn_mcp/tenant/` changes lines only in the additive hunks; the files are not rewritten in full.

### Re-review scope

- All AC-1..AC-20 under A1, plus B-1..B-7 with their named tests.
- The 25/25 regression suite.
- The test-diff rule: only A1/C1's single 4A test hunk plus new or 4B-created tests.

**Re-review (2026-10-07): PASS.** No blocking findings remain. All gates and the 25/25 regression suite are green. The remaining non-blocking findings are logged as P4B-8..P4B-12 in `DEFERRED_DEBT.md` ("Phase 4B close-out"). `create_note` stays disabled in production pending EXT-1 (`ROADMAP.md` §5).
