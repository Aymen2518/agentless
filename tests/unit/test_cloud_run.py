"""Cloud Run target: lifecycle against FakeGcp, schema rules and the IAM-only invocation guarantee."""

from __future__ import annotations

import json
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


# --- auth: googleToken -----------------------------------------------------------------------------------------------

_GOOGLE_TOKEN = {"auth": "googleToken", "googleToken": {"allowedPrincipals": ["ops@acme.com", "domain:acme.com"]}}


def _google_token(data: dict) -> None:
    data["cloudRun"] = {**_GOOGLE_TOKEN}
    data["access"] = {"invokers": ["domain:acme.com"]}


def test_google_token_keeps_the_iam_check_and_passes_the_app_settings(make_run, gcp: FakeGcp, edit) -> None:
    edit(_google_token)
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("container must verify the Google token in x-google-token" in d for d in service.details)
    deploy(make_run())

    assert _service(gcp)["invokerIamDisabled"] is False
    env = _env(gcp)
    assert env["AGENTLESS_AUTH_MODE"] == "googleToken"
    assert env["AGENTLESS_TOKEN_HEADER"] == "x-google-token"
    assert env["AGENTLESS_TOKEN_AUDIENCES"] == URL
    assert env["AGENTLESS_ALLOWED_PRINCIPALS"] == "ops@acme.com,domain:acme.com"
    members = gcp.iam_members("runService", SERVICE) or {}
    assert members.get("roles/run.invoker") == {"domain:acme.com"}
    assert not {"allUsers", "allAuthenticatedUsers"} & {m for ms in members.values() for m in ms}


def test_switching_auth_mode_only_updates_the_service_env(make_run, gcp: FakeGcp, edit) -> None:
    deploy(make_run())
    assert "AGENTLESS_AUTH_MODE" not in _env(gcp)
    builds = gcp.kinds().count("build")
    edit(_google_token)
    assert actions(make_run())["service"] == Action.UPDATE
    deploy(make_run())
    assert _env(gcp)["AGENTLESS_AUTH_MODE"] == "googleToken"
    edit(lambda d: d.update(cloudRun={}))
    deploy(make_run())
    assert not any(k.startswith("AGENTLESS_") for k in _env(gcp))
    assert gcp.kinds().count("build") == builds  # config only, never a rebuild


def test_google_token_header_and_audiences_reach_the_container(make_run, gcp: FakeGcp, edit) -> None:
    def change(d: dict) -> None:
        _google_token(d)
        d["cloudRun"]["googleToken"] |= {"header": "X-App-Token", "audiences": ["https://agent.acme.com"]}

    edit(change)
    deploy(make_run())
    env = _env(gcp)
    assert env["AGENTLESS_TOKEN_HEADER"] == "x-app-token"
    assert env["AGENTLESS_TOKEN_AUDIENCES"] == "https://agent.acme.com"


@pytest.mark.parametrize("block", ["environment", "secrets"])
def test_agent_env_cannot_set_the_google_token_settings(run_dir: Path, edit, block: str) -> None:
    def change(d: dict) -> None:
        _google_token(d)
        d["agent"][block] = {"AGENTLESS_ALLOWED_PRINCIPALS": "*" if block == "environment" else {"secret": "s"}}

    edit(change)
    with pytest.raises(ConfigError, match="AGENTLESS_ALLOWED_PRINCIPALS are set by cloudRun.auth: googleToken"):
        _load(run_dir)


def test_principals_are_lowercased(run_dir: Path, edit) -> None:
    edit(lambda d: d.update(cloudRun={"auth": "googleToken", "googleToken": {"allowedPrincipals": ["Ops@Acme.com"]}}))
    run = _load(run_dir).config.cloud_run
    assert run and run.google_token and run.google_token.allowed_principals == ["ops@acme.com"]


def test_deployer_outside_the_allowed_principals_warns(make_run, edit) -> None:
    def change(d: dict) -> None:
        _google_token(d)
        d["provider"]["deployer"] = {"impersonate": "ci@proj-dev.iam.gserviceaccount.com"}

    edit(change)
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("deployer ci@proj-dev.iam.gserviceaccount.com is not in" in d for d in service.details)


def test_no_invokers_with_google_token_warns(make_run, edit) -> None:
    edit(lambda d: d.update(cloudRun={**_GOOGLE_TOKEN}))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("only the deployer passes the IAM check" in d for d in service.details)


@pytest.mark.parametrize(
    ("cloud_run", "message"),
    [
        ({"auth": "googleToken"}, "needs cloudRun.googleToken.allowedPrincipals"),
        ({**_GOOGLE_TOKEN, "iap": True}, "exclusive"),
        ({**_GOOGLE_TOKEN, "ingress": "internal"}, "use ingress all"),
        ({"googleToken": {"allowedPrincipals": ["ops@acme.com"]}}, "only valid with auth: googleToken"),
        ({"auth": "googleToken", "googleToken": {"allowedPrincipals": []}}, "at least 1"),
        ({"auth": "googleToken", "googleToken": {"allowedPrincipals": ["allUsers"]}}, "invalid principal"),
        (
            {"auth": "googleToken", "googleToken": {"header": "Authorization", "allowedPrincipals": ["a@b.co"]}},
            "reserved by HTTP, Cloud Run or IAP",
        ),
        (
            {
                "auth": "googleToken",
                "googleToken": {"header": "X-Serverless-Authorization", "allowedPrincipals": ["a@b.co"]},
            },
            "reserved by HTTP, Cloud Run or IAP",
        ),
        (
            {"auth": "googleToken", "googleToken": {"header": "Host", "allowedPrincipals": ["a@b.co"]}},
            "reserved",
        ),
        (
            {
                "auth": "googleToken",
                "googleToken": {"header": "X-Goog-Authenticated-User", "allowedPrincipals": ["a@b.co"]},
            },
            "reserved",
        ),
        (
            {"auth": "googleToken", "googleToken": {"header": "Content-Type", "allowedPrincipals": ["a@b.co"]}},
            "reserved",
        ),
        ({"auth": "googleToken", "googleToken": {"allowedPrincipals": ["a@b.co,c@d.co"]}}, "invalid principal"),
        ({"auth": "googleToken", "googleToken": {"allowedPrincipals": ["a@b.co"], "audiences": [""]}}, "audience"),
        (
            {"auth": "googleToken", "googleToken": {"allowedPrincipals": ["a@b.co"], "audiences": ["a,b"]}},
            "audience",
        ),
        ({"auth": "public"}, "auth"),
    ],
)
def test_google_token_settings_are_validated(run_dir: Path, edit, cloud_run: dict, message: str) -> None:
    edit(lambda d: d.update(cloudRun=cloud_run))
    with pytest.raises(ConfigError, match=message):
        _load(run_dir)


def test_google_token_session_uses_the_serverless_header(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.oauth2 import id_token

    from agentless.providers.cloud_run.clients import id_token_session

    audiences: list[str] = []
    monkeypatch.setattr(id_token, "fetch_id_token", lambda _req, aud: audiences.append(aud) or "tok")

    plain = id_token_session(object(), URL)
    assert plain.headers["Authorization"] == "Bearer tok"

    both = id_token_session(object(), f"{URL}/run_sse", app_header="x-google-token")
    assert both.headers["X-Serverless-Authorization"] == "Bearer tok"
    assert both.headers["x-google-token"] == "tok"
    assert "Authorization" not in both.headers  # left for the app, never filled by agentless

    custom = id_token_session(object(), URL, app_header="x-google-token", app_token="user-tok")
    assert custom.headers["X-Serverless-Authorization"] == "Bearer tok"
    assert custom.headers["x-google-token"] == "user-tok"
    assert audiences == [URL] * 3


def test_google_token_session_with_a_service_account_key() -> None:
    from agentless.providers.cloud_run.clients import id_token_session

    class Minted:
        token = None

        def refresh(self, _request) -> None:
            self.token = "sa-tok"

    class Key:
        def __init__(self) -> None:
            self.audiences: list[str] = []

        def with_target_audience(self, aud: str) -> Minted:
            self.audiences.append(aud)
            return Minted()

    key = Key()
    session = id_token_session(key, URL, app_header="x-google-token")
    assert key.audiences == [URL]
    assert session.headers["X-Serverless-Authorization"] == "Bearer sa-tok"
    assert session.headers["x-google-token"] == "sa-tok"


def test_app_token_needs_google_token_auth(fake_cli: Path, edit) -> None:
    from typer.testing import CliRunner

    from agentless import cli

    edit(_cloud_run)
    _scaffold_for_cloud_run(fake_cli)
    result = CliRunner().invoke(cli.app, ["invoke", "-c", str(fake_cli / "agent.yaml"), "-m", "hi", "--app-token", "t"])
    assert result.exit_code == 1 and "only applies to Cloud Run agents with cloudRun.auth: googleToken" in result.output


@pytest.mark.parametrize("app_header", [None, "x-google-token"])
def test_token_headers_are_dropped_on_a_cross_host_redirect(monkeypatch: pytest.MonkeyPatch, app_header) -> None:
    import requests
    from google.oauth2 import id_token

    from agentless.providers.cloud_run.clients import id_token_session

    monkeypatch.setattr(id_token, "fetch_id_token", lambda _req, _aud: "tok")
    session = id_token_session(object(), URL, app_header=app_header)

    def redirected(target: str) -> requests.PreparedRequest:
        original = requests.Request("GET", f"{URL}/run_sse", headers=dict(session.headers)).prepare()
        response = requests.Response()
        response.request = original
        follow = original.copy()
        follow.url = target
        session.rebuild_auth(follow, response)
        return follow

    leaked = redirected("https://evil.example.com/")
    assert not {"Authorization", "X-Serverless-Authorization", "x-google-token"} & set(leaked.headers)
    kept = {"X-Serverless-Authorization", "x-google-token"} if app_header else {"Authorization"}
    assert kept <= set(redirected(f"{URL}/other").headers)


# --- auth: webhook ---------------------------------------------------------------------------------------------------

_WEBHOOK = {"auth": "webhook", "webhook": {"secret": {"secret": "gitlab-webhook-token"}}}


def _webhook(data: dict) -> None:
    data["cloudRun"] = json.loads(json.dumps(_WEBHOOK))


def _secret_env(gcp: FakeGcp) -> dict[str, dict]:
    container = _service(gcp)["template"]["containers"][0]
    return {e["name"]: e["valueSource"]["secretKeyRef"] for e in container["env"] if "valueSource" in e}


def _all_users(gcp: FakeGcp) -> bool:
    return "allUsers" in (gcp.iam_members("runService", SERVICE) or {}).get("roles/run.invoker", set())


def test_webhook_keeps_iam_on_grants_all_users_and_hands_the_secret_to_the_container(
    make_run, gcp: FakeGcp, edit
) -> None:
    edit(_webhook)
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("allUsers holds roles/run.invoker" in d for d in service.details)
    assert not any("never public" in d for d in service.details)
    access = next(c for c in changeset.changes if c.resource == "access")
    assert "+ roles/run.invoker on the service → allUsers" in access.details
    deploy(make_run())

    assert _service(gcp)["invokerIamDisabled"] is False
    assert _all_users(gcp)
    env = _env(gcp)
    assert env["AGENTLESS_AUTH_MODE"] == "webhook"
    assert env["AGENTLESS_WEBHOOK_HEADER"] == "x-gitlab-token"
    assert _secret_env(gcp)["AGENTLESS_WEBHOOK_SECRET"] == {"secret": "gitlab-webhook-token", "version": "latest"}
    accessors = gcp.iam_members("secret", "gitlab-webhook-token") or {}
    assert accessors.get("roles/secretmanager.secretAccessor")  # the runtime SA reads it
    kinds = gcp.kinds()
    assert kinds.count("webhook_probe") == 1
    grant = next(i for i, c in enumerate(gcp.calls) if c[0] == "iam" and c[1] == "runService")
    assert grant < kinds.index("webhook_probe")  # probed once public
    plan = actions(make_run())
    assert plan["service"] == Action.NOOP and plan["access"] == Action.NOOP


def test_webhook_fails_closed_when_the_container_lets_anonymous_calls_in(make_run, gcp: FakeGcp, edit) -> None:
    edit(_webhook)
    gcp.webhook_statuses = (200, 401)
    with pytest.raises(RuntimeError, match="answered 200 without x-gitlab-token"):
        deploy(make_run())
    assert not _all_users(gcp)  # revoked
    assert _service(gcp)["invokerIamDisabled"] is False

    gcp.webhook_statuses = (401, 401)  # the app is fixed: the next deploy grants allUsers again and probes
    assert actions(make_run())["access"] != Action.NOOP
    deploy(make_run())
    assert _all_users(gcp)
    assert gcp.kinds().count("webhook_probe") == 2


def test_webhook_rejects_a_wrong_token_too(make_run, gcp: FakeGcp, edit) -> None:
    edit(_webhook)
    gcp.webhook_statuses = (401, 200)  # checks the header is present, not its value
    with pytest.raises(RuntimeError, match="200 with a wrong one"):
        deploy(make_run())
    assert not _all_users(gcp)


@pytest.mark.parametrize("error", [RuntimeError("probe crashed"), KeyboardInterrupt()])
def test_webhook_fails_closed_when_the_probe_itself_fails(make_run, gcp: FakeGcp, edit, error) -> None:
    edit(_webhook)
    real = gcp.webhook_probe

    def broken(_url: str, _header: str) -> list[int]:
        raise error

    gcp.webhook_probe = broken  # type: ignore[method-assign]
    with pytest.raises(type(error)):
        deploy(make_run())
    assert not _all_users(gcp)

    gcp.webhook_probe = real  # type: ignore[method-assign]
    deploy(make_run())
    assert _all_users(gcp)


def test_a_new_revision_of_a_public_webhook_service_is_probed_and_fails_closed(
    make_run, gcp: FakeGcp, run_dir: Path, edit
) -> None:
    edit(_webhook)
    deploy(make_run())
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    gcp.webhook_statuses = (200, 200)  # the new code dropped the gate
    with pytest.raises(RuntimeError, match="answered 200"):
        deploy(make_run())
    assert not _all_users(gcp)


def test_webhook_probe_timeouts_count_as_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from agentless.providers.cloud_run.clients import CloudRunClients

    def timeout(*_a, **_k):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(requests, "get", timeout)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    statuses = CloudRunClients.webhook_probe(object.__new__(CloudRunClients), URL, "x-gitlab-token")  # type: ignore[arg-type]
    assert statuses == [0, 0]


def test_switching_from_webhook_back_to_iam_revokes_all_users(make_run, gcp: FakeGcp, edit) -> None:
    edit(_webhook)
    deploy(make_run())
    edit(lambda d: d.update(cloudRun={}))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert any("- secret_env.AGENTLESS_WEBHOOK_SECRET" in d for d in service.details)
    access = next(c for c in changeset.changes if c.resource == "access")
    assert "- roles/run.invoker on the service → allUsers" in access.details
    before = len(gcp.calls)
    deploy(make_run())
    assert not _all_users(gcp)
    assert not any(k.startswith("AGENTLESS_") for k in {**_env(gcp), **_secret_env(gcp)})
    assert gcp.kinds().count("webhook_probe") == 1
    calls = gcp.calls[before:]
    revoke = next(i for i, c in enumerate(calls) if c[0] == "iam" and c[1] == "runService")
    assert calls[revoke][4] == [("roles/run.invoker", "allUsers")]
    assert revoke < next(i for i, c in enumerate(calls) if c[0] == "run_update")  # closed before the gate goes
    assert actions(make_run())["access"] == Action.NOOP


def test_a_service_deployed_with_iam_off_gets_it_back_on(make_run, gcp: FakeGcp, edit) -> None:
    edit(_webhook)
    deploy(make_run())
    gcp.run_services[SERVICE]["invokerIamDisabled"] = True  # how agentless 0.10 and 0.11 deployed webhook mode
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert "~ turn the invoker IAM check back on (auth webhook now grants allUsers)" in service.details
    deploy(make_run())
    assert _service(gcp)["invokerIamDisabled"] is False
    assert _all_users(gcp)
    assert actions(make_run())["service"] == Action.NOOP


def test_webhook_code_only_keeps_all_users_and_probes_once(make_run, gcp: FakeGcp, run_dir: Path, edit) -> None:
    edit(_webhook)
    deploy(make_run())
    (run_dir / "app" / "agent.py").write_text("# changed\n")
    deploy(make_run(), code_only=True)
    assert _service(gcp)["invokerIamDisabled"] is False
    assert _all_users(gcp)
    assert gcp.kinds().count("webhook_probe") == 2


@pytest.mark.parametrize(
    "binding", [("roles/run.invoker", "allAuthenticatedUsers"), ("roles/run.developer", "allUsers")]
)
def test_other_public_grants_still_block_in_webhook_mode(make_run, gcp: FakeGcp, edit, binding) -> None:
    edit(_webhook)
    deploy(make_run())
    gcp.iam_modify("runService", SERVICE, [binding], [])
    with pytest.raises(DeployError, match="callable through IAM"):
        deploy(make_run(), force=True)


def test_webhook_under_org_policies_deploys_with_both_tags(make_run, gcp: FakeGcp, edit) -> None:
    gcp.require_invoker_iam = True
    gcp.ingress_all_tag = INGRESS_TAG
    gcp.all_users_tag = USERS_TAG
    edit(lambda d: (_webhook(d), _tags(INGRESS_TAG, USERS_TAG)(d)))
    deploy(make_run())
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_ALL"
    assert _all_users(gcp)
    assert gcp.kinds().count("webhook_probe") == 1


def test_webhook_all_users_grant_fails_without_the_sharing_tag(make_run, gcp: FakeGcp, edit) -> None:
    gcp.require_invoker_iam = True
    gcp.ingress_all_tag = INGRESS_TAG
    gcp.all_users_tag = USERS_TAG
    edit(lambda d: (_webhook(d), _tags(INGRESS_TAG)(d)))
    with pytest.raises(RuntimeError, match="permitted customer"):
        deploy(make_run())
    assert not _all_users(gcp)


def test_webhook_header_can_be_changed(make_run, gcp: FakeGcp, edit) -> None:
    def change(d: dict) -> None:
        _webhook(d)
        d["cloudRun"]["webhook"] |= {"header": "X-Hub-Signature-256"}

    edit(change)
    deploy(make_run())
    assert _env(gcp)["AGENTLESS_WEBHOOK_HEADER"] == "x-hub-signature-256"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(cloudRun={"auth": "webhook"}), "needs cloudRun.webhook.secret"),
        (lambda d: d.update(cloudRun={**_WEBHOOK, "iap": True}), "exclusive"),
        (lambda d: d.update(cloudRun={"webhook": _WEBHOOK["webhook"]}), "only valid with auth: webhook"),
        (
            lambda d: d.update(
                cloudRun={"auth": "webhook", "webhook": {"header": "Authorization", "secret": {"secret": "s"}}}
            ),
            "reserved",
        ),
        (
            lambda d: d.update(
                cloudRun={"auth": "webhook", "webhook": {"header": "X-Forwarded-For", "secret": {"secret": "s"}}}
            ),
            "reserved",
        ),
        (lambda d: (_webhook(d), d.update(access={"invokers": ["user:a@b.co"]})), "access.invokers grant nothing"),
        (
            lambda d: (_webhook(d), d.update(publish={"agentRegistry": {}})),
            "remove publish with cloudRun.auth: webhook",
        ),
        (
            lambda d: (_webhook(d), d["agent"].update(environment={"AGENTLESS_WEBHOOK_HEADER": "x"})),
            "AGENTLESS_WEBHOOK_HEADER are set by cloudRun.auth: webhook",
        ),
        (
            lambda d: (_webhook(d), d["agent"].update(secrets={"AGENTLESS_WEBHOOK_SECRET": {"secret": "other"}})),
            "AGENTLESS_WEBHOOK_SECRET are set by cloudRun.auth: webhook",
        ),
    ],
)
def test_webhook_settings_are_validated(run_dir: Path, edit, change, message: str) -> None:
    edit(change)
    with pytest.raises(ConfigError, match=message):
        _load(run_dir)


def test_webhook_session_sends_only_the_secret() -> None:
    from agentless.providers.cloud_run.clients import webhook_session

    session = webhook_session(URL, "x-gitlab-token", "s3cret")
    assert session.headers["x-gitlab-token"] == "s3cret"
    assert "Authorization" not in session.headers and "X-Serverless-Authorization" not in session.headers
    with pytest.raises(RuntimeError, match="not an https://\\*.run.app"):
        webhook_session("https://evil.example.com", "x-gitlab-token", "s3cret")


def test_webhook_secret_is_dropped_on_a_cross_host_redirect() -> None:
    import requests

    from agentless.providers.cloud_run.clients import webhook_session

    session = webhook_session(URL, "x-gitlab-token", "s3cret")
    prepared = requests.Request("GET", "https://evil.example.com/", headers={"x-gitlab-token": "s3cret"}).prepare()
    response = requests.Response()
    response.request = requests.Request("GET", f"{URL}/run_sse").prepare()
    session.rebuild_auth(prepared, response)
    assert "x-gitlab-token" not in prepared.headers


def test_webhook_probe_sends_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from agentless.providers.cloud_run.clients import CloudRunClients

    seen: list[dict] = []

    class Response:
        def __init__(self, status: int) -> None:
            self.status_code = status

    answers = iter([503, 401, 403])  # a cold start, then the app's answers
    monkeypatch.setattr(requests, "get", lambda url, headers, **_: seen.append(headers) or Response(next(answers)))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    statuses = CloudRunClients.webhook_probe(object.__new__(CloudRunClients), URL, "x-gitlab-token")  # type: ignore[arg-type]
    assert statuses == [401, 403]
    assert seen[0] == {} and seen[1] == {}
    assert set(seen[2]) == {"x-gitlab-token"} and seen[2]["x-gitlab-token"].startswith("agentless-probe-")


def test_invoke_in_webhook_mode_sends_the_secret_not_a_google_token(fake_cli: Path, edit, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentless import cli

    def change(d: dict) -> None:
        _cloud_run(d)
        _webhook(d)

    edit(change)
    _scaffold_for_cloud_run(fake_cli)
    CliRunner().invoke(cli.app, ["deploy", "-c", str(fake_cli / "agent.yaml"), "--yes"])
    secrets: list[str] = []
    monkeypatch.setattr(cli, "read_secret", lambda _p, secret, version, _d: secrets.append(secret) or "s3cret")
    sent: dict = {}

    def fake_invoke(url, app, message, **kw):
        sent.update(kw, url=url)
        yield {"session_id": "s-1"}

    monkeypatch.setattr(cli.run_ops, "invoke", fake_invoke)
    result = CliRunner().invoke(cli.app, ["invoke", "-c", str(fake_cli / "agent.yaml"), "-m", "hi"])
    assert result.exit_code == 0, result.output
    assert secrets == ["gitlab-webhook-token"]
    assert sent["http"].headers["x-gitlab-token"] == "s3cret"
    assert "Authorization" not in sent["http"].headers


# --- tags: bound on the service, before it opens -------------------------------------------------------------------

INGRESS_TAG = "123/allowIngressAllForCloudRun/allow-ingress-all"
USERS_TAG = "123/allUsersIngress/True"


def _tags(*values: str) -> Callable[[dict], None]:
    def change(data: dict) -> None:
        data.setdefault("cloudRun", {})["tags"] = dict(v.rsplit("/", 1) for v in values)

    return change


def test_ingress_all_is_refused_without_its_tag(make_run, gcp: FakeGcp) -> None:
    gcp.ingress_all_tag = INGRESS_TAG  # the org policy the fake simulates
    with pytest.raises(RuntimeError, match="run.allowedIngress"):
        deploy(make_run())


def test_a_new_service_is_created_closed_tagged_then_opened(make_run, gcp: FakeGcp, edit) -> None:
    gcp.ingress_all_tag = INGRESS_TAG
    edit(_tags(INGRESS_TAG, USERS_TAG))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert f"+ tag {INGRESS_TAG} (bound before the service opens)" in service.details
    deploy(make_run())

    kinds = gcp.kinds()
    assert kinds.index("run_create") < kinds.index("run_tag") < kinds.index("run_update")
    created = next(c[2] for c in gcp.calls if c[0] == "run_create")
    assert created["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    assert created["invokerIamDisabled"] is False
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_ALL"
    assert set(gcp.run_service_tags(SERVICE)) == {INGRESS_TAG, USERS_TAG}
    assert actions(make_run())["service"] == Action.NOOP


def test_without_tags_the_service_is_created_in_one_call(make_run, gcp: FakeGcp) -> None:
    deploy(make_run())
    assert "run_tag" not in gcp.kinds()
    assert "run_update" not in gcp.kinds()


def test_a_tag_is_bound_before_the_update_that_needs_it(make_run, gcp: FakeGcp, edit) -> None:
    edit(lambda d: d.update(cloudRun={"ingress": "internal"}))
    deploy(make_run())
    gcp.ingress_all_tag = INGRESS_TAG
    edit(lambda d: d.update(cloudRun={"ingress": "all", "tags": {INGRESS_TAG.rsplit("/", 1)[0]: "allow-ingress-all"}}))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert f"+ tag {INGRESS_TAG}" in service.details
    deploy(make_run())

    kinds = gcp.kinds()
    assert kinds.index("run_tag") < len(kinds) - 1 - kinds[::-1].index("run_update")
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_ALL"


def test_a_dropped_tag_is_unbound_and_tags_bound_elsewhere_are_kept(make_run, gcp: FakeGcp, edit) -> None:
    edit(_tags(INGRESS_TAG, USERS_TAG))
    deploy(make_run())
    gcp.run_service_tag(SERVICE, "123/env/prod")  # bound outside agentless
    edit(_tags(INGRESS_TAG))
    _, changeset = make_run().plan(DeployOptions())
    service = next(c for c in changeset.changes if c.resource == "service")
    assert service.action == Action.UPDATE
    assert f"- tag {USERS_TAG}" in service.details
    before = len(gcp.calls)
    deploy(make_run())

    assert set(gcp.run_service_tags(SERVICE)) == {INGRESS_TAG, "123/env/prod"}
    assert "run_update" not in [c[0] for c in gcp.calls[before:]]  # no new revision for a tag
    assert actions(make_run())["service"] == Action.NOOP


def test_a_wanted_tag_already_bound_outside_agentless_is_never_removed(make_run, gcp: FakeGcp, edit) -> None:
    deploy(make_run())
    gcp.run_service_tag(SERVICE, INGRESS_TAG)
    edit(_tags(INGRESS_TAG))
    assert actions(make_run())["service"] == Action.NOOP
    edit(lambda d: d["cloudRun"].pop("tags"))
    assert actions(make_run())["service"] == Action.NOOP  # not agentless's: not unbound
    deploy(make_run())
    assert INGRESS_TAG in gcp.run_service_tags(SERVICE)


def test_a_failed_tag_binding_leaves_the_service_closed_and_the_rerun_finishes(make_run, gcp: FakeGcp, edit) -> None:
    gcp.ingress_all_tag = INGRESS_TAG
    edit(_tags(INGRESS_TAG))
    real = gcp.run_service_tag

    def denied(_name: str, _value: str) -> None:
        raise RuntimeError("403 resourcemanager.tagValueBindings.create denied")

    gcp.run_service_tag = denied  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="403"):
        deploy(make_run())
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY"  # never opened

    gcp.run_service_tag = real  # type: ignore[method-assign]
    deploy(make_run())
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_ALL"
    assert INGRESS_TAG in gcp.run_service_tags(SERVICE)


def test_a_recreated_service_gets_its_tags_back(make_run, gcp: FakeGcp, edit) -> None:
    gcp.ingress_all_tag = INGRESS_TAG
    edit(_tags(INGRESS_TAG))
    deploy(make_run())
    gcp.run_service_delete(SERVICE)
    deploy(make_run())
    assert INGRESS_TAG in gcp.run_service_tags(SERVICE)
    assert _service(gcp)["ingress"] == "INGRESS_TRAFFIC_ALL"


def test_webhook_with_tags_opens_after_tagging_and_probes(make_run, gcp: FakeGcp, edit) -> None:
    gcp.ingress_all_tag = INGRESS_TAG
    edit(lambda d: (_webhook(d), _tags(INGRESS_TAG)(d)))
    deploy(make_run())
    created = next(c[2] for c in gcp.calls if c[0] == "run_create")
    assert created["invokerIamDisabled"] is False
    assert _service(gcp)["invokerIamDisabled"] is False
    assert _all_users(gcp)
    assert gcp.kinds().index("run_tag") < gcp.kinds().index("webhook_probe")


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        ({"allowIngressAllForCloudRun": "allow-ingress-all"}, "must be <org or project id>/<key short name>"),
        ({"123/a/b": "x"}, "must be <org or project id>/<key short name>"),
        ({"123/allowIngressAllForCloudRun": "a/b"}, "must be a short name"),
    ],
)
def test_tags_are_validated(run_dir: Path, edit, tags: dict, message: str) -> None:
    edit(lambda d: d.update(cloudRun={"tags": tags}))
    with pytest.raises(ConfigError, match=message):
        _load(run_dir)


def test_a_new_tag_alone_makes_no_new_revision(make_run, gcp: FakeGcp, edit) -> None:
    deploy(make_run())
    edit(_tags(INGRESS_TAG))
    before = len(gcp.calls)
    deploy(make_run())
    kinds = [c[0] for c in gcp.calls[before:]]
    assert "run_tag" in kinds and "run_update" not in kinds


def test_without_tags_the_tag_bindings_are_never_listed(make_run, gcp: FakeGcp) -> None:
    def listed(_name: str) -> dict[str, str]:
        raise AssertionError("tag bindings listed without cloudRun.tags")

    gcp.run_service_tags = listed  # type: ignore[method-assign]
    deploy(make_run())
    assert actions(make_run())["service"] == Action.NOOP


class _ProbeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code, self.text = status, text


_IAM_403 = _ProbeResponse(403, "<h1>Error: Forbidden</h1> Your client does not have permission to get URL / ")


@pytest.mark.parametrize(
    ("answers", "statuses"),
    [
        ([_IAM_403, _IAM_403, _ProbeResponse(401), _ProbeResponse(403, '{"error": "unauthorized"}')], [401, 403]),
        ([_IAM_403] * 50, [0, 0]),  # the grant never reached the container: proves nothing, fail closed
    ],
)
def test_webhook_probe_waits_past_cloud_run_iam_refusals(
    monkeypatch: pytest.MonkeyPatch, answers: list, statuses: list[int]
) -> None:
    import requests

    from agentless.providers.cloud_run.clients import CloudRunClients

    replies = iter(answers)
    monkeypatch.setattr(requests, "get", lambda *_a, **_k: next(replies))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert CloudRunClients.webhook_probe(object.__new__(CloudRunClients), URL, "x-gitlab-token") == statuses  # type: ignore[arg-type]
