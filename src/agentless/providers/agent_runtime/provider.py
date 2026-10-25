"""Orchestrates plan, deploy, status and remove for an Agent Runtime deployment."""

from __future__ import annotations

from typing import Any

from agentless.compat.agents_cli import write_deployment_metadata
from agentless.plan.model import Context, DeployOptions
from agentless.providers.agent_runtime.clients import GcpClients
from agentless.providers.agent_runtime.resources import ALL_RESOURCES, EngineResource, agent_principal
from agentless.providers.agent_runtime.spec import a2a_card_url
from agentless.providers.base import BaseProvider, DeployError, make_store
from agentless.state.store import State

__all__ = ["AgentRuntimeProvider", "DeployError", "make_store"]


class AgentRuntimeProvider(BaseProvider):
    """Plans and applies the Agent Runtime resource list against GCP."""

    resource_types = ALL_RESOURCES
    code_resources = frozenset({"engine"})

    def make_clients(self) -> Any:  # noqa: D102
        cfg = self.project.config.provider
        return GcpClients(cfg.project, cfg.region, self.project.deployer)

    def status(self) -> bool:
        """Finalize a `--no-wait` engine operation; True when nothing is pending anymore."""
        with self.store.lock("status"):
            ctx = self.context(DeployOptions(), None)
            engine = next(r for r in self.resources if isinstance(r, EngineResource))
            done = engine.finalize(ctx)
        if done:
            self._write_metadata(ctx)
        return done

    def _info(self, state: State) -> dict[str, Any]:
        cfg = self.project.config
        engine = state.resources.get("engine") or {}
        name = engine.get("name")
        out: dict[str, Any] = {}
        if name:
            engine_id = name.rsplit("/", 1)[-1]
            out |= {
                "engine": name,
                "sourceHash": (engine.get("sourceHash") or "")[:12],
                "pendingOperation": (engine.get("pending") or {}).get("operation"),
                "queryUrl": f"https://{cfg.provider.region}-aiplatform.googleapis.com/v1/{name}:streamQuery",
                "a2aCardUrl": a2a_card_url(cfg.provider.region, name, self.project.agent_directory),
                "console": f"https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/"
                f"{cfg.provider.region}/agent-engines/{engine_id}?project={cfg.provider.project}",
            }
        if buckets := (state.resources.get("buckets") or {}).get("items"):
            out["buckets"] = {e.get("key", n): f"gs://{n}" for n, e in sorted(buckets.items())}
        if state.resources.get("serviceAccount"):
            out["serviceAccount"] = state.resources["serviceAccount"]["email"]
        if principal := agent_principal(engine.get("effectiveIdentity")):
            out["agentIdentity"] = f"principal://{principal}"
        if monitors := (state.resources.get("evaluation") or {}).get("monitors"):
            out["onlineMonitors"] = {
                k: m["name"] + ("" if m.get("enabled", True) else " (suspended)") for k, m in sorted(monitors.items())
            }
        if state.resources.get("agentRegistry"):
            out["agentRegistry"] = state.resources["agentRegistry"].get("name")
        if ge := state.resources.get("geminiEnterprise"):
            out["geminiEnterprise"] = ge.get("agentName")
            out["geminiEnterpriseRegistration"] = ge.get("registration", "adk")
            if ge.get("agentGateway"):
                out["agentGateway"] = ge["agentGateway"]
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
