"""Translate agent.yaml into the reasoning-engine spec, update masks and API payloads."""

from __future__ import annotations

import hashlib
import tomllib
from typing import Any

from agentless.config.loader import Project
from agentless.config.schema import IdentityType

# Fields whose change cannot be applied in place.
IMMUTABLE_FIELDS = ("psc_interface_config", "encryption_spec", "identity_type")

# Normalized field -> REST update mask path.
UPDATE_MASKS = {
    "display_name": "display_name",
    "description": "description",
    "labels": "labels",
    "context_spec": "context_spec",
    "agent_framework": "spec.agent_framework",
    "service_account": "spec.service_account",
    "env": "spec.deployment_spec.env",
    "secret_env": "spec.deployment_spec.secret_env",
    "min_instances": "spec.deployment_spec.min_instances",
    "max_instances": "spec.deployment_spec.max_instances",
    "resource_limits": "spec.deployment_spec.resource_limits",
    "container_concurrency": "spec.deployment_spec.container_concurrency",
    "agent_server_mode": "spec.deployment_spec.agent_server_mode",
    # Only sent when the engine has never been deployed (bare Agent Identity shell or adopted engine).
    "psc_interface_config": "spec.deployment_spec.psc_interface_config",
    "encryption_spec": "encryption_spec",
}

# Changing these needs a source rebuild, since image build args live in source_code_spec.
CODE_FIELDS = ("build_args",)

_SECRET_MARK = "secret:"


def desired_spec(project: Project, *, service_account: str | None, engine_name: str | None) -> dict[str, Any]:
    """Normalized desired state of the engine, comparable across runs and stored in state.

    Args:
        project: Loaded project.
        service_account: Runtime SA email when identity type is serviceAccount.
        engine_name: Existing engine name, used for APP_URL; None before creation.
    """
    cfg = project.config
    agent = cfg.agent
    identity_type = {
        IdentityType.SERVICE_ACCOUNT: None,
        IdentityType.PLATFORM: None,
        IdentityType.AGENT_IDENTITY: "AGENT_IDENTITY",
    }[cfg.identity.type]
    return {
        "display_name": cfg.display_name,
        "description": agent.description or "",
        "labels": dict(
            sorted(
                {**cfg.provider.labels, "agentless-service": cfg.service, "agentless-stage": cfg.provider.stage}.items()
            )
        ),
        "agent_framework": agent.framework,
        "service_account": service_account or "",
        "identity_type": identity_type,
        "env": dict(sorted(_env(project, engine_name).items())),
        "secret_env": {k: {"secret": v.secret, "version": v.version} for k, v in sorted(agent.secrets.items())},
        "min_instances": agent.runtime.min_instances,
        "max_instances": agent.runtime.max_instances,
        "resource_limits": {"cpu": agent.runtime.cpu, "memory": agent.runtime.memory},
        "container_concurrency": agent.runtime.concurrency,
        "agent_server_mode": agent.runtime.server_mode.value if agent.runtime.server_mode else None,
        "psc_interface_config": _psc(project),
        "context_spec": _context_spec(project),
        "encryption_spec": {"kms_key_name": agent.encryption.kms_key} if agent.encryption.kms_key else None,
        "build_args": dict(sorted(agent.build.args.items())),
    }


def _env(project: Project, engine_name: str | None) -> dict[str, str]:
    """User env plus the defaults agents-cli injects, so both tools deploy identical runtimes."""
    cfg = project.config
    env = dict(cfg.agent.environment)
    if cfg.memory.sessions == "inMemory":
        env.setdefault("SESSION_SERVICE_URI", "memory://")
    if cfg.memory.artifacts_bucket:
        env.setdefault("LOGS_BUCKET_NAME", cfg.memory.artifacts_bucket.removeprefix("gs://"))
    version = _project_version(project)
    if version:
        env.setdefault("AGENT_VERSION", version)
    if "GEMINI_API_KEY" not in env and "GOOGLE_API_KEY" not in env:
        env.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        env.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    telemetry = cfg.agent.telemetry
    env.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", str(telemetry.enabled).lower())
    env.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "SPAN_AND_EVENT" if telemetry.capture_message_content else "NO_CONTENT",
    )
    env.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", str(telemetry.capture_message_content).lower())
    if engine_name:
        env.setdefault(
            "APP_URL", f"https://{cfg.provider.region}-aiplatform.googleapis.com/reasoningEngines/v1/{engine_name}/api"
        )
    return env


def _project_version(project: Project) -> str | None:
    pyproject = project.source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    return tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("version")


def _psc(project: Project) -> dict[str, Any] | None:
    psc = project.config.network.psc_interface
    if psc is None:
        return None
    out: dict[str, Any] = {"network_attachment": psc.network_attachment}
    if psc.dns_peering:
        out["dns_peering_configs"] = [
            {"domain": d.domain, "target_project": d.target_project, "target_network": d.target_network}
            for d in psc.dns_peering
        ]
    return out


def _model_path(project: Project, model: str) -> str:
    if model.startswith("projects/"):
        return model
    return f"projects/{project.config.provider.project}/locations/{project.config.provider.region}/publishers/google/models/{model}"


def _context_spec(project: Project) -> dict[str, Any] | None:
    bank = project.config.memory.memory_bank
    if bank is None:
        return None
    config: dict[str, Any] = {}
    if bank.generation_model:
        config["generation_config"] = {"model": _model_path(project, bank.generation_model)}
    if bank.embedding_model:
        config["similarity_search_config"] = {"embedding_model": _model_path(project, bank.embedding_model)}
    if bank.ttl:
        config["ttl_config"] = {"default_ttl": bank.ttl}
    if bank.topics:
        config["customization_configs"] = [
            {"memory_topics": [{"managed_memory_topic": {"managed_topic_enum": t}} for t in bank.topics]}
        ]
    if bank.disable_memory_revisions is not None:
        config["disable_memory_revisions"] = bank.disable_memory_revisions
    return {"memory_bank_config": config}


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Fields that differ between last-applied and desired specs."""
    old = old or {}
    return {k: (old.get(k), v) for k, v in new.items() if old.get(k) != v}


def describe(field: str, before: Any, after: Any) -> list[str]:
    """Diff lines for one field; dict fields are expanded per key, secrets never printed."""
    if (
        isinstance(before, dict | type(None))
        and isinstance(after, dict)
        and field in ("env", "labels", "secret_env", "build_args")
    ):
        before = before or {}
        lines = []
        for key in sorted(set(before) | set(after)):
            if key not in after:
                lines.append(f"- {field}.{key}")
            elif key not in before:
                lines.append(f"+ {field}.{key} = {_show(after[key])}")
            elif before[key] != after[key]:
                lines.append(f"~ {field}.{key}: {_show(before[key])} → {_show(after[key])}")
        return lines
    return [f"~ {field}: {_show(before)} → {_show(after)}"]


def _show(value: Any) -> str:
    if isinstance(value, dict) and set(value) == {"secret", "version"}:
        return f"[{_SECRET_MARK}{value['secret']}@{value['version']}]"
    return repr(value)


def api_payload(spec: dict[str, Any], fields: list[str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Build the create (fields=None) or update payload and its update masks.

    On update, a selected field whose desired value is None keeps its mask with no value, which resets it.
    """
    updating = fields is not None
    selected = set(spec) if fields is None else set(fields)
    config: dict[str, Any] = {}
    engine: dict[str, Any] = {}
    deployment: dict[str, Any] = {}
    masks: list[str] = []

    for top in ("display_name", "description", "labels", "context_spec", "encryption_spec"):
        if top in selected and (spec[top] is not None or updating):
            config[top] = spec[top] if spec[top] is not None else {}
            masks.append(UPDATE_MASKS[top])
    if "agent_framework" in selected:
        engine["agent_framework"] = spec["agent_framework"]
        masks.append(UPDATE_MASKS["agent_framework"])
    if "service_account" in selected:
        if spec["service_account"]:
            engine["service_account"] = spec["service_account"]
        masks.append(UPDATE_MASKS["service_account"])
    if "identity_type" in selected and spec["identity_type"]:
        engine["identity_type"] = spec["identity_type"]
    if "env" in selected:
        deployment["env"] = [{"name": k, "value": v} for k, v in spec["env"].items()]
        masks.append(UPDATE_MASKS["env"])
    if "secret_env" in selected:
        deployment["secret_env"] = [{"name": k, "secret_ref": v} for k, v in spec["secret_env"].items()]
        masks.append(UPDATE_MASKS["secret_env"])
    for name in (
        "min_instances",
        "max_instances",
        "resource_limits",
        "container_concurrency",
        "agent_server_mode",
        "psc_interface_config",
    ):
        if name not in selected:
            continue
        if spec[name] is not None:
            deployment[name] = spec[name]
        if spec[name] is not None or updating:
            masks.append(UPDATE_MASKS[name])
    if deployment or any(m.startswith("spec.deployment_spec.") for m in masks):
        engine["deployment_spec"] = deployment
    if engine:
        config["spec"] = engine
    return config, masks


def redact(spec: dict[str, Any], sensitive: frozenset[str]) -> dict[str, Any]:
    """Copy of the spec safe to print and store: values that came from ${secret:} become a short hash."""
    if not sensitive:
        return spec

    def mask(value: str) -> str:
        return f"<secret sha256:{hashlib.sha256(value.encode()).hexdigest()[:12]}>" if value in sensitive else value

    out = dict(spec)
    for name in ("env", "build_args"):
        out[name] = {k: mask(v) for k, v in spec[name].items()}
    return out
