# Phase 5C HV Verification: expanded recruiting reads

- **Status:** Builder-authored record for `PHASE5C_WORK_PACKAGE.md` §5 (HV-1, G-1), 2026-10-07.
- **Scope:** every Bullhorn mechanism that `find_records` / `get_activity` use. The packaged resource
  `src/bullhorn_mcp/mappings/bullhorn_query_support.yaml` cites the HV ID below for every entry; an
  entry without an HV ID fails to load (`reads/support.py`).
- **Tenant verification:** not performed. It would need a connected tenant and an administrator, and no
  tenant values may be committed (D-5-22). Every verdict below rests on the official documentation only.
  Where the documentation is silent, the item is **unresolved** and the dependent capability returns the
  §5 guard with zero Bullhorn calls. Nothing is approximated.
- **Outcome summary** (consistent with Amendment C3-3):

| ID | Verdict | Consequence in code |
|---|---|---|
| HV-Q1 | **Verified** (`/query` for all 8 entities) | Every query-support entry has `operation: query`. No Lucene renderer exists. |
| HV-Q2 | **Partially verified** | Operators `=`, `<>`, `<`, `<=`, `>`, `>=`, `IN`, `NOT IN`, `IS [NOT] NULL`, `AND`/`OR`/parentheses, to-one paths, boolean and epoch-ms literals and `'x'` string literals are verified. `LIKE` (no `starts_with`), string escaping (restricted charset `^[A-Za-z0-9 ._@-]{1,200}$`, else `unsupported_value`) and decimal literals (`unsupported_value`) are unresolved. |
| HV-Q3 | **Unresolved** | No impact (HV-Q1: `/search` is not used). |
| HV-Q4 | **Partially verified** (paging); ordering **unresolved** | Offset paging only, `consistency: "offset"` + warning, no `orderBy`; every `sort` is `unsupported_sort`. P4B-5 stays open. |
| HV-Q5 | **Verified** | Every date/datetime field used is a documented `Timestamp`; filters render epoch ms, output is UTC `Z`. |
| HV-Q6 | **Partially verified** | Exclusion term `(isDeleted = false OR isDeleted IS NULL)` + post-filter; `include_deleted=true` is `unsupported_filter` for every entity. |
| HV-Q7 | **Partially verified** | Backoff base 1.0 s, cap 8 s; `Retry-After` never parsed; fixed cap of 2 in-flight requests per principal. |
| HV-Q8 | **Verified** | `user` binds to `CorporateUser` (D-5C-16). |
| HV-Q9 | **Partially verified** | `placement.submission_id` default; recruiter and client corporation tenant-mapped; `interview_rescheduled` unsupported; `recurrence_not_expanded` warning. |
| HV-Q9b | **Verified** | Interviews are parent appointments only: `parentAppointment IS NULL`. |
| HV-Q10 | **Unresolved** | `status_history` dating rejected; `job_status_changed` / `candidate_status_changed` are `unsupported_concept`. |
| HV-Q11 | **Partially verified** | Every non-200 is `error` / `bullhorn_error`; never retried under another identity. |
| HV-Q12 | **Verified** (upper bound) | Conservative caps: 2,000 characters of `where`, 30 fields. |
| HV-Q13 | **Verified** | Nested selection `field(sub)` for to-one associations and composites. |

## Sources

Official reference: https://bullhorn.github.io/rest-api-docs/ (REST API reference, "S1") and its Entity
Reference ("S2"), cached on 2026-10-06/07 (`apiref.html`, `entityref.html`) and compared with the live pages.
Quotations are verbatim; line references are to the text extraction of the cached copy.

## HV-Q1: read operation and response shape (all `FIND_ENTITIES`)

**Verdict: verified.** S1, `GET /query/{entity}`: "Retrieves a list of entities … The where parameter accepts
Java Persistance Query Language (JPQL) syntax"; request form
`{corpToken}/query/{entity}?where={query-text}&fields={fields}&orderBy={fields}&count={count}&start={start}`.
The operation is generic over `{entity}`. S1's own example queries `ClientContact`, which is also a
`/search` entity ("The following entity types support the search operation: Candidate, ClientContact,
ClientCorporation, JobOrder, Lead, Note, Opportunity, Placement, Task"), so `/query` is not limited to the
non-search entities; "Entity types not listed above use the query operation" covers JobSubmission,
Appointment and CorporateUser. S2 documents each of the 8 entities (`Candidate`, `JobOrder`,
`JobSubmission`, `Placement`, `ClientCorporation`, `ClientContact`, `Appointment`, `CorporateUser` — the
last as "Read-only entity that represents an internal user at an organization").
Response shape (S1 example response for `GET /query`): `{"data": [ {...}, ... ]}` (with `start` / `count`).
`total` is not documented for `/query` (only `totalOnly=true` returns a count), so `complete` is decided
from page length (HV-Q4). **Code:** `EntityReader` requires a `data` list; anything else is
`bullhorn_error`. Entity → query entry: `bullhorn_query_support.yaml` (`hv: HV-Q1`).

## HV-Q2: `/query` `where` grammar

S1, "Query where parameter", verbatim:

> Simple comparisons: `property = value`, `property <> value`, `property < value`, `property <= value`,
> `property > value`, `property >= value`.
> May use compound property names (not for to-many properties): `owner.lastName = 'Smith'`,
> `owner.corporation.name = 'Acme'`.
> IS [NOT] NULL: `property IS NULL`, `property IS NOT NULL`.
> [NOT] IN: `property IN (value, value)`, `property NOT IN (value, value)`.
> Logical Expressions: NOT, AND, OR … Grouping by parentheses.
> Boolean values: `true | false` (examples `enabled = true`, `willingToRelocate = false`).
> Datetime values: UNIX long millis. For example, `dateAdded > 1324579022`.

String literal form: S1 example `where = lastName = 'smith'` (single quotes).

| Item | Verdict | Code |
|---|---|---|
| `=`, `<>`, `<`, `<=`, `>`, `>=` | verified | `eq`/`gt`/`gte`/`lt`/`lte` render `=`/`>`/`>=`/`<`/`<=` |
| `IN`, `NOT IN` | verified | `in`; `NOT IN … OR … IS NULL` internal only (cancelled-exclusion of interviews) |
| `IS NULL`, `IS NOT NULL` | verified | `is_null`; `IS NOT NULL` internal only (`id IS NOT NULL` as the always-true base clause of a non-soft-deletable entity, since `where` is required) |
| `AND`, `OR`, parentheses | verified | filters are AND-combined; `OR` only inside internal groups |
| to-one paths (`owner.id`) | verified | `association_paths_verified: true`; paths are single-level (`x.id`) from the catalog |
| boolean literal | verified | `true` / `false` from an exact `bool` |
| datetime literal | verified | epoch ms from an exact `int` |
| integer literal | verified (S1 example `dateAdded > 1324579022`, and id comparisons in S1 examples) | `str(int)` on an exact `int` |
| string literal `'x'` | verified | single-quoted |
| string escaping (quotes inside a value) | **unresolved**: no escape rule is documented | `string_escape_verified: false`: values must match `^[A-Za-z0-9 ._@-]{1,200}$`, else `unsupported_value`. Names containing an apostrophe are therefore unsupported (accepted by C3-3). |
| `LIKE` / prefix | **unresolved**: not in the grammar | no `starts_with` operator (`prefix_match_verified: false`) |
| decimal literal | **unresolved**: only integer examples | finite floats are `unsupported_value` (`float_literal_verified: false`) |
| maximum `where` length | see HV-Q12 | — |

**Filterable fields.** Each raw field in `filterable` was checked to exist on that entity in S2 (for
example Candidate `firstName` String (50), `dateAdded` Timestamp, `owner` To-one association;
JobSubmission `sendingUser` To-one association; Placement `jobSubmission` To-one association; Appointment
`parentAppointment` To-one association). `address.city` / `address.state` use the composite `address`
(S2 type "Address"/"COMPOSITE"), a compound property name that is not to-many. Operators per field follow
the canonical type table of §3.1 intersected with the verified grammar. Tenant custom fields
(`custom_fields`) get `eq`, `in`, `is_null` only.

## HV-Q3: `/search` Lucene

**Verdict: unresolved** (per-entity index field names, range inclusivity, date format and reserved-character
escaping are not documented in a form 5C could rely on). **Impact:** none: by HV-Q1 every entity uses
`/query`; `bullhorn/query_syntax.py` has no Lucene renderer. Should an entry ever say `operation: search`,
`entity_support` returns `unsupported_entity` (`hv: HV-Q3`) with zero calls.

## HV-Q4: paging and ordering

- **Paging, verified:** S1 `/query` parameters: "count — Limit on the number of records to return. If the set
  of matched results is larger than count, cap the returned results at size count"; "start — From the set of
  matched results, return record numbers start through (start + count)". A page shorter than `count`
  therefore means the result set is exhausted. The documented maximums of other operations (500 for
  `/search`) are larger than the conservative page size used here (`max_page_size: 100`). `total` is not
  returned by `/query` (`total_in_response: false`).
- **Ordering, unresolved:** S1 `/query` `orderBy` is "Name of property on which to base the order of returned
  entities", with no direction syntax and no default order. (The `-`/`+` prefix is documented only for other
  operations' `orderBy`, e.g. "Precede field name with a minus sign (-) or plus sign (+)" on the
  `/savedSearch` endpoint, not for `/query`.) Tie-break stability is undocumented.
- **Code:** offset paging only; no `orderBy` is sent; `consistency: "offset"` with a warning stating that the
  order is server order and not guaranteed; any `sort` → `unsupported_sort` (`hv: HV-Q4`); keyset paging is
  not implemented (AC-11 as amended by C3-3); P4B-5 stays open.

## HV-Q5: date representation

**Verdict: verified.** S1: "Datetime values: UNIX long millis". S2 types for every date field used:
Candidate `dateAdded`, `dateLastModified`; JobOrder `dateAdded`, `startDate`; JobSubmission `dateAdded`;
Placement `dateAdded` ("Timestamp (5)"), `dateBegin`, `dateEnd`; ClientCorporation / ClientContact
`dateAdded`; Appointment `dateAdded`, `dateBegin`, `dateEnd`; CorporateUser `userDateAdded` — all
`Timestamp`. **Code:** bounds are epoch ms (`tenant/timeutil.parse_bound`), output via
`coerce_epoch_millis_to_utc_iso` (UTC `Z`) plus `occurred_at_local` for events.

## HV-Q6: soft delete and user status

S2 `isDeleted` rows (field | type | description | Not null):

| Entity | `isDeleted` | Not null |
|---|---|---|
| Candidate | Boolean, "marked as deleted in the Bullhorn system" | X |
| ClientContact | Boolean | X |
| JobSubmission | Boolean | X |
| Appointment | Boolean | X |
| JobOrder | Boolean | (nullable) |
| CorporateUser | Boolean, "Indicates whether CorporateUser is deleted." | (nullable) |
| Placement | no `isDeleted` field | — |
| ClientCorporation | S1, DELETE: "immutable entities, which are neither hard-deletable nor soft-deletable. Immutable entities include … ClientCorporation" | — |

S1: "Soft deletes one or more soft-deletable entities, which sets the isDeleted property of the entity to
true"; for to-many association reads, "entities that are soft-deleted (deletion is indicated by the
isDeleted field) are not returned". **Whether `/query` returns soft-deleted rows is not documented.**

- **Architect interpretation HV-Q6-I (recorded as an interpretation, not a verified fact):** a NULL `isDeleted`
  means "not deleted" (an unset boolean flag; treating it as deleted would hide live jobs and users).
- **Code:** for every soft-deletable entity the query carries `(isDeleted = false OR isDeleted IS NULL)` and the
  rows are post-filtered (`isDeleted is True` dropped, counted in a warning). Placement and ClientCorporation
  carry no term (`soft_delete: {field: null}`). `include_deleted=true` → `unsupported_filter` (`hv: HV-Q6`) for
  every entity. An entry marked `unresolved` → `unsupported_entity` (`deleted_semantics_unresolved`).
- **CorporateUser enabled/inactive:** S2 `enabled` Boolean, "Indicates whether the CorporateUser may log in";
  `status` String (100), "Status of the CorporateUser". Neither is filtered implicitly (D-5C-8): `status` is
  an ordinary canonical field; `enabled` is not canonical (C3-3).

## HV-Q7: rate limits

S1 error table: "429 — Rate Limited – Wait 1 second then retry request. Repeat until successful."; "500 —
Internal Server Error – … Try again later." `Retry-After`, 503 semantics and concurrency limits are **not
documented** (the 5A HV-C9/C10 evidence adds nothing for reads). **Code (`bullhorn/reads.py`):** 429/503 at
most 2 retries with full-jitter exponential backoff, base 1.0 s (C3-3), cap 8 s; 500/502/504 at most one
retry; `Retry-After` is never parsed; 30 s budget per call (`rate_limited` on overrun, partial results
discarded); at most 2 in-flight requests per `(tenant_key, principal_key)`.

## HV-Q8: CorporateUser

**Verdict: verified.** S2 CorporateUser table: `id` Integer; `userDateAdded` Timestamp ("Date the record was
added to the system"); `firstName` String (50); `lastName` String (50); `email` String (100); `status`
String (100); `enabled` Boolean; `isDeleted` Boolean. Read operation: HV-Q1. S2 has **no** `dateAdded` row
for CorporateUser, so canonical `date_added` maps to `userDateAdded`. **Code:**
`bullhorn_standard_fields.yaml` `CorporateUser` section with the default mappings of C3-3 (`id`,
`date_added: userDateAdded`, `first_name`, `last_name`, `full_name: "{firstName} {lastName}"`, `email`,
`status`); `find_records(entity="user")` uses capability `records.user`.

## HV-Q9: Placement and Appointment fields

- **Placement → submission, verified:** S2 Placement `jobSubmission` To-one association. Default
  `placement.submission_id: {field: jobSubmission, key: id}`.
- **Placement → client corporation, recruiter: no documented direct field** suitable as a default
  (C3-3). `placement.client_corporation_id` and `placement.recruiter_id` are tenant-mapped with no default;
  unmapped links are `null` and listed in `unresolved_links`; a `recruiter_id` filter on `placement_created`
  is then `unsupported_filter`.
- **Appointment classification/status:** S2 has `type` String (30) and no status/cancellation field. The
  interview classification is a tenant concept mapping (for example on `type`); `appointment.status` is
  tenant-mapped (typically a custom field).
- **Recurrence:** S2 `recurrenceType`, `recurrenceFrequency`, `recurrenceDayBits`, `recurrenceStyle` are fields
  of one Appointment record ("Null for a one-time appointment"), so a series is one record. Events carry
  `recurrence_not_expanded` (`recurrence_single_record: true`).
- **Reschedule linkage:** none documented → `interview_rescheduled` is `unsupported_concept` (`hv: HV-Q9`).
- **Attendees:** S2 `attendees` To-many (AppointmentAttendee). Attendee composites are Phase 6.

## HV-Q9b: Appointment invitee copies (Amendment C3-3a)

**Verdict: verified.** S2 Appointment description, verbatim: "A separate Appointment instance is created for
each user who is invited to the appointment; the instance belonging to the Appointment owner (the person who
created it) is the parent, and has a null value for the parentAppointment property. The Appointment
instances belonging to the invitees are the child instances; these refer to the parent in their
parentAppointment properties". S2 field row: "parentAppointment | To-one association | Appointment that is
the parent of this one, if any." **Code:** `instance_filter: {field: parentAppointment, op: is_null}`; every
`interview_*` concept and `find_records(concept=interview_*)` adds `parentAppointment IS NULL`, and a
defensive post-filter drops rows with a non-null parent. Children are never counted. If the entry is removed,
every `interview_*` concept is `unsupported_concept` (`invitee_copies_unresolved`).

## HV-Q10: status history

**Verdict: unresolved.**
- `JobSubmissionHistory` (S2): "Read-only entity that represents the transaction history of a JobSubmission.
  The GET /query/JobSubmissionHistory call returns a list…", but its `dateAdded` is documented as "Date on which
  the JobSubmission record was created", not the transaction time, so the change time is undocumented.
- `{Entity}EditHistory` (S2) for JobOrder: `fieldChanges` (To-many `{Entity}EditHistoryFieldChange`); the mapping
  of recorded column names to API field names is undocumented.
- No Candidate history entity is documented.

**Code:** `history_sources` entries are `verified: false`; `client_submission_dating = status_history` is rejected at
validation (`STATUS_HISTORY_VERIFIED = False`), and is `unsupported_concept` (`status_history_unresolved`) if
present; `job_status_changed` / `candidate_status_changed` are `unsupported_concept`.

## HV-Q11: Bullhorn permissions

S1 error table: "403 — Forbidden – The entity requested is hidden for administrators only"; S1 fields
syntax: "For nested to-many associations for which the user does not have read entitlements, only data for
predefined fields is returned. The other fields are returned with a value of 'null'." Record-level and
to-one field entitlements are **not documented**. **Code:** any non-200 (after the documented retries) is
`error` / `bullhorn_error` with only the status code; there is no retry under any other identity, and no
service-session fallback (G-4). A `null` value returned for an entitlement-limited field is output as `null`.

## HV-Q12: URL / `fields` length

S1: "always use the POST version of the query call rather than this GET version for where values that exceed
7500 characters in length". No `fields` limit is documented. **Code:** conservative fixed caps
`max_where_chars: 2000` (`unsupported_value`, `hv: HV-Q12`, no request) and `max_fields: 30`.

## HV-Q13: nested sub-fields

**Verdict: verified.** S1 "Fields" syntax: "Nested, to select sub-fields of composite, to-one and to-many
associations( )", example `fields=id,name,address(city,zip),owner(corporation(name)),categories(name)`; "When
selecting to-one and to-many association fields on an entity, id is automatically included if no sub-fields
are specified." **Code:** `render_fields` emits `name` or `name(sub,…)` (one level), e.g. `owner(id)`,
`jobSubmission(id)`; `parentAppointment` is requested plain (its id is included automatically).
