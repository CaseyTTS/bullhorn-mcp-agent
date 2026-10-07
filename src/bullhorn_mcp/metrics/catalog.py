"""The packaged metric catalog (``mappings/metric_catalog.yaml``, version 1; Phase 6 M1 §2).

Each count metric names one 5C activity concept; each ratio names two count metrics.
Concept meanings come only from ``tenant/capabilities`` and ``activity/derivers``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from ..schema.canonical_catalog import read_packaged_text
from ..tenant import yaml_strict
from ..tenant.capabilities import ACTIVITY_CONCEPTS

RESOURCE = "metric_catalog.yaml"
SUPPORTED_VERSION = 1
KINDS = ("count", "ratio")


class CatalogError(ValueError):
    """The packaged catalog is malformed (a packaging defect, never caller input)."""


@dataclass(frozen=True)
class Metric:
    id: str
    kind: str
    concept: str | None = None
    numerator: str | None = None
    denominator: str | None = None


@dataclass(frozen=True)
class MetricCatalog:
    version: int
    metrics: dict[str, Metric]

    def concept_of(self, metric_id: str) -> str:
        concept = self.metrics[metric_id].concept
        assert concept is not None
        return concept

    def counts_for(self, metric_id: str) -> tuple[str, ...]:
        """The count metrics a metric depends on (itself for a count)."""
        m = self.metrics[metric_id]
        if m.kind == "count":
            return (m.id,)
        assert m.numerator is not None and m.denominator is not None
        return (m.numerator, m.denominator)


def parse(text: str) -> MetricCatalog:
    data = yaml_strict.parse(text)
    if not isinstance(data, dict) or set(data) != {"version", "metrics"}:
        raise CatalogError("must be a mapping with exactly 'version' and 'metrics'")
    if type(data["version"]) is not int or data["version"] != SUPPORTED_VERSION:
        raise CatalogError("unsupported version")
    raw = data["metrics"]
    if not isinstance(raw, list) or not raw:
        raise CatalogError("metrics: must be a non-empty list")
    metrics: dict[str, Metric] = {}
    for entry in raw:
        if not isinstance(entry, dict) or type(entry.get("id")) is not str or entry.get("kind") not in KINDS:
            raise CatalogError("metric: needs a string id and a known kind")
        mid = entry["id"]
        if mid in metrics:
            raise CatalogError(f"duplicate metric {mid}")
        if entry["kind"] == "count":
            if set(entry) != {"id", "kind", "concept"} or entry["concept"] not in ACTIVITY_CONCEPTS:
                raise CatalogError(f"{mid}: a count needs exactly one known concept")
            metrics[mid] = Metric(mid, "count", concept=entry["concept"])
        else:
            if set(entry) != {"id", "kind", "numerator", "denominator"}:
                raise CatalogError(f"{mid}: a ratio needs exactly numerator and denominator")
            metrics[mid] = Metric(mid, "ratio", numerator=entry["numerator"], denominator=entry["denominator"])
    for m in metrics.values():
        if m.kind == "ratio":
            for side in (m.numerator, m.denominator):
                if side not in metrics or metrics[side].kind != "count":
                    raise CatalogError(f"{m.id}: ratio sides must be count metrics")
    return MetricCatalog(version=data["version"], metrics=metrics)


@functools.lru_cache(maxsize=1)
def load() -> MetricCatalog:
    """Load (once) and validate the packaged catalog. No network."""
    return parse(read_packaged_text(RESOURCE))
