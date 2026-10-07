"""Canonical schema & mapping layer (architecture layer 3).

``SchemaContext.load()`` is the single entry point for later phases. Importing
this package, and loading the catalogs or a profile, never calls Bullhorn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bullhorn_catalog import BullhornCatalog, load_bullhorn_catalog
from .canonical_catalog import CanonicalCatalog, CanonicalEntity, CanonicalField, load_canonical_catalog
from .discovery import DiscoveryReport, EntityDiscovery, SchemaDiscoverer, build_draft_profile, redact_sample
from .errors import CatalogError, ProfileError, SchemaError, UnknownCanonicalFieldError
from .mapping_profile import MappingProfile, ProfileStatus, load_active_profile, resolve_profile_path
from .translator import CanonicalRecord, FieldResolution, FieldTranslator


@dataclass(frozen=True)
class SchemaContext:
    """Catalogs + active profile + translator, loaded together."""

    canonical: CanonicalCatalog
    bullhorn: BullhornCatalog
    profile_status: ProfileStatus
    translator: FieldTranslator

    @classmethod
    def load(cls, profile_path: Path | None = None) -> SchemaContext:
        """Load the catalogs and the active profile.

        With ``profile_path=None`` the profile location comes from
        ``BULLHORN_MAPPING_PROFILE``. Profile problems never raise; they are
        reported in ``profile_status`` and catalog defaults apply.
        """
        canonical = load_canonical_catalog()
        bullhorn = load_bullhorn_catalog()
        status = load_active_profile(profile_path, canonical)
        translator = FieldTranslator(canonical, bullhorn, status.profile)
        return cls(canonical=canonical, bullhorn=bullhorn, profile_status=status, translator=translator)


__all__ = [
    "BullhornCatalog",
    "CanonicalCatalog",
    "CanonicalEntity",
    "CanonicalField",
    "CanonicalRecord",
    "CatalogError",
    "DiscoveryReport",
    "EntityDiscovery",
    "FieldResolution",
    "FieldTranslator",
    "MappingProfile",
    "ProfileError",
    "ProfileStatus",
    "SchemaContext",
    "SchemaDiscoverer",
    "SchemaError",
    "UnknownCanonicalFieldError",
    "build_draft_profile",
    "load_active_profile",
    "load_bullhorn_catalog",
    "load_canonical_catalog",
    "redact_sample",
    "resolve_profile_path",
]
