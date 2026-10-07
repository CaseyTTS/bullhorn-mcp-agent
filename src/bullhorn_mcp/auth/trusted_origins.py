"""Trusted Bullhorn origins (Phase 5A, D-5A-5; closes DEBT-1).

A host is trusted only when it is a configured suffix **at a label boundary**
(``auth-west.bullhornstaffing.com`` for the suffix ``.bullhornstaffing.com``),
or exactly the bare domain. ``bullhornstaffing.com.attacker.example`` and
``evilbullhornstaffing.com`` are rejected, as are userinfo (``user@host``),
percent-encoding, non-ASCII hosts, empty labels and malformed ports.

``is_trusted_url`` additionally requires the ``https`` scheme. The legacy
password-grant redirect guard checks hosts only (``is_trusted_bullhorn_host``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_SUFFIXES = (".bullhornstaffing.com",)
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_PORT_RE = re.compile(r"[0-9]{1,5}", re.ASCII)
MAX_HOST_CHARS = 253


def _split_netloc(netloc: object) -> str | None:
    """The lower-cased host of ``netloc`` (``host`` or ``host:port``), or ``None`` when malformed."""
    if not isinstance(netloc, str) or not netloc or len(netloc) > MAX_HOST_CHARS + 6:
        return None
    if any(ch in netloc for ch in "@%\\/?#[] \t\r\n") or not netloc.isascii():
        return None
    host, sep, port = netloc.partition(":")
    if sep and (not _PORT_RE.fullmatch(port) or not 0 < int(port) < 65536):
        return None
    host = host.lower()
    if not host or len(host) > MAX_HOST_CHARS or host.endswith("."):
        return None
    if not all(_LABEL_RE.fullmatch(label) for label in host.split(".")):
        return None
    return host


@dataclass(frozen=True)
class TrustedOriginPolicy:
    """Host-suffix allowlist with a label boundary."""

    suffixes: tuple[str, ...] = DEFAULT_SUFFIXES

    @classmethod
    def from_suffixes(cls, suffixes: Iterable[str]) -> TrustedOriginPolicy:
        cleaned = []
        for raw in suffixes:
            if not isinstance(raw, str):
                raise ValueError("trusted suffixes must be strings")
            s = raw.strip().lower()
            if not s.startswith("."):
                s = "." + s
            if _split_netloc(s[1:]) is None or s.count(".") < 2:
                raise ValueError("a trusted suffix must be a domain with at least two labels")
            cleaned.append(s)
        if not cleaned:
            raise ValueError("at least one trusted suffix is required")
        return cls(tuple(cleaned))

    def is_trusted_host(self, netloc: object) -> bool:
        host = _split_netloc(netloc)
        if host is None:
            return False
        for suffix in self.suffixes:
            if host == suffix[1:]:
                return True
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        return False

    def is_trusted_url(self, url: object) -> bool:
        """``https`` only, a trusted host, no userinfo, no fragment. Accepts a URL string or a parsed URL."""
        if not isinstance(url, str) and hasattr(url, "geturl") and hasattr(url, "scheme"):
            url = url.geturl()  # a urllib.parse result (the legacy redirect guard passes ``parsed``)
        if not isinstance(url, str) or not url or len(url) > 4096 or not url.isascii():
            return False
        if any(ch in url for ch in "\\ \t\r\n"):
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme != "https" or parts.fragment or parts.username is not None or parts.password is not None:
            return False
        return self.is_trusted_host(parts.netloc)

    def origin(self, url: str) -> str | None:
        """``https://host[:port]`` of a trusted URL, else ``None``."""
        if not self.is_trusted_url(url):
            return None
        return "https://" + urlsplit(url).netloc.lower()


DEFAULT_POLICY = TrustedOriginPolicy()


def is_trusted_bullhorn_host(netloc: object) -> bool:
    """The default policy's host check (used by the legacy redirect guard)."""
    return DEFAULT_POLICY.is_trusted_host(netloc)


def is_trusted_bullhorn_url(url: object) -> bool:
    return DEFAULT_POLICY.is_trusted_url(url)
