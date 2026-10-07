"""Tenant setup & mapping management MCP tools (Phase 4A, CT-1).

Six tools: ``setup_status``, ``discover_schema``, ``get_mapping_profile``,
``propose_mapping_changes``, ``commit_mapping_changes`` and
``manage_mapping_profile``. None of them writes to Bullhorn: the only Bullhorn
traffic is ``GET /meta/*`` (through ``MetaDiscovery``) and the login flow.
Every failure is returned as a bounded ``ERROR:`` string.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from collections.abc import Callable
from typing import Any

from .. import server
from ..auth import AuthenticationError
from ..bullhorn.client import BullhornAPIError
from ..bullhorn.meta import MetaDiscovery
from ..crosscutting import audit, permissions
from ..schema.bullhorn_catalog import load_bullhorn_catalog
from ..schema.errors import SchemaError, describe_value, truncate_text
from ..tenant import changes as tenant_changes
from ..tenant.profile_v2 import TenantProfileV2, rest_url_fingerprint
from ..tenant.revalidation import (
    DiscoverySnapshot,
    build_drift_report,
    discover,
    load_snapshot,
    merge_snapshots,
)
from ..tenant.state import CREDENTIAL_ENV_VARS, ConnectionCheck, compute_setup_state
from ..tenant.store import STORE_ENV_VAR, SetupStore, SetupStoreError, check_external_path, store_from_env, write_external_text
from ..tenant.timeutil import format_utc, utc_now
from ..tenant.validation import evaluate

MAX_OUTPUT_STRING = 2000
MAX_OUTPUT_CHARS = 2_000_000
MAX_API_ERROR_CHARS = 300
MAX_LIST_ITEMS = 500
MAX_HISTORY_ENTRIES = 50
VIEWS = ("active", "version", "history", "diff", "proposal")


def _now() -> _dt.datetime:
    """The tools' clock (patched in tests)."""
    return utc_now()


# ---------------------------------------------------------------------- #
# Shared plumbing
# ---------------------------------------------------------------------- #


def _bound(value: Any, depth: int = 0) -> Any:
    """Bound every string in the output, and every list (an output line can never be unbounded)."""
    if depth > 50:
        return "<nested>"
    if isinstance(value, str):
        return truncate_text(value, MAX_OUTPUT_STRING)
    if isinstance(value, dict):
        return {truncate_text(str(k), 200): _bound(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_bound(v, depth + 1) for v in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(f"... and {len(value) - MAX_LIST_ITEMS} more")
        return items
    return value


def _respond(data: dict[str, Any]) -> str:
    text = server.format_response(_bound(data))
    return text if len(text) <= MAX_OUTPUT_CHARS else truncate_text(text, MAX_OUTPUT_CHARS)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (AuthenticationError, BullhornAPIError)):
        return truncate_text(f"{type(exc).__name__}: {exc}", MAX_API_ERROR_CHARS)
    if isinstance(exc, SchemaError):
        lines = [truncate_text(line, MAX_OUTPUT_STRING) for line in str(exc).splitlines()[:110]]
        return truncate_text("\n".join(lines), 50_000)
    if isinstance(exc, ValueError):  # e.g. missing credentials from BullhornConfig.from_env
        return truncate_text(str(exc), MAX_API_ERROR_CHARS)
    return f"internal error ({type(exc).__name__})"


def _invoke(tool: str, operation: str, args: dict[str, Any], body: Callable[[], tuple[str, str]]) -> str:
    """Permission check, body, exactly one audit record; never lets an exception escape."""
    start = time.perf_counter()
    decision = permissions.check(tool, operation)
    if not decision.allowed:
        audit.log_invocation(
            tool=tool, args=args, result_summary="denied", duration_ms=(time.perf_counter() - start) * 1000, success=False
        )
        return server._permission_denied_message(tool, decision)
    try:
        result, summary = body()
        success = True
    except Exception as exc:  # the MCP boundary: raw exceptions never escape
        message = _error_text(exc)
        result = f"ERROR: {message}"
        summary = f"error: {truncate_text(message.splitlines()[0] if message else '', 200)}"
        success = False
    audit.log_invocation(
        tool=tool, args=args, result_summary=summary, duration_ms=(time.perf_counter() - start) * 1000, success=success
    )
    return result


def _require_store() -> SetupStore:
    store = store_from_env()
    if store is None:
        raise SetupStoreError(f"{STORE_ENV_VAR} is not configured")
    return store


def _active_profile(store: SetupStore) -> tuple[int | None, TenantProfileV2 | None]:
    active = store.active_version()
    return active, (store.read_version(active) if active is not None else None)


def _check_connection() -> ConnectionCheck:
    try:
        session = server.get_client().auth.session
        return ConnectionCheck(ok=True, rest_url_fingerprint=rest_url_fingerprint(session.rest_url))
    except Exception as exc:
        return ConnectionCheck(ok=False, error=truncate_text(f"{type(exc).__name__}: {exc}", MAX_API_ERROR_CHARS))


def _entity_summary(snapshot: DiscoverySnapshot, names: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    catalog = load_bullhorn_catalog()
    for canon in names:
        ent = snapshot.entity(canon)
        if ent is None:
            continue
        custom = [
            {
                "field": f.name,
                "label": f.label,
                "data_type": f.data_type,
                "field_type": f.field_type,
                "has_options": f.options is not None,
            }
            for f in ent.fields.values()
            if catalog.is_custom_field(f.name)
        ]
        out[canon] = {
            "bullhorn_entity": ent.bullhorn_entity,
            "field_count": len(ent.fields),
            "custom_fields": custom[:200],
            "custom_fields_truncated": max(0, len(custom) - 200),
            "warnings": list(ent.warnings),
            "error": ent.error,
            "meta_unusable": ent.meta_unusable,
        }
    return out


def _run_discovery(store: SetupStore, entities: Any) -> tuple[DiscoverySnapshot, dict[str, Any], dict[str, Any], list[str]]:
    """Discover (metadata only), merge into the stored snapshot, build the drift report. Writes nothing."""
    if entities is not None and (not isinstance(entities, list) or not all(isinstance(e, str) for e in entities)):
        raise SetupStoreError("entities must be a list of entity names or null")
    previous_doc = store.read_discovery() or {}
    previous = load_snapshot(previous_doc)
    client = server.get_client()
    # Log in first: an authentication failure aborts discovery (nothing is recorded)
    # instead of being recorded as a per-entity metadata error.
    fingerprint = rest_url_fingerprint(client.auth.session.rest_url)
    checked_at = format_utc(_now())
    fresh = discover(MetaDiscovery(client), entities, checked_at, fingerprint)
    merged = merge_snapshots(previous, fresh)
    try:
        _, profile = _active_profile(store)
    except SchemaError:
        profile = None
    names = list(fresh.entities)
    report = build_drift_report(previous, merged, profile, names)
    return merged, report, previous_doc, names


# ---------------------------------------------------------------------- #
# Tools
# ---------------------------------------------------------------------- #


@server.mcp.tool()
def setup_status(check_connection: bool = True) -> str:
    """Report the tenant setup state, missing requirements and per-capability readiness.

    Args:
        check_connection: Also log in to Bullhorn to confirm connectivity and the
            REST URL fingerprint (default true).

    Returns:
        JSON with state, missing_requirements, capabilities, active_version,
        open_proposals and last_validation.
    """
    args = {"check_connection": check_connection}

    def body() -> tuple[str, str]:
        connection = None
        if check_connection is True and all((os.environ.get(n) or "").strip() for n in CREDENTIAL_ENV_VARS):
            connection = _check_connection()
        state = compute_setup_state(os.environ, connection, now=_now())
        return _respond(state.to_dict()), f"ok: {state.state}"

    return _invoke("setup_status", "read", args, body)


@server.mcp.tool()
def discover_schema(entities: list[str] | None = None) -> str:
    """Discover the tenant's Bullhorn field metadata (metadata only; no record data, no samples).

    Writes the local discovery snapshot and drift report; never changes mappings
    and never writes to Bullhorn.

    Args:
        entities: Canonical or Bullhorn entity names to discover (default: every catalog entity).

    Returns:
        JSON with per-entity summaries and the drift report.
    """
    args = {"entities": entities}

    def body() -> tuple[str, str]:
        store = _require_store()
        merged, report, previous_doc, names = _run_discovery(store, entities)
        drift = previous_doc.get("drift_unresolved") is True or bool(report["has_findings"])
        store.write_discovery(
            {
                "checked_at": merged.checked_at,
                "snapshot": merged.to_dict(),
                "report": report,
                "drift_unresolved": drift,
                "last_validation": previous_doc.get("last_validation"),
            }
        )
        return (
            _respond(
                {
                    "checked_at": merged.checked_at,
                    "entities": _entity_summary(merged, names),
                    "report": report,
                    "drift_unresolved": drift,
                }
            ),
            "ok",
        )

    return _invoke("discover_schema", "read", args, body)


def _filter_records(doc: dict[str, Any], entity: str | None, search: str | None) -> dict[str, Any]:
    needle = search.lower() if isinstance(search, str) and search else None

    def keep(rec: dict[str, Any], key: str) -> bool:
        if entity is not None and rec.get("entity") != entity:
            return False
        return needle is None or needle in key.lower() or needle in str(rec.get("target", "")).lower()

    doc = dict(doc)
    doc["field_mappings"] = [r for r in doc["field_mappings"] if keep(r, f"{r.get('entity')}.{r.get('field')}")]
    doc["value_mappings"] = [r for r in doc["value_mappings"] if keep(r, str(r.get("key")))]
    return doc


@server.mcp.tool()
def get_mapping_profile(
    view: str = "active",
    version: int | None = None,
    other_version: int | None = None,
    entity: str | None = None,
    search: str | None = None,
) -> str:
    """Read the tenant mapping profile, its history, a version diff, or a proposal.

    Args:
        view: One of "active", "version", "history", "diff", "proposal".
        version: The version for view="version"; the first version for view="diff".
        other_version: The second version for view="diff" (default: the active version).
        entity: Only show mappings of this canonical entity (views "active"/"version").
        search: A substring filter; for view="proposal" it is the proposal ID
            (omit it to list open proposals).

    Returns:
        JSON for the requested view.
    """
    args = {"view": view, "version": version, "other_version": other_version, "entity": entity, "search": search}

    def body() -> tuple[str, str]:
        if view not in VIEWS:
            raise SetupStoreError(f"view must be one of {list(VIEWS)}, got {describe_value(view)}")
        if search is not None and (not isinstance(search, str) or len(search) > 200):
            raise SetupStoreError("search must be a string of at most 200 characters")
        store = _require_store()
        active, _ = _active_profile(store) if view in ("active", "diff") else (store.active_version(), None)
        if view in ("active", "version"):
            number = active if view == "active" else version
            if number is None:
                if view == "version":
                    raise SetupStoreError("version is required for view='version'")
                return _respond({"view": view, "active_version": None, "profile": None}), "ok"
            doc = store.read_version(number).to_dict()
            return _respond({"view": view, "active_version": active, "profile": _filter_records(doc, entity, search)}), "ok"
        if view == "diff":
            if version is None:
                raise SetupStoreError("version is required for view='diff'")
            second = other_version if other_version is not None else active
            if second is None:
                raise SetupStoreError("other_version is required when there is no active version")
            diff = tenant_changes.compute_diff(store.read_version(version), store.read_version(second))
            return _respond({"view": "diff", "from_version": version, "to_version": second, "diff": diff}), "ok"
        if view == "history":
            entries = store.read_history()
            if search:
                needle = search.lower()
                entries = [
                    e for e in entries
                    if any(needle in str(e.get(k, "")).lower() for k in ("action", "actor", "correlation_id", "proposal_id"))
                ]
            latest = list(reversed(entries))[:MAX_HISTORY_ENTRIES]
            return _respond({"view": "history", "count": len(entries), "entries": latest}), "ok"
        # proposal
        now = _now()
        if not search:
            summaries = [
                {k: p.get(k) for k in ("proposal_id", "base_version", "action", "status", "created_at", "expires_at", "actor")}
                for p in store.list_proposals()
                if tenant_changes.is_open(p, now)
            ]
            return _respond({"view": "proposal", "open_proposals": summaries}), "ok"
        proposal = store.load_proposal(search)
        if proposal is None:
            raise SetupStoreError("unknown proposal_id")
        shown = {k: v for k, v in proposal.items() if k != "draft"}
        shown["open"] = tenant_changes.is_open(proposal, now)
        return _respond({"view": "proposal", "proposal": shown}), "ok"

    return _invoke("get_mapping_profile", "read", args, body)


@server.mcp.tool()
def propose_mapping_changes(changes: list[dict[str, Any]], base_version: int | None = None) -> str:
    """Propose mapping changes (the dry-run): validate, diff, hash and store a proposal.

    Nothing is activated; only a local proposal file is written. Commit it with
    commit_mapping_changes using the returned proposal_id and diff_hash.

    Args:
        changes: Change operations, each an object with an "op": init_tenant,
            apply_verified_defaults, set_field_mapping, deactivate_field_mapping,
            remove_field_mapping, set_value_mapping, deactivate_value_mapping,
            remove_value_mapping, set_setting, rollback_to, import_document.
        base_version: The version the change is built on (default: the active version).

    Returns:
        JSON with proposal_id, diff_hash, expires_at, diff, conflicts, dependencies, validation.
    """
    args = {"changes": changes, "base_version": base_version}

    def body() -> tuple[str, str]:
        store = _require_store()
        result = tenant_changes.propose(store, changes, base_version, now=_now(), env=os.environ)
        return _respond(result), f"ok: proposal {result['proposal_id']}"

    return _invoke("propose_mapping_changes", "write", args, body)


@server.mcp.tool()
def commit_mapping_changes(proposal_id: str, diff_hash: str, decision: str) -> str:
    """Approve or reject a stored proposal. The actor comes from BULLHORN_MCP_ACTOR.

    Args:
        proposal_id: The proposal_id returned by propose_mapping_changes.
        diff_hash: The diff_hash returned by propose_mapping_changes.
        decision: "approve" or "reject".

    Returns:
        JSON with status (committed, rejected or refused), version (on commit) and correlation_id.
    """
    args = {"proposal_id": proposal_id, "diff_hash": diff_hash, "decision": decision}

    def body() -> tuple[str, str]:
        store = _require_store()
        result = tenant_changes.commit(store, proposal_id, diff_hash, decision, now=_now(), env=os.environ)
        summary = f"{result['status']}: correlation_id={result['correlation_id']}"
        if "version" in result:
            summary += f" version={result['version']}"
        return _respond(result), summary

    return _invoke("commit_mapping_changes", "write", args, body)


@server.mcp.tool()
def manage_mapping_profile(action: str, path: str | None = None, format: str = "v2") -> str:
    """Validate or export the active mapping profile.

    Import and rollback are proposals (propose_mapping_changes with
    import_document / rollback_to), so nothing is ever activated silently.

    Args:
        action: "validate" (fresh metadata discovery + validation of the active
            version, recorded locally) or "export" (write the active profile to a new file).
        path: For "export": a new .yaml/.yml file outside the setup store.
        format: For "export": "v2" (default) or "v1".

    Returns:
        JSON describing the result.
    """
    args = {"action": action, "path": path, "format": format}

    def body() -> tuple[str, str]:
        store = _require_store()
        if action == "export":
            if format not in ("v2", "v1"):
                raise SetupStoreError(f"format must be 'v2' or 'v1', got {describe_value(format)}")
            active, profile = _active_profile(store)
            if profile is None:
                raise SetupStoreError("there is no active version to export")
            target = check_external_path(path, store.root, for_write=True)
            text = profile.to_yaml_text() if format == "v2" else profile.to_v1_profile().to_yaml_text()
            write_external_text(target, text)
            return _respond({"action": "export", "format": format, "version": active, "path": str(target)}), "ok"
        if action == "validate":
            active, profile = _active_profile(store)
            if profile is None or active is None:
                raise SetupStoreError("there is no active version to validate")
            merged, report, previous_doc, names = _run_discovery(store, None)
            result = evaluate(profile, merged)
            drift = bool(report["has_findings"]) or bool(result.broken)
            store.write_discovery(
                {
                    "checked_at": merged.checked_at,
                    "snapshot": merged.to_dict(),
                    "report": report,
                    "drift_unresolved": drift,
                    "last_validation": {
                        "version": active,
                        "checked_at": merged.checked_at,
                        "states": result.state_map(),
                        "summary": result.summary(),
                    },
                }
            )
            return (
                _respond(
                    {
                        "action": "validate",
                        "version": active,
                        "validation": result.summary(),
                        "report": report,
                        "drift_unresolved": drift,
                    }
                ),
                "ok",
            )
        raise SetupStoreError(f"action must be 'validate' or 'export', got {describe_value(action)}")

    operation = "write" if action == "export" else "read"
    return _invoke("manage_mapping_profile", operation, args, body)
