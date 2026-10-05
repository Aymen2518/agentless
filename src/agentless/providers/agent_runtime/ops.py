"""Day-2 operations on a deployed engine: logs and invoke."""

from __future__ import annotations

import datetime
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

_RESOURCE_TYPE = "aiplatform.googleapis.com/ReasoningEngine"


def log_filter(engine_name: str, since: datetime.datetime, severity: str | None) -> str:
    """Cloud Logging filter for one engine."""
    engine_id = engine_name.rsplit("/", 1)[-1]
    parts = [
        f'resource.type="{_RESOURCE_TYPE}"',
        f'resource.labels.reasoning_engine_id="{engine_id}"',
        f'timestamp>="{since.isoformat()}"',
    ]
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
