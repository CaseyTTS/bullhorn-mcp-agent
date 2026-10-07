# Phase 3: Unverified Bullhorn Mappings (AM-3 verification record)

- **Author:** Builder, Phase 3.
- **Scope:** every Bullhorn name in `src/bullhorn_mcp/mappings/bullhorn_standard_fields.yaml`: the `entities` keys, every `standard_fields` entry, every raw source in `default_mappings` (including nested `field`/`key` parts and template placeholders), the `custom_field_patterns` families, and the named entries in `sensitive_field_patterns`.
- **Result:** every name was confirmed. Nothing was omitted, so each table below says "None".

## 1. Sources consulted

| Source | URL | Accessed | API version shown |
|---|---|---|---|
| Bullhorn REST API, *Entity Reference* (per-entity field tables) | https://bullhorn.github.io/rest-api-docs/entityref.html | 2026-10-06 | None shown on the page |
| Bullhorn REST API, *API Reference*: "Entity ids" section ("All entities have a field named id that is the primary key of the entity. When selecting to-one and to-many association fields on an entity, id is automatically included if no sub-fields are specified.") and "Property Metadata" section of `GET /meta/{Entity}` | https://bullhorn.github.io/rest-api-docs/ | 2026-10-06 | None shown on the page |

**Method.** Both pages were downloaded as raw HTML. Each entity's field table was parsed (first column = field name), and every name in the catalog was checked by exact, case-sensitive match against the table of the entity it belongs to:

- **Entities:** each `entities` key is an `<h1>` section of the Entity Reference.
- **Top-level fields** (`standard_fields`, plain mapping sources, template placeholders): each is a row of that entity's field table.
- **Nested `{field, key}` parts:**
  - `address.city` / `address.state`: `address` is a row typed `Address`/`COMPOSITE` on Candidate, JobOrder and ClientCorporation, and its description lists the sub-fields `address1, address2, city, state, zip, countryID`.
  - `<association>.id`: every outer field is a row typed "To-one association" in that entity's table, and the API Reference "Entity ids" rule confirms `id` on every associated entity. This covers Note's `personReference` and `commentingPerson`, whose target ("Person") has no section of its own in the Entity Reference.
- **Custom-field families:** the Entity Reference documents them as ranges. Examples are `customText1 to 40`, `customTextBlock1 to 10`, `customInt1 to 23`, `customFloat1 to 23`, `customDate1 to 13`, `customObject1s to 35s` and `customEncryptedText1 to 10` (Candidate), and `customBillRate1-10` and `customPayRate1-10` (Placement).
- **Sensitive names:** `ssn`, `taxID`, `dateOfBirth`, `password`, `gender`, `ethnicity`, `veteran`, `disability` and `maritalStatus` are all rows of the Candidate table.
- **`DEFAULT_FIELDS`:** every name in `DEFAULT_FIELDS` (`src/bullhorn_mcp/bullhorn/client.py:13-19`) was confirmed for its entity. No escalation was needed.

## 2. Omitted Bullhorn entities

| Bullhorn entity | Intended canonical entity | Reason not confirmed |
|---|---|---|
| None | None | None |

## 3. Omitted field mappings

| Bullhorn entity | Canonical field | Intended raw source (as in §4.3) | Reason not confirmed |
|---|---|---|---|
| None | None | None | None |

## 4. Omitted `standard_fields` / `custom_field_patterns` entries

| Bullhorn entity | Canonical field | Intended raw source (as in §4.3) | Reason not confirmed |
|---|---|---|---|
| None | None | None | None |

## 5. Retained-unverified sensitive patterns

| Pattern | Reason retained |
|---|---|
| None | None |
