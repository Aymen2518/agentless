from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from agentless.config.loader import Project, load_project
from agentless.hooks import plugin_manager
from agentless.providers.agent_runtime.provider import AgentRuntimeProvider
from agentless.state.store import LocalStateStore
from tests.fakes import FakeGcp

FIXTURE = Path(__file__).parent / "fixtures" / "sample-agent"


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "sample-agent"
    shutil.copytree(FIXTURE, dest, ignore=shutil.ignore_patterns(".agentless", "deployment_metadata.json"))
    return dest


@pytest.fixture
def edit(agent_dir: Path) -> Callable[[Callable[[dict], None]], None]:
    """Mutate agent.yaml through a callback."""

    def apply(fn: Callable[[dict], None]) -> None:
        path = agent_dir / "agent.yaml"
        data = yaml.safe_load(path.read_text())
        fn(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    return apply


@pytest.fixture
def gcp() -> FakeGcp:
    return FakeGcp()


@pytest.fixture
def make_provider(agent_dir: Path, gcp: FakeGcp) -> Callable[..., AgentRuntimeProvider]:
    def make(stage: str | None = None) -> AgentRuntimeProvider:
        project: Project = load_project(agent_dir / "agent.yaml", stage=stage)
        store = LocalStateStore(agent_dir, project.stage)
        return AgentRuntimeProvider(project, hooks=plugin_manager(), clients=gcp, store=store, echo=lambda _: None)

    return make
