"""Shared helpers for the Phase 4A tenant tests (not a test module)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from bullhorn_mcp.bullhorn.meta import EntityMeta, MetaSource, parse_entity_meta
from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog
from bullhorn_mcp.tenant.changes import commit, propose
from bullhorn_mcp.tenant.profile_v2 import current_catalog_fingerprint
from bullhorn_mcp.tenant.revalidation import DiscoverySnapshot, entity_snapshot
from bullhorn_mcp.tenant.store import SetupStore

NOW = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
NOW_TEXT = "2026-10-06T12:00:00Z"
REST_URL = "https://rest99.bullhornstaffing.com/rest-services/abc123"
FINGERPRINT = current_catalog_fingerprint()

CREDENTIALS = {
    "BULLHORN_CLIENT_ID": "id",
    "BULLHORN_CLIENT_SECRET": "secret",
    "BULLHORN_USERNAME": "user",
    "BULLHORN_PASSWORD": "pw",
}

# Extra (custom) fields present on JobOrder in the default tenant metadata.
JOB_EXTRA = {
    "customText12": {"label": "Priority", "options": [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}]},
    "customInt3": {"label": "Primary Recruiter"},
}


def field_entry(name: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "type": "SCALAR", "dataType": "String", "label": extra.pop("label", name)}
    entry.update(extra)
    return entry


def meta_payload(bullhorn_entity: str, extra: dict[str, dict[str, Any]] | None = None, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    catalog = load_bullhorn_catalog()
    fields = [field_entry(n) for n in catalog.standard_fields(bullhorn_entity) if n not in drop]
    for name, attrs in (extra or {}).items():
        fields.append(field_entry(name, **dict(attrs)))
    return {"entity": bullhorn_entity, "label": bullhorn_entity, "fields": fields}


def tenant_payloads(job_extra: dict[str, dict[str, Any]] | None = None, job_drop: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    catalog = load_bullhorn_catalog()
    out = {}
    for bh in catalog.entities:
        if bh == "JobOrder":
            out[bh] = meta_payload(bh, JOB_EXTRA if job_extra is None else job_extra, job_drop)
        else:
            out[bh] = meta_payload(bh)
    return out


def meta_source(payloads: dict[str, Any], failures: dict[str, BaseException] | None = None) -> Mock:
    """A ``MetaSource`` mock built from raw ``/meta`` payloads (collaborator boundary)."""
    source = Mock(spec=MetaSource)

    def get(entity: str) -> EntityMeta:
        if failures and entity in failures:
            raise failures[entity]
        return parse_entity_meta(entity, payloads[entity])

    source.get_entity_meta.side_effect = get
    return source


def snapshot(
    payloads: dict[str, dict[str, Any]] | None = None,
    checked_at: str = NOW_TEXT,
    rest_fp: str | None = None,
    errors: dict[str, str] | None = None,
) -> DiscoverySnapshot:
    catalog = load_bullhorn_catalog()
    payloads = tenant_payloads() if payloads is None else payloads
    entities = {}
    for bh, ent in catalog.entities.items():
        if errors and bh in errors:
            entities[ent.canonical_entity] = entity_snapshot(ent.canonical_entity, bh, None, errors[bh])
        elif bh in payloads:
            entities[ent.canonical_entity] = entity_snapshot(ent.canonical_entity, bh, parse_entity_meta(bh, payloads[bh]), None)
    return DiscoverySnapshot(checked_at=checked_at, rest_url_fingerprint=rest_fp, entities=entities)


def write_snapshot(store: SetupStore, snap: DiscoverySnapshot, drift: bool = False, **extra: Any) -> None:
    store.write_discovery({"checked_at": snap.checked_at, "snapshot": snap.to_dict(), "report": None, "drift_unresolved": drift, **extra})


def make_store(tmp_path: Path) -> SetupStore:
    root = tmp_path / "store"
    root.mkdir(exist_ok=True)
    return SetupStore(root)


def env(store: SetupStore | None = None, actor: str | None = "admin@example.com", **extra: str) -> dict[str, str]:
    out = dict(CREDENTIALS)
    if store is not None:
        out["BULLHORN_SETUP_STORE"] = str(store.root)
    if actor is not None:
        out["BULLHORN_MCP_ACTOR"] = actor
    out.update(extra)
    return out


def propose_and_commit(store: SetupStore, changes: list[dict[str, Any]], now: dt.datetime = NOW, **env_extra: str) -> dict[str, Any]:
    e = env(store, **env_extra)
    proposal = propose(store, changes, now=now, env=e)
    result = commit(store, proposal["proposal_id"], proposal["diff_hash"], "approve", now=now, env=e)
    assert result["status"] == "committed", result
    return result


def init_tenant(store: SetupStore, extra: list[dict[str, Any]] | None = None, now: dt.datetime = NOW) -> dict[str, Any]:
    return propose_and_commit(store, [{"op": "init_tenant", "tenant_id": "acme"}, *(extra or [])], now=now)


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
