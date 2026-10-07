"""Tenant setup & mapping management (Phase 4A).

Importing this package never calls Bullhorn. All Bullhorn metadata access goes
through ``MetaDiscovery`` (via ``revalidation.discover``); nothing here writes
to Bullhorn.
"""

from __future__ import annotations

from .actor import ActorResolution, resolve_actor
from .capabilities import CAPABILITIES, GateResult
from .changes import commit, compute_diff, compute_diff_hash, propose
from .profile_v2 import (
    FORMAT,
    FieldMappingRecord,
    RecordValidation,
    Settings,
    TenantProfileV2,
    ValueMappingRecord,
    ValueTarget,
    current_catalog_fingerprint,
    load_activity_concepts,
    rest_url_fingerprint,
)
from .revalidation import DiscoverySnapshot, build_drift_report, discover
from .state import ConnectionCheck, SetupState, compute_setup_state, require_capability
from .store import SetupStore, SetupStoreError, store_from_env
from .timeutil import coerce_epoch_millis_to_utc_iso, validate_timezone
from .validation import EvaluationResult, evaluate

__all__ = [
    "CAPABILITIES",
    "FORMAT",
    "ActorResolution",
    "ConnectionCheck",
    "DiscoverySnapshot",
    "EvaluationResult",
    "FieldMappingRecord",
    "GateResult",
    "RecordValidation",
    "SetupState",
    "SetupStore",
    "SetupStoreError",
    "Settings",
    "TenantProfileV2",
    "ValueMappingRecord",
    "ValueTarget",
    "build_drift_report",
    "coerce_epoch_millis_to_utc_iso",
    "commit",
    "compute_diff",
    "compute_diff_hash",
    "compute_setup_state",
    "current_catalog_fingerprint",
    "discover",
    "evaluate",
    "load_activity_concepts",
    "propose",
    "require_capability",
    "resolve_actor",
    "rest_url_fingerprint",
    "store_from_env",
    "validate_timezone",
]
