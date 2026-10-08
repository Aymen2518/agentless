"""Day-2 operations on a deployed engine: logs, metrics, console links and invoke."""

from __future__ import annotations

import datetime
import json
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

_RESOURCE_TYPE = "aiplatform.googleapis.com/ReasoningEngine"


def log_filter(engine_name: str, since: datetime.datetime | None, severity: str | None) -> str:
    """Cloud Logging filter for one engine."""
    engine_id = engine_name.rsplit("/", 1)[-1]
    parts = [f'resource.type="{_RESOURCE_TYPE}"', f'resource.labels.reasoning_engine_id="{engine_id}"']
    if since:
        parts.append(f'timestamp>="{since.isoformat()}"')
    if severity:
        parts.append(f"severity>={severity.upper()}")
    return " AND ".join(parts)


def read_logs(
    project: str,
    engine_name: str,
    *,
    since: datetime.timedelta,
    severity: str | None,
    tail: bool,
    echo: Callable[[str], None],
    credentials: Any,
) -> None:
    """Print engine logs, optionally following new entries."""
    from google.cloud import logging as cloud_logging

    client = cloud_logging.Client(project=project, credentials=credentials)
    start = datetime.datetime.now(tz=datetime.UTC) - since
    seen: set[str] = set()
    while True:
        entries = client.list_entries(
            filter_=log_filter(engine_name, start, severity), order_by=cloud_logging.ASCENDING, page_size=500
        )
        for entry in entries:
            if entry.insert_id in seen:
                continue
            seen.add(entry.insert_id)
            payload = entry.payload if isinstance(entry.payload, str) else json.dumps(entry.payload, default=str)
            echo(f"{entry.timestamp:%Y-%m-%d %H:%M:%S} {entry.severity or 'DEFAULT':<8} {payload}")
            start = max(start, entry.timestamp)
        if not tail:
            return
        time.sleep(5)


def invoke(
    region: str,
    engine_name: str,
    message: str,
    *,
    user_id: str,
    session_id: str | None,
    project: str,
    credentials: Any,
) -> Iterator[dict[str, Any]]:
    """Stream ADK events from `:streamQuery`, creating a session when none is given (same calls as agents-cli)."""
    from google.auth.transport.requests import AuthorizedSession

    http = AuthorizedSession(credentials)
    http.headers["X-Goog-User-Project"] = project
    base = f"https://{region}-aiplatform.googleapis.com/v1/{engine_name}"
    if not session_id:
        resp = http.post(
            f"{base}:query", json={"class_method": "async_create_session", "input": {"user_id": user_id}}, timeout=60
        )
        resp.raise_for_status()
        session_id = resp.json()["output"]["id"]
    yield {"session_id": session_id}
    payload = {
        "class_method": "async_stream_query",
        "input": {"user_id": user_id, "session_id": session_id, "message": message},
    }
    with http.post(f"{base}:streamQuery", json=payload, stream=True, timeout=300) as resp:
        if not resp.ok:
            raise RuntimeError(f"streamQuery failed ({resp.status_code}): {resp.text}")
        for line in resp.iter_lines(decode_unicode=True):
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def event_text(event: dict[str, Any]) -> tuple[str | None, str]:
    """(author, concatenated text parts) of an ADK event."""
    parts = (event.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    calls = [p["function_call"]["name"] for p in parts if isinstance(p, dict) and p.get("function_call")]
    if calls:
        text += "".join(f"[tool call: {c}]" for c in calls)
    return event.get("author"), text


_MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
_METRIC_PREFIX = "aiplatform.googleapis.com/reasoning_engine/"
CONSOLE_TARGETS = ("console", "logs", "traces")


def console_links(project: str, region: str, engine_name: str) -> dict[str, str]:
    """Cloud Console pages for one engine. Traces are project-wide: the Trace explorer can't be pre-filtered by URL."""
    engine_id = engine_name.rsplit("/", 1)[-1]
    query = urllib.parse.quote(log_filter(engine_name, None, None), safe="")
    return {
        "console": f"https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/{region}"
        f"/agent-engines/{engine_id}?project={project}",
        "logs": f"https://console.cloud.google.com/logs/query;query={query}?project={project}",
        "traces": f"https://console.cloud.google.com/traces/explorer?project={project}",
    }


def metrics(
    project: str, engine_name: str, *, since: datetime.timedelta, credentials: Any, session: Any = None
) -> dict[str, Any]:
    """Request count by response class, 5xx error rate and p50/p95 latency over the window (Cloud Monitoring)."""
    from google.auth.transport.requests import AuthorizedSession

    http = session or AuthorizedSession(credentials)
    end = datetime.datetime.now(tz=datetime.UTC)
    seconds = max(60, int(since.total_seconds()))
    engine_id = engine_name.rsplit("/", 1)[-1]

    def query(metric: str, aligner: str, reducer: str, group_by: str | None = None) -> list[dict[str, Any]]:
        params = {
            "filter": f'metric.type="{_METRIC_PREFIX}{metric}" AND resource.labels.reasoning_engine_id="{engine_id}"',
            "interval.startTime": (end - datetime.timedelta(seconds=seconds)).isoformat(),
            "interval.endTime": end.isoformat(),
            "aggregation.alignmentPeriod": f"{seconds}s",
            "aggregation.perSeriesAligner": aligner,
            "aggregation.crossSeriesReducer": reducer,
        }
        if group_by:
            params["aggregation.groupByFields"] = group_by
        response = http.get(_MONITORING_URL.format(project=project), params=params, timeout=60)
        response.raise_for_status()
        return response.json().get("timeSeries", [])

    by_class: dict[str, int] = {}
    for series in query("request_count", "ALIGN_SUM", "REDUCE_SUM", "metric.labels.response_code_class"):
        cls = series.get("metric", {}).get("labels", {}).get("response_code_class", "unknown")
        by_class[cls] = by_class.get(cls, 0) + sum(int(pt["value"].get("int64Value", 0)) for pt in series["points"])
    total = sum(by_class.values())

    latency: dict[str, float | None] = {}
    for pct in (50, 95):
        series = query("request_latencies", "ALIGN_DELTA", f"REDUCE_PERCENTILE_{pct:02d}")
        points = series[0]["points"] if series and series[0].get("points") else []
        value = points[0]["value"].get("doubleValue") if points else None
        latency[f"p{pct}"] = _to_ms(value, series[0].get("unit", "ms")) if value is not None else None

    return {
        "engine": engine_name,
        "window": f"{seconds}s",
        "requests": total,
        "byClass": dict(sorted(by_class.items())),
        "errorRate": (by_class.get("5xx", 0) / total) if total else None,
        "latencyMs": latency,
    }


def _to_ms(value: float, unit: str) -> float:
    return value * {"s": 1000.0, "us": 0.001, "ns": 0.000001}.get(unit, 1.0)
