# Phase 5B Work Package: Note-Action Setup Completion

- **Status:** binding Architect specification, 2026-10-07.
- **Governing decisions:** `PHASE5_PROPOSAL.md` §0, specifically:
  - D-5-4 (`/settings/commentActionList` only if verified);
  - D-5-6 (5B runs first);
  - D-5-14 / D-5-15 (no tool changes);
  - D-5-16 (5B behaviour, plus P4B-8);
  - D-5-21 (internally rich, externally compact);
  - D-5-22 (privacy and process).
- **Baseline:** committed HEAD `d5330fb` (Phases 0–4B).
- **Tool count:** 19 → **19**. No tool is added, and no tool schema changes.

## 0. Decisions (D-5B-n)

| ID | Decision |
|---|---|
| D-5B-1 | **Discovery sources, in order:** (1) `/meta/Note` `action` options, already captured by the 4A snapshot (HV-A2/B10); (2) `GET /settings/commentActionList`, **only if HV-D1..D3 are verified**. A module constant `SETTINGS_ACTION_SOURCE_VERIFIED` is set from the HV result. If it is `False`, the source is never called, and discovery reports `source_unresolved: settings`. (3) Administrator entry (4A `set_value_mapping`). Values are **never guessed**. |
| D-5B-2 | **Provenance.** Each `note_action` value-mapping record carries an optional `discovery_source ∈ {meta, settings, administrator}`. The field is additive in v2 and absent on older records. Records stay keyed and versioned exactly as in 4A. |
| D-5B-3 | **Adoption is never automatic.** Discovered values reach the profile **only** through `propose_mapping_changes` → `commit_mapping_changes`, using the new op `apply_discovered_note_actions`. That op creates or updates `note_action` records with `source: discovered` and `semantic: null` for the values the admin selects. |
| D-5B-4 | **Lifecycle ops.** The existing ops cover view (`get_mapping_profile`), add/remap (`set_value_mapping`), deactivate (`deactivate_value_mapping`), remove, and version/rollback (`rollback_to`). New ops: `reactivate_value_mapping {key}` restores a deactivated record unchanged, and `apply_discovered_note_actions {values[], key_prefix?}`. Both are propose-only and committed through the 4A commit flow. |
| D-5B-5 | **Compare and stale detection.** `discover_schema` (schema unchanged) records the action-value set per source in the discovery snapshot. The drift report gains `note_action_drift`, with these fields: `new_values` (in Bullhorn, not mapped); `stale_values` (mapped and active, but absent from every verified source); `reactivatable` (deactivated, but present again); `unverifiable` (no verified source available). Any `stale_values` set `drift_unresolved` and lead to `setup_revalidation_required` (4A semantics). Nothing is auto-remapped or auto-deactivated. |
| D-5B-6 | **Status visibility (closes P4B-A1-1).** The `setup_status` **output** gains an additive field, `requirement_details`: a map from capability name to `[requirement strings]`. For `notes.create` it contains `value_mapping:note.action:note_action` when no usable mapping exists. The existing `missing_requirements` and the capabilities aggregation are **unchanged**, so 4A `test_rows` is untouched. |
| D-5B-7 | **P4B-8 fix: scrub before redact.** A `BullhornAPIError` raised by `EntityWriter` keeps the raw error body in a **private, non-rendered** attribute. Its `str()` / `repr()` / `args` never contain the raw body. `safe_error_text(exc, scrub=...)` now proceeds in this order: (1) take the raw body when present; (2) scrub the comment text and its lines of 8 or more characters; (3) redact secrets; (4) bound the result. When no raw body is present, behaviour is unchanged. |
| D-5B-8 | **Privacy (D-5-22).** All fixtures use synthetic action values, for example `"Test Action A"`, and synthetic tenants. No real tenant values, profile or settings payloads are committed. |
| D-5B-9 | **Out of scope:** enabling `create_note` (that is 5A, per D-5-18); the D-5-9 tenant verification procedure (5A); identity and sessions (5A); new tools; and any change to the `create_note` validation semantics (unknown actions already fail closed, per 4B). |

## 1. Files

### 1.1 New files

```
src/bullhorn_mcp/bullhorn/settings_reader.py   # SettingsReader(client).get(names) — authenticated GET /settings/{names};
                                               #   same session/401-once pattern and bounded+redacted errors as writes.py; read-only
src/bullhorn_mcp/notes/action_discovery.py     # sources → {source: values|unresolved}, drift computation (D-5B-5), adoption helper
tests/test_notes_action_discovery.py, tests/test_settings_reader.py, tests/test_phase5b_*.py, tests/fixtures/notes5b/...
docs/architecture/PHASE5B_HV_VERIFICATION.md   # Builder-authored (§3)
```

### 1.2 Pre-existing files that may change (additive only; nothing else)

| File | Allowed change |
|---|---|
| `src/bullhorn_mcp/tenant/changes.py` (4A) | Add the ops `apply_discovered_note_actions` and `reactivate_value_mapping` to the op table and dispatcher. Existing ops behave identically. |
| `src/bullhorn_mcp/tenant/profile_v2.py` (4A/4B) | Add the optional `discovery_source` on value records (parse, validate and `to_dict`; omitted when unset, so the round-trip of old documents is byte-identical). |
| `src/bullhorn_mcp/tenant/revalidation.py` (4A) | Additively extend the snapshot with per-source note-action value sets and add `note_action_drift` to the drift report. Existing report keys are unchanged. |
| `src/bullhorn_mcp/tools/setup.py` (4A) | `discover_schema`: call `notes.action_discovery` internally when Note is in scope (no schema change). `setup_status`: add the `requirement_details` output field. Nothing else. |
| `src/bullhorn_mcp/bullhorn/writes.py` (4B) | The D-5B-7 raw-body retention and the reordered `safe_error_text`. |
| `src/bullhorn_mcp/writes/pipeline.py` (4B) | Only if needed to pass the exception through to `safe_error_text` unchanged (D-5B-7). |

**Must not change:**
- the protected Phase 0–2 files, including `tools/__init__.py`, `server.py`, `client.py`, `crosscutting/*`, `auth/*` and `config.py`;
- `schema/*.py` and the catalogs;
- `pyproject.toml`;
- **every existing test**. No test edit is approved for 5B. If an existing test fails, **stop and escalate**.

## 2. Behaviour and acceptance criteria

### Gates

- **AC-1.** Full `pytest`, `ruff check .` and `mypy src/bullhorn_mcp` exit 0, and the 25/25 regression suite passes. If 5B touches packaging, the wheel still builds and contains the resources. 5B adds no resource.
- **AC-2.** `git diff d5330fb --name-status` shows only `A` entries plus `M` for the files in §1.2. No existing test file shows as `M`.
- **AC-3.** The tool registry is unchanged: there are exactly 19 tools, and every pinned schema test passes unmodified. A new test asserts that the `discover_schema` and `setup_status` schemas equal their 4A pins.
- **AC-4.** These greps hold:
  - `client\._request` has no new hits outside `bullhorn/writes.py` and `bullhorn/settings_reader.py`;
  - `settings_reader.py` issues only `GET` requests;
  - `safe_load` still has exactly 1 hit.

### Discovery and HV guard

- **AC-5.** With `SETTINGS_ACTION_SOURCE_VERIFIED=False` (the default if HV-D1 is unresolved), `discover_schema` makes **no** request to `/settings/*` (respx asserts it), and the report shows `source_unresolved: settings`. Administrator-configured mappings are untouched.
- **AC-6.** With the flag on (mocked verified shape), the `/settings` values are discovered with `discovery_source: settings`. A malformed, non-list or oversized body, or a non-200 response, produces a bounded warning and `unverifiable`. It never raises and never adopts anything.
- **AC-7.** Discovery never mutates the profile: the active version and the version files are byte-identical before and after.

### Adoption and lifecycle

- **AC-8.** `apply_discovered_note_actions` takes effect only through propose → commit. It rejects any value not present in the latest snapshot's verified sources; administrator-entered values use `set_value_mapping`. It also rejects values longer than 30 characters (HV-B10). The committed records have `source: discovered`, `semantic: null` and the correct `discovery_source`.
- **AC-9.** `reactivate_value_mapping` restores a deactivated record with identical content and a new version. On an active or unknown key it is a validation error. Rollback to a prior version restores the prior note-action set.
- **AC-10.** `create_note` validation uses only **active** mappings. A deactivated value is rejected with `valid_values`. A value never mapped is rejected (4B behaviour, re-asserted). No code path substitutes or invents an action, and no request body is built.

### Drift, status and privacy

- **AC-11.** Drift fixtures produce the expected `new_values`, `stale_values`, `reactivatable` and `unverifiable` sets. Stale values set `drift_unresolved`, and the state becomes `setup_revalidation_required`. No mapping is changed automatically.
- **AC-12.** The `setup_status` output contains `requirement_details["notes.create"]`, which lists `value_mapping:note.action:note_action` when no usable mapping exists and is empty once one is committed. `missing_requirements` equals its 4A value for the same fixtures, and 4A `test_rows` passes unmodified.
- **AC-13.** `grep -rniE "<the user's company/tenant identifiers>"` is checked by the Reviewer against the supplied private list. No fixture contains real tenant data. All action values in tests are synthetic.

### P4B-8

- **AC-14.** An error body echoing a comment that contains a secret-like pattern (for example `password=Spring2024 …`) is scrubbed **before** redaction. No fragment of the comment text of 8 or more characters appears in the result, journal or audit (`caplog`).
- **AC-15.** `str(exc)`, `repr(exc)` and `exc.args` never contain the raw body.
- **AC-16.** The B-5 corpus tests still pass unmodified. In the HV docs, P4B-8 is marked **CLOSED**.

## 3. HV-1 items (record in `PHASE5B_HV_VERIFICATION.md`; unresolved means fail closed)

| ID | Mechanism | Guard if unresolved |
|---|---|---|
| HV-D1 | `GET /settings/{name}` request form, and the **response shape** for `commentActionList` (the key name and value type, for example a list or a delimited string) | `SETTINGS_ACTION_SOURCE_VERIFIED=False`; source never called |
| HV-D2 | Whether `commentActionList` values equal the exact strings accepted in `Note.action` (String(30)) | Same as HV-D1 |
| HV-D3 | Permissions and error behaviour of `/settings` for non-admin API users | Treat any non-200 as `unverifiable`; never fall back to guessing |

**Sources:** S1 `GET /settings` (whose example list includes `commentActionList`) and S2 Note.action ("values ... configured in the private label attribute called commentActionList"). Quote these verbatim.

**Tenant verification.** If the docs are insufficient, verification against a connected tenant is allowed (HV-1 rule 2), but only by an administrator, read-only, with the evidence recorded **without** committing tenant values (D-5-22).

## 4. Security & Identity Review

**Required: YES.** 5B touches the consequential-write path (`create_note` action validation and error handling), secrets exposure (P4B-8, scrubbing before redaction) and administrator-only configuration changes. Under `docs/process/SECURITY_REVIEWER.md`, 5B is complete only when **both** the Independent Reviewer and the Security & Identity Reviewer PASS.

Every item below is **blocking** for the Security & Identity Reviewer. The reviewer reproduces each attack independently:

| ID | Attack item |
|---|---|
| S-5B-1 | **Secrets and comment leakage (P4B-8).** No token, password or comment fragment appears in model-visible output, logs, audit, journal or exception `str`/`repr`/`args`. The reviewer covers split, encoded and secret-like comment echoes. |
| S-5B-2 | **Fail-closed action validation.** An unknown, deactivated, case-variant, whitespace-padded, Unicode-confusable or over-length action is never accepted or substituted. No request body is built. |
| S-5B-3 | **Unverified source.** With HV-D1 unresolved, no `/settings/*` call can be triggered through any tool argument or state. A tenant `/settings` value is never adopted without propose → commit. |
| S-5B-4 | **Admin-only configuration.** Discovered adoption, reactivation and rollback cannot bypass 4A's commit gates: actor required, admin allowlist, `diff_hash`, expiry, stale-base rejection. No new path writes the profile. |
| S-5B-5 | **No identity or authorization regression.** No tool schema gains an identity parameter. Legacy permission outcomes are unchanged. `create_note` remains production-disabled (`HV_B11_VERIFIED=False`). |
| S-5B-6 | **Privacy.** No real tenant data is present in fixtures or docs (D-5-22). |

## 5. Process

The order is: Builder → fresh **Independent Reviewer** → fresh **Security & Identity Reviewer** → final gates.

- **What each reviewer receives:** only this package, the diff and the raw gate outputs.
- **Independent Reviewer focus areas:**
  - `/settings` being called while unverified;
  - auto-adoption or auto-remapping;
  - substitution of unknown actions;
  - drift false negatives;
  - scrub-before-redact bypasses;
  - raw-body leakage;
  - schema drift of the setup tools;
  - unnecessary tool growth (D-5-21);
  - tenant data in fixtures.
- **If either reviewer fails the sub-phase:** Architect triage → Builder fix → a fresh instance of each affected reviewer → gates.

**No commit or push** until all Phase 5 sub-phases pass (D-5-22).

---

## 5B Review Triage (2026-10-07)

**Verdicts.** The Independent Reviewer returned **PASS** with 9 non-blocking findings. The Security & Identity Reviewer returned **FAIL** with 1 blocking finding.

**Architect rulings:**
- B-1 stays **blocking**. It is fixed now, not deferred.
- Two non-blocking findings are **folded into this round as local fixes**: Sec-N2 and Sec-N4. They are trivial, sit inside 5B files, and close defects that would surface the moment the `/settings` source is enabled.
- Everything else is logged in `DEFERRED_DEBT.md` ("Phase 5B review follow-ups").

### B-1 (blocking; pre-existing; protected file): path traversal via the caller-supplied `entity` in `BullhornClient`

**Finding.** `bullhorn/client.py` builds request paths from an unvalidated tool argument:
- `:89` `f"/search/{entity}"`
- `:127` `f"/query/{entity}"`
- `:147` `f"/entity/{entity}/{entity_id}"`
- `:160` `f"/meta/{entity}"`

httpx normalizes `../`, so `search_entities(entity="../settings/commentActionList", ...)` sends an **authenticated GET to any path under `rest_url`**. This violates S-5B-3, and it becomes a cross-user exposure on the 5A shared server. The `candidate_id` paths at `:169`, `:195` and `:239` are typed `int` and already checked `> 0`. They are not affected, but the regression test covers them.

**Fix: the exact allowed diff to `src/bullhorn_mcp/bullhorn/client.py`.** Only these changes are allowed; no other line may change.

1. **One added import line** in the import block: `import re`
2. **Added after the `DEFAULT_FIELDS` dict** (module level), exactly this code:

   ```python
   _ENTITY_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}", re.ASCII)


   def _check_entity(entity: object) -> str:
       """Reject anything but a plain Bullhorn entity name before it reaches a URL path."""
       if not isinstance(entity, str) or _ENTITY_NAME_RE.fullmatch(entity) is None:
           raise BullhornAPIError("Invalid entity name")
       return entity


   def _check_entity_id(entity_id: object) -> int:
       """Reject anything but an int (not bool) entity id before it reaches a URL path."""
       if isinstance(entity_id, bool) or not isinstance(entity_id, int):
           raise BullhornAPIError("Invalid entity id")
       return entity_id
   ```

3. **One added line** as the first statement of each method body, after the docstring:
   - in `search`, `query` and `get_meta`: `entity = _check_entity(entity)`;
   - in `get`: `entity = _check_entity(entity)`, plus a second added line, `entity_id = _check_entity_id(entity_id)`.

**Rules for the fix:**
- **No input is echoed.** The error messages are constant and never include the rejected input, so the error text cannot carry an injection payload.
- **Legacy tools are unchanged for valid names.** `search_entities`, `query_entities`, `get_job` and `get_candidate` already map `BullhornAPIError` to `ERROR: …`, so their behaviour and schemas for legitimate entity names are unchanged.
- **Legacy tests must pass unmodified.** If any fails, **stop and escalate**.

**Required regression tests** (a new file, `tests/test_client_entity_validation.py`):
- **R-B1a.** A traversal corpus is run through `client.search`, `client.query`, `client.get` and `client.get_meta`. Each case raises `BullhornAPIError("Invalid entity name")`, and respx records **zero** requests. The corpus:
  - `../settings/commentActionList`;
  - `x/../../settings/commentActionList`;
  - `%2e%2e%2fsettings`;
  - `..%2Fsettings`;
  - `..\\settings`;
  - `Job?x=1`, `Job#a`;
  - `Job Order`, `" Job"`, `"Job\n"`;
  - `ＪobOrder` (fullwidth);
  - `Jöb`;
  - `""`;
  - `"A"*65`;
  - `"1Job"`;
  - `None`, `123`, `["JobOrder"]`.
- **R-B1b.** The same corpus through the **tools** `search_entities` and `query_entities` returns a string starting with `ERROR:` that does **not** contain the input, with zero requests.
- **R-B1c.** `client.get("JobOrder", "1/../../settings/x")`, `client.get("JobOrder", True)` and `client.get("JobOrder", 1.5)` → `Invalid entity id`, with zero requests.
- **R-B1d.** Every legitimate entity name in the Bullhorn catalog passes, and the issued paths are byte-identical to today's (`/search/JobOrder` and so on).
- **R-B1e.** A grep test: every `f"/` path in `client.py` that interpolates `entity` or `entity_id` is in a method whose first statement calls `_check_entity`.

**Regression case 10.** The approved protected-file diff for `client.py` is exactly items 1–3 above. The coordinator freezes it by content hash.

### Folded local fixes (inside 5B files only)

**Sec-N4: no adoption from a forged or stale snapshot.**
- `verified_sources_by_value` must ignore the `settings` source whenever `SETTINGS_ACTION_SOURCE_VERIFIED` is `False`, even if a snapshot claims `settings: verified`.
- `apply_discovered_note_actions.key_prefix` must fullmatch `[a-z][a-z0-9_]{0,31}` (with `re.ASCII`). Generated keys must stay inside the `note.action.` namespace.
- **Tests:**
  - A forged snapshot with `settings` values, with the flag off → adoption is rejected.
  - An over-long prefix, a prefix containing `.`, `/` or uppercase, or a prefix that collides with another entity's namespace → rejected.

**Sec-N2: no payload echo.**
- `action_discovery.py:55` warnings may carry only the **type name and length** of the bad item, never `describe_value(raw)` of `/settings` payload content.
- **Test:** with the flag on and a malformed payload containing a sentinel string, the sentinel appears in no snapshot, drift report, output or log.

### Re-review scope

- **The Security & Identity Reviewer** (fresh) re-verifies B-1 and S-5B-1..6.
- **The Independent Reviewer** (fresh) re-checks AC-1..16, plus the B-1 / Sec-N2 / Sec-N4 tests and the new `client.py` diff (scope only).
- **Gates:** full pytest, ruff, mypy, and 25/25 regression with case 10 extended by the frozen `client.py` hunk.

**Re-review (2026-10-07): PASS.** Both reviewers passed, with no blocking findings. The non-blocking findings are logged as P5B-10..15 in `DEFERRED_DEBT.md` ("Phase 5B close-out"). P5B-10 and P5B-11 are folded into 5A (`PHASE5A_WORK_PACKAGE.md` Amendment A1).
