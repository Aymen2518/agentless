"""Evaluation of a deployed engine: metric names, evaluation runs and their verdict against thresholds."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_VERSION_RE = re.compile(r"_v\d+$")
_DONE = {"SUCCEEDED", "FAILED", "CANCELLED"}


def metric_spec_name(name: str) -> str:
    """API spec name of a predefined metric: `safety` → latest (`safety_v3`), `safety_v1` stays as is."""
    from vertexai._genai import _evals_constant as known

    base = name.strip().lower()
    if base in known.SUPPORTED_PREDEFINED_METRICS:
        return base
    if base in known.METRIC_LATEST_SPEC_NAME:
        return known.METRIC_LATEST_SPEC_NAME[base]
    if f"{base}_v1" in known.SUPPORTED_PREDEFINED_METRICS:
        return f"{base}_v1"
    choices = sorted(known.METRIC_LATEST_SPEC_NAME)
    raise ValueError(f"unknown evaluation metric {name!r}; predefined metrics: {', '.join(choices)}")


def _base(name: str) -> str:
    return _VERSION_RE.sub("", name.lower())


@dataclass
class MetricResult:
    """Average score of one metric and its threshold; `passed` is None when the metric only reports."""

    name: str
    score: float | None
    threshold: float | None

    @property
    def passed(self) -> bool | None:
        """True at or above the threshold, False below it or without a score, None without a threshold."""
        if self.threshold is None:
            return None
        return self.score is not None and self.score >= self.threshold


def verdict(summary: dict[str, Any], thresholds: dict[str, float | None]) -> list[MetricResult]:
    """Match `<candidate>/<metric>/AVERAGE` summary keys to the configured metrics, by name without version."""
    averages: dict[str, float] = {}
    for key, value in summary.items():
        path, _, aggregate = key.rpartition("/")
        if aggregate == "AVERAGE" and isinstance(value, int | float):
            averages[_base(path.rsplit("/", 1)[-1])] = float(value)
    return [MetricResult(name, averages.get(_base(name)), threshold) for name, threshold in thresholds.items()]


def run(
    *,
    project: str,
    region: str,
    engine: str,
    dataset: dict[str, Any],
    dest: str,
    metrics: list[str],
    credentials: Any,
    display_name: str,
    timeout: float = 3600,
    poll: float = 15,
    on_tick: Callable[[str, float], None] | None = None,
) -> dict[str, Any]:
    """Run inference on the deployed engine and grade it server-side (Agent Platform Evaluation Service).

    Returns:
        {"name", "state", "summary"}: the run resource name, its final state and its summary metrics.
    """
    try:
        from vertexai import Client, types
        from vertexai._genai import evals as _  # noqa: F401  (needs the evaluation extra: pandas, …)
    except ImportError as e:
        raise RuntimeError(
            "evaluation needs extra dependencies: install agentless-cli[eval] "
            "(or google-cloud-aiplatform[evaluation]) next to agentless"
        ) from e

    client = Client(project=project, location=region, credentials=credentials)
    evaluation = client.evals.create_evaluation_run(
        dataset=types.EvaluationDataset.model_validate(dataset),
        dest=dest,
        metrics=[metric_spec_name(m) for m in metrics],
        agent=engine,
        display_name=display_name,
        labels={"managed-by": "agentless"},
    )
    start = time.monotonic()
    while True:
        evaluation = client.evals.get_evaluation_run(name=evaluation.name)
        state = str(getattr(evaluation.state, "value", evaluation.state) or "UNSPECIFIED")
        if state in _DONE:
            break
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise RuntimeError(f"evaluation run {evaluation.name} still {state} after {int(timeout)}s")
        if on_tick:
            on_tick(state, elapsed)
        time.sleep(poll)
    if state != "SUCCEEDED":
        raise RuntimeError(f"evaluation run {evaluation.name} {state.lower()}: {evaluation.error}")
    results = evaluation.evaluation_run_results
    summary = (results.summary_metrics.metrics if results and results.summary_metrics else None) or {}
    return {"name": evaluation.name, "state": state, "summary": dict(summary)}
