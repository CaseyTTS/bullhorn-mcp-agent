"""Strict YAML parsing for tenant setup documents (closes NB-9 for 4A paths).

``parse`` first composes the node graph with the safe loader's composer under
the ``YAML_PARSE_ERRORS`` guard and rejects duplicate mapping keys at any
depth, then delegates to the single ``safe_parse_yaml`` choke point (no new
YAML load call site). Every failure is a bounded ``YamlParseFailure``.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..schema.errors import describe_value
from ..schema.yaml_safe import YAML_PARSE_ERRORS, YamlParseFailure, safe_parse_yaml

MAX_YAML_CHARS = 2_000_000
_MERGE_TAG = "tag:yaml.org,2002:merge"

__all__ = ["MAX_YAML_CHARS", "YamlParseFailure", "parse"]


def _same_key(a: Any, b: Any) -> bool:
    # Python dict semantics (1 == 1.0 == True) decide whether a later key silently overwrites an earlier one.
    try:
        return bool(a == b)
    except YAML_PARSE_ERRORS:  # pragma: no cover - scalar comparisons do not raise
        return False


def _check_duplicates(root: yaml.Node | None) -> None:
    if root is None:
        return
    loader = yaml.SafeLoader("")
    try:
        stack: list[yaml.Node] = [root]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, yaml.MappingNode):
                keys: list[Any] = []
                for key_node, value_node in node.value:
                    stack.append(key_node)
                    stack.append(value_node)
                    if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == _MERGE_TAG:
                        continue
                    try:
                        key = loader.construct_object(key_node, deep=True)
                        hash(key)
                    except YAML_PARSE_ERRORS:
                        continue  # the safe loader itself reports unconstructable keys
                    if any(_same_key(k, key) for k in keys):
                        line = key_node.start_mark.line + 1 if key_node.start_mark is not None else 0
                        raise YamlParseFailure(f"duplicate mapping key {describe_value(key)} at line {line}")
                    keys.append(key)
            elif isinstance(node, yaml.SequenceNode):
                stack.extend(node.value)
    finally:
        loader.dispose()


def parse(text: object) -> Any:
    """Parse one YAML document strictly. Raises only ``YamlParseFailure``."""
    if not isinstance(text, str):
        raise YamlParseFailure(f"YAML input must be text, got {describe_value(text)}")
    if len(text) > MAX_YAML_CHARS:
        raise YamlParseFailure(f"YAML document is larger than {MAX_YAML_CHARS} characters")
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        _check_duplicates(root)
    except YamlParseFailure:
        raise
    except YAML_PARSE_ERRORS as exc:
        if isinstance(exc, RecursionError):
            raise YamlParseFailure("YAML nesting too deep") from exc
        raise YamlParseFailure(f"YAML parse error ({type(exc).__name__})") from exc
    return safe_parse_yaml(text)
