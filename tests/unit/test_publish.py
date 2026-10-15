"""Agent Registry, A2A registration in Gemini Enterprise, sharing, app gateway and grant-only IAM."""

import json

import pytest

from agentless.plan.model import Action, DeployOptions
from agentless.providers.agent_runtime.provider import DeployError

YES = lambda _cs: True
GE_APP = "projects/987654/locations/global/collections/default_collection/engines/ge-app"
GATEWAY = "projects/proj-dev/locations/europe-west1/agentGateways/gw"
REGISTRY = "projects/proj-dev/locations/europe-west1/services/sample-agent-dev"
GE_SA = "serviceAccount:service-987654@gcp-sa-discoveryengine.iam.gserviceaccount.com"
INVOKER = "roles/aiplatform.user"


def deploy(make_provider, options=None):
    return make_provider().deploy(options or DeployOptions(), YES)


def plan(make_provider):
    return {c.resource: c for c in make_provider().plan(DeployOptions())[1].changes}


def registry(d, **kw):
    d.setdefault("publish", {})["agentRegistry"] = kw


def gemini(d, **kw):
    d.setdefault("publish", {})["geminiEnterprise"] = {"app": GE_APP, **kw}


def engine_name(gcp):
    return next(iter(gcp.engines))


def finish_operations(make_provider, gcp):
    for op in gcp.operations.values():
        op["done"] = True
    assert make_provider().status()
    gcp.ops_done_immediately = True


def bump_card(gcp, agent_dir):
    """New code whose card differs, as a redeploy would serve."""
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v2'\n")
    gcp.engines[engine_name(gcp)]["card_version"] = 2


# --- Agent Registry ------------------------------------------------------------------------------------------------


def test_registry_registers_card_and_refreshes_it_after_a_code_deploy(make_provider, gcp, edit, agent_dir):
    edit(lambda d: registry(d))
    assert plan(make_provider)["agentRegistry"].action == Action.CREATE
    deploy(make_provider)
    body = gcp.registry[REGISTRY]
    assert body["agentSpec"]["type"] == "A2A_AGENT_CARD"
    assert body["agentSpec"]["content"]["url"].endswith(f"{engine_name(gcp)}/api/a2a/app")
    assert body["displayName"] == "sample-agent"
    assert plan(make_provider)["agentRegistry"].action == Action.NOOP

    # The card changes with the code: the same deploy pushes it, no second deploy needed.
    gcp.engines[engine_name(gcp)]["card_version"] = 2
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v2'\n")
    deploy(make_provider)
    assert gcp.registry[REGISTRY]["agentSpec"]["content"]["version"] == "2"
    assert gcp.kinds().count("registry_patch") == 1
    assert plan(make_provider)["agentRegistry"].action == Action.NOOP


def test_registry_detects_card_drift_on_plan(make_provider, gcp, edit):
    edit(lambda d: registry(d, displayName="Support"))
    deploy(make_provider)
    gcp.engines[engine_name(gcp)]["card_version"] = 3  # changed outside this deploy
    change = plan(make_provider)["agentRegistry"]
    assert change.action == Action.UPDATE
    deploy(make_provider)
    assert gcp.registry[REGISTRY]["agentSpec"]["content"]["version"] == "3"
    assert gcp.registry[REGISTRY]["displayName"] == "Support"


def test_registry_needs_an_a2a_agent(make_provider, edit, agent_dir):
    manifest = agent_dir / "agents-cli-manifest.yaml"
    manifest.write_text(manifest.read_text().replace("is_a2a: true", "is_a2a: false"))
    edit(lambda d: registry(d))
    assert "A2A" in (plan(make_provider)["agentRegistry"].blocked or "")
    with pytest.raises(DeployError, match="A2A"):
        deploy(make_provider)


def test_registry_rename_registers_new_before_deleting_old(make_provider, gcp, edit):
    edit(lambda d: registry(d))
    deploy(make_provider)
    edit(lambda d: registry(d, serviceId="support", location="europe-west4"))
    assert plan(make_provider)["agentRegistry"].action == Action.REPLACE
    deploy(make_provider)
    new = "projects/proj-dev/locations/europe-west4/services/support"
    assert list(gcp.registry) == [new]
    kinds = [c for c in gcp.calls if c[0].startswith("registry_")]
    assert kinds[-2:] == [("registry_create", new), ("registry_delete", REGISTRY)]
    assert make_provider().remove(YES)
    assert not gcp.registry


def test_dropping_registry_unregisters(make_provider, gcp, edit):
    edit(lambda d: registry(d))
    deploy(make_provider)
    edit(lambda d: d.pop("publish"))
    assert plan(make_provider)["agentRegistry"].action == Action.DELETE
    deploy(make_provider)
    assert not gcp.registry
    assert "agentRegistry" not in make_provider().store.read().resources


def test_registry_waits_for_a_no_wait_engine(make_provider, gcp, edit):
    gcp.ops_done_immediately = False
    edit(lambda d: registry(d))
    deploy(make_provider, DeployOptions(no_wait=True))
    assert not gcp.registry
    finish_operations(make_provider, gcp)
    assert plan(make_provider)["agentRegistry"].action == Action.CREATE
    deploy(make_provider)
    assert list(gcp.registry) == [REGISTRY]


def test_registry_rename_with_engine_pending_keeps_old_service(make_provider, gcp, edit, agent_dir):
    edit(lambda d: registry(d))
    deploy(make_provider)
    gcp.ops_done_immediately = False
    edit(lambda d: registry(d, serviceId="support"))
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v2'\n")
    deploy(make_provider, DeployOptions(no_wait=True))
    assert list(gcp.registry) == [REGISTRY], "old service stays until its replacement exists"
    finish_operations(make_provider, gcp)
    deploy(make_provider)
    assert list(gcp.registry) == ["projects/proj-dev/locations/europe-west1/services/support"]


def test_registry_create_failure_converges_on_rerun(make_provider, gcp, edit):
    edit(lambda d: registry(d))
    real = gcp.registry_create

    def lost_response(parent, service_id, body):
        real(parent, service_id, body)  # created server-side, but the client sees an error
        raise RuntimeError("503 backend error")

    gcp.registry_create = lost_response
    with pytest.raises(RuntimeError, match="503"):
        deploy(make_provider)
    gcp.registry_create = real
    deploy(make_provider)
    assert list(gcp.registry) == [REGISTRY]
    assert gcp.kinds()[-1] == "registry_patch"
    assert plan(make_provider)["agentRegistry"].action == Action.NOOP


# --- Gemini Enterprise: A2A registration, sharing, authorization ----------------------------------------------------


def _auth(**kw):
    return {"id": "sample-oauth", "clientId": "cid", "clientSecret": "s3cret", "scopes": ["openid"], **kw}


def test_a2a_registration_carries_card_and_agent_authorization(make_provider, gcp, edit):
    edit(lambda d: gemini(d, registration="a2a", sharing="allUsers", authorization=_auth(displayName="Sample")))
    deploy(make_provider)
    [agent] = gcp.ge_agents.values()
    payload = agent["payload"]
    card = json.loads(payload["a2a_agent_definition"]["json_agent_card"])
    assert card["url"].endswith("/api/a2a/app")
    assert "adk_agent_definition" not in payload
    assert payload["authorization_config"] == {
        "agent_authorization": "projects/987654/locations/global/authorizations/sample-oauth"
    }
    assert payload["sharing_config"] == {"scope": "ALL_USERS"}
    [oauth] = gcp.ge_authorizations.values()
    assert oauth["display_name"] == "Sample" and oauth["scopes"] == ["openid"]
    state = make_provider().store._state.read_text()
    assert "s3cret" not in state and '"registration": "a2a"' in state
    assert plan(make_provider)["geminiEnterprise"].action == Action.NOOP


def test_a2a_registration_is_refreshed_when_the_card_changes(make_provider, gcp, edit, agent_dir):
    edit(lambda d: gemini(d, registration="a2a"))
    deploy(make_provider)
    bump_card(gcp, agent_dir)
    deploy(make_provider)
    [agent] = gcp.ge_agents.values()
    assert json.loads(agent["payload"]["a2a_agent_definition"]["json_agent_card"])["version"] == "2"
    assert gcp.kinds().count("ge_create") == 1


def test_adk_registration_is_not_patched_by_a_code_deploy(make_provider, gcp, edit, agent_dir):
    edit(lambda d: gemini(d))
    deploy(make_provider)
    bump_card(gcp, agent_dir)
    deploy(make_provider)
    assert "ge_patch" not in gcp.kinds()


def test_a2a_registration_adopts_an_existing_one_by_card_url(make_provider, gcp, edit):
    deploy(make_provider)
    card_url = f"https://europe-west1-aiplatform.googleapis.com/reasoningEngines/v1/{engine_name(gcp)}/api/a2a/app"
    existing = f"{GE_APP}/assistants/default_assistant/agents/published-by-agents-cli"
    gcp.ge_agents[existing] = {"card_url": card_url, "payload": {}}
    edit(lambda d: gemini(d, registration="a2a"))
    deploy(make_provider)
    assert list(gcp.ge_agents) == [existing]
    assert gcp.kinds()[-1] == "ge_patch"


def test_switching_registration_replaces_it(make_provider, gcp, edit):
    edit(lambda d: gemini(d, authorization=_auth()))
    deploy(make_provider)
    [old] = gcp.ge_agents
    edit(lambda d: gemini(d, registration="a2a", authorization=_auth()))
    change = plan(make_provider)["geminiEnterprise"]
    assert change.action == Action.REPLACE and "~ registration: adk → a2a" in change.details
    deploy(make_provider)
    [(new, agent)] = gcp.ge_agents.items()
    assert new != old and "a2a_agent_definition" in agent["payload"]
    assert list(gcp.ge_authorizations) == ["projects/987654/locations/global/authorizations/sample-oauth"]


def test_switch_to_a2a_with_engine_pending_keeps_old_registration(make_provider, gcp, edit, agent_dir):
    edit(lambda d: gemini(d))
    deploy(make_provider)
    [old] = gcp.ge_agents
    gcp.ops_done_immediately = False
    edit(lambda d: gemini(d, registration="a2a"))
    (agent_dir / "app" / "agent.py").write_text("root_agent = 'v2'\n")
    deploy(make_provider, DeployOptions(no_wait=True))
    assert list(gcp.ge_agents) == [old]
    finish_operations(make_provider, gcp)
    deploy(make_provider)
    [(new, agent)] = gcp.ge_agents.items()
    assert new != old and "a2a_agent_definition" in agent["payload"]


def test_a2a_registration_needs_an_a2a_agent(make_provider, edit, agent_dir):
    manifest = agent_dir / "agents-cli-manifest.yaml"
    manifest.write_text(manifest.read_text().replace("is_a2a: true", "is_a2a: false"))
    edit(lambda d: gemini(d, registration="a2a"))
    assert "A2A" in (plan(make_provider)["geminiEnterprise"].blocked or "")


# --- Gemini Enterprise app gateway -----------------------------------------------------------------------------------


def test_gateway_is_set_once_and_never_reverted(make_provider, gcp, edit):
    edit(lambda d: gemini(d, agentGateway=GATEWAY))
    change = plan(make_provider)["geminiEnterprise"]
    assert any("app egress gateway" in line for line in change.details)
    deploy(make_provider)
    assert gcp.ge_gateways[GE_APP] == GATEWAY
    assert plan(make_provider)["geminiEnterprise"].action == Action.NOOP
    assert make_provider().info()["agentGateway"] == GATEWAY
    assert make_provider().remove(YES)
    assert gcp.ge_gateways[GE_APP] == GATEWAY
    assert [c for c in gcp.calls if c[0] == "ge_gateway"] == [("ge_gateway", GE_APP, GATEWAY)]


def test_gateway_set_outside_agentless_is_left_alone(make_provider, gcp, edit):
    other = "projects/proj-dev/locations/europe-west1/agentGateways/other"
    gcp.ge_gateways[GE_APP] = other
    edit(lambda d: gemini(d, agentGateway=GATEWAY))
    change = plan(make_provider)["geminiEnterprise"]
    assert not change.blocked and any("already routes" in line for line in change.details)
    deploy(make_provider)
    assert gcp.ge_gateways[GE_APP] == other and "ge_gateway" not in gcp.kinds()


def test_gateway_added_later_is_an_update(make_provider, gcp, edit):
    edit(lambda d: gemini(d))
    deploy(make_provider)
    edit(lambda d: gemini(d, agentGateway=GATEWAY))
    assert plan(make_provider)["geminiEnterprise"].action == Action.UPDATE
    deploy(make_provider)
    assert gcp.ge_gateways[GE_APP] == GATEWAY


# --- grant-only IAM: invokers and the Gemini Enterprise service agent ---------------------------------------------


def test_invokers_and_service_agent_are_granted_and_never_revoked(make_provider, gcp, edit):
    edit(lambda d: (gemini(d), d.update(access={"invokers": ["group:users@example.com"]})))
    details = plan(make_provider)["iam"].details
    assert any("group:users@example.com (grant-only" in line for line in details)
    deploy(make_provider)
    project = gcp.policies[("project", "proj-dev")]
    assert {"group:users@example.com", GE_SA} <= project[INVOKER]
    owned = {tuple(b)[3] for b in make_provider().store.read().resources["iam"]["bindings"]}
    assert "group:users@example.com" not in owned and GE_SA not in owned

    edit(lambda d: (d.pop("access"), d.pop("publish")))
    deploy(make_provider)
    assert make_provider().remove(YES)
    assert {"group:users@example.com", GE_SA} <= project[INVOKER]


def test_present_grant_only_binding_makes_no_iam_call(make_provider, gcp, edit):
    deploy(make_provider)
    gcp.policies[("project", "proj-dev")][INVOKER].add("group:users@example.com")
    edit(lambda d: d.update(access={"invokers": ["group:users@example.com"]}))
    assert plan(make_provider)["iam"].action == Action.NOOP


def test_owned_binding_that_becomes_grant_only_is_kept(make_provider, gcp, edit):
    member = "group:users@example.com"
    deploy(make_provider)
    # A runtime member can't be an invoker in practice, so seed an owned binding directly.
    state = make_provider().store.read()
    state.resources["iam"]["bindings"].append(["project", "proj-dev", INVOKER, member])
    make_provider().store.write(state)
    gcp.policies[("project", "proj-dev")][INVOKER].add(member)
    edit(lambda d: d.update(access={"invokers": [member]}))
    deploy(make_provider)
    assert member in gcp.policies[("project", "proj-dev")][INVOKER]


def test_service_agent_opt_out_and_project_id_app(make_provider, gcp, edit):
    edit(lambda d: gemini(d, grantServiceAgent=False))
    deploy(make_provider)
    assert not any("discoveryengine" in m for m in gcp.policies[("project", "proj-dev")][INVOKER])

    # An app addressed by the target project's id still resolves to its number.
    edit(lambda d: gemini(d, app=GE_APP.replace("987654", "proj-dev")))
    deploy(make_provider)
    expected = "serviceAccount:service-123456@gcp-sa-discoveryengine.iam.gserviceaccount.com"
    assert expected in gcp.policies[("project", "proj-dev")][INVOKER]

    edit(lambda d: gemini(d, app=GE_APP.replace("987654", "other-proj")))
    assert any("cannot derive" in line for line in plan(make_provider)["iam"].details)


def test_invalid_invoker_is_rejected(make_provider, edit):
    edit(lambda d: d.update(access={"invokers": ["users@example.com"]}))
    with pytest.raises(Exception, match="invalid IAM member"):
        make_provider()


def test_info_card_url_uses_the_api_passthrough(make_provider, gcp, edit):
    edit(lambda d: registry(d))
    deploy(make_provider)
    info = make_provider().info()
    assert info["a2aCardUrl"].endswith(f"{engine_name(gcp)}/api/a2a/app/.well-known/agent-card.json")
    assert info["agentRegistry"] == REGISTRY


# --- regressions from review -----------------------------------------------------------------------------------------


def test_display_name_change_reaches_the_registration(make_provider, gcp, edit):
    edit(lambda d: gemini(d, displayName="Before"))
    deploy(make_provider)
    edit(lambda d: gemini(d, displayName="After", sharing="allUsers"))
    deploy(make_provider)
    [agent] = gcp.ge_agents.values()
    assert agent["payload"]["displayName"] == "After"
    assert agent["payload"]["sharing_config"] == {"scope": "ALL_USERS"}


def test_ge_move_crash_before_cleanup_is_finished_next_run(make_provider, gcp, edit, monkeypatch):
    from agentless.providers.agent_runtime.resources import GeminiEnterpriseResource

    edit(lambda d: gemini(d))
    deploy(make_provider)
    [old] = gcp.ge_agents
    edit(lambda d: gemini(d, app=GE_APP.replace("ge-app", "ge-app-2")))
    real = GeminiEnterpriseResource.cleanup
    monkeypatch.setattr(GeminiEnterpriseResource, "cleanup", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        deploy(make_provider)
    monkeypatch.setattr(GeminiEnterpriseResource, "cleanup", real)
    assert old in gcp.ge_agents and len(gcp.ge_agents) == 2
    change = plan(make_provider)["geminiEnterprise"]
    assert change.action == Action.UPDATE and change.data.get("cleanup")
    deploy(make_provider)
    [remaining] = gcp.ge_agents
    assert remaining != old and "ge-app-2" in remaining
    assert plan(make_provider)["geminiEnterprise"].action == Action.NOOP


def test_registry_rename_crash_before_cleanup_is_finished_next_run(make_provider, gcp, edit, monkeypatch):
    from agentless.providers.agent_runtime.resources import AgentRegistryResource

    edit(lambda d: registry(d))
    deploy(make_provider)
    edit(lambda d: registry(d, serviceId="support"))
    real = AgentRegistryResource.cleanup
    monkeypatch.setattr(AgentRegistryResource, "cleanup", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        deploy(make_provider)
    monkeypatch.setattr(AgentRegistryResource, "cleanup", real)
    assert len(gcp.registry) == 2
    deploy(make_provider)
    assert list(gcp.registry) == ["projects/proj-dev/locations/europe-west1/services/support"]


def test_refresh_with_empty_plan_data_still_replaces_properly(make_provider, gcp, edit, agent_dir):
    """--code-only defers a pending switch; a refresh in that run must not patch the old registration in place."""
    edit(lambda d: (gemini(d), registry(d)))
    deploy(make_provider)
    [old] = gcp.ge_agents
    edit(lambda d: (gemini(d, registration="a2a"), registry(d, serviceId="support")))
    gcp.engines.clear()  # engine recreated in this run, which triggers the refresh
    deploy(make_provider, DeployOptions(code_only=True))
    assert old not in gcp.ge_agents or gcp.ge_agents[old].get("engine"), "adk agent must not receive an a2a card"
    deploy(make_provider)
    [(name, agent)] = gcp.ge_agents.items()
    assert name != old and "a2a_agent_definition" in agent["payload"]
    assert list(gcp.registry) == ["projects/proj-dev/locations/europe-west1/services/support"]
