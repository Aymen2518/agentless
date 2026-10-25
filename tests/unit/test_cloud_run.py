"""Cloud Run target: lifecycle against FakeGcp, schema rules and the IAM-only invocation guarantee."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agentless.config.loader import ConfigError, load_project
from agentless.hooks import plugin_manager
from agentless.plan.model import Action, DeployOptions
from agentless.providers.base import DeployError
from agentless.providers.cloud_run.provider import CloudRunProvider
from agentless.state.store import LocalStateStore
from tests.fakes import FakeGcp

SERVICE = "projects/proj-dev/locations/europe-west1/services/sample-agent-dev"
URL = "https://sample-agent-dev-123456.europe-west1.run.app"
GE_APP = "projects/987/locations/global/collections/default_collection/engines/app-1"
GE_AGENT = "serviceAccount:service-987@gcp-sa-discoveryengine.iam.gserviceaccount.com"


def _cloud_run(data: dict) -> None:
    data["provider"]["name"] = "cloud-run"
    data["provider"]["stagingBucket"] = "st-bucket"
    data["memory"] = {"sessions": "inMemory"}


@pytest.fixture
def run_dir(agent_dir: Path, edit: Callable) -> Path:
    edit(_cloud_run)
    _scaffold_for_cloud_run(agent_dir)
    return agent_dir


def _scaffold_for_cloud_run(agent_dir: Path) -> None:
    manifest = agent_dir / "agents-cli-manifest.yaml"
    manifest.write_text(manifest.read_text().replace('"agent_runtime"', '"cloud_run"'))


@pytest.fixture
def make_run(run_dir: Path, gcp: FakeGcp) -> Callable[..., CloudRunProvider]:
    def make(stage: str | None = None) -> CloudRunProvider:
        project = load_project(run_dir / "agent.yaml", stage=stage)
        store = LocalStateStore(run_dir, project.stage)
        return CloudRunProvider(project, hooks=plugin_manager(), clients=gcp, store=store, echo=lambda _: None)

    return make


def deploy(provider: CloudRunProvider, **options: bool):
    return provider.deploy(DeployOptions(**options), lambda _: True)


def actions(provider: CloudRunProvider) -> dict[str, Action]:
    _, changeset = provider.plan(DeployOptions())
    return {c.resource: c.action for c in changeset.changes}


def _service(gcp: FakeGcp) -> dict:
    return gcp.run_services[SERVICE]


def _env(gcp: FakeGcp) -> dict[str, str]:
    container = _service(gcp)["template"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


# --- lifecycle ---------------------------------------------------------------------------------------------------


def test_first_deploy_builds_and_creates_an_iam_protected_service(make_run, gcp: FakeGcp) -> None:
    deploy(make_run())

    assert gcp.kinds().index("upload") < gcp.kinds().index("build") < gcp.kinds().index("run_create")
    assert "run_update" not in gcp.kinds()  # one revision on the first deploy
    service = _service(gcp)
    assert service["invokerIamDisabled"] is False
    container = service["template"]["containers"][0]
    assert "@sha256:" in container["image"]  # pinned by digest
    assert container["image"].startswith("europe-west1-docker.pkg.dev/proj-dev/cloud-run-source-deploy/")
    assert service["template"]["serviceAccount"] == "sample-dev@proj-dev.iam.gserviceaccount.com"
    env = _env(gcp)
    assert env["APP_URL"] == URL
    assert env["GOOGLE_CLOUD_PROJECT"] == "proj-dev"
    assert env["SESSION_SERVICE_URI"] == "memory://"
    secrets = {e["name"]: e["valueSource"] for e in container["env"] if "valueSource" in e}
    assert secrets == {"TOKEN": {"secretKeyRef": {"secret": "tok", "version": "latest"}}}
    assert service["template"]["scaling"] == {"minInstanceCount": 0, "maxInstanceCount": 5}
    info = make_run().info()
    assert info["url"] == URL and info["cloudRunService"] == SERVICE
    meta = (make_run().project.source_dir / "deployment_metadata.json").read_text()
    assert '"deployment_target": "cloud_run"' in meta and URL in meta


def test_rerun_is_a_no_op(make_run, gcp: FakeGcp) -> None:
    deploy(make_run())
    calls = len(gcp.calls)
    assert set(actions(make_run()).values()) == {Action.NOOP}
    deploy(make_run())
    assert len(gcp.calls) == calls


def test_config_change_updates_the_service_without_rebuilding(make_run, gcp: FakeGcp, edit) -> None:
    deploy(make_run())
    edit(lambda d: d["agent"]["environment"].update(DEBUG="true"))
    assert actions(make_run())["build"] == Action.NOOP
    deploy(make_run())
    assert gcp.kinds().count("build") == 1
    assert _env(gcp)["DEBUG"] == "true"


def test_code_change_rebuilds_and_rolls_out_a_new_image(make_run, gcp: FakeGcp, run_dir: Path) -> None:
    deploy(make_run())
    before = _service(gcp)["template"]["containers"][0]["image"]
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    assert actions(make_run())["build"] == Action.UPDATE
    deploy(make_run())
    assert gcp.kinds().count("build") == 2
    assert _service(gcp)["template"]["containers"][0]["image"] != before


def test_build_args_change_rebuilds(make_run, gcp: FakeGcp, edit) -> None:
    deploy(make_run())
    edit(lambda d: d["agent"].update(build={"args": {"PIP_INDEX": "x"}}))
    deploy(make_run())
    builds = [c for c in gcp.calls if c[0] == "build"]
    assert [b[2] for b in builds] == [{}, {"PIP_INDEX": "x"}]
    assert builds[0][1] != builds[1][1]  # a new tag, so the service picks up the new digest
    assert _service(gcp)["template"]["containers"][0]["image"].endswith(gcp.ar_images[builds[1][1]])


def test_already_built_tag_is_reused(make_run, gcp: FakeGcp) -> None:
    deploy(make_run())
    first = gcp.kinds().count("build")
    # State lost the build (e.g. a crash before saving): the tag already in Artifact Registry is reused.
    state = make_run().store.read()
    assert state is not None
    state.resources.pop("build")
    make_run().store.write(state)
    deploy(make_run())
    assert gcp.kinds().count("build") == first


def test_prebuilt_image_skips_the_build(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(cloudRun={"image": "europe-docker.pkg.dev/ci/agents/sample:1.2.3"}))
    deploy(make_run())
    assert "build" not in gcp.kinds() and "upload" not in gcp.kinds()
    assert _service(gcp)["template"]["containers"][0]["image"] == "europe-docker.pkg.dev/ci/agents/sample:1.2.3"
    edit(lambda d: d["cloudRun"].update(image="europe-docker.pkg.dev/ci/agents/sample:1.2.4"))
    assert actions(make_run())["service"] == Action.UPDATE
    deploy(make_run())
    assert _service(gcp)["template"]["containers"][0]["image"].endswith(":1.2.4")


def test_missing_artifact_registry_repository_blocks(make_run, gcp: FakeGcp) -> None:
    gcp.ar_repositories.clear()
    with pytest.raises(DeployError, match="Artifact Registry repository cloud-run-source-deploy not found"):
        deploy(make_run())
    assert "run_create" not in gcp.kinds()


def test_network_settings_map_to_the_service(make_run, gcp: FakeGcp, edit) -> None:
    edit(
        lambda d: d.update(
            cloudRun={
                "ingress": "internal",
                "iap": True,
                "timeout": "900s",
                "vpcEgress": {"network": "vpc", "subnetwork": "sub", "egress": "all-traffic"},
            }
        )
    )
    deploy(make_run())
    service = _service(gcp)
    assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    assert service["iapEnabled"] is True and service["launchStage"] == "BETA"
    assert service["invokerIamDisabled"] is False  # internal ingress never replaces IAM
    assert service["template"]["vpcAccess"] == {
        "networkInterfaces": [{"network": "vpc", "subnetwork": "sub"}],
        "egress": "ALL_TRAFFIC",
    }
    assert service["template"]["timeout"] == "900s"


def test_service_deleted_outside_agentless_is_recreated_with_its_invokers(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(access={"invokers": ["group:team@example.com"]}))
    deploy(make_run())
    gcp.run_service_delete(SERVICE)
    assert actions(make_run())["service"] == Action.CREATE
    deploy(make_run())
    assert gcp.policies[("runService", SERVICE)]["roles/run.invoker"] == {"group:team@example.com"}


def test_foreign_service_with_the_same_name_blocks(make_run, gcp: FakeGcp) -> None:
    gcp.run_services[SERVICE] = {"labels": {"team": "other"}, "template": {"containers": [{"image": "x:1"}]}}
    with pytest.raises(DeployError, match="not managed by agentless"):
        deploy(make_run())


def test_labelled_service_without_state_is_adopted(make_run, gcp: FakeGcp) -> None:
    gcp.run_services[SERVICE] = {
        "labels": {"agentless-service": "sample-agent", "agentless-stage": "dev"},
        "template": {"containers": [{"image": "x:1"}]},
        "uri": URL,
    }
    deploy(make_run())
    assert "run_create" not in gcp.kinds() and "run_update" in gcp.kinds()


def test_partial_failure_then_rerun(make_run, gcp: FakeGcp) -> None:
    real = gcp.run_service_create
    gcp.run_service_create = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="boom"):
        deploy(make_run())
    assert "build" in make_run().store.read().resources  # type: ignore[union-attr]
    gcp.run_service_create = real  # type: ignore[method-assign]
    deploy(make_run())
    assert gcp.kinds().count("build") == 1  # the image built before the failure is reused
    assert SERVICE in gcp.run_services


def test_code_only_patches_the_image_and_defers_config(make_run, gcp: FakeGcp, run_dir: Path, edit) -> None:
    deploy(make_run())
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    edit(lambda d: d["agent"]["environment"].update(NEW="1"))
    _, changeset = make_run().plan(DeployOptions(code_only=True))
    by = {c.resource: c.action for c in changeset.changes}
    assert by["build"] == Action.UPDATE and by["service"] == Action.UPDATE and by["iam"] == Action.NOOP
    deploy(make_run(), code_only=True)
    assert "NEW" not in _env(gcp)
    deploy(make_run())
    assert _env(gcp)["NEW"] == "1"


def test_remove_deletes_service_bindings_sources_and_sa(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(access={"invokers": ["group:team@example.com"]}))
    deploy(make_run())
    assert make_run().remove(lambda _: True)
    assert SERVICE not in gcp.run_services
    assert not gcp.uploads
    assert "sample-dev@proj-dev.iam.gserviceaccount.com" not in gcp.service_accounts
    assert make_run().store.read() is None
    assert gcp.ar_images  # images are kept


# --- invokers: IAM only, never public --------------------------------------------------------------------------


def test_invokers_get_run_invoker_and_only_owned_bindings_are_revoked(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(access={"invokers": ["group:a@example.com", "user:b@example.com"]}))
    deploy(make_run())
    gcp.policies[("runService", SERVICE)]["roles/run.invoker"].add("user:foreign@example.com")
    edit(lambda d: d.update(access={"invokers": ["group:a@example.com"]}))
    deploy(make_run())
    assert gcp.policies[("runService", SERVICE)]["roles/run.invoker"] == {
        "group:a@example.com",
        "user:foreign@example.com",
    }


@pytest.mark.parametrize("member", ["allUsers", "allAuthenticatedUsers"])
def test_public_invokers_are_rejected(run_dir: Path, edit, member: str) -> None:
    edit(lambda d: d.update(access={"invokers": [member]}))
    with pytest.raises(ConfigError, match="must be granted by identity"):
        load_project(run_dir / "agent.yaml")


@pytest.mark.parametrize("member", ["allUsers", "allAuthenticatedUsers"])
def test_a_public_grant_made_outside_agentless_blocks_deploys(make_run, gcp: FakeGcp, member: str) -> None:
    deploy(make_run())
    gcp.policies[("runService", SERVICE)]["roles/run.invoker"].add(member)
    _, changeset = make_run().plan(DeployOptions())
    blocked = {c.resource: c.blocked for c in changeset.changes if c.blocked}
    assert "agents must only be callable through IAM" in blocked["access"]
    with pytest.raises(DeployError, match="callable through IAM"):
        deploy(make_run(), force=True)


def test_invoker_check_disabled_outside_agentless_is_turned_back_on(make_run, gcp: FakeGcp) -> None:
    deploy(make_run())
    gcp.run_services[SERVICE]["invokerIamDisabled"] = True
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert service.action == Action.UPDATE and any("invoker IAM check" in d for d in service.details)
    deploy(make_run())
    assert _service(gcp)["invokerIamDisabled"] is False


# --- sessions engine -----------------------------------------------------------------------------------------------


def test_platform_sessions_get_a_companion_engine(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(memory={"memoryBank": {"ttl": "3600s"}}))
    deploy(make_run())
    assert gcp.kinds().count("run_create") == 1 and "run_update" not in gcp.kinds()
    (engine,) = gcp.engines
    assert gcp.engines[engine]["display_name"] == "sample-agent-dev-sessions"
    assert gcp.engines[engine]["config"]["context_spec"] == {
        "memory_bank_config": {"ttl_config": {"default_ttl": "3600s"}}
    }
    env = _env(gcp)
    assert env["GOOGLE_CLOUD_AGENT_ENGINE_ID"] == engine.rsplit("/", 1)[-1]
    assert env["GOOGLE_CLOUD_AGENT_ENGINE_LOCATION"] == "europe-west1"
    sa = "serviceAccount:sample-dev@proj-dev.iam.gserviceaccount.com"
    # The sessions grant is scoped to the engine; the project grant is only what identity.roles declares.
    assert gcp.policies[("reasoningEngine", engine)]["roles/aiplatform.user"] == {sa}
    owned = make_run().store.read().resources["iam"]["bindings"]  # type: ignore[union-attr]
    assert ["reasoningEngine", engine, "roles/aiplatform.user", sa] in owned
    # back to in-memory: the service drops the engine first, then the engine goes
    edit(lambda d: d.update(memory={"sessions": "inMemory"}))
    deploy(make_run())
    assert not gcp.engines
    assert "GOOGLE_CLOUD_AGENT_ENGINE_ID" not in _env(gcp)
    kinds = gcp.kinds()
    last_update = len(kinds) - 1 - kinds[::-1].index("run_update")
    assert kinds.index("engine_delete") > last_update


# --- publishing ------------------------------------------------------------------------------------------------


def test_gemini_enterprise_registers_the_cloud_run_card(make_run, gcp: FakeGcp, edit, run_dir: Path) -> None:
    edit(lambda d: d.update(publish={"geminiEnterprise": {"app": GE_APP}}))
    deploy(make_run())
    (agent,) = gcp.ge_agents.values()
    assert agent["card_url"] == f"{URL}/a2a/app"
    assert "a2a_agent_definition" in agent["payload"]
    assert GE_AGENT in gcp.policies[("runService", SERVICE)]["roles/run.invoker"]
    # a new image can change the card: GE is refreshed
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    deploy(make_run())
    assert gcp.kinds().count("ge_patch") == 1


def test_agent_registry_registers_the_cloud_run_card(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(publish={"agentRegistry": {}}))
    deploy(make_run())
    (body,) = gcp.registry.values()
    assert body["agentSpec"]["content"]["url"] == f"{URL}/a2a/app"


# --- schema rules ------------------------------------------------------------------------------------------------


def _load(run_dir: Path):
    return load_project(run_dir / "agent.yaml")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(identity={"type": "platform"}), "identity.type must be serviceAccount"),
        (lambda d: d.update(network={"pscInterface": {"networkAttachment": "x"}}), "use cloudRun.vpcEgress"),
        (lambda d: d["agent"]["runtime"].update(serverMode="STABLE"), "serverMode"),
        (lambda d: d.update(access={"role": "roles/run.developer"}), "access.role is fixed"),
        (
            lambda d: d.update(publish={"geminiEnterprise": {"app": GE_APP, "registration": "adk"}}),
            "registration must be a2a",
        ),
        (lambda d: d["provider"].pop("stagingBucket"), "needs provider.stagingBucket"),
        (lambda d: d.update(cloudRun={"timeout": "4000s"}), "between 1s and 3600s"),
        (lambda d: d.update(cloudRun={"allowUnauthenticated": True}), "allowUnauthenticated"),
    ],
)
def test_unsupported_settings_are_rejected(run_dir: Path, edit, change, message: str) -> None:
    edit(change)
    with pytest.raises(ConfigError, match=message):
        _load(run_dir)


def test_cloud_run_block_needs_the_cloud_run_provider(agent_dir: Path, edit) -> None:
    edit(lambda d: d.update(cloudRun={}))
    with pytest.raises(ConfigError, match="only valid with provider.name: cloud-run"):
        load_project(agent_dir / "agent.yaml")


def test_prebuilt_image_needs_no_staging_bucket_nor_dockerfile(run_dir: Path, edit) -> None:
    edit(lambda d: (d["provider"].pop("stagingBucket"), d.update(cloudRun={"image": "x/y:1"})))
    (run_dir / "Dockerfile").unlink()
    assert _load(run_dir).config.cloud_run.image == "x/y:1"  # type: ignore[union-attr]


def test_manifest_target_must_match_the_provider(agent_dir: Path, edit) -> None:
    edit(_cloud_run)  # the fixture project is scaffolded for agent_runtime
    with pytest.raises(ConfigError, match="targets 'agent_runtime' but provider.name is 'cloud-run'"):
        load_project(agent_dir / "agent.yaml")


def test_example_config_validates(run_dir: Path) -> None:
    example = Path(__file__).parents[2] / "examples" / "cloud-run.yaml"
    (run_dir / "agent.yaml").write_text(example.read_text())
    project = load_project(run_dir / "agent.yaml", stage="dev")
    assert project.config.provider.name == "cloud-run"
    assert project.config.cloud_run is not None


# --- CLI -----------------------------------------------------------------------------------------------------------


def test_cli_lifecycle_and_unsupported_flags(fake_cli: Path, edit) -> None:
    import json

    from typer.testing import CliRunner

    from agentless import cli

    edit(_cloud_run)
    _scaffold_for_cloud_run(fake_cli)
    runner = CliRunner()
    cfg = ["-c", str(fake_cli / "agent.yaml")]
    assert runner.invoke(cli.app, ["plan", *cfg, "--detailed-exitcode"]).exit_code == 2
    no_wait = runner.invoke(cli.app, ["deploy", *cfg, "-y", "--no-wait"])
    assert no_wait.exit_code == 1 and "not supported on cloud-run" in no_wait.output
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y"]).exit_code == 0
    info = json.loads(runner.invoke(cli.app, ["info", *cfg, "--json"]).output)
    assert info["url"] == URL and info["target"] == "cloud-run"
    opened = runner.invoke(cli.app, ["open", "logs", *cfg, "--print"])
    assert opened.exit_code == 0 and "cloud_run_revision" in opened.output
    evaluated = runner.invoke(cli.app, ["eval", *cfg, "--dataset", str(fake_cli / "agent.yaml"), "-m", "x"])
    assert evaluated.exit_code == 1 and "not supported on cloud-run" in evaluated.output
    assert runner.invoke(cli.app, ["remove", *cfg, "--yes"]).exit_code == 0


def test_invoke_creates_a_session_and_parses_sse_events() -> None:
    from agentless.providers.cloud_run import ops

    class Response:
        def __init__(self, body=None, lines=()):
            self.ok, self.status_code, self._body, self._lines = True, 200, body, lines

        def json(self):
            return self._body

        def iter_lines(self, decode_unicode=False):
            return iter(self._lines)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Http:
        def __init__(self):
            self.posts: list[tuple[str, dict]] = []

        def post(self, url, json, **kwargs):
            self.posts.append((url, json))
            if url.endswith("/sessions"):
                return Response({"id": "s-1"})
            return Response(lines=['data: {"author": "agent", "content": {"parts": [{"text": "hi"}]}}', "", ": ping"])

    http = Http()
    events = list(ops.invoke(URL, "app", "hello", user_id="u", session_id=None, credentials=None, http=http))
    assert events == [{"session_id": "s-1"}, {"author": "agent", "content": {"parts": [{"text": "hi"}]}}]
    assert http.posts[0][0] == f"{URL}/apps/app/users/u/sessions"
    assert http.posts[1][1]["new_message"] == {"role": "user", "parts": [{"text": "hello"}]}


def test_session_engine_recreated_outside_agentless_repoints_the_service(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(memory={"sessions": "agentPlatform"}))
    deploy(make_run())
    (old,) = gcp.engines
    gcp.engine_delete(old)
    deploy(make_run())
    (new,) = gcp.engines
    assert new != old
    assert _env(gcp)["GOOGLE_CLOUD_AGENT_ENGINE_ID"] == new.rsplit("/", 1)[-1]


def test_internal_ingress_with_gemini_enterprise_warns(make_run, edit) -> None:
    edit(lambda d: d.update(cloudRun={"ingress": "internal"}, publish={"geminiEnterprise": {"app": GE_APP}}))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("Gemini Enterprise calls the agent from outside your VPC" in d for d in service.details)


def test_sessions_grant_is_not_project_wide(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: (d["identity"]["roles"].update(project=[]), d.update(memory={"sessions": "agentPlatform"})))
    deploy(make_run())
    sa = "serviceAccount:sample-dev@proj-dev.iam.gserviceaccount.com"
    assert sa not in gcp.policies[("project", "proj-dev")].get("roles/aiplatform.user", set())
    (engine,) = gcp.engines
    assert sa in gcp.policies[("reasoningEngine", engine)]["roles/aiplatform.user"]
    # recreated outside agentless: the plan isn't blocked, and the new engine gets the grant
    gcp.engine_delete(engine)
    _, changeset = make_run().plan(DeployOptions())
    assert not changeset.blocked
    deploy(make_run())
    (new,) = gcp.engines
    assert sa in gcp.policies[("reasoningEngine", new)]["roles/aiplatform.user"]


def test_code_only_keeps_live_security_settings(make_run, gcp: FakeGcp, run_dir: Path, edit) -> None:
    edit(lambda d: d.update(cloudRun={"iap": True}))
    deploy(make_run())
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    deploy(make_run(), code_only=True)
    service = _service(gcp)
    assert service["iapEnabled"] is True and service["launchStage"] == "BETA"
    assert service["invokerIamDisabled"] is False
    assert "uri" in service  # output-only fields are the API's, not sent back


def test_code_only_does_not_lift_the_public_block(make_run, gcp: FakeGcp, run_dir: Path) -> None:
    deploy(make_run())
    gcp.policies[("runService", SERVICE)]["roles/run.invoker"].add("allUsers")
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    with pytest.raises(DeployError, match="callable through IAM"):
        deploy(make_run(), code_only=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://svc-1.europe-west1.run.app/x",
        "https://evil.example.com/a2a/app/.well-known/agent-card.json",
        "https://svc.run.app.evil.example.com/",
        "https://user@svc-1.europe-west1.run.app/",
        "https://svc-1.europe-west1.run.app:8443/",
    ],
)
def test_credentials_only_go_to_cloud_run_urls(url: str) -> None:
    from agentless.providers.cloud_run.clients import id_token_session

    with pytest.raises(RuntimeError, match="refusing to send credentials"):
        id_token_session(object(), url)


def test_cloud_run_urls_are_accepted() -> None:
    from agentless.providers.cloud_run.clients import audience

    assert audience(f"{URL}/a2a/app/.well-known/agent-card.json") == URL


def test_service_accounts_are_looked_up_in_their_own_project() -> None:
    # Through projects/-, IAM answers 403 (not 404) for a recently deleted account, so a redeploy after remove failed.
    from agentless.providers.agent_runtime.clients import _sa_resource

    email = "dummy-agent-dev@acme-dev.iam.gserviceaccount.com"
    assert _sa_resource(email) == f"projects/acme-dev/serviceAccounts/{email}"
    assert _sa_resource("123-compute@developer.gserviceaccount.com").startswith("projects/-/")


def test_remove_with_a_session_engine_is_complete(make_run, gcp: FakeGcp, edit) -> None:
    # The engine is deleted before IAM revokes its grant on it: that grant must count as gone, not fail the remove.
    edit(lambda d: d.update(memory={"sessions": "agentPlatform"}))
    deploy(make_run())
    assert make_run().remove(lambda _: True)
    assert not gcp.engines and make_run().store.read() is None
