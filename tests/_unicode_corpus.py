"""Shared hostile-character corpus for the Phase 3 round-trip and identifier fuzz tests (R-11a)."""

import functools
import unicodedata

_EXPLICIT = (
    list(range(0x00, 0x20))
    + [0x7F]
    + list(range(0x80, 0xA0))
    + [0xA0, 0xAD, 0x34F, 0x61C, 0x115F, 0x180E]
    + list(range(0x200B, 0x2010))
    + [0x2028, 0x2029]
    + list(range(0x202A, 0x202F))
    + list(range(0x2060, 0x2065))
    + list(range(0x2066, 0x206A))
    + [0x3000, 0x3164]
    + [0xFEFF, 0xFFF9, 0xFFFA, 0xFFFB, 0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF]
    + [0xD800, 0xDBFF, 0xDC00, 0xDFFF]
    + [0xFDD0, 0xFDEF]
    + [0x1F600, 0xE0001, 0xE007F, 0x10FFFF]
    + [0x301, 0x20DD]
)


@functools.lru_cache(maxsize=1)
def category_representatives() -> tuple[int, ...]:
    """The first code point of each Unicode general category."""
    first: dict[str, int] = {}
    for cp in range(0x110000):
        first.setdefault(unicodedata.category(chr(cp)), cp)
    return tuple(sorted(first.values()))


@functools.lru_cache(maxsize=1)
def corpus() -> tuple[str, ...]:
    """Explicit hostile code points plus one representative per general category, deduplicated."""
    seen: dict[int, None] = {}
    for cp in _EXPLICIT + list(category_representatives()):
        seen.setdefault(cp, None)
    return tuple(chr(cp) for cp in seen)


def placements(ch: str) -> dict[str, str]:
    return {"alone": ch, "prefix": ch + "ab", "suffix": "ab" + ch, "middle": "a" + ch + "b"}
