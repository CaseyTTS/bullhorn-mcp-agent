# Phase 4A: Bullhorn Mechanism Verification (HV-1)

- **Author:** Builder, Phase 4A.
- **Date checked:** 2026-10-06.
- **Scope:** HV-A1 to HV-A5 from `PHASE4A_WORK_PACKAGE.md` §4.
- **Method:** Both reference pages were downloaded as raw HTML, converted to text, and searched for the exact keys and examples quoted below.

## Sources

| Ref | Source | URL |
|---|---|---|
| S1 | Bullhorn REST API reference: "Entity metadata" table, `GET /meta/{Entity}` section with its "Property Metadata" list, `login` example response, query "Datetime values" rule, and entity example responses | https://bullhorn.github.io/rest-api-docs/ |
| S2 | Bullhorn REST API Entity Reference: JobOrder field table | https://bullhorn.github.io/rest-api-docs/entityref.html |
| S3 | Phase 3 verification record | `docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md` |

Neither page shows an API version.

## Results

| ID | Mechanism | Verdict | Evidence | How 4A uses it / guard |
|---|---|---|---|---|
| HV-A1 | `GET /meta/{Entity}?fields=*` returns `fields[]` with `name`, `label`, `dataType`, `type`, `optional`/`required`, `readOnly`, `options` | **Partially verified.** `name`, `label`, `type` and `dataType` are **verified**. `required`, `optional` and `readonly` are **unresolved** for the `fields=*` response the client actually sends. | S1, `GET /meta/{Entity}`: "Returns entity and property metadata"; Property Metadata lists `name`, `label` ("may be missing"), `type` ("one of ID, SCALAR, COMPOSITE, TO_ONE, or TO_MANY") and `dataType`. The example response for `meta/Candidate?fields=*` shows `"name"`, `"type"` and `"dataType"`. S1, "Entity metadata" table: "\* Fields that are returned only if meta=full", and `optional`, `required` and `readonly` are starred. The key is spelled `readonly` (lowercase), and it is described as "Is the property hidden (specified by fieldmap)", not as read-only. `client.get_meta` sends only `fields=*` and no `meta=full` (DEBT-3, not taken in 4A). | The snapshot records `required` and `read_only` as `MetaDiscovery` parses them. A missing key becomes `false`, and `meta.py` already accepts both `readOnly` and `readonly`. **Guard:** nothing in 4A decides anything from `required` or `read_only`. No state, validation, broken-mapping or gating logic reads them. A change in them is reported only as a `changed_fields` entry. Test: `tests/test_tenant_revalidation.py::TestDiscovery::test_required_read_only_informational`. |
| HV-A2 | Whether `options` (picklist values) can appear in the `fields=*` response, and their shape | **Verified:** they may appear, and they may be missing. | S1, Property Metadata of `GET /meta/{Entity}`: "options: The hard-coded options from fieldMap in an array of value/label pairs; may be missing." In the "Entity metadata" table, `options` is **not** starred, so it is not limited to `meta=full`. `optionsType` and `optionsUrl` are starred. | Options are recorded only when the response contains them, in the existing `{value, label}` shape (`meta.py`). **Guard:** when options are absent, every value mapping on that field is `values_unverified` (a warning). The drift report lists it as `unverifiable`, which does not count as a finding. Tests: `tests/test_tenant_validation.py::TestValueMappings::test_options_absent_means_values_unverified` and `tests/test_tenant_revalidation.py::TestDriftReport::test_value_drift` / `test_unverifiable_alone_is_not_a_finding`. |
| HV-A3 | Bullhorn timestamp representation for `dateAdded`-type fields (epoch milliseconds) | **Verified** for entity `Timestamp` fields. | S1, query syntax, "Datetime values": "UNIX long millis. For example, dateAdded > 1324579022". S1, `GET /entity/JobOrder/{id}/fileAttachments` example: `"dateAdded" : 1530815115887` (13 digits, milliseconds). Also seen elsewhere in S1: `"dateLastComment" : 1607036876320`, `"dateApproved" : 1716523200001`. One exception, outside 4A's use: the `savedSearch` example shows `"dateAdded" : "2013-01-31"`. That is a different, non-entity resource. | `coerce_epoch_millis_to_utc_iso` ships as the UTC primitive (D-4A-11). **No 4A code path calls it.** Its first consumer is Phase 5. It rejects every non-`int` input. Tests: `tests/test_tenant_timeutil.py`. |
| HV-A4 | Nested `owner` association with sub-field `id` on JobOrder | **Verified** (Phase 3, re-checked). | S3 §1, which covers `<association>.id`. Re-checked in S2, JobOrder table: "owner, To-one association, CorporateUser who owns the JobOrder." S1, "Entity ids": "All entities have a field named id that is the primary key of the entity." | 4A uses it only through the existing catalog default `owner_id: {field: owner, key: id}`, through `apply_verified_defaults`. That default is materialized only when `owner` is present in the discovery snapshot. |
| HV-A5 | `restUrl` from the REST login response | **Verified.** | S1, `login` example response: `"BhRestToken" : "...", "restUrl" : "https://rest{swimlane#}.bullhornstaffing.com/rest-services/{corpToken}/"`. The existing `auth/bullhorn_password.py` already requires both keys. | Used only as a fingerprint. The stored value is the sha256 of `session.rest_url`, so the URL, including the corp token, is never stored. It is captured during `discover_schema` (the snapshot) and `setup_status(check_connection=True)`, and compared by the state machine (row 5). |

4A uses **no** `/settings`, `/options`, action-list or status-list endpoints.

## Unresolved items and their guards (AC-20)

| Item | Code that would depend on it | Guard | Test |
|---|---|---|---|
| HV-A1: `required`, `optional`, `readonly` in a `fields=*` response (documented as `meta=full` only; `readonly` semantics described as "hidden") | Snapshot attributes `required` / `read_only` | Informational only. No decision reads them; changes go only to `changed_fields`. | `test_tenant_revalidation.py::TestDiscovery::test_required_read_only_informational` |
