"""Built-in variable sources: self, stage, opt, param, env, file, secret, tf."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

import yaml

from agentless.config.variables import Missing, SourceContext, SourceFn, VariableError, dig


def build_sources(*, root: Path, stage: str, options: dict[str, Any], params: dict[str, Any]) -> dict[str, SourceFn]:
    """Return the built-in source table for one load.

    Args:
        root: Directory of agent.yaml, base for relative file paths.
        stage: The selected stage.
        options: CLI options exposed through `${opt:...}`.
        params: CLI `--param` overrides, highest priority for `${param:...}`.
    """

    def self_(ctx: SourceContext, _arg: str | None, key: str) -> Any:
        return ctx.resolver.get(key)

    def stage_(_ctx: SourceContext, _arg: str | None, _key: str) -> Any:
        return stage

    def opt(_ctx: SourceContext, _arg: str | None, key: str) -> Any:
        if options.get(key) is None:
            raise Missing(f"option --{key} not set")
        return options[key]

    def param(ctx: SourceContext, _arg: str | None, key: str) -> Any:
        if key in params:
            return params[key]
        for scope in (stage, "default"):
            try:
                return ctx.resolver.get(f"stages.{scope}.params.{key}")
            except Missing:
                continue
        raise Missing(f"param {key!r} not defined for stage {stage!r} or default")

    def env(_ctx: SourceContext, _arg: str | None, key: str) -> Any:
        if key not in os.environ:
            raise Missing(f"environment variable {key} not set")
        return os.environ[key]

    def file(ctx: SourceContext, arg: str | None, key: str) -> Any:
        if not arg:
            raise VariableError(ctx.path, "file source needs a path: ${file(./path.yml):key}")
        path = (root / arg).resolve()
        if not path.is_file():
            raise Missing(f"file {arg} not found")
        return dig(_load_document(path), key, arg)

    def secret(ctx: SourceContext, _arg: str | None, key: str) -> Any:
        name = key
        if not name.startswith("projects/"):
            secret_id, _, version = key.partition("@")
            name = f"projects/{ctx.resolver.get('provider.project')}/secrets/{secret_id}/versions/{version or 'latest'}"
        return _access_secret(name)

    def tf(ctx: SourceContext, arg: str | None, key: str) -> Any:
        if not arg or not arg.startswith("gs://"):
            raise VariableError(ctx.path, "tf source needs a GCS state path: ${tf(gs://bucket/prefix):output}")
        outputs = _read_tf_outputs(arg, _project_or_none(ctx))
        if key not in outputs:
            raise Missing(f"output {key!r} not in {arg}")
        return outputs[key]["value"]

    return {
        "self": self_,
        "stage": stage_,
        "opt": opt,
        "param": param,
        "env": env,
        "file": file,
        "secret": secret,
        "tf": tf,
    }


def _project_or_none(ctx: SourceContext) -> str | None:
    """Target project for client quota, unless the variable being resolved is the project itself."""
    if ctx.path == "provider.project":
        return None
    try:
        return str(ctx.resolver.get("provider.project"))
    except (Missing, VariableError):
        return None


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


@functools.cache
def _access_secret(name: str) -> str:
    from google.api_core import exceptions
    from google.cloud import secretmanager

    try:
        response = secretmanager.SecretManagerServiceClient().access_secret_version(name=name)
    except exceptions.NotFound:
        raise Missing(f"secret {name} not found") from None
    return response.payload.data.decode("utf-8")


@functools.cache
def _read_tf_outputs(location: str, project: str | None) -> dict[str, Any]:
    from google.api_core import exceptions
    from google.cloud import storage

    path = location.removeprefix("gs://").rstrip("/")
    if not path.endswith(".tfstate"):
        path = f"{path}/default.tfstate"
    bucket, _, blob = path.partition("/")
    try:
        state = json.loads(storage.Client(project=project).bucket(bucket).blob(blob).download_as_bytes())
    except exceptions.NotFound:
        raise Missing(f"terraform state gs://{path} not found") from None
    return state.get("outputs", {})
