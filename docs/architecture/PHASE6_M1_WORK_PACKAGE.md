# Phase 6 — Milestone M1 Work Package: `get_recruiting_metrics` (period counts and stage ratios, two-tier)

| | |
|---|---|
| **Status** | Binding Architect specification, 2026-10-07. Time-boxed vertical slice. D-5-25 scope discipline applies. **Amendments M1-A and M1-B** (at the end of this document) are binding. Both reviews have passed; M1-B is the last fix. **Superseded in part by `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8.8 (D-6-1..D-6-4): see "Policy supersession" below.** |
| **Governing documents** | D-5-5, D-5-11, D-5-15, D-5-21, D-5-24 (`PHASE5_PROPOSAL.md`); `REQ_RECRUITING_ANALYTICS_READ_MODEL.md` §8 (TT-1..TT-8, and §8.8); `CANONICAL_ACTIVITY_VOCABULARY.md`; `PHASE5C_WORK_PACKAGE.md` C4-2 / P5C-1. |
| **Baseline** | Phase 5 HEAD (READY FOR COMMIT). Tools: 22 → **23**. |
| **Data path** | **Live Bullhorn only**, through the existing 5C internal services. There is no analytics store; the Analytics/Data Pipeline Agent's recommendation is deferred to a later milestone. |

> **Policy supersession (2026-10-07; D-6-1..D-6-4).** Under §3 of this document, **any** `workspace_only` caller, including a user who has logged out of Bullhorn, receives Tier 2 aggregates. That behaviour is **superseded**:
> - Tier 2 analytics requires an explicit, admin-controlled Workspace analytics grant.
> - Being logged out or unlinked never grants it.
>
> The M1 code does not yet conform. This is P6-12: pre-production required, and it **blocks any shared deployment that exposes this tool**. Tier 1 behaviour (the caller's own session) already conforms to D-6-1.

## 1. The slice (and what it is not)

**One tool.**

```
get_recruiting_metrics(metrics: list[str], date_from: str, date_to: str, period: str = "month")
```

- `metrics`: 1–8 IDs from the packaged catalog (§2).
- `period`: `"month"` or `"quarter"`.
- Bounds are **date-only**, in the tenant `reporting_timezone`, and must fall exactly on period boundaries. `date_to` is exclusive.
- The range covers at most 12 months or 4 quarters.
- There are **no** other parameters: no filters, no `group_by`, no scope, no tier argument.

**Out of M1 (deferred to `DEFERRED_DEBT.md` as P6-*):**
- recruiter, client, job or geography dimensions, and the dominance rule;
- drill-back IDs;
- cohort (tracked) funnels;
- rounding;
- the query budget and differencing history (Q-T5);
- an analytics store, caching and timeline merge;
- composites;
- a dashboard contract.

**Why M1 needs no differencing history.** Tier 2 sees only fixed, period-aligned cells and never sums over ranges. Every returned number is a single aligned cell value, and the same cell always has the same value, so differencing across requests reveals nothing new. (Amended by M1-B: this holds only for closed periods.)

## 2. Metric catalog (`mappings/metric_catalog.yaml`, version 1; packaged resource)

| Metric ID | Kind | Definition (reuses the 5C concept derivers; nothing is redefined) |
|---|---|---|
| `jobs_created` | count | `job_created` events per period |
| `client_submissions` | count | `client_submission` events per period |
| `interviews_scheduled` | count | `interview_scheduled` events per period |
| `offers_extended` | count | `offer_extended` events per period |
| `placements_created` | count | `placement_created` events per period |
| `submission_to_interview` | period ratio | `interviews_scheduled` ÷ `client_submissions`, within the same period |
| `interview_to_offer` | period ratio | `offers_extended` ÷ `interviews_scheduled`, within the same period |
| `offer_to_placement` | period ratio | `placements_created` ÷ `offers_extended`, within the same period |

**Rules:**
- **Concepts come only from the vocabulary.** Each concept's availability and definition come only from `tenant/capabilities.concept_requirements` and `activity/derivers`, which are the 5C single sources.
- **Unavailable concepts.** If a concept is `definition_missing`, `unsupported` or `setup_revalidation_required`, the metric returns that status with `missing_requirements` and no numbers. The same applies to any ratio that uses the concept.
- **Counting.** Counts enumerate events through the 5C internal activity service: `activity/service.py`, called directly with an explicit `IdentityContext` and client.
- **Internal page budget.** The service gets one additive keyword, `max_pages` (default unchanged at 5; the metrics path passes 40). The 30 s budget of D-5C-12 still applies.
- **Incomplete results.** If enumeration is not `complete`, the metric's status is `incomplete`, with **no** number. A partial count is never returned.

## 3. Tiers (identity-derived only; REQ §8)

| Tier | Execution identity | Output |
|---|---|---|
| `bullhorn_user` / `local` | The **caller's own** session (`server.get_client()`), so Bullhorn permissions apply | Exact per-period cells; range totals; ratios; `provenance` (profile, catalog and policy versions; keyed `request_hash`). No record IDs. |
| `workspace_only` (Tier 2) | The tenant's **service identity**, resolved by `identity.sessions.service_client(...)` **only** inside `metrics/tier2.py`. No service principal configured for the tenant → `unavailable` / `service_identity_not_configured`. | **Per-period cells only. No totals over ranges.** Suppression per §4. Validated against the output allowlist per §4. |
| `service` | — | Denied by the frozen `SERVICE_READ_TOOLS`, unchanged (P5C-1 stays open for direct internal use). |

### 3.1 How Tier 2 reaches the tool: exact approved change to frozen 5A code

`src/bullhorn_mcp/identity/principal.py` line 40 becomes exactly:

```python
TIER2_ALLOWLIST = frozenset({"bullhorn_session", "setup_status", "get_recruiting_metrics"})  # A2-2; Phase 6 M1 (D-5-24)
```

`crosscutting/permissions.py` is **not** changed: its tier gate already reads `TIER2_ALLOWLIST`. Case 10 is re-frozen with this one line.

## 4. Tier 2 policy (`metrics/tier2_policy.py`; applied server-side before return)

**P-1. Minimum cohort `k`.**
- `k` comes from the new tenant setting `tier2_min_cohort`: an integer from 5 to 1000. If unset, the default is **10** (Q-T1).
- It is set only by a setup admin of the tenant, through `set_setting` with propose/commit. (Amended by M1-A2: in M1, only through admin-gated import.)
- A count cell with a value below `k` → `{"value": null, "suppressed": true, "reason": "insufficient_aggregate_population"}`. A zero is suppressed too.

**P-2. Ratio cells.**
- A ratio is returned only if **both** the numerator cell and the denominator cell are at least `k`. Otherwise it is suppressed with the same reason.
- If a count cell is suppressed, a request for a ratio involving it cannot reveal it.

**P-3. No margins.** Tier 2 never receives totals, sums, averages or any value computed across cells. So there is no complementary recovery, within one response or across responses.

**P-4. Output allowlist.**
- The Tier 2 response must validate **exactly** against the schema below. Any extra key is a server error (`policy_violation`), and nothing is returned.

  ```
  {status, tier:"workspace_only", period, reporting_timezone,
   definition:{metric_catalog_version, policy_version, k},
   metrics:{<id>:{status, missing_requirements?, cells:[{period_start, value|null, suppressed, reason?}]}}}
  ```

- Never present: record IDs, names, emails, events, `links`, `provenance` IDs, `activity_id`s, warnings carrying data, the `request_hash` or a cursor.

**P-5. Errors.** Tier 2 errors are generic (`error` / `rate_limited` / `unavailable`). No Bullhorn text is passed through.

**P-6. Audit.** As 5C B-2 / R2-B1: names, then types and lengths, then a keyed `request_hmac` that is never shown to the caller. The audit also records `tier` and `execution: service|caller`.

## 5. Files

**New:**
- `src/bullhorn_mcp/metrics/{__init__,catalog,compute,tier2,tier2_policy}.py`
- `src/bullhorn_mcp/tools/metrics.py`
- `src/bullhorn_mcp/mappings/metric_catalog.yaml`
- `tests/test_phase6_m1_*.py`

**Allowlisted existing changes (exact):**

| File | Change |
|---|---|
| `tools/__init__.py` (protected) | One added line: `from . import metrics  # noqa: F401` |
| `identity/principal.py` (frozen 5A) | Line 40 only, as in §3.1 |
| `activity/service.py` (5C) | Additive `max_pages: int = 5` keyword on the internal entry point. Tool behaviour is unchanged. |
| `tenant/profile_v2.py` (4A) | Add `tier2_min_cohort` to `_SETTINGS_KEYS`, validated as an exact `int` from 5 to 1000. Emitted only when set. |
| `tests/test_tools_notes.py` line 28 | `APPROVED_ADDITIVE_TOOLS = {"bullhorn_session", "find_records", "get_activity", "get_recruiting_metrics"}  # Phase 5/6 approved additive tools (D-5-15)` |
| `tests/test_phase5a_security_tools.py` | After line 47, add `PHASE6_TOOLS = {"get_recruiting_metrics"}  # Phase 6 M1`.<br>Line 96: `== ALL_20 \| PHASE5C_TOOLS \| PHASE6_TOOLS`.<br>Line 97: `== 20 + len(PHASE5C_TOOLS) + len(PHASE6_TOOLS)`. |
| `tests/test_phase5c_security_tools.py` | After the `ALL_22` set, add `PHASE6_TOOLS = {"get_recruiting_metrics"}  # Phase 6 M1`.<br>Line 121: `== ALL_22 \| PHASE6_TOOLS`.<br>Line 122: `== 22 + len(PHASE6_TOOLS)`.<br>Line 214: `== ALL_22 \| PHASE6_TOOLS`. |

**Must not change:**
- `crosscutting/*`, `server.py`, `auth/*`, `bullhorn/*`;
- `identity/*` beyond §3.1;
- `reads/*`, `activity/derivers.py`, `tenant/capabilities.py`;
- `schema/*`, the legacy `tools/*`, `tools/records.py`;
- `activity_concepts.yaml`, the catalogs;
- every other existing test.

(Amended by M1-A1 for `reads/records.py` and `tenant/state.py`.)

Any other failing existing test → **stop and escalate**.

## 6. Acceptance criteria

1. **AC-1.** `pytest`, `ruff check .` and `mypy` are green. The regression suite passes 25/25, with case 10 re-frozen for the §5 hunks. The Phase 5 security suite passes unchanged. The wheel includes `metric_catalog.yaml`.
2. **AC-2.** The registry has exactly 23 tools.
   - The `get_recruiting_metrics` schema pin is exactly `{metrics, date_from, date_to, period}`.
   - No identity-like or tier parameter appears (5A AC-5).
3. **AC-3.** Validation, each case returning `rejected_validation` with zero Bullhorn calls:
   - an unknown metric ID;
   - more than 8 metrics;
   - misaligned bounds;
   - a naive datetime, or anything other than date-only bounds;
   - a range of more than 12 months or 4 quarters;
   - an unknown `period`.
4. **AC-4.** For each count metric, Tier 1 cells equal the number of events that 5C `get_activity` returns for the same concept and period, on shared fixtures (a parity test). A missing or unsupported concept returns the same status and `missing_requirements` as `get_activity`.
5. **AC-5.** Incomplete enumeration (a page cap or time budget, simulated) gives `incomplete` with no number, in both tiers.
6. **AC-6.** Tier 1 runs under the caller's `BhRestToken`. respx asserts every request, and `service_client` is never called.
7. **AC-7.** Tier 2 runs under the service token only. In a concurrent A (Tier 1) / B (Tier 2) test, each request carries the right token. With no service principal configured → `unavailable`.
8. **AC-8.** Tier 2 suppression:
   - cells below `k` (including 0) are suppressed;
   - ratios need both sides at least `k`;
   - there are no totals or margins anywhere in the Tier 2 output (key scan);
   - the default `k` is 10;
   - setting `k=4` is rejected;
   - `k` set by a non-admin is denied.
9. **AC-9.** The Tier 2 output allowlist validator rejects an injected extra key (`policy_violation`). Every Tier 2 golden response validates.
10. **AC-10.** Sentinel scan of the Tier 2 outputs, errors, logs and audit. The fixtures contain names, emails, record IDs, client names and note text. None of them appears, nor any `activity_id`.

## 7. Security & Identity Review (required, blocking)

The reviewer attacks each item below:

| ID | Attack |
|---|---|
| SM-1 | Tier escalation via arguments or claims (AC-2). |
| SM-2 | Tier 2 obtains record-level data through any path: IDs, names, provenance, warnings or errors (AC-9, AC-10). |
| SM-3 | Recovery of a suppressed cell by complement or differencing: totals, ratios, overlapping ranges, period switching between month and quarter (AC-8). Specifically: quarter cells versus month cells. If one month in a quarter is below `k` and the quarter is not, the quarter total is a margin over months. **Rule: a Tier 2 quarter cell is suppressed if any of its month cells is below `k`.** Required test. |
| SM-4 | Service-identity misuse: is it reachable from any other tool or tier (grep plus test)? Does Tier 1 ever use it (AC-6)? |
| SM-5 | Bullhorn permission bypass: does Tier 1 always use the caller's session? |
| SM-6 | Cross-tenant use of `k`, the catalog or the service session (tenant from the A3-1 claim). |
| SM-7 | Resource exhaustion: 8 metrics × 12 periods × 40 pages within the 30 s budget, failing as `incomplete`. |
| SM-8 | Regression: the workspace_only denial of the other 20 non-allowlisted tools is unchanged (R-A2a rerun). |

**New regression cases:**
- SR-37: Tier 2 suppression, no margins, and the quarter rule.
- SR-38: the Tier 2 output allowlist and sentinel scan.
- SR-39: Tier 1 runs on the caller's session only.

**Process:** Builder → Independent Reviewer → Security & Identity Reviewer → gates → commit/push. D-5-25 applies: only blocking findings are fixed; everything else is logged as P6-* debt.

---

## Amendment M1-A (2026-10-07): Tier 2 setup gate, the `k` setting, and the `noqa`

### M1-A1: evaluate setup state for the execution identity. Option (a) is approved.

**Options rejected:**
- **Option (c)** is rejected: a separate Tier 2 setup check would be a second implementation of the setup gate.
- **Option (b) alone** is not enough: it does not reach the call path.

**Exact allowed changes.** Each is an additive, **keyword-only** parameter whose default reproduces current behaviour exactly:

| File | Change |
|---|---|
| `reads/records.py` | `check_setup(..., *, execution_tier: str \| None = None)` |
| `activity/service.py` | The internal entry point gains `*, execution_tier: str \| None = None`, passed through to `check_setup`. |
| `tenant/state.py` (frozen 5A; A3-2) | `compute_setup_state(..., *, execution_tier: str \| None = None)` is passed to `_shared_row1`. `None` means today's `current_tier()` behaviour. |

**Constraints.**
- **Only one accepted value.** Any value other than `None` or `"service"` raises `ValueError`.
- **What `"service"` means.** Row 1 is satisfied by the resolved service session for the selected tenant. Nothing else in the setup evaluation changes: the tenant store, the profile, the `rest_url` binding and the capability and concept requirements are all evaluated as normal.
- **Only one caller.** `execution_tier="service"` is passed **only** from `metrics/tier2.py`. This is checked by a grep test: no other module passes `execution_tier=`.
- **Tools are unaffected.** No tool schema exposes the parameter. `find_records`, `get_activity` and `setup_status` never pass it.
- **No record-level output for Tier 2.** The internal service results reach a Tier 2 caller **only** after reduction to cells in `metrics/compute.py` and the `tier2_policy` output-allowlist validation (P-4). Events, IDs and warnings are discarded before the policy is applied.

**Tests:**

| ID | Test |
|---|---|
| T-M1A-a | The three `xfail(strict)` shared-mode Tier 2 tests become plain passes. |
| T-M1A-b | `compute_setup_state` / `check_setup` outputs are unchanged for `execution_tier=None`. The Phase 4A, 5A and 5C state tests pass unmodified. |
| T-M1A-c | `execution_tier="bullhorn_user"` and any other value raise `ValueError`. |
| T-M1A-d | Grep: only `metrics/tier2.py` passes `execution_tier=`. |
| T-M1A-e | A `workspace_only` caller of `find_records` / `get_activity` is still denied (R-A2a rerun) and never reaches `execution_tier`. |

**Case 10.** Re-frozen for the `tenant/state.py` hunk, which is a frozen 5A file.

### M1-A2: the `k` setting gap. Accepted for M1.

**How `k` is set in M1.** `tier2_min_cohort` is set only through the admin-gated `import_document` (validated as an exact `int` from 5 to 1000), or left at the safe default of 10.

**AC-8 is read accordingly:**
- `k=4` is rejected at import validation;
- a non-admin cannot import.

**No `changes.py` hunk.**

**Debt logged:**

| ID | Item | Target |
|---|---|---|
| P6-1 | `set_setting` support for integer settings (`changes.py` `SETTING_CHOICES` / the `isinstance(value, str)` assertion) | Next Phase 6 milestone |
| P6-2 | `Settings.tier2_min_cohort` is typed `Any`. It is validated at runtime; mypy is blocked by the frozen `changes.py:344`. Tighten the type together with P6-1. | Next Phase 6 milestone |

### M1-A3: `# noqa: E501` on `tests/test_tools_notes.py` line 28. Ratified.

The approved line text exceeds the line length. The `noqa` suffix is part of the approved hunk, and case 10 freezes it.

---

## Amendment M1-B (2026-10-07): Tier 2 returns closed periods only (Sec NB-1, promoted to blocking)

**Ruling.** Option (a), fix now. This is a Tier 2 differencing defect, so it is in scope under D-5-25.

**The problem.** The premise in §1, "the same cell always has the same value", fails for open periods. By polling the current or a future month, a Tier 2 caller sees each increment once the cell reaches `k`. For example, a change from 10 to 11 reveals one new placement.

**Rule (implemented in `metrics/tier2_policy.py`; Tier 2 only).**
- **When a period is closed.** At request time, compute `boundary` = the start (00:00) of the **current month** in the tenant's `reporting_timezone`. A period is **closed** only if its exclusive end is at or before `boundary`.
- **Months.** A month is closed when the month has ended.
- **Quarters.** A quarter is closed only when its last month has ended.
- **Open and future periods.** Every cell of an open or future period, for counts and ratios alike, is returned as `{"value": null, "suppressed": true, "reason": "period_open"}`.
- **Order of rules.** The `period_open` rule is applied **before**, and independently of, the `k` rule and the quarter rule.
- **Bullhorn calls.** Enumeration may still run for the whole range, so that closed-period cells can be computed. It must not be relied on for open periods.
- **Allowlist.** The P-4 output allowlist is unchanged: `reason` already exists, and `period_open` is added to its allowed values.
- **Tier 1** is unchanged: the caller's own data, under their own Bullhorn permissions.

**Tests (blocking; part of SR-37):**

| ID | Test |
|---|---|
| T-M1B-a | With a frozen clock (mid-month) and fixtures giving the current month and the next month counts of at least `k`, Tier 2 returns `period_open` for both, while the prior months show values. |
| T-M1B-b | A quarter containing the current month gives `period_open`, even if its completed months are at least `k`. The prior quarter is closed and shows its value. |
| T-M1B-c | **Timezone boundary.** A clock time that is 1 hour before local midnight on the 1st in a non-UTC `reporting_timezone` treats the prior month as still open. At or after local midnight, it is closed. |
| T-M1B-d | **Increment test.** Two Tier 2 calls, with one placement added to the current month between them, give byte-identical responses. |
| T-M1B-e | Tier 1 still returns open-period values. |

**Residual risk (logged).** A closed period can still change through backdated records or deletes in Bullhorn. That is a much narrower channel. It is logged as **P6-3**, to be addressed with a snapshot or frozen-cell store in the analytics-store milestone (`ANALYTICS_PIPELINE_RECOMMENDATIONS.md`).

**Re-review.** A brief Security & Identity check of the M1-B diff, then the gates, then commit/push.
