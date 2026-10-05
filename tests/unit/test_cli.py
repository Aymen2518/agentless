import json

import pytest
from typer.testing import CliRunner

from agentless import cli
from agentless.config.loader import load_project
from agentless.hooks import plugin_manager
from agentless.providers.agent_runtime import spec as engine_spec

runner = CliRunner()


def test_init_from_manifest_produces_valid_config(agent_dir):
    (agent_dir / "agent.yaml").unlink()
    result = runner.invoke(cli.app, ["init", str(agent_dir), "--project", "llm-dev"])
    assert result.exit_code == 0, result.output
    project = load_project(agent_dir / "agent.yaml")
    assert project.config.service == "sample-agent"
    assert project.config.identity.service_account.name == "sample-agent-dev"
    assert runner.invoke(cli.app, ["init", str(agent_dir)]).exit_code == 1


def test_validate_reports_errors(agent_dir):
    (agent_dir / "agent.yaml").write_text("service: Bad_Name\nprovider: {project: p, region: global}\n")
    result = runner.invoke(cli.app, ["validate", "-c", str(agent_dir / "agent.yaml")])
    assert result.exit_code == 1
    assert "service" in result.output and "provider.region" in result.output


def test_schema_has_camel_case_properties():
    result = runner.invoke(cli.app, ["schema"])
    schema = json.loads(result.output)
    assert "stagingBucket" in schema["$defs"]["Provider"]["properties"]


def test_plan_deploy_info_remove(fake_cli):
    cfg = ["-c", str(fake_cli / "agent.yaml")]
    plan = runner.invoke(cli.app, ["plan", *cfg, "--detailed-exitcode"])
    assert plan.exit_code == 2 and "3 to create" in plan.output
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y"]).exit_code == 0
    assert runner.invoke(cli.app, ["plan", *cfg, "--detailed-exitcode"]).exit_code == 0
    info = json.loads(runner.invoke(cli.app, ["info", *cfg, "--json"]).output)
    assert info["engine"].startswith("projects/123456/locations/europe-west1/reasoningEngines/")
    assert info["serviceAccount"] == "sample-dev@proj-dev.iam.gserviceaccount.com"
    assert runner.invoke(cli.app, ["remove", *cfg]).exit_code == 1  # non-interactive without --yes
    assert runner.invoke(cli.app, ["remove", *cfg, "--yes"]).exit_code == 0


def test_print_masks_secret_values(agent_dir, monkeypatch):
    monkeypatch.setattr("agentless.config.sources._access_secret", lambda name, deployer: "hunter2")
    path = agent_dir / "agent.yaml"
    path.write_text(path.read_text() + "custom: { token: '${secret:api-token}' }\n")
    result = runner.invoke(cli.app, ["print", "-c", str(path)])
    assert result.exit_code == 0 and "hunter2" not in result.output and "********" in result.output


def test_removing_all_secrets_clears_secret_env(agent_dir):
    project = load_project(agent_dir / "agent.yaml")
    before = engine_spec.desired_spec(project, service_account="sa@x", engine_name=None)
    after = {**before, "secret_env": {}}
    payload, masks = engine_spec.api_payload(after, list(engine_spec.diff(before, after)))
    assert payload == {"spec": {"deployment_spec": {"secret_env": []}}}
    assert masks == ["spec.deployment_spec.secret_env"]


def test_plugin_hooks_can_veto(fake_cli, gcp):
    from agentless.hooks import hookimpl

    class Policy:
        @hookimpl
        def agentless_after_load(self, project):
            if "owner" not in project.config.provider.labels:
                raise cli.ConfigError("label 'owner' is mandatory")

    project = load_project(fake_cli / "agent.yaml")
    pm = plugin_manager([Policy()])
    with pytest.raises(cli.ConfigError, match="owner"):
        pm.hook.agentless_after_load(project=project)
