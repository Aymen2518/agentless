import json

import pytest

from agentless.config.loader import ConfigError, load_project
from agentless.package.packager import PackageError, collect


def test_loads_fixture_with_stage_params(agent_dir):
    project = load_project(agent_dir / "agent.yaml", stage="prod")
    cfg = project.config
    assert cfg.provider.project == "proj-prod"
    assert cfg.agent.runtime.min_instances == 2
    assert cfg.agent.environment == {"BUCKET": "bk-prod", "DEBUG": "false"}
    assert cfg.identity.service_account.name == "sample-prod"
    assert project.agent_directory == "app"


def test_stage_defaults_from_provider_stage(agent_dir):
    assert load_project(agent_dir / "agent.yaml").stage == "dev"


def test_undeclared_stage_rejected(agent_dir):
    with pytest.raises(ConfigError, match="stage 'qa' is not declared"):
        load_project(agent_dir / "agent.yaml", stage="qa")


def test_validation_error_uses_yaml_paths(agent_dir, edit):
    edit(lambda d: d["agent"]["runtime"].update(minInstances=50))
    with pytest.raises(ConfigError, match=r"agent\.runtime\.minInstances"):
        load_project(agent_dir / "agent.yaml")


def test_reserved_env_rejected(agent_dir, edit):
    edit(lambda d: d["agent"]["environment"].update(GOOGLE_CLOUD_PROJECT="x"))
    with pytest.raises(ConfigError, match="reserved"):
        load_project(agent_dir / "agent.yaml")


def test_sa_required_for_service_account_identity(agent_dir, edit):
    edit(lambda d: d["identity"].pop("serviceAccount"))
    with pytest.raises(ConfigError, match="identity: Value error, identity.serviceAccount is required"):
        load_project(agent_dir / "agent.yaml")


def test_missing_dockerfile(agent_dir):
    (agent_dir / "Dockerfile").unlink()
    with pytest.raises(ConfigError, match="Dockerfile"):
        load_project(agent_dir / "agent.yaml")


def test_non_agent_runtime_target_rejected(agent_dir):
    manifest = agent_dir / "agents-cli-manifest.yaml"
    manifest.write_text(manifest.read_text().replace("agent_runtime", "cloud_run"))
    with pytest.raises(ConfigError, match="cloud_run"):
        load_project(agent_dir / "agent.yaml")


def test_package_honours_ignore_rules_and_excludes(agent_dir):
    (agent_dir / ".venv").mkdir()
    (agent_dir / ".venv" / "x").write_text("x")
    (agent_dir / "app" / "key.secret").write_text("s")
    pkg = collect(agent_dir, ("agent.yaml",))
    assert "agent.yaml" not in pkg.files
    assert not any(f.startswith(".venv") or f.endswith(".secret") for f in pkg.files)
    assert "app/agent.py" in pkg.files


def test_gcloudignore_takes_precedence(agent_dir):
    (agent_dir / ".gcloudignore").write_text("app/\n")
    pkg = collect(agent_dir)
    assert not any(f.startswith("app/") for f in pkg.files)


def test_hash_is_stable_and_ignores_metadata(agent_dir):
    before = collect(agent_dir).sha256
    (agent_dir / "deployment_metadata.json").write_text(json.dumps({"x": 1}))
    assert collect(agent_dir).sha256 == before
    (agent_dir / "app" / "agent.py").write_text("root_agent = 1\n")
    assert collect(agent_dir).sha256 != before


def test_ignored_dockerfile_fails(agent_dir):
    (agent_dir / ".gcloudignore").write_text("Dockerfile\n")
    with pytest.raises(PackageError):
        collect(agent_dir)


def test_other_stages_are_not_resolved(agent_dir, edit):
    edit(lambda d: d["stages"]["prod"]["params"].update(project="${env:DOES_NOT_EXIST_123}"))
    assert load_project(agent_dir / "agent.yaml", stage="dev").config.provider.project == "proj-dev"
