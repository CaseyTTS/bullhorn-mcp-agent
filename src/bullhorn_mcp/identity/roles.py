"""Administrator-configured roles (Phase 5A, D-5A-10 / PA-3).

``roles.setup_admins``, ``roles.write_approvers`` and ``service_principals`` in the
admin config list principals, each as a principal key (64 hex) or as an
``{issuer, subject}`` pair. Both forms normalise to the principal key
``sha256(json([issuer, subject]))``, so a role can never be matched by a
``client_id``, a display name or any other claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
MAX_CLAIM_CHARS = 1024


def principal_key_for(issuer: str, subject: str) -> str:
    """``sha256`` over an unambiguous encoding of ``(issuer, subject)`` (``sub`` is unique only per issuer)."""
    payload = json.dumps([issuer, subject], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def parse_principal(where: str, value: object, errors: list[str]) -> str | None:
    """A principal key from a config entry, or ``None`` (with an error appended)."""
    if isinstance(value, str):
        if _HEX64_RE.fullmatch(value):
            return value
        errors.append(f"{where}: must be a 64-hex principal key or an {{issuer, subject}} mapping")
        return None
    if isinstance(value, dict) and set(value) == {"issuer", "subject"}:
        iss, sub = value["issuer"], value["subject"]
        if (
            isinstance(iss, str) and isinstance(sub, str) and iss and sub
            and len(iss) <= MAX_CLAIM_CHARS and len(sub) <= MAX_CLAIM_CHARS
        ):
            return principal_key_for(iss, sub)
    errors.append(f"{where}: must be a 64-hex principal key or an {{issuer, subject}} mapping of non-empty strings")
    return None


def parse_principal_list(where: str, value: object, errors: list[str]) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        errors.append(f"{where}: must be a list")
        return frozenset()
    out = []
    for i, item in enumerate(value):
        key = parse_principal(f"{where}[{i}]", item, errors)
        if key is not None:
            out.append(key)
    return frozenset(out)


@dataclass(frozen=True)
class Roles:
    setup_admins: frozenset[str] = field(default_factory=frozenset)
    write_approvers: frozenset[str] = field(default_factory=frozenset)
    analytics_viewers: frozenset[str] = field(default_factory=frozenset)  # Phase 6 M2 (D-6-2)
    service_principals: frozenset[str] = field(default_factory=frozenset)

    def is_setup_admin(self, principal_key: str | None) -> bool:
        return principal_key is not None and principal_key in self.setup_admins

    def is_write_approver(self, principal_key: str | None) -> bool:
        return principal_key is not None and principal_key in self.write_approvers

    def is_analytics_viewer(self, principal_key: str | None) -> bool:
        return principal_key is not None and principal_key in self.analytics_viewers

    def is_service(self, principal_key: str | None) -> bool:
        return principal_key is not None and principal_key in self.service_principals

    @staticmethod
    def overlap(a: Iterable[str], b: Iterable[str]) -> frozenset[str]:
        return frozenset(a) & frozenset(b)
