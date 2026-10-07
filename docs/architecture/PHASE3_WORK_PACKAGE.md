# Phase 3 Work Package: Canonical Schema & Mapping Layer

- **Status:** APPROVED by the user on 2026-10-06. This file is the approved package `mellow-booping-chipmunk.md`, copied faithfully, with the user-mandated amendments folded in (see §0).
- **Baseline:** `4d7da27` (Phase 2 complete), on branch `feature/bullhorn-orchestrator`.
- **Architecture source of truth:** `enter-planning-mode-only-binary-cherny.md` (§2–§5, §8), as amended by `docs/architecture/DEFERRED_DEBT.md`.
- **Process:** Architect, then Builder, then an independent Reviewer (`docs/process/*.md`).

---

## 0. User-mandated amendments (binding; these override anything below that conflicts)

| ID | Amendment | Where it applies |
|---|---|---|
| AM-1 | Do **not** modify `BullhornClient.get_meta()`: not its signature, body, or params. Do **not** add `meta=full` or any other request parameter. Picklist enrichment is deferred to Phase 4 (DEBT-3). | §2, §3 C-8, §4.7, AC-4, AC-15 |
| AM-2 | Add `pyyaml>=6.0` to runtime dependencies and `types-PyYAML` to dev dependencies in `pyproject.toml`. This is the only change allowed to an existing file. | §1, AC-2 |
| AM-3 | Check every Bullhorn standard entity name and field name in `bullhorn_standard_fields.yaml` (the `entities` keys, `standard_fields`, and every raw source in `default_mappings`, including nested `field`/`key` parts and template placeholders) against authoritative Bullhorn REST API reference material, such as https://bullhorn.github.io/rest-api-docs/entityref.html. If a name cannot be confirmed, **omit it and flag it**. Never guess. The Builder records every omitted or flagged item in `docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md` (see §4.10). | §3 C-9, §4.2, §4.3, §4.10, AC-6 |
| AM-4 | New files under `docs/architecture/` are allowed additions: this file, `DEFERRED_DEBT.md`, and `PHASE3_UNVERIFIED_MAPPINGS.md`. AC-2 is amended to match. | §1, AC-2 |
| AM-5 | Build only the Phase 3 library and its new tests. Do not change `tools/*.py`, `server.py`, `auth/**`, `crosscutting/**`, `config.py`, `bullhorn/client.py`, or any existing test file. | §2, §3 C-1, AC-2, AC-20 |
| AM-6 | Build a wheel and confirm that the YAML resources are inside it. | AC-12 |

---

## Context

Phase 2 (commit `4d7da27`) split the MCP into `auth/`, `bullhorn/`, `tools/` and `crosscutting/` packages. Phase 3 adds layer 3, "Canonical Schema/Map". This layer is an ATS-agnostic vocabulary that many future agents and front-ends can share:

- Bullhorn field names are translated at this layer's boundary and never leak above it.
- The layer finds tenant custom fields through `get_meta()`, which is dead code today.
- It supports importable tenant mapping profiles.
- It stays safe when a mapping is missing, incomplete or invalid.

**Decisions from planning (2026-10-06).** These are logged in `docs/architecture/DEFERRED_DEBT.md`.

- **D1 Library-only.** The 10 existing MCP tools keep frozen schemas and unchanged code in Phase 3. Agents get canonical support through Phase 4 (setup tools) and Phase 5 (`find_*`). Phase 3 delivers the library and proves it with tests.
- **D2 `query_builder.py` moves to Phase 5**, next to `find_entities`, its only consumer. This amends the roadmap (ROADMAP-AMENDMENT-1).
- **D3 Catalogs are versioned package resources.** They live at `src/bullhorn_mcp/mappings/*.yaml` and load through `importlib.resources`. Tenant profiles stay outside the package, at the path in `BULLHORN_MAPPING_PROFILE`.

**Pre-existing gaps (not Phase 3 scope).**

- `auth/trusted_origins.py` was not delivered. `auth/bullhorn_password.py:102,113` still hardcodes `bullhornstaffing.com`. This is DEBT-1.
- The `config/` package was not delivered. This is DEBT-2.

Both are logged as deliberate deferrals in `DEFERRED_DEBT.md`. Phase 3 does not touch them.

---

## 1. In scope (files to add)

```
src/bullhorn_mcp/mappings/__init__.py              # empty marker so importlib.resources resolves
src/bullhorn_mcp/mappings/canonical_schema.yaml
src/bullhorn_mcp/mappings/bullhorn_standard_fields.yaml
src/bullhorn_mcp/mappings/profile.example.yaml     # checked-in example tenant profile (docs + tests)
src/bullhorn_mcp/bullhorn/meta.py                  # MetaDiscovery: wraps client.get_meta (from plan §3)
src/bullhorn_mcp/schema/__init__.py                # SchemaContext facade (below)
src/bullhorn_mcp/schema/errors.py                  # SchemaError hierarchy
src/bullhorn_mcp/schema/canonical_catalog.py
src/bullhorn_mcp/schema/bullhorn_catalog.py
src/bullhorn_mcp/schema/mapping_profile.py
src/bullhorn_mcp/schema/translator.py
src/bullhorn_mcp/schema/discovery.py
tests/test_schema_*.py, tests/test_bullhorn_meta.py, tests/fixtures/{meta,profiles}/...
docs/architecture/PHASE3_WORK_PACKAGE.md           # this file (Architect)
docs/architecture/DEFERRED_DEBT.md                 # architecture-debt log (Architect)
docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md    # Builder-authored; see §4.10 (AM-3)
```

**Modify:** `pyproject.toml` only (AM-2). Add `pyyaml>=6.0` to runtime dependencies and `types-PyYAML` to dev dependencies.

Hatch already packages non-`.py` files under `src/bullhorn_mcp`. The Builder must not assume this works. They must prove it by building a wheel (AC-12, AM-6). If the YAML files are missing from the wheel, the fix goes in `pyproject.toml` build config, which is still the only existing file allowed to change.

## 2. Out of scope (must NOT happen in Phase 3)

- Any change to `tools/*.py`, `server.py`, `auth/**`, `crosscutting/**`, `config.py`, `bullhorn/client.py` (including `DEFAULT_FIELDS`, and `get_meta`'s signature, body and request params), or any existing test file (AM-5).
- Adding `meta=full`, or any other parameter, to `/meta/{entity}` requests. Picklist enrichment is Phase 4 (AM-1, DEBT-3).
- New MCP tools: `discover_schema`, `review_unmapped_fields`, `apply_field_mapping`, `finalize/export/import_mapping_profile`, `describe_*` and `find_*`. These belong to Phase 4 or 5.
- `connection_status` gaining `setup_required` (Phase 4).
- `schema/query_builder.py` (Phase 5, per D2).
- Value coercion. Phase 3 does not convert epoch-ms to ISO, normalize enum values, or coerce types. Values pass through as Bullhorn returns them.
- Entity writes, and building `canonical → raw` *payloads* for create/update (Phase 5).
- A default profile path. If `BULLHORN_MAPPING_PROFILE` is unset, there is no profile. Phase 4 decides the default location.
- DEBT-1 (`trusted_origins.py`) and DEBT-2 (`config/` package).
- Guessing Bullhorn field names from memory, naming conventions, or analogy with other entities (AM-3).

## 3. Standing constraints

- **C-1** All 10 tools stay behaviorally and schema-identical. `TestMCPToolSchemasUnchanged` and the whole existing suite pass unmodified.
- **C-2 Bullhorn-name isolation.**
  - `canonical_schema.yaml` and `canonical_catalog.py` contain no Bullhorn entity or field names.
  - Only `bullhorn_catalog.py`, `mapping_profile.py` (as raw targets), `translator.py`, `discovery.py` and `bullhorn/meta.py` know Bullhorn names.
  - `schema/*` never imports `server`, `tools`, or `bullhorn.client`.
  - Discovery depends on a `MetaSource` Protocol (`get_entity_meta(entity) -> EntityMeta`), not on the client.
- **C-3 No network at load time.** Loading the catalogs or a profile never calls Bullhorn. Only `MetaDiscovery` and `SchemaDiscoverer` call it, and only when invoked explicitly.
- **C-4 Metadata-only by default.** By default, discovery issues `/meta/{entity}` calls only.
- **C-5** Use `yaml.safe_load` only. Never use `yaml.load` or `yaml.unsafe_load`.
- **C-6** Reject unknown or newer `version:` values in any YAML file. Never guess at them.
- **C-7** `ruff check .`, `mypy src/bullhorn_mcp` and `pytest` are all green.
- **C-8** `BullhornClient.get_meta()` is called exactly as it exists at `src/bullhorn_mcp/bullhorn/client.py:150-160`. Do not wrap it with extra params or subclass it to change its request (AM-1).
- **C-9** Every Bullhorn name in `bullhorn_standard_fields.yaml` has been verified against authoritative Bullhorn REST reference material. Unconfirmed names are omitted and flagged, never guessed (AM-3).

---

## 4. Module specifications

### 4.1 `canonical_schema.yaml` / `canonical_catalog.py`

```yaml
version: 1
entities:
  candidate:
    description: "A person being recruited."
    fields:
      id:         { type: id, required: true }
      first_name: { type: string }
      owner_id:   { type: reference, ref: user }
      ...
```

- **Allowed `type` values:** `id, string, text, integer, number, boolean, datetime, date, reference (requires ref:), list`.
- **Status-like fields** are `string` in v1, because Bullhorn status values are tenant-configured and value normalization is out of scope. This replaces the illustrative `enum` in the architecture plan's example. It is an intentional refinement.
- **Key format:** every entity and field key matches `^[a-z][a-z0-9_]*$`.
- **API:** `load_canonical_catalog() -> CanonicalCatalog` (cached), with `.entities`, `.entity(name)`, `.field(entity, name)` and `.has_field(...)`. Frozen dataclasses: `CanonicalEntity` and `CanonicalField(name, type, required, ref, description)`.
- **Errors:** validation errors are aggregated into a single `CatalogError` that lists all problems. This follows the "collect all, then raise once" pattern of `BullhornConfig.from_env` in `src/bullhorn_mcp/config.py:31-42`.
- **Canonical set is fixed:** the canonical catalog defines the full §4.3 canonical field set no matter what AM-3 verification finds. AM-3 only removes Bullhorn-side mappings. A canonical field whose standard mapping was omitted simply has no default mapping. Tenants can supply one through a profile `standard:` override, and the translator reports it in `unresolved`/`missing`.

### 4.2 `bullhorn_standard_fields.yaml` / `bullhorn_catalog.py`

```yaml
version: 1
custom_field_patterns:          # tenant-configurable families, applied to every entity
  - 'customText\d+'
  - 'customTextBlock\d+'
  - 'customInt\d+'
  - 'customFloat\d+'
  - 'customDate\d+'
  - 'customObject\d+s'
  - 'customEncryptedText\d+'    # flagged sensitive: never sampled
  - 'customBillRate\d+'
  - 'customPayRate\d+'
sensitive_field_patterns: ['customEncryptedText\d+', 'ssn', 'dateOfBirth', ...]
entities:
  Candidate:
    canonical_entity: candidate
    standard_fields: [id, firstName, lastName, email, phone, mobile, status, ...]
    default_mappings:            # canonical -> raw; Bullhorn-standard knowledge, not tenant
      id: id
      full_name: "{firstName} {lastName}"
      owner_id: { field: owner, key: id }
```

- **Cross-validation at load time:** each `default_mappings` key must exist in the canonical catalog, and each raw source must appear in `standard_fields`.
- **Superset rule:** `standard_fields` is a superset of the current `DEFAULT_FIELDS` in `src/bullhorn_mcp/bullhorn/client.py:13-19`. A test enforces this.
  - If AM-3 verification cannot confirm a name that is already in `DEFAULT_FIELDS`, the Builder must **stop and escalate to the Architect**. They must not omit it, because that would break AC-6, and they must not guess.
- **Pattern lists:** the AM-3 verification requirement also covers the `custom_field_patterns` families and the named entries in `sensitive_field_patterns`.
  - A custom-field family that cannot be confirmed for any entity is omitted and flagged.
  - A sensitive pattern that cannot be confirmed may stay **only** because it widens redaction. Keeping it is safe, since it can only cause *fewer* values to be sampled. It must still be listed in the flag file under "Retained-unverified sensitive patterns".
- **API:** `load_bullhorn_catalog()`, `.bullhorn_entity_for(canonical)`, `.canonical_entity_for(bullhorn)`, `.is_custom_field(name)`, `.is_sensitive(name)`, `.default_mappings(canonical_entity)`.

### 4.3 Canonical entities: minimum field set and *candidate* standard mappings

These are the **candidate** standard mappings for `default_mappings`. Each one is included only if it passes AM-3 verification.

- `a.b` means a nested `{field: a, key: b}` mapping.
- `"{x} {y}"` means a template.
- Every entity also has `id → id` and `date_added → dateAdded`. These are also subject to verification per entity.

| Canonical entity | Bullhorn entity | Minimum canonical fields → Bullhorn standard source (candidate, subject to AM-3) |
|---|---|---|
| `candidate` | Candidate | first_name→firstName, last_name→lastName, full_name→"{firstName} {lastName}", email→email, phone→phone, mobile→mobile, status→status, occupation→occupation, skills→skillSet, source→source, city→address.city, state→address.state, owner_id→owner.id, date_last_modified→dateLastModified |
| `job` | JobOrder | title→title, status→status, employment_type→employmentType, is_open→isOpen, num_openings→numOpenings, description→description, start_date→startDate, salary→salary, pay_rate→payRate, bill_rate→clientBillRate, city→address.city, state→address.state, client_corporation_id→clientCorporation.id, client_contact_id→clientContact.id, owner_id→owner.id |
| `submission` | JobSubmission | candidate_id→candidate.id, job_id→jobOrder.id, status→status, source→source, sending_user_id→sendingUser.id |
| `placement` | Placement | candidate_id→candidate.id, job_id→jobOrder.id, status→status, start_date→dateBegin, end_date→dateEnd, employment_type→employmentType, salary→salary, pay_rate→payRate, bill_rate→clientBillRate |
| `client_corporation` | ClientCorporation | name→name, status→status, phone→phone, website→companyURL, city→address.city, state→address.state |
| `client_contact` | ClientContact | first_name, last_name, full_name (template), email, phone, title→occupation, status→status, client_corporation_id→clientCorporation.id, owner_id→owner.id |
| `appointment` | Appointment | subject→subject, type→type, description→description, location→location, start_at→dateBegin, end_at→dateEnd, candidate_id→candidateReference.id, client_contact_id→clientContactReference.id, job_id→jobOrder.id, owner_id→owner.id |
| `note` | Note | action→action, body→comments, person_id→personReference.id, author_id→commentingPerson.id, job_id→jobOrder.id, is_deleted→isDeleted |
| `tearsheet` | Tearsheet | name→name, description→description, is_private→isPrivate, owner_id→owner.id |
| `candidate_reference` | CandidateReference | candidate_id→candidate.id, reference_first_name→referenceFirstName, reference_last_name→referenceLastName, reference_email→referenceEmail, reference_phone→referencePhone, reference_title→referenceTitle, company_name→companyName, status→status |

To-many associations, such as tearsheet members and job submissions, are not in v1. They arrive with composites in Phase 6.

**Builder verification (AM-3, mandatory):**

- Confirm every raw name in the table above against Bullhorn's REST entity reference before committing. That includes the Bullhorn entity name, every top-level field, every nested `field` and `key`, and every template placeholder.
- If a name can't be confirmed:
  - Remove it from `default_mappings`, and from `standard_fields` if it appears there.
  - Never guess it.
  - List it in `docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md`.
- If a whole Bullhorn **entity** can't be confirmed:
  - Omit its entire `entities.<BullhornEntity>` block and flag it.
  - The canonical entity still exists in `canonical_schema.yaml` (AC-5), but it has no Bullhorn binding.
  - `bullhorn_entity_for` returns `None` for it.
  - `FieldTranslator.canonical_to_raw` for it returns `bullhorn_entity=None`, `raw_fields=[]`, and every requested known field in `unresolved`. It does not raise, because the names are known canonical names (§4.6).
  - Discovery skips it, because it is not a catalog entity.
- If a template has any unverified placeholder, the whole template mapping is omitted.

Live discovery (`standard_missing_in_tenant`) is the runtime backstop.

### 4.4 Standard vs. tenant/custom mappings

- **Standard mappings** are canonical fields defined in `canonical_schema.yaml` and mapped by `default_mappings` in the Bullhorn catalog.
  - A profile's `standard:` section may *override* a mapping, for example when a tenant stores `skills` in `customTextBlock2`.
  - Override keys must exist in the canonical catalog, or the profile fails to load.
- **Tenant/custom mappings** are canonical names a tenant *adds*, declared in the profile's `custom:` section.
  - Names must match `^[a-z][a-z0-9_]*$`.
  - Names must **not** collide with any canonical field of that entity. A collision is a load error.
  - Each entry may carry an optional `type`, which defaults to `string`.
  - These fields are tenant-local and surface separately in output (§4.6), so shared agents never confuse a tenant extension with a shared field.
- **Resolution order** in the translator: profile `custom` > profile `standard` override > catalog `default_mappings`. No profile, a missing profile, or an invalid profile means catalog defaults only.

### 4.5 `mapping_profile.py`

The profile format follows the architecture plan's §4 shape: `version: 1`, `tenant`, `generated_at`, and `entities.<canonical>.{standard, custom, unmapped_bullhorn_fields}`.

**Target forms.** Exactly three are accepted:

1. A raw name string matching `^[A-Za-z_][A-Za-z0-9_]*$`.
2. A template string containing only `{rawName}` placeholders. A test rejects `{a.b}`, `{a[0]}`, `{a!r}`, `{a:>5}`, `{}` and positional placeholders.
3. A `{field, key}` dict for a nested lookup. Both parts are identifier-validated. In `custom` only, the dict may also carry an optional `type`.

**Unmapped fields.** `unmapped_bullhorn_fields` entries are either bare strings or dicts of the form `{field, label, data_type, field_type, options?, required?, read_only?}`. Both are normalized to the dict form.

**API:**

- `MappingProfile.load(path)`, `.from_dict(d)` and `.to_dict()`.
- `.save(path)` writes atomically (temp file, then `os.replace`), and its output round-trips.
- `resolve_profile_path() -> Path | None` reads `BULLHORN_MAPPING_PROFILE`.
- `load_active_profile() -> ProfileStatus(state: "none"|"loaded"|"missing"|"invalid", profile|None, errors)`:
  - It never raises.
  - On `missing` or `invalid`, it logs a warning to `bullhorn_mcp.schema` and sets `profile=None`, so a broken profile can never partially apply.

**Validation** is aggregated: one `ProfileError` reports every error and never stops at the first one. The loader does not call Bullhorn.

### 4.6 `translator.py`: FieldTranslator, and how unmapped fields are represented

`FieldTranslator(canonical_catalog, bullhorn_catalog, profile | None)`

**`canonical_to_raw(entity, fields: list[str] | None) -> FieldResolution(bullhorn_entity, raw_fields, unresolved)`**

- `fields=None` means every mapped field.
- Templates expand to their placeholders, and a nested `a.b` requests `a`.
- The output is deduplicated, keeps its order, and always includes `id`. The one exception is an entity with no Bullhorn binding, which returns empty `raw_fields` (§4.3).
- An unknown canonical entity or field name raises `UnknownCanonicalFieldError`, because that is a caller error.
- A known field with no mapping goes into `unresolved` and never raises. Examples: a custom name absent from this profile, or a standard field whose default was omitted under AM-3.

**`raw_to_canonical(entity, record: dict) -> CanonicalRecord`** returns a frozen dataclass with:

- `fields`: shared canonical fields that resolved. A Bullhorn `null` becomes `None`.
- `custom`: tenant extension fields that resolved.
- `missing`: canonical names whose raw source wasn't present in the record, either because it wasn't fetched or because this tenant doesn't have it. These are *omitted* from `fields`, never set to `None`, so "null in Bullhorn" stays distinguishable from "not retrieved".
- `unmapped`: every raw key in the record that no mapping consumed, with its value kept verbatim. Nothing is silently dropped.
- `.to_dict()`.

Edge cases:

- A template whose placeholder is missing or `None` lands in `missing`. A partial string is never rendered.
- A nested lookup whose outer value isn't a dict lands in `missing` and never raises.

**`raw_to_canonical_many(entity, records)`** maps the single-record function over a list.

**Errors:** a non-dict record or a non-list input raises `SchemaError`. The translator never throws `KeyError` or `TypeError` on hostile shapes.

### 4.7 `bullhorn/meta.py`: wiring `get_meta()` (read-only use of the existing client)

`MetaDiscovery(client)` implements the `MetaSource` Protocol.

- **`get_entity_meta(entity)`** calls the existing `client.get_meta(entity)` **exactly as it is**: no extra arguments, no subclassing, no monkeypatching, and no direct `_request` calls (AM-1, C-8). It parses the response into `EntityMeta(entity, label, fields: tuple[FieldMeta, ...], warnings)`.
- **`FieldMeta`** holds `name, label, data_type, field_type, required, read_only, options (tuple of (value,label)) | None, associated_entity`.
- **Caching:** results are cached per entity for the instance's lifetime, and `clear_cache()` resets the cache.
- **Graceful degradation:** each of these produces a warning, never an exception:
  - A missing or non-list `fields` value.
  - A non-dict entry.
  - A missing name.
  - A duplicate name (the first occurrence wins).
  - A null label (falls back to the name).
  - An unknown `dataType` (kept as a string).
  - A malformed `options` value (becomes `None`).
- **Errors:** `BullhornAPIError` and `AuthenticationError` propagate to the caller, and discovery catches them per entity.
- **Picklist options** are parsed *only if they are already present* in the response that the unchanged `get_meta()` returns. Phase 3 does **not** add `meta=full` or any other parameter to fetch them. That enrichment is deferred to Phase 4 as an additive client change (DEBT-3, AM-1).

### 4.8 `discovery.py`: SchemaDiscoverer

`SchemaDiscoverer(meta_source, canonical_catalog, bullhorn_catalog, profile|None)`

**`.discover(entities=None, include_sample_values=False, sample_source=None) -> DiscoveryReport`**

- Defaults to every catalog entity, meaning every Bullhorn entity that has a block in the verified Bullhorn catalog.
- Returns one `EntityDiscovery` per entity with these parts:
  - `standard_present`.
  - `standard_missing_in_tenant`: catalog standard fields the tenant meta lacks.
  - `mappings_broken`: canonical mappings, from the catalog or the profile, whose raw source isn't in the tenant meta.
  - `custom_mapped`.
  - `custom_unmapped`: custom-pattern fields in meta with no profile mapping. Each carries its label, type and options, plus an `appears_configured = label != name` flag.
  - `other_unrecognized`: non-custom fields in neither the catalog nor the profile. These are reported, never auto-mapped.
  - `error`: set per entity. One failing entity doesn't abort the others.
- `.to_dict()` is JSON-safe.

**Sample values (opt-in only)**

- Sampling requires `include_sample_values=True` **and** an explicit `sample_source` callable that fetches one record. Without both, no `/search`, `/query` or `/entity` call is made.
- Fields that are sensitive, encrypted or flagged confidential are never sampled.
- Every sampled value passes through `redact_sample()`:

| Value | Redacted form |
|---|---|
| Email-shaped string | `a***@***` |
| Value with 4 or more digits | Digits masked |
| Other string, 3 characters or fewer | `***` |
| Other string, longer than 3 characters | First 2 characters + `***` |
| Number | `<number>` |
| Date | `<date>` |
| Boolean | `<boolean>` |
| Dict | `<object>` |
| List | `<list>` |

**`build_draft_profile(report, tenant, existing=None) -> MappingProfile`** is pure and deterministic.

- `standard` holds only the overrides from `existing`. Catalog defaults aren't copied, so they keep tracking catalog upgrades.
- `custom` is preserved from `existing` verbatim.
- `unmapped_bullhorn_fields` is filled from `custom_unmapped`, sorted by field name.
- Phase 4's setup tools call this directly.

### 4.9 `schema/__init__.py`: SchemaContext facade

`SchemaContext.load(profile_path: Path | None = None) -> SchemaContext`

- Exposes `.canonical`, `.bullhorn`, `.profile_status` and `.translator`.
- Never raises on profile problems. It reports them in `profile_status` instead.
- This is the single entry point that Phase 4 and 5 tools will call, for example through a future `server.get_schema()`. Phase 3 does **not** wire it into `server.py`.

### 4.10 `docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md` (Builder-authored, AM-3)

This file is required even if every name was verified. In that case each table says "None".

It must contain these sections:

1. **Sources consulted:** each URL or document title, the date accessed, and the API version shown, if any.
2. **Omitted Bullhorn entities:** a table of Bullhorn entity, intended canonical entity, and reason not confirmed.
3. **Omitted field mappings:** a table of Bullhorn entity, canonical field, intended raw source (as written in §4.3), and reason not confirmed.
4. **Omitted `standard_fields` / `custom_field_patterns` entries:** a table with the same shape as section 3.
5. **Retained-unverified sensitive patterns:** see §4.2.

If the authoritative reference cannot be reached at all, the Builder **stops and escalates** to the Architect. They must not fill the catalogs from memory, and they must not ship an empty catalog without a decision.

---

## 5. Backward compatibility of raw tools

- No existing file under `tools/`, `server.py`, `auth/`, `crosscutting/`, `config.py` or `bullhorn/client.py` changes. `git diff 4d7da27 --name-status` must show only additions plus `M pyproject.toml`.
- Raw `fields=` strings, `DEFAULT_FIELDS` fallbacks, and Lucene/JPQL passthrough in all 10 tools stay untouched. The canonical layer is opt-in and sits beside these, never in front of them.
- When canonical support is exposed later (Phase 4/5), it will come through new tools or additive parameters whose default reproduces today's raw output. That is a standing constraint for those phases.

## 6. How Phase 4 setup/onboarding consumes this layer

| Phase 4 tool | Phase 3 API it calls |
|---|---|
| `discover_schema` | `SchemaDiscoverer(MetaDiscovery(get_client()), …).discover(...).to_dict()` |
| `review_unmapped_fields` | `DiscoveryReport` → `custom_unmapped` (+ redacted samples if opted in) |
| `apply_field_mapping` | `MappingProfile.from_dict/to_dict` + validation on each edit |
| `finalize_mapping_profile` | `MappingProfile.save` + `SchemaContext.load` (hot reload) |
| `export/import_mapping_profile` | `load/save`; import additionally diffs against `discover()` → `mappings_broken` |
| `connection_status.setup_required` | `load_active_profile().state != "loaded"` |

---

## 7. Acceptance criteria (each mechanically checkable)

The numbering is unchanged from the approved package. Wording is adjusted only where an amendment requires it.

### Packaging & hygiene

**AC-1.** `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` all exit 0.

**AC-2** (amended per AM-4/AM-5). `git diff 4d7da27 --name-status` lists only:

- `A` entries for paths under:
  - `src/bullhorn_mcp/mappings/`
  - `src/bullhorn_mcp/schema/`
  - `tests/` (new files only)
  - `docs/architecture/` (`PHASE3_WORK_PACKAGE.md`, `DEFERRED_DEBT.md`, `PHASE3_UNVERIFIED_MAPPINGS.md`)
- `A src/bullhorn_mcp/bullhorn/meta.py`
- `M pyproject.toml`

There are no `M`, `D` or `R` entries for any other path. In particular, none touch `tools/`, `server.py`, `auth/`, `crosscutting/`, `config.py`, `bullhorn/client.py` or any pre-existing test file.

`git diff 4d7da27 -- pyproject.toml` shows:

- `pyyaml>=6.0` added to runtime dependencies.
- `types-PyYAML` added to dev dependencies.
- Optionally, wheel build-include config needed for AC-12.
- Nothing else.

**AC-3.** `grep -rnE "yaml\.(load|unsafe_load)\(" src` returns nothing.

**AC-4.** `grep -rnE "from \.\.(server|tools)|bullhorn\.client|from \.\. import server" src/bullhorn_mcp/schema` returns nothing.

Additionally (AM-1):

- `git diff 4d7da27 -- src/bullhorn_mcp/bullhorn/client.py` is empty.
- `grep -rn "meta=full" src/bullhorn_mcp` returns nothing.
- `grep -rn "_request" src/bullhorn_mcp/bullhorn/meta.py` returns nothing.

**AC-12** (amended per AM-6). The Builder runs `pip wheel . --no-deps -w <scratch>` and lists the wheel contents, for example with `python -m zipfile -l <wheel>`. The raw output is handed to the Reviewer, and it shows all three of these files:

- `bullhorn_mcp/mappings/canonical_schema.yaml`
- `bullhorn_mcp/mappings/bullhorn_standard_fields.yaml`
- `bullhorn_mcp/mappings/profile.example.yaml`

A test loads both catalogs with `importlib.resources`.

### Catalogs

**AC-5.** The canonical catalog loads and contains exactly the 10 entities in §4.3, each with at least the listed canonical fields. This holds whatever AM-3 omits on the Bullhorn side. All keys match the snake_case regex, and no key equals a Bullhorn entity name from the Bullhorn catalog.

**AC-6** (amended per AM-3).

- Every Bullhorn catalog `default_mappings` key exists in the canonical catalog.
- Every raw source is in `standard_fields`.
- Every `DEFAULT_FIELDS` entry from `client.py:13-19` is in the matching `standard_fields`.
- `docs/architecture/PHASE3_UNVERIFIED_MAPPINGS.md` exists and has the five §4.10 sections.
- No name listed there as omitted appears in `bullhorn_standard_fields.yaml` for that entity. The Reviewer checks this by grep or by a test.
- Every §4.3 mapping is in exactly one of two places: present in `default_mappings`, or listed as omitted in the flag file.

**AC-7.** Each of these raises `CatalogError`: version 2, a missing version, an unknown type, `reference` without `ref`, and a bad key. A fixture with 3 defects reports all 3 in a single error.

### Mapping profile

**AC-8.** Fixtures cover each of these cases, and each produces the expected `ProfileError` (errors are aggregated):

- A valid profile.
- A missing version.
- Version 2.
- An unknown canonical entity.
- An unknown `standard` key.
- A custom name colliding with a canonical field.
- A malformed custom dict.
- Every rejected template form in §4.5.
- A non-identifier raw name.

**AC-9.** A `load → save → load` round-trip produces an equal profile. `profile.example.yaml` loads cleanly. Its raw targets use only names present in the verified Bullhorn catalog, or names matching `custom_field_patterns`.

**AC-10.** `load_active_profile()` returns `none`, `missing`, `invalid` or `loaded` correctly and never raises. For an invalid profile it returns `profile=None`, and a warning appears in `caplog`.

### Translator

**AC-11.** Unit tests cover:

- Template expansion.
- Nested `{field,key}`.
- Override order (custom > standard override > default).
- `id` always included.
- Deduplication.
- `unresolved` for unmapped custom names.
- `UnknownCanonicalFieldError` for unknown names.

**AC-13.** `raw_to_canonical` tests cover:

- A null raw value becomes `None` in `fields`.
- An absent raw key is reported in `missing` and does not appear in `fields`.
- Every unconsumed raw key is in `unmapped` with its value identical.
- A template with a missing part goes to `missing`.
- A nested lookup on a non-dict value goes to `missing`.
- A hostile (non-dict) record raises `SchemaError`, and nothing raises `KeyError` or `TypeError`.

**AC-14.** With no profile, translating a `sample_candidate`-style record uses catalog defaults. With an invalid profile, the output is identical to the no-profile output.

### Meta & discovery

**AC-15.** `MetaDiscovery` is tested with respx-mocked `/meta/{entity}` responses, including these hostile shapes:

- Empty fields.
- A missing `fields` key.
- Non-dict entries.
- A nameless field.
- A duplicate name.
- A null label.
- An unknown dataType.
- Malformed options.
- 1,000 fields.

None of them raises, and each produces the expected warnings. Repeated calls hit the network once, because of the cache. At least one test asserts that each recorded `/meta/` request has `fields=*` and no `meta` query parameter (AM-1).

**AC-16.** A **metadata-only test** runs the default `discover()` over all entities with respx. It asserts that every recorded request path starts with `/meta/` and that none contains `/search`, `/query` or `/entity`.

**AC-17.** A discovery test against fixture tenant meta covers:

- A standard field missing in the tenant appears in `standard_missing_in_tenant`.
- `customText3` with a label appears in `custom_unmapped` with `appears_configured=True`.
- A profile-mapped `customText7` appears in `custom_mapped`.
- A profile mapping to an absent field appears in `mappings_broken`.
- One entity returning a 500 sets `error` on that entity only, and the others complete.

**AC-18.** Sample tests confirm:

- With `include_sample_values=True`, every surfaced value differs from the raw value.
- Email, phone and long strings are masked as specified.
- `customEncryptedText*` and sensitive fields are never passed to the sample source.
- With `include_sample_values=False`, the sample source is never called.

**AC-19.** `build_draft_profile` is deterministic (two calls produce equal output). It preserves `existing.custom` verbatim and sorts the unmapped fields. Its output passes `MappingProfile` validation.

### Backward compatibility

**AC-20.** `TestMCPToolSchemasUnchanged` and every pre-existing test pass with no edits. `git diff 4d7da27 --name-status -- tests` shows only `A` entries.

**AC-21.** Importing `bullhorn_mcp.schema` makes no HTTP request. This is checked by importing it under `respx.mock(assert_all_called=False)` with zero recorded calls, with no Bullhorn environment variables set.

---

## 8. Process & verification

1. **Architect.** D1–D3, ROADMAP-AMENDMENT-1 (QueryBuilder moves to Phase 5), the `meta=full` follow-up (DEBT-3), and the Phase 2 gaps (DEBT-1, DEBT-2) are logged in `docs/architecture/DEFERRED_DEBT.md`. Done.
2. **Builder.**
   - Implements exactly §1–§4, including the AM-3 verification and the §4.10 flag file.
   - Runs `pytest -q`, `ruff check .`, `mypy src/bullhorn_mcp` and the AC-12 wheel build plus listing.
   - Hands the raw output of each command to the Reviewer.
   - Escalates to the Architect (and does not guess) in two cases: the Bullhorn reference is unreachable, or a `DEFAULT_FIELDS` name can't be verified.
3. **Reviewer.** A fresh Reviewer gets only:
   - this work package,
   - `git diff 4d7da27`,
   - the raw command output,
   - `PHASE3_UNVERIFIED_MAPPINGS.md`.

   The Reviewer attacks hostile meta and profile fixtures, template injection, the metadata-only guarantee, redaction leakage, and catalog-name fidelity. For fidelity, they spot-check `bullhorn_standard_fields.yaml` names against the cited reference. They then rule PASS or FAIL against AC-1 through AC-21.
4. **Done.** The phase is done only when the Reviewer passes it with no open blocking findings. Then commit `Phase 3: add canonical schema and mapping layer`.
