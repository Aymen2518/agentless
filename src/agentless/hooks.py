"""Plugin hooks, the agentless equivalent of Serverless lifecycle events.

Plugins are Python packages exposing an `agentless` entry point; listing them under `plugins:` in
agent.yaml is not needed. Each hook receives keyword arguments only, so new ones can be added safely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pluggy

if TYPE_CHECKING:
    from agentless.config.loader import Project
    from agentless.config.variables import SourceFn
    from agentless.package.packager import Package
    from agentless.plan.model import ChangeSet, Context

PROJECT_NAME = "agentless"
hookspec = pluggy.HookspecMarker(PROJECT_NAME)
hookimpl = pluggy.HookimplMarker(PROJECT_NAME)


class Spec:
    """Hook specifications."""

    @hookspec
    def agentless_variable_sources(self) -> dict[str, SourceFn]:  # ty: ignore[empty-body]
        """Contribute extra `${name:...}` variable sources."""

    @hookspec
    def agentless_after_load(self, project: Project) -> None:
        """Inspect or reject the validated config, e.g. enforce org policies on labels or roles."""

    @hookspec
    def agentless_after_package(self, project: Project, package: Package) -> None:
        """Run after the source tree is fingerprinted, before planning."""

    @hookspec
    def agentless_after_plan(self, context: Context, changeset: ChangeSet) -> None:
        """Inspect the plan; raise to abort."""

    @hookspec
    def agentless_before_apply(self, context: Context, changeset: ChangeSet) -> None:
        """Run right before the first change is applied."""

    @hookspec
    def agentless_after_deploy(self, context: Context, changeset: ChangeSet) -> None:
        """Run after a successful deploy, e.g. smoke tests or notifications."""

    @hookspec
    def agentless_after_remove(self, context: Context) -> None:
        """Run after a successful remove."""


def plugin_manager(extra: list[Any] | None = None) -> pluggy.PluginManager:
    """Plugin manager loaded with installed `agentless` entry points plus `extra` plugins."""
    pm = pluggy.PluginManager(PROJECT_NAME)
    pm.add_hookspecs(Spec)
    pm.load_setuptools_entrypoints(PROJECT_NAME)
    for plugin in extra or []:
        pm.register(plugin)
    return pm


def variable_sources(pm: pluggy.PluginManager) -> dict[str, SourceFn]:
    """Merge variable sources contributed by plugins."""
    merged: dict[str, SourceFn] = {}
    for contributed in pm.hook.agentless_variable_sources():
        merged.update(contributed or {})
    return merged
