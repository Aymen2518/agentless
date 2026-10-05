"""Serverless-style `${source:key, fallback}` variable resolution over a raw YAML tree."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_SOURCE_RE = re.compile(r"^(?P<name>[a-zA-Z][\w-]*)(?:\((?P<arg>.*)\))?:(?P<key>.*)$", re.DOTALL)
_LITERAL_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


class VariableError(Exception):
    """A variable could not be resolved."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path or '<root>'}: {message}")
        self.path = path


class Missing(LookupError):
    """Raised by a source when the key does not exist, so a fallback may apply."""


@dataclass
class Expr:
    """One `${...}` occurrence."""

    source: str | None
    arg: list[Part] | None
    key: list[Part]
    fallback: list[Part] | None
    raw: str
    quoted: bool = False  # a quoted fallback is always a string, never coerced to bool/number


Part = str | Expr


@dataclass
class SourceContext:
    """What a source sees when it is called."""

    resolver: Resolver
    path: str


SourceFn = Callable[[SourceContext, str | None, str], Any]


def _scan_braced(text: str, start: int) -> int:
    """Return the index of the `}` closing the `${` that starts at `start`."""
    depth, i, quote = 0, start, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"" and depth >= 1:
            quote = ch
        elif text.startswith("${", i):
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unterminated '${{' in {text!r}")


def _split_top_level(text: str, sep: str) -> tuple[str, str | None]:
    """Split on the first `sep` that is not inside `${}`, parentheses or quotes."""
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif text.startswith("${", i):
            depth += 1
            i += 1
        elif ch in "})":
            depth -= 1
        elif ch == "(":
            depth += 1
        elif ch == sep and depth == 0:
            return text[:i], text[i + 1 :]
        i += 1
    return text, None


def parse(text: str) -> list[Part]:
    """Parse a string into literal parts and expressions."""
    parts: list[Part] = []
    i = 0
    while True:
        j = text.find("${", i)
        if j < 0:
            if i < len(text):
                parts.append(text[i:])
            return parts
        if j > i:
            parts.append(text[i:j])
        end = _scan_braced(text, j)
        parts.append(_parse_expr(text[j + 2 : end], text[j : end + 1]))
        i = end + 1


def _parse_expr(body: str, raw: str) -> Expr:
    primary, fallback = _split_top_level(body, ",")
    primary = primary.strip()
    fallback_parts, quoted = _parse_fallback(fallback.strip()) if fallback is not None else (None, False)
    # A `(` before the first `:` means the source takes an argument, e.g. file(./x.yml):key.
    match = _SOURCE_RE.match(primary)
    if match and not primary.startswith("${"):
        arg = match.group("arg")
        return Expr(
            match.group("name"),
            parse(arg) if arg is not None else None,
            parse(match.group("key")),
            fallback_parts,
            raw,
            quoted,
        )
    return Expr(None, None, parse(primary), fallback_parts, raw, quoted)


def _parse_fallback(text: str) -> tuple[list[Part], bool]:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return [text[1:-1]], True
    return parse(text), False


def _coerce_literal(text: str) -> Any:
    """Turn an unquoted fallback literal into a typed value."""
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    if _LITERAL_NUMBER_RE.match(text):
        return float(text) if "." in text else int(text)
    return text


@dataclass
class Resolver:
    """Lazily resolves every `${}` in a raw config tree, with memoisation and cycle detection."""

    raw: dict[str, Any]
    sources: dict[str, SourceFn]
    skip: frozenset[str] = frozenset()  # subtrees left raw unless referenced, e.g. other stages' params
    _cache: dict[str, Any] = field(default_factory=dict)
    _active: list[str] = field(default_factory=list)

    def resolve_all(self) -> dict[str, Any]:
        """Return a fully resolved deep copy of the tree."""
        return self._resolve_node(self.raw, "", walking=True)

    def get(self, path: str) -> Any:
        """Resolved value at a dotted path; raises Missing when absent."""
        if path in self._cache:
            return self._cache[path]
        if path in self._active:
            cycle = " -> ".join([*self._active[self._active.index(path) :], path])
            raise VariableError(path, f"circular reference: {cycle}")
        node = self._walk(path)
        self._active.append(path)
        try:
            value = self._resolve_node(node, path)
        finally:
            self._active.pop()
        self._cache[path] = value
        return value

    def _walk(self, path: str) -> Any:
        """Raw node at `path`, resolving intermediate `${...}` strings so `a.b` works when `a` is a variable."""
        node: Any = self.raw
        prefix = ""
        for segment in _segments(path):
            if isinstance(node, str) and "${" in node:
                node = self.get(prefix)
            if isinstance(segment, int):
                if not isinstance(node, list) or segment >= len(node):
                    raise Missing(path)
                node = node[segment]
                prefix = f"{prefix}[{segment}]"
            else:
                if not isinstance(node, Mapping) or segment not in node:
                    raise Missing(path)
                node = node[segment]
                prefix = _join(prefix, segment)
        return node

    def _resolve_node(self, node: Any, path: str, walking: bool = False) -> Any:
        if walking and path in self.skip:
            return node
        if isinstance(node, Mapping):
            return {k: self._resolve_node(v, _join(path, str(k)), walking) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve_node(v, f"{path}[{i}]", walking) for i, v in enumerate(node)]
        if isinstance(node, str) and "${" in node:
            try:
                return self.render(parse(node), path)
            except ValueError as e:
                raise VariableError(path, str(e)) from e
        return node

    def render(self, parts: list[Part], path: str) -> Any:
        """Evaluate parsed parts; a lone expression keeps its native type."""
        if len(parts) == 1 and isinstance(parts[0], Expr):
            return self._eval(parts[0], path)
        out = []
        for part in parts:
            value = part if isinstance(part, str) else self._eval(part, path)
            if isinstance(value, dict | list):
                raise VariableError(path, f"cannot embed a {type(value).__name__} inside a string")
            out.append("" if value is None else str(value))
        return "".join(out)

    def _eval(self, expr: Expr, path: str) -> Any:
        try:
            return self._lookup_expr(expr, path)
        except Missing as e:
            if expr.fallback is None:
                raise VariableError(path, f"{expr.raw} could not be resolved: {e}") from None
            if expr.quoted:
                return expr.fallback[0]
            if len(expr.fallback) == 1 and isinstance(expr.fallback[0], str):
                return _coerce_literal(expr.fallback[0])
            return self.render(expr.fallback, path)

    def _lookup_expr(self, expr: Expr, path: str) -> Any:
        key = str(self.render(expr.key, path)).strip()
        name = expr.source
        if name is None:
            # Bare `${stage}` or `${provider.region}` are shorthands for well-known values / self references.
            name, key = ("stage", "") if key == "stage" else ("self", key)
        source = self.sources.get(name)
        if source is None:
            raise VariableError(path, f"unknown variable source {name!r} in {expr.raw}")
        arg = str(self.render(expr.arg, path)).strip() if expr.arg is not None else None
        return source(SourceContext(self, path), arg, key)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _lookup(tree: Any, path: str) -> Any:
    node = tree
    for segment in _segments(path):
        if isinstance(segment, int):
            if not isinstance(node, list) or segment >= len(node):
                raise Missing(path)
            node = node[segment]
        else:
            if not isinstance(node, Mapping) or segment not in node:
                raise Missing(path)
            node = node[segment]
    return node


def _segments(path: str) -> list[str | int]:
    out: list[str | int] = []
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        out.append(int(token[1:-1]) if token.startswith("[") else token)
    return out


def dig(value: Any, key: str, what: str) -> Any:
    """Follow a dotted key into a loaded document; empty key returns the whole document."""
    if not key:
        return value
    try:
        return _lookup(value, key)
    except Missing:
        raise Missing(f"{key!r} not found in {what}") from None
