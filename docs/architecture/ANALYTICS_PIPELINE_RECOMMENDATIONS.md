# Analytics Pipeline Recommendations (Phase 6+)

- **Author / audience:** Analytics / Data Pipeline Agent → Architect, 2026-10-07. Advice only; the Architect decides scope.
- **Labels:** **[V]** verified in repo docs (`PHASE5C_HV_VERIFICATION.md`, catalog YAML) · **[A]** assumption, needs HV
  before use · **[R]** recommendation. No tenant data was used. No prototype was built: the decisions below
  depend on HV items, not on tooling behaviour.

## 0. Bottom line

1. **[R] Phase 6 M1 stays live-only.** Compute `get_recruiting_metrics` from the 5C services (`find_records` /
   `get_activity` internals) per request. Don't build a store until a measured need appears (Q-10 in the read model).
2. **[R] When a store is added, it holds only *derived canonical events* plus a minimal set of deriver input fields,
   produced by the MCP's own reader and derivers.** It is a cache of what the MCP already computes, not a second
   source of business rules.
3. **[R] Backfill by small, closed `dateAdded` windows, so each window fits in one page.** This avoids the
   unordered offset-paging risk instead of trying to mitigate it.
4. **[R] Don't use dlt in the first store slice.** Write a small in-repo loader (idempotent upserts on
   `activity_id`). Look at dlt again if more than one destination or source appears.
5. **[R] Tier 2 reads the store only through an aggregate view layer.** No role used by Tier 2 can select from a
   record-level table.

## 1. Historical extraction feasibility

**Verified facts**
- [V] Every entity uses `GET /query/{entity}`. `/search` is not used (HV-Q1).
- [V] Paging is offset-only (`start`/`count`). A short page means the set is exhausted. No `total` is returned.
  The catalog caps pages at 100. **No `orderBy` is sent: order is unresolved, and so is tie-break stability** (HV-Q4, P4B-5).
- [V] Dates are epoch-ms `Timestamp`s. `dateAdded` is filterable with `gte`/`lt` on all 7 dated entities
  (CorporateUser uses `userDateAdded`) (HV-Q5, `bullhorn_query_support.yaml`).
- [V] `dateLastModified` is catalogued and filterable **only on Candidate**.
- [V] Soft delete: the query carries `(isDeleted = false OR isDeleted IS NULL)` and results are post-filtered.
  `include_deleted` is unsupported. Placement has no `isDeleted`. ClientCorporation is immutable (HV-Q6).
  **Whether `/query` returns soft-deleted rows at all is undocumented.**
- [V] Rate handling: 429/503 get at most 2 retries (1 s base, 8 s cap). There are at most 2 in-flight requests
  per (tenant, principal), and each call has a 30 s budget (HV-Q7). Limits: `where` ≤ 2,000 chars, ≤ 30 fields (HV-Q12).
- [V] Status-change time is undocumented (HV-Q10). `job_status_changed`, `candidate_status_changed` and
  `status_history` dating are unsupported, and `interview_rescheduled` is unsupported (HV-Q9).

**Consistency risk of large offset backfills.** With no ordering, rows inserted or deleted between page requests
can be skipped or duplicated. Deduplicating by `id` fixes duplicates but not skips.
**[R] Window partitioning:** split the backfill range into half-open `[a, b)` `dateAdded` windows, sized so that
each window returns fewer than 100 rows **in a single request** (`start=0`). By HV-Q4 a short page is complete,
so no offset is needed. If a window returns a full page, halve it and retry (adaptive bisection, floor 1 s). This
needs [A] HV-P1 (one `/query` response is internally consistent) and [A] HV-P2 (`dateAdded` is immutable, so a
closed window changes only through deletions or backdated inserts).

Cost estimate (unmeasured): 100k records at ~60 rows/window ≈ 1.7k requests; at 2 in flight and 0.5–1 s each,
≈ 15–30 min per entity. Fine for nightly or one-off backfill; too slow for live multi-year Tier 2 trends.
[A] HV-P3: the tenant API quota and daily cap are undocumented in the repo.

**Incremental extraction**
- **New records:** re-read the windows from the last high-water mark minus a **lag** (e.g. 24 h, [A] HV-P4: the
  commit-visibility delay is unknown). This is verified-feasible on all entities via `dateAdded`.
- **Edits to existing records** (status changes that drive `client_submission` and offers, interview outcome or
  cancellation): these need a modification cursor. That is verified for Candidate only.
  [A] HV-P5: whether `dateLastModified` exists, is filterable and is bumped on status/custom-field edits for
  JobOrder, JobSubmission, Placement and Appointment. Until that is verified, the only safe way to pick up
  edits is a **rolling re-read** of a trailing period (e.g. the last 90 days of `dateAdded` nightly, plus a
  full re-sweep weekly or monthly). Edits to records older than the re-read horizon arrive late, at the next
  full sweep.
- **Deletes:** soft-deleted rows can't be listed (`include_deleted` unsupported, `/query` behaviour unknown).
  [R] Detect deletions by **id-set difference** per re-read window: an id stored earlier but absent now gets a
  tombstone (`deleted_observed_at`). This is not an assertion that the record was deleted, only that it is no
  longer visible. Visibility loss caused by permissions (HV-Q11) looks identical, so run extraction under one
  stable service identity.

## 2. Ingestion tooling: dlt or equivalent

**Facts (dlt docs and PyPI, fetched 2026-10-07):** `dlt` 1.31.0, Apache-2.0, Python ≥3.10 <3.15. It offers
cursor-based incremental loading with lag windows, `append`/`replace`/`merge` (primary-key dedup) write
dispositions, pipeline state, and refresh modes (`drop_data`, `drop_resources`, `drop_sources`). DuckDB and
Postgres are supported destinations. Its core install has about 25 runtime dependencies (gitpython, sqlglot,
pendulum, fsspec, tenacity, croniter, simplejson, setuptools, pywin32 on Windows, among others).
I found no first-party Bullhorn source and did not verify one.

**Fit assessment**
- dlt's strengths are generic REST extraction and schema inference. Here neither should be used: the MCP's
  `EntityReader` already owns auth, the HV-gated query grammar, retries, concurrency caps and soft-delete
  filtering. dlt's REST source would bypass all of that, which conflicts with the charter.
- What dlt could still offer is a **loader**: `pipeline.run(iter_of_events, write_disposition="merge",
  primary_key="activity_id")`. That is a thin gain over about 150 lines of upsert code, against a heavy
  dependency tree and its own state store (the `_dlt_*` tables), which becomes a second source of
  incremental state.
- [R] **Decision:** an in-repo loader (DB-API upserts, own `extraction_run` table). Revisit dlt as an optional
  `[analytics]` extra only for multi-destination or warehouse needs. Airbyte/Meltano are heavier and would also
  bypass the reader.

## 3. Storage: DuckDB (dev) vs Postgres (prod) and schema sketch

- [R] **DuckDB** for dev/tests: file per tenant, zero ops, fast `GROUP BY`. Single-writer, no roles, so **never
  the Tier 2 backend in a multi-user deployment.**
- [R] **Postgres** for prod: roles, views, grants and RLS give Tier 2 separation in the database itself. Use
  portable SQL (only `JSON`/`JSONB` behind one adapter) so one set of migrations runs on both.

Schema sketch (every table carries `tenant_key`; all primary keys start with `tenant_key`):

```
extraction_run(tenant_key, run_id, kind[backfill|incremental|resweep|rederive], entity, window_from_ms,
               window_to_ms, started_at, finished_at, status, rows_seen, service_identity_ref,
               catalog_fingerprint, code_version)
source_row  -- RESTRICTED: deriver inputs only, no names/contact/free text/notes bodies
  (tenant_key, canonical_entity, source_id, payload_json  -- only fields named in the deriver Plan
   field_set_fingerprint, first_seen_run, last_seen_run, extracted_at, deleted_observed_at NULL)
activity_event  -- RESTRICTED: vocabulary §1 events exactly as derivers emit them
  (tenant_key, activity_id, concept, occurred_at_utc, state, source_entity, source_id,
   links_json, unresolved_links_json, attribution_json, definition_json, evidence_json,
   profile_version, rule_id, mapping_fingerprint, catalog_fingerprint, derived_at, run_id,
   superseded_at NULL)                PK (tenant_key, activity_id, profile_version)
agg_<metric>  -- Tier 2-reachable: pre-bucketed counts, no ids
  (tenant_key, metric_id, metric_def_version, bucket_start, bucket_width, dim_level, dim_value,
   cohort_n, value, policy_version, built_from_profile_version, built_at)
```

Provenance per event: source ids (`source_id`, `links`), `extracted_at`/`run_id`, `profile_version`, `rule_id`,
`mapping_fingerprint` (hash of the `definition.mappings` records plus `settings`; no helper exists yet, see S1) and
`catalog_fingerprint` (`current_catalog_fingerprint()`). **Payload minimization:** `source_row` holds only the canonical fields that `derivers.plan()` requests (ids,
dates, status and mapped type/status fields). No candidate names, emails, addresses below the approved geography
level, or note text are stored.

## 4. Refresh, backfill, idempotency, re-derivation

- **Idempotency [R]:** `activity_id` is deterministic (`events.make_activity_id`). Upsert on
  `(tenant_key, activity_id, profile_version)`. `source_row` upserts on `(tenant_key, entity, source_id)`. A
  rerun of any window converges; a crashed run is just rerun (`extraction_run.status`).
- **Late-arriving edits:** handled by the rolling re-read in §1. When `source_row.payload` changes, the derivers
  re-run for that row, and the old event version is marked `superseded_at` (kept for audit and for the
  differencing ledger, never served).
- **Soft deletes:** the tombstone from the id-set difference (§1) marks the dependent events `superseded_at`.
  Aggregates rebuild from the live events only.
- **Tenant mapping or profile change:** on a new `profile_version` or `mapping_fingerprint`, run a
  `rederive` job that replays the derivers over the stored `source_row` (**no Bullhorn calls**), provided that
  `field_set_fingerprint` covers the new plan's fields. If the new mapping needs a field that was never
  extracted, a targeted re-extraction is needed. Serve the old profile's aggregates, labelled with their
  version, until the rederive completes; never serve a mix of versions.
- **Required code seam [R]:** the derivers must be callable on stored rows without a live `ReadContext`
  client. Today `derivers.event(ctx, concept, src, row)` needs `ctx` only for profile, timezone and `now`. A
  small "offline context" seam is the one refactor a store needs, and it keeps the rules single-sourced.
- **State concepts** (`interview_upcoming`, `offer_pending`) are read-time states. **Never store them as
  events.** Compute them at query time from stored events, or live.

## 5. Live vs store vs hybrid, by question type

| Question type | Recommendation | Why |
|---|---|---|
| Record-level lookup / drill-back (Tier 1) | **Live, always** | Must respect the user's own Bullhorn permissions (HV-Q11). A store read under the service identity would widen access. |
| Short-range operational counts (≤ ~30 days, one recruiter/job) Tier 1 | **Live** | Small windows; fresh; M1 path |
| `interview_upcoming`, `offer_pending`, "current pipeline" states | **Live** | Read-time states by definition |
| Multi-month/year trends, funnel conversion, time-to-fill (Tier 1 or 2) | **Store** (after slice S3) | Backfill cost (§1) makes live computation too slow and unbounded per request |
| Tier 2 approved aggregates | **Store aggregates (`agg_*`)** once available; live via service identity until then | Fixed catalog, enables pre-suppression and differencing ledger |
| Recent-period trend + today | **Hybrid**: store for closed buckets, live for the open bucket | Watermark = last completed run; label `as_of` |
| Status-transition timing (`client_submission` via history, job/candidate status changes) | **Unsupported** in both | HV-Q10; a store could *observe* transitions between runs (option O-1, §8), but that is a vocabulary decision, not a pipeline one |

## 6. Tier 2 safety in a store

- **[R] Aggregate-only access path.** Tier 2 queries run under a DB role with `SELECT` only on `agg_*` views.
  `source_row`, `activity_event` and `extraction_run` are not granted to that role. Enforce this in Postgres
  grants and test it with a "Tier 2 role cannot select restricted tables" test. In DuckDB dev, this boundary is
  code-only, so DuckDB must never back a multi-user Tier 2 deployment.
- **[R] Pre-built allowlisted cells.** `agg_*` holds only catalog metric × approved dimension level × minimum
  bucket width. There are no id or name dimensions; geography is only state/metro/region; client data only as
  segment (TT-8.1, 8.4, 8.5).
- **[R] Thresholds stay in the server**, applied at query time, not in the table: `k`, `k_client`,
  complementary suppression and the dominance rule are applied to the requested cell set, so per-request
  complements are covered. `cohort_n` is stored, but **never returned**.
- **[R] Differencing ledger** (TT-8.3): `tier2_answered(tenant_key, principal_hash, cohort_def_hash,
  cohort_n, answered_at)` in a separate schema with retention Q-T5. The overlap check compares against it.
  Rebuilds caused by late edits change past cells. Treat a changed cell value as a new answer, so that
  "before/after edit" differencing is caught. Round the values (TT-8.3) and refuse a cell whose change since
  the last answer is smaller than `k`.
- **[R] Service identity scope.** The extraction job runs under the read-only service identity. It is a separate
  process with write access to the restricted tables only. The Tier 2 request path never holds Bullhorn
  credentials or restricted-table grants. No `activity_id` or source id appears in `agg_*` (TT-8.6).

## 7. Recommended Phase 6 slices after M1 (each shippable alone)

| Slice | Delivers | Ships value without later slices |
|---|---|---|
| **S1 Offline deriver seam + mapping fingerprint** | `derivers.event` usable with an offline context; `mapping_fingerprint` helper; equivalence test live-vs-offline on fixtures | Removes risk for all later store work; no new infra |
| **S2 Windowed extractor (dev, DuckDB)** | Adaptive `dateAdded` bisection reader over `EntityReader` (single-request windows), `extraction_run`, `source_row`, `activity_event` upserts; CLI only, admin-only, no tool exposure | Measurable backfill cost per entity → answers HV-P3 empirically on a test tenant |
| **S3 Rolling re-read + tombstones + rederive** | Trailing-window refresh, id-set-diff tombstones, `rederive` on profile change, `superseded_at` | Store stays correct under edits/mapping changes |
| **S4 Tier 2 aggregate layer (Postgres)** | `agg_*` build, Tier 2 DB role, ledger table, `get_recruiting_metrics` Tier 2 path switched to store for closed buckets | Faster, bounded Tier 2; Security reviewer attack list §8.6 re-run |
| **S5 Hybrid Tier 1 trends** | Closed buckets from store + open bucket live, `as_of` labelling | Long-range Tier 1 trends |

## 8. Open questions and HV items

| ID | Item | Type |
|---|---|---|
| HV-P1 | Is one `/query` response a consistent snapshot? | Bullhorn HV |
| HV-P2 | Is `dateAdded` immutable for all 7 dated entities? | Bullhorn HV |
| HV-P3 | Tenant API quota, daily caps, and safe sustained request rate for a background job | Bullhorn HV / tenant measurement |
| HV-P4 | Commit-visibility lag between write and `/query` visibility (sets the incremental lag) | Bullhorn HV |
| HV-P5 | `dateLastModified` presence, filterability and bump semantics on JobOrder, JobSubmission, Placement, Appointment (status and custom-field edits) | Bullhorn HV; it decides cost of edit capture |
| HV-P6 | Whether `/query` returns `isDeleted=true` rows when not filtered (makes deletion capture direct) | Bullhorn HV (extends HV-Q6) |
| HV-P7 | Can Placements be hard-deleted? Otherwise the tombstone semantics stay "not visible" | Bullhorn HV |
| HV-P8 | `totalOnly=true` count on `/query`: usable as a cheap per-window completeness check? | Bullhorn HV |
| Q-P1 | Is a service identity that reads everything acceptable for building Tier 1-visible aggregates? Tier 1 store aggregates could exceed a user's own Bullhorn permissions. | Architect / Security |
| Q-P2 | Retention for `source_row` and superseded events (minimization vs audit) | Architect / user |
| Q-P3 | Hosting: per-tenant database or a shared database with row-level security | Architect |
| O-1 | Option: derive "observed status transitions" from run-to-run snapshot differences, giving a time *interval*, not an instant. This would need a vocabulary bump and decision (D4); it must not be introduced by the pipeline. | Vocabulary decision |
| Q-P4 | Trigger for building the store at all: a latency or quota threshold measured after M1 (read-model Q-10) | Architect |
