# Phase 4B: Bullhorn Mechanism Verification (HV-1)

> **EXTERNAL DEPENDENCY (Architect note, 2026-10-07).** `create_note` is code-complete but **disabled in production** until HV-B11 is verified. HV-B11 is a *documented* mechanism for obtaining the logged-in user's CorporateUser id, used for `commentingPerson` and NoteEntity auto-creation. It must be verified against the Bullhorn reference or against a connected tenant. Two preconditions apply before `HV_B11_VERIFIED` is ever set to `True`:
>
> 1. Record the verification in this file.
> 2. Close `DEFERRED_DEBT.md` P4B-8 (comment scrubbing before redaction).
>
> See `ROADMAP.md` §5 (External dependencies, EXT-1).

- **Author:** Builder, Phase 4B.
- **Date checked:** 2026-10-06.
- **Scope:** HV-B1 to HV-B10 from `PHASE4B_WORK_PACKAGE.md` §4.
- **Method:** Both reference pages were downloaded as raw HTML. They were then converted to text, and every entity field table was parsed with its column positions kept, so "Not null" and "Read-only" marks are attributed to the right column. Each quote below is verbatim from that text.

## Sources

| Ref | Source | URL |
|---|---|---|
| S1 | Bullhorn REST API reference | https://bullhorn.github.io/rest-api-docs/ |
| S2 | Bullhorn REST API Entity Reference | https://bullhorn.github.io/rest-api-docs/entityref.html |
| S3 | Phase 4A verification record (HV-A2, HV-A3) | `docs/architecture/PHASE4A_HV_VERIFICATION.md` |

S1 sections used:
- "General GET request options" / "Entity fields" (abbreviated "Entity fields" below);
- "Entities" / "Notes";
- `GET /entity` (single, multiple, to-many);
- `PUT /entity`, "Create To-many Associations", `POST /entity`;
- `GET /search`, `GET /query`, `GET /allCorpNotes`;
- "Errors".

S2 tables used: `Note`, `NoteEntity`, `Candidate`, `ClientContact`, `JobOrder`, `Placement`, `ClientCorporation`, `JobSubmission`, `CorporateUser`.

## Results

### HV-B1: `PUT /entity/Note` with a JSON body, and the response shape

**Verdict:** Verified (generic entity create).

**Evidence (S1, `PUT /entity`):**
- The example is `curl -X PUT -H "Content-Type: application/json" -d '{"firstName" : "Alanzo", "lastName" : "Smith"}' .../entity/Candidate`. Its example response is `{ "changedEntityId" : 1489, "changeType" : "INSERT" }`.
- "You can use HTTP PUT requests to create new entities. The URL looks the same as GET request URL, but without the last path element containing an entity ID. Place the data comprising the new entity to be inserted in JSON format in the request body."
- "Most entities in the Bullhorn data model contain mandatory fields. ... All mandatory fields without default values must have values specified in the JSON body of the PUT request or return a 400 error."

**Error bodies:** S1 "Errors" documents the status codes only (400, 401, 403, 404, 405, 406, 410, 429, 500, 503). It does not document an error-body shape.

**How 4B uses it:**
- `EntityWriter.create("Note", body)` sends `PUT {restUrl}/entity/Note` with a JSON body.
- Only an HTTP 200 whose body has an int `changedEntityId` and `changeType == "INSERT"` counts as a created record.
- A 200 response with any other body is treated as **in doubt**. The ledger stays `pending`, so nothing is retried and the outcome must be verified by hand.
  - This includes a 200 whose body is not JSON or not a JSON object, such as `"ok"`, `[]`, `null` or HTML. `EntityWriter` raises the distinct `WriteOutcomeUnknown` for these, and the pipeline maps it to `in_doubt` (review fix B-4).
- Every non-200 response raises `BullhornAPIError`. Its message is the status plus the body, redacted and bounded (§1.5). The body shape is never parsed.

### HV-B2: Note association fields, and which can be set in the create body

**Verdict:** Verified.

**Evidence (S1, `PUT /entity`):**
- "You cannot create to-many associations on the entity being inserted. You must create them in a subsequent 'associate' call."
- "You can create to-one associations. The associations can only be to existing entities."
- "Associations fields are set by giving as their values a JSON object containing one field, named 'id'."

**Evidence (S2, Note table; columns are type | Not null | Read-only):**

| Field | Type | Not null | Read-only | Documented description |
|---|---|---|---|---|
| `personReference` | To-one | X | | "Person with whom this Note is associated." |
| `jobOrder` | To-one | | | "Primary JobOrder associated with this Note." |
| `commentingPerson` | To-one | X | | "The default value is user who creates the Note." |
| `candidates` | To-many | | | |
| `clientContacts` | To-many | | | |
| `jobOrders` | To-many | | X | |
| `placements` | To-many | | X | |
| `entities` | To-many | | X | NoteEntities |
| `people` | To-many | | X | |

- The Note entity has **no** corporation field and **no** JobSubmission field.

**How 4B uses it:**
- **Create body:** `action`, `comments` and `personReference: {id}` (the primary target, which must be a candidate or a client contact), plus `jobOrder: {id}` when exactly one `job` association is requested.
- **Disabled association types** (`rejected_validation` / `unsupported_association`):
  - a second `job`, because `jobOrders` is read-only;
  - `placement`, because `placements` is read-only;
  - `client_corporation`, because there is no field;
  - `submission`, because there is no field.
- **Disabled target types** (`unsupported_target`): `job`, `placement`, `client_corporation` and `submission`. `personReference` is "Not null" and has no documented default, so a Note cannot be created without a person.

### HV-B3: to-many association endpoint

**Verdict:** Verified (generic).

**Evidence (S1, "Create To-many Associations"):**
- The example is `curl -X PUT .../entity/Candidate/3084/primarySkills/964,684,253`. The documented path is `{corpToken}/entity/{entityType}/{entity-id}/{to-many-association-name}/{entity-id},*}`.
- "You can add to-many associations to an entity with a PUT request in which you specify entity IDs of the entities you want to associate. The call fails if any of the association entities you specify are already associated."

**How 4B uses it:**
- `EntityWriter.associate("Note", id, "candidates" | "clientContacts", ids)` is used only for the writable to-many fields from HV-B2.
- IDs that equal the primary `personReference` target are never sent twice: a duplicate `(type, id)` is a validation error.

### HV-B4: whether a note is visible on a record only via `NoteEntity`

**Verdict:** Partially verified.

**Evidence (S2, Note):**
- "Represents a note (comment) associated with a Candidate, ClientContact, CorporateUser, JobOrder, JobShift, Lead, or Opportunity. Notes can be accessed via the 'Notes' tab on the person's record."
- "If you include a commentingPerson value and a personReference value when you create a Note, the association to an entity is made automatically and you do not need to make a separate call to create a NoteEntity."

**Evidence (S2, NoteEntity):**
- "Represents the Candidate, ClientContact, CorporateUser, JobOrder, or Placement associated with a Note."
- Fields: `note`, `targetEntityID`, and `targetEntityName` ("For Candidates ClientContacts, and CorporateUsers, specify 'User' ... For JobOrders and Placements, specify the actual entity name").

**Unresolved:**
- The docs do not say whether the automatic NoteEntity is also created when `commentingPerson` is left to its documented default (HV-B8) instead of being sent.
- Explicitly creating a `NoteEntity` could duplicate an automatic one, and that behaviour is undocumented.

**How 4B uses it (after review fix B-2):**
- 4B never creates `NoteEntity` records.
- `commentingPerson` is sent only when HV-B11 is verified. It is not verified, so the guard below applies.
- **Guard:** every type whose record visibility depends on NoteEntity auto-creation is disabled. That is `candidate` and `client_contact`, as the target (`personReference`) or as an association. Each returns `rejected_validation` / `unsupported_association`, and no HTTP call is made. Every other target type is already disabled (HV-B2), so **`create_note` cannot write in production** until HV-B11 is verified.
- **Read-back, when enabled:** it requests `entities(targetEntityID,targetEntityName)`.
  - A linked association without its NoteEntity has status `note_entity_absent`.
  - When NoteEntities cannot be read, the status is `unverified`.
  - Either way the overall status is `partially_committed`, never `committed`.
  - When no association is linked at all, the status is `failed_orphan`.

### HV-B5: querying notes (`/query/Note`, `/search/Note`, where-syntax, `orderBy`, `count`/`start`)

**Verdict:** Mostly unresolved. Only the to-many read of `JobOrder.notes` / `Placement.notes` is verified.

**Evidence (S1, `GET /search`):**
- "The following entity types support the search operation: Candidate, ClientContact, ClientCorporation, JobOrder, Lead, Note, Opportunity, Placement, Task".
- "Entity types not listed above use the query operation". So `/query/Note` is not documented for Note.
- The Lucene **index field names** for Note are not documented anywhere in S1/S2. Discovering them needs a runtime `GET /search/Note` with no parameters. Neither are the date-range syntax or association sub-field names. Search `sort` is documented ("Precede with minus sign to perform descending sort").

**Evidence (S1, `GET /entity` "To-many Associations"):**
- "returns the to-many associated entities of the specified type for the specified entity ID(s). The call supports the same query parameters as the query call", with `start`, `count` and `orderBy` ("Name of property on which to base the order of returned entities"; no direction syntax is documented for it).
- S2: `JobOrder.notes` is "To-many association — Notes associated with the JobOrder". `Placement.notes` is "To-many association — Notes associated with this Placement".
- S2: Candidate and ClientContact have **no** `notes` field. `ClientCorporation.notes` is a `String`, not an association. JobSubmission has none.
- S1 "Entities / Notes": "to-many associated entities that are soft-deleted (deletion is indicated by the isDeleted field) are not returned."
- S1 `GET /allCorpNotes` documents only `fields` and `clientCorpId`. `start`/`count` and ordering are not documented, so it is not used.

**How 4B uses it:**
- **Supported read scopes:** `job_id` (`GET /entity/JobOrder/{id}/notes`) and `placement_id` (`GET /entity/Placement/{id}/notes`), with `start` and `count`. Each is also reachable as `target_type` + `target_id`.
- No `orderBy` is sent. Results are returned in server order with a warning.
- `include_deleted=False` is satisfied by the documented behaviour, and deleted rows are also filtered defensively.
- **Guarded:**
  - The scopes `candidate_id`, `client_contact_id`, `client_corporation_id` and `submission_id`, and their `target_type` forms, return `unsupported_filter`.
  - The filters `action_type`, `author`, `date_from`/`date_to` and `include_deleted=True` return `unsupported_filter`. Each is validated first, so an unknown action is still `rejected` and a naive datetime is still an error.
  - The **duplicate probe** is skipped with the warning `duplicate_probe_skipped`. Duplicates are still prevented by the persistent ledger.

### HV-B6: read-back `GET /entity/Note/{id}?fields=...`, including to-many sub-field syntax

**Verdict:** Verified.

**Evidence (S1, "Entity fields"):**
- "fields=id,name,address(city,zip),owner(corporation(name)),categories(name)"
- "When selecting to-one and to-many association fields, id is automatically included if no subfield is specified"
- "fields=categories[3],jobSubmissions[5](dateAdded,jobOrder(name))", and "The default count of to-many entities is 5. The maximum count is 10".
- S1, `GET /entity`: `{corpToken}/entity/{entityType}/{entityId}?fields={fieldList}`.

**How 4B uses it:**
- The read-back requests `id,dateAdded,action,isDeleted,personReference,commentingPerson,jobOrder,candidates[10](id),clientContacts[10](id),entities[10](targetEntityID,targetEntityName)`. At most 10 associations are allowed, so `[10]` covers them all.
- To-many fields are not requested in the to-many list reads of HV-B5, because nesting inside a to-many GET is not documented. Those link types are reported in `unresolved_links`.

### HV-B7: Note `comments` maximum length and type

**Verdict:** Verified.

**Evidence:** S2, Note: "comments | String (2147483647) | Text of this Note."

**How 4B uses it:** the documented limit is larger than 10,000, so the 10,000-character cap applies (`MAX_COMMENT_CHARS`).

### HV-B8: `commentingPerson` on create

**Verdict:** Verified.

**Evidence:** S2, Note: "commentingPerson | To-one association | Person who created the Note. The default value is user who creates the Note." It is marked Not null, and it has a default, so it is not mandatory per S1 `PUT /entity`.

**How 4B uses it:** the field is not required. Sending it (for NoteEntity auto-creation) needs the authenticated user's CorporateUser id; see HV-B11.

### HV-B11: a documented mechanism for the authenticated user's CorporateUser id (review fix B-2)

**Verdict:** Unresolved.

**Evidence, S1 (searched for "userId", "currentUser", "corporateUserId", "user id", "login", "ping", "settings"):**
- `login` example response: `{ "BhRestToken" : "...", "restUrl" : "https://rest{swimlane#}.bullhornstaffing.com/rest-services/{corpToken}/" }`. No user id is returned.
- `GET /ping`: "Returns the date of the calling client's session expiration." Its example response is `{ "sessionExpires" : 1323449994922 }`. No user id.
- `GET /settings/setting1[,setting2…]`: the documented example settings are `allPrivateLabelIds`, `currencyFormat`, `accountLockoutDuration`, `allDeptIds` and `commentActionList`. **No user-id setting is documented.** `/settings` is also excluded from 4B by D-4B-11.
- `/services/CorporateUser/{corporateUserID}` requires the id as input; it does not return the caller's id.

**Evidence, S2:** `CorporateUser.username` is documented ("username for logging in to Bullhorn"). Two things are undocumented:
- that the OAuth/API username used by this server equals that field;
- that a `/query/CorporateUser?where=username='...'` lookup identifies the authenticated session.

Using that lookup would be an inference, not a documented mechanism, so it is **not** used.

**Guard (as specified by the triage, B-2):**
- `writes/pipeline.py` has `HV_B11_VERIFIED = False`.
- `candidate` and `client_contact`, as target or association, return `unsupported_association` with no HTTP call. This covers every target type 4B supported, so `create_note` cannot write in production.
- The current-user resolver (`WriteContext.current_user`) is server-side only and is never a tool argument. It is consulted only when `HV_B11_VERIFIED` is true, and its result is cached per session.
- Tests mock that path to prove the body carries `commentingPerson.id` from the resolver.

### HV-B9: CorporateUser lookup by name for the `author` filter

**Verdict:** Partially verified; not exercised.

**Evidence:**
- S2, CorporateUser: `firstName`, `lastName` and `name` ("Name of the CorporateUser").
- S1: CorporateUser is not in the search list, so the query operation applies. The JPQL example is `query/ClientContact?...&where=lastName='smith'`.

**How 4B uses it:**
- The `author` filter has no verified note-side mechanism (HV-B5), so it returns `unsupported_filter` whether given an ID or a name.
- No CorporateUser request is ever made in 4B.

### HV-B10: presence of the Note `action` field's `options` in `/meta/Note`

**Verdict:** Verified: options may be present or missing.

**Evidence:**
- S3, HV-A2 (S1, Property Metadata): "options: ... may be missing", and `options` is not limited to `meta=full`.
- S2, Note: "action | String (30) | Action type associated with Note. The list of values is configured in the private label attribute called commentActionList."

**How 4B uses it:**
- `note_action` values are evaluated by the existing 4A value-mapping evaluation. When `options` are absent, the values are `values_unverified` but usable.
- Values are strings of at most 30 characters (the documented field length).
- No `/settings` endpoint is used.

## Additional verification used by identity resolution

S2 documents `isDeleted` ("Indicates whether this record is marked as deleted") on Candidate, ClientContact and JobOrder. Identity cards request it, so a soft-deleted target stops the pipeline (`rejected_target`).

Other fields used, all in S2 and in the Phase 3 catalog:
- Candidate: `firstName`, `lastName`, `status`, `owner`.
- ClientContact: `firstName`, `lastName`, `clientCorporation`.
- JobOrder: `title`, `status`, `clientCorporation`, `owner`.

## Unresolved items and their guards (AC-19)

| Item | Guard (refusal) | Test |
|---|---|---|
| HV-B2/HV-B3: `placement`, `client_corporation` and `submission` associations, and a 2nd `job` | `rejected_validation` with `unsupported_association`; no HTTP call at all | `tests/test_writes_pipeline.py::TestValidation::test_unsupported_association_no_http` |
| HV-B2: non-person primary target (`job`, `placement`, `client_corporation`, `submission`) | `rejected_validation` with `unsupported_target`; no HTTP call | `tests/test_writes_pipeline.py::TestValidation::test_unsupported_target_no_http` |
| HV-B4/HV-B11: NoteEntity auto-creation needs `commentingPerson`, and no documented current-user id mechanism exists | `candidate` / `client_contact` (target or association) return `unsupported_association`; no HTTP call | `tests/test_writes_review_fixes.py::TestB2::test_unresolved_guard_rejects_person_targets`, `tests/test_writes_review_fixes.py::TestB2::test_unresolved_guard_rejects_person_associations`, `tests/test_tools_notes.py::TestHvB11Guard::test_production_default_rejects_person_targets` |
| HV-B4 read-back (when HV-B11 is enabled): a NoteEntity is absent or unreadable | Association status `note_entity_absent` or `unverified`; overall `partially_committed` | `tests/test_writes_review_fixes.py::TestB2::test_readback_without_note_entity_is_partial`, `tests/test_writes_pipeline.py::TestReadBack::test_note_entity_absent_warns` |
| HV-B5: Lucene/JPQL note filters, the candidate/contact/corporation/submission scopes, ordering | `unsupported_filter` listing the supported filters; no request is issued | `tests/test_notes_reads.py::TestUnsupported` |
| HV-B5: duplicate probe | Skipped with the warning `duplicate_probe_skipped`. No `/search` or `/query` request is ever made by `create_note`. | `tests/test_writes_pipeline.py::TestPreview::test_preview_only_gets` |
| HV-B9: CorporateUser name lookup | The `author` filter is `unsupported_filter`; no CorporateUser request | `tests/test_notes_reads.py::TestUnsupported` |
