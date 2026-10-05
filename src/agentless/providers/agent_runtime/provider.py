"""Orchestrates plan, deploy, status and remove for an Agent Runtime deployment."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import pluggy

from agentless.compat.agents_cli import write_deployment_metadata
from agentless.config.loader import Project
from agentless.package.packager import Package, collect
from agentless.plan.model import Action, Change, ChangeSet, Context, DeployOptions, Resource
from agentless.providers.agent_runtime.clients import GcpClients
from agentless.providers.agent_runtime.resources import ALL_RESOURCES, EngineResource
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


class AgentRuntimeProvider:
    """Plans and applies the ordered resource list against GCP."""

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
        self.resources: list[Resource] = [cls() for cls in ALL_RESOURCES]

    @functools.cached_property
    def clients(self) -> Any:
        """GCP facade, created on first use so offline commands never authenticate."""
        cfg = self.project.config.provider
        return self._clients or GcpClients(cfg.project, cfg.region, self.project.deployer)

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
                if change.resource != "engine" and change.action != Action.NOOP:
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
        """Finalize a `--no-wait` engine operation; True when nothing is pending anymore."""
        with self.store.lock("status"):
            ctx = self.context(DeployOptions(), None)
            engine = next(r for r in self.resources if isinstance(r, EngineResource))
            done = engine.finalize(ctx)
        if done:
            self._write_metadata(ctx)
        return done

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
        engine = state.resources.get("engine") or {}
        name = engine.get("name")
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
        if name:
            engine_id = name.rsplit("/", 1)[-1]
            out |= {
                "engine": name,
                "sourceHash": (engine.get("sourceHash") or "")[:12],
                "pendingOperation": (engine.get("pending") or {}).get("operation"),
                "queryUrl": f"https://{cfg.provider.region}-aiplatform.googleapis.com/v1/{name}:streamQuery",
                "a2aCardUrl": f"https://{cfg.provider.region}-aiplatform.googleapis.com/reasoningEngines/v1/{name}/a2a/"
                f"{self.project.agent_directory}/.well-known/agent-card.json",
                "console": f"https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/"
                f"{cfg.provider.region}/agent-engines/{engine_id}?project={cfg.provider.project}",
            }
        if state.resources.get("serviceAccount"):
            out["serviceAccount"] = state.resources["serviceAccount"]["email"]
        if engine.get("effectiveIdentity"):
            out["agentIdentity"] = f"principal://{engine['effectiveIdentity']}"
        if state.resources.get("geminiEnterprise"):
            out["geminiEnterprise"] = state.resources["geminiEnterprise"].get("agentName")
        out["iamBindings"] = len((state.resources.get("iam") or {}).get("bindings", []))
        return out

    def _write_metadata(self, ctx: Context) -> None:
        engine = ctx.state.resources.get("engine") or {}
        if not engine.get("name") or engine.get("pending"):
            return
        path = write_deployment_metadata(
            self.project.source_dir,
            engine_name=engine["name"],
            agent_directory=self.project.agent_directory,
            is_a2a=bool(self.project.manifest and self.project.manifest.is_a2a),
        )
        self.echo(f"  wrote {path.name} (agents-cli `run --url`, `publish` keep working)")
