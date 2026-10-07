# Phase 4A Work Package: Tenant Setup & Mapping Management

**Status.** Architect specification, 2026-10-06. This package is optimized for speed: decisions are made here, not left to the Builder.

**Inputs:**
- `REQ_TENANT_SETUP_MAPPING_MANAGEMENT.md` (TS-1..7, HV-1, CT-1)
- `ROADMAP.md` revision 3, Phase 4A
- `DEFERRED_DEBT.md`
- The Phase 3 APIs, used **as they exist**:
  - `MappingProfile.from_dict`, `load_canonical_catalog`, `load_bullhorn_catalog`;
  - `parse_target` / `is_raw_name` (`schema/bullhorn_catalog.py`);
  - `SchemaDiscoverer`, `MetaDiscovery`, `FieldTranslator`, `SchemaContext`;
  - `safe_parse_yaml` / `YAML_PARSE_ERRORS` (`schema/yaml_safe.py`);
  - `describe_value` / `truncate_text` / `cap_errors` (`schema/errors.py`).

**Baseline.** HEAD after Phase 3, with Phase 3 committed.

**Approach.** Phase 4A adds a **new** package and **new** tools. Phase 3 code is not rewritten.

---

## 0. Decisions (D-4A-n). These are binding for the Builder; the user may revise them later

| ID | Topic | Decision (most conservative reasonable default) |
|---|---|---|
| D-4A-1 | Q-S1 tenant identity | The administrator supplies `tenant_id` in the first change set (`init_tenant`), as an identifier string. The store also records the connected `session.rest_url` as a **fingerprint**. A changed fingerprint puts the tenant in `setup_revalidation_required`; it never deletes or invalidates mappings. No Bullhorn login field is assumed. |
| D-4A-2 | Q-S2 administrator / actor | The actor comes from the env var `BULLHORN_MCP_ACTOR`. It is never taken from a tool argument, because an LLM could spoof that. `commit_mapping_changes` is refused when the actor is unset. If `BULLHORN_SETUP_ADMINS` (a comma list) is set, the actor must be in it. |
| D-4A-3 | Q-S3 retention | Keep **all** versions. No pruning in 4A. |
| D-4A-4 | Q-S4 revalidation trigger | Event-driven only. The triggers are: catalog fingerprint changed, `rest_url` fingerprint changed, unresolved drift recorded by `discover_schema`, or an imported version not yet validated against meta. There is no time-based trigger. |
| D-4A-5 | Q-S5 deactivation | Supported (`active: false`) and distinct from removal. Both create a new version. |
| D-4A-6 | Q-S6 partial setup | Gating is per capability. A capability whose own requirements are valid may report `ok` even when the overall state is not `setup_valid`. |
| D-4A-7 | Storage location | Env var `BULLHORN_SETUP_STORE` (a directory). There is **no default path**: if unset, `setup_status` reports it as a missing requirement. The `config/` package stays in DEBT-2 (Phase 9). `BULLHORN_MAPPING_PROFILE` (v1 file) behavior is unchanged. |
| D-4A-8 | Profile format | A **new v2 document** in the new package. `schema/mapping_profile.py` is **not** changed and v1 still loads. v2 is converted to a v1 `MappingProfile` **only via** `MappingProfile.from_dict` for translation. That conversion also closes NB-13 by construction. |
| D-4A-9 | Approval for config changes | Two steps. `propose_mapping_changes` acts as the dry-run: it validates, diffs, hashes and stores the proposal. `commit_mapping_changes` requires `proposal_id` + `diff_hash` + `decision` + the actor (D-4A-2), and rejects expired or stale proposals. It is self-contained in the new package; `crosscutting/approval.py` is **not** modified. 4B may later lift it into a shared primitive. |
| D-4A-10 | DEBT-3 (`meta=full`) | **Not taken in 4A.** `client.py` is not modified. 4A uses picklist `options` only when the existing `fields=*` response already contains them. Otherwise value mappings are administrator-confirmed and marked `values_unverified`, which is a warning, not an error. DEBT-3 and NB-15 are re-deferred to **4B**, where action-type discovery needs them. |
| D-4A-11 | Timezone (SB-4) | 4A delivers the `reporting_timezone` setting, with default `"UTC"` and validation via `zoneinfo.ZoneInfo` (an unresolvable name is a validation error), plus a UTC-only coercion primitive. Conversion into the reporting zone, and any `tzdata` dependency, go to Phase 5, the first consumer. |
| D-4A-12 | Samples | 4A tools **never** expose sample values (`include_sample_values` is not offered). NB-3 and NB-19 are re-deferred to "before any tool exposes samples" (Phase 5). |
| D-4A-13 | Canonical additions | Two product-defined fields are added to `job` in `canonical_schema.yaml`: `priority` and `primary_recruiter_id` (reference to `user`). They get **no** Bullhorn default mapping, so setup reports them `unresolved` until an administrator maps them. A canonical `user` entity is **not** added: Phase 3 test `test_exactly_the_ten_entities` must pass unmodified. RAG-1 and NB-4 are re-deferred to Phase 5. |
| D-4A-14 | `connection_status` | **Unchanged.** Setup state is exposed only by the new `setup_status` tool. |
| D-4A-15 | Concept targets | Value-mapping concept targets come from a new packaged list, `mappings/activity_concepts.yaml`. It holds `version: 1` plus the 15 concept IDs from `CANONICAL_ACTIVITY_VOCABULARY.md` §2, verbatim. 4B extends it into full definitions (D4). |

**Correction (4A review P4A-3, 2026-10-06).** The vocabulary in §2 lists **16** concept IDs, so "15" in D-4A-15 is a miscount. The shipped `activity_concepts.yaml`, which has 16 IDs, is correct.

**Escalation.** No item needs escalation. Every default above avoids assuming Bullhorn behavior.

## 1. In scope

### 1.1 New files

```
src/bullhorn_mcp/tenant/__init__.py        # public API re-exports
src/bullhorn_mcp/tenant/profile_v2.py      # TenantProfileV2 dataclasses, from_dict/to_dict, to_v1_profile()
src/bullhorn_mcp/tenant/yaml_strict.py     # duplicate-key check via yaml.compose + safe_parse_yaml (no new safe_load site)
src/bullhorn_mcp/tenant/store.py           # SetupStore: versions, active pointer, proposals, history, discovery snapshot
src/bullhorn_mcp/tenant/changes.py         # change ops, apply-to-draft, diff, diff_hash, proposals, commit
src/bullhorn_mcp/tenant/validation.py      # structural + meta-snapshot validation, conflicts, dependencies
src/bullhorn_mcp/tenant/revalidation.py    # discovery snapshot + drift report (TS-5)
src/bullhorn_mcp/tenant/state.py           # setup state machine + capability gating (TS-7)
src/bullhorn_mcp/tenant/capabilities.py    # capability → required mappings registry (initial entries below)
src/bullhorn_mcp/tenant/actor.py           # actor resolution (D-4A-2)
src/bullhorn_mcp/tenant/timeutil.py        # coerce_epoch_millis_to_utc_iso(); validate_timezone()
src/bullhorn_mcp/tools/setup.py            # the 6 MCP tools (§2)
src/bullhorn_mcp/mappings/activity_concepts.yaml
tests/test_tenant_*.py, tests/test_tools_setup.py, tests/fixtures/tenant/...
docs/architecture/PHASE4A_HV_VERIFICATION.md   # Builder-authored (§4)
```

### 1.2 Pre-existing files that may change: additive only, exact

| File | Change | Owning phase of file |
|---|---|---|
| `src/bullhorn_mcp/tools/__init__.py` | **One added line** after line 7: `from . import setup  # noqa: F401`. Line 7 itself is unchanged. | Phase 2 (protected; regression case 10 must be amended to allow exactly this line) |
| `src/bullhorn_mcp/mappings/canonical_schema.yaml` | **Two added lines** inside `job.fields`: `priority: { type: string, description: "Tenant-mapped job priority." }` and `primary_recruiter_id: { type: reference, ref: user, description: "Tenant-mapped primary recruiter." }` | Phase 3 |

**No other pre-existing file may change.** That includes `server.py`, `client.py`, `crosscutting/**`, `auth/**`, `config.py`, every other `schema/*.py`, `bullhorn_standard_fields.yaml`, `pyproject.toml` and every existing test.

If adding the two canonical fields makes **any** existing test fail, the Builder must **stop and escalate**. Tests must not be edited.

### 1.3 Profile v2 document (D-4A-8)

```yaml
format: tenant-profile/v2          # discriminator; distinct from v1 "version: 1"
tenant: {id: <str>, label: <str|null>}
profile_version: <int ≥1>          # assigned by store on commit
catalog_fingerprint: <sha256 of packaged canonical+bullhorn catalog text>
rest_url_fingerprint: <str|null>
field_mappings:                     # one record per (entity, field); 'id' fields are rejected
  - {entity, field, kind: standard|custom, target: <Phase 3 target form>, type: <custom only, optional>,
     source: standard|discovered|administrator, active: bool,
     validation: {state: valid|unresolved|broken|unvalidated, checked_at, detail},
     created_at, updated_at}       # ISO-8601 UTC strings
value_mappings:                     # key unique
  - {key, target: {kind: concept, name: <activity_concepts id>} | {kind: ordering, entity, field},
     bullhorn_field: <raw name on the canonical entity's Bullhorn entity>, values: [str|int ...],
     source: discovered|administrator, active: bool, validation: {..., values_unverified: bool},
     created_at, updated_at}
settings: {reporting_timezone: "UTC"}
```

**Validation rules (aggregated `ProfileError`; Phase 3 bounding rules apply):**

- **Field mappings.**
  - The canonical entity and field must exist; `kind=custom` names follow the Phase 3 custom rules.
  - Targets are validated by `parse_target`.
  - The Bullhorn entity is implied by `load_bullhorn_catalog().bullhorn_entity_for(entity)`. If that returns `None`, the result is an error.
  - At most one **active** record per (entity, field).
  - Values must be finite scalars (F-10).
  - Mapping `id` is an error (F-5).
- **Value mappings.**
  - An `ordering` target must reference an existing canonical field.
  - Two active concept mappings sharing the same `(bullhorn_field, value)` are a **conflict** error. Example: one value mapped to both `interview_completed` and `interview_cancelled`.
- **`to_v1_profile()`.** Builds `{version:1, tenant:id, generated_at, entities:{e:{standard:{…active standard-kind}, custom:{…active custom-kind}}}}` and returns `MappingProfile.from_dict(...)`.

### 1.4 Store layout (`BULLHORN_SETUP_STORE`)

```
versions/v000001.yaml   immutable (created with open(..., "x")); v2 docs
active.json             {"version": n, "activated_at", "actor", "correlation_id"}   (atomic os.replace)
proposals/<id>.json     {base_version, changes, draft, diff, diff_hash, validation, actor, created_at, expires_at(+24h), status}
history.jsonl           append-only: {correlation_id, timestamp, actor, action, version, prev_version, diff, validation_summary}
discovery/latest.json   last discovery snapshot (meta-only) + drift report + checked_at
```

**Store rules:**

- The store wraps all `OSError` as `SetupStoreError(SchemaError)`, with a bounded message (closes NB-11 for store paths).
- Every YAML read goes through `yaml_strict.parse()`. That function runs `yaml.compose` under the `YAML_PARSE_ERRORS` guard, rejects duplicate mapping keys at any depth, and then calls `safe_parse_yaml` (closes NB-9 for 4A paths).
- `grep -rn "safe_load" src/bullhorn_mcp` must still return exactly 1 hit, and `yaml.load(` must not appear.

### 1.5 Change operations (`propose_mapping_changes.changes`)

| `op` | Fields |
|---|---|
| `init_tenant` | `tenant_id`, `label?`. Only allowed when no active version exists. |
| `apply_verified_defaults` | `entities?`. Materializes catalog `default_mappings` whose raw sources are present in the latest discovery snapshot as `source: standard` records. Absent ones are reported `unresolved`. |
| `set_field_mapping` | `entity`, `field`, `target`, `kind?`, `type?` |
| `deactivate_field_mapping` / `remove_field_mapping` | `entity`, `field` |
| `set_value_mapping` | `key`, `target`, `bullhorn_field`, `values` |
| `deactivate_value_mapping` / `remove_value_mapping` | `key` |
| `set_setting` | `name: reporting_timezone`, `value` |
| `rollback_to` | `version`. Its content equals that version and receives a new version number. |
| `import_document` | `path`; accepts v1 or v2. A v1 file is converted to records with `source: administrator` and `validation: unvalidated`. |

**Proposal processing:**

- **Base.** A proposal is built against `base_version` (the current active version, or none). It produces the full draft, a diff of `{key, change: added|removed|changed|deactivated, old, new}`, conflicts, `dependencies` (the capabilities from the registry affected by each key), and validation against the latest discovery snapshot.
- **`diff_hash`.** `diff_hash = sha256(type-tagged canonical JSON of {base_version, diff})`. Type-tagged means each scalar is encoded as `[type_name, repr]` (closes NB-18 for v2).
- **Commit.** On commit, the proposal is re-validated. The commit is **refused** if any of these holds:
  - the proposal is expired;
  - the hash does not match;
  - `base_version` is not the current active version (stale; never a silent overwrite);
  - the actor is missing or not allowed;
  - any structural error or conflict exists;
  - any **active** mapping is in state `broken`.

  Mappings in states `unresolved` and `values_unverified` are allowed, and they are reported.
- **On success.** Write the new version file, switch the active pointer, append to history, and emit `audit.log_invocation`. A rollback whose target revalidates as `broken` is refused; there is no override in 4A.

### 1.6 State machine (`state.compute_setup_state`)

This is a pure function of: the env (credentials, store, actor), the connection check result, the store contents, and the current catalog fingerprint. The first matching row wins.

| # | Condition | State |
|---|---|---|
| 1 | Any of the 4 credential env vars is missing, or the connection check fails | `disconnected` |
| 2 | The store is not configured, or no active version exists and no open (unexpired) proposal exists | `connected_setup_required` |
| 3 | No active version exists, and ≥1 open proposal exists | `setup_in_progress` |
| 4 | The active version fails structural load/validation, or has any active `broken` mapping in its last recorded validation | `setup_invalid` |
| 5 | Catalog fingerprint ≠ recorded, `rest_url` fingerprint ≠ recorded, unresolved drift exists in `discovery/latest.json`, or the active version was imported and is still `unvalidated` | `setup_revalidation_required` |
| 6 | Otherwise | `setup_valid` |

**Output fields:**
- `state`
- `missing_requirements[]` (strings such as `"env:BULLHORN_SETUP_STORE"`, `"mapping:job.priority"`)
- `capabilities{name: {ok, missing[]}}`
- `active_version`
- `open_proposals`
- `last_validation`

**Capability registry (initial).**

| Capability | Requires |
|---|---|
| `jobs.priority` | an active, valid field mapping `job.priority`, plus an `ordering` value mapping for `job.priority` |
| `jobs.primary_recruiter` | an active, valid `job.primary_recruiter_id` |
| `canonical_reads` | state ∈ {`setup_valid`, `setup_revalidation_required`} |

The gate API is `require_capability(name) -> GateResult(ok, missing)`.

### 1.7 Revalidation report (TS-5; produced by `discover_schema`)

**Discovery.**
- Discovery is metadata-only (Phase 3 `SchemaDiscoverer`), covering the catalog entities.
- The snapshot stores, per entity and field: `name`, `label`, `data_type`, `field_type`, `required`, `read_only`, and `options` (when present).

**Report contents.** Compared against the previous snapshot and the active version:
- `new_fields`, `removed_fields`;
- `changed_fields` (any snapshot attribute changed);
- `broken_mappings` (an active field mapping's raw source is absent);
- `newly_unmapped` (custom-pattern fields not mapped and not present before);
- `value_drift` (a value-mapping value is no longer among the field's options, or options are absent, in which case it is reported `unverifiable`);
- `meta_unusable` (the entity had a structural warning and zero fields; NB-7). For an entity marked `meta_unusable`, broken/removed findings are **suppressed**.

**What the report never does.** It never mutates mappings. Any finding sets `drift_unresolved=true` in `discovery/latest.json`. That flag is cleared only by a successful commit, or by a `manage_mapping_profile(action="validate")` run that finds no broken or drift findings.

**Error and warning text** in the tool output is bounded: `describe_value` / `truncate_text`, ≤100 warnings per entity, and raw `BullhornAPIError` text truncated to 300 characters (closes NB-6 and NB-12 at the MCP boundary).

## 2. Public MCP tool surface (CT-1): 6 new tools; the 10 existing tools are unchanged

| Tool | Signature (all params JSON-schema-typed) | R/W |
|---|---|---|
| `setup_status` | `(check_connection: bool = True)` | R |
| `discover_schema` | `(entities: list[str] \| None = None)`. Metadata-only; writes the discovery snapshot and report. | R (Bullhorn) / W (local snapshot) |
| `get_mapping_profile` | `(view: str = "active", version: int \| None = None, other_version: int \| None = None, entity: str \| None = None, search: str \| None = None)`. `view` ∈ {`active`, `version`, `history`, `diff`, `proposal`}. For `proposal`, `version` holds no meaning and `search` holds the proposal ID. | R |
| `propose_mapping_changes` | `(changes: list[dict], base_version: int \| None = None)` → `{proposal_id, diff_hash, expires_at, diff, conflicts, dependencies, validation}`. This *is* the dry-run. | W (local proposal only) |
| `commit_mapping_changes` | `(proposal_id: str, diff_hash: str, decision: str)`. `decision` ∈ {`approve`, `reject`} → `{status, version?, correlation_id}` | W (local) |
| `manage_mapping_profile` | `(action: str, path: str \| None = None, format: str = "v2")`. `action` ∈ {`validate`, `export`}. `validate` = fresh discovery + validation of the active version + recording the result. `export` writes the active v2 doc, or a v1 view if `format="v1"`, via an atomic write. **Import and rollback go through `propose_mapping_changes`** (`import_document` / `rollback_to`), so they are never activated silently. | R / W (local file) |

**Behavior shared by every tool:**
- Calls `permissions.check(tool, operation)` and `audit.log_invocation`, following the existing tool pattern.
- Returns `server.format_response(...)`.
- Converts every `SchemaError` / `SetupStoreError` into a bounded `ERROR:` string. Raw exceptions never escape.
- **No tool writes to Bullhorn.**

## 3. Out of scope

- Bullhorn record writes and `create_note` (4B). Note action-type discovery and enforcement (4B). DEBT-3 / `meta=full` (4B).
- A canonical `user` entity, RAG-1 and NB-4 (5). Entity reads, `find_*`, `query_builder`, conversion into the reporting timezone, `tzdata` (5).
- Sample values (D-4A-12). Time-based revalidation. Version pruning. Rollback override.
- Changing `connection_status`, `crosscutting/*`, `client.py`, `server.py`, the `schema/*.py` modules, or `bullhorn_standard_fields.yaml`.
- The `config/` package (DEBT-2, Phase 9).

## 4. HV-1: Bullhorn mechanisms 4A depends on

The Builder **verifies each item** against https://bullhorn.github.io/rest-api-docs/ and the entity reference, then records the result (source URL, date, verified / unresolved) in `docs/architecture/PHASE4A_HV_VERIFICATION.md`. Any unresolved item must not be relied on by code: guard it, and report it as `unresolved`.

| ID | Mechanism | 4A use |
|---|---|---|
| HV-A1 | `GET /meta/{Entity}?fields=*` response: `fields[]` with `name`, `label`, `dataType`, `type`, `optional`/required, `readOnly`, `options` | Snapshot attributes |
| HV-A2 | Whether `options` (picklist values) can appear in the `fields=*` response, and its shape | `values_unverified` logic. If it is unresolved, every value mapping is `values_unverified`. |
| HV-A3 | Bullhorn timestamp representation (epoch milliseconds) for `dateAdded`-type fields | `coerce_epoch_millis_to_utc_iso`. If unresolved, the primitive ships but is unused, and this is documented. |
| HV-A4 | Nested `owner` association with sub-field `id` on JobOrder | Already verified in Phase 3. Cite that verification. |
| HV-A5 | `restUrl` from the REST login response | Already used by the existing code; fingerprint only |

4A uses **no** `/settings` or action/status-list endpoints.

## 5. Deferred-debt disposition (the Architect updates `DEFERRED_DEBT.md` at phase close)

**Closed in 4A:**

| Item | Scope of closure |
|---|---|
| RAG-9 | v2 format |
| RAG-2 | Canonical field and mapping part |
| RAG-3 | Mapping part |
| RAG-8 | Setting plus UTC primitive part |
| OWG-1 | Value-mapping mechanism |
| OWG-3 / OWG-5 | Configuration-change parts |
| OWG-10 | Actor |
| NB-6 / NB-12 | At the MCP boundary |
| NB-7 | |
| NB-9 | 4A paths |
| NB-11 | Store paths |
| NB-13 | By construction |
| NB-18 | v2 |

**Re-deferred:**

| Item | New target |
|---|---|
| DEBT-3, NB-15 | 4B |
| NB-3, NB-19 | Phase 5, before samples are exposed |
| NB-5 | 5 |
| NB-4, RAG-1 | 5 |
| NB-17 | 5. Not triggered: 4A uses only the packaged catalog. |

## 6. Acceptance criteria (each mechanically checkable)

**Gates**

- **AC-1.** `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` each exit 0.
- **AC-2.** The Phase 0–3 regression suite passes 25/25. Case 10 is amended *only* to allow the single added line in `tools/__init__.py`.
- **AC-3.** `git diff <phase3-head> --name-status` lists only `A` entries, plus `M src/bullhorn_mcp/tools/__init__.py` and `M src/bullhorn_mcp/mappings/canonical_schema.yaml`. The diff of each of those two files consists solely of the added lines given in §1.2.
- **AC-4.** `pip wheel . --no-deps` contains `bullhorn_mcp/mappings/activity_concepts.yaml` and `bullhorn_mcp/tenant/*.py`. This is verified by listing the wheel contents.
- **AC-5.** These grep checks hold:
  - `grep -rn "safe_load" src/bullhorn_mcp` returns exactly 1 hit;
  - `grep -rnE "yaml\.(load|unsafe_load)\(" src` returns 0 hits;
  - `grep -rn "_request\|get_meta(" src/bullhorn_mcp/tenant src/bullhorn_mcp/tools/setup.py` returns 0 hits (all meta access goes through `MetaDiscovery`).
- **AC-6.** `TestMCPToolSchemasUnchanged` and `TestConnectionStatus` pass unmodified. The tool registry contains the 10 original tools plus exactly the 6 tools in §2. A test pins all 16 names and the schemas of the 6 new tools.

**Profile and store**

- **AC-7.** v2 validation errors are aggregated, and fixtures cover each of these cases:
  - unknown entity or field;
  - an `id` mapping;
  - two active records for one field;
  - an invalid target (every Phase 3 rejected form);
  - a `kind=custom` name colliding with a canonical field;
  - a concept value conflict;
  - an unknown concept;
  - an `ordering` target on an unknown field;
  - a non-finite value;
  - a duplicate YAML key (via `yaml_strict`);
  - an invalid timezone.

  Each case raises `ProfileError`; nothing raises `TypeError`, `KeyError` or any other unexpected exception. The F-9-style hostile YAML corpus (≥ 20 cases) run through `yaml_strict.parse` raises only the documented error.
- **AC-8.** For a v2 doc, `to_v1_profile()` produces a profile accepted by `MappingProfile.from_dict`. A `FieldTranslator` built from it maps `job.priority` → `customText12` when the record targets `customText12`. A v1 profile file imported through `import_document` produces records with `source=administrator` and `validation=unvalidated`. Phase 3 `load_active_profile` behavior is unchanged: its existing tests pass.
- **AC-9.** Version files are immutable. A second write to an existing version path raises `SetupStoreError`. The active pointer is switched atomically. `history.jsonl` gains exactly one line per commit, containing the actor, correlation ID, diff and validation summary. A store directory that is unreadable or not a directory produces `SetupStoreError`, never `OSError`.

**Controlled change (TS-4) and rollback (TS-5)**

- **AC-10.** `propose_mapping_changes` writes nothing except `proposals/`. The active version and the version files are byte-unchanged afterwards. The returned diff shows `old` and `new` values for every changed key, and the returned `dependencies` list the affected capabilities.
- **AC-11.** `commit_mapping_changes` is refused, and nothing changes, in each of these cases:
  - wrong `diff_hash`;
  - expired proposal (simulated clock);
  - stale `base_version` (another commit happened first);
  - `BULLHORN_MCP_ACTOR` unset;
  - actor not in `BULLHORN_SETUP_ADMINS` when that variable is set;
  - a conflict;
  - an active `broken` mapping.

  An approved commit creates version n+1, activates it, and returns `correlation_id`. A `reject` decision marks the proposal rejected and activates nothing.
- **AC-12.** `rollback_to(k)` produces a new version whose records equal version k's, and the history records the rollback. A rollback to a version that is now `broken` is refused.
- **AC-13.** `diff_hash` changes when one value changes from `1` to `True`, or from `1` to `1.0` (type-aware).

**State, gating and revalidation (TS-5, TS-7)**

- **AC-14.** A parametrized test drives `compute_setup_state` through every row of §1.6 and asserts the state and `missing_requirements`. Credential rotation (the same store, with new credential env values and the same `rest_url`) still yields `setup_valid` (TS-6). A changed `rest_url` yields `setup_revalidation_required`, and the mappings are retained.
- **AC-15.** The capability gate returns `ok=False` with exact `missing` entries for `jobs.priority` when the priority mapping is absent, and `ok=True` once it is mapped and valid, even while another capability is unresolved (D-4A-6).
- **AC-16.** `discover_schema` against respx-mocked `/meta` responses issues only `/meta/` requests: every recorded request path starts with `/meta/`. A fixture pair of "previous" and "current" snapshots yields each report category in §1.7, including `meta_unusable` suppression. The active version is unchanged after discovery, and `drift_unresolved` is set.
- **AC-17.** `apply_verified_defaults` creates `source: standard` records only for defaults whose sources are present in the snapshot. Absent ones appear as `unresolved`. `job.priority` and `job.primary_recruiter_id` are `unresolved` after defaults, because they have no default mapping.

**Tools and boundaries**

- **AC-18.** Every tool returns a bounded string: no output line is longer than 10,000 characters for 1,000-field meta fixtures and hostile error bodies. No tool calls any Bullhorn write. With respx, the only HTTP calls ever made are `GET /meta/*` plus the login flow, exercised through `check_connection`.
- **AC-19.** No sample values appear anywhere in the outputs of `discover_schema` or `get_mapping_profile`. A test asserts that the `sample` key is absent.
- **AC-20.** `PHASE4A_HV_VERIFICATION.md` exists. It lists HV-A1 to HV-A5, each with a source and a verdict. Code paths that depend on an unresolved item are guarded, and a test covers each one.
- **AC-21.** `coerce_epoch_millis_to_utc_iso(0) == "1970-01-01T00:00:00Z"`. Non-int, bool, NaN or out-of-range input raises `ValueError`, never a raw exception from another family. `validate_timezone("UTC")` passes. An unknown zone name fails with a bounded message.

**Process.** The process is Builder → fresh Reviewer. The Reviewer receives only this package, the diff and the raw outputs of the gates. The Reviewer should concentrate on: proposal and commit bypass, stale or concurrent commits, duplicate-key and hostile YAML, path traversal in `export` / `import` paths (they must be resolved and must not escape via symlinks into the store), unbounded output, and state-machine edges.

**Commit message:** `Phase 4A: add tenant setup and mapping management`.
