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

    name: Literal["agent-runtime"] = "agent-runtime"
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
            raise ValueError("Agent Runtime needs a regional location, not 'global'")
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


class SecretRef(_Model):
    """Secret Manager reference exposed as an env var."""

    secret: str
    version: str = "latest"


class Telemetry(_Model):
    """Telemetry toggles mapped to the env vars agents-cli sets."""

    enabled: bool = True
    capture_message_content: bool = False


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
    telemetry: Telemetry = Field(default_factory=Telemetry)
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


class Authorization(_Model):
    """OAuth authorization used by Gemini Enterprise to call the agent on the user's behalf."""

    id: str
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
    authorization: Authorization | None = None


class Publish(_Model):
    """Post-deploy publication targets."""

    gemini_enterprise: GeminiEnterprise | None = None


class AgentConfig(_Model):
    """Root of `agent.yaml`."""

    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    framework_version: str = "1"
    provider: Provider
    stages: dict[str, StageConfig] = Field(default_factory=dict)
    agent: Agent = Field(default_factory=Agent)
    identity: Identity = Field(default_factory=lambda: Identity(type=IdentityType.PLATFORM))
    network: Network = Field(default_factory=Network)
    memory: Memory = Field(default_factory=Memory)
    publish: Publish = Field(default_factory=Publish)
    custom: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Engine display name, unique per service and stage."""
        return self.agent.display_name or f"{self.service}-{self.provider.stage}"
