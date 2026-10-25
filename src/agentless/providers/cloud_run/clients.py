"""GCP calls of a Cloud Run deployment: Cloud Run Admin v2, Cloud Build and Artifact Registry (REST).

The shared service account, IAM, bucket, engine and publishing calls are inherited from GcpClients.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentless.providers.agent_runtime.clients import _IAM_RETRIES, Binding, GcpClients, merge_policy

_RUN = "https://run.googleapis.com/v2"
_BUILD = "https://cloudbuild.googleapis.com/v1"
_AR = "https://artifactregistry.googleapis.com/v1"
_BUILDER = "gcr.io/cloud-builders/docker"
_BUILD_DONE = ("SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED")


class CloudRunClients(GcpClients):
    """Real GCP implementation for the Cloud Run target."""

    # --- Cloud Run services ----------------------------------------------------------------------------------------

    def run_service_get(self, name: str) -> dict[str, Any] | None:
        """The service, or None when it does not exist."""
        return self._get_or_none(f"{_RUN}/{name}")

    def run_service_create(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create the service and wait until its first revision serves; returns the service."""
        parent, _, service_id = name.rpartition("/services/")
        operation = self._rest("POST", f"{_RUN}/{parent}/services", params={"serviceId": service_id}, json=body)
        return self._run_lro(operation)

    def run_service_update(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace the service's config (a new revision) and wait until it serves; returns the service."""
        return self._run_lro(self._rest("PATCH", f"{_RUN}/{name}", json=body))

    def run_service_delete(self, name: str) -> None:
        """Delete the service, waiting for the operation."""
        operation = self._rest("DELETE", f"{_RUN}/{name}")
        if operation:
            self._run_lro(operation)

    def _run_lro(self, operation: dict[str, Any], poll: float = 3, timeout: float = 900) -> dict[str, Any]:
        start = time.monotonic()
        while not operation.get("done"):
            if time.monotonic() - start > timeout:
                raise RuntimeError(f"operation {operation['name']} still running after {int(timeout)}s")
            time.sleep(poll)
            operation = self._rest("GET", f"{_RUN}/{operation['name']}")
        if operation.get("error"):
            raise RuntimeError(f"operation {operation['name']} failed: {operation['error']}")
        return operation.get("response") or {}

    # --- IAM on services -------------------------------------------------------------------------------------------

    def iam_members(self, rtype: str, name: str) -> dict[str, set[str]] | None:  # noqa: D102
        if rtype != "runService":
            return super().iam_members(rtype, name)
        policy = self._run_policy(name)
        if policy is None:
            return None
        return {b["role"]: set(b.get("members", [])) for b in policy.get("bindings", []) if not b.get("condition")}

    def iam_modify(self, rtype: str, name: str, add: Iterable[Binding], remove: Iterable[Binding]) -> None:  # noqa: D102
        if rtype != "runService":
            super().iam_modify(rtype, name, add, remove)
            return
        add, remove = list(add), list(remove)
        if not add and not remove:
            return
        url = f"{_RUN}/{name}:setIamPolicy"
        for attempt in range(_IAM_RETRIES):
            policy = self._run_policy(name)
            if policy is None:
                raise RuntimeError(f"404 Cloud Run service {name} not found")
            merge_policy(policy, add, remove)
            response = self._http.post(url, json={"policy": policy}, timeout=60)
            if response.ok:
                return
            if response.status_code not in (409, 412) or attempt == _IAM_RETRIES - 1:
                raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text}")
            time.sleep(2**attempt)

    def _run_policy(self, name: str) -> dict[str, Any] | None:
        url = f"{_RUN}/{name}:getIamPolicy"
        response = self._http.get(url, params={"options.requestedPolicyVersion": 3}, timeout=60)
        if response.status_code == 404:
            return None
        if response.status_code == 403 and self.run_service_get(name) is None:
            return None
        if not response.ok:
            raise RuntimeError(f"GET {url} -> {response.status_code}: {response.text}")
        return response.json()

    # --- Artifact Registry and Cloud Build -------------------------------------------------------------------------

    def ar_repository_exists(self, repository: str) -> bool:
        """Whether the Docker repository exists in the target project and region."""
        url = f"{_AR}/projects/{self.project}/locations/{self.region}/repositories/{repository}"
        return self._get_or_none(url) is not None

    def ar_image_digest(self, image: str) -> str | None:
        """Digest a `<region>-docker.pkg.dev/<project>/<repo>/<package>:<tag>` image points at; None if untagged."""
        path, _, tag = image.rpartition(":")
        _, project, repository, package = path.split("/", 3)
        quoted = urllib.parse.quote(package, safe="")
        url = f"{_AR}/projects/{project}/locations/{self.region}/repositories/{repository}/packages/{quoted}/tags/{tag}"
        found = self._get_or_none(url)
        return found["version"].rsplit("/", 1)[-1] if found and found.get("version") else None

    def upload_source(self, bucket: str, name: str, archive: Path) -> None:
        """Upload the source tarball Cloud Build reads."""
        self._storage.bucket(bucket).blob(name).upload_from_filename(str(archive), content_type="application/gzip")

    def delete_objects(self, bucket: str, prefix: str) -> None:
        """Delete every object under a prefix (the uploaded source tarballs)."""
        for blob in self._storage.list_blobs(bucket, prefix=prefix):
            blob.delete()

    def cloud_build(
        self, source: str, image: str, build_args: dict[str, str], service_account: str | None, tags: list[str]
    ) -> str:
        """Build the Dockerfile of a `gs://bucket/object` tarball, push `image`, wait; returns the image digest."""
        bucket, _, obj = source.removeprefix("gs://").partition("/")
        args = ["build", "-t", image]
        for key, value in sorted(build_args.items()):
            args += ["--build-arg", f"{key}={value}"]
        body: dict[str, Any] = {
            "source": {"storageSource": {"bucket": bucket, "object": obj}},
            "steps": [{"name": _BUILDER, "args": [*args, "."]}],
            "images": [image],
            "tags": tags,
            "options": {"logging": "CLOUD_LOGGING_ONLY"},
        }
        if service_account:
            body["serviceAccount"] = f"projects/{self.project}/serviceAccounts/{service_account}"
        parent = f"projects/{self.project}/locations/{self.region}"
        operation = self._rest("POST", f"{_BUILD}/{parent}/builds", json=body)
        build = (operation.get("metadata") or {}).get("build") or {}
        log_url = build.get("logUrl")
        # Poll the build, not the operation: a regional build's operation name isn't served by the operations API.
        start = time.monotonic()
        while build.get("status") not in _BUILD_DONE:
            if time.monotonic() - start > 3600:
                raise RuntimeError(f"build still running after 1h: {log_url}")
            time.sleep(5)
            build = self._rest("GET", f"{_BUILD}/{parent}/builds/{build['id']}")
        if build.get("status") != "SUCCESS":
            detail = build.get("statusDetail") or build.get("status")
            raise RuntimeError(f"Cloud Build failed: {detail} (logs: {log_url})")
        images = (build.get("results") or {}).get("images") or []
        digest = next((i.get("digest") for i in images if i.get("name") == image), None)
        if not digest:
            raise RuntimeError(f"Cloud Build pushed no digest for {image}")
        return digest

    # --- calling the service ---------------------------------------------------------------------------------------

    def a2a_card(self, url: str) -> dict[str, Any] | None:
        """Agent card of an IAM-protected service, fetched with an ID token; None while it isn't served."""
        import requests

        try:
            response = id_token_session(self._credentials, url).get(url, timeout=60)
        except requests.ConnectionError:
            return None
        if response.status_code == 404 or response.status_code >= 500:
            return None
        if not response.ok:
            raise RuntimeError(f"GET {url} -> {response.status_code}: {response.text}")
        return response.json()


def audience(url: str) -> str:
    """ID-token audience Cloud Run expects: the URL origin."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def id_token_session(credentials: Any, url: str) -> Any:
    """HTTP session that sends a Google ID token for the service at `url`. Never falls back to anonymous calls.

    Impersonated deployers mint the token through the IAM Credentials API and service account keys sign their own.
    User credentials (`gcloud auth application-default login`) send the ID token their refresh returns, which Cloud
    Run accepts for user accounts, like `gcloud auth print-identity-token`. Otherwise the metadata server mints one.
    """
    import google.auth.exceptions
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2 import credentials as user_credentials
    from google.oauth2 import id_token

    target = audience(url)
    if isinstance(credentials, impersonated_credentials.Credentials):
        token_credentials: Any = impersonated_credentials.IDTokenCredentials(
            credentials, target_audience=target, include_email=True
        )
        return AuthorizedSession(token_credentials)
    if hasattr(credentials, "with_target_audience"):  # service account key
        return AuthorizedSession(credentials.with_target_audience(target))
    token = None
    if isinstance(credentials, user_credentials.Credentials):
        credentials.refresh(Request())
        token = credentials.id_token
    if not token:
        try:
            token = id_token.fetch_id_token(Request(), target)  # metadata server (CI on GCP, Cloud Build, GCE)
        except google.auth.exceptions.DefaultCredentialsError as e:
            raise RuntimeError(
                "cannot get an ID token to call the IAM-protected Cloud Run service; log in with "
                "`gcloud auth application-default login` or set provider.deployer.impersonate"
            ) from e
    import requests

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return session
