"""Translate agent.yaml into a Cloud Run service (Admin API v2 REST body)."""

from __future__ import annotations

from typing import Any

from agentless.config.loader import Project
from agentless.providers.agent_runtime.spec import base_env

INGRESS = {
    "all": "INGRESS_TRAFFIC_ALL",
    "internal": "INGRESS_TRAFFIC_INTERNAL_ONLY",
    "internal-and-cloud-load-balancing": "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
}
EGRESS = {"private-ranges-only": "PRIVATE_RANGES_ONLY", "all-traffic": "ALL_TRAFFIC"}
INVOKER_ROLE = "roles/run.invoker"
ALL_USERS = "allUsers"  # the invoker of an auth: webhook service; the container checks the shared secret


def service_name(project: Project) -> str:
    """Full resource name of the service."""
    cfg = project.config
    return f"projects/{cfg.provider.project}/locations/{cfg.provider.region}/services/{cfg.run_service_name}"


def service_url(project: Project, project_number: str) -> str:
    """Deterministic URL of the service, known before it exists (used for APP_URL and the A2A card)."""
    cfg = project.config
    return f"https://{cfg.run_service_name}-{project_number}.{cfg.provider.region}.run.app"


def a2a_card_url(url: str, agent_directory: str) -> str:
    """Well-known agent-card URL of a Cloud Run agent (agents-cli's FastAPI app serves A2A under /a2a/<app>)."""
    return f"{url.rstrip('/')}/a2a/{agent_directory}/.well-known/agent-card.json"


def image_uri(project: Project, tag: str) -> str:
    """Artifact Registry tag a source build pushes; tagged by source and build-args hash, so the same code is never rebuilt."""
    cfg = project.config
    assert cfg.cloud_run is not None
    return (
        f"{cfg.provider.region}-docker.pkg.dev/{cfg.provider.project}/{cfg.cloud_run.artifact_registry}/"
        f"{cfg.run_service_name}:{tag}"
    )


def app_token_header(project: Project) -> str | None:
    """Header the container reads the caller's Google token from (auth: googleToken), else None."""
    run = project.config.cloud_run
    return run.google_token.header if run and run.auth == "googleToken" and run.google_token else None


def webhook_header(project: Project) -> str | None:
    """Header the container reads the shared secret from (auth: webhook), else None."""
    run = project.config.cloud_run
    return run.webhook.header if run and run.auth == "webhook" and run.webhook else None


def auth_env(project: Project, url: str) -> dict[str, str]:
    """Settings the container needs to check the caller itself; empty with auth: iam.

    They override agent.env: the check is part of the deployment, not something an env var may loosen.
    """
    run = project.config.cloud_run
    if run and run.auth == "webhook" and run.webhook:
        return {"AGENTLESS_AUTH_MODE": "webhook", "AGENTLESS_WEBHOOK_HEADER": run.webhook.header}
    if not run or run.auth != "googleToken" or not run.google_token:
        return {}
    token = run.google_token
    return {
        "AGENTLESS_AUTH_MODE": "googleToken",
        "AGENTLESS_TOKEN_HEADER": token.header,
        "AGENTLESS_TOKEN_AUDIENCES": ",".join(token.audiences or [url]),
        "AGENTLESS_ALLOWED_PRINCIPALS": ",".join(token.allowed_principals),
    }


def desired_spec(
    project: Project, *, service_account: str | None, url: str, session_engine: str | None
) -> dict[str, Any]:
    """Normalized desired state of the service (everything but the image), comparable across runs and stored in state.

    Args:
        project: Loaded project.
        service_account: Runtime SA email.
        url: Service URL, for APP_URL.
        session_engine: Companion engine name holding sessions and Memory Bank, if any.
    """
    cfg = project.config
    run = cfg.cloud_run
    assert run is not None
    agent = cfg.agent
    env = base_env(project)
    env.setdefault("GOOGLE_CLOUD_PROJECT", cfg.provider.project)
    env.setdefault("APP_URL", url)
    if session_engine:
        env.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ID", session_engine.rsplit("/", 1)[-1])
        env.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", cfg.provider.region)
    env |= auth_env(project, url)
    secrets = dict(agent.secrets)
    if run.auth == "webhook" and run.webhook:
        secrets["AGENTLESS_WEBHOOK_SECRET"] = run.webhook.secret
    vpc = run.vpc_egress
    spec: dict[str, Any] = {
        "labels": dict(
            sorted(
                {**cfg.provider.labels, "agentless-service": cfg.service, "agentless-stage": cfg.provider.stage}.items()
            )
        ),
        "description": agent.description or "",
        "service_account": service_account or "",
        "env": dict(sorted(env.items())),
        "secret_env": {k: {"secret": v.secret, "version": v.version} for k, v in sorted(secrets.items())},
        "min_instances": agent.runtime.min_instances,
        "max_instances": agent.runtime.max_instances,
        "resource_limits": {"cpu": agent.runtime.cpu, "memory": agent.runtime.memory},
        "container_concurrency": agent.runtime.concurrency,
        "ingress": INGRESS[run.ingress],
        "iap": run.iap,
        "vpc_egress": (
            {"network": vpc.network, "subnetwork": vpc.subnetwork, "egress": EGRESS[vpc.egress]} if vpc else None
        ),
        "timeout": run.timeout,
        "cpu_throttling": run.cpu_throttling,
        "session_affinity": run.session_affinity,
        "port": run.port,
        "encryption_key": agent.encryption.kms_key,
    }
    return spec


def api_service(spec: dict[str, Any], image: str) -> dict[str, Any]:
    """REST body of the service. The invoker IAM check is always on (auth: webhook grants allUsers instead)."""
    container: dict[str, Any] = {
        "image": image,
        "ports": [{"containerPort": spec["port"]}],
        "env": [{"name": k, "value": v} for k, v in spec["env"].items()]
        + [
            {"name": k, "valueSource": {"secretKeyRef": {"secret": v["secret"], "version": v["version"]}}}
            for k, v in spec["secret_env"].items()
        ],
        "resources": {
            "limits": dict(spec["resource_limits"]),
            "cpuIdle": spec["cpu_throttling"],
            "startupCpuBoost": True,
        },
    }
    template: dict[str, Any] = {
        "labels": dict(spec["labels"]),
        "scaling": {"minInstanceCount": spec["min_instances"], "maxInstanceCount": spec["max_instances"]},
        "timeout": spec["timeout"],
        "sessionAffinity": spec["session_affinity"],
        "maxInstanceRequestConcurrency": spec["container_concurrency"],
        "containers": [container],
    }
    if spec["service_account"]:
        template["serviceAccount"] = spec["service_account"]
    if spec["encryption_key"]:
        template["encryptionKey"] = spec["encryption_key"]
    if vpc := spec["vpc_egress"]:
        template["vpcAccess"] = {
            "networkInterfaces": [{"network": vpc["network"], "subnetwork": vpc["subnetwork"]}],
            "egress": vpc["egress"],
        }
    body: dict[str, Any] = {
        "labels": dict(spec["labels"]),
        "description": spec["description"],
        "ingress": spec["ingress"],
        "invokerIamDisabled": False,  # org policies such as run.managed.requireInvokerIam forbid turning it off
        "template": template,
    }
    if spec["iap"]:
        body["iapEnabled"] = True
        body["launchStage"] = "BETA"  # direct IAP on Cloud Run is a beta feature
    return body
