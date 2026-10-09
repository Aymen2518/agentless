import json

import pytest

from agentless.plan.model import Action, DeployOptions
from agentless.providers.agent_runtime.provider import DeployError
from agentless.state.store import LockedError

SA = "serviceAccount:sample-dev@proj-dev.iam.gserviceaccount.com"
YES = lambda _cs: True


def actions(changeset):
    return {c.resource: c.action for c in changeset.changes}


def deploy(make_provider, options=None, stage=None):
    return make_provider(stage).deploy(options or DeployOptions(), YES)


def change(changeset, resource):
    return next(c for c in changeset.changes if c.resource == resource)


def engine_updates(gcp):
    return [c for c in gcp.calls if c[0] == "engine_update"]


def test_first_deploy_creates_everything(make_provider, gcp, agent_dir):
    _, cs = make_provider().plan(DeployOptions())
    assert actions(cs) == {
        "serviceAccount": Action.CREATE,
        "agentIdentity": Action.NOOP,
        "buckets": Action.NOOP,
        "iam": Action.CREATE,
        "engine": Action.CREATE,
        "geminiEnterprise": Action.NOOP,
    }
    deploy(make_provider)

    assert gcp.kinds()[:3] == ["create_sa", "iam", "iam"]  # identity and grants exist before the engine
    create = next(c for c in gcp.calls if c[0] == "engine_create")[2]
    deployment = create["spec"]["deployment_spec"]
    assert create["spec"]["service_account"] == SA.split(":")[1]
    assert deployment["min_instances"] == 0 and deployment["max_instances"] == 5
    assert deployment["secret_env"] == [{"name": "TOKEN", "secret_ref": {"secret": "tok", "version": "latest"}}]
    env = {e["name"]: e["value"] for e in deployment["env"]}
    assert env["BUCKET"] == "bk-dev" and env["AGENT_VERSION"] == "0.3.0" and "APP_URL" not in env
    assert "source_code_spec" in create["spec"]
    assert create["labels"]["agentless-stage"] == "dev"

    # APP_URL needs the engine name, so a follow-up env-only update is issued.
    [follow_up] = engine_updates(gcp)
    assert follow_up[2]["update_mask"] == "spec.deployment_spec.env"

    assert gcp.policies[("secret", "tok")]["roles/secretmanager.secretAccessor"] == {SA}
    assert gcp.policies[("bucket", "bk-dev")]["roles/storage.objectViewer"] == {SA}
    metadata = json.loads((agent_dir / "deployment_metadata.json").read_text())
    assert metadata["remote_agent_runtime_id"] == follow_up[1]
    assert metadata["is_a2a"] is True


def test_second_deploy_is_a_noop(make_provider, gcp):
    deploy(make_provider)
    _, cs = make_provider().plan(DeployOptions())
    assert not cs.pending
    calls = len(gcp.calls)
    assert make_provider().deploy(DeployOptions(), lambda c: bool(c.pending)) is None
    assert len(gcp.calls) == calls


def test_env_change_updates_only_env_without_rebuild(make_provider, gcp, edit):
    deploy(make_provider)
    edit(lambda d: d["agent"]["environment"].update(BUCKET="other"))
    _, cs = make_provider().plan(DeployOptions())
    assert {c.resource for c in cs.pending} == {"engine"}
    assert any("env.BUCKET" in d for d in change(cs, "engine").details)
    deploy(make_provider)
    last = engine_updates(gcp)[-1][2]
    assert last["update_mask"] == "spec.deployment_spec.env"
    assert "source_code_spec" not in last["spec"]


def test_code_change_rebuilds_only_source(make_provider, gcp, agent_dir):
    deploy(make_provider)
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v2'\n")
    deploy(make_provider)
    last = engine_updates(gcp)[-1][2]
    assert last["update_mask"] == (
        "spec.source_code_spec.inline_source.source_archive,spec.source_code_spec.image_spec"
    )


def test_code_only_defers_config(make_provider, gcp, agent_dir, edit):
    deploy(make_provider)
    edit(lambda d: d["agent"]["runtime"].update(maxInstances=9))
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v3'\n")
    deploy(make_provider, DeployOptions(code_only=True))
    assert "max_instances" not in engine_updates(gcp)[-1][2]["update_mask"]
    _, cs = make_provider().plan(DeployOptions())
    engine = change(cs, "engine")
    assert engine.action == Action.UPDATE and engine.data["fields"] == ["max_instances"] and not engine.data["code"]


def test_iam_never_touches_foreign_bindings(make_provider, gcp, edit):
    gcp.policies[("project", "proj-dev")]["roles/viewer"].add("user:someone@example.com")
    gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"].add(SA)  # granted outside agentless
    deploy(make_provider)
    edit(lambda d: d["identity"]["roles"].update(project=["roles/bigquery.jobUser"]))
    _, cs = make_provider().plan(DeployOptions())
    assert actions(cs)["iam"] == Action.UPDATE
    deploy(make_provider)
    project = gcp.policies[("project", "proj-dev")]
    assert project["roles/aiplatform.user"] == {SA}, "binding agentless did not create must survive"
    assert project["roles/bigquery.jobUser"] == {SA}
    make_provider().remove(YES)
    assert project["roles/viewer"] == {"user:someone@example.com"}
    assert project["roles/aiplatform.user"] == {SA}
    assert project["roles/bigquery.jobUser"] == set()


def test_psc_change_is_blocked_without_allow_replace(make_provider, gcp, edit):
    deploy(make_provider)
    edit(
        lambda d: d.update(network={"pscInterface": {"networkAttachment": "projects/h/regions/r/networkAttachments/a"}})
    )
    _, cs = make_provider().plan(DeployOptions())
    assert change(cs, "engine").action == Action.REPLACE and cs.blocked
    with pytest.raises(DeployError, match="--allow-replace"):
        deploy(make_provider)
    old = next(iter(gcp.engines))
    deploy(make_provider, DeployOptions(allow_replace=True))
    assert ("engine_delete", old) in gcp.calls
    new = [c for c in gcp.calls if c[0] == "engine_create"][-1][2]
    assert new["spec"]["deployment_spec"]["psc_interface_config"]["network_attachment"].endswith("/a")


def test_adopts_engine_deployed_by_agents_cli(make_provider, gcp):
    existing = "projects/123456/locations/europe-west1/reasoningEngines/999"
    gcp.engines[existing] = {"display_name": "sample-agent-dev"}
    _, cs = make_provider().plan(DeployOptions())
    assert change(cs, "engine").action == Action.UPDATE and "adopting" in change(cs, "engine").details[0]
    deploy(make_provider)
    assert not [c for c in gcp.calls if c[0] == "engine_create"]
    assert engine_updates(gcp)[0][1] == existing


def test_renaming_service_account_revokes_before_deleting(make_provider, gcp, edit):
    deploy(make_provider)
    edit(lambda d: d["identity"]["serviceAccount"].update(name="sample2-${stage}"))
    _, cs = make_provider().plan(DeployOptions())
    assert actions(cs)["serviceAccount"] == Action.REPLACE
    gcp.calls.clear()
    deploy(make_provider)
    kinds = gcp.kinds()
    assert kinds.index("create_sa") < kinds.index("iam") < kinds.index("delete_sa")
    assert ("delete_sa", "sample-dev@proj-dev.iam.gserviceaccount.com") in gcp.calls
    assert gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"] == {
        "serviceAccount:sample2-dev@proj-dev.iam.gserviceaccount.com"
    }


def test_existing_service_account_is_kept_on_remove(make_provider, gcp, edit):
    gcp.service_accounts["shared@proj-dev.iam.gserviceaccount.com"] = {"email": "x", "display_name": "x"}
    edit(
        lambda d: d["identity"].update(
            serviceAccount={"create": False, "email": "shared@proj-dev.iam.gserviceaccount.com"}
        )
    )
    deploy(make_provider)
    make_provider().remove(YES)
    assert "delete_sa" not in gcp.kinds()
    assert not gcp.engines


def test_agent_identity_mints_principal_before_iam(make_provider, gcp, edit):
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    _, cs = make_provider().plan(DeployOptions())
    assert actions(cs)["agentIdentity"] == Action.CREATE
    assert "known after apply" in change(cs, "iam").summary
    deploy(make_provider)
    kinds = gcp.kinds()
    assert kinds.index("engine_create_identity") < kinds.index("iam") < kinds.index("engine_update")
    assert "engine_create" not in kinds
    [member] = gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"]
    assert member.startswith("principal://agents.global")
    first = engine_updates(gcp)[0][2]
    assert "spec.source_code_spec.inline_source.source_archive" in first["update_mask"]
    assert not make_provider().plan(DeployOptions())[1].pending


def test_platform_identity_grants_reasoning_engine_service_agent(make_provider, gcp, edit):
    edit(lambda d: d.pop("identity"))
    deploy(make_provider)
    expected = "serviceAccount:service-123456@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
    assert gcp.policies[("secret", "tok")]["roles/secretmanager.secretAccessor"] == {expected}


def test_memory_bank_and_publish(make_provider, gcp, edit):
    app = "projects/123456/locations/global/collections/default_collection/engines/ge-app"

    def add(d):
        d["memory"] = {
            "memoryBank": {"generationModel": "gemini-2.5-flash", "ttl": "86400s", "topics": ["USER_PREFERENCES"]}
        }
        d["publish"] = {
            "geminiEnterprise": {
                "app": app,
                "authorization": {
                    "id": "sample-oauth",
                    "clientId": "cid",
                    "clientSecret": "s3cret",
                    "scopes": ["openid", "email"],
                },
            }
        }

    edit(add)
    deploy(make_provider)
    create = next(c for c in gcp.calls if c[0] == "engine_create")[2]
    bank = create["context_spec"]["memory_bank_config"]
    assert bank["generation_config"]["model"].endswith(
        "/locations/europe-west1/publishers/google/models/gemini-2.5-flash"
    )
    assert bank["ttl_config"] == {"default_ttl": "86400s"}
    [agent] = gcp.ge_agents.values()
    assert agent["payload"]["authorization_config"]["tool_authorizations"] == [
        "projects/123456/locations/global/authorizations/sample-oauth"
    ]
    state_text = (make_provider().store._state).read_text()
    assert "s3cret" not in state_text

    assert not make_provider().plan(DeployOptions())[1].pending
    edit(lambda d: d["publish"]["geminiEnterprise"].update(displayName="Renamed"))
    deploy(make_provider)
    assert gcp.kinds()[-1] == "ge_patch"

    edit(lambda d: d.pop("publish"))
    deploy(make_provider)
    assert not gcp.ge_agents and not gcp.ge_authorizations


def test_remove_deletes_what_was_created(make_provider, gcp, agent_dir):
    deploy(make_provider)
    assert make_provider().remove(YES)
    assert not gcp.engines and not gcp.service_accounts
    assert make_provider().store.read() is None


def test_lock_prevents_concurrent_deploys(make_provider):
    provider = make_provider()
    with provider.store.lock("other"), pytest.raises(LockedError):
        deploy(make_provider)


def test_no_wait_then_status(make_provider, gcp):
    gcp.ops_done_immediately = False
    deploy(make_provider, DeployOptions(no_wait=True))
    _, cs = make_provider().plan(DeployOptions())
    assert cs.blocked and "--status" in cs.blocked[0].blocked
    assert make_provider().status() is False
    for op in gcp.operations.values():
        op["done"] = True
    assert make_provider().status() is True
    assert make_provider().store.read().resources["engine"]["spec"] is not None


def test_stages_are_isolated(make_provider, gcp):
    deploy(make_provider)
    gcp.project = "proj-prod"
    _, cs = make_provider("prod").plan(DeployOptions())
    assert actions(cs)["engine"] == Action.CREATE


def test_missing_iam_target_blocks_plan(make_provider, gcp):
    real = gcp.iam_members
    gcp.iam_members = lambda rtype, name: None if rtype == "bucket" else real(rtype, name)
    _, cs = make_provider().plan(DeployOptions())
    assert change(cs, "iam").blocked and "bucket/bk-dev" in change(cs, "iam").blocked


GE_APP = "projects/123456/locations/global/collections/default_collection/engines/ge-app"


def _publish(d, app=GE_APP, auth_id="sample-oauth"):
    ge = {"app": app}
    if auth_id:
        ge["authorization"] = {"id": auth_id, "clientId": "cid", "clientSecret": "s3cret", "scopes": ["openid"]}
    d["publish"] = {"geminiEnterprise": ge}


def test_switch_to_agent_identity_replaces_without_orphans(make_provider, gcp, edit):
    deploy(make_provider)
    old_engine = next(iter(gcp.engines))
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    with pytest.raises(DeployError, match="--allow-replace"):
        deploy(make_provider)
    deploy(make_provider, DeployOptions(allow_replace=True))
    assert old_engine not in gcp.engines
    [(name, engine)] = gcp.engines.items()
    state = make_provider().store.read().resources["engine"]
    assert state["name"] == name and "previous" not in state and state["spec"] is not None
    assert gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"] == {
        f"principal://{engine['effective_identity']}"
    }
    assert not gcp.service_accounts
    assert not make_provider().plan(DeployOptions())[1].pending


def test_identity_shell_keeps_cmek_out_of_its_first_update(make_provider, gcp, edit):
    key = "projects/kms-p/locations/europe-west1/keyRings/r/cryptoKeys/k"
    edit(lambda d: d["agent"].update(encryption={"kmsKey": key}))
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    deploy(make_provider)
    [minted] = [c for c in gcp.calls if c[0] == "engine_create_identity"]
    assert minted[2] == {"kms_key_name": key}
    [update] = [c for c in gcp.calls if c[0] == "engine_update"]
    masks = update[2]["update_mask"].split(",")
    assert "encryption_spec" not in masks and "encryption_spec" not in update[2]


def test_switch_to_agent_identity_resumes_after_failed_update(make_provider, gcp, edit, monkeypatch):
    # The 0.2.2 failure: old engine deleted, then the shell's first update is rejected. A re-run must finish.
    deploy(make_provider)
    old_engine = next(iter(gcp.engines))
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    real_update = gcp.engine_update

    def reject(name, config):
        raise RuntimeError("400 INVALID_ARGUMENT: Cannot update encryption_spec in ReasoningEngine.")

    monkeypatch.setattr(gcp, "engine_update", reject)
    with pytest.raises(RuntimeError, match="encryption_spec"):
        deploy(make_provider, DeployOptions(allow_replace=True))
    assert old_engine not in gcp.engines
    monkeypatch.setattr(gcp, "engine_update", real_update)
    deploy(make_provider, DeployOptions(allow_replace=True))
    [(name, engine)] = gcp.engines.items()
    state = make_provider().store.read().resources["engine"]
    assert state["name"] == name and state["spec"] is not None and "previous" not in state
    assert gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"] == {
        f"principal://{engine['effective_identity']}"
    }
    assert not make_provider().plan(DeployOptions())[1].pending


def test_switch_to_agent_identity_ignores_sa_email_in_state(make_provider, gcp, edit):
    # State written before agent_principal(): the SA engine's effectiveIdentity (its SA email) was recorded.
    deploy(make_provider)
    store = make_provider().store
    state = store.read()
    state.resources["engine"]["effectiveIdentity"] = "sample-dev@proj-dev.iam.gserviceaccount.com"
    store.write(state)
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    assert actions(make_provider().plan(DeployOptions(allow_replace=True))[1])["agentIdentity"] == Action.CREATE
    deploy(make_provider, DeployOptions(allow_replace=True))
    [engine] = gcp.engines.values()
    assert gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"] == {
        f"principal://{engine['effective_identity']}"
    }


def test_vanished_identity_engine_is_recreated_and_iam_follows(make_provider, gcp, edit):
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    deploy(make_provider)
    gcp.engines.clear()
    deploy(make_provider)
    [engine] = gcp.engines.values()
    assert gcp.policies[("project", "proj-dev")]["roles/aiplatform.user"] == {
        f"principal://{engine['effective_identity']}"
    }
    assert not make_provider().plan(DeployOptions())[1].pending


def test_publish_follows_recreated_engine(make_provider, gcp, edit):
    edit(_publish)
    deploy(make_provider)
    gcp.engines.clear()
    deploy(make_provider)
    [agent] = gcp.ge_agents.values()
    assert agent["engine"] == next(iter(gcp.engines))


def test_moving_publish_app_keeps_shared_authorization(make_provider, gcp, edit):
    edit(_publish)
    deploy(make_provider)
    edit(lambda d: _publish(d, app=GE_APP.replace("ge-app", "ge-app-2")))
    deploy(make_provider)
    assert list(gcp.ge_authorizations) == ["projects/123456/locations/global/authorizations/sample-oauth"]
    [agent_name] = gcp.ge_agents
    assert "ge-app-2" in agent_name


def test_dropping_authorization_deletes_it(make_provider, gcp, edit):
    edit(_publish)
    deploy(make_provider)
    edit(lambda d: _publish(d, auth_id=None))
    deploy(make_provider)
    assert not gcp.ge_authorizations
    [agent] = gcp.ge_agents.values()
    assert "authorization_config" not in agent["payload"]


def test_partial_iam_failure_keeps_track_of_earlier_grants(make_provider, gcp):
    real = gcp.iam_modify

    def flaky(rtype, name, add, remove):
        if rtype == "secret":
            raise RuntimeError("boom")
        real(rtype, name, add, remove)

    gcp.iam_modify = flaky
    with pytest.raises(RuntimeError, match="boom"):
        deploy(make_provider)
    owned = {tuple(b) for b in make_provider().store.read().resources["iam"]["bindings"]}
    assert ("bucket", "bk-dev", "roles/storage.objectViewer", SA) in owned
    gcp.iam_modify = real
    deploy(make_provider)
    assert gcp.policies[("secret", "tok")]["roles/secretmanager.secretAccessor"] == {SA}


def test_secret_values_never_reach_plan_or_state(make_provider, gcp, edit, monkeypatch):
    monkeypatch.setattr("agentless.config.sources._access_secret", lambda name, deployer: "hunter2")
    edit(lambda d: d["agent"]["environment"].update(API_KEY="${secret:api-key}"))
    _, cs = make_provider().plan(DeployOptions())
    deploy(make_provider)
    edit(lambda d: d["agent"]["environment"].update(BUCKET="changed"))
    _, cs = make_provider().plan(DeployOptions())
    text = "\n".join(line for c in cs.changes for line in c.details)
    assert "hunter2" not in text
    assert "hunter2" not in make_provider().store._state.read_text()
    create = next(c for c in gcp.calls if c[0] == "engine_create")[2]
    assert {"name": "API_KEY", "value": "hunter2"} in create["spec"]["deployment_spec"]["env"]


def test_agent_identity_first_deploy_sends_psc(make_provider, gcp, edit):
    def change(d):
        d["identity"] = {"type": "agentIdentity"}
        d["network"] = {"pscInterface": {"networkAttachment": "projects/h/regions/r/networkAttachments/a"}}

    edit(change)
    deploy(make_provider)
    first = engine_updates(gcp)[0][2]
    assert "spec.deployment_spec.psc_interface_config" in first["update_mask"]
    assert first["spec"]["deployment_spec"]["psc_interface_config"]["network_attachment"].endswith("/a")


def test_code_only_defers_identity_changes(make_provider, gcp, edit, agent_dir):
    deploy(make_provider)
    edit(lambda d: d["identity"]["serviceAccount"].update(name="sample2-${stage}"))
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v9'\n")
    _, cs = make_provider().plan(DeployOptions(code_only=True))
    assert actions(cs)["serviceAccount"] == Action.NOOP and actions(cs)["iam"] == Action.NOOP
    deploy(make_provider, DeployOptions(code_only=True))
    assert "delete_sa" not in gcp.kinds() and "sample-dev@proj-dev.iam.gserviceaccount.com" in gcp.service_accounts


def test_unsetting_server_mode_resets_it(make_provider, gcp, edit):
    edit(lambda d: d["agent"]["runtime"].update(serverMode="EXPERIMENTAL"))
    deploy(make_provider)
    edit(lambda d: d["agent"]["runtime"].pop("serverMode"))
    deploy(make_provider)
    last = engine_updates(gcp)[-1][2]
    assert last["update_mask"] == "spec.deployment_spec.agent_server_mode"
    assert "agent_server_mode" not in last["spec"]["deployment_spec"]


def test_partial_remove_keeps_state(make_provider, gcp):
    deploy(make_provider)

    def broken(*_a):
        raise RuntimeError("denied")

    gcp.iam_modify = broken
    with pytest.raises(DeployError, match="partial remove"):
        make_provider().remove(YES)
    assert make_provider().store.read().resources["iam"]["bindings"]
