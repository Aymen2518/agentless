"""The agent never gets access to agentless state, and edited state never steers a delete or a rollout."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agentless.config.loader import ConfigError, load_project
from agentless.plan.model import DeployOptions
from tests.fakes import FakeGcp


def _deploy(provider) -> None:
    provider.deploy(DeployOptions(), lambda _: True)


def _staging(data: dict) -> None:
    data["provider"]["stagingBucket"] = "acme-state"


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda d: d.setdefault("memory", {}).update(artifactsBucket="gs://acme-state"), "memory.artifactsBucket"),
        (lambda d: d.setdefault("resources", {}).update(buckets={"data": {"name": "acme-state"}}), "buckets.data"),
        (
            lambda d: d["identity"]["roles"]["resources"].append(
                {"type": "bucket", "name": "acme-state", "roles": ["roles/storage.objectViewer"]}
            ),
            "identity.roles.resources",
        ),
    ],
)
def test_the_agent_can_never_get_access_to_the_state_bucket(agent_dir, edit, change, where) -> None:
    edit(_staging)
    edit(change)
    with pytest.raises(ConfigError, match=f"{where}.*holds agentless state"):
        load_project(agent_dir / "agent.yaml")


def test_project_wide_storage_write_roles_warn_about_the_state_bucket(agent_dir, edit) -> None:
    edit(_staging)
    edit(lambda d: d["identity"]["roles"]["project"].append("roles/storage.objectAdmin"))
    warnings = load_project(agent_dir / "agent.yaml").config.warnings()
    assert any("acme-state, which holds agentless state" in w for w in warnings)


def _tamper(provider, key: str, **fields: object) -> None:
    state = provider.store.read()
    state.resources[key] = {**state.resources[key], **fields}
    provider.store.write(state)


def test_remove_refuses_a_service_account_state_points_elsewhere(make_provider: Callable, gcp: FakeGcp) -> None:
    _deploy(make_provider())
    _tamper(make_provider(), "serviceAccount", email="admin@other-project.iam.gserviceaccount.com")
    with pytest.raises(RuntimeError, match="refusing to act on it"):
        make_provider().remove(lambda _: True)
    assert ("delete_sa", "admin@other-project.iam.gserviceaccount.com") not in gcp.calls


def test_remove_refuses_an_engine_of_another_project(make_provider: Callable, gcp: FakeGcp) -> None:
    _deploy(make_provider())
    other = "projects/999/locations/europe-west1/reasoningEngines/42"
    gcp.engines[other] = {"name": other}
    _tamper(make_provider(), "engine", name=other)
    with pytest.raises(RuntimeError, match="refusing to act on it"):
        make_provider().remove(lambda _: True)
    assert other in gcp.engines
