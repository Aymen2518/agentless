"""Typed model of `agent.yaml`, validated after variable resolution."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from agentless.auth import check_service_account

_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_SA_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class StageConfig(_Model):
    """Per-stage overrides, read through `${param:...}`."""

    params: dict[str, Any] = Field(default_factory=dict)


class DeployerConfig(_Model):
    """Service account agentless impersonates for every GCP call (needs roles/iam.serviceAccountTokenCreator)."""

    impersonate: str | None = Field(
        default=None,
        description="Deployer service account email. Overridden by --impersonate-service-account and "
        "AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT. Cannot use ${secret:} or ${tf:}.",
    )
    delegates: list[str] = Field(
        default_factory=list, description="Delegation chain, in order, when the caller cannot impersonate directly."
    )

    @field_validator("impersonate")
    @classmethod
    def _sa_email(cls, v: str | None) -> str | None:
        return check_service_account(v) if v and v.strip() else None

    @field_validator("delegates")
    @classmethod
    def _sa_emails(cls, v: list[str]) -> list[str]:
        return [check_service_account(email) for email in v]

    @model_validator(mode="after")
    def _delegates_need_target(self) -> DeployerConfig:
        if self.delegates and not self.impersonate:
            raise ValueError("delegates need `impersonate`")
        return self


class Provider(_Model):
    """Where and how the agent is deployed."""

    name: Literal["agent-runtime", "cloud-run"] = Field(
        default="agent-runtime",
        description="agent-runtime: a reasoning engine on Agent Runtime. cloud-run: a Cloud Run service (see cloudRun).",
    )
    stage: str = "dev"
    project: str
    region: str
    staging_bucket: str | None = Field(default=None, description="Bucket holding agentless state, without gs://.")
    labels: dict[str, str] = Field(default_factory=dict)
    deployer: DeployerConfig = Field(default_factory=DeployerConfig)

    @field_validator("region")
    @classmethod
    def _regional(cls, v: str) -> str:
        if v == "global":
            raise ValueError("agents deploy to a regional location, not 'global'")
        return v

    @field_validator("staging_bucket")
    @classmethod
    def _strip_scheme(cls, v: str | None) -> str | None:
        return v.removeprefix("gs://").rstrip("/") if v else v

    @field_validator("labels")
    @classmethod
    def _valid_labels(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _LABEL_RE.match(key):
                raise ValueError(f"invalid label key {key!r}")
        return v


class VpcEgress(_Model):
    """Direct VPC egress of a Cloud Run service."""

    network: str = Field(description="VPC network name or full resource name.")
    subnetwork: str = Field(description="Subnetwork name or full resource name, in provider.region.")
    egress: Literal["private-ranges-only", "all-traffic"] = "private-ranges-only"


_RESERVED_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-serverless-authorization",
    "host",
    "cookie",
    "forwarded",
    "connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
)
_RESERVED_HEADER_PREFIXES = ("x-goog-", "x-cloud-", "x-forwarded-", "content-")
_PRINCIPAL_RE = re.compile(r"^(domain:[a-z0-9.-]+\.[a-z]{2,}|[^@\s:,]+@[^@\s:,]+\.[a-z]{2,})$")
AUTH_ENV = (
    "AGENTLESS_AUTH_MODE",
    "AGENTLESS_TOKEN_HEADER",
    "AGENTLESS_TOKEN_AUDIENCES",
    "AGENTLESS_ALLOWED_PRINCIPALS",
    "AGENTLESS_WEBHOOK_HEADER",
    "AGENTLESS_WEBHOOK_SECRET",
)


def _app_header(v: str) -> str:
    v = v.lower()
    if v in _RESERVED_HEADERS or v.startswith(_RESERVED_HEADER_PREFIXES):
        raise ValueError(f"{v} is reserved by HTTP, Cloud Run or IAP; pick another header")
    return v


class GoogleToken(_Model):
    """Second check of `cloudRun.auth: googleToken`, done by the container: agentless only passes these settings."""

    header: str = Field(
        default="x-google-token",
        pattern=r"^[A-Za-z0-9-]+$",
        description="Request header carrying the caller's Google ID token for the app to verify.",
    )
    audiences: list[str] = Field(
        default_factory=list, description="Accepted `aud` values of that token; defaults to the service URL."
    )
    allowed_principals: list[str] = Field(
        min_length=1,
        description="Who the container accepts: emails (users or service accounts) or domain:<domain>.",
    )

    @field_validator("header")
    @classmethod
    def _header(cls, v: str) -> str:
        return _app_header(v)

    @field_validator("audiences")
    @classmethod
    def _audiences(cls, v: list[str]) -> list[str]:
        for aud in v:
            if not aud.strip() or "," in aud or aud != aud.strip():
                raise ValueError(f"invalid audience {aud!r} (a URL or OAuth client ID, without commas)")
        return v

    @field_validator("allowed_principals")
    @classmethod
    def _principals(cls, v: list[str]) -> list[str]:
        v = [p.lower() for p in v]  # token emails are lowercase
        for principal in v:
            if not _PRINCIPAL_RE.match(principal):
                raise ValueError(f"invalid principal {principal!r} (expected an email or domain:<domain>)")
        return v


class SecretRef(_Model):
    """Secret Manager reference exposed as an env var."""

    secret: str
    version: str = "latest"


class Webhook(_Model):
    """Shared-secret gate of `cloudRun.auth: webhook`, checked by the container: Cloud Run's IAM check is off."""

    header: str = Field(
        default="x-gitlab-token",
        pattern=r"^[A-Za-z0-9-]+$",
        description="Request header carrying the shared secret (GitLab sends X-Gitlab-Token).",
    )
    secret: SecretRef = Field(
        description="Secret Manager secret holding the expected value; the container gets it as "
        "AGENTLESS_WEBHOOK_SECRET and the runtime identity is granted access to it."
    )

    @field_validator("header")
    @classmethod
    def _header(cls, v: str) -> str:
        return _app_header(v)


class CloudRun(_Model):
    """Cloud Run service settings (provider.name: cloud-run); callers need roles/run.invoker unless auth: webhook."""

    service_name: str | None = Field(
        default=None, pattern=r"^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$", description="Defaults to <service>-<stage>."
    )
    image: str | None = Field(
        default=None, description="Prebuilt image (e.g. built in CI). When set, agentless does not build the source."
    )
    artifact_registry: str = Field(
        default="cloud-run-source-deploy",
        pattern=r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$",
        description="Docker repository in provider.region that receives built images; it must already exist.",
    )
    build_service_account: str | None = Field(
        default=None, description="Service account Cloud Build runs as; defaults to the project's build default."
    )
    ingress: Literal["all", "internal", "internal-and-cloud-load-balancing"] = Field(
        default="all",
        description="Network sources allowed to reach the service. IAM still applies to every request.",
    )
    iap: bool = Field(default=False, description="Put Identity-Aware Proxy in front of the service.")
    auth: Literal["iam", "googleToken", "webhook"] = Field(
        default="iam",
        description="iam: Cloud Run checks the caller's ID token (Authorization). googleToken: Cloud Run still "
        "checks it, sent as X-Serverless-Authorization, and the container also verifies the Google token in "
        "googleToken.header. webhook: for callers that cannot get a Google token (GitLab, GitHub webhooks): "
        "Cloud Run's invoker IAM check is off and the container must reject every request without the secret in "
        "webhook.header; agentless checks that it does after each rollout and turns IAM back on if not.",
    )
    google_token: GoogleToken | None = Field(default=None, description="Settings of auth: googleToken.")
    webhook: Webhook | None = Field(default=None, description="Settings of auth: webhook.")
    vpc_egress: VpcEgress | None = None
    timeout: str = Field(default="300s", pattern=r"^\d+s$", description="Request timeout, at most 3600s.")
    cpu_throttling: bool = Field(default=False, description="Only allocate CPU during requests.")
    session_affinity: bool = True
    port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("timeout")
    @classmethod
    def _max_timeout(cls, v: str) -> str:
        if not 1 <= int(v[:-1]) <= 3600:
            raise ValueError("timeout must be between 1s and 3600s")
        return v

    @field_validator("build_service_account")
    @classmethod
    def _sa(cls, v: str | None) -> str | None:
        return check_service_account(v) if v else v

    @model_validator(mode="after")
    def _auth(self) -> CloudRun:
        if self.auth == "googleToken":
            if self.google_token is None:
                raise ValueError("auth googleToken needs cloudRun.googleToken.allowedPrincipals")
            if self.iap:
                raise ValueError("auth googleToken and iap are exclusive: IAP already verifies the caller")
            if self.ingress == "internal":
                raise ValueError("auth googleToken is for callers outside the VPC; use ingress all")
        elif self.google_token is not None:
            raise ValueError("cloudRun.googleToken is only valid with auth: googleToken")
        if self.auth == "webhook":
            if self.webhook is None:
                raise ValueError("auth webhook needs cloudRun.webhook.secret")
            if self.iap:
                raise ValueError("auth webhook and iap are exclusive: webhook callers cannot pass IAP")
        elif self.webhook is not None:
            raise ValueError("cloudRun.webhook is only valid with auth: webhook")
        return self


class Source(_Model):
    """Location of the agents-cli project to package."""

    path: str = "."
    agent_directory: str | None = None


class Build(_Model):
    """Container build settings (sourceCodeSpec.imageSpec)."""

    args: dict[str, str] = Field(default_factory=dict)


class ServerMode(StrEnum):
    """Agent server mode on Agent Runtime."""

    STABLE = "STABLE"
    EXPERIMENTAL = "EXPERIMENTAL"


class Runtime(_Model):
    """Scaling and sizing (spec.deploymentSpec)."""

    cpu: str = "1"
    memory: str = "4Gi"
    min_instances: int = Field(default=1, ge=0, le=10)
    max_instances: int = Field(default=10, ge=1, le=1000)
    concurrency: int = Field(default=8, ge=1)
    server_mode: ServerMode | None = None

    @model_validator(mode="after")
    def _bounds(self) -> Runtime:
        if self.min_instances > self.max_instances:
            raise ValueError("minInstances cannot exceed maxInstances")
        return self


class Telemetry(_Model):
    """Deprecated: use `observability.tracing`."""

    model_config = ConfigDict(json_schema_extra={"deprecated": True})

    enabled: bool = True
    capture_message_content: bool = False


class Tracing(_Model):
    """Cloud Trace export from the agent (OpenTelemetry), mapped to the env vars agents-cli sets."""

    enabled: bool = Field(
        default=True,
        description="Export traces. Also grants the runtime identity roles/cloudtrace.agent, roles/logging.logWriter "
        "and roles/monitoring.metricWriter on the project (not needed for the platform service agent).",
    )
    capture_content: bool = Field(
        default=False, description="Record prompts and responses in spans. Keep off where data is sensitive."
    )


class Observability(_Model):
    """Tracing, logging and monitoring of the deployed agent."""

    tracing: Tracing = Field(default_factory=Tracing)


class Encryption(_Model):
    """Customer-managed encryption key."""

    kms_key: str | None = None


class Agent(_Model):
    """The deployed agent itself."""

    display_name: str | None = None
    description: str | None = None
    source: Source = Field(default_factory=Source)
    framework: str = "google-adk"
    build: Build = Field(default_factory=Build)
    runtime: Runtime = Field(default_factory=Runtime)
    environment: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretRef] = Field(default_factory=dict)
    telemetry: Telemetry | None = Field(default=None, description="Deprecated: use observability.tracing.")
    encryption: Encryption = Field(default_factory=Encryption)

    @field_validator("environment", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        return {k: str(val).lower() if isinstance(val, bool) else str(val) for k, val in (v or {}).items()}

    @field_validator("environment")
    @classmethod
    def _no_reserved(cls, v: dict[str, str]) -> dict[str, str]:
        if "GOOGLE_CLOUD_PROJECT" in v:
            raise ValueError("GOOGLE_CLOUD_PROJECT is reserved and injected by Agent Runtime")
        return v

    @model_validator(mode="after")
    def _no_overlap(self) -> Agent:
        clash = set(self.environment) & set(self.secrets)
        if clash:
            raise ValueError(f"defined both as environment and secret: {sorted(clash)}")
        return self


class IdentityType(StrEnum):
    """Runtime identity of the agent."""

    PLATFORM = "platform"
    SERVICE_ACCOUNT = "serviceAccount"
    AGENT_IDENTITY = "agentIdentity"


class ServiceAccount(_Model):
    """Runtime service account: created by agentless or pre-existing."""

    create: bool = True
    name: str | None = Field(default=None, description="Account ID when create=true.")
    display_name: str | None = None
    email: str | None = Field(default=None, description="Existing account email when create=false.")

    @model_validator(mode="after")
    def _one_of(self) -> ServiceAccount:
        if self.create and not self.name:
            raise ValueError("serviceAccount.name is required when create is true")
        if self.create and self.name and not _SA_ID_RE.match(self.name):
            raise ValueError(f"invalid service account id {self.name!r} (6-30 chars, lowercase, digits, hyphens)")
        if not self.create and not self.email:
            raise ValueError("serviceAccount.email is required when create is false")
        return self


class OrganizationRoles(_Model):
    """Org-level roles; usually need an org admin to apply."""

    id: str
    roles: list[str]


ResourceType = Literal["project", "folder", "organization", "bucket", "secret", "serviceAccount", "bigqueryDataset"]


class ResourceRoles(_Model):
    """Roles granted on a single resource."""

    type: ResourceType
    name: str = Field(description="Resource id: bucket name, secret id, SA email, `project:dataset`, folder id…")
    roles: list[str]


class Roles(_Model):
    """Least-privilege grants for the agent's identity."""

    project: list[str] = Field(default_factory=list)
    organization: OrganizationRoles | None = None
    resources: list[ResourceRoles] = Field(default_factory=list)

    @field_validator("project")
    @classmethod
    def _role_format(cls, v: list[str]) -> list[str]:
        for role in v:
            if not role.startswith(("roles/", "projects/", "organizations/")):
                raise ValueError(f"invalid role {role!r}")
        return v


class Identity(_Model):
    """Who the agent runs as and what it may access."""

    type: IdentityType = IdentityType.SERVICE_ACCOUNT
    service_account: ServiceAccount | None = None
    roles: Roles = Field(default_factory=Roles)

    @model_validator(mode="after")
    def _sa_required(self) -> Identity:
        if self.type == IdentityType.SERVICE_ACCOUNT and self.service_account is None:
            raise ValueError("identity.serviceAccount is required when type is serviceAccount")
        if self.type != IdentityType.SERVICE_ACCOUNT and self.service_account is not None:
            raise ValueError(f"identity.serviceAccount must be omitted when type is {self.type.value}")
        return self


class DnsPeering(_Model):
    """DNS peering for PSC-I."""

    domain: str
    target_project: str
    target_network: str


class PscInterface(_Model):
    """Private Service Connect interface (create-time only)."""

    network_attachment: str
    dns_peering: list[DnsPeering] = Field(default_factory=list)


class Network(_Model):
    """Private networking for the agent."""

    psc_interface: PscInterface | None = None


class MemoryBank(_Model):
    """Memory Bank (contextSpec.memoryBankConfig)."""

    generation_model: str | None = None
    embedding_model: str | None = None
    ttl: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?s$")
    topics: list[str] = Field(default_factory=list, description="Managed memory topics, e.g. USER_PREFERENCES.")
    disable_memory_revisions: bool | None = None


class Memory(_Model):
    """Sessions and long-term memory."""

    sessions: Literal["agentPlatform", "inMemory"] = "agentPlatform"
    artifacts_bucket: str | None = Field(default=None, description="GCS bucket for ADK artifacts (LOGS_BUCKET_NAME).")
    memory_bank: MemoryBank | None = None


_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,61}[a-z0-9]$")

BUCKET_ROLES = {
    "objectViewer": "roles/storage.objectViewer",
    "objectUser": "roles/storage.objectUser",
    "objectAdmin": "roles/storage.objectAdmin",
}


class BucketLifecycle(_Model):
    """Object lifecycle; agentless owns the bucket's lifecycle rules once this is set, and leaves them alone otherwise."""

    delete_after_days: int | None = Field(default=None, ge=1, description="Delete objects older than this.")


class Bucket(_Model):
    """A GCS bucket agentless creates and owns, with access granted to the agent's runtime identity."""

    name: str = Field(description="Globally unique bucket name, e.g. acme-reports-${stage}.")
    location: str | None = Field(default=None, description="Region or multi-region; defaults to provider.region.")
    storage_class: Literal["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"] = "STANDARD"
    versioning: bool = False
    lifecycle: BucketLifecycle = Field(default_factory=BucketLifecycle)
    kms_key: str | None = Field(
        default=None,
        description="CMEK for objects. The Cloud Storage service agent needs encrypter/decrypter on the key.",
    )
    access: Literal["objectViewer", "objectUser", "objectAdmin", "none"] = Field(
        default="objectUser", description="Role granted to the runtime identity on this bucket."
    )
    env: str | None = Field(
        default=None, pattern=r"^[A-Z_][A-Z0-9_]*$", description="Env var that receives the bucket name."
    )
    deletion_policy: Literal["retain", "delete"] = Field(
        default="retain",
        description="On `remove` or when dropped from agent.yaml: retain keeps the bucket and its data; "
        "delete removes it only if it is empty. Objects are never deleted.",
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.removeprefix("gs://")
        if not _BUCKET_RE.match(value) or ".." in value or value.startswith("goog"):
            raise ValueError(f"invalid bucket name {value!r}")
        return value


class Resources(_Model):
    """Data resources agentless creates for the agent."""

    buckets: dict[str, Bucket] = Field(default_factory=dict)

    @field_validator("buckets")
    @classmethod
    def _unique(cls, value: dict[str, Bucket]) -> dict[str, Bucket]:
        for key in value:
            if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", key):
                raise ValueError(f"invalid bucket key {key!r}")
        for attr in ("name", "env"):
            seen = [getattr(b, attr) for b in value.values() if getattr(b, attr)]
            if dupes := sorted({v for v in seen if seen.count(v) > 1}):
                raise ValueError(f"duplicate bucket {attr}: {', '.join(dupes)}")
        return value


class Authorization(_Model):
    """OAuth authorization used by Gemini Enterprise to call the agent on the user's behalf."""

    id: str
    display_name: str | None = Field(default=None, description="Shown on the consent prompt; defaults to the id.")
    client_id: str
    client_secret: str
    scopes: list[str]
    authorization_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"


class GeminiEnterprise(_Model):
    """Gemini Enterprise registration."""

    app: str = Field(pattern=r"^projects/[^/]+/locations/[^/]+/collections/[^/]+/engines/[^/]+$")
    display_name: str | None = None
    description: str | None = None
    tool_description: str | None = None
    registration: Literal["adk", "a2a"] = Field(
        default="adk",
        description="adk: Gemini Enterprise calls the engine natively (recommended on Agent Runtime). "
        "a2a: registers the agent card; the agent must be an A2A agent.",
    )
    sharing: Literal["allUsers", "restricted", "private"] | None = Field(
        default=None,
        description="allUsers: everyone in the app. restricted: the users and groups given access in the console "
        "(User permissions). private: only the creator and admins. Omit to leave the app's setting alone.",
    )
    agent_gateway: str | None = Field(
        default=None,
        pattern=r"^projects/[^/]+/locations/[^/]+/agentGateways/[^/]+$",
        description="Egress Agent Gateway of the app. Set when the app has none; never changed or reverted, "
        "since every agent of the app shares it.",
    )
    grant_service_agent: bool = Field(
        default=True,
        description="Grant access.role on the engine to the app's Discovery Engine service agent, so the app can "
        "call the agent.",
    )
    authorization: Authorization | None = None


class AgentRegistry(_Model):
    """Registration of the agent's A2A card as a service in the Agent Registry."""

    location: str | None = Field(default=None, description="Registry location; defaults to provider.region.")
    service_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$", description="Defaults to <service>-<stage>."
    )
    display_name: str | None = Field(default=None, description="Defaults to the card's name.")
    description: str | None = Field(default=None, description="Defaults to the card's description.")


class Publish(_Model):
    """Post-deploy publication targets."""

    agent_registry: AgentRegistry | None = None
    gemini_enterprise: GeminiEnterprise | None = None


_MEMBER_PREFIXES = ("user:", "group:", "serviceAccount:", "domain:", "principal://", "principalSet://")
_PUBLIC_MEMBERS = ("allUsers", "allAuthenticatedUsers")


class Access(_Model):
    """Who may call the agent: IAM bindings on the reasoning engine itself, not the project."""

    invokers: list[str] = Field(
        default_factory=list, description="IAM members granted `role` on the engine (user:, group:, …)."
    )
    role: str = Field(
        default="roles/aiplatform.user",
        description="Role granted on the engine. For least privilege, use a custom role that holds only "
        "aiplatform.reasoningEngines.query, e.g. projects/<project>/roles/agentInvoker.",
    )

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if not v.startswith(("roles/", "projects/", "organizations/")):
            raise ValueError(f"invalid role {v!r}")
        return v

    @field_validator("invokers")
    @classmethod
    def _members(cls, v: list[str]) -> list[str]:
        for member in v:
            if member in _PUBLIC_MEMBERS:
                raise ValueError(f"{member} cannot invoke an agent: callers must be granted by identity (IAM)")
            if not member.startswith(_MEMBER_PREFIXES):
                raise ValueError(f"invalid IAM member {member!r} (expected user:, group:, serviceAccount:, …)")
        return v


class MonitorFilter(_Model):
    """Which traces an online monitor scores; all set bounds must hold."""

    min_duration_seconds: float | None = Field(default=None, ge=0, description="Only traces at least this long.")
    max_duration_seconds: float | None = Field(default=None, ge=0, description="Only traces at most this long.")
    min_total_tokens: int | None = Field(default=None, ge=0, description="Only traces using at least this many tokens.")
    max_total_tokens: int | None = Field(default=None, ge=0, description="Only traces using at most this many tokens.")


class EvalMonitor(_Model):
    """An online monitor: samples the deployed agent's traces and scores them about every 10 minutes."""

    metrics: list[str] = Field(
        min_length=1,
        description="Predefined metrics, e.g. safety, final_response_quality, tool_use_quality (latest version) or "
        "a versioned spec name such as safety_v1.",
    )
    sampling_percent: int = Field(default=10, ge=1, le=100, description="Share of traces to score.")
    max_samples_per_run: int | None = Field(default=None, ge=1, description="Cost cap per evaluation run.")
    filter: MonitorFilter = Field(default_factory=MonitorFilter)
    enabled: bool = Field(default=True, description="false suspends the monitor and keeps its configuration.")
    display_name: str | None = Field(default=None, max_length=63, description="Defaults to <service>-<stage>-<key>.")


class Evaluation(_Model):
    """Evaluation of the deployed agent: on demand (`agentless eval`), after deploy, and online monitors."""

    dataset: str | None = Field(
        default=None, description="agents-cli EvaluationDataset (JSON with eval_cases), relative to agent.yaml."
    )
    dest: str | None = Field(
        default=None,
        pattern=r"^gs://[^/]+",
        description="GCS prefix for evaluation results. Defaults to gs://<stagingBucket>/agentless/<service>/<stage>/"
        "evals when provider.stagingBucket is set.",
    )
    metrics: dict[str, float | None] = Field(
        default_factory=dict,
        description="Metric name → minimum average score (0 to 1); null reports the metric without gating on it.",
    )
    after_deploy: bool = Field(
        default=False, description="Run the evaluation at the end of `deploy` and fail when a threshold is missed."
    )
    monitors: dict[str, EvalMonitor] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def _thresholds(cls, v: dict[str, float | None]) -> dict[str, float | None]:
        for name, threshold in v.items():
            if threshold is not None and not 0 <= threshold <= 1:
                raise ValueError(f"threshold of {name} must be between 0 and 1, got {threshold}")
        return v

    @field_validator("monitors")
    @classmethod
    def _monitor_keys(cls, v: dict[str, EvalMonitor]) -> dict[str, EvalMonitor]:
        for key in v:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", key):
                raise ValueError(f"monitor key {key!r} must match [a-z][a-z0-9-]{{0,30}}")
        return v

    @model_validator(mode="after")
    def _runnable(self) -> Evaluation:
        if self.after_deploy and not (self.dataset and self.metrics):
            raise ValueError("afterDeploy needs a dataset and metrics")
        return self


class AgentConfig(_Model):
    """Root of `agent.yaml`."""

    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    framework_version: str = "1"
    provider: Provider
    stages: dict[str, StageConfig] = Field(default_factory=dict)
    agent: Agent = Field(default_factory=Agent)
    identity: Identity = Field(default_factory=lambda: Identity(type=IdentityType.PLATFORM))
    access: Access = Field(default_factory=Access)
    network: Network = Field(default_factory=Network)
    memory: Memory = Field(default_factory=Memory)
    publish: Publish = Field(default_factory=Publish)
    observability: Observability = Field(default_factory=Observability)
    evaluation: Evaluation = Field(default_factory=Evaluation)
    resources: Resources = Field(default_factory=Resources)
    cloud_run: CloudRun | None = Field(
        default=None, description="Cloud Run settings; only with provider.name: cloud-run."
    )
    custom: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bucket_env(self) -> AgentConfig:
        for key, bucket in self.resources.buckets.items():
            if bucket.env and bucket.env in self.agent.environment:
                raise ValueError(
                    f"resources.buckets.{key}.env sets {bucket.env}, which agent.environment also sets; keep one"
                )
        return self

    @model_validator(mode="after")
    def _legacy_telemetry(self) -> AgentConfig:
        legacy = self.agent.telemetry
        if legacy is None:
            return self
        if "tracing" in self.observability.model_fields_set:
            raise ValueError("set observability.tracing or the deprecated agent.telemetry, not both")
        self.observability.tracing = Tracing(enabled=legacy.enabled, capture_content=legacy.capture_message_content)
        return self

    @model_validator(mode="after")
    def _monitors_need_content(self) -> AgentConfig:
        tracing = self.observability.tracing
        if self.evaluation.monitors and not (tracing.enabled and tracing.capture_content):
            raise ValueError(
                "evaluation.monitors score prompts and responses from traces: set observability.tracing.enabled "
                "and observability.tracing.captureContent to true"
            )
        return self

    @model_validator(mode="after")
    def _target(self) -> AgentConfig:
        if self.provider.name != "cloud-run":
            if self.cloud_run is not None:
                raise ValueError("cloudRun is only valid with provider.name: cloud-run")
            return self
        self.cloud_run = self.cloud_run or CloudRun()
        ge = self.publish.gemini_enterprise
        if ge is not None and "registration" not in ge.model_fields_set:
            ge.registration = "a2a"  # Gemini Enterprise reaches a Cloud Run agent through its A2A card
        unsupported = [
            (self.identity.type != IdentityType.SERVICE_ACCOUNT, "identity.type must be serviceAccount"),
            (self.network.psc_interface is not None, "network.pscInterface: use cloudRun.vpcEgress"),
            (self.agent.runtime.server_mode is not None, "agent.runtime.serverMode is Agent Runtime only"),
            (bool(self.evaluation.monitors), "evaluation.monitors are not supported on Cloud Run yet"),
            (self.evaluation.after_deploy, "evaluation.afterDeploy is not supported on Cloud Run yet"),
            (ge is not None and ge.registration == "adk", "publish.geminiEnterprise.registration must be a2a"),
            (
                "role" in self.access.model_fields_set,
                "access.role is fixed to roles/run.invoker on Cloud Run; remove it",
            ),
            (
                not self.cloud_run.image and not self.provider.staging_bucket,
                "building the source needs provider.stagingBucket (or set cloudRun.image)",
            ),
        ]
        if self.cloud_run.auth != "iam":
            reserved = sorted((set(self.agent.environment) | set(self.agent.secrets)) & set(AUTH_ENV))
            unsupported.append(
                (bool(reserved), f"{', '.join(reserved)} are set by cloudRun.auth: {self.cloud_run.auth}")
            )
        if self.cloud_run.auth == "webhook":
            unsupported += [
                (bool(self.access.invokers), "access.invokers grant nothing with cloudRun.auth: webhook; remove them"),
                (
                    ge is not None or self.publish.agent_registry is not None,
                    (
                        "publish targets call the agent with Google tokens, not the webhook secret: "
                        "remove publish with cloudRun.auth: webhook"
                    ),
                ),
            ]
        problems = [message for failed, message in unsupported if failed]
        if problems:
            raise ValueError("provider cloud-run: " + "; ".join(problems))
        return self

    @property
    def session_engine(self) -> bool:
        """Whether a Cloud Run agent needs a companion engine to hold its sessions and Memory Bank."""
        return self.provider.name == "cloud-run" and (
            self.memory.sessions == "agentPlatform" or self.memory.memory_bank is not None
        )

    @property
    def run_service_name(self) -> str:
        """Cloud Run service name, unique per service and stage."""
        name = (self.cloud_run.service_name if self.cloud_run else None) or f"{self.service}-{self.provider.stage}"
        return name[:49].rstrip("-")

    def deprecations(self) -> list[str]:
        """Settings that still work but should be migrated."""
        if self.agent.telemetry is None:
            return []
        return [
            (
                "agent.telemetry is deprecated: move it to observability.tracing "
                "(enabled, captureContent instead of captureMessageContent)"
            )
        ]

    @property
    def display_name(self) -> str:
        """Engine display name, unique per service and stage."""
        return self.agent.display_name or f"{self.service}-{self.provider.stage}"
