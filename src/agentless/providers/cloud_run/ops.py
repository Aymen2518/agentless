"""Day-2 operations on a deployed Cloud Run agent: logs, metrics, console links and invoke."""

from __future__ import annotations

import datetime
import json
import urllib.parse
from collections.abc import Iterator
from typing import Any

from agentless.providers.agent_runtime.ops import request_metrics
from agentless.providers.cloud_run.clients import id_token_session

_METRIC_PREFIX = "run.googleapis.com/"


def log_filter(service_name: str, since: datetime.datetime | None, severity: str | None) -> str:
    """Cloud Logging filter for one service."""
    service_id = service_name.rsplit("/", 1)[-1]
    parts = ['resource.type="cloud_run_revision"', f'resource.labels.service_name="{service_id}"']
    if since:
        parts.append(f'timestamp>="{since.isoformat()}"')
    if severity:
        parts.append(f"severity>={severity.upper()}")
    return " AND ".join(parts)


def console_links(project: str, region: str, service_name: str) -> dict[str, str]:
    """Cloud Console pages for one service."""
    service_id = service_name.rsplit("/", 1)[-1]
    query = urllib.parse.quote(log_filter(service_name, None, None), safe="")
    return {
        "console": f"https://console.cloud.google.com/run/detail/{region}/{service_id}?project={project}",
        "logs": f"https://console.cloud.google.com/logs/query;query={query}?project={project}",
        "traces": f"https://console.cloud.google.com/traces/explorer?project={project}",
    }


def metrics(
    project: str, service_name: str, *, since: datetime.timedelta, credentials: Any, session: Any = None
) -> dict[str, Any]:
    """Request count by response class, 5xx error rate and p50/p95 latency of the service."""
    service_id = service_name.rsplit("/", 1)[-1]
    data = request_metrics(
        project,
        _METRIC_PREFIX,
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service_id}"',
        since=since,
        credentials=credentials,
        session=session,
    )
    return {"service": service_name, **data}


def invoke(
    url: str,
    app_name: str,
    message: str,
    *,
    user_id: str,
    session_id: str | None,
    credentials: Any,
    app_header: str | None = None,
    app_token: str | None = None,
    http: Any = None,
) -> Iterator[dict[str, Any]]:
    """Stream ADK events from the agent's `/run_sse`, creating a session when none is given.

    Every request carries a Google ID token: the service only answers callers holding roles/run.invoker. With
    auth: googleToken, `app_header` also carries `app_token` (default: that ID token) for the container's check.
    With auth: webhook, pass `http` from webhook_session: the shared secret instead of a Google token.
    """
    http = http or id_token_session(credentials, url, app_header=app_header, app_token=app_token)
    base = url.rstrip("/")
    if not session_id:
        resp = http.post(f"{base}/apps/{app_name}/users/{user_id}/sessions", json={}, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"creating a session failed ({resp.status_code}): {resp.text}")
        session_id = resp.json()["id"]
    yield {"session_id": session_id}
    payload = {
        "app_name": app_name,
        "user_id": user_id,
        "session_id": session_id,
        "new_message": {"role": "user", "parts": [{"text": message}]},
        "streaming": False,
    }
    with http.post(f"{base}/run_sse", json=payload, stream=True, timeout=300) as resp:
        if not resp.ok:
            raise RuntimeError(f"run_sse failed ({resp.status_code}): {resp.text}")
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                try:
                    yield json.loads(line.removeprefix("data:").strip())
                except json.JSONDecodeError:
                    continue
