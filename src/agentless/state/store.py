"""Deployment state: what agentless created and last applied, per service and stage."""

from __future__ import annotations

import contextlib
import datetime
import getpass
import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

STATE_VERSION = 1


class StateError(Exception):
    """State could not be read, written or locked."""


class LockedError(StateError):
    """Another deployment holds the lock."""


@dataclass
class State:
    """Serializable deployment state."""

    service: str
    stage: str
    project: str
    region: str
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: str | None = None
    updated_by: str | None = None
    version: int = STATE_VERSION

    def to_json(self) -> str:
        """Serialize to stable, diff-friendly JSON."""
        return json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> State:
        """Deserialize, rejecting newer formats."""
        data = json.loads(text)
        if data.get("version", 1) > STATE_VERSION:
            raise StateError(f"state version {data['version']} is newer than this agentless supports")
        return cls(**data)


class StateStore(Protocol):
    """Backend persisting state and serialising deployments."""

    location: str

    def read(self) -> State | None:
        """Return state or None when nothing was deployed yet."""
        ...

    def write(self, state: State) -> None:
        """Persist state."""
        ...

    def delete(self) -> None:
        """Drop state after a full remove."""
        ...

    def lock(self, operation: str) -> contextlib.AbstractContextManager[None]:
        """Exclusive lock for the duration of an apply."""
        ...

    def force_unlock(self) -> None:
        """Remove a stale lock."""
        ...


def _ci_job_url() -> str | None:
    """Link to the CI job holding the lock (GitLab CI or GitHub Actions)."""
    if os.environ.get("CI_JOB_URL"):
        return os.environ["CI_JOB_URL"]
    if os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return None


def _holder(operation: str) -> str:
    return json.dumps(
        {
            "operation": operation,
            "who": f"{getpass.getuser()}@{socket.gethostname()}",
            "ci_job": _ci_job_url(),
            "since": _now(),
        }
    )


def _now() -> str:
    return datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")


def _stamp(state: State) -> None:
    state.updated_at = _now()
    state.updated_by = os.environ.get("GITLAB_USER_LOGIN") or os.environ.get("GITHUB_ACTOR") or getpass.getuser()


class GcsStateStore:
    """State at gs://<bucket>/agentless/<service>/<stage>/state.json, locked via generation preconditions."""

    def __init__(self, bucket: str, service: str, stage: str, *, project: str | None = None, client: Any = None):
        from google.cloud import storage

        self._bucket = (client or storage.Client(project=project)).bucket(bucket)
        prefix = f"agentless/{service}/{stage}"
        self._state = self._bucket.blob(f"{prefix}/state.json")
        self._lock = self._bucket.blob(f"{prefix}/lock.json")
        self.location = f"gs://{bucket}/{prefix}/state.json"

    def read(self) -> State | None:  # noqa: D102
        from google.api_core import exceptions

        try:
            return State.from_json(self._state.download_as_text())
        except exceptions.NotFound:
            return None

    def write(self, state: State) -> None:  # noqa: D102
        _stamp(state)
        self._state.upload_from_string(state.to_json(), content_type="application/json")

    def delete(self) -> None:  # noqa: D102
        from google.api_core import exceptions

        with contextlib.suppress(exceptions.NotFound):
            self._state.delete()

    @contextlib.contextmanager
    def lock(self, operation: str) -> Iterator[None]:  # noqa: D102
        from google.api_core import exceptions

        try:
            self._lock.upload_from_string(_holder(operation), if_generation_match=0)
        except exceptions.PreconditionFailed:
            holder = self._lock.download_as_text() if self._lock.exists() else "unknown"
            raise LockedError(f"{self.location} is locked by {holder} (use `agentless unlock` if stale)") from None
        try:
            yield
        finally:
            with contextlib.suppress(exceptions.NotFound):
                self._lock.delete()

    def force_unlock(self) -> None:  # noqa: D102
        from google.api_core import exceptions

        with contextlib.suppress(exceptions.NotFound):
            self._lock.delete()


class LocalStateStore:
    """State in `.agentless/<stage>/state.json` next to agent.yaml; for local experiments only."""

    def __init__(self, root: Path, stage: str):
        self._dir = root / ".agentless" / stage
        self._state = self._dir / "state.json"
        self._lock = self._dir / "lock.json"
        self.location = str(self._state)

    def read(self) -> State | None:  # noqa: D102
        return State.from_json(self._state.read_text(encoding="utf-8")) if self._state.is_file() else None

    def write(self, state: State) -> None:  # noqa: D102
        _stamp(state)
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state.with_suffix(".tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        tmp.replace(self._state)

    def delete(self) -> None:  # noqa: D102
        self._state.unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self, operation: str) -> Iterator[None]:  # noqa: D102
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockedError(
                f"{self._lock} exists: {self._lock.read_text()} (use `agentless unlock` if stale)"
            ) from None
        with os.fdopen(fd, "w") as fh:
            fh.write(_holder(operation))
        try:
            yield
        finally:
            self._lock.unlink(missing_ok=True)

    def force_unlock(self) -> None:  # noqa: D102
        self._lock.unlink(missing_ok=True)
