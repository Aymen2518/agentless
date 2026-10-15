"""In-memory stand-in for GcpClients that records every call."""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agentless.providers.agent_runtime.clients import _GE_AGENT_FIELDS, ge_agent_mask


class FakeGcp:
    def __init__(self, project: str = "proj-dev", region: str = "europe-west1"):
        self.project, self.region = project, region
        self.calls: list[tuple[Any, ...]] = []
        self.service_accounts: dict[str, dict[str, Any]] = {}
        self.policies: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.engines: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.ge_agents: dict[str, dict[str, Any]] = {}
        self.ge_authorizations: dict[str, dict[str, Any]] = {}
        self.ge_gateways: dict[str, str] = {}  # app -> egress Agent Gateway
        self.registry: dict[str, dict[str, Any]] = {}
        self.buckets: dict[str, dict[str, Any]] = {}
        self.bucket_objects: dict[str, int] = defaultdict(int)
        self.absent_buckets: set[str] = set()  # IAM targets that do not exist (others are assumed to)
        self.ops_done_immediately = True
        self._ids = itertools.count(1)

    # project / SA
    def project_number(self) -> str:
        return "123456"

    def get_service_account(self, email: str) -> dict[str, Any] | None:
        return self.service_accounts.get(email)

    def create_service_account(self, account_id: str, display_name: str, description: str) -> str:
        email = f"{account_id}@{self.project}.iam.gserviceaccount.com"
        self.calls.append(("create_sa", email))
        self.service_accounts[email] = {"email": email, "display_name": display_name}
        return email

    def update_service_account_display_name(self, email: str, display_name: str) -> None:
        self.calls.append(("update_sa", email))
        self.service_accounts[email]["display_name"] = display_name

    def delete_service_account(self, email: str) -> None:
        self.calls.append(("delete_sa", email))
        self.service_accounts.pop(email, None)

    # IAM
    def iam_members(self, rtype: str, name: str) -> dict[str, set[str]] | None:
        if rtype == "bucket" and name in self.absent_buckets:
            return None
        return {r: set(m) for r, m in self.policies[(rtype, name)].items()}

    def iam_modify(self, rtype: str, name: str, add: Any, remove: Any) -> None:
        add, remove = list(add), list(remove)
        if rtype == "bucket" and name in self.absent_buckets:
            raise RuntimeError(f"404 bucket {name} not found")
        for _, member in add:
            if member.startswith(("principal://", "principalSet://")) and ".system.id.goog/" not in member:
                raise RuntimeError(f"400 The member {member} is of an unknown type")
        self.calls.append(("iam", rtype, name, sorted(add), sorted(remove)))
        for role, member in add:
            self.policies[(rtype, name)][role].add(member)
        for role, member in remove:
            self.policies[(rtype, name)][role].discard(member)

    # buckets
    def bucket_get(self, name: str) -> dict[str, Any] | None:
        b = self.buckets.get(name)
        return None if b is None else {"name": name, **{k: (dict(v) if k == "labels" else v) for k, v in b.items()}}

    def bucket_create(self, name: str, spec: dict[str, Any]) -> None:
        if name in self.buckets:
            raise RuntimeError(f"409 bucket {name} already exists")
        self.calls.append(("bucket_create", name, spec))
        self.buckets[name] = {**spec, "labels": dict(spec["labels"]), "project_number": "123456"}
        self.absent_buckets.discard(name)

    def bucket_update(self, name: str, spec: dict[str, Any]) -> None:
        self.calls.append(("bucket_update", name, spec))
        live = self.buckets[name]
        live.update({k: v for k, v in spec.items() if k not in ("labels", "location")})
        live["labels"] = {**live.get("labels", {}), **spec["labels"]}

    def bucket_is_empty(self, name: str) -> bool:
        return not self.bucket_objects[name]

    def bucket_delete(self, name: str) -> None:
        if self.bucket_objects[name]:
            raise RuntimeError(f"409 bucket {name} is not empty")
        self.calls.append(("bucket_delete", name))
        self.buckets.pop(name, None)
        self.absent_buckets.add(name)

    # engines
    def _op(self, engine: str) -> str:
        name = f"{engine}/operations/{next(self._ids)}"
        self.operations[name] = {"done": self.ops_done_immediately, "error": None}
        return name

    def engine_get(self, name: str) -> dict[str, Any] | None:
        e = self.engines.get(name)
        return (
            None
            if e is None
            else {
                "name": name,
                "display_name": e["display_name"],
                "effective_identity": e.get("effective_identity"),
                "service_account": None,
            }
        )

    def engine_find(self, display_name: str) -> list[str]:
        return [n for n, e in self.engines.items() if e["display_name"] == display_name]

    def engine_code_spec(
        self, source_dir: Path, source_packages: list[str], build_args: dict[str, str], framework: str
    ) -> tuple[dict[str, Any], list[str]]:
        return (
            {
                "source_code_spec": {
                    "inline_source": {"source_archive": f"{len(source_packages)} files"},
                    "image_spec": {"build_args": build_args},
                }
            },
            ["spec.source_code_spec.inline_source.source_archive", "spec.source_code_spec.image_spec"],
        )

    def engine_create(self, config: dict[str, Any]) -> str:
        name = f"projects/123456/locations/{self.region}/reasoningEngines/{next(self._ids)}"
        self.engines[name] = {"display_name": config["display_name"], "config": config}
        if config.get("spec", {}).get("identity_type") == "AGENT_IDENTITY":
            self.engines[name]["effective_identity"] = f"agents.global.org-1.system.id.goog/resources/aiplatform/{name}"
        elif config.get("spec", {}).get("service_account"):
            # Like the live API: a service-account engine reports the SA email as its effective identity.
            self.engines[name]["effective_identity"] = config["spec"]["service_account"]
        self.calls.append(("engine_create", name, config))
        return self._op(name)

    def engine_create_identity(
        self, display_name: str, labels: dict[str, str], encryption_spec: Any = None
    ) -> dict[str, Any]:
        name = f"projects/123456/locations/{self.region}/reasoningEngines/{next(self._ids)}"
        identity = f"agents.global.org-1.system.id.goog/resources/aiplatform/{name}"
        self.engines[name] = {"display_name": display_name, "effective_identity": identity}
        self.calls.append(("engine_create_identity", name, encryption_spec))
        return {"name": name, "effective_identity": identity}

    def engine_update(self, name: str, config: dict[str, Any]) -> str:
        if name not in self.engines:
            raise RuntimeError(f"404 engine {name} not found")
        if "encryption_spec" in config.get("update_mask", "").split(","):
            raise RuntimeError("400 INVALID_ARGUMENT: Cannot update encryption_spec in ReasoningEngine.")
        self.calls.append(("engine_update", name, config))
        return self._op(name)

    def operation_status(self, operation: str) -> tuple[bool, str | None]:
        op = self.operations[operation]
        return op["done"], op["error"]

    def wait(self, operation: str, on_tick: Any = None, poll: float = 10) -> None:
        self.operations[operation]["done"] = True

    def engine_delete(self, name: str) -> None:
        self.calls.append(("engine_delete", name))
        self.engines.pop(name, None)

    # A2A card served by an engine (url = the engine's A2A endpoint, as agents-cli's A2A app does)
    def a2a_card(self, url: str) -> dict[str, Any] | None:
        engine = url.split("/reasoningEngines/v1/", 1)[1].split("/api/", 1)[0]
        if engine not in self.engines:
            return None
        self.calls.append(("a2a_card", url))
        base = url.removesuffix("/.well-known/agent-card.json")
        version = self.engines[engine].get("card_version", 1)
        return {"name": "sample-agent", "description": f"card v{version}", "url": base, "version": str(version)}

    # Gemini Enterprise
    @staticmethod
    def _ge_target(payload: dict[str, Any]) -> dict[str, Any]:
        if "adk_agent_definition" in payload:
            return {"engine": payload["adk_agent_definition"]["provisioned_reasoning_engine"]["reasoning_engine"]}
        return {"card_url": json.loads(payload["a2a_agent_definition"]["json_agent_card"])["url"]}

    def ge_find_agent(self, app: str, *, engine: str | None = None, card_url: str | None = None) -> str | None:
        return next(
            (
                n
                for n, a in self.ge_agents.items()
                if n.startswith(app)
                and ((engine and a.get("engine") == engine) or (card_url and a.get("card_url") == card_url))
            ),
            None,
        )

    def ge_create_agent(self, app: str, payload: dict[str, Any]) -> str:
        name = f"{app}/assistants/default_assistant/agents/{next(self._ids)}"
        self.ge_agents[name] = {**self._ge_target(payload), "payload": payload}
        self.calls.append(("ge_create", name))
        return name

    def ge_patch_agent(self, name: str, payload: dict[str, Any]) -> None:
        if name not in self.ge_agents:
            raise RuntimeError(f"404 agent {name} not found")
        self.calls.append(("ge_patch", name))
        # Like the API: only masked fields change; a masked field missing from the payload is cleared.
        masked = {key for key, mask in _GE_AGENT_FIELDS if mask in ge_agent_mask(payload)}
        merged = {k: v for k, v in self.ge_agents[name]["payload"].items() if k not in masked}
        merged |= {k: v for k, v in payload.items() if k in masked}
        self.ge_agents[name] = {**self._ge_target(merged), "payload": merged}

    def ge_delete_agent(self, name: str) -> None:
        self.calls.append(("ge_delete", name))
        self.ge_agents.pop(name, None)

    def ge_upsert_authorization(self, app: str, auth_id: str, oauth: dict[str, Any]) -> str:
        parts = app.split("/")
        name = f"projects/{parts[1]}/locations/{parts[3]}/authorizations/{auth_id}"
        self.calls.append(("ge_auth", name))
        self.ge_authorizations[name] = oauth
        return name

    def ge_delete_authorization(self, name: str) -> None:
        self.calls.append(("ge_auth_delete", name))
        self.ge_authorizations.pop(name, None)

    def ge_app_gateway(self, app: str) -> str | None:
        return self.ge_gateways.get(app)

    def ge_set_app_gateway(self, app: str, gateway: str) -> None:
        self.calls.append(("ge_gateway", app, gateway))
        self.ge_gateways[app] = gateway

    # Agent Registry
    def registry_get(self, name: str) -> dict[str, Any] | None:
        return self.registry.get(name)

    def registry_create(self, parent: str, service_id: str, body: dict[str, Any]) -> None:
        name = f"{parent}/services/{service_id}"
        if name in self.registry:
            raise RuntimeError(f"409 service {name} already exists")
        self.calls.append(("registry_create", name))
        self.registry[name] = body

    def registry_patch(self, name: str, body: dict[str, Any]) -> None:
        if name not in self.registry:
            raise RuntimeError(f"404 service {name} not found")
        self.calls.append(("registry_patch", name))
        self.registry[name] = body

    def registry_delete(self, name: str) -> None:
        self.calls.append(("registry_delete", name))
        self.registry.pop(name, None)

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]
