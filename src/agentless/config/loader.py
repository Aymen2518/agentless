"""Load `agent.yaml`: pick the stage, resolve variables, then validate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentless.compat.agents_cli import Manifest, read_manifest
from agentless.config.schema import AgentConfig
from agentless.config.sources import build_sources
from agentless.config.variables import Missing, Resolver, SourceFn, VariableError

DEFAULT_CONFIG_FILE = "agent.yaml"


class ConfigError(Exception):
    """agent.yaml is invalid."""


@dataclass(frozen=True)
class Project:
    """A loaded, resolved and validated agent.yaml plus its agents-cli context."""

    config: AgentConfig
    resolved: dict[str, Any]
    config_path: Path
    source_dir: Path
    manifest: Manifest | None
    sensitive: frozenset[str] = frozenset()

    @property
    def stage(self) -> str:
        """Selected stage."""
        return self.config.provider.stage

    @property
    def agent_directory(self) -> str:
        """Python package holding the agent, inside source_dir."""
        return self.config.agent.source.agent_directory or (self.manifest.agent_directory if self.manifest else "app")


def load_project(
    config_path: Path,
    *,
    stage: str | None = None,
    options: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    extra_sources: dict[str, SourceFn] | None = None,
) -> Project:
    """Load and fully validate a project.

    Args:
        config_path: Path to agent.yaml.
        stage: `--stage` override.
        options: All CLI options, exposed as `${opt:...}`.
        params: `--param k=v` overrides.
        extra_sources: Variable sources contributed by plugins.

    Raises:
        ConfigError: When the file is unreadable, a variable fails, or validation fails.
    """
    if not config_path.is_file():
        raise ConfigError(f"{config_path} not found (run `agentless init` to create one)")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{config_path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    root = config_path.parent.resolve()
    options = {k: v for k, v in (options or {}).items() if v is not None}
    if stage:
        options["stage"] = stage
    params = params or {}

    sensitive: set[str] = set()

    def make_resolver(selected: str) -> Resolver:
        sources = build_sources(root=root, stage=selected, options=options, params=params)
        sources.update(extra_sources or {})
        secret = sources["secret"]

        def tracked_secret(ctx: Any, arg: str | None, key: str) -> Any:
            value = secret(ctx, arg, key)
            sensitive.add(str(value))
            return value

        sources["secret"] = tracked_secret
        others = frozenset(f"stages.{name}" for name in raw.get("stages") or {} if name not in (selected, "default"))
        return Resolver(raw, sources, skip=others)

    try:
        selected = stage or _bootstrap_stage(make_resolver)
        stages = raw.get("stages") or {}
        if stages and selected not in stages and "default" not in stages:
            raise ConfigError(f"stage {selected!r} is not declared under `stages` ({', '.join(stages)})")
        raw.setdefault("provider", {})["stage"] = selected
        resolved = make_resolver(selected).resolve_all()
    except VariableError as e:
        raise ConfigError(f"variable error at {e}") from e

    try:
        config = AgentConfig.model_validate(resolved)
    except ValidationError as e:
        raise ConfigError(format_validation_error(e)) from e

    source_dir = (root / config.agent.source.path).resolve()
    manifest = read_manifest(source_dir)
    _check_agents_cli_project(config, source_dir, manifest)
    return Project(config, resolved, config_path.resolve(), source_dir, manifest, frozenset(sensitive))


def _bootstrap_stage(make_resolver: Any) -> str:
    """Resolve `provider.stage` with only opt/env sources meaningful; default `dev`."""
    resolver = make_resolver("dev")
    try:
        value = resolver.get("provider.stage")
    except Missing:
        return "dev"
    return str(value) if value else "dev"


def _check_agents_cli_project(config: AgentConfig, source_dir: Path, manifest: Manifest | None) -> None:
    if not source_dir.is_dir():
        raise ConfigError(f"agent.source.path: {source_dir} does not exist")
    if manifest and manifest.deployment_target not in (None, "agent_runtime"):
        raise ConfigError(
            f"agents-cli project targets {manifest.deployment_target!r}; agentless v1 deploys to agent_runtime only"
        )
    agent_dir = config.agent.source.agent_directory or (manifest.agent_directory if manifest else "app")
    if not (source_dir / agent_dir).is_dir():
        raise ConfigError(f"agent directory {source_dir / agent_dir} not found")
    if not (source_dir / "Dockerfile").is_file():
        raise ConfigError(f"{source_dir}/Dockerfile not found: Agent Runtime builds the agent from a Dockerfile")


def format_validation_error(error: ValidationError) -> str:
    """Render pydantic errors as `path: message` lines using YAML (camelCase) paths."""
    lines = ["agent.yaml is invalid:"]
    for err in error.errors():
        loc = ".".join(f"[{p}]" if isinstance(p, int) else str(p) for p in err["loc"]).replace(".[", "[")
        lines.append(f"  - {loc or '<root>'}: {err['msg']}")
    return "\n".join(lines)
