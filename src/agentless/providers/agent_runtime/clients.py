"""Thin facade over every GCP API agentless calls, so resources stay testable with a fake."""

from __future__ import annotations

import contextlib
import functools
import json
import time
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agentless.auth import Deployer

_GE_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/static/oauth/oauth.html"
_IAM_RETRIES = 5

Binding = tuple[str, str]  # (role, member)


class GcpClients:
    """Real GCP implementation; every SDK import is lazy to keep the CLI fast."""

    def __init__(self, project: str, region: str, deployer: Deployer | None = None):
        self.project = project
        self.region = region
        self.deployer = deployer or Deployer()

    @functools.cached_property
    def _credentials(self) -> Any:
        return self.deployer.credentials()

    # --- project -------------------------------------------------------------------------------------------------

    @functools.cache  # noqa: B019
    def project_number(self) -> str:
        """Numeric id of the target project."""
        from google.cloud import resourcemanager_v3

        client = resourcemanager_v3.ProjectsClient(credentials=self._credentials)
        name = client.get_project(name=f"projects/{self.project}").name
        return name.split("/")[1]

    # --- service accounts ------------------------------------------------------------------------------------------

    @functools.cached_property
    def _iam_admin(self) -> Any:
        from google.cloud import iam_admin_v1

        return iam_admin_v1.IAMClient(credentials=self._credentials)

    def get_service_account(self, email: str) -> dict[str, Any] | None:
        """Service account by email, or None."""
        from google.api_core import exceptions

        try:
            sa = self._iam_admin.get_service_account(name=f"projects/-/serviceAccounts/{email}")
        except exceptions.NotFound:
            return None
        return {"email": sa.email, "display_name": sa.display_name, "unique_id": sa.unique_id}

    def create_service_account(self, account_id: str, display_name: str, description: str) -> str:
        """Create a service account and return its email."""
        from google.cloud import iam_admin_v1

        sa = self._iam_admin.create_service_account(
            request=iam_admin_v1.CreateServiceAccountRequest(
                name=f"projects/{self.project}",
                account_id=account_id,
                service_account=iam_admin_v1.ServiceAccount(display_name=display_name, description=description),
            )
        )
        return sa.email

    def update_service_account_display_name(self, email: str, display_name: str) -> None:
        """Patch the display name."""
        from google.cloud import iam_admin_v1

        self._iam_admin.patch_service_account(
            request=iam_admin_v1.PatchServiceAccountRequest(
                service_account=iam_admin_v1.ServiceAccount(
                    name=f"projects/{self.project}/serviceAccounts/{email}", display_name=display_name
                ),
                update_mask={"paths": ["display_name"]},
            )
        )

    def delete_service_account(self, email: str) -> None:
        """Delete a service account; missing is fine."""
        from google.api_core import exceptions

        with contextlib.suppress(exceptions.NotFound):
            self._iam_admin.delete_service_account(name=f"projects/-/serviceAccounts/{email}")

    # --- IAM -----------------------------------------------------------------------------------------------------

    def iam_members(self, rtype: str, name: str) -> dict[str, set[str]] | None:
        """Unconditional role → members of a resource's IAM policy; None when the resource does not exist."""
        from google.api_core import exceptions

        if rtype == "reasoningEngine":
            policy = self._engine_policy(name)
            if policy is None:
                return None
            return {b["role"]: set(b.get("members", [])) for b in policy.get("bindings", []) if not b.get("condition")}
        try:
            return self._iam_members(rtype, name)
        except exceptions.NotFound:
            return None

    def _engine_policy(self, engine: str) -> dict[str, Any] | None:
        """IAM policy of a reasoning engine (REST: the SDK has no IAM call for engines); None when it's gone.

        The policy version is a query parameter and the body is empty (v1 discovery document).
        """
        url = f"https://{engine.split('/')[3]}-aiplatform.googleapis.com/v1/{engine}:getIamPolicy"
        response = self._http.post(url, params={"options.requestedPolicyVersion": 3}, timeout=60)
        if response.status_code == 404:
            return None
        if response.status_code == 403 and self.engine_get(engine) is None:
            return None  # IAM endpoints often answer 403 for a resource that no longer exists
        if not response.ok:
            raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text}")
        return response.json()

    def _engine_modify(self, engine: str, add: list[Binding], remove: list[Binding]) -> None:
        """Read-modify-write an engine's policy; the etag makes a concurrent change fail with 409, then retry."""
        url = f"https://{engine.split('/')[3]}-aiplatform.googleapis.com/v1/{engine}:setIamPolicy"
        for attempt in range(_IAM_RETRIES):
            policy = self._engine_policy(engine)
            if policy is None:
                raise RuntimeError(f"404 reasoning engine {engine} not found")
            bindings: list[dict[str, Any]] = policy.setdefault("bindings", [])
            for role, member in add:
                binding = next((b for b in bindings if b["role"] == role and not b.get("condition")), None)
                if binding is None:
                    binding: dict[str, Any] = {"role": role, "members": []}
                    bindings.append(binding)
                members: list[str] = binding.setdefault("members", [])
                if member not in members:
                    members.append(member)
            for role, member in remove:
                for b in bindings:
                    if b["role"] == role and not b.get("condition") and member in b.get("members", []):
                        b["members"].remove(member)
            policy["bindings"] = [b for b in bindings if b.get("members")]
            policy["version"] = 3
            response = self._http.post(url, json={"policy": policy}, timeout=60)
            if response.ok:
                return
            if response.status_code not in (409, 412) or attempt == _IAM_RETRIES - 1:
                raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text}")
            time.sleep(2**attempt)

    def _iam_members(self, rtype: str, name: str) -> dict[str, set[str]]:
        if rtype == "bigqueryDataset":
            return _bq_members(name, self.project, self._credentials)
        if rtype == "bucket":
            policy = _bucket(name, self.project, self._credentials).get_iam_policy(requested_policy_version=3)
            return {b["role"]: set(b["members"]) for b in policy.bindings if not b.get("condition")}
        client, resource = _proto_iam(rtype, name, self.project, self._credentials)
        policy = client.get_iam_policy(request={"resource": resource, "options": {"requested_policy_version": 3}})
        return {b.role: set(b.members) for b in policy.bindings if not b.condition.expression}

    def iam_modify(self, rtype: str, name: str, add: Iterable[Binding], remove: Iterable[Binding]) -> None:
        """Read-modify-write a policy touching only the given bindings, retrying on etag conflicts."""
        add, remove = list(add), list(remove)
        if not add and not remove:
            return
        if rtype == "bigqueryDataset":
            _bq_modify(name, add, remove, self.project, self._credentials)
            return
        if rtype == "reasoningEngine":
            self._engine_modify(name, add, remove)
            return
        if rtype == "bucket":
            _retry(lambda: _bucket_modify(name, add, remove, self.project, self._credentials))
            return
        client, resource = _proto_iam(rtype, name, self.project, self._credentials)

        def attempt() -> None:
            policy = client.get_iam_policy(request={"resource": resource, "options": {"requested_policy_version": 3}})
            for role, member in add:
                binding = next((b for b in policy.bindings if b.role == role and not b.condition.expression), None)
                if binding is None:
                    binding = policy.bindings.add(role=role)
                if member not in binding.members:
                    binding.members.append(member)
            for role, member in remove:
                for b in policy.bindings:
                    if b.role == role and not b.condition.expression and member in b.members:
                        b.members.remove(member)
            for empty in [b for b in policy.bindings if not b.members]:
                policy.bindings.remove(empty)
            policy.version = 3
            client.set_iam_policy(request={"resource": resource, "policy": policy})

        _retry(attempt)

    # --- buckets -----------------------------------------------------------------------------------------------

    @functools.cached_property
    def _storage(self) -> Any:
        from google.cloud import storage

        return storage.Client(project=self.project, credentials=self._credentials)

    def bucket_get(self, name: str) -> dict[str, Any] | None:
        """Settings agentless manages; None when missing, {"forbidden": True} when it exists but can't be read."""
        from google.api_core import exceptions

        try:
            bucket = self._storage.get_bucket(name)
        except exceptions.NotFound:
            return None
        except exceptions.Forbidden:
            return {"name": name, "forbidden": True}
        ages = [
            r.get("condition", {}).get("age")
            for r in bucket.lifecycle_rules
            if r.get("action", {}).get("type") == "Delete" and set(r.get("condition", {})) == {"age"}
        ]
        return {
            "name": name,
            "project_number": str(bucket.project_number) if bucket.project_number else None,
            "location": bucket.location or "",
            "storage_class": bucket.storage_class,
            "versioning": bool(bucket.versioning_enabled),
            "delete_after_days": ages[0] if len(ages) == 1 else None,
            "labels": dict(bucket.labels or {}),
            "kms_key": bucket.default_kms_key_name,
        }

    def bucket_create(self, name: str, spec: dict[str, Any]) -> None:
        """Create with uniform access and public access prevention always on."""
        bucket = self._storage.bucket(name)
        _apply_bucket_spec(bucket, spec, labels=spec["labels"])
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.iam_configuration.public_access_prevention = "enforced"
        self._storage.create_bucket(bucket, location=spec["location"])

    def bucket_update(self, name: str, spec: dict[str, Any]) -> None:
        """Patch managed settings; labels set outside agentless are kept."""
        bucket = self._storage.get_bucket(name)
        _apply_bucket_spec(bucket, spec, labels={**(bucket.labels or {}), **spec["labels"]})
        bucket.patch()

    def bucket_is_empty(self, name: str) -> bool:
        """True when no object (including noncurrent versions) is left."""
        return next(iter(self._storage.list_blobs(name, max_results=1, versions=True)), None) is None

    def bucket_delete(self, name: str) -> None:
        """Delete an empty bucket; the API refuses (409) if anything is left."""
        from google.api_core import exceptions

        try:
            self._storage.bucket(name).delete()
        except exceptions.NotFound:
            pass

    # --- reasoning engines ---------------------------------------------------------------------------------------

    @functools.cached_property
    def _vertex(self) -> Any:
        try:
            from agentplatform import Client
        except ImportError:  # SDKs older than the agentplatform rename
            from vertexai import Client

        return Client(
            project=self.project,
            location=self.region,
            credentials=self._credentials,
            http_options={"api_version": "v1beta1"},
        )

    def engine_get(self, name: str) -> dict[str, Any] | None:
        """Engine summary, or None when it no longer exists."""
        from google.genai import errors

        try:
            resource = self._vertex.agent_engines.get(name=name).api_resource
        except errors.ClientError as e:
            if e.code == 404:
                return None
            raise
        spec = resource.spec
        return {
            "name": resource.name,
            "display_name": resource.display_name,
            "effective_identity": getattr(spec, "effective_identity", None) if spec else None,
            "service_account": getattr(spec, "service_account", None) if spec else None,
        }

    def engine_find(self, display_name: str) -> list[str]:
        """Names of engines with this display name (used to adopt agents-cli deployments)."""
        return [
            e.api_resource.name
            for e in self._vertex.agent_engines.list(config={"filter": f'display_name="{display_name}"'})
            if e.api_resource.display_name == display_name
        ]

    def engine_code_spec(
        self, source_dir: Path, source_packages: list[str], build_args: dict[str, str], framework: str
    ) -> tuple[dict[str, Any], list[str]]:
        """Build `spec.source_code_spec` (inline tarball) using the SDK, as agents-cli does."""
        with contextlib.chdir(source_dir):
            try:
                cfg = self._vertex.agent_engines._create_config(
                    mode="update",
                    source_packages=source_packages,
                    image_spec={"build_args": build_args} if build_args else {},
                    agent_framework=framework,
                )
            except ValueError as e:  # the SDK validates the source tree with bare ValueErrors
                raise RuntimeError(f"packaging the agent source failed: {e}") from e
        masks = [m for m in cfg.get("update_mask", "").split(",") if m.startswith("spec.source_code_spec")]
        return {"source_code_spec": cfg["spec"]["source_code_spec"]}, masks

    def engine_create(self, config: dict[str, Any]) -> str:
        """Start a create; returns the operation name."""
        return self._vertex.agent_engines._create(config=config).name

    def engine_create_identity(
        self, display_name: str, labels: dict[str, str], encryption_spec: dict[str, str] | None
    ) -> dict[str, Any]:
        """Create a bare engine with Agent Identity so its principal exists before code is deployed."""
        config: dict[str, Any] = {"identity_type": "AGENT_IDENTITY", "display_name": display_name, "labels": labels}
        if encryption_spec:
            config["encryption_spec"] = encryption_spec
        agent = self._vertex.agent_engines.create(config=config)
        return {"name": agent.api_resource.name, "effective_identity": agent.api_resource.spec.effective_identity}

    def engine_update(self, name: str, config: dict[str, Any]) -> str:
        """Start an update; returns the operation name."""
        return self._vertex.agent_engines._update(name=name, config=config).name

    def operation_status(self, operation: str) -> tuple[bool, str | None]:
        """(done, error message) of an engine operation."""
        op = self._vertex.agent_engines._get_agent_operation(operation_name=operation)
        return bool(op.done), str(op.error) if op.error else None

    def wait(self, operation: str, on_tick: Callable[[float], None] | None = None, poll: float = 10) -> None:
        """Block until the operation finishes; raise on failure."""
        start = time.monotonic()
        while True:
            done, error = self.operation_status(operation)
            if done:
                if error:
                    raise RuntimeError(f"operation {operation} failed: {error}")
                return
            if on_tick:
                on_tick(time.monotonic() - start)
            time.sleep(poll)

    def engine_delete(self, name: str) -> None:
        """Delete an engine with its sessions and memories, waiting for the operation to finish."""
        from google.genai import errors

        try:
            operation = self._vertex.agent_engines.delete(name=name, force=True)
        except errors.ClientError as e:
            if e.code == 404:
                return
            raise
        if getattr(operation, "name", None) and not getattr(operation, "done", False):
            self.wait(operation.name, poll=5)

    # --- Gemini Enterprise (Discovery Engine v1alpha REST) -------------------------------------------------------

    @functools.cached_property
    def _http(self) -> Any:
        from google.auth.transport.requests import AuthorizedSession

        session = AuthorizedSession(self._credentials)
        session.headers["X-Goog-User-Project"] = self.project
        return session

    def _rest(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, url, timeout=60, **kwargs)
        if response.status_code == 404 and method == "DELETE":
            return {}
        if not response.ok:
            raise RuntimeError(f"{method} {url} -> {response.status_code}: {response.text}")
        return response.json() if response.content else {}

    def _get_or_none(self, url: str) -> dict[str, Any] | None:
        response = self._http.get(url, timeout=60)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError(f"GET {url} -> {response.status_code}: {response.text}")
        return response.json()

    def a2a_card(self, url: str) -> dict[str, Any] | None:
        """Agent card served by a deployed engine; None when it isn't served (yet), e.g. while new code starts."""
        import requests

        try:
            response = self._http.get(url, timeout=60)
        except requests.ConnectionError:
            return None
        if response.status_code == 404 or response.status_code >= 500:
            return None
        if not response.ok:
            raise RuntimeError(f"GET {url} -> {response.status_code}: {response.text}")
        return response.json()

    def ge_find_agent(self, app: str, *, engine: str | None = None, card_url: str | None = None) -> str | None:
        """Name of the GE agent registered for this reasoning engine (adk) or agent card url (a2a), if any."""
        url = f"{_ge_base(app)}/v1alpha/{app}/assistants/default_assistant/agents"
        page_token = None
        while True:
            data = self._rest("GET", url, params={"pageToken": page_token} if page_token else None)
            for agent in data.get("agents", []):
                if _ge_matches(agent, engine, card_url):
                    return agent["name"]
            page_token = data.get("nextPageToken")
            if not page_token:
                return None

    def ge_create_agent(self, app: str, payload: dict[str, Any]) -> str:
        """Register the agent; returns its resource name."""
        url = f"{_ge_base(app)}/v1alpha/{app}/assistants/default_assistant/agents"
        return self._rest("POST", url, json=payload)["name"]

    def ge_patch_agent(self, name: str, payload: dict[str, Any]) -> None:
        """Update a registration in place.

        The mask covers the fields in the payload, plus authorization_config so a dropped authorization is cleared.
        sharing_config is only masked when set, so omitting `sharing` keeps whatever the app has.
        """
        mask = ",".join(ge_agent_mask(payload))
        self._rest("PATCH", f"{_ge_base(name)}/v1alpha/{name}", params={"updateMask": mask}, json=payload)

    def ge_delete_agent(self, name: str) -> None:
        """Unregister; missing is fine."""
        self._rest("DELETE", f"{_ge_base(name)}/v1alpha/{name}")

    def ge_upsert_authorization(self, app: str, auth_id: str, oauth: dict[str, Any]) -> str:
        """Create or patch the OAuth authorization GE uses to call the agent as the user."""
        parts = app.split("/")
        parent = f"projects/{parts[1]}/locations/{parts[3]}"
        name = f"{parent}/authorizations/{auth_id}"
        scope = " ".join(oauth["scopes"])
        query = urllib.parse.urlencode(
            {
                "client_id": oauth["client_id"],
                "redirect_uri": _GE_REDIRECT_URI,
                "scope": scope,
                "include_granted_scopes": "true",
                "response_type": "code",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        body = {
            "displayName": oauth.get("display_name") or auth_id,
            "serverSideOauth2": {
                "clientId": oauth["client_id"],
                "clientSecret": oauth["client_secret"],
                "authorizationUri": f"{oauth['authorization_uri']}?{query}",
                "tokenUri": oauth["token_uri"],
                "scopes": oauth["scopes"],
            },
        }
        base = _ge_base(app)
        response = self._http.get(f"{base}/v1alpha/{name}", timeout=60)
        if response.status_code == 404:
            self._rest(
                "POST", f"{base}/v1alpha/{parent}/authorizations", params={"authorizationId": auth_id}, json=body
            )
        else:
            mask = "displayName,serverSideOauth2"
            self._rest("PATCH", f"{base}/v1alpha/{name}", params={"updateMask": mask}, json=body)
        return name

    def ge_delete_authorization(self, name: str) -> None:
        """Delete an authorization; missing is fine."""
        self._rest("DELETE", f"{_ge_base(name)}/v1alpha/{name}")

    def ge_app_gateway(self, app: str) -> str | None:
        """Egress Agent Gateway the app routes agent traffic through, if any."""
        data = self._rest("GET", f"{_ge_base(app)}/v1alpha/{app}")
        return ((data.get("agentGatewaySetting") or {}).get("defaultEgressAgentGateway") or {}).get("name") or None

    def ge_set_app_gateway(self, app: str, gateway: str) -> None:
        """Route the app's agent traffic through an Agent Gateway."""
        self._rest(
            "PATCH",
            f"{_ge_base(app)}/v1alpha/{app}",
            params={"updateMask": "agentGatewaySetting.defaultEgressAgentGateway.name"},
            json={"agentGatewaySetting": {"defaultEgressAgentGateway": {"name": gateway}}},
        )

    # --- Agent Registry (v1alpha REST) ---------------------------------------------------------------------------

    def registry_get(self, name: str) -> dict[str, Any] | None:
        """A registry service, or None."""
        return self._get_or_none(f"{_REGISTRY}/v1alpha/{name}")

    def registry_create(self, parent: str, service_id: str, body: dict[str, Any]) -> None:
        """Register a service under `projects/{p}/locations/{loc}`."""
        self._rest("POST", f"{_REGISTRY}/v1alpha/{parent}/services", params={"serviceId": service_id}, json=body)

    def registry_patch(self, name: str, body: dict[str, Any]) -> None:
        """Update a service's card, display name and description."""
        mask = "agentSpec,displayName,description"
        self._rest("PATCH", f"{_REGISTRY}/v1alpha/{name}", params={"updateMask": mask}, json=body)

    def registry_delete(self, name: str) -> None:
        """Unregister; missing is fine."""
        self._rest("DELETE", f"{_REGISTRY}/v1alpha/{name}")

    # --- online evaluators (Agent Platform v1beta1 REST; every mutation is a long-running operation) --------------

    def _aiplatform(self, name: str) -> str:
        return f"https://{name.split('/')[3]}-aiplatform.googleapis.com/v1beta1/{name}"

    def _lro(self, operation: dict[str, Any], poll: float = 3, timeout: float = 600) -> dict[str, Any]:
        """Wait for an aiplatform operation; returns its response."""
        start = time.monotonic()
        while not operation.get("done"):
            if time.monotonic() - start > timeout:
                raise RuntimeError(f"operation {operation['name']} still running after {int(timeout)}s")
            time.sleep(poll)
            operation = self._rest("GET", self._aiplatform(operation["name"]))
        if operation.get("error"):
            raise RuntimeError(f"operation {operation['name']} failed: {operation['error']}")
        return operation.get("response") or {}

    def evaluator_get(self, name: str) -> dict[str, Any] | None:
        """An online evaluator, or None."""
        return self._get_or_none(self._aiplatform(name))

    def evaluator_find(self, parent: str, display_name: str, agent: str) -> str | None:
        """Evaluator of this agent with this display name, e.g. one a crashed run created but never recorded."""
        url, page_token = f"{self._aiplatform(parent)}/onlineEvaluators", None
        while True:
            data = self._rest("GET", url, params={"pageToken": page_token} if page_token else None)
            for evaluator in data.get("onlineEvaluators", []):
                if evaluator.get("displayName") == display_name and evaluator.get("agentResource") == agent:
                    return evaluator["name"]
            page_token = data.get("nextPageToken")
            if not page_token:
                return None

    def evaluator_create(self, parent: str, body: dict[str, Any]) -> str:
        """Create an online evaluator under `projects/{p}/locations/{l}`; returns its name."""
        operation = self._rest("POST", f"{self._aiplatform(parent)}/onlineEvaluators", json=body)
        response = self._lro(operation)
        if response.get("name"):
            return response["name"]
        # The operation name embeds the new evaluator: …/onlineEvaluators/{id}/operations/{op}
        return operation["name"].split("/operations/")[0]

    def evaluator_patch(self, name: str, body: dict[str, Any], mask: list[str]) -> None:
        """Update the fields in `mask` (camelCase paths)."""
        operation = self._rest("PATCH", self._aiplatform(name), params={"updateMask": ",".join(mask)}, json=body)
        self._lro(operation)

    def evaluator_set_enabled(self, name: str, enabled: bool) -> None:
        """Activate or suspend an evaluator; its configuration is kept."""
        operation = self._rest("POST", f"{self._aiplatform(name)}:{'activate' if enabled else 'suspend'}", json={})
        self._lro(operation)

    def evaluator_delete(self, name: str) -> None:
        """Delete an evaluator; missing is fine."""
        operation = self._rest("DELETE", self._aiplatform(name))
        if operation:
            self._lro(operation)


_REGISTRY = "https://agentregistry.googleapis.com"
# Patchable registration fields: (payload key, update-mask path), in mask order.
_GE_AGENT_FIELDS = (
    ("displayName", "display_name"),
    ("description", "description"),
    ("icon", "icon"),
    ("adk_agent_definition", "adk_agent_definition"),
    ("a2a_agent_definition", "a2a_agent_definition"),
    ("authorization_config", "authorization_config"),
    ("sharing_config", "sharing_config"),
)


def ge_agent_mask(payload: dict[str, Any]) -> list[str]:
    """Update-mask paths for a registration payload.

    Covers what the payload sets, plus authorization_config so a dropped authorization is cleared. sharing_config is
    only masked when set, so omitting it keeps the app's value.
    """
    return [mask for key, mask in _GE_AGENT_FIELDS if key in payload or key == "authorization_config"]


def _ge_matches(agent: dict[str, Any], engine: str | None, card_url: str | None) -> bool:
    if engine:
        definition = (agent.get("adkAgentDefinition") or {}).get("provisionedReasoningEngine") or {}
        if definition.get("reasoningEngine") == engine:
            return True
    if card_url:
        raw = (agent.get("a2aAgentDefinition") or {}).get("jsonAgentCard") or "{}"
        with contextlib.suppress(ValueError):
            card = json.loads(raw)
            return isinstance(card, dict) and card.get("url") == card_url
    return False


def _ge_base(resource: str) -> str:
    location = resource.split("/")[3]
    return (
        "https://discoveryengine.googleapis.com"
        if location == "global"
        else (f"https://{location}-discoveryengine.googleapis.com")
    )


def _proto_iam(rtype: str, name: str, project: str, credentials: Any) -> tuple[Any, str]:
    if rtype == "project":
        from google.cloud import resourcemanager_v3

        return resourcemanager_v3.ProjectsClient(credentials=credentials), f"projects/{name}"
    if rtype == "folder":
        from google.cloud import resourcemanager_v3

        return resourcemanager_v3.FoldersClient(credentials=credentials), f"folders/{name.removeprefix('folders/')}"
    if rtype == "organization":
        from google.cloud import resourcemanager_v3

        return resourcemanager_v3.OrganizationsClient(
            credentials=credentials
        ), f"organizations/{name.removeprefix('organizations/')}"
    if rtype == "secret":
        from google.cloud import secretmanager

        resource = name if name.startswith("projects/") else f"projects/{project}/secrets/{name}"
        return secretmanager.SecretManagerServiceClient(credentials=credentials), resource
    if rtype == "serviceAccount":
        from google.cloud import iam_admin_v1

        return iam_admin_v1.IAMClient(credentials=credentials), f"projects/-/serviceAccounts/{name}"
    raise ValueError(f"unsupported IAM resource type {rtype}")


def _retry(fn: Callable[[], None]) -> None:
    from google.api_core import exceptions

    for attempt in range(_IAM_RETRIES):
        try:
            fn()
            return
        except (exceptions.Aborted, exceptions.Conflict, exceptions.PreconditionFailed, exceptions.BadRequest) as e:
            # BadRequest covers a freshly created service account not yet visible to IAM.
            transient = not isinstance(e, exceptions.BadRequest) or "does not exist" in str(e)
            if not transient or attempt == _IAM_RETRIES - 1:
                raise
            time.sleep(2**attempt)


def _apply_bucket_spec(bucket: Any, spec: dict[str, Any], labels: dict[str, str]) -> None:
    bucket.storage_class = spec["storage_class"]
    bucket.versioning_enabled = spec["versioning"]
    bucket.labels = labels
    if spec["kms_key"] or bucket.default_kms_key_name:
        bucket.default_kms_key_name = spec["kms_key"]
    bucket.clear_lifecycle_rules()
    if spec["delete_after_days"]:
        bucket.add_lifecycle_delete_rule(age=spec["delete_after_days"])


def _bucket(name: str, project: str, credentials: Any) -> Any:
    from google.cloud import storage

    return storage.Client(project=project, credentials=credentials).bucket(name.removeprefix("gs://"))


def _bucket_modify(name: str, add: list[Binding], remove: list[Binding], project: str, credentials: Any) -> None:
    bucket = _bucket(name, project, credentials)
    policy = bucket.get_iam_policy(requested_policy_version=3)
    for role, member in add:
        binding = next((b for b in policy.bindings if b["role"] == role and not b.get("condition")), None)
        if binding is None:
            policy.bindings.append({"role": role, "members": {member}})
        else:
            binding["members"] = set(binding["members"]) | {member}
    for role, member in remove:
        for b in policy.bindings:
            if b["role"] == role and not b.get("condition"):
                b["members"] = set(b["members"]) - {member}
    policy.bindings[:] = [b for b in policy.bindings if b["members"]]
    policy.version = 3
    bucket.set_iam_policy(policy)


_BQ_LEGACY = {
    "OWNER": "roles/bigquery.dataOwner",
    "WRITER": "roles/bigquery.dataEditor",
    "READER": "roles/bigquery.dataViewer",
}


def _bq_role(role: str) -> str:
    return _BQ_LEGACY.get(role, role)


def _bq_split(name: str) -> str:
    return name.replace(":", ".", 1)


def _bq_entity(member: str) -> tuple[str, str]:
    kind, _, value = member.partition(":")
    if kind in ("serviceAccount", "user"):
        return "userByEmail", value
    if kind == "group":
        return "groupByEmail", value
    return "iamMember", member


def _bq_members(name: str, project: str, credentials: Any) -> dict[str, set[str]]:
    from google.cloud import bigquery

    dataset = bigquery.Client(project=project, credentials=credentials).get_dataset(_bq_split(name))
    members: dict[str, set[str]] = {}
    for entry in dataset.access_entries:
        if not entry.role:
            continue
        prefix = {
            "userByEmail": "serviceAccount:" if str(entry.entity_id).endswith("gserviceaccount.com") else "user:",
            "groupByEmail": "group:",
        }.get(entry.entity_type, "")
        members.setdefault(_bq_role(entry.role), set()).add(f"{prefix}{entry.entity_id}")
    return members


def _bq_modify(name: str, add: list[Binding], remove: list[Binding], project: str, credentials: Any) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=project, credentials=credentials)
    dataset = client.get_dataset(_bq_split(name))
    entries = list(dataset.access_entries)
    for role, member in remove:
        entity_type, entity_id = _bq_entity(member)
        entries = [
            e
            for e in entries
            if not (_bq_role(e.role or "") == role and e.entity_type == entity_type and e.entity_id == entity_id)
        ]
    for role, member in add:
        entity_type, entity_id = _bq_entity(member)
        entries.append(bigquery.AccessEntry(role=role, entity_type=entity_type, entity_id=entity_id))
    dataset.access_entries = entries
    client.update_dataset(dataset, ["access_entries"])  # uses the fetched etag, fails on concurrent edits
