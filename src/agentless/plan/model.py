"""Planned changes and the resource contract every provider resource implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentless.config.loader import Project
    from agentless.package.packager import Package
    from agentless.state.store import State, StateStore


class Action(StrEnum):
    """What applying a change does."""

    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    DELETE = "delete"
    NOOP = "no-op"


@dataclass
class Change:
    """One resource's planned change; `data` carries provider payload from plan to apply."""

    resource: str
    action: Action
    summary: str
    details: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    blocked: str | None = None


@dataclass
class ChangeSet:
    """Ordered changes for one deploy or remove."""

    changes: list[Change]

    @property
    def pending(self) -> list[Change]:
        """Changes that do something."""
        return [c for c in self.changes if c.action != Action.NOOP]

    @property
    def blocked(self) -> list[Change]:
        """Changes that need an explicit flag to proceed."""
        return [c for c in self.changes if c.blocked]


@dataclass(frozen=True)
class DeployOptions:
    """Flags that alter planning and applying."""

    force: bool = False
    code_only: bool = False
    allow_replace: bool = False
    no_wait: bool = False


@dataclass
class Context:
    """Everything a resource needs: config, state, package, clients and cross-resource outputs."""

    project: Project
    state: State
    store: StateStore | None
    clients: Any
    options: DeployOptions = field(default_factory=DeployOptions)
    package: Package | None = None
    echo: Callable[[str], None] = print
    reapply: set[str] = field(default_factory=set)  # resources to reconcile again, e.g. IAM after a new principal

    def save(self) -> None:
        """Persist state after each resource so a crash never orphans what was created."""
        if self.store is not None:
            self.store.write(self.state)


class Resource(ABC):
    """A managed GCP object; resources are applied in list order and destroyed in reverse."""

    key: str = ""

    @abstractmethod
    def plan(self, ctx: Context) -> Change:
        """Compare desired config with state and live GCP."""

    @abstractmethod
    def apply(self, ctx: Context, change: Change) -> None:
        """Execute the change and record the outcome in ctx.state.resources[key]."""

    @abstractmethod
    def plan_destroy(self, ctx: Context) -> Change:
        """What `remove` would do."""

    @abstractmethod
    def destroy(self, ctx: Context) -> None:
        """Delete only what state says agentless created."""

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: B027
        """Deferred deletions, run in reverse order after every resource was applied."""

    def _state(self, ctx: Context) -> dict[str, Any]:
        return ctx.state.resources.get(self.key) or {}

    def _set_state(self, ctx: Context, value: dict[str, Any] | None) -> None:
        if value is None:
            ctx.state.resources.pop(self.key, None)
        else:
            ctx.state.resources[self.key] = value
        ctx.save()
