"""An agent's own entries in a shared URL map: find them, strip them and add them, without touching anything else.

Several agents (and the load balancer's own IaC) share one URL map. An agent owns exactly:

- host mode (no pathPrefix): one host rule for its hosts and one path matcher, both named after the agent;
- path mode (pathPrefix): the path rules pointing at its backend service, inside path matchers that someone else owns.

Everything else in the map is foreign: never edited, never removed. Pure functions, no GCP calls.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

COMPUTE_ROOT = "https://www.googleapis.com/compute/v1/"
MATCHER_PREFIX = "agentless-"  # path matchers agentless creates; another agent's matcher is never shared


@dataclass(frozen=True)
class Route:
    """Where an agent is served on a URL map."""

    hosts: tuple[str, ...]
    path_prefix: str | None
    matcher: str  # name of the agent's own path matcher (host mode)
    backend: str  # its backend service, as a path (projects/…)
    description: str

    @property
    def paths(self) -> list[str]:
        """Path rule paths in path mode."""
        assert self.path_prefix
        return [self.path_prefix, f"{self.path_prefix}/*"]


def path(link: str) -> str:
    """`projects/…` path of a Compute self link or partial URL."""
    index = link.find("projects/")
    return link[index:] if index >= 0 else link


def link(resource_path: str) -> str:
    """Full Compute URL of a `projects/…` path, as URL maps store references."""
    return COMPUTE_ROOT + resource_path


def _matcher(url_map: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((m for m in url_map.get("pathMatchers", []) if m.get("name") == name), None)


def _host_rule(url_map: dict[str, Any], host: str) -> dict[str, Any] | None:
    return next((r for r in url_map.get("hostRules", []) if host in r.get("hosts", [])), None)


def _ours(rule: dict[str, Any], backend: str) -> bool:
    return bool(backend) and path(rule.get("service") or "") == backend  # no backend: nothing is ours


def owned(url_map: dict[str, Any], route: Route) -> dict[str, Any]:
    """The agent's entries, normalized for comparison."""
    rules = {
        m["name"]: sorted(tuple(r.get("paths", [])) for r in m.get("pathRules", []) if _ours(r, route.backend))
        for m in url_map.get("pathMatchers", [])
        if m.get("name") != route.matcher
    }
    own = _matcher(url_map, route.matcher)
    return {
        "hostRules": sorted(
            tuple(sorted(r.get("hosts", [])))
            for r in url_map.get("hostRules", [])
            if r.get("pathMatcher") == route.matcher
        ),
        "matcher": None if own is None else path(own.get("defaultService") or ""),
        "pathRules": {name: paths for name, paths in rules.items() if paths},
    }


def strip(url_map: dict[str, Any], route: Route) -> bool:
    """Remove the agent's entries in place; returns whether anything was removed."""
    changed = False
    if not route.matcher:  # no name: no host rule or path matcher is ours
        route = Route(route.hosts, route.path_prefix, "\0", route.backend, route.description)
    host_rules = [r for r in url_map.get("hostRules", []) if r.get("pathMatcher") != route.matcher]
    if len(host_rules) != len(url_map.get("hostRules", [])):
        url_map["hostRules"], changed = host_rules, True
    matchers = []
    for matcher in url_map.get("pathMatchers", []):
        if matcher.get("name") == route.matcher:
            changed = True
            continue
        rules = matcher.get("pathRules", [])
        kept = [r for r in rules if not _ours(r, route.backend)]
        if len(kept) != len(rules):
            matcher["pathRules"], changed = kept, True
        matchers.append(matcher)
    if "pathMatchers" in url_map:
        url_map["pathMatchers"] = matchers
    return changed


def conflicts(url_map: dict[str, Any], route: Route) -> list[str]:
    """Why the route can't be added to the map, once the agent's own entries are stripped; empty when it can."""
    name = url_map.get("name", "the URL map")
    clean = copy.deepcopy(url_map)
    strip(clean, route)
    problems: list[str] = []
    for host in route.hosts:
        rule = _host_rule(clean, host)
        if route.path_prefix is None:
            if rule is not None:
                problems.append(
                    f"host {host} is already routed by path matcher {rule.get('pathMatcher')} of URL map {name}; "
                    "set cloudRun.loadBalancer.pathPrefix to share it"
                )
            continue
        if rule is None:
            problems.append(
                f"no host rule of URL map {name} routes {host}: add one with a path matcher in the load balancer's "
                "IaC, or drop cloudRun.loadBalancer.pathPrefix to give the agent its own host rule"
            )
            continue
        matcher_name = rule.get("pathMatcher", "")
        matcher = _matcher(clean, matcher_name) or {}
        if matcher_name.startswith(MATCHER_PREFIX):
            problems.append(
                f"host {host} is routed by {matcher_name}, another agentless agent's own path matcher (removed with "
                "it): route the host from a path matcher your IaC owns, or give this agent its own host"
            )
        elif matcher.get("routeRules"):
            problems.append(
                f"path matcher {matcher_name} of URL map {name} uses routeRules, which can't be mixed with the path "
                "rules agentless adds"
            )
        else:
            taken = {p: r.get("service", "") for r in matcher.get("pathRules", []) for p in r.get("paths", [])}
            problems += [
                f"{host}{p} is already routed to {path(taken[p]).rsplit('/', 1)[-1]} by {matcher_name}"
                for p in route.paths
                if p in taken
            ]
    return problems


def attach(url_map: dict[str, Any], route: Route) -> None:
    """Add the agent's entries in place. Call on a map without them and without conflicts."""
    service = link(route.backend)
    if route.path_prefix is None:
        url_map.setdefault("hostRules", []).append(
            {"hosts": list(route.hosts), "pathMatcher": route.matcher, "description": route.description}
        )
        url_map.setdefault("pathMatchers", []).append(
            {"name": route.matcher, "defaultService": service, "description": route.description}
        )
        return
    seen: set[str] = set()
    for host in route.hosts:
        rule = _host_rule(url_map, host)
        assert rule is not None  # conflicts() said so
        name = rule["pathMatcher"]
        if name in seen:
            continue
        seen.add(name)
        matcher = _matcher(url_map, name)
        assert matcher is not None
        matcher.setdefault("pathRules", []).append(
            {
                "paths": route.paths,
                "service": service,
                # Path rules match longest-first, so order never matters; the agent is served at its root.
                "routeAction": {"urlRewrite": {"pathPrefixRewrite": "/"}},
            }
        )


def in_place(url_map: dict[str, Any], route: Route) -> bool:
    """Whether the map already routes exactly as the route says."""
    wanted = copy.deepcopy(url_map)
    strip(wanted, route)
    if conflicts(wanted, route):
        return False
    attach(wanted, route)
    return owned(url_map, route) == owned(wanted, route) and _rewrites_ok(url_map, route)


def _rewrites_ok(url_map: dict[str, Any], route: Route) -> bool:
    for matcher in url_map.get("pathMatchers", []):
        for rule in matcher.get("pathRules", []):
            if _ours(rule, route.backend) and (rule.get("routeAction") or {}).get("urlRewrite") != {
                "pathPrefixRewrite": "/"
            }:
                return False
    return True


def reconcile(url_map: dict[str, Any], route: Route) -> bool:
    """Make the map route as the route says, in place; returns whether it changed. Raises on a conflict."""
    if in_place(url_map, route):
        return False
    if problems := conflicts(url_map, route):
        raise RuntimeError("; ".join(problems))
    strip(url_map, route)
    attach(url_map, route)
    return True


def describe(route: Route, url_map: str) -> str:
    """Plan line for the route."""
    target = route.backend.rsplit("/", 1)[-1]
    if route.path_prefix is None:
        return f"+ URL map {url_map}: {', '.join(route.hosts)} → {target} (own host rule and path matcher)"
    where = ", ".join(f"{h}{route.path_prefix}/*" for h in route.hosts)
    return f"+ URL map {url_map}: {where} → {target} (prefix stripped)"
