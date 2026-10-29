"""Orchestrates plan, deploy and remove for a Cloud Run deployment."""

from __future__ import annotations

from typing import Any

from agentless.compat.agents_cli import write_deployment_metadata
from agentless.plan.model import Context
from agentless.providers.base import BaseProvider
from agentless.providers.cloud_run import spec as run_spec
from agentless.providers.cloud_run.clients import CloudRunClients
from agentless.providers.cloud_run.resources import ALL_RESOURCES
from agentless.state.store import State


class CloudRunProvider(BaseProvider):
    """Plans and applies the Cloud Run resource list against GCP."""

    resource_types = ALL_RESOURCES
    code_resources = frozenset({"build", "service"})

    def make_clients(self) -> Any:  # noqa: D102
        cfg = self.project.config.provider
        clients = CloudRunClients(cfg.project, cfg.region, self.project.deployer)
        clients.app_token_header = run_spec.app_token_header(self.project)
        return clients

    def _info(self, state: State) -> dict[str, Any]:
        cfg = self.project.config
        service = state.resources.get("service") or {}
        out: dict[str, Any] = {"target": "cloud-run"}
        if name := service.get("name"):
            out |= {
                "cloudRunService": name,
                "url": service.get("url"),
                "image": service.get("image"),
                "auth": cfg.cloud_run.auth if cfg.cloud_run else "iam",
                "console": f"https://console.cloud.google.com/run/detail/{cfg.provider.region}/"
                f"{name.rsplit('/', 1)[-1]}?project={cfg.provider.project}",
            }
            if service.get("cardUrl"):
                out["a2aCardUrl"] = service["cardUrl"]
        if engine := (state.resources.get("sessionEngine") or {}).get("name"):
            out["sessionEngine"] = engine
        if buckets := (state.resources.get("buckets") or {}).get("items"):
            out["buckets"] = {e.get("key", n): f"gs://{n}" for n, e in sorted(buckets.items())}
        if state.resources.get("serviceAccount"):
            out["serviceAccount"] = state.resources["serviceAccount"]["email"]
        if state.resources.get("agentRegistry"):
            out["agentRegistry"] = state.resources["agentRegistry"].get("name")
        if ge := state.resources.get("geminiEnterprise"):
            out["geminiEnterprise"] = ge.get("agentName")
            out["geminiEnterpriseRegistration"] = ge.get("registration", "a2a")
        out["invokers"] = sorted({b[3] for b in (state.resources.get("access") or {}).get("bindings", [])})
        out["iamBindings"] = len((state.resources.get("iam") or {}).get("bindings", []))
        return out

    def _write_metadata(self, ctx: Context) -> None:
        url = (ctx.state.resources.get("service") or {}).get("url")
        if not url:
            return
        path = write_deployment_metadata(
            self.project.source_dir,
            service_url=url,
            agent_directory=self.project.agent_directory,
            is_a2a=bool(self.project.manifest and self.project.manifest.is_a2a),
        )
        self.echo(f"  wrote {path.name}")
