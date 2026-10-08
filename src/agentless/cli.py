"""`agentless` command line: the Serverless-style lifecycle for Agent Runtime agents."""

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
from agentless.hooks import plugin_manager, variable_sources
from agentless.package.packager import PackageError, write_archive
from agentless.plan.model import Action, ChangeSet, DeployOptions
from agentless.plan.render import render
from agentless.providers.agent_runtime import ops
from agentless.providers.agent_runtime.provider import AgentRuntimeProvider, DeployError
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


def _provider(project: Project, pm: Any) -> AgentRuntimeProvider:
    return AgentRuntimeProvider(project, hooks=pm, echo=typer.echo)


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
    impersonate: ImpersonateOpt = None,
) -> None:
    """Reconcile GCP with agent.yaml: identity, IAM, engine, memory, networking, Gemini Enterprise."""
    project, pm = _load(config, stage, param, impersonate)
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


def _engine_name(provider: AgentRuntimeProvider) -> str:
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
    """Print the engine's Cloud Logging entries."""
    project, pm = _load(config, stage, param, impersonate)
    window = _window(since)
    ops.read_logs(
        project.config.provider.project,
        _engine_name(_provider(project, pm)),
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
    data = ops.metrics(
        project.config.provider.project,
        _engine_name(_provider(project, pm)),
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
    url = ops.console_links(cfg.project, cfg.region, _engine_name(_provider(project, pm)))[target]
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
    impersonate: ImpersonateOpt = None,
) -> None:
    """Send a message to the deployed agent and stream its answer."""
    project, pm = _load(config, stage, param, impersonate)
    cfg = project.config
    for event in ops.invoke(
        cfg.provider.region,
        _engine_name(_provider(project, pm)),
        message,
        user_id=user,
        session_id=session,
        project=cfg.provider.project,
        credentials=project.deployer.credentials(),
    ):
        if "session_id" in event and len(event) == 1:
            typer.secho(f"session {event['session_id']}", dim=True)
        elif raw:
            typer.echo(json.dumps(event))
        else:
            author, text = ops.event_text(event)
            if text:
                typer.echo(f"[{author}] {text}")


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
