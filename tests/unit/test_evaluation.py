import json

import pytest
from typer.testing import CliRunner

from agentless import cli
from agentless.config.loader import ConfigError, load_project
from agentless.plan.model import Action, DeployOptions
from agentless.providers.agent_runtime import evals
from agentless.providers.agent_runtime import spec as engine_spec

runner = CliRunner()
YES = lambda _cs: True


def monitors(**overrides):
    def apply(data):
        data["observability"] = {"tracing": {"captureContent": True}}
        data["evaluation"] = {"monitors": {"quality": {"metrics": ["safety"], "samplingPercent": 20, **overrides}}}

    return apply


def evaluation(gcp):
    return {k: v for k, v in gcp.evaluators.items()}


# --- schema and helpers ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ({"metrics": {"safety": 1.5}}, "between 0 and 1"),
        ({"afterDeploy": True, "metrics": {"safety": 0.8}}, "afterDeploy needs a dataset"),
        ({"dest": "s3://x"}, "dest"),
        ({"monitors": {"Bad_Key": {"metrics": ["safety"]}}}, "monitor key"),
        ({"monitors": {"q": {"metrics": []}}}, "metrics"),
    ],
)
def test_evaluation_block_is_validated(edit, agent_dir, block, error):
    edit(lambda d: d.update(evaluation=block))
    with pytest.raises(ConfigError, match=error):
        load_project(agent_dir / "agent.yaml")


def test_monitors_need_content_in_traces(edit, agent_dir):
    edit(lambda d: d.update(evaluation={"monitors": {"q": {"metrics": ["safety"]}}}))
    with pytest.raises(ConfigError, match="captureContent"):
        load_project(agent_dir / "agent.yaml")


def test_monitors_turn_on_gen_ai_semantic_conventions(edit, agent_dir):
    edit(monitors())
    env = engine_spec.desired_spec(load_project(agent_dir / "agent.yaml"), service_account="sa@x", engine_name=None)
    assert env["env"]["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"


def test_metric_spec_names():
    assert evals.metric_spec_name("safety_v1") == "safety_v1"
    assert evals.metric_spec_name("SAFETY").startswith("safety_v")
    assert evals.metric_spec_name("multi_turn_task_success") == "multi_turn_task_success_v1"
    with pytest.raises(ValueError, match="unknown evaluation metric"):
        evals.metric_spec_name("vibes")


def test_verdict_matches_versioned_summary_keys():
    summary = {
        "candidate-1/final_response_quality_v2/AVERAGE": 0.9,
        "candidate-1/final_response_quality_v2/STANDARD_DEVIATION": 0.1,
        "candidate-1/safety_v3/AVERAGE": 0.5,
    }
    results = {r.name: r for r in evals.verdict(summary, {"final_response_quality": 0.8, "safety": 0.7, "x": None})}
    assert results["final_response_quality"].passed is True
    assert results["safety"].passed is False
    assert results["x"].passed is None and results["x"].score is None
    assert evals.verdict({}, {"safety": 0.5})[0].passed is False  # a gated metric without a score fails


# --- online monitors ---------------------------------------------------------------------------------------------


def test_monitor_lifecycle(make_provider, gcp, edit):
    edit(monitors())
    _, cs = make_provider().plan(DeployOptions())
    assert next(c for c in cs.changes if c.resource == "evaluation").action == Action.CREATE
    make_provider().deploy(DeployOptions(), YES)
    [(name, body)] = evaluation(gcp).items()
    engine = next(iter(gcp.engines))
    assert body["agentResource"] == engine and body["config"]["randomSampling"]["percentage"] == 20
    assert body["cloudObservability"]["openTelemetry"]["semconvVersion"]
    assert body["displayName"] == "sample-agent-dev-quality"
    assert make_provider().info()["onlineMonitors"] == {"quality": name}

    _, cs = make_provider().plan(DeployOptions())
    assert not cs.pending  # no-op

    gcp.calls.clear()
    edit(monitors(samplingPercent=50, enabled=False))
    make_provider().deploy(DeployOptions(), YES)
    assert ("evaluator_patch", name, "config") in gcp.calls
    assert ("evaluator_suspend", name) in gcp.calls
    assert gcp.evaluators[name]["state"] == "SUSPENDED"
    assert "(suspended)" in make_provider().info()["onlineMonitors"]["quality"]

    edit(lambda d: d["evaluation"].pop("monitors"))
    make_provider().deploy(DeployOptions(), YES)
    assert not gcp.evaluators and "evaluation" not in make_provider()._state().resources


def test_monitor_recreated_when_deleted_outside(make_provider, gcp, edit):
    edit(monitors())
    make_provider().deploy(DeployOptions(), YES)
    gcp.evaluators.clear()
    _, cs = make_provider().plan(DeployOptions())
    assert any("deleted outside" in d for d in next(c for c in cs.changes if c.resource == "evaluation").details)
    make_provider().deploy(DeployOptions(), YES)
    assert len(gcp.evaluators) == 1


def test_crashed_create_is_adopted(make_provider, gcp, edit, monkeypatch):
    edit(monitors())
    original = gcp.evaluator_create

    def create_then_crash(parent, body):
        original(parent, body)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(gcp, "evaluator_create", create_then_crash)
    with pytest.raises(RuntimeError, match="connection reset"):
        make_provider().deploy(DeployOptions(), YES)
    monkeypatch.setattr(gcp, "evaluator_create", original)
    make_provider().deploy(DeployOptions(), YES)
    assert len(gcp.evaluators) == 1  # adopted, not duplicated
    assert make_provider().info()["onlineMonitors"]["quality"] == next(iter(gcp.evaluators))


def test_monitor_follows_a_replaced_engine(make_provider, gcp, edit):
    edit(monitors())
    make_provider().deploy(DeployOptions(), YES)
    old_engine = next(iter(gcp.engines))
    edit(
        lambda d: d.setdefault("agent", {}).update(
            encryption={"kmsKey": "projects/p/locations/l/keyRings/r/cryptoKeys/k"}
        )
    )
    make_provider().deploy(DeployOptions(allow_replace=True), YES)
    [body] = gcp.evaluators.values()
    assert body["agentResource"] != old_engine and body["agentResource"] in gcp.engines


def test_remove_deletes_monitors_before_the_engine(make_provider, gcp, edit):
    edit(monitors())
    make_provider().deploy(DeployOptions(), YES)
    gcp.calls.clear()
    assert make_provider().remove(YES)
    kinds = gcp.kinds()
    assert kinds.index("evaluator_delete") < kinds.index("engine_delete") and not gcp.evaluators


def test_unknown_monitor_metric_fails_plan(make_provider, edit):
    edit(monitors(metrics=["vibes"]))
    with pytest.raises(RuntimeError, match="evaluation.monitors.quality: unknown evaluation metric"):
        make_provider().plan(DeployOptions())


# --- CLI: eval and the deploy gate --------------------------------------------------------------------------------


@pytest.fixture
def eval_setup(fake_cli, edit, monkeypatch):
    (fake_cli / "dataset.json").write_text(json.dumps({"eval_cases": [{"prompt": "hi"}]}))
    edit(
        lambda d: d.update(
            evaluation={
                "dataset": "dataset.json",
                "dest": "gs://evals",
                "metrics": {"safety": 0.8, "tool_use_quality": None},
            }
        )
    )
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"name": "projects/p/locations/l/evaluationRuns/1", "state": "SUCCEEDED", "summary": scores}

    scores = {"c/safety_v3/AVERAGE": 0.9, "c/tool_use_quality_v2/AVERAGE": 0.4}
    monkeypatch.setattr(evals, "run", fake_run)
    monkeypatch.setattr(cli.auth, "_credentials", lambda *_: "creds")
    return fake_cli, seen, scores


def test_eval_passes_and_saves_results(eval_setup):
    agent_dir, seen, _ = eval_setup
    cfg = ["-c", str(agent_dir / "agent.yaml")]
    assert runner.invoke(cli.app, ["eval", *cfg]).exit_code == 1  # nothing deployed yet
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y"]).exit_code == 0
    result = runner.invoke(cli.app, ["eval", *cfg])
    assert result.exit_code == 0, result.output
    assert "✔ safety" in result.output and "· tool_use_quality" in result.output
    call = seen[-1]
    assert call["engine"].startswith("projects/123456/") and call["dest"] == "gs://evals"
    assert call["metrics"] == ["safety", "tool_use_quality"] and call["dataset"]["eval_cases"]
    [saved] = (agent_dir / ".agentless" / "dev" / "eval").iterdir()
    assert json.loads(saved.read_text())["passed"] is True


def test_eval_fails_below_threshold_and_json(eval_setup):
    agent_dir, _, _ = eval_setup
    cfg = ["-c", str(agent_dir / "agent.yaml")]
    runner.invoke(cli.app, ["deploy", *cfg, "-y"])
    result = runner.invoke(cli.app, ["eval", *cfg, "--metric", "tool_use_quality=0.5"])
    assert result.exit_code == 1 and "✖ tool_use_quality" in result.output
    out = runner.invoke(cli.app, ["eval", *cfg, "--json", "-o", str(agent_dir / "r.json")])
    assert out.exit_code == 0
    assert json.loads(out.stdout)["metrics"][0] == {"name": "safety", "score": 0.9, "threshold": 0.8, "passed": True}
    assert (agent_dir / "r.json").is_file()
    assert runner.invoke(cli.app, ["eval", *cfg, "--metric", "safety=2"]).exit_code == 1


def test_deploy_gate(eval_setup, edit):
    agent_dir, seen, scores = eval_setup
    cfg = ["-c", str(agent_dir / "agent.yaml")]
    edit(lambda d: d["evaluation"].update(afterDeploy=True))
    scores["c/safety_v3/AVERAGE"] = 0.1
    result = runner.invoke(cli.app, ["deploy", *cfg, "-y"])
    assert result.exit_code == 1 and "✔ deployed" in result.output
    assert "deployment stays in place" in result.output and len(seen) == 1

    assert runner.invoke(cli.app, ["deploy", *cfg, "-y"]).exit_code == 0  # nothing to deploy: no gate
    assert len(seen) == 1
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y", "--eval"]).exit_code == 1  # explicit: evaluates anyway
    assert runner.invoke(cli.app, ["deploy", *cfg, "-y", "--force", "--no-eval"]).exit_code == 0
    assert len(seen) == 2

    result = runner.invoke(cli.app, ["deploy", *cfg, "-y", "--force", "--no-wait"])
    assert result.exit_code == 0 and "evaluation skipped" in result.output and len(seen) == 2
