"""`agentless` command line: the Serverless-style lifecycle for agents on Agent Runtime or Cloud Run."""

from __future__ import annotations

import datetime
import functools
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar, cast

import typer
import yaml

from agentless import __version__, auth
from agentless.compat.agents_cli import read_manifest
from agentless.config.loader import DEFAULT_CONFIG_FILE, ConfigError, Project, load_project
from agentless.config.schema import AgentConfig
from agentless.config.sources import read_secret
from agentless.hooks import plugin_manager, variable_sources
from agentless.package.packager import PackageError, write_archive
from agentless.plan.model import Action, ChangeSet, DeployOptions
from agentless.plan.render import render
from agentless.providers.agent_runtime import evals, ops
from agentless.providers.agent_runtime.provider import AgentRuntimeProvider, DeployError
from agentless.providers.base import BaseProvider
from agentless.providers.cloud_run import ops as run_ops
from agentless.providers.cloud_run import spec as run_spec
from agentless.providers.cloud_run.clients import webhook_session
from agentless.providers.cloud_run.provider import CloudRunProvider
from agentless.state.store import StateError

app = typer.Typer(
    help="Deploy ADK agents to Google Cloud Agent Platform from a declarative agent.yaml.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to agent.yaml.")]
StageOpt = Annotated[str | None, typer.Option("--stage", "-s", help="Stage to target (default: provider.stage).")]
ParamOpt = Annotated[list[str] | None, typer.Option("--param", "-p", help="Override a stage param: key=value.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]
ImpersonateOpt = Annotated[
    str | None,
    typer.Option(
        auth.FLAG,
        metavar="SA_EMAIL",
        help=f"Act as this service account (`a,b,target` chains through delegates). Overrides {auth.ENV_VAR} and "
        "provider.deployer.",
    ),
]

F = TypeVar("F", bound=Callable[..., Any])


def _fail(message: str) -> None:
    typer.secho(f"✖ {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def handle_errors(fn: F) -> F:
    """Turn expected failures into a red message and exit code 1."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except (ConfigError, StateError, DeployError, PackageError) as e:
            _fail(str(e))
        except Exception as e:  # GCP API errors surface with their own message
            if type(e).__module__.startswith(("google.", "requests")) or isinstance(e, RuntimeError):
                _fail(f"{type(e).__name__}: {e}{_hint(e)}")
            raise

    return cast(F, wrapper)


def _hint(error: Exception) -> str:
    """Point at the missing grant when impersonation is refused."""
    if "getAccessToken" in str(error):
        return (
            "\n  hint: the caller needs roles/iam.serviceAccountTokenCreator on the impersonated service account "
            "(and on each delegate)"
        )
    return ""


def _params(raw: list[str] | None) -> dict[str, str]:
    out = {}
    for item in raw or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ConfigError(f"--param expects key=value, got {item!r}")
        out[key.strip()] = value
    return out


def _load(
    config: Path, stage: str | None, params: list[str] | None, impersonate: str | None = None, **options: Any
) -> tuple[Project, Any]:
    pm = plugin_manager()
    project = load_project(
        config,
        stage=stage,
        options=options,
        params=_params(params),
        extra_sources=variable_sources(pm),
        impersonate=impersonate,
    )
    pm.hook.agentless_after_load(project=project)
    for message in project.config.deprecations():
        typer.secho(f"⚠ {message}", fg=typer.colors.YELLOW, err=True)
    return project, pm


def _provider(project: Project, pm: Any) -> BaseProvider:
    if _on_cloud_run(project):
        return CloudRunProvider(project, hooks=pm, echo=typer.echo)
    return AgentRuntimeProvider(project, hooks=pm, echo=typer.echo)


def _on_cloud_run(project: Project) -> bool:
    return project.config.provider.name == "cloud-run"


def _service_name(provider: BaseProvider) -> str:
    """Deployed Cloud Run service; raises when nothing is deployed."""
    name = provider.info().get("cloudRunService")
    if not name:
        raise DeployError("nothing deployed for this stage yet")
    return name


def _header(project: Project, verb: str) -> None:
    cfg = project.config
    identity = project.deployer.describe()
    typer.secho(
        f"{verb} {cfg.service} → stage {cfg.provider.stage} ({cfg.provider.project}, {cfg.provider.region})"
        + (f" as {identity}" if identity else ""),
        bold=True,
    )


@app.command()
@handle_errors
def version() -> None:
    """Print the agentless version."""
    typer.echo(__version__)


@app.command()
@handle_errors
def init(
    path: Annotated[Path, typer.Argument(help="agents-cli project directory.")] = Path("."),
    project_id: Annotated[str, typer.Option("--project", help="GCP project for the dev stage.")] = "my-dev-project",
    force: Annotated[bool, typer.Option(help="Overwrite an existing agent.yaml.")] = False,
) -> None:
    """Create agent.yaml from an agents-cli project's manifest."""
    target = path / DEFAULT_CONFIG_FILE
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists (use --force to overwrite)")
    manifest = read_manifest(path)
    if manifest is None:
        raise ConfigError(f"no agents-cli project in {path.resolve()} (agents-cli-manifest.yaml not found)")
    service = re.sub(r"[^a-z0-9-]", "-", (manifest.name or path.resolve().name).lower()).strip("-")
    target.write_text(
        INIT_TEMPLATE.format(
            schema_url=SCHEMA_URL,
            service=service,
            region=manifest.region or "europe-west1",
            project=project_id,
            sa=service[:24].rstrip("-"),
        ),
        encoding="utf-8",
    )
    typer.secho(f"✔ wrote {target}", fg=typer.colors.GREEN)
    typer.echo("  next: edit project ids and roles, then `agentless plan --stage dev`")


@app.command()
@handle_errors
def validate(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Resolve variables and validate agent.yaml (no GCP calls unless ${secret:}/${tf:} are used)."""
    project, _ = _load(config, stage, param, impersonate)
    typer.secho(f"✔ {project.config_path.name} is valid for stage {project.stage}", fg=typer.colors.GREEN)
    if identity := project.deployer.describe():
        typer.echo(f"  deployer: {identity} (from {project.deployer.source})")


@app.command("print")
@handle_errors
def print_(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Print agent.yaml with every variable resolved; secret values are masked."""
    project, _ = _load(config, stage, param, impersonate)

    def mask(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: mask(v) for k, v in node.items()}
        if isinstance(node, list):
            return [mask(v) for v in node]
        return "********" if isinstance(node, str) and node in project.sensitive else node

    typer.echo(yaml.safe_dump(mask(project.resolved), sort_keys=False))


@app.command()
@handle_errors
def package(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Also write a reproducible .tar.gz.")] = None,
    impersonate: ImpersonateOpt = None,
) -> None:
    """List the files that would be uploaded and their content hash (offline)."""
    project, pm = _load(config, stage, param, impersonate)
    pkg = _provider(project, pm).package()
    for f in pkg.files:
        typer.echo(f"  {f}")
    typer.secho(f"✔ {len(pkg.files)} files, sha256 {pkg.sha256}", fg=typer.colors.GREEN)
    if output:
        typer.echo(f"  wrote {write_archive(pkg, output)}")


@app.command()
@handle_errors
def plan(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    force: Annotated[bool, typer.Option(help="Plan a rebuild even if the source is unchanged.")] = False,
    code_only: Annotated[bool, typer.Option("--code-only", help="Only push code; defer config changes.")] = False,
    allow_replace: Annotated[bool, typer.Option("--allow-replace")] = False,
    detailed_exitcode: Annotated[bool, typer.Option(help="Exit 2 when there are changes (for CI).")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Show what `deploy` would change, without changing anything."""
    project, pm = _load(config, stage, param, impersonate)
    _header(project, "Planning")
    _, changeset = _provider(project, pm).plan(DeployOptions(force, code_only, allow_replace))
    typer.echo(render(changeset, "Changes:"))
    if changeset.blocked:
        raise typer.Exit(1)
    if detailed_exitcode and changeset.pending:
        raise typer.Exit(2)


def _confirm(yes: bool, title: str) -> Callable[[ChangeSet], bool]:
    def confirm(changeset: ChangeSet) -> bool:
        typer.echo(render(changeset, title))
        if changeset.blocked:
            raise DeployError("plan has blocked changes (see ✋ above)")
        if not changeset.pending:
            typer.secho("✔ nothing to do", fg=typer.colors.GREEN)
            return False
        destructive = any(c.action in (Action.DELETE, Action.REPLACE) for c in changeset.pending)
        # CI runs non-interactively and proceeds, like `serverless deploy`; a TTY asks unless --yes.
        if yes or not sys.stdin.isatty():
            return True
        return typer.confirm("Apply these changes?", default=not destructive)

    return confirm


@app.command()
@handle_errors
def deploy(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    yes: YesOpt = False,
    force: Annotated[bool, typer.Option(help="Rebuild even if the source is unchanged.")] = False,
    code_only: Annotated[
        bool, typer.Option("--code-only", help="Only push code; config changes wait for the next full deploy.")
    ] = False,
    allow_replace: Annotated[
        bool,
        typer.Option(
            "--allow-replace", help="Allow deleting and recreating the engine for immutable changes (PSC-I, CMEK)."
        ),
    ] = False,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Return once the engine operation has started.")] = False,
    status: Annotated[bool, typer.Option("--status", help="Check/finalize a --no-wait deployment.")] = False,
    run_eval: Annotated[
        bool | None,
        typer.Option(
            "--eval/--no-eval",
            help="Evaluate the deployed agent afterwards and exit 1 below a threshold (default: "
            "evaluation.afterDeploy). --eval also evaluates when there was nothing to deploy.",
        ),
    ] = None,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Reconcile GCP with agent.yaml: identity, IAM, engine, memory, networking, Gemini Enterprise."""
    project, pm = _load(config, stage, param, impersonate)
    if _on_cloud_run(project) and (no_wait or status):
        raise ConfigError("--no-wait and --status are not supported on cloud-run yet: a deploy waits for the revision")
    provider = _provider(project, pm)
    if status:
        if provider.status():
            typer.secho("✔ no deployment in progress", fg=typer.colors.GREEN)
        else:
            typer.secho("⏳ still running; check again later", fg=typer.colors.YELLOW)
        return
    _header(project, "Deploying")
    options = DeployOptions(force=force, code_only=code_only, allow_replace=allow_replace, no_wait=no_wait)
    applied = provider.deploy(options, _confirm(yes, "Changes:"))
    if applied is not None:
        typer.secho("✔ deployed", fg=typer.colors.GREEN)
        _print_info(provider.info())
    gate = project.config.evaluation.after_deploy if run_eval is None else run_eval
    if not gate or (applied is None and run_eval is None):
        return
    if no_wait:
        typer.secho("⚠ evaluation skipped: the engine is still deploying (--no-wait)", fg=typer.colors.YELLOW)
        return
    if not _evaluate(project, provider, None, {}, None):
        _fail("evaluation below threshold" + (" (the deployment stays in place)" if applied is not None else ""))


def _evaluate(
    project: Project,
    provider: BaseProvider,
    dataset: Path | None,
    overrides: dict[str, float | None],
    output: Path | None,
    as_json: bool = False,
) -> bool:
    """Run the evaluation against the stage's engine, print and save the results; False when a threshold fails."""
    if _on_cloud_run(project):
        raise ConfigError("`eval` is not supported on cloud-run yet (it evaluates a reasoning engine)")
    cfg, ev = project.config, project.config.evaluation
    path = dataset or (project.config_path.parent / ev.dataset if ev.dataset else None)
    if path is None:
        raise ConfigError("no dataset: set evaluation.dataset or pass --dataset")
    if not path.is_file():
        raise ConfigError(f"evaluation dataset {path} not found")
    thresholds = {**ev.metrics, **overrides}
    if not thresholds:
        raise ConfigError("no metrics: set evaluation.metrics or pass --metric name[=threshold]")
    dest = ev.dest or (
        f"gs://{cfg.provider.staging_bucket}/agentless/{cfg.service}/{cfg.provider.stage}/evals"
        if cfg.provider.staging_bucket
        else None
    )
    if dest is None:
        raise ConfigError("no results location: set evaluation.dest or provider.stagingBucket")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"evaluation dataset {path} is not valid JSON: {e}") from e
    engine = _engine_name(provider)
    stamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d-%H%M%S")
    log = (lambda m: typer.secho(m, err=True)) if as_json else typer.echo
    log(f"  {path.name}: {len(data.get('eval_cases', []))} cases against {engine} …")
    run = evals.run(
        project=cfg.provider.project,
        region=cfg.provider.region,
        engine=engine,
        dataset=data,
        dest=dest,
        metrics=list(thresholds),
        credentials=project.deployer.credentials(),
        display_name=f"{cfg.service}-{cfg.provider.stage}-{stamp}",
        on_tick=lambda state, s: log(f"  … {state.lower()} {int(s // 60)}m{int(s % 60):02d}s"),
    )
    results = evals.verdict(run["summary"], thresholds)
    passed = all(r.passed is not False for r in results)
    report = {
        "engine": engine,
        "run": run["name"],
        "dataset": str(path),
        "dest": dest,
        "passed": passed,
        "metrics": [{"name": r.name, "score": r.score, "threshold": r.threshold, "passed": r.passed} for r in results],
        "summary": run["summary"],
    }
    out = output or project.config_path.parent / ".agentless" / cfg.provider.stage / "eval" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return passed
    for r in results:
        score = "-" if r.score is None else f"{r.score:.2f}"
        limit = "" if r.threshold is None else f"  (min {r.threshold:.2f})"
        mark = {True: "✔", False: "✖", None: "·"}[r.passed]
        typer.secho(
            f"  {mark} {r.name:<28} {score}{limit}",
            fg={True: typer.colors.GREEN, False: typer.colors.RED, None: None}[r.passed],
        )
    typer.echo(f"  run      {run['name']}\n  results  {dest}\n  saved    {out}")
    return passed


def _print_info(info: dict[str, Any]) -> None:
    for key, value in info.items():
        if value not in (None, "", 0):
            typer.echo(f"  {key:<17} {value}")


@app.command()
@handle_errors
def info(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Show what is deployed for a stage."""
    project, pm = _load(config, stage, param, impersonate)
    data = _provider(project, pm).info()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        _print_info(data)


UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _window(since: str) -> datetime.timedelta:
    match = re.fullmatch(r"(\d+)([smhd])", since)
    if not match:
        raise ConfigError("--since expects <number><s|m|h|d>")
    return datetime.timedelta(**{UNITS[match[2]]: int(match[1])})


def _engine_name(provider: BaseProvider) -> str:
    name = provider.info().get("engine")
    if not name:
        raise DeployError("nothing deployed for this stage yet")
    return name


@app.command()
@handle_errors
def logs(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    since: Annotated[str, typer.Option(help="Look-back window, e.g. 30m, 2h, 1d.")] = "1h",
    severity: Annotated[str | None, typer.Option(help="Minimum severity, e.g. WARNING.")] = None,
    tail: Annotated[bool, typer.Option("--tail", "-t", help="Keep following new entries.")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Print the agent's Cloud Logging entries."""
    project, pm = _load(config, stage, param, impersonate)
    window = _window(since)
    provider = _provider(project, pm)
    if _on_cloud_run(project):
        service = _service_name(provider)
        ops.read_logs(
            project.config.provider.project,
            service,
            since=window,
            severity=severity,
            tail=tail,
            echo=typer.echo,
            credentials=project.deployer.credentials(),
            make_filter=lambda start, sev: run_ops.log_filter(service, start, sev),
        )
        return
    ops.read_logs(
        project.config.provider.project,
        _engine_name(provider),
        since=window,
        severity=severity,
        tail=tail,
        echo=typer.echo,
        credentials=project.deployer.credentials(),
    )


@app.command()
@handle_errors
def metrics(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    since: Annotated[str, typer.Option(help="Look-back window, e.g. 30m, 2h, 1d.")] = "1h",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Request count, error rate and latency of the deployed agent (Cloud Monitoring, read-only)."""
    project, pm = _load(config, stage, param, impersonate)
    provider = _provider(project, pm)
    if _on_cloud_run(project):
        data = run_ops.metrics(
            project.config.provider.project,
            _service_name(provider),
            since=_window(since),
            credentials=project.deployer.credentials(),
        )
    else:
        data = ops.metrics(
            project.config.provider.project,
            _engine_name(provider),
            since=_window(since),
            credentials=project.deployer.credentials(),
        )
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    cfg = project.config
    typer.secho(f"{cfg.service} → stage {cfg.provider.stage}, last {since}", bold=True)
    classes = ", ".join(f"{k} {v}" for k, v in data["byClass"].items())
    typer.echo(f"  requests  {data['requests']}" + (f"  ({classes})" if classes else ""))
    rate = data["errorRate"]
    typer.echo(f"  errors    {'-' if rate is None else f'{rate:.1%}'} 5xx")
    p50, p95 = (data["latencyMs"][k] for k in ("p50", "p95"))
    typer.echo(f"  latency   p50 {_ms(p50)}  p95 {_ms(p95)}")
    if not data["requests"]:
        typer.echo("  no requests in this window (metrics can lag a few minutes)")


def _ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{value:.0f}ms"


@app.command("open")
@handle_errors
def open_(
    target: Annotated[str, typer.Argument(help=f"One of: {', '.join(ops.CONSOLE_TARGETS)}.")] = "console",
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    print_only: Annotated[bool, typer.Option("--print", help="Print the URL instead of opening a browser.")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Open the agent's Cloud Console page, its logs, or the project's traces."""
    if target not in ops.CONSOLE_TARGETS:
        raise ConfigError(f"unknown target {target!r}; choose one of: {', '.join(ops.CONSOLE_TARGETS)}")
    project, pm = _load(config, stage, param, impersonate)
    cfg = project.config.provider
    provider = _provider(project, pm)
    if _on_cloud_run(project):
        url = run_ops.console_links(cfg.project, cfg.region, _service_name(provider))[target]
    else:
        url = ops.console_links(cfg.project, cfg.region, _engine_name(provider))[target]
    typer.echo(url)
    if not print_only:
        typer.launch(url)


@app.command()
@handle_errors
def invoke(
    message: Annotated[str, typer.Option("--message", "-m", help="Message to send.")],
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    session: Annotated[str | None, typer.Option(help="Reuse a session id.")] = None,
    user: Annotated[str, typer.Option(help="User id for the session.")] = "agentless-cli",
    raw: Annotated[bool, typer.Option(help="Print raw ADK events as JSON lines.")] = False,
    app_token: Annotated[
        str | None,
        typer.Option(
            help="Cloud Run auth googleToken: token sent in cloudRun.googleToken.header (default: your ID token). "
            "auth webhook: the shared secret (default: read from cloudRun.webhook.secret)."
        ),
    ] = None,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Send a message to the deployed agent and stream its answer."""
    project, pm = _load(config, stage, param, impersonate)
    cfg = project.config
    provider = _provider(project, pm)
    webhook = run_spec.webhook_header(project)
    if app_token and not (run_spec.app_token_header(project) or webhook):
        raise ConfigError("--app-token only applies to Cloud Run agents with cloudRun.auth: googleToken or webhook")
    if _on_cloud_run(project):
        _service_name(provider)
        url = provider.info()["url"]
        http = None
        if webhook:
            assert cfg.cloud_run and cfg.cloud_run.webhook
            ref = cfg.cloud_run.webhook.secret
            token = app_token or read_secret(cfg.provider.project, ref.secret, ref.version, project.deployer)
            http = webhook_session(url, webhook, token)
        events = run_ops.invoke(
            url,
            project.agent_directory,
            message,
            user_id=user,
            session_id=session,
            credentials=project.deployer.credentials(),
            app_header=run_spec.app_token_header(project),
            app_token=app_token,
            http=http,
        )
    else:
        events = ops.invoke(
            cfg.provider.region,
            _engine_name(provider),
            message,
            user_id=user,
            session_id=session,
            project=cfg.provider.project,
            credentials=project.deployer.credentials(),
        )
    for event in events:
        if "session_id" in event and len(event) == 1:
            typer.secho(f"session {event['session_id']}", dim=True)
        elif raw:
            typer.echo(json.dumps(event))
        elif "error" in event and "content" not in event:
            _fail(f"the agent failed: {event['error']}")
        else:
            author, text = ops.event_text(event)
            if text:
                typer.echo(f"[{author}] {text}")


def _metric_overrides(raw: list[str] | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for item in raw or []:
        name, sep, value = item.partition("=")
        try:
            threshold = float(value) if sep and value.strip() else None
        except ValueError:
            raise ConfigError(f"--metric expects name or name=threshold, got {item!r}") from None
        if threshold is not None and not 0 <= threshold <= 1:
            raise ConfigError(f"--metric threshold must be between 0 and 1, got {item!r}")
        out[name.strip()] = threshold
    return out


@app.command("eval")
@handle_errors
def eval_(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    dataset: Annotated[
        Path | None, typer.Option("--dataset", "-d", help="EvaluationDataset JSON (default: evaluation.dataset).")
    ] = None,
    metric: Annotated[
        list[str] | None,
        typer.Option("--metric", "-m", help="Add or override a metric: name or name=threshold. Repeatable."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Results file (default: .agentless/<stage>/eval/<ts>.json).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Evaluate the deployed agent on a dataset (Agent Platform Evaluation Service); exit 1 below a threshold."""
    project, pm = _load(config, stage, param, impersonate)
    if not as_json:
        _header(project, "Evaluating")
    if not _evaluate(project, _provider(project, pm), dataset, _metric_overrides(metric), output, as_json):
        if not as_json:
            typer.secho("✖ evaluation below threshold", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
@handle_errors
def remove(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    yes: YesOpt = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Delete everything agentless created for a stage; pre-existing resources are kept."""
    project, pm = _load(config, stage, param, impersonate)
    _header(project, "Removing")

    def confirm(changeset: ChangeSet) -> bool:
        typer.echo(render(changeset, "Will delete:"))
        if not changeset.pending:
            typer.secho("✔ nothing deployed", fg=typer.colors.GREEN)
            return False
        if yes:
            return True
        if not sys.stdin.isatty():
            raise DeployError("refusing to remove non-interactively without --yes")
        return typer.confirm(f"Delete stage {project.stage} of {project.config.service}?", default=False)

    if _provider(project, pm).remove(confirm):
        typer.secho("✔ removed", fg=typer.colors.GREEN)


@app.command()
@handle_errors
def unlock(
    config: ConfigOpt = Path(DEFAULT_CONFIG_FILE),
    stage: StageOpt = None,
    param: ParamOpt = None,
    yes: YesOpt = False,
    impersonate: ImpersonateOpt = None,
) -> None:
    """Release a stale deployment lock (only when no deploy is running)."""
    project, pm = _load(config, stage, param, impersonate)
    provider = _provider(project, pm)
    if yes or typer.confirm(f"Force-unlock {provider.store.location}?", default=False):
        provider.store.force_unlock()
        typer.secho("✔ unlocked", fg=typer.colors.GREEN)


@app.command()
@handle_errors
def schema(output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Emit the agent.yaml JSON Schema (for IDE autocompletion)."""
    text = json.dumps(AgentConfig.model_json_schema(by_alias=True), indent=2) + "\n"
    if output:
        output.write_text(text, encoding="utf-8")
        typer.secho(f"✔ wrote {output}", fg=typer.colors.GREEN)
    else:
        typer.echo(text)


# Published JSON Schema for IDE completion, served by the docs site.
SCHEMA_URL = "https://aymen2518.github.io/agentless/schema/agent.schema.json"

INIT_TEMPLATE = """\
# yaml-language-server: $schema={schema_url}
service: {service}
frameworkVersion: "1"

provider:
  name: agent-runtime
  stage: ${{opt:stage, 'dev'}}
  project: ${{param:project}}
  region: {region}
  # stagingBucket: ${{tf(gs://<state-bucket>/<prefix>):staging_bucket}}   # shared state for CI; local otherwise
  labels:
    managed-by: agentless
    env: ${{stage}}

stages:
  dev:
    params:
      project: {project}
  # uat:
  #   params: {{ project: my-uat-project }}
  # prod:
  #   params: {{ project: my-prod-project, minInstances: 2 }}

agent:
  description: TODO describe what the agent does
  runtime:
    cpu: "1"
    memory: 4Gi
    minInstances: ${{param:minInstances, 1}}
    maxInstances: 10
    concurrency: 8
  environment: {{}}
  secrets: {{}}
  #   API_TOKEN: {{ secret: my-secret, version: latest }}

identity:
  type: serviceAccount
  serviceAccount:
    create: true
    name: {sa}-${{stage}}
  roles:
    project:
      - roles/aiplatform.user
      - roles/logging.logWriter
      - roles/cloudtrace.agent
      - roles/serviceusage.serviceUsageConsumer

# memory:
#   memoryBank: {{ generationModel: gemini-2.5-flash, embeddingModel: text-embedding-005, ttl: 2592000s }}
# network:
#   pscInterface: {{ networkAttachment: projects/<host>/regions/{region}/networkAttachments/<name> }}
# publish:
#   geminiEnterprise:
#     app: projects/<number>/locations/global/collections/default_collection/engines/<app-id>
"""


if __name__ == "__main__":
    app()
