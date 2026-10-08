import datetime
import json
import urllib.parse

import pytest
import yaml
from typer.testing import CliRunner

from agentless import auth, cli
from agentless.config.loader import ConfigError, load_project
from agentless.plan.model import DeployOptions
from agentless.providers.agent_runtime import ops

runner = CliRunner()
SA = "serviceAccount:sample-dev@proj-dev.iam.gserviceaccount.com"
TRACING = {"roles/cloudtrace.agent", "roles/logging.logWriter", "roles/monitoring.metricWriter"}
YES = lambda _cs: True


def env_of(gcp):
    create = next(c for c in gcp.calls if c[0] == "engine_create")[2]
    return {e["name"]: e["value"] for e in create["spec"]["deployment_spec"]["env"]}


def project_roles(gcp, member):
    return {role for role, members in gcp.policies[("project", "proj-dev")].items() if member in members}


# --- agent.yaml ---------------------------------------------------------------------------------------------


def test_tracing_is_on_by_default_without_content(make_provider, gcp):
    make_provider().deploy(DeployOptions(), YES)
    env = env_of(gcp)
    assert env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] == "true"
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "NO_CONTENT"
    assert env["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"


def test_capture_content_from_observability_block(make_provider, gcp, edit):
    edit(lambda d: d.update(observability={"tracing": {"captureContent": True}}))
    make_provider().deploy(DeployOptions(), YES)
    env = env_of(gcp)
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_AND_EVENT"
    assert env["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "true"


def test_tracing_toggle_through_a_param(agent_dir, edit):
    # The documented way to flip it per stage or from the CLI while agent.yaml stays the source of truth.
    edit(lambda d: d.update(observability={"tracing": {"captureContent": "${param:captureContent, false}"}}))
    path = agent_dir / "agent.yaml"
    assert not load_project(path).config.observability.tracing.capture_content
    assert load_project(path, params={"captureContent": "true"}).config.observability.tracing.capture_content


def test_legacy_agent_telemetry_still_works_and_warns(agent_dir, edit):
    edit(lambda d: d["agent"].update(telemetry={"enabled": False, "captureMessageContent": True}))
    config = load_project(agent_dir / "agent.yaml").config
    assert not config.observability.tracing.enabled and config.observability.tracing.capture_content
    assert any("agent.telemetry is deprecated" in m for m in config.deprecations())
    result = runner.invoke(cli.app, ["validate", "-c", str(agent_dir / "agent.yaml")])
    assert result.exit_code == 0 and "deprecated" in result.output


def test_legacy_and_new_tracing_together_is_an_error(agent_dir, edit):
    edit(lambda d: d["agent"].update(telemetry={"enabled": False}))
    edit(lambda d: d.update(observability={"tracing": {"enabled": True}}))
    with pytest.raises(ConfigError, match="not both"):
        load_project(agent_dir / "agent.yaml")


def test_unknown_tracing_key_is_rejected(agent_dir, edit):
    edit(lambda d: d.update(observability={"tracing": {"captureMessageContent": True}}))
    with pytest.raises(ConfigError):
        load_project(agent_dir / "agent.yaml")


# --- automatic IAM ------------------------------------------------------------------------------------------


def test_tracing_grants_its_roles_automatically(make_provider, gcp):
    _, cs = make_provider().plan(DeployOptions())
    iam = next(c for c in cs.changes if c.resource == "iam")
    auto = [d for d in iam.details if "(automatic: tracing)" in d]
    assert len(auto) == 3 and all(any(r in d for r in TRACING) for d in auto)
    make_provider().deploy(DeployOptions(), YES)
    assert TRACING <= project_roles(gcp, SA)


def test_declared_role_is_not_labelled_automatic(make_provider, edit):
    edit(lambda d: d["identity"]["roles"]["project"].append("roles/logging.logWriter"))
    _, cs = make_provider().plan(DeployOptions())
    iam = next(c for c in cs.changes if c.resource == "iam")
    [log_writer] = [d for d in iam.details if "roles/logging.logWriter" in d]
    assert "automatic" not in log_writer
    assert sum("(automatic: tracing)" in d for d in iam.details) == 2


def test_turning_tracing_off_revokes_only_what_agentless_granted(make_provider, gcp, edit):
    gcp.policies[("project", "proj-dev")]["roles/cloudtrace.agent"].add(SA)  # granted outside agentless
    make_provider().deploy(DeployOptions(), YES)
    edit(lambda d: d.update(observability={"tracing": {"enabled": False}}))
    make_provider().deploy(DeployOptions(), YES)
    roles = project_roles(gcp, SA)
    assert "roles/cloudtrace.agent" in roles, "foreign binding must survive"
    assert not {"roles/logging.logWriter", "roles/monitoring.metricWriter"} & roles
    assert not make_provider().plan(DeployOptions())[1].pending


def test_agent_identity_principal_gets_tracing_roles(make_provider, gcp, edit):
    edit(lambda d: d.update(identity={"type": "agentIdentity", "roles": {"project": ["roles/aiplatform.user"]}}))
    make_provider().deploy(DeployOptions(), YES)
    [engine] = gcp.engines.values()
    assert TRACING <= project_roles(gcp, f"principal://{engine['effective_identity']}")


def test_platform_identity_gets_no_extra_grants(make_provider, gcp, edit):
    edit(lambda d: d.update(identity={"type": "platform"}))
    make_provider().deploy(DeployOptions(), YES)
    assert not any(TRACING & set(roles) for roles in [project_roles(gcp, m) for m in _members(gcp)])


def _members(gcp):
    return {m for policy in gcp.policies.values() for members in policy.values() for m in members}


# --- metrics and console links ------------------------------------------------------------------------------


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        metric = params["filter"]
        if "request_count" in metric:
            return _Response(
                {
                    "timeSeries": [
                        _series({"response_code_class": "2xx"}, [{"int64Value": "18"}]),
                        _series({"response_code_class": "5xx"}, [{"int64Value": "2"}]),
                    ]
                }
            )
        value = 1500.0 if params["aggregation.crossSeriesReducer"].endswith("95") else 0.4
        unit = "ms" if value > 1 else "s"
        return _Response({"timeSeries": [{**_series({}, [{"doubleValue": value}]), "unit": unit}]})


def _series(labels, values):
    return {"metric": {"labels": labels}, "points": [{"value": v} for v in values]}


ENGINE = "projects/123/locations/europe-west1/reasoningEngines/987"


def test_metrics_summarises_requests_errors_and_latency():
    session = _Session()
    data = ops.metrics("proj", ENGINE, since=datetime.timedelta(hours=1), credentials=None, session=session)
    assert data["requests"] == 20 and data["byClass"] == {"2xx": 18, "5xx": 2}
    assert data["errorRate"] == pytest.approx(0.1)
    assert data["latencyMs"] == {"p50": pytest.approx(400.0), "p95": pytest.approx(1500.0)}
    url, params = session.calls[0]
    assert url == "https://monitoring.googleapis.com/v3/projects/proj/timeSeries"
    assert 'reasoning_engine_id="987"' in params["filter"] and params["aggregation.alignmentPeriod"] == "3600s"


def test_metrics_with_no_traffic():
    class Empty(_Session):
        def get(self, url, params, timeout):
            return _Response({})

    data = ops.metrics("proj", ENGINE, since=datetime.timedelta(minutes=5), credentials=None, session=Empty())
    assert data["requests"] == 0 and data["errorRate"] is None and data["latencyMs"] == {"p50": None, "p95": None}


def test_console_links_point_at_this_engine():
    links = ops.console_links("proj", "europe-west1", ENGINE)
    assert links["console"].endswith("/locations/europe-west1/agent-engines/987?project=proj")
    query = urllib.parse.unquote(links["logs"].split("query=", 1)[1].split("?", 1)[0])
    assert 'reasoning_engine_id="987"' in query and "timestamp" not in query
    assert links["traces"] == "https://console.cloud.google.com/traces/explorer?project=proj"


def test_cli_metrics_and_open(fake_cli, monkeypatch):
    cfg = ["-c", str(fake_cli / "agent.yaml")]
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y"]).exit_code == 0
    monkeypatch.setattr(auth, "_credentials", lambda *_: "creds")
    seen = {}

    def fake_metrics(project, engine, *, since, credentials):
        seen.update(project=project, engine=engine, since=since, credentials=credentials)
        return {
            "engine": engine,
            "window": "1800s",
            "requests": 4,
            "byClass": {"2xx": 4},
            "errorRate": 0.0,
            "latencyMs": {"p50": 250.0, "p95": 2100.0},
        }

    monkeypatch.setattr(ops, "metrics", fake_metrics)
    result = runner.invoke(cli.app, ["metrics", *cfg, "--since", "30m"])
    assert result.exit_code == 0, result.output
    assert "requests  4" in result.output and "0.0% 5xx" in result.output and "p95 2.1s" in result.output
    assert seen["project"] == "proj-dev" and seen["since"] == datetime.timedelta(minutes=30)
    assert seen["credentials"] == "creds"
    assert json.loads(runner.invoke(cli.app, ["metrics", *cfg, "--json"]).output)["requests"] == 4

    launched = []
    monkeypatch.setattr(cli.typer, "launch", launched.append)
    out = runner.invoke(cli.app, ["open", "logs", *cfg, "--print"])
    assert out.exit_code == 0 and "logs/query;query=" in out.output and not launched
    assert runner.invoke(cli.app, ["open", "traces", *cfg]).exit_code == 0 and len(launched) == 1
    bad = runner.invoke(cli.app, ["open", "dashboards", *cfg])
    assert bad.exit_code != 0 and "unknown target" in bad.output


def test_schema_documents_observability(agent_dir):
    from agentless.config.schema import AgentConfig

    schema = json.dumps(AgentConfig.model_json_schema(by_alias=True))
    assert "captureContent" in schema and "observability" in schema
    assert yaml.safe_load((agent_dir / "agent.yaml").read_text())  # fixture untouched
