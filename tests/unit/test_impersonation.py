import ast
from pathlib import Path

import pytest
from google.auth import impersonated_credentials
from google.auth.credentials import AnonymousCredentials
from typer.testing import CliRunner

from agentless import auth, cli
from agentless.auth import ENV_VAR, Deployer
from agentless.config.loader import ConfigError, load_project
from agentless.providers.agent_runtime.clients import GcpClients
from agentless.state.store import LocalStateStore, State

SRC = Path(__file__).parents[2] / "src" / "agentless"
DEV_SA = "deployer-dev@proj-dev.iam.gserviceaccount.com"
PROD_SA = "deployer-prod@proj-prod.iam.gserviceaccount.com"
HOP_SA = "hop@proj-shared.iam.gserviceaccount.com"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    auth._credentials.cache_clear()
    yield
    auth._credentials.cache_clear()


@pytest.fixture
def adc(monkeypatch):
    source = AnonymousCredentials()
    calls = []

    def default(scopes=None):
        calls.append(scopes)
        return source, "proj-adc"

    monkeypatch.setattr("google.auth.default", default)
    return source, calls


@pytest.fixture
def with_deployer(edit):
    def apply(deployer: dict) -> None:
        edit(lambda d: d["provider"].update(deployer=deployer))

    return apply


# --- Deployer ------------------------------------------------------------------------------------------------------


def test_chain_takes_last_account_as_target():
    deployer = Deployer.from_chain(f" {HOP_SA} ,{DEV_SA}", "flag")
    assert deployer == Deployer(DEV_SA, (HOP_SA,))
    assert deployer.describe() == f"{DEV_SA} via {HOP_SA}"
    assert Deployer().describe() is None


@pytest.mark.parametrize("value", ["", "alice@example.com", "not-an-email", f"{DEV_SA},someone@gmail.com"])
def test_chain_rejects_non_service_accounts(value):
    with pytest.raises(ValueError):
        Deployer.from_chain(value, "flag")


def test_plain_adc_without_impersonation(adc):
    source, calls = adc
    assert Deployer().credentials() is source
    assert calls == [list(auth.SCOPES)]


def test_impersonated_credentials_wrap_adc_once(adc):
    source, calls = adc
    creds = Deployer(DEV_SA, (HOP_SA,)).credentials()
    assert isinstance(creds, impersonated_credentials.Credentials)
    assert isinstance(creds._source_credentials, type(source))
    assert creds._target_principal == DEV_SA
    assert creds._delegates == [HOP_SA]
    assert creds._target_scopes == list(auth.SCOPES)
    assert Deployer(DEV_SA, (HOP_SA,), source="other").credentials() is creds
    assert len(calls) == 1


# --- loader: precedence and offline resolution ---------------------------------------------------------------------


def test_no_deployer_means_adc(agent_dir):
    assert load_project(agent_dir / "agent.yaml").deployer == Deployer()


def test_yaml_deployer_follows_the_stage(agent_dir, edit, with_deployer):
    edit(lambda d: d["stages"]["dev"]["params"].update(deployer=DEV_SA))
    edit(lambda d: d["stages"]["prod"]["params"].update(deployer=PROD_SA))
    with_deployer({"impersonate": "${param:deployer}", "delegates": [HOP_SA]})
    dev = load_project(agent_dir / "agent.yaml", stage="dev").deployer
    prod = load_project(agent_dir / "agent.yaml", stage="prod").deployer
    assert (dev.impersonate, dev.delegates, dev.source) == (DEV_SA, (HOP_SA,), "agent.yaml")
    assert prod.impersonate == PROD_SA


def test_env_beats_yaml_and_flag_beats_env(agent_dir, with_deployer, monkeypatch):
    with_deployer({"impersonate": DEV_SA})
    monkeypatch.setenv(ENV_VAR, PROD_SA)
    from_env = load_project(agent_dir / "agent.yaml").deployer
    assert (from_env.impersonate, from_env.source) == (PROD_SA, ENV_VAR)
    from_flag = load_project(agent_dir / "agent.yaml", impersonate=f"{HOP_SA},{DEV_SA}").deployer
    assert (from_flag.impersonate, from_flag.delegates, from_flag.source) == (DEV_SA, (HOP_SA,), auth.FLAG)


@pytest.mark.parametrize("value", ["${secret:deployer-sa}", "${tf(gs://b/p):deployer}", "${secret:x, 'fallback'}"])
def test_deployer_cannot_come_from_gcp_reads(agent_dir, with_deployer, monkeypatch, value):
    monkeypatch.setattr("agentless.config.sources._access_secret", pytest.fail)
    monkeypatch.setattr("agentless.config.sources._read_tf_outputs", pytest.fail)
    with_deployer({"impersonate": value})
    with pytest.raises(ConfigError, match=r"provider\.deployer cannot use"):
        load_project(agent_dir / "agent.yaml")


@pytest.mark.parametrize(
    ("deployer", "message"),
    [
        ({"impersonate": "alice@example.com"}, "provider.deployer.impersonate"),
        ({"delegates": [HOP_SA]}, "delegates need `impersonate`"),
        ({"impersonate": DEV_SA, "unknown": 1}, "provider.deployer.unknown"),
    ],
)
def test_invalid_yaml_deployer(agent_dir, with_deployer, deployer, message):
    with_deployer(deployer)
    with pytest.raises(ConfigError, match=message.replace(".", r"\.")):
        load_project(agent_dir / "agent.yaml")


@pytest.mark.parametrize("blank", ["", "   ", " , ", "\n"])
def test_blank_flag_or_env_falls_back(agent_dir, with_deployer, monkeypatch, blank):
    path = agent_dir / "agent.yaml"
    monkeypatch.setenv(ENV_VAR, blank)
    assert load_project(path, impersonate=blank).deployer == Deployer()
    with_deployer({"impersonate": DEV_SA})
    assert load_project(path, impersonate=blank).deployer.source == "agent.yaml"
    monkeypatch.setenv(ENV_VAR, PROD_SA)
    assert load_project(path, impersonate=blank).deployer.impersonate == PROD_SA


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_blank_yaml_deployer_means_adc(agent_dir, with_deployer, blank):
    with_deployer({"impersonate": blank})
    assert load_project(agent_dir / "agent.yaml").deployer == Deployer()


def test_unset_param_falls_back_to_adc(agent_dir, with_deployer):
    with_deployer({"impersonate": "${param:deployer, null}"})
    assert load_project(agent_dir / "agent.yaml").deployer == Deployer()


def test_invalid_flag_is_a_config_error(agent_dir):
    with pytest.raises(ConfigError, match="not a service account"):
        load_project(agent_dir / "agent.yaml", impersonate="alice@example.com")


def test_secrets_are_read_as_the_deployer(agent_dir, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "agentless.config.sources._access_secret", lambda name, deployer: seen.append(deployer) or "value"
    )
    path = agent_dir / "agent.yaml"
    path.write_text(path.read_text() + "custom: { token: '${secret:api-token}' }\n")
    load_project(path, impersonate=DEV_SA)
    assert seen == [Deployer(DEV_SA)]


# --- clients, state, CLI -------------------------------------------------------------------------------------------


def test_gcp_clients_use_the_deployer_credentials(adc):
    clients = GcpClients("proj-dev", "europe-west1", Deployer(DEV_SA))
    assert clients._credentials._target_principal == DEV_SA


def test_lock_and_state_record_the_deployer(tmp_path):
    store = LocalStateStore(tmp_path, "dev", deployer=DEV_SA)
    with store.lock("deploy"):
        assert f'"as": "{DEV_SA}"' in (tmp_path / ".agentless" / "dev" / "lock.json").read_text()
    store.write(State("svc", "dev", "proj-dev", "europe-west1"))
    assert store.read().updated_by.endswith(f" as {DEV_SA}")
    LocalStateStore(tmp_path, "dev").write(state := State("svc", "dev", "proj-dev", "europe-west1"))
    assert " as " not in state.updated_by


GCP_COMMANDS = {"validate", "print", "package", "plan", "deploy", "info", "logs", "invoke", "remove", "unlock"}


def test_every_config_command_accepts_the_flag():
    import typer.main

    group = typer.main.get_command(cli.app)
    for name in GCP_COMMANDS:
        opts = {o for p in group.commands[name].params for o in p.opts}
        assert auth.FLAG in opts, name


def test_plan_header_and_validate_show_the_deployer(fake_cli, monkeypatch):
    config = str(fake_cli / "agent.yaml")
    result = runner.invoke(cli.app, ["plan", "-c", config, auth.FLAG, DEV_SA])
    assert result.exit_code == 0, result.output
    assert f"(proj-dev, europe-west1) as {DEV_SA}" in result.output
    monkeypatch.setenv(ENV_VAR, PROD_SA)
    result = runner.invoke(cli.app, ["validate", "-c", config])
    assert f"deployer: {PROD_SA} (from {ENV_VAR})" in result.output


def test_refused_impersonation_gets_a_hint(fake_cli, monkeypatch):
    from google.auth.exceptions import RefreshError

    def refuse(*_args, **_kwargs):
        raise RefreshError(
            "Unable to acquire impersonated credentials", "Permission 'iam.serviceAccounts.getAccessToken'"
        )

    monkeypatch.setattr(cli.AgentRuntimeProvider, "plan", refuse)
    result = runner.invoke(cli.app, ["plan", "-c", str(fake_cli / "agent.yaml"), auth.FLAG, DEV_SA])
    assert result.exit_code == 1
    assert "roles/iam.serviceAccountTokenCreator" in result.output


# --- guard: nothing authenticates behind the deployer's back -------------------------------------------------------


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            yield node, name


def test_every_sdk_client_gets_explicit_credentials():
    missing = []
    for path in SRC.rglob("*.py"):
        for node, name in _calls(ast.parse(path.read_text())):
            if (name.endswith("Client") or name == "AuthorizedSession") and not (
                any(k.arg == "credentials" for k in node.keywords) or (name == "AuthorizedSession" and node.args)
            ):
                missing.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert not missing, "SDK clients built without the deployer's credentials: " + ", ".join(missing)


def test_only_auth_calls_application_default_credentials():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "auth.py" and "google.auth.default" in path.read_text()
    ]
    assert not offenders
