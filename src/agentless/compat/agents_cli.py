"""Interop with agents-cli projects: read the manifest, write deployment_metadata.json."""

from __future__ import annotations

import datetime
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MANIFEST_FILE = "agents-cli-manifest.yaml"
METADATA_FILE = "deployment_metadata.json"


@dataclass(frozen=True)
class Manifest:
    """The subset of the agents-cli manifest agentless relies on."""

    name: str | None
    agent_directory: str
    region: str | None
    deployment_target: str | None
    is_a2a: bool
    session_type: str | None


def read_manifest(project_dir: Path) -> Manifest | None:
    """Read `agents-cli-manifest.yaml`, falling back to `[tool.agents-cli]` in pyproject.toml."""
    data: dict[str, Any] | None = None
    manifest = project_dir / MANIFEST_FILE
    pyproject = project_dir / "pyproject.toml"
    if manifest.is_file():
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    elif pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("agents-cli")
    if data is None:
        return None
    params = data.get("create_params", {}) or {}
    return Manifest(
        name=data.get("name"),
        agent_directory=data.get("agent_directory", "app"),
        region=data.get("region"),
        deployment_target=params.get("deployment_target"),
        is_a2a=bool(params.get("is_a2a", False)),
        session_type=params.get("session_type"),
    )


def write_deployment_metadata(project_dir: Path, *, engine_name: str, agent_directory: str, is_a2a: bool) -> Path:
    """Write the file agents-cli reads for `run --url`, `deploy --status` and `publish`."""
    path = project_dir / METADATA_FILE
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            data = {}
    data.pop("pending_operation", None)
    data.update(
        remote_agent_runtime_id=engine_name,
        deployment_target="agent_runtime",
        is_a2a=is_a2a,
        agent_directory=agent_directory,
        deployment_timestamp=datetime.datetime.now(tz=datetime.UTC).isoformat(),
    )
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
