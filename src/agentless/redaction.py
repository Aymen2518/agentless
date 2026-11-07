"""Masking of secret values (from ${secret:} or sensitive ${tf:} outputs) in anything printed or stored."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

# A secret this long is also masked inside a longer string ("postgres://u:${secret:pw}@h"). Shorter ones are only
# masked when they are the whole value: masking every occurrence of a 3-character value would garble unrelated text.
MIN_EMBEDDED = 6


def hashed(value: str) -> str:
    """Stable stand-in for a secret: equal secrets compare equal in stored specs, and nothing is revealed."""
    return f"<secret sha256:{hashlib.sha256(value.encode()).hexdigest()[:12]}>"


def mask_text(value: str, sensitive: frozenset[str], mark: Callable[[str], str] = hashed) -> str:
    """`value` with every secret in it replaced by `mark(secret)`; longest secrets first, so none is left in part."""
    if not sensitive:
        return value
    if value in sensitive:
        return mark(value)
    for secret in sorted((s for s in sensitive if len(s) >= MIN_EMBEDDED), key=len, reverse=True):
        if secret in value:
            value = value.replace(secret, mark(secret))
    return value


def mask_tree(node: Any, sensitive: frozenset[str], mark: Callable[[str], str] = hashed) -> Any:
    """Copy of a JSON-like tree with every string masked (keys included)."""
    if not sensitive:
        return node
    if isinstance(node, dict):
        return {mask_tree(k, sensitive, mark): mask_tree(v, sensitive, mark) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [mask_tree(v, sensitive, mark) for v in node]
    if isinstance(node, str):
        return mask_text(node, sensitive, mark)
    return node


def remember(value: Any, sensitive: set[str]) -> None:
    """Record a secret value, or every leaf of a structured one (a sensitive Terraform map or list)."""
    if isinstance(value, dict):
        for item in value.values():
            remember(item, sensitive)
    elif isinstance(value, list | tuple):
        for item in value:
            remember(item, sensitive)
    elif value is not None and not isinstance(value, bool) and str(value):
        sensitive.add(str(value))
