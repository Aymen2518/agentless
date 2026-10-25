"""Target-agnostic orchestration: package, plan, deploy and remove an ordered resource list under the state lock."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ClassVar

import pluggy

from agentless.config.loader import Project
from agentless.package.packager import Package, collect
from agentless.plan.model import Action, Change, ChangeSet, Context, DeployOptions, Resource
from agentless.state.store import GcsStateStore, LocalStateStore, State, StateError, StateStore


class DeployError(Exception):
    """Deployment cannot proceed."""


def make_store(project: Project) -> StateStore:
    """GCS store when provider.stagingBucket is set, else a local store."""
    cfg = project.config
    deployer = project.deployer.describe()
    if cfg.provider.staging_bucket:
        return GcsStateStore(
            cfg.provider.staging_bucket,
            cfg.service,
            cfg.provider.stage,
            project=cfg.provider.project,
            credentials=project.deployer.credentials(),
            deployer=deployer,
        )
    return LocalStateStore(project.config_path.parent, cfg.provider.stage, deployer=deployer)


class BaseProvider:
    """Plans and applies the ordered resource list against GCP; subclasses pick the resources and clients."""

    resource_types: ClassVar[tuple[type[Resource], ...]] = ()
    # Resources `--code-only` still applies; everything else follows config and waits for a full deploy.
    code_resources: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        project: Project,
        *,
        hooks: pluggy.PluginManager,
        clients: Any = None,
        store: StateStore | None = None,
        echo: Callable[[str], None] = print,
    ):
        self.project = project
        self.hooks = hooks
        self._clients = clients
        self._store = store
        self.echo = echo
        self.resources: list[Resource] = [cls() for cls in self.resource_types]

    def make_clients(self) -> Any:
        """The target's GCP facade."""
        raise NotImplementedError

    @functools.cached_property
    def clients(self) -> Any:
        """GCP facade, created on first use so offline commands never authenticate."""
        return self._clients or self.make_clients()

    @functools.cached_property
    def store(self) -> StateStore:
        """State backend, created on first use."""
        return self._store or make_store(self.project)

    def package(self) -> Package:
        """Fingerprint the agent source, excluding agent.yaml so config edits never force a rebuild."""
        excluded = []
        try:
            excluded.append(self.project.config_path.relative_to(self.project.source_dir).as_posix())
        except ValueError:
            pass
        package = collect(self.project.source_dir, tuple(excluded))
        self.hooks.hook.agentless_after_package(project=self.project, package=package)
        return package

    def _state(self) -> State:
        cfg = self.project.config
        state = self.store.read()
        if state is None:
            return State(cfg.service, cfg.provider.stage, cfg.provider.project, cfg.provider.region)
        if state.project != cfg.provider.project or state.region != cfg.provider.region:
            raise StateError(
                f"{self.store.location} belongs to {state.project}/{state.region}, config targets "
                f"{cfg.provider.project}/{cfg.provider.region}; remove the old deployment first"
            )
        return state

    def context(self, options: DeployOptions, package: Package | None) -> Context:
        """Fresh context with current state."""
        return Context(self.project, self._state(), self.store, self.clients, options, package, self.echo)

    def plan(self, options: DeployOptions) -> tuple[Context, ChangeSet]:
        """Compute the changeset without touching anything."""
        ctx = self.context(options, self.package())
        changeset = ChangeSet([r.plan(ctx) for r in self.resources])
        if options.code_only:
            # Identity, IAM and publishing follow config, so they wait for the next full deploy too.
            for change in changeset.changes:
                if change.resource not in self.code_resources and change.action != Action.NOOP:
                    change.details.insert(0, f"{change.action.value} deferred by --code-only")
                    change.action, change.blocked, change.data = Action.NOOP, None, {}
        self.hooks.hook.agentless_after_plan(context=ctx, changeset=changeset)
        return ctx, changeset

    def deploy(self, options: DeployOptions, confirm: Callable[[ChangeSet], bool]) -> ChangeSet | None:
        """Lock, plan, ask `confirm`, then apply: forward pass for changes, reverse pass for deferred deletions.

        Returns:
            The applied changeset, or None when nothing was applied.
        """
        with self.store.lock("deploy"):
            ctx, changeset = self.plan(options)
            if not confirm(changeset):
                return None
            if changeset.blocked:
                raise DeployError("; ".join(f"{c.resource}: {c.blocked}" for c in changeset.blocked))
            by_key = {r.key: r for r in self.resources}
            self.hooks.hook.agentless_before_apply(context=ctx, changeset=changeset)
            for change in changeset.changes:
                if change.action == Action.NOOP:
                    continue
                self.echo(f"  → {change.resource}: {change.action.value} {change.summary}")
                by_key[change.resource].apply(ctx, change)
            for resource in self.resources:
                if resource.key in ctx.reapply:
                    self.echo(f"  → {resource.key}: refresh")
                    resource.apply(ctx, Change(resource.key, Action.UPDATE, "refresh"))
            for change in reversed(changeset.changes):
                by_key[change.resource].cleanup(ctx, change)
            ctx.save()
        self._write_metadata(ctx)
        self.hooks.hook.agentless_after_deploy(context=ctx, changeset=changeset)
        return changeset

    def status(self) -> bool:
        """Finalize a `--no-wait` operation; True when nothing is pending anymore."""
        return True

    def remove(self, confirm: Callable[[ChangeSet], bool]) -> bool:
        """Lock, show what will be deleted, then delete what state says agentless owns and the state itself."""
        with self.store.lock("remove"):
            ctx = self.context(DeployOptions(), None)
            changeset = ChangeSet([r.plan_destroy(ctx) for r in reversed(self.resources)])
            if not confirm(changeset):
                return False
            for resource in reversed(self.resources):
                if resource.key in ctx.state.resources:
                    self.echo(f"  → {resource.key}: delete")
                    resource.destroy(ctx)
            if ctx.state.resources:
                ctx.save()
                raise DeployError(f"partial remove, still tracked: {', '.join(ctx.state.resources)}; rerun `remove`")
            self.store.delete()
        self.hooks.hook.agentless_after_remove(context=ctx)
        return True

    def info(self) -> dict[str, Any]:
        """Deployed endpoints and identities from state."""
        cfg = self.project.config
        state = self._state()
        out: dict[str, Any] = {
            "service": cfg.service,
            "stage": cfg.provider.stage,
            "project": cfg.provider.project,
            "region": cfg.provider.region,
            "state": self.store.location,
            "updatedAt": state.updated_at,
            "updatedBy": state.updated_by,
            "deployer": self.project.deployer.describe(),
        }
        return out | self._info(state)

    def _info(self, state: State) -> dict[str, Any]:
        """Target-specific entries of `info`."""
        return {}

    def _write_metadata(self, ctx: Context) -> None:
        """Write agents-cli's deployment_metadata.json after a deploy."""
