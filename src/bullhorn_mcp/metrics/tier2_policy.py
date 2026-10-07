"""Tier 2 (``workspace_only``) aggregate policy, applied server-side before return (Phase 6 M1 §4).

- P-1: minimum cohort ``k`` (tenant setting ``tier2_min_cohort``, 5..1000; default 10). A count
  cell below ``k`` (zero included) is suppressed. A quarter cell is suppressed if any of its
  month cells is below ``k`` (SM-3: a quarter is a margin over months).
- P-2: a ratio cell is returned only if both its numerator and denominator cells are unsuppressed.
- P-3: no totals, sums or averages across cells.
- P-4: the response must validate exactly against the output allowlist; anything else is
  ``policy_violation`` and nothing is returned.
- M1-B: only closed periods are returned. A period is closed when its exclusive end is at or before
  the start of the current month in the reporting timezone; open and future cells are
  ``period_open``, applied before (and independently of) the ``k`` and quarter rules.
- P-5: errors are generic (``error`` / ``rate_limited`` / ``unavailable``) with a fixed code.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..schema.query_builder import Problem
from ..tenant.profile_v2 import TIER2_MIN_COHORT_RANGE
from ..tenant.timeutil import resolve_zone
from .catalog import MetricCatalog
from .compute import PERIODS, POLICY_VERSION, UNAVAILABLE_STATUSES, CountResult, Request, _add_months, metric_status

DEFAULT_K = 10
MIN_K, MAX_K = TIER2_MIN_COHORT_RANGE
TIER = "workspace_only"
SUPPRESSED_REASON = "insufficient_aggregate_population"
OPEN_REASON = "period_open"
REASONS = (SUPPRESSED_REASON, OPEN_REASON)
METRIC_STATUSES = ("ok", "incomplete", *UNAVAILABLE_STATUSES)
ERROR_STATUSES = ("error", "rate_limited", "unavailable")
ERROR_CODES = (
    "service_identity_not_configured",
    "setup_required",
    "bullhorn_error",
    "rate_limited",
    "internal_error",
    "policy_violation",
)
VALIDATION_ITEMS = ("metrics", "date_from", "date_to", "period")
TOP_KEYS = frozenset({"status", "tier", "period", "reporting_timezone", "definition", "metrics"})
DEFINITION_KEYS = frozenset({"metric_catalog_version", "policy_version", "k"})
_PERIOD_START_RE = re.compile(r"\d{4}-\d{2}-01", re.ASCII)
_REQUIREMENT_RE = re.compile(r"[A-Za-z0-9_.:|\-]{1,200}", re.ASCII)
_TZ_RE = re.compile(r"[A-Za-z0-9_+\-/]{1,64}", re.ASCII)


class PolicyViolation(Exception):
    """The Tier 2 response failed the output allowlist (P-4). Carries no data."""


def cohort(profile: Any) -> int:
    """``k`` for the caller's tenant profile (P-1); the default when unset."""
    settings = getattr(profile, "settings", None)
    value = getattr(settings, "tier2_min_cohort", None)
    if type(value) is int and MIN_K <= value <= MAX_K:
        return value
    return DEFAULT_K


def generic(status: str, code: str) -> dict[str, Any]:
    """A generic Tier 2 error (P-5): a fixed status and code, nothing else."""
    assert status in ERROR_STATUSES and code in ERROR_CODES
    return {"status": status, "tier": TIER, "error": code}


def rejected(problems: Sequence[Problem]) -> dict[str, Any]:
    """Tier 2 validation errors: parameter names and fixed messages only."""
    errors = [{"code": p.code, "item": p.item if p.item in VALIDATION_ITEMS else "arguments", "message": p.reason} for p in problems]
    return {"status": "rejected_validation", "tier": TIER, "errors": errors}


def _suppressed(start: str, reason: str = SUPPRESSED_REASON) -> dict[str, Any]:
    return {"period_start": start, "value": None, "suppressed": True, "reason": reason}


def closed_boundary(now: _dt.datetime, timezone: str) -> _dt.date:
    """M1-B: the start of the current month in the reporting timezone (periods ending by then are closed)."""
    local = now.astimezone(resolve_zone(timezone))
    return _dt.date(local.year, local.month, 1)


def build(
    req: Request, cat: MetricCatalog, counts: Mapping[str, CountResult], k: int, timezone: str, *, now: _dt.datetime
) -> dict[str, Any]:
    """The Tier 2 response: closed periods only (M1-B), suppressed per P-1/P-2, no margins (P-3)."""
    boundary = closed_boundary(now, timezone)
    metrics: dict[str, Any] = {}
    for mid in req.metrics:
        state = metric_status(mid, cat, counts)
        if state.status != "ok":
            entry: dict[str, Any] = {"status": state.status, "cells": []}
            if state.status in UNAVAILABLE_STATUSES:
                entry["missing_requirements"] = list(state.missing_requirements)
            metrics[mid] = entry
            continue
        m = cat.metrics[mid]
        cells: list[dict[str, Any]] = []
        for start, idx in req.periods:
            day = start.isoformat()
            if _add_months(req.months[idx[-1]], 1) > boundary:  # M1-B: open or future period, before k
                cells.append(_suppressed(day, OPEN_REASON))
                continue
            if m.kind == "count":
                months = counts[mid].months
                if any(months[i] < k for i in idx):  # P-1 and the SM-3 quarter rule
                    cells.append(_suppressed(day))
                else:
                    cells.append({"period_start": day, "value": sum(months[i] for i in idx), "suppressed": False})
                continue
            assert m.numerator is not None and m.denominator is not None
            num, den = counts[m.numerator].months, counts[m.denominator].months
            if any(num[i] < k for i in idx) or any(den[i] < k for i in idx):  # P-2: both sides must be returnable
                cells.append(_suppressed(day))
            else:
                cells.append({"period_start": day, "value": sum(num[i] for i in idx) / sum(den[i] for i in idx), "suppressed": False})
        metrics[mid] = {"status": "ok", "cells": cells}
    return {
        "status": "ok",
        "tier": TIER,
        "period": req.period,
        "reporting_timezone": timezone,
        "definition": {"metric_catalog_version": cat.version, "policy_version": POLICY_VERSION, "k": k},
        "metrics": metrics,
    }


def _check(condition: bool) -> None:
    if not condition:
        raise PolicyViolation("policy_violation")


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def validate_output(doc: Any, cat: MetricCatalog) -> None:
    """P-4: the exact output allowlist. Raises ``PolicyViolation`` on any deviation."""
    _check(isinstance(doc, dict) and set(doc) == TOP_KEYS)
    _check(doc["status"] == "ok" and doc["tier"] == TIER and doc["period"] in PERIODS)
    _check(type(doc["reporting_timezone"]) is str and _TZ_RE.fullmatch(doc["reporting_timezone"]) is not None)
    definition = doc["definition"]
    _check(isinstance(definition, dict) and set(definition) == DEFINITION_KEYS)
    _check(all(type(definition[name]) is int for name in DEFINITION_KEYS))
    _check(MIN_K <= definition["k"] <= MAX_K)
    metrics = doc["metrics"]
    _check(isinstance(metrics, dict) and 1 <= len(metrics) and set(metrics) <= set(cat.metrics))
    for mid, entry in metrics.items():
        _check(isinstance(entry, dict) and {"status", "cells"} <= set(entry) <= {"status", "missing_requirements", "cells"})
        _check(entry["status"] in METRIC_STATUSES)
        if "missing_requirements" in entry:
            reqs = entry["missing_requirements"]
            _check(entry["status"] in UNAVAILABLE_STATUSES and isinstance(reqs, list))
            _check(all(type(r) is str and _REQUIREMENT_RE.fullmatch(r) is not None for r in reqs))
        cells = entry["cells"]
        _check(isinstance(cells, list) and (entry["status"] == "ok" or not cells))
        for cell in cells:
            _check(isinstance(cell, dict) and type(cell.get("period_start")) is str)
            _check(_PERIOD_START_RE.fullmatch(cell["period_start"]) is not None)
            if cell.get("suppressed") is True:
                _check(set(cell) == {"period_start", "value", "suppressed", "reason"})
                _check(cell["value"] is None and cell["reason"] in REASONS)
            else:
                _check(set(cell) == {"period_start", "value", "suppressed"} and cell["suppressed"] is False)
                _check(_is_number(cell["value"]))
                if cat.metrics[mid].kind == "count":  # a returned count is never below k (P-1)
                    _check(type(cell["value"]) is int and cell["value"] >= definition["k"])
