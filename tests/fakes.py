"""In-memory stand-in for GcpClients that records every call."""

from __future__ import annotations

import itertools
from collections import defaultdict
from pathlib import Path
from typing import Any


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
        return {r: set(m) for r, m in self.policies[(rtype, name)].items()}

    def iam_modify(self, rtype: str, name: str, add: Any, remove: Any) -> None:
        add, remove = list(add), list(remove)
        for _, member in add:
            if member.startswith(("principal://", "principalSet://")) and ".system.id.goog/" not in member:
                raise RuntimeError(f"400 The member {member} is of an unknown type")
        self.calls.append(("iam", rtype, name, sorted(add), sorted(remove)))
        for role, member in add:
            self.policies[(rtype, name)][role].add(member)
        for role, member in remove:
            self.policies[(rtype, name)][role].discard(member)

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

    # Gemini Enterprise
    def ge_find_agent(self, app: str, engine_name: str) -> str | None:
        return next((n for n, a in self.ge_agents.items() if a["engine"] == engine_name and n.startswith(app)), None)

    def ge_create_agent(self, app: str, payload: dict[str, Any]) -> str:
        name = f"{app}/assistants/default_assistant/agents/{next(self._ids)}"
        engine = payload["adk_agent_definition"]["provisioned_reasoning_engine"]["reasoning_engine"]
        self.ge_agents[name] = {"engine": engine, "payload": payload}
        self.calls.append(("ge_create", name))
        return name

    def ge_patch_agent(self, name: str, payload: dict[str, Any]) -> None:
        self.calls.append(("ge_patch", name))
        engine = payload["adk_agent_definition"]["provisioned_reasoning_engine"]["reasoning_engine"]
        self.ge_agents[name] = {"engine": engine, "payload": payload}

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

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]
