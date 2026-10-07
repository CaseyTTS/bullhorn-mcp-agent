# Phase 3 Review Triage

- **Reviewer verdict:** FAIL.
- **Triaged by:** Architect (per `docs/process/ARCHITECT.md`), 2026-10-06.
- **Governing spec:** `docs/architecture/PHASE3_WORK_PACKAGE.md`. AC numbering is unchanged.

**Triage rules.** Every Reviewer BLOCKING finding stays blocking. The Architect may promote findings but never demotes them. Each non-blocking finding is logged in `docs/architecture/DEFERRED_DEBT.md` with a target phase. Nothing is dropped.

## Summary

| Finding | Reviewer class | Architect class | Disposition |
|---|---|---|---|
| B1 unhashable value raises `TypeError` | BLOCKING | BLOCKING | Fix F-1 |
| B2 unbounded `repr` in errors (alias bomb) | BLOCKING | BLOCKING | Fix F-2 |
| B3 `$` + `match` accepts trailing newline | BLOCKING | BLOCKING | Fix F-3 |
| N1 nested mapping drops sibling sub-fields | non-blocking | **PROMOTED to BLOCKING** | Fix F-4 |
| N8 profile can override canonical `id` | non-blocking | **PROMOTED to BLOCKING** (Architect clarification) | Fix F-5 |
| N10a `{a:}` empty format spec accepted | non-blocking | **PROMOTED to BLOCKING** | Fix F-6 |
| N10b `{__class__}` placeholder accepted | non-blocking | NON-BLOCKING, no action | DEBT log NB-10 (closed) |
| N2 snake_case typos go to `unresolved` | non-blocking | NON-BLOCKING | DEBT log NB-2 (Phase 5) |
| N3 redaction residue and name-only sensitivity | non-blocking | NON-BLOCKING | DEBT log NB-3 (Phase 4) |
| N4 `ref:` targets not validated | non-blocking | NON-BLOCKING | DEBT log NB-4 (unscheduled) |
| N5 noisy "null label" warning | non-blocking | NON-BLOCKING | DEBT log NB-5 (Phase 4) |
| N6 raw `response.text` in discovery `error` | non-blocking | NON-BLOCKING | DEBT log NB-6 (Phase 4) |
| N7 200 with non-object meta body is a warning, not an error | non-blocking | NON-BLOCKING | DEBT log NB-7 (Phase 4) |
| N9 duplicate YAML keys keep the last value | non-blocking | NON-BLOCKING | DEBT log NB-9 (Phase 4) |

### Why three findings were promoted

**N1 → F-4.** WP §4.6 binds `unmapped` with the words "Nothing is silently dropped." Today a nested mapping marks the whole outer key as consumed. Sibling sub-fields such as `owner.firstName` and `address.zip` then appear in neither `fields` nor `unmapped`. That is silent data loss, and it directly violates a binding clause.

**N8 → F-5.** The spec did not anticipate this case, so the Architect resolves it here. Resolving ambiguity is an Architect duty, and this clarification is now binding. WP §4.6 requires `canonical_to_raw` to "always include `id`". That clause assumes canonical `id` **is** raw `id`. An override such as `standard: {id: customText1}` makes the translator inconsistent:
- It still requests raw `id`.
- It reports a different value as the record's identity.
- Phase 5 lookups by id would then silently target the wrong record.

The fix is cheap and contained, and the Builder is already reworking the same validator for B1–B3.

**N10a → F-6.** WP §4.5 form 2 says "A template string containing only `{rawName}` placeholders" and "nothing else allowed". `{a:}` is not that form. It is accepted only because `string.Formatter().parse` reports `format_spec=''` for both `{a}` and `{a:}`.

---

## Blocking fixes (back to Builder)

These rules apply to every fix:

- Touch only Phase 3 files: `src/bullhorn_mcp/schema/**`, `src/bullhorn_mcp/bullhorn/meta.py`, `src/bullhorn_mcp/mappings/**`, and new `tests/` files.
- Do not edit any pre-existing test. Phase 3 test files created in this phase may be extended.
- AC-1 through AC-21 must all remain satisfied.

### F-1 (B1): Hostile-typed values must never raise outside the aggregated error

**Problem.** `mapping_profile.py:259` (`ftype not in CUSTOM_TYPES`) and `canonical_catalog.py:192` (`ftype not in VALID_TYPES`) raise `TypeError` when the value is unhashable, for example a list or a dict. Any error collected before that point, such as a bad `tenant`, is lost.

**Fix requirements:**

1. Every YAML-sourced value must be type-checked with `isinstance(..., str)` before it is used in a membership test against a `set`, `frozenset` or `dict`, or used as a dict key. This applies in `canonical_catalog.py`, `bullhorn_catalog.py` and `mapping_profile.py`. A non-string `type` gets a normal aggregated error such as `type must be a string`.
2. In addition, `MappingProfile.load`, `MappingProfile.from_dict`, `load_canonical_catalog` (and its internal from-data path) and `load_bullhorn_catalog` must each guarantee that **only** their documented error (`ProfileError` / `CatalogError`) can escape for malformed input.
   - Do this by fixing each site. Do not add a blanket `except Exception` that swallows errors.
   - If a residual broad guard is added as defence in depth, it must re-raise as the documented error **and** still include the errors already aggregated.

**Required regression tests (new test file or new tests in Phase 3 test files):**

- **R-1a.** Use the profile `{version: 1, tenant: 5, entities: {candidate: {custom: {a: {field: owner, key: id, type: [x]}}}}}`.
  - `from_dict` raises `ProfileError`, not `TypeError`.
  - The error lists **both** the `tenant` problem and the `type` problem.
- **R-1b.** The same check for the canonical catalog: a field with `type: [x]` plus a second independent defect. Both are reported in one `CatalogError`.
- **R-1c.** A parametrized test injects each of `[1]`, `{a: 1}`, `5`, `true` and `null` into every value position in profile validation:
  - `version`, `tenant`, `generated_at`;
  - `entities` and its entity value;
  - `standard` key and value;
  - `custom` key and value, plus the custom `type`, `field` and `key`;
  - `unmapped_bullhorn_fields` and its entry fields.

  Every case either loads or raises `ProfileError`. No other exception type escapes.
- **R-1d.** `load_active_profile()` pointed at the R-1a profile written to `tmp_path` returns `state="invalid"` and does not raise.

### F-2 (B2): Error and warning messages must be bounded; no `repr` of non-scalar input

**Problem.** `mapping_profile.py:128`, `canonical_catalog.py:142` and `bullhorn_catalog.py:230` interpolate `{v!r}`. A YAML alias bomb under `version:` expands through `repr`: a 406-byte profile yields a 312 MB message. `load_active_profile` then logs it. The same pattern appears at every `!r` interpolation of YAML-sourced values in `schema/` (see `grep -n "!r}" src/bullhorn_mcp/schema/*.py`).

**Fix requirements:**

1. Add a single helper to `src/bullhorn_mcp/schema/errors.py`. Suggested name: `describe_value(value) -> str`. Its rules:
   - For `str`, `int`, `float`, `bool` and `None`: return `repr(value)` truncated to at most 80 characters, ending in `...` when truncated.
   - For any other type: return `<{type(value).__name__}>`. It must never call `repr`/`str` on containers or iterate them.
2. Every error or warning message in `schema/*.py` that interpolates a YAML-sourced value must use the helper. That includes keys, names, versions, types, templates, raw names and regex patterns. No `!r` or `repr()` may be applied directly to such values.
   - `!r` remains allowed only on values already proven to be identifier-validated strings, and on caller-supplied API arguments in `translator.py`, `discovery.py` and `canonical_catalog.py` lookups. Those must also be bounded: pass them through the helper as well. This keeps the mechanical check simple.
3. `ProfileStatus.errors` and the warning logged by `load_active_profile` must each be bounded. The cap is at most 100 error lines, then one `... and N more` line. Each line is bounded by rule 1.

**Required regression tests:**

- **R-2a.** A ~400-byte profile whose `version:` is a 9-level YAML alias bomb (each level a list of 9 aliases to the previous level). The test:
  - writes it to `tmp_path`;
  - calls `MappingProfile.load`, which must raise `ProfileError` with `len(str(err)) < 10_000`;
  - calls `load_active_profile()` with the env var pointed at it, which must return `invalid`;
  - checks that every `caplog` record message has length < 10_000;
  - finishes in < 2 s of wall-clock time (assert with `time.perf_counter`).
- **R-2b.** The same alias bomb placed under `tenant`, under a custom `type`, and under an `unmapped_bullhorn_fields` entry. Each gets the same bounds.
- **R-2c.** The same alias bomb under `version:` for the canonical and Bullhorn catalog loaders, using their from-data or from-text path. Each raises `CatalogError` with `len(str(err)) < 10_000`.
- **R-2d.** A profile with 500 independent defects. The test checks that the errors are aggregated and that the error count is capped per requirement 3.

**Mechanical check for the Reviewer.** `grep -nE "!r\}|repr\(" src/bullhorn_mcp/schema/*.py` shows no use on a YAML-sourced value. The only uses allowed are inside `describe_value` itself.

### F-3 (B3): Exact-match all identifier and key regexes

**Problem.** `KEY_RE` (`canonical_catalog.py:27`), `RAW_NAME_RE` and `BULLHORN_ENTITY_RE` (`bullhorn_catalog.py:24-25`) are `$`-anchored and applied with `.match`. Python's `$` matches before a trailing `\n`. As a result:

- `"email\n"` passes the §4.4 collision check.
- A raw target `"firstName\n"` is accepted and can reach the Bullhorn `fields=` parameter.

The same applies to nested `field`/`key` parts, template placeholders, and `translator.py:132`. `_EMAIL_RE` (`discovery.py:27`) has the same construction.

**Fix requirements:**

1. Every regex in `src/bullhorn_mcp/schema/` and `src/bullhorn_mcp/bullhorn/meta.py` that validates a whole value must be applied with `.fullmatch(...)`. Remove the `^`/`$` anchors from those patterns, or keep them, but in either case use `fullmatch`. Using `\Z` with `.match` is acceptable only if `fullmatch` is impossible. Note that `is_custom_field` and `is_sensitive` already use `fullmatch`.
2. This covers:
   - canonical entity, field and `ref` keys;
   - profile custom names and `standard` keys;
   - raw names;
   - nested `field`/`key` parts;
   - template placeholders;
   - Bullhorn entity names;
   - the translator's snake_case check;
   - `_EMAIL_RE`.

**Required regression tests:**

- **R-3a.** A profile with `custom: {"email\n": "customText1"}` for `candidate` raises `ProfileError`. The name is rejected as invalid and never treated as a non-colliding name.
- **R-3b.** Each of the following raises `ProfileError` when used as a target:
  - a raw target `"firstName\n"`;
  - a nested `{field: "owner\n", key: id}`;
  - a nested `{field: owner, key: "id\n"}`;
  - a template `"{firstName\n} {lastName}"`.
- **R-3c.** In the catalog validators, each of these raises `CatalogError`:
  - a canonical key `"candidate\n"`;
  - a field key `"first_name\n"`;
  - `ref: "user\n"`;
  - a Bullhorn entity key `"Candidate\n"`;
  - a `standard_fields` entry `"firstName\n"`.
- **R-3d.** `FieldTranslator.canonical_to_raw("candidate", ["first_name\n"])` raises `UnknownCanonicalFieldError`.
- **R-3e.** Property-style assertion: for every `FieldResolution` produced in the translator tests, no element of `raw_fields` contains a character outside `[A-Za-z0-9_]`.

**Mechanical check for the Reviewer.** `grep -nE "_RE\.match\(|re\.match\(" src/bullhorn_mcp/schema src/bullhorn_mcp/bullhorn/meta.py` returns nothing.

### F-4 (N1, promoted): Nested mappings must not silently drop sibling sub-fields

**Problem.** In `translator.py:175,221`, `_extract` for a `NestedField` reports the whole outer key as consumed. Any other sub-keys of that object disappear from the output.

**Fix requirements (Architect decision, binding):**

1. Consumption is tracked at two levels:
   - whole raw keys, from `RawField` sources and template placeholders;
   - `(outer, key)` pairs, from `NestedField`.
2. After all shared and custom mappings are applied:
   - A raw key consumed **whole** is excluded from `unmapped`.
   - A raw key whose value is a dict, and that was **only** consumed through `(outer, key)` pairs, appears in `unmapped` as `unmapped[outer] = {k: v for k, v in record[outer].items() if (outer, k) not consumed}`. Values are verbatim and dict order is preserved. The key is omitted from `unmapped` only if that residual dict is empty.
   - A raw key with no consumption at all appears in `unmapped` with its value identical. This is the current AC-13 behavior and is unchanged.
3. A nested mapping that lands in `missing` consumes nothing. Examples: the outer value is not a dict, or the key is absent.
4. `CanonicalRecord.unmapped` type and `to_dict()` are unchanged. The values are simply now residual dicts where applicable.
5. Update the docstring of `raw_to_canonical` to state this rule.

**Required regression tests:**

- **R-4a.** Record `{"id": 1, "owner": {"id": 5, "firstName": "A", "lastName": "B"}, "address": {"city": "X", "state": "Y", "zip": "1", "address1": "2 Main"}, "foo": [1]}`, translated as `candidate` with no profile:
  - `fields["owner_id"] == 5`
  - `fields["city"] == "X"`
  - `unmapped == {"owner": {"firstName": "A", "lastName": "B"}, "address": {"zip": "1", "address1": "2 Main"}, "foo": [1]}`
- **R-4b.** When every sub-key of an outer object is consumed, the outer key is absent from `unmapped`. Example: `{"id": 1, "owner": {"id": 5}}`.
- **R-4c.** A nested mapping whose outer value is a non-dict, for example `"owner": 7`: `owner_id` is in `missing` and `unmapped["owner"] == 7`.
- **R-4d.** Conservation property over the R-4a record and the AC-14 sample record. Every leaf of the input appears in `fields`, `custom` or `unmapped`. "Leaf" here means each top-level key, and each sub-key of a dict-valued key that a nested mapping touched.

### F-5 (N8, promoted): Canonical `id` is not overridable

**Problem.** A profile may set `standard: {id: customText1}`. `canonical_to_raw` still requests raw `id`, but `raw_to_canonical` then reports `customText1` as the record's identity.

**Fix requirements (Architect clarification of WP §4.4, binding from now on):**

1. The canonical field `id` of every entity always maps to the raw field `id`.
2. A profile `standard:` section containing the key `id` is a load error, aggregated into `ProfileError` with a message naming the entity.
3. The Bullhorn catalog loader cross-validates that every entity's `default_mappings.id` is exactly the raw name `id`. Anything else is a `CatalogError`. The shipped catalog must already satisfy this.
4. The profile `custom:` collision rule already forbids a custom `id`. A test confirms it.

**Required regression tests:**

- **R-5a.** A profile with `entities: {candidate: {standard: {id: customText1}}}` raises `ProfileError`. When given an invalid profile, `load_active_profile` returns `invalid`. The translator output then equals the no-profile output, as in AC-14.
- **R-5b.** Catalog data whose `default_mappings` has `id: { field: owner, key: id }` raises `CatalogError`.
- **R-5c.** A profile with `custom: {id: customText1}` raises `ProfileError` (collision).

### F-6 (N10a, promoted): Templates accept only literal text and exact `{rawName}` placeholders

**Problem.** `bullhorn_catalog.py:91-116` validates templates through `string.Formatter().parse`. That function reports `format_spec=''` for `{a:}` and conversion `None`/`''` edge forms, so `{a:}` is accepted.

**Fix requirements:**

1. Before or instead of `Formatter.parse`, the whole template string must `fullmatch` this pattern:

   ```
   (?:[^{}]|\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_]*\})*
   ```

   That is: literal text, the escaped braces `{{` / `}}`, or `{identifier}` placeholders, and nothing else.
2. The template must still contain at least one placeholder.
3. The same validator is used for catalog `default_mappings`, profile `standard`, and profile `custom` templates.
4. Rendering behavior is unchanged: it uses `record.get` and never `str.format`.

**Required regression tests:**

- **R-6a.** These templates are rejected in both catalog and profile contexts: `"{a:}"`, `"{a!}"`, `"{ a}"`, `"{a }"`, `"{a}{"`, `"}{a}"`.
- **R-6b.** The previously required rejects still fail: `{a.b}`, `{a[0]}`, `{a!r}`, `{a:>5}`, `{}`, `{0}`.
- **R-6c.** These templates are accepted: `"{firstName} {lastName}"` and `"{{x}} {firstName}"`. For the second one, rendering against `{"firstName": "A"}` gives `"{x} A"`.

---

## Re-review scope

After the Builder's rework:

1. The Builder hands the Reviewer:
   - the updated `git diff 4d7da27`;
   - the raw output of `pytest -q`, `ruff check .` and `mypy src/bullhorn_mcp`;
   - the AC-12 wheel listing;
   - the output of the mechanical greps in F-2 and F-3.
2. The Reviewer re-checks **all** of AC-1 through AC-21, not only the fixes.
3. The Reviewer also confirms that each F-1 to F-6 requirement and each named regression test (R-1a through R-6c) exists and passes.
4. AC-2's allowed-file list now also includes `docs/architecture/PHASE3_REVIEW_TRIAGE.md`, which is an Architect-authored, allowed addition.
5. The phase is done only on a Reviewer PASS with no open blocking findings.

Non-blocking items are recorded in `docs/architecture/DEFERRED_DEBT.md` under "Phase 3 review follow-ups".

---

# Round 2 (re-review, 2026-10-06)

- **Reviewer verdict:** FAIL, with one blocking finding.
- **What passed:** AC-1 to AC-21; F-2 to F-6; F-1 except clause #2.

## Round 2 summary

| Finding | Reviewer class | Architect class | Disposition |
|---|---|---|---|
| B-1 YAML parse stage leaks `ValueError` / `RecursionError` | BLOCKING | BLOCKING | Fix F-7 |
| N-a option `value`/`label` accept containers (an alias bomb loads as a "valid" profile) | non-blocking | **PROMOTED to BLOCKING** | Fix F-8 |
| N-b `MappingProfile.load` lets `OSError` escape | non-blocking | NON-BLOCKING | DEBT NB-11 (Phase 4) |
| N-c unbounded `!r` in `meta.py` warnings; sample-source exception text in `discovery.py:317` | non-blocking | NON-BLOCKING | DEBT NB-12 (Phase 4, with NB-6) |
| N-d translator honors an `id` override on a directly constructed profile | non-blocking | NON-BLOCKING | DEBT NB-13 (Phase 4) |
| Info: hostile `dict` subclass `__getitem__` raising `KeyError` | informational | NON-BLOCKING, no action | DEBT NB-14 (closed) |

**Builder round-1 notes.** These were raised outside the review and are dispositioned here so nothing is dropped:

| Note | Disposition |
|---|---|
| (1) dict-subclass `KeyError` | NB-14 |
| (2) unbounded meta warnings | NB-12 |
| (3) unbounded discovery error text | the existing NB-6 |
| (4) `MappingProfile.save` onto a directory raises `PermissionError`/`OSError`, leaving no temp file | NB-11 |

### Why N-a was promoted

N-a breaks three binding clauses:

1. **§4.5 normalization contract.** `unmapped_bullhorn_fields` entries are normalized to the same dict form whose `options` mirror `FieldMeta.options`. That is a "tuple of (value,label)" scalar pairs (WP §4.7), and `meta.py:204` already enforces it by rejecting container values. A profile must not accept a shape that the meta parser rejects.
2. **F-2 intent.** F-2 requires profile handling to stay bounded. Today a 524-byte profile reaches `state="loaded"` while carrying about 387 million elements by reference.
3. **AC-9 in practice.** `to_dict`, `save` and equality on that profile blow up. Equality after a reload took 2.8 s, and serialization would explode further.

The fix is a few lines in the validator the Builder is already touching. Deferring it to Phase 4 would mean shipping a "valid" profile format that Phase 4 must then retroactively narrow.

## Round 2 blocking fixes (back to Builder)

The same standing rules apply as in Round 1: Phase 3 files only, no edits to pre-existing tests, and AC-1 to AC-21 must stay green.

### F-7 (B-1): The YAML parse stage must raise only the documented error

**Problem.** These three parse sites catch only `yaml.YAMLError`:

- `mapping_profile.py:172-175`;
- the `canonical_catalog.py` `from_yaml_text` parse `try` (about lines 195-198);
- `bullhorn_catalog.py:219-222`.

Two hostile inputs get past that handler:

- A 5,000-digit integer makes PyYAML's int constructor raise `ValueError` (Python's int-string conversion limit).
- Deeply nested flow sequences or mappings raise `RecursionError`.

Both escape `MappingProfile.load`, `CanonicalCatalog.from_yaml_text` and `BullhornCatalog.from_yaml_text` raw. `load_active_profile`'s broad handler masks the problem, but the F-1 #2 guarantee is broken.

**Fix requirements:**

1. Each of the three parse sites catches `(yaml.YAMLError, ValueError, RecursionError)` around `yaml.safe_load`.
   - It re-raises as `ProfileError` (profile) or `CatalogError` (catalogs), chaining the cause with `from exc`.
   - The message is bounded per F-2: a fixed prefix (`YAML parse error:` / `YAML value error:` / `YAML nesting too deep`) followed by `truncate_text(str(exc))`. For `RecursionError`, the prefix alone is used, without the exception text.
2. Do **not** catch `Exception` or `BaseException` broadly at these sites. `MemoryError` and `KeyboardInterrupt` must still propagate.
3. Do **not** change `sys.setrecursionlimit` or `sys.set_int_max_str_digits` globally.
4. This applies only to the parse stage. Post-parse validation is already covered by F-1.
5. If any other `yaml.safe_load` call site exists in `src/bullhorn_mcp`, the same rule applies there. The Reviewer checks this with `grep -rn "safe_load" src/bullhorn_mcp`.

**Required regression tests:**

- **R-7a.** A 5,000-digit integer under `version:`, and separately under `tenant:`, in a profile file. `MappingProfile.load` raises `ProfileError` with `len(str(err)) < 10_000`, and `load_active_profile()` returns `invalid`.
- **R-7b.** A 5,000-deep flow sequence (`"version: 1\ntenant: " + "[" * 5000 + "]" * 5000`). `MappingProfile.load` raises `ProfileError`, not `RecursionError`. Also test a 3,000-deep nested block or flow mapping.
- **R-7c.** R-7a and R-7b (5,000-digit int and 5,000-deep flow sequence) for `CanonicalCatalog.from_yaml_text` and `BullhornCatalog.from_yaml_text`. Each raises `CatalogError` with a bounded message. Use the actual public from-text entry points; if these are named differently, test the equivalents that `load_canonical_catalog` / `load_bullhorn_catalog` use.
- **R-7d.** Extend the R-1c intent to the parse stage. A parametrized test over the three loaders × {5,000-digit int, 5,000-deep sequence, 3,000-deep mapping, invalid UTF-8 (profile only)} asserts that the raised exception type is exactly the documented error.

### F-8 (N-a, promoted): Profile option values and labels must be scalars

**Problem.** `mapping_profile.py:343-348` accepts any value for `options[].value` and `options[].label`. A 524-byte profile with a 9-level alias bomb in an option value loads successfully.

**Fix requirements:**

1. In `unmapped_bullhorn_fields[].options[]`:
   - `value` must be one of `str`, `int`, `float`, `bool` or `None`.
   - `label`, if present, must be `str` or `None`.

   Anything else is an aggregated `ProfileError`, and its message uses `describe_value` (F-2). This matches the scalar-pair shape `meta.py:204` enforces. If `meta.py` and the profile end up sharing one scalar check, that is acceptable, but `meta.py`'s observable behavior must not change.
2. No other `unmapped_bullhorn_fields` key may hold a container. `field`, `label`, `data_type` and `field_type` are already string-checked, and `required`/`read_only` are bool-checked. A test confirms this.
3. `build_draft_profile` output must still validate (AC-19). Options come from `FieldMeta`, which is already scalar.

**Required regression tests:**

- **R-8a.** The Reviewer's 524-byte profile: a 9-level alias bomb in `unmapped_bullhorn_fields[0].options[0].value`.
  - `MappingProfile.load` raises `ProfileError` with `len(str(err)) < 10_000`, in under 2 s.
  - `load_active_profile()` returns `invalid`, not `loaded`.
- **R-8b.** The same bomb in `options[0].label`, with the same assertions.
- **R-8c.** `options: [{value: [1]}]`, `[{value: {a: 1}}]` and `[{value: 1, label: [x]}]` each raise `ProfileError`. The scalar forms `[{value: 1}]`, `[{value: "A", label: "Alpha"}]`, `[{value: true}]` and `[{value: null, label: null}]` load. After a `save → load` round-trip, each profile is equal to the original (AC-9).
- **R-8d.** Every non-`options` key of an unmapped entry rejects a list value with `ProfileError`.

## Round 2 re-review scope

1. The Builder hands the Reviewer:
   - the updated `git diff 4d7da27`;
   - the raw output of `pytest -q`, `ruff check .` and `mypy src/bullhorn_mcp`;
   - the AC-12 wheel listing;
   - the F-2/F-3 greps, plus `grep -rn "safe_load" src/bullhorn_mcp`.
2. The Reviewer re-checks AC-1 to AC-21 and F-1 to F-8, including all named tests from R-1a through R-8d.
3. The phase is done only on a Reviewer PASS with no open blocking findings.

---

# Round 3 (re-review, 2026-10-06)

- **Reviewer verdict:** FAIL, with two blocking findings.
- **What passed:** AC-1 to AC-21; F-2 to F-6; F-8 on the profile side.
- **Architect's note.** Both blockers trace back to the Architect's own fix text. F-7 #1 listed exception types one by one, which invited whack-a-mole. F-8 #3 asserted, without checking, that `FieldMeta` options already satisfy the new label rule; they do not. The decisions below replace those clauses.

## Round 3 summary

| Finding | Reviewer class | Architect class | Disposition |
|---|---|---|---|
| B-1 explicit YAML core tags with bad values leak `KeyError` / `IndexError` / `AttributeError` | BLOCKING | BLOCKING | Fix F-9 (supersedes F-7 #1) |
| B-2 meta-derived non-`str` option labels make `build_draft_profile` abort (regression from F-8) | BLOCKING | BLOCKING | Fix F-10 (amends F-8 #3) |
| N-1 option `value: .nan` loads, but `save → load` is not equal | non-blocking | **PROMOTED to BLOCKING**, folded into F-10 | Fix F-10 #4 |

**Why N-1 was promoted.** AC-9 requires `load → save → load` equality. A profile that loads cleanly but cannot satisfy AC-9 is an AC-9 defect. F-10 touches the same validator, so the fix costs nothing extra.

## Round 3 blocking fixes (back to Builder)

The same standing rules apply as in Rounds 1 and 2: Phase 3 files only, no edits to pre-existing tests, and AC-1 to AC-21 must stay green.

### F-9 (B-1): One shared YAML parse helper closes the whole exception class

This supersedes F-7 #1. F-7 #2 to #5 still apply.

**Decision.** Use both approaches the Reviewer suggested:

- **Containment:** a single choke point that catches a broad, but explicitly enumerated, set of built-in exception base classes.
- **Proof:** a generative cross-product test.

**Fix requirements:**

1. **Shared helper.** Add one helper in a new file, `src/bullhorn_mcp/schema/yaml_safe.py`, which is an allowed addition under `schema/`. Its signature is `safe_parse_yaml(text: str) -> Any`.
   - It calls `yaml.safe_load(text)`.
   - It catches exactly this module-level tuple:

     ```python
     YAML_PARSE_ERRORS: tuple[type[BaseException], ...] = (
         yaml.YAMLError,
         ValueError,        # includes UnicodeError and int-digit limit
         RecursionError,
         LookupError,       # KeyError, IndexError
         AttributeError,
         TypeError,
         ArithmeticError,   # OverflowError, ZeroDivisionError
     )
     ```

   - It re-raises one internal exception, `YamlParseFailure(SchemaError)`, carrying a bounded message:
     - `YAML nesting too deep` for `RecursionError`;
     - otherwise `YAML parse error ({type(exc).__name__}): {truncate_text(str(exc))}`.
2. **Callers.** `MappingProfile.load`, `CanonicalCatalog.from_yaml_text` and `BullhornCatalog.from_yaml_text` (or whatever `load_canonical_catalog` / `load_bullhorn_catalog` actually use) each call `safe_parse_yaml`. They convert `YamlParseFailure` into `ProfileError` / `CatalogError` respectively, using `from exc`.
3. **Single call site.** No other code in `src/bullhorn_mcp` calls `yaml.safe_load` directly.
4. **Still forbidden.**
   - Catching `Exception` or `BaseException`. `MemoryError`, `KeyboardInterrupt` and `SystemExit` must propagate.
   - Changing global interpreter limits.
   - Registering custom constructors that widen what YAML can produce. C-5 still holds.
5. **Post-parse values.** Explicit tags that construct *successfully* produce types the validators may not expect: `!!timestamp` gives `datetime`/`date`, `!!binary` gives `bytes`, and `!!set`/`!!omap`/`!!pairs` give `set`/`list`-of-tuples. F-1 #1 already requires every such value to fail validation with an aggregated error. The generative test below proves it.

**Mechanical checks for the Reviewer:**

- `grep -rn "safe_load" src/bullhorn_mcp` shows exactly one hit, in `schema/yaml_safe.py`.
- `grep -rnE "except (Exception|BaseException)\b" src/bullhorn_mcp/schema src/bullhorn_mcp/bullhorn/meta.py` shows no hit at a parse site. Any pre-existing hit elsewhere must already be justified in Round 1 terms: re-raise as the documented error with the aggregated errors included.

**Required regression tests (one new test module, e.g. `tests/test_schema_yaml_fuzz.py`):**

- **R-9a. Generative cross-product.**
  - **Tags (12):** `!!bool`, `!!int`, `!!float`, `!!timestamp`, `!!binary`, `!!null`, `!!str`, `!!seq`, `!!map`, `!!set`, `!!omap`, `!!pairs`.
  - **Hostile values (at least these 16):** `''`, `'_'`, `'-'`, `':'`, `'0x'`, `'0b'`, `'0o'`, `'.'`, `'maybe'`, `'abc'`, a 5,000-digit string, `'1e999999'`, `'é中🙂'`, `'[1, [2]]'`, `'{a: 1}'`, `'!!python/object:os.system'`. The values are emitted as quoted plain scalars where the tag requires a scalar, and as flow collections for `!!seq`/`!!map`/`!!set`/`!!omap`/`!!pairs`.
  - **Positions (at least 2 per loader):** the top-level `version:` and one nested value position. For the profile that is `tenant:` and an `unmapped_bullhorn_fields` entry `label`. For the catalogs it is an entity `description` and a field `type`.
  - **Loaders (3):** `MappingProfile.load` (via `tmp_path`), the canonical from-text loader, and the Bullhorn from-text loader.
  - **Assertion:** each case either loads successfully or raises **exactly** the documented error (`ProfileError` or `CatalogError`). The check is `type(exc) is ProfileError` (or `CatalogError`), or the matching subclass check if the hierarchy defines subclasses. `len(str(exc)) < 10_000`. No other exception type escapes.
  - **Size:** at least 12 × 16 × 2 × 3 = 1,152 parametrized cases, and the module finishes in under 30 s.
- **R-9b.** The Reviewer's exact repros, each through all three loaders, each raising only the documented error: `!!bool maybe`, `!!bool ''`, `!!timestamp abc`, `!!int ''`, `!!int _`, `!!int '-'`, `!!float ''`, `!!float _`.
- **R-9c.** `MemoryError` still propagates. Monkeypatch `yaml.safe_load` inside `yaml_safe` to raise `MemoryError`, and assert it is not converted.
- **R-9d.** R-7a to R-7d stay in place and keep passing.

### F-10 (B-2 + N-1): Meta-derived options must always produce a valid draft; option values must round-trip

This amends F-8 #3. F-8 #1 (the profile label is `str | None`) **stands**.

**Decision.** The profile format stays strict, and the draft builder normalizes. The reasons:

- Relaxing the profile label rule to any scalar would put a type-ambiguous label into a long-lived, user-edited artifact.
- `meta.py` observable behavior must not change.
- Normalization therefore happens at the single boundary where meta data becomes profile data: `build_draft_profile` in `schema/discovery.py`.

**Fix requirements:**

1. **Label normalization.** When `build_draft_profile` copies `custom_unmapped[].options` into `UnmappedField.options`, each option `(value, label)` is converted as follows:

   | `label` type | Draft label |
   |---|---|
   | `str` | unchanged |
   | `None` | `None` |
   | `bool` | `"true"` / `"false"` (lowercase, JSON spelling); checked **before** `int`, since `bool` subclasses `int` |
   | `int` | `str(label)` |
   | finite `float` | `repr(label)`, Python's shortest round-trip form, e.g. `1.5` → `"1.5"` |
   | non-finite `float` | `None` |
   | anything else | the whole options list for that field becomes `None` (defensive; `meta.py` already rejects containers) |

2. **Value normalization.**
   - Option `value`s of type `str`, `int`, finite `float`, `bool` and `None` are kept unchanged.
   - If any value in a field's options is a non-finite float (`nan`, `inf`, `-inf`; Python's `json` module accepts these), that field's `options` becomes `None` in the draft. The field itself is still listed.
3. **Draft never aborts.** For any `DiscoveryReport` that `SchemaDiscoverer.discover` can produce, `build_draft_profile` must not raise `ProfileError`. With requirements 1 and 2 in place, revalidation (`discovery.py:404`) is expected to always pass.
   - Do **not** add per-entity try/except isolation inside `build_draft_profile`. If revalidation fails, it is a bug and must surface loudly, so a silently partial draft is worse.
   - Requirement 3 is instead enforced by the R-10 tests, as a property.
4. **N-1: non-finite floats are rejected in profiles.**
   - In profile validation, `unmapped_bullhorn_fields[].options[].value` must not be a non-finite float. `.nan`, `.inf` and `-.inf` each produce an aggregated `ProfileError`, with the message built via `describe_value`.
   - This keeps AC-9 equality well-defined.
   - No other profile position accepts floats today. If any does, the same rule applies there.
5. **`meta.py` unchanged in behavior.** `FieldMeta.options` keeps the raw meta types: `{"value": 1}` still parses to `((1, 1),)`. Existing `meta.py` tests from this phase pass unmodified.

**Required regression tests:**

- **R-10a. End-to-end, parametrized.** Mock `/meta/Candidate` with respx: one custom field `customText1`, with a label, carrying each of these `options` shapes in turn. Run `MetaDiscovery.get_entity_meta`, then `SchemaDiscoverer.discover`, then `build_draft_profile(report, tenant="t")`. Assert:
  - no exception;
  - `MappingProfile.from_dict(draft.to_dict())` equals the draft;
  - after `save` to `tmp_path` and `load`, the reloaded profile equals the draft;
  - the draft options for `customText1` equal the expected value below.

  | Meta `options` | Expected draft options |
  |---|---|
  | `[{"value": 1}]` | `((1, "1"),)` |
  | `[{"value": true}]` | `((True, "true"),)` |
  | `[{"value": 1.5}]` | `((1.5, "1.5"),)` |
  | `[{"value": "A", "label": 7}]` | `(("A", "7"),)` |
  | `[{"value": "A"}]` | `(("A", "A"),)` |
  | `[{"value": "A", "label": null}]` | `(("A", None),)` |
  | `[{"value": "A", "label": "Alpha"}]` | `(("A", "Alpha"),)` |
  | `[{"value": 2, "label": 2.0}]` | `((2, "2.0"),)` |
  | `[{"value": "A", "label": false}]` | `(("A", "false"),)` |

- **R-10b. Non-finite values from meta.** The respx body is raw text containing `NaN`, `Infinity` or `-Infinity` as an option value, for example `{"fields":[{"name":"customText1","label":"X","options":[{"value":NaN}]}]}`, which httpx decodes with Python's `json`. The draft has `options is None` for that field, the field is still present, and save/load equality holds.
- **R-10c. Multi-entity property.** A discovery run over at least 3 entities, where every entity carries a mix of the R-10a and R-10b shapes. `build_draft_profile` succeeds, and every entity appears in the draft.
- **R-10d. N-1 in profiles.** A profile whose option value is `.nan`, `.inf` or `-.inf` raises `ProfileError`. A profile whose option value is `1.5` round-trips equal.
- **R-10e. `meta.py` behavior pinned.** `parse` / `get_entity_meta` on `[{"value": 1}]` yields options `((1, 1),)`, and on `[{"value": "A", "label": 7}]` yields `(("A", 7),)`. This proves the normalization happens only in the draft builder.
- **R-10f.** AC-19's existing tests pass unmodified: determinism, `existing.custom` preserved verbatim, and the sorted order of unmapped fields.

## Round 3 re-review scope

1. The Builder hands the Reviewer:
   - the updated `git diff 4d7da27`;
   - the raw output of `pytest -q`, `ruff check .` and `mypy src/bullhorn_mcp`;
   - the AC-12 wheel listing;
   - the F-2/F-3/F-9 greps.
2. The Reviewer re-checks AC-1 to AC-21 and F-1 to F-10, with F-9 superseding F-7 #1 and F-10 amending F-8 #3. That includes all named tests from R-1a through R-10f.
3. The phase is done only on a Reviewer PASS with no open blocking findings.

---

# Round 4 (re-review, 2026-10-06)

- **Reviewer verdict:** FAIL, with three blocking findings.
- **What passed:** AC-1 to AC-21 except AC-9; F-2 to F-9.
- **What this round changes.** Rather than patch three individual bugs, each fix below closes a whole **defect class**. Each class gets two things:
  - a single structural rule, enforced at one choke point;
  - a generative or property test whose corpus would also have caught the sibling cases.

  The Builder must implement the class rule, not just the repro.

## Round 4 summary

| Finding | Defect class | Reviewer class | Architect class | Disposition |
|---|---|---|---|---|
| B-1 U+0085 (NEL) folds to a space on `save → load` | **Serialization fidelity:** a profile that validates may not survive `save → load` | BLOCKING | BLOCKING | Fix F-11 |
| B-2 Unicode digits match `customText\d+`, and the draft aborts for every entity | **Unicode / identifier consistency:** classification regexes and validation regexes disagree on the character set | BLOCKING | BLOCKING | Fix F-12 |
| B-3 catalog regexes raise `OverflowError` / `RecursionError` on compile | **Untrusted regex strings:** arbitrary regex syntax in data files | BLOCKING | BLOCKING | Fix F-13 |
| Note: `import bullhorn_mcp.schema` loads `server`/`tools` through the package `__init__` | n/a | note | NON-BLOCKING | DEBT NB-16 |

**Siblings proactively folded in.** No Reviewer finding raised these. They belong to the same classes, so they are included now so that round 5 does not find them:

- **F-11 #4: `DiscoveryReport.to_dict()` must be strict JSON.** WP §4.8 says "JSON-safe". Meta option values can be `NaN`/`Infinity`, because Python's `json` module accepts them, so they can reach `to_dict()` today.
- **F-12 #4: redaction must stay Unicode-aware.** Forcing `re.ASCII` on the redaction regexes would make `_DIGIT_RE` stop masking non-ASCII digits. That would be a privacy regression. The rule is therefore split: classification regexes are ASCII, and masking regexes are Unicode-wide.

## Round 4 blocking fixes (back to Builder)

The same standing rules apply as in Rounds 1 to 3: Phase 3 files only, no edits to pre-existing tests, and AC-1 to AC-21 must stay green.

### F-11 (B-1): Serialization fidelity. Every valid profile round-trips, and the code enforces this, not just the tests

**Class rule.** Any `MappingProfile` that validates must satisfy `load(save(p)) == p`. A profile that cannot be serialized losslessly must never be written.

**Fix requirements:**

1. **Escape all non-ASCII on write.** `MappingProfile.save` (`mapping_profile.py:184`) calls `yaml.safe_dump` with `allow_unicode=False`, so every non-ASCII and control character is written as a YAML escape. The other arguments stay `sort_keys=False, default_flow_style=False`. The file is still written as UTF-8 with `newline="\n"`.
2. **Verify before writing.** Before creating the temp file, `save` must re-parse the dumped text through `safe_parse_yaml` (F-9), rebuild it with `MappingProfile.from_dict`, and compare it to `self`.
   - If the two are not equal, `save` raises `ProfileError` with a bounded message such as `profile cannot be serialized losslessly`. It does not create the temp file and does not touch the target.
   - This closes the class even for characters the corpus below misses.
   - Factor the dump-and-verify step into one function, for example `MappingProfile.to_yaml_text() -> str`, which `save` uses. Phase 4 export will reuse it.
3. **No other writer.** No other code in `src/bullhorn_mcp` serializes a profile to YAML. The Reviewer checks this with `grep -rn "safe_dump\|yaml.dump" src/bullhorn_mcp`, which must show exactly one hit, inside `to_yaml_text`.
4. **Class extension: `DiscoveryReport.to_dict()` is strict JSON.** `json.dumps(report.to_dict(), allow_nan=False)` must succeed for every report `discover()` can produce.
   - To achieve that, `discover()` applies the F-10 #2 rule when it builds `custom_unmapped`: if any option value is a non-finite float, that field's `options` becomes `None` and a warning is added.
   - `build_draft_profile` keeps its own F-10 normalization, which is idempotent.
   - `meta.py` stays unchanged (F-10 #5).

**Required regression tests (new module, e.g. `tests/test_schema_roundtrip_fuzz.py`):**

- **R-11a. Character corpus, built programmatically in the test.** The corpus is:
  - every code point U+0000–U+001F, U+007F and U+0080–U+009F (all C0 and C1 controls, which includes NEL U+0085);
  - U+00A0, U+00AD, U+034F, U+061C, U+115F, U+180E, U+200B–U+200F, U+2028, U+2029, U+202A–U+202E, U+2060–U+2064, U+2066–U+2069, U+3000 and U+3164;
  - U+FEFF, U+FFF9–U+FFFB, U+FFFC, U+FFFD, U+FFFE and U+FFFF;
  - the lone surrogates U+D800, U+DBFF, U+DC00 and U+DFFF;
  - U+FDD0 and U+FDEF;
  - U+1F600, U+E0001, U+E007F and U+10FFFF;
  - the combining marks U+0301 and U+20DD;
  - **one representative code point for each Unicode general category.** For each of the 30 categories in `unicodedata`, take the first code point `c` in `range(0x110000)` where `unicodedata.category(chr(c)) == cat`.
- **R-11b. Every string position × corpus × placement.** Each character is placed alone, as a prefix, as a suffix, and in the middle (for example `"a" + ch + "b"`).
  - **String positions:** `tenant`, `generated_at`, unmapped `label`, `data_type`, `field_type`, option `value` (as a `str`) and option `label`, and the literal text of a template in both `standard` and `custom`. A template example is `"{firstName}" + s + "{lastName}"`, skipping `{` and `}`, which F-6 governs.
  - **For each case:**
    - If `MappingProfile.from_dict(d)` raises `ProfileError`, the case passes vacuously. Rejecting is allowed; corrupting is not.
    - Otherwise `MappingProfile.load(tmp)` after `p.save(tmp)` must equal `p`.
    - For template cases, the rendered output against a fixed record must also be equal before and after the round-trip.
  - **Expected scale:** about 100 corpus characters × 4 placements × 9 positions, so roughly 3,600 cases. The suite must still finish in under 60 s.
- **R-11c. Non-string scalars.** Option values `0`, `-0`, `2**63`, `-(2**63)`, `10**4000` (must either round-trip or be rejected by the save self-check with `ProfileError`; never a raw exception), `-0.0`, `5e-324`, `1.7976931348623157e308`, `True`, `False` and `None`. Each round-trips equal or is rejected with `ProfileError`.
- **R-11d. Draft path.** A respx-mocked `/meta/Candidate` whose custom field `label`, `dataType`, `type` and option labels and values are drawn from the R-11a corpus, with 10 corpus characters per field. The flow is `discover → build_draft_profile → save → load`, and the result equals the draft. Include the Reviewer's cases explicitly: field label `"Region\u0085Code"` and option label `"x\u0085y"`.
- **R-11e. Save self-check is live.** Monkeypatch the dumper inside `to_yaml_text` to emit `allow_unicode=True` text. Then `save` of a profile with `tenant="acme\x85corp"` raises `ProfileError`, and the target path does not exist afterwards.
- **R-11f. Strict-JSON report.** For R-10b's `NaN`/`Infinity`/`-Infinity` meta bodies and for the R-11d corpus, `json.dumps(report.to_dict(), allow_nan=False)` succeeds, and the affected field has `options is None` plus a warning.
- **R-11g. The Reviewer's exact repros.** These all round-trip equal: `tenant "acme\x85corp"`, a template `"{firstName}\x85{lastName}"` (rendered output unchanged), the field label `"Region\u0085Code"`, and the option label `"x\u0085y"`.

### F-12 (B-2): Identifier character-set consistency. Classification can never admit a name that validation rejects

**Class rule.** "Raw Bullhorn field name" has exactly one definition: `RAW_NAME_RE` (ASCII `[A-Za-z_][A-Za-z0-9_]*`, applied with `fullmatch`). Every classifier that can route a meta field name toward the profile must be a **subset** of that definition.

**Fix requirements:**

1. **ASCII flag on identifier and classification regexes.** Every regex in `src/bullhorn_mcp/schema/` and `src/bullhorn_mcp/bullhorn/meta.py` that validates or classifies an *identifier* is compiled with `re.ASCII`. That includes:
   - `KEY_RE`, `RAW_NAME_RE`, `BULLHORN_ENTITY_RE`, `TEMPLATE_RE` and `_PLAIN_SEGMENT`;
   - every `custom_field_patterns` and `sensitive_field_patterns` entry, at `bullhorn_catalog.py:179-180` and the validation compile at about `:285`.
2. **Gate on the single definition.**
   - `BullhornCatalog.is_custom_field(name)` returns `True` only if `RAW_NAME_RE.fullmatch(name)` **and** a custom pattern matches. This is defense in depth on top of #1.
   - `is_sensitive` keeps returning `True` for any match. It may be broader, never narrower.
3. **Invalid names are reported, never routed.** In `SchemaDiscoverer`, a meta field whose name fails `RAW_NAME_RE.fullmatch`:
   - is never put into `standard_present`, `custom_mapped` or `custom_unmapped`;
   - is never passed to a sample source;
   - is listed in `other_unrecognized`;
   - adds one bounded warning per entity naming the count, plus up to 10 names via `describe_value`;
   - therefore can never reach `build_draft_profile`.

   `meta.py` behavior is unchanged: `EntityMeta` still contains the field.
4. **Masking regexes stay Unicode-wide.** This prevents a privacy regression. `_DIGIT_RE` and any other regex whose job is to mask or redact sample values (`discovery.py:29-30`) must **not** use `re.ASCII`.
   - The digit-masking test must confirm that non-ASCII digits are masked.
   - `_EMAIL_RE` may stay Unicode-wide.
   - Record this exemption in a one-line comment at the regex definitions.
5. **Draft-never-aborts, as a property.** F-10 #3 is restated with this class included. For **any** meta response body that is a JSON object, the pipeline `discover()` → `build_draft_profile()` → `save` → `load` never raises and round-trips equal.

**Mechanical checks for the Reviewer:**

- Every `re.compile(` in `src/bullhorn_mcp/schema/*.py` and `bullhorn/meta.py` either passes `re.ASCII` or appears on the explicit masking allowlist (`_DIGIT_RE`, `_EMAIL_RE`).
- A test asserts `pattern.flags & re.ASCII` for every compiled pattern held by a loaded `BullhornCatalog` (custom and sensitive) and for every module-level identifier regex.

**Required regression tests (new module, e.g. `tests/test_schema_identifier_fuzz.py`):**

- **R-12a. Hostile name corpus, built programmatically.** For each base name in `customText`, `customTextBlock`, `customInt`, `customFloat`, `customDate`, `customObject{}s`, `customEncryptedText`, `customBillRate` and `customPayRate`, the test builds:
  - names using **every** `Nd` (decimal digit) code point from `unicodedata` in place of the ASCII digit. Iterate `range(0x110000)` and take every code point with category `Nd`, deduplicated to the first digit-one of each block (that is about 70 code points, one per digit block);
  - the ASCII name with each R-11a corpus character appended, prepended, and inserted after the prefix;
  - Cyrillic and Greek homoglyph swaps (for example `сustomText1` with a Cyrillic `с`, and `customΤext1` with a Greek `Τ`);
  - fullwidth Latin (`ｃｕｓｔｏｍＴｅｘｔ１`);
  - NFKC-equivalent forms.
- **R-12b. Classification property.** For every R-12a name `n`:

  ```
  bullhorn_catalog.is_custom_field(n) == (n.isascii() and RAW_NAME_RE.fullmatch(n) is not None and any custom pattern fullmatches n)
  ```

  For every name that is not a valid raw name, `is_custom_field(n)` is `False`.
- **R-12c. End-to-end property.** For each Bullhorn entity in the catalog, a respx-mocked meta response contains:
  - all R-12a names, as fields with labels;
  - one valid `customText1`;
  - one valid `customText3` with a label.

  The run goes `discover()` → `build_draft_profile()` → `save` → `load`. Assert:
  - no exception, and the round-trip is equal;
  - every invalid name is in `other_unrecognized` and in no other bucket;
  - the warning count is bounded (one per entity);
  - the draft's `unmapped_bullhorn_fields` contains `customText1` and `customText3` and no invalid names;
  - with `include_sample_values=True`, the sample source is never called with an invalid name.
- **R-12d. The Reviewer's repro.** `customText١`, `customText１`, `customObject٢s` and `customInt\U0001d7d9`, through a real `BullhornClient` + respx. The draft builds for all entities.
- **R-12e. Masking stays Unicode-wide.** `redact_sample("١٢٣٤٥٦٧")`, `redact_sample("１２３４５")` and `redact_sample("call ٠٥٥١٢٣٤٥٦٧")` contain none of the original digit characters. For every string `v` in the R-11a corpus combined with digits, `redact_sample(v) != v`.

### F-13 (B-3): Untrusted regex strings. Catalog patterns are restricted to a fixed grammar

**Class rule.** Catalog pattern lists are data, not code. Rather than accept arbitrary regex syntax and try to contain everything it can do, the loader accepts only a fixed, linear-time pattern grammar. That rules out:
- compile-time errors such as overflow and recursion;
- match-time catastrophic backtracking (ReDoS), which is the sibling the Reviewer did not raise;
- Unicode-class surprises (F-12).

**Fix requirements:**

1. **Fixed grammar.** Each entry in `custom_field_patterns` and `sensitive_field_patterns` must, as a **string**, fullmatch this meta-grammar (compiled with `re.ASCII`):

   ```
   [A-Za-z_][A-Za-z0-9_]*(?:\\d\+[A-Za-z0-9_]*)?
   ```

   In words: an identifier literal, optionally followed by exactly one `\d+` and an optional identifier suffix.
   - Every pattern currently shipped fits this grammar: `customText\d+`, `customObject\d+s`, `ssn`, `dateOfBirth`, and so on.
   - Anything else is an aggregated `CatalogError` naming the entry via `describe_value`, for example `pattern must be an identifier optionally containing one \d+`.
   - The loader also enforces a length cap of 64 characters per pattern and 64 entries per list.
2. **Compile only after the grammar check.** Patterns are compiled only after they pass the grammar check, with `re.ASCII`, and used only with `fullmatch`.
   - As defense in depth, the compile call is wrapped in `except (re.error, *YAML_PARSE_ERRORS)` (the F-9 tuple), converting to `CatalogError`.
   - A pattern that passes the grammar check is expected never to fail compilation. A test asserts this.
3. **Same rule on every code path.** The validation path (about `bullhorn_catalog.py:284-288`) and the construction path (`:179-180`) use one shared function, so the two cannot drift.

**Required regression tests (new module, e.g. `tests/test_schema_catalog_regex_fuzz.py`):**

- **R-13a. Hostile patterns.** Each of these, placed in each of the two lists through `BullhornCatalog.from_yaml_text`, raises exactly `CatalogError` with a bounded message, in under 1 s each:
  - **The Reviewer's repros:** `'a{4294967296}'` and `'(' * 2000 + ')' * 2000`.
  - **ReDoS shapes:** `'(a+)+$'`, `'(a|a)*b'`, `'(.*)*x'`.
  - **Other regex syntax:** `'customText\\w+'`, `'customText\\d*'`, `'customText[0-9]+'`, `'(?i)ssn'`, `'.*'`, `''`, `'\\'`, `'['`, `'a' * 65`.
  - **Unicode in the literal:** `'customText١'`.
  - **Non-string entries:** `5`, `[x]`, `null`.
- **R-13b. List cap.** A list of 65 valid patterns raises `CatalogError`.
- **R-13c. Grammar soundness, generative.** For about 500 random strings generated from the meta-grammar (seeded, deterministic), each one:
  - compiles without error;
  - fullmatches a matching ASCII name in under 10 ms;
  - does not match the same name with the digits replaced by `١`.
- **R-13d. Shipped catalog.** The packaged `bullhorn_standard_fields.yaml` loads unchanged, and every one of its patterns passes the grammar.

## Round 4 re-review scope

1. The Builder hands the Reviewer:
   - the updated `git diff 4d7da27`;
   - the raw output of `pytest -q`, `ruff check .` and `mypy src/bullhorn_mcp`;
   - the AC-12 wheel listing;
   - the F-2, F-3 and F-9 greps, plus:
     - `grep -rn "safe_dump\|yaml.dump" src/bullhorn_mcp` (F-11 #3);
     - `grep -rn "re.compile(" src/bullhorn_mcp/schema src/bullhorn_mcp/bullhorn/meta.py` (F-12 #1).
2. The Reviewer re-checks AC-1 to AC-21 and F-1 to F-13, including all named tests from R-1a through R-13d.
3. The Reviewer is also invited to rerun their own round-4 repro scripts (`review4/roundtrip.py`, `e2e.py`, `respx_digits.py` and `regex_attack.py`) against the new code.
4. The phase is done only on a Reviewer PASS with no open blocking findings.

---

# Round 5: PASS (independent re-review, 2026-10-06)

- **Reviewer verdict:** **PASS**, with no blocking findings.
- **Architect triage:** none of the three new non-blocking findings is promoted. Each is logged in `docs/architecture/DEFERRED_DEBT.md`:

| Finding | Architect class | Disposition |
|---|---|---|
| 1. `to_yaml_text` self-check revalidates against the packaged canonical catalog, not the one the profile was built with | NON-BLOCKING | NB-17 (Phase 4) |
| 2. The F-11 self-check and AC-9 equality are type-blind (`True == 1 == 1.0`, `-0.0 == 0.0`) | NON-BLOCKING | NB-18 (Phase 4) |
| 3. `redact_sample` has fixed points (`'ab***'`, `'***'`, `None`, `'a***@***'` → `'a****'`) | NON-BLOCKING | NB-19 (Phase 4, grouped with NB-3) |

**Wording conflict in finding 3 (spec defect, not a code defect).** For the fixed-point inputs, the literal wording of AC-18 ("every surfaced value differs from the raw value") conflicts with the binding §4.8 redaction table, which mandates exactly these outputs: for example, any string of 3 characters or fewer becomes `***`.

The Architect's ruling is that the more specific §4.8 table governs. AC-18's intent is that no raw value is surfaced un-redacted, and that holds: the fixed points are inputs that are already in redacted form, so nothing leaks. The AC-18 wording is to be corrected when NB-19 / NB-3 are addressed in Phase 4.

**Carried forward.** NB-6 and NB-12 (unbounded discovery error and warning text) are unchanged and not worsened. They remain Phase 4 items.

**Phase status.** Phase 3 is done under `docs/process/ARCHITECT.md`: the Reviewer passed it with no outstanding blocking findings. The planned commit message is `Phase 3: add canonical schema and mapping layer`.
