"""Managed resources of an Agent Runtime deployment, applied in order and destroyed in reverse."""

from __future__ import annotations

import datetime
import hashlib
import json
from collections import defaultdict
from typing import Any

from agentless.config.schema import IdentityType
from agentless.plan.model import Action, Change, Context, Resource
from agentless.providers.agent_runtime import spec as engine_spec

_GE_ICON = "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/smart_toy/default/24px.svg"

IamKey = tuple[str, str, str, str]  # (resource type, resource name, role, member)


def service_account_email(ctx: Context) -> str | None:
    """Runtime SA email for serviceAccount identity."""
    identity = ctx.project.config.identity
    if identity.type != IdentityType.SERVICE_ACCOUNT or identity.service_account is None:
        return None
    sa = identity.service_account
    return sa.email if not sa.create else f"{sa.name}@{ctx.project.config.provider.project}.iam.gserviceaccount.com"


def agent_principal(identity: str | None) -> str | None:
    """The identity if it is an Agent Identity principal; None for the SA email a service-account engine reports."""
    return identity if identity and ".system.id.goog/" in identity else None


def runtime_member(ctx: Context) -> str | None:
    """IAM member the agent runs as; None while an Agent Identity principal does not exist yet."""
    identity_type = ctx.project.config.identity.type
    if identity_type == IdentityType.SERVICE_ACCOUNT:
        return f"serviceAccount:{service_account_email(ctx)}"
    if identity_type == IdentityType.PLATFORM:
        return f"serviceAccount:service-{ctx.clients.project_number()}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
    principal = agent_principal((ctx.state.resources.get("engine") or {}).get("effectiveIdentity"))
    return f"principal://{principal}" if principal else None


class ServiceAccountResource(Resource):
    """The runtime service account (identity.type = serviceAccount)."""

    key = "serviceAccount"

    def _desired(self, ctx: Context) -> dict[str, Any] | None:
        identity = ctx.project.config.identity
        if identity.type != IdentityType.SERVICE_ACCOUNT or identity.service_account is None:
            return None
        sa = identity.service_account
        return {
            "email": service_account_email(ctx),
            "create": sa.create,
            "display_name": sa.display_name or f"agentless {ctx.project.config.service} ({ctx.project.stage})",
        }

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        desired, st = self._desired(ctx), self._state(ctx)
        old_owned = st.get("email") if st.get("created") else None
        if desired is None:
            if old_owned:
                return Change(self.key, Action.DELETE, old_owned, data={"cleanup": old_owned})
            return Change(self.key, Action.NOOP, "not used")
        cleanup = {"cleanup": old_owned} if old_owned and old_owned != desired["email"] else {}
        live = ctx.clients.get_service_account(desired["email"])
        if not desired["create"]:
            if live is None:
                return Change(self.key, Action.NOOP, desired["email"], blocked="service account does not exist")
            return Change(
                self.key, Action.UPDATE if cleanup else Action.NOOP, f"{desired['email']} (existing)", data=cleanup
            )
        if live is None:
            return Change(self.key, Action.REPLACE if cleanup else Action.CREATE, desired["email"], data=cleanup)
        if live["display_name"] != desired["display_name"] and st.get("created"):
            return Change(
                self.key,
                Action.UPDATE,
                desired["email"],
                [f"~ displayName: {live['display_name']!r} → {desired['display_name']!r}"],
                data=cleanup,
            )
        note = "" if st.get("email") == desired["email"] else " (exists, not managed)"
        return Change(self.key, Action.UPDATE if cleanup else Action.NOOP, desired["email"] + note, data=cleanup)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        desired = self._desired(ctx)
        if desired is None:
            return
        st = self._state(ctx)
        created = bool(st.get("created")) and st.get("email") == desired["email"]
        live = ctx.clients.get_service_account(desired["email"])
        if live is None and desired["create"]:
            account_id = desired["email"].split("@")[0]
            ctx.clients.create_service_account(
                account_id, desired["display_name"], f"Managed by agentless for {ctx.project.config.service}"
            )
            created = True
        elif live and created and live["display_name"] != desired["display_name"]:
            ctx.clients.update_service_account_display_name(desired["email"], desired["display_name"])
        # Keep the old owned account in state until cleanup so a crash never loses track of it.
        self._set_state(
            ctx,
            {
                "email": desired["email"],
                "created": created,
                **({"previous": change.data["cleanup"]} if change.data.get("cleanup") else {}),
            },
        )

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        old = change.data.get("cleanup")
        if not old:
            return
        ctx.clients.delete_service_account(old)
        st = self._state(ctx)
        st.pop("previous", None)
        self._set_state(ctx, st if st.get("email") and st.get("email") != old else None)

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if st.get("created"):
            return Change(self.key, Action.DELETE, st["email"])
        return Change(self.key, Action.NOOP, f"{st['email']} (not created by agentless, kept)" if st else "none")

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        for email in filter(None, [st.get("previous"), st.get("email") if st.get("created") else None]):
            ctx.clients.delete_service_account(email)
        self._set_state(ctx, None)


class AgentIdentityResource(Resource):
    """Bare engine created first so the Agent Identity principal exists before IAM and code."""

    key = "agentIdentity"

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        if ctx.project.config.identity.type != IdentityType.AGENT_IDENTITY:
            return Change(self.key, Action.NOOP, "not used")
        engine = ctx.state.resources.get("engine") or {}
        if principal := agent_principal(engine.get("effectiveIdentity")):
            return Change(self.key, Action.NOOP, principal)
        details = [f"replaces {engine['name']} (identity type change)"] if engine.get("name") else []
        return Change(self.key, Action.CREATE, "bare engine to mint the agent principal", details)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if change.action != Action.CREATE:
            return
        cfg = ctx.project.config
        old = (ctx.state.resources.get("engine") or {}).get("name")
        created = self._orphan(ctx, old)
        if created is None:
            spec = engine_spec.desired_spec(ctx.project, service_account=None, engine_name=None)
            created = ctx.clients.engine_create_identity(cfg.display_name, spec["labels"], spec["encryption_spec"])
        entry: dict[str, Any] = {
            "name": created["name"],
            "effectiveIdentity": created["effective_identity"],
            "created": True,
            "spec": None,
            "sourceHash": None,
        }
        if old and old != created["name"]:
            entry["previous"] = old  # deleted by the engine step once the replacement is live
        ctx.state.resources["engine"] = entry
        ctx.echo(f"    principal://{created['effective_identity']}")
        ctx.save()

    def _orphan(self, ctx: Context, old: str | None) -> dict[str, Any] | None:
        """Reuse a bare identity engine left by an interrupted run instead of minting a second one."""
        for name in ctx.clients.engine_find(ctx.project.config.display_name):
            live = ctx.clients.engine_get(name) or {}
            if name != old and agent_principal(live.get("effective_identity")):
                return {"name": name, "effective_identity": live["effective_identity"]}
        return None

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        return Change(self.key, Action.NOOP, "removed with the engine")

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        return None


def _target(k: IamKey) -> str:
    return f"{k[0]}/{k[1]}"


class IamResource(Resource):
    """Role bindings for the runtime identity; only bindings agentless added are ever removed."""

    key = "iam"

    def desired(self, ctx: Context, member: str) -> set[IamKey]:
        """All bindings implied by agent.yaml, including secret and artifact access."""
        cfg = ctx.project.config
        roles = cfg.identity.roles
        out: set[IamKey] = {("project", cfg.provider.project, r, member) for r in roles.project}
        if roles.organization:
            out |= {("organization", roles.organization.id, r, member) for r in roles.organization.roles}
        for res in roles.resources:
            out |= {(res.type, res.name, r, member) for r in res.roles}
        out |= {("secret", s.secret, "roles/secretmanager.secretAccessor", member) for s in cfg.agent.secrets.values()}
        if cfg.memory.artifacts_bucket:
            out.add(("bucket", cfg.memory.artifacts_bucket.removeprefix("gs://"), "roles/storage.objectUser", member))
        return out

    def _owned(self, ctx: Context) -> set[IamKey]:
        return {tuple(b) for b in self._state(ctx).get("bindings", [])}  # type: ignore[misc]

    def _save_owned(self, ctx: Context, owned: set[IamKey]) -> None:
        self._set_state(ctx, {"bindings": sorted(list(k) for k in owned)} if owned else None)

    def _diff(self, ctx: Context, desired: set[IamKey]) -> tuple[set[IamKey], set[IamKey], set[IamKey], list[str]]:
        """(to add, to remove, already granted outside agentless, missing target resources)."""
        owned = self._owned(ctx)
        live: dict[tuple[str, str], dict[str, set[str]]] = {}
        missing: list[str] = []
        for rtype, name in sorted({(k[0], k[1]) for k in desired | owned}):
            members = ctx.clients.iam_members(rtype, name)
            if members is None:
                missing.append(f"{rtype}/{name}")
            live[(rtype, name)] = members or {}

        def granted(k: IamKey) -> bool:
            return k[3] in live[(k[0], k[1])].get(k[2], set())

        to_add = {k for k in desired if not granted(k)}
        to_remove = {k for k in owned - desired if granted(k)}
        foreign = {k for k in desired - owned if granted(k)}
        return to_add, to_remove, foreign, missing

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        member = runtime_member(ctx)
        if member is None:
            pending = self.desired(ctx, "<agent principal>")
            return Change(
                self.key,
                Action.CREATE,
                f"{len(pending)} binding(s), principal known after apply",
                [f"+ {k[2]} on {_target(k)}" for k in sorted(pending)],
            )
        desired = self.desired(ctx, member)
        to_add, to_remove, foreign, missing = self._diff(ctx, desired)
        missing_desired = sorted({_target(k) for k in desired if _target(k) in missing})
        details = [f"+ {k[2]} on {_target(k)} → {k[3]}" for k in sorted(to_add)]
        details += [f"- {k[2]} on {_target(k)} → {k[3]}" for k in sorted(to_remove)]
        details += [f"= {k[2]} on {_target(k)} (already granted, not managed)" for k in sorted(foreign)]
        blocked = (
            f"target resources do not exist: {', '.join(missing_desired)} "
            "(agentless grants access to data resources, it does not create them)"
            if missing_desired
            else None
        )
        if not to_add and not to_remove:
            return Change(
                self.key, Action.NOOP, f"{len(self._owned(ctx))} managed binding(s)", details, blocked=blocked
            )
        return Change(
            self.key,
            Action.UPDATE if self._owned(ctx) else Action.CREATE,
            f"{len(to_add)} to grant, {len(to_remove)} to revoke",
            details,
            blocked=blocked,
        )

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        member = runtime_member(ctx)
        if member is None:
            raise RuntimeError("agent principal is unknown; the agentIdentity step did not run")
        desired = self.desired(ctx, member)
        to_add, to_remove, _, missing = self._diff(ctx, desired)
        # A removed target (e.g. deleted bucket) has nothing left to revoke.
        to_remove = {k for k in to_remove if _target(k) not in missing}
        # Owned but undesired bindings that are already gone need no revoke and stop being tracked.
        owned = self._owned(ctx) - ((self._owned(ctx) - desired) - to_remove)
        for target in sorted({_target(k) for k in to_add | to_remove}):
            add = {k for k in to_add if _target(k) == target}
            remove = {k for k in to_remove if _target(k) == target}
            self._modify(ctx, add, remove)
            owned = (owned | add) - remove
            self._save_owned(ctx, owned)  # per target, so a later failure never loses track of earlier grants
        self._save_owned(ctx, owned)

    def _modify(self, ctx: Context, to_add: set[IamKey], to_remove: set[IamKey]) -> None:
        grouped: dict[tuple[str, str], dict[str, list[tuple[str, str]]]] = defaultdict(lambda: {"add": [], "rm": []})
        for k in to_add:
            grouped[(k[0], k[1])]["add"].append((k[2], k[3]))
        for k in to_remove:
            grouped[(k[0], k[1])]["rm"].append((k[2], k[3]))
        for (rtype, name), ops in sorted(grouped.items()):
            ctx.clients.iam_modify(rtype, name, ops["add"], ops["rm"])

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        owned = self._owned(ctx)
        if not owned:
            return Change(self.key, Action.NOOP, "no managed bindings")
        return Change(
            self.key,
            Action.DELETE,
            f"revoke {len(owned)} binding(s)",
            [f"- {k[2]} on {_target(k)} → {k[3]}" for k in sorted(owned)],
        )

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        owned = self._owned(ctx)
        failures = []
        for target in sorted({_target(k) for k in owned}):
            keys = {k for k in owned if _target(k) == target}
            try:
                self._modify(ctx, set(), keys)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "NotFound":
                    owned -= keys
                else:
                    failures.append(f"{target}: {e}")
                continue
            owned -= keys
            self._save_owned(ctx, owned)
        if failures:
            ctx.echo("    ⚠ could not revoke: " + "; ".join(failures))
        self._save_owned(ctx, owned)


class EngineResource(Resource):
    """The reasoning engine: config diffed against last-applied spec, code against the source hash."""

    key = "engine"

    def _desired(self, ctx: Context, engine_name: str | None) -> dict[str, Any]:
        return engine_spec.desired_spec(
            ctx.project, service_account=service_account_email(ctx), engine_name=engine_name
        )

    def _stored(self, spec: dict[str, Any], ctx: Context) -> dict[str, Any]:
        """Desired spec as it may appear in state and plan output."""
        return engine_spec.redact(spec, ctx.project.sensitive)

    def _code_changed(self, ctx: Context, st: dict[str, Any], desired: dict[str, Any]) -> bool:
        assert ctx.package is not None
        last = st.get("spec") or {}
        return (
            ctx.options.force
            or st.get("sourceHash") != ctx.package.sha256
            or last.get("build_args") != desired["build_args"]
        )

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if st.get("pending"):
            return Change(
                self.key,
                Action.NOOP,
                st["name"],
                blocked=f"operation {st['pending']['operation']} in progress: run `agentless deploy --status`",
            )
        name = st.get("name")
        data: dict[str, Any] = {}
        if name and ctx.clients.engine_get(name) is None:
            return Change(
                self.key,
                Action.CREATE,
                ctx.project.config.display_name,
                [f"{name} no longer exists; it will be recreated"],
                data={"recreate": True, "old": name},
            )
        if not name and ctx.project.config.identity.type != IdentityType.AGENT_IDENTITY:
            matches = ctx.clients.engine_find(ctx.project.config.display_name)
            if len(matches) > 1:
                return Change(
                    self.key,
                    Action.NOOP,
                    ctx.project.config.display_name,
                    blocked=f"{len(matches)} engines share this display name; set agent.displayName",
                )
            if matches:
                name, data["adopt"] = matches[0], matches[0]
        if not name:
            return Change(
                self.key,
                Action.CREATE,
                ctx.project.config.display_name,
                [f"source {ctx.package.sha256[:12]} ({len(ctx.package.files)} files)"] if ctx.package else [],
            )
        desired = self._stored(self._desired(ctx, name), ctx)
        last = None if data.get("adopt") else st.get("spec")
        changed = engine_spec.diff(last, desired)
        details = [f"adopting existing engine {name} (deployed outside agentless)"] if data.get("adopt") else []
        immutable = [f for f in engine_spec.IMMUTABLE_FIELDS if f in changed and last is not None]
        if immutable and not ctx.options.code_only:
            details += [line for f in immutable for line in engine_spec.describe(f, *changed[f])]
            blocked = (
                None
                if ctx.options.allow_replace
                else (
                    f"{', '.join(immutable)} cannot change in place; rerun with --allow-replace to delete and recreate "
                    "the engine (sessions and memories are lost)"
                )
            )
            return Change(
                self.key, Action.REPLACE, name, details, data={**data, "replace": True, "old": name}, blocked=blocked
            )
        code = self._code_changed(ctx, st, desired)
        fields = [] if ctx.options.code_only else [f for f in changed if f not in engine_spec.CODE_FIELDS]
        for f in fields:
            details += engine_spec.describe(f, *changed[f])
        if code:
            assert ctx.package is not None
            old = (st.get("sourceHash") or "none")[:12]
            details.append(f"~ source: {old} → {ctx.package.sha256[:12]} (rebuild)")
        if ctx.options.code_only and changed:
            details.append(f"config changes deferred by --code-only: {', '.join(sorted(changed))}")
        if not fields and not code and not data.get("adopt"):
            return Change(self.key, Action.NOOP, name)
        return Change(self.key, Action.UPDATE, name, details, data={**data, "fields": fields, "code": code})

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        assert ctx.package is not None
        st: dict[str, Any] = dict(self._state(ctx))
        before = {"name": st.get("name"), "effectiveIdentity": st.get("effectiveIdentity")}
        old = change.data.get("old")
        if change.data.get("replace"):
            ctx.echo(f"    deleting {old} for replacement")
            ctx.clients.engine_delete(old)
        if old and st.get("name") == old:
            st = {}  # replaced or vanished; a fresh Agent Identity shell from this run is kept
        previous = st.pop("previous", None)
        if previous and previous != old:
            ctx.echo(f"    deleting {previous} (superseded)")
            ctx.clients.engine_delete(previous)
        if change.data.get("adopt"):
            st = {"name": change.data["adopt"], "created": True, "spec": None, "sourceHash": None}
        name = st.get("name")
        desired = self._desired(ctx, name)
        code_spec, code_masks = ctx.clients.engine_code_spec(
            ctx.package.root, ctx.package.source_packages, desired["build_args"], desired["agent_framework"]
        )

        if not name:
            config, _ = engine_spec.api_payload(desired)
            config.setdefault("spec", {}).update(code_spec)
            operation = ctx.clients.engine_create(config)
            st = {"name": operation.rpartition("/operations/")[0], "created": True, "spec": None, "sourceHash": None}
            new_spec, new_hash = self._stored(desired, ctx), ctx.package.sha256
        else:
            last = st.get("spec")
            if last is None:
                # Never deployed (bare shell or adopted): send everything, including create-time fields.
                fields = [f for f in desired if f not in engine_spec.CODE_FIELDS and f != "identity_type"]
            else:
                fields = change.data.get("fields", [])
                fields = [f for f in fields if f not in engine_spec.CODE_FIELDS + engine_spec.IMMUTABLE_FIELDS]
            code = last is None or change.data.get("code", True)
            config, masks = engine_spec.api_payload(desired, fields)
            if code:
                config.setdefault("spec", {}).update(code_spec)
                masks += code_masks
            config["update_mask"] = ",".join(masks)
            operation = ctx.clients.engine_update(name, config)
            new_spec = last if ctx.options.code_only and last is not None else self._stored(desired, ctx)
            new_hash = ctx.package.sha256 if code else st.get("sourceHash")

        st["pending"] = {
            "operation": operation,
            "spec": new_spec,
            "sourceHash": new_hash,
            "since": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        }
        self._set_state(ctx, st)
        ctx.echo(f"    operation {operation}")
        if ctx.options.no_wait:
            ctx.echo("    not waiting; check with `agentless deploy --status`")
            return
        self._wait(ctx, operation)
        self.finalize(ctx)
        self._apply_app_url(ctx)
        after = self._state(ctx)
        if after.get("effectiveIdentity") != before["effectiveIdentity"] and before["effectiveIdentity"]:
            ctx.reapply.add("iam")  # a recreated Agent Identity engine has a new principal
        if after.get("name") != before["name"] and before["name"]:
            ctx.reapply.add("geminiEnterprise")

    def _wait(self, ctx: Context, operation: str) -> None:
        ctx.clients.wait(operation, on_tick=lambda s: ctx.echo(f"    … {int(s // 60)}m{int(s % 60):02d}s"))

    def finalize(self, ctx: Context) -> bool:
        """Promote a finished pending operation into state; False while still running."""
        st = dict(self._state(ctx))
        pending = st.get("pending")
        if not pending:
            return True
        done, error = ctx.clients.operation_status(pending["operation"])
        if not done:
            return False
        st.pop("pending")
        if error:
            self._set_state(ctx, st)
            raise RuntimeError(f"engine operation failed: {error}")
        st["spec"], st["sourceHash"] = pending["spec"], pending["sourceHash"]
        live = ctx.clients.engine_get(st["name"]) or {}
        if principal := agent_principal(live.get("effective_identity")):
            st["effectiveIdentity"] = principal
        self._set_state(ctx, st)
        return True

    def _apply_app_url(self, ctx: Context) -> None:
        """After a create the engine name is known, so push the APP_URL env the A2A card needs."""
        st = dict(self._state(ctx))
        if ctx.options.code_only or st.get("spec") is None:
            return
        desired = self._desired(ctx, st["name"])
        stored_env = self._stored(desired, ctx)["env"]
        if stored_env == st["spec"]["env"]:
            return
        ctx.echo("    setting APP_URL now that the engine name is known")
        config, masks = engine_spec.api_payload(desired, ["env"])
        config["update_mask"] = ",".join(masks)
        operation = ctx.clients.engine_update(st["name"], config)
        st["pending"] = {
            "operation": operation,
            "spec": {**st["spec"], "env": stored_env},
            "sourceHash": st["sourceHash"],
            "since": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        }
        self._set_state(ctx, st)
        self._wait(ctx, operation)
        self.finalize(ctx)

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st.get("name"):
            return Change(self.key, Action.NOOP, "none")
        if not st.get("created"):
            return Change(self.key, Action.NOOP, f"{st['name']} (not created by agentless, kept)")
        return Change(self.key, Action.DELETE, st["name"], ["sessions and memories are deleted with it"])

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        if st.get("previous"):
            ctx.clients.engine_delete(st["previous"])
        if st.get("name") and st.get("created"):
            ctx.clients.engine_delete(st["name"])
        self._set_state(ctx, None)


class GeminiEnterpriseResource(Resource):
    """Registration of the engine as an agent in a Gemini Enterprise app, plus its OAuth authorization."""

    key = "geminiEnterprise"

    def _payload(self, ctx: Context, engine_name: str, authorization: str | None) -> dict[str, Any]:
        ge = ctx.project.config.publish.gemini_enterprise
        assert ge is not None
        agent = ctx.project.config.agent
        payload: dict[str, Any] = {
            "displayName": ge.display_name or ctx.project.config.display_name,
            "description": ge.description or agent.description or ctx.project.config.display_name,
            "icon": {"uri": _GE_ICON},
            "adk_agent_definition": {
                "tool_settings": {"tool_description": ge.tool_description or agent.description or ""},
                "provisioned_reasoning_engine": {"reasoning_engine": engine_name},
            },
        }
        if authorization:
            payload["authorization_config"] = {"tool_authorizations": [authorization]}
        return payload

    def _auth_hash(self, ctx: Context) -> str | None:
        ge = ctx.project.config.publish.gemini_enterprise
        if ge is None or ge.authorization is None:
            return None
        return _hash(ge.authorization.model_dump())

    def _auth_name(self, ctx: Context) -> str | None:
        ge = ctx.project.config.publish.gemini_enterprise
        if ge is None or ge.authorization is None:
            return None
        parts = ge.app.split("/")
        return f"projects/{parts[1]}/locations/{parts[3]}/authorizations/{ge.authorization.id}"

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        ge, st = ctx.project.config.publish.gemini_enterprise, self._state(ctx)
        if ge is None:
            if st:
                return Change(self.key, Action.DELETE, st.get("agentName", st.get("app", "")), data={"cleanup": st})
            return Change(self.key, Action.NOOP, "not published")
        if not st:
            return Change(self.key, Action.CREATE, ge.app)
        if st.get("app") != ge.app:
            return Change(
                self.key, Action.REPLACE, ge.app, [f"~ app: {st.get('app')} → {ge.app}"], data={"cleanup": st}
            )
        engine = (ctx.state.resources.get("engine") or {}).get("name") or ""
        changed = st.get("payloadHash") != _hash(self._payload(ctx, engine, self._auth_name(ctx)))
        changed |= st.get("authorizationHash") != self._auth_hash(ctx)
        return Change(self.key, Action.UPDATE if changed else Action.NOOP, st.get("agentName", ge.app))

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        ge = ctx.project.config.publish.gemini_enterprise
        if ge is None:
            return
        engine = (ctx.state.resources.get("engine") or {}).get("name")
        if not engine:
            raise RuntimeError("cannot publish to Gemini Enterprise: engine has no name yet")
        moving = bool(change.data.get("cleanup"))
        current = dict(self._state(ctx))
        st = {} if moving else current
        authorization = self._auth_name(ctx)
        if ge.authorization and (st.get("authorizationHash") != self._auth_hash(ctx) or moving):
            ctx.clients.ge_upsert_authorization(ge.app, ge.authorization.id, ge.authorization.model_dump())
        payload = self._payload(ctx, engine, authorization)
        agent_name = st.get("agentName") or ctx.clients.ge_find_agent(ge.app, engine)
        if agent_name:
            ctx.clients.ge_patch_agent(agent_name, payload)
        else:
            agent_name = ctx.clients.ge_create_agent(ge.app, payload)
        stale_auth = st.get("authorizationName")
        if stale_auth and stale_auth != authorization:
            ctx.clients.ge_delete_authorization(stale_auth)  # removed or renamed; it holds an OAuth secret
        self._set_state(
            ctx,
            {
                "app": ge.app,
                "agentName": agent_name,
                "payloadHash": _hash(payload),
                "authorizationName": authorization,
                "authorizationHash": self._auth_hash(ctx),
                **({"previous": change.data["cleanup"]} if moving else {}),
            },
        )
        ctx.echo(f"    {agent_name}")

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        old = change.data.get("cleanup")
        if not old:
            return
        _unpublish(ctx, old, keep=self._auth_name(ctx))
        st = self._state(ctx)
        st.pop("previous", None)
        self._set_state(ctx, st if ctx.project.config.publish.gemini_enterprise else None)

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "not published")
        return Change(self.key, Action.DELETE, st.get("agentName", st.get("app", "")))

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        if st.get("previous"):
            _unpublish(ctx, st["previous"], keep=st.get("authorizationName"))
        _unpublish(ctx, st)
        self._set_state(ctx, None)


def _unpublish(ctx: Context, entry: dict[str, Any], keep: str | None = None) -> None:
    if entry.get("agentName"):
        ctx.clients.ge_delete_agent(entry["agentName"])
    if entry.get("authorizationName") and entry["authorizationName"] != keep:
        ctx.clients.ge_delete_authorization(entry["authorizationName"])


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


ALL_RESOURCES: tuple[type[Resource], ...] = (
    ServiceAccountResource,
    AgentIdentityResource,
    IamResource,
    EngineResource,
    GeminiEnterpriseResource,
)
