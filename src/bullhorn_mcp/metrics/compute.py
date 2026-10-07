"""Period counts and stage ratios over the 5C activity service (Phase 6 M1 §1-§3).

Counts are enumerated only through ``activity/service.get_activity`` (called directly with
an explicit context and client), one calendar month of the tenant ``reporting_timezone`` at
a time; a quarter is its three months. Concept availability and meaning come only from the
5C single sources (``tenant/capabilities`` via the service, ``activity/derivers``).

A count is ``ok`` only when every month was enumerated completely; otherwise it is
``incomplete`` and carries no number (never a partial count). Each month has a page budget
of ``METRICS_MAX_PAGES`` Bullhorn requests; the reader's 30 s per-call budget (D-5C-12)
covers the whole tool call and its exhaustion also yields ``incomplete``.

This module never resolves a client and never touches the service identity: the Tier 1
path uses the caller's client (``run_tier1``); Tier 2 lives only in ``metrics/tier2.py``.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..activity import service as activity_service
from ..bullhorn.reads import ReadRateLimited
from ..reads.records import Outcome, ReadContext, check_setup, load_tenant, normalized_hash, rejected
from ..schema.query_builder import Problem
from . import catalog as catalog_mod
from .catalog import MetricCatalog

MAX_METRICS = 8
MAX_MONTHS = 12
PERIODS = ("month", "quarter")
METRICS_MAX_PAGES = 40
POLICY_VERSION = 1
UNAVAILABLE_STATUSES = ("definition_missing", "unsupported", "setup_revalidation_required")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


@dataclass(frozen=True)
class Request:
    metrics: tuple[str, ...]
    period: str
    months: tuple[_dt.date, ...]  # the month starts covered by [date_from, date_to)
    periods: tuple[tuple[_dt.date, tuple[int, ...]], ...]  # (period start, indexes into months)


@dataclass(frozen=True)
class CountResult:
    status: str
    missing_requirements: tuple[str, ...] = ()
    months: tuple[int, ...] = field(default=())  # one count per requested month, only when ``ok``


class BudgetExhausted(Exception):
    """The per-call time budget ran out: every count not yet complete is ``incomplete``."""


# ---------------------------------------------------------------------- #
# Validation (no Bullhorn call; never echoes a caller value)
# ---------------------------------------------------------------------- #


def _add_months(day: _dt.date, n: int) -> _dt.date:
    total = day.year * 12 + (day.month - 1) + n
    return _dt.date(total // 12, total % 12 + 1, 1)


def _parse_date(value: object) -> _dt.date | None:
    if type(value) is not str or _DATE_RE.fullmatch(value) is None:
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def validate(args: Mapping[str, Any], cat: MetricCatalog) -> Request | list[Problem]:
    """The request, or validation problems. ``item`` is always a parameter name."""
    errors: list[Problem] = []
    metrics = args.get("metrics")
    if type(metrics) is not list or not 1 <= len(metrics) <= MAX_METRICS:
        errors.append(Problem("invalid_metric", "metrics", f"metrics must be a list of 1 to {MAX_METRICS} metric IDs"))
    elif any(type(m) is not str or m not in cat.metrics for m in metrics):
        errors.append(Problem("invalid_metric", "metrics", f"every metric must be one of {sorted(cat.metrics)}"))
    elif len(set(metrics)) != len(metrics):
        errors.append(Problem("invalid_metric", "metrics", "duplicate metric"))
    period = args.get("period")
    if type(period) is not str or period not in PERIODS:
        errors.append(Problem("invalid_value", "period", f"period must be one of {list(PERIODS)}"))
        period = None
    bounds: dict[str, _dt.date | None] = {}
    for name in ("date_from", "date_to"):
        day = _parse_date(args.get(name))
        bounds[name] = day
        if day is None:
            errors.append(Problem("invalid_value", name, f"{name} must be a date (YYYY-MM-DD) in the reporting timezone"))
        elif period is not None and (day.day != 1 or (period == "quarter" and day.month % 3 != 1)):
            errors.append(Problem("invalid_value", name, f"{name} must fall on a {period} boundary"))
    lo, hi = bounds["date_from"], bounds["date_to"]
    if not errors and lo is not None and hi is not None:
        span = (hi.year - lo.year) * 12 + hi.month - lo.month
        if span <= 0:
            errors.append(Problem("invalid_value", "date_from", "date_from must be earlier than date_to (the range is [from, to))"))
        elif span > MAX_MONTHS:
            errors.append(Problem("range_too_wide", "date_to", "the range may cover at most 12 months or 4 quarters"))
    if errors:
        return errors
    assert lo is not None and hi is not None and type(period) is str and type(metrics) is list
    span = (hi.year - lo.year) * 12 + hi.month - lo.month
    months = tuple(_add_months(lo, i) for i in range(span))
    step = 1 if period == "month" else 3
    periods = tuple((months[i], tuple(range(i, i + step))) for i in range(0, span, step))
    return Request(metrics=tuple(metrics), period=period, months=months, periods=periods)


# ---------------------------------------------------------------------- #
# Enumeration through the 5C activity service
# ---------------------------------------------------------------------- #


ActivityRead = Callable[..., dict[str, Any]]


def _count_month(ctx: ReadContext, concept: str, start: _dt.date, end: _dt.date, read: ActivityRead) -> CountResult:
    """One concept over ``[start, end)``: an exact count, an availability status or ``incomplete``."""
    args: dict[str, Any] = {
        "concepts": [concept],
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "limit": activity_service.MAX_LIMIT,
    }
    reader = ctx.get_reader()
    first = reader.requests
    total = 0
    while True:
        remaining = METRICS_MAX_PAGES - (reader.requests - first)
        if remaining <= 0:
            return CountResult("incomplete")
        try:
            out = read(ctx, args, max_pages=remaining)
        except ReadRateLimited as exc:
            if "time budget" in str(exc):
                raise BudgetExhausted() from None
            raise
        if out.get("status") != "ok":
            raise Outcome(out)
        block = out["concepts"][concept]
        if block["status"] != "ok":
            return CountResult(block["status"], tuple(block["missing_requirements"]))
        total += len(block["events"])
        if out["next_cursor"] is None:
            return CountResult("ok", months=(total,)) if block["complete"] else CountResult("incomplete")
        args = {**args, "cursor": out["next_cursor"]}


def compute(ctx: ReadContext, req: Request, cat: MetricCatalog, read: ActivityRead | None = None) -> dict[str, CountResult]:
    """Every count metric the request needs, keyed by count metric ID.

    ``read`` is the 5C ``get_activity`` entry point (Tier 2 binds its service ``execution_tier`` in ``metrics/tier2.py``).
    """
    read = read or activity_service.get_activity
    needed: list[str] = []
    for mid in req.metrics:
        needed.extend(c for c in cat.counts_for(mid) if c not in needed)
    results: dict[str, CountResult] = {}
    exhausted = False
    for cid in needed:
        if exhausted:
            results[cid] = CountResult("incomplete")
            continue
        concept = cat.concept_of(cid)
        months: list[int] = []
        result: CountResult | None = None
        for i, start in enumerate(req.months):
            try:
                one = _count_month(ctx, concept, start, _add_months(start, 1), read)
            except BudgetExhausted:
                exhausted = True
                result = CountResult("incomplete")
                break
            if one.status != "ok":
                result = one
                break
            months.append(one.months[0])
        results[cid] = result if result is not None else CountResult("ok", months=tuple(months))
    return results


def metric_status(metric_id: str, cat: MetricCatalog, counts: Mapping[str, CountResult]) -> CountResult:
    """A metric's status: a count's own, or for a ratio the first unavailable side (then ``incomplete``)."""
    sides = [counts[c] for c in cat.counts_for(metric_id)]
    for status in UNAVAILABLE_STATUSES:
        if any(s.status == status for s in sides):
            missing = [m for s in sides if s.status in UNAVAILABLE_STATUSES for m in s.missing_requirements]
            return CountResult(status, tuple(dict.fromkeys(missing)))
    if any(s.status != "ok" for s in sides):
        return CountResult("incomplete")
    return CountResult("ok")


# ---------------------------------------------------------------------- #
# Tier 1 (bullhorn_user / local): the caller's own session, exact values, provenance
# ---------------------------------------------------------------------- #


def _ratio(num: int, den: int) -> float | None:
    return num / den if den > 0 else None


def tier1_output(ctx: ReadContext, req: Request, cat: MetricCatalog, counts: Mapping[str, CountResult], req_hash: str) -> dict[str, Any]:
    out_metrics: dict[str, Any] = {}
    for mid in req.metrics:
        state = metric_status(mid, cat, counts)
        if state.status != "ok":
            entry: dict[str, Any] = {"status": state.status, "cells": []}
            if state.status in UNAVAILABLE_STATUSES:
                entry["missing_requirements"] = list(state.missing_requirements)
            out_metrics[mid] = entry
            continue
        m = cat.metrics[mid]
        cells: list[dict[str, Any]] = []
        if m.kind == "count":
            months = counts[mid].months
            for start, idx in req.periods:
                cells.append({"period_start": start.isoformat(), "value": sum(months[i] for i in idx)})
            out_metrics[mid] = {"status": "ok", "kind": "count", "cells": cells, "total": sum(months)}
        else:
            assert m.numerator is not None and m.denominator is not None
            num, den = counts[m.numerator].months, counts[m.denominator].months
            for start, idx in req.periods:
                cells.append({"period_start": start.isoformat(), "value": _ratio(sum(num[i] for i in idx), sum(den[i] for i in idx))})
            out_metrics[mid] = {"status": "ok", "kind": "ratio", "cells": cells, "total": _ratio(sum(num), sum(den))}
    provenance = ctx.provenance(req_hash)
    provenance.update({"metric_catalog_version": cat.version, "policy_version": POLICY_VERSION})
    return {
        "status": "ok",
        "tier": ctx.ident.access_tier,
        "period": req.period,
        "reporting_timezone": ctx.timezone,
        "definition": {"metric_catalog_version": cat.version, "policy_version": POLICY_VERSION},
        "metrics": out_metrics,
        "provenance": provenance,
        "warnings": list(dict.fromkeys(ctx.warnings)),
    }


def run_tier1(ctx: ReadContext, args: Mapping[str, Any], get_client: Callable[[], Any]) -> dict[str, Any]:
    """Validate first (no Bullhorn call), then run under the caller's own client."""
    try:
        load_tenant(ctx)
        cat = catalog_mod.load()
        req = validate(args, cat)
        if isinstance(req, list):
            return rejected(req)
        ctx.client = get_client()
        check_setup(ctx)
        counts = compute(ctx, req, cat)
        return tier1_output(ctx, req, cat, counts, normalized_hash(ctx, args))
    except Outcome as out:
        return out.result
