"""Managed resources of an Agent Runtime deployment, applied in order and destroyed in reverse."""

from __future__ import annotations

import datetime
import hashlib
import json
from collections import defaultdict
from typing import Any

from agentless.config.schema import BUCKET_ROLES, IdentityType
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


def bucket_spec(ctx: Context, key: str) -> dict[str, Any]:
    """Settings agentless enforces on a declared bucket, in the shape `clients.bucket_get` returns."""
    cfg = ctx.project.config
    bucket = cfg.resources.buckets[key]
    labels = {**cfg.provider.labels, "agentless-service": cfg.service, "agentless-stage": cfg.provider.stage}
    return {
        "location": (bucket.location or cfg.provider.region).upper(),
        "storage_class": bucket.storage_class,
        "versioning": bucket.versioning,
        "delete_after_days": bucket.lifecycle.delete_after_days,
        "labels": dict(sorted(labels.items())),
        "kms_key": bucket.kms_key,
    }


_BUCKET_FIELDS = ("storage_class", "versioning", "delete_after_days", "kms_key")


def _bucket_diff(desired: dict[str, Any], live: dict[str, Any]) -> list[str]:
    diffs = [f"{f}: {live.get(f)!r} → {desired[f]!r}" for f in _BUCKET_FIELDS if live.get(f) != desired[f]]
    # Labels set outside agentless are left alone; only ours must match.
    labels = {k: v for k, v in desired["labels"].items() if (live.get("labels") or {}).get(k) != v}
    diffs += [f"labels.{k}: {(live.get('labels') or {}).get(k)!r} → {v!r}" for k, v in labels.items()]
    return diffs


def _bucket_line(name: str, spec: dict[str, Any], env: str | None) -> str:
    extras = [spec["location"].lower(), spec["storage_class"]]
    if spec["versioning"]:
        extras.append("versioned")
    if spec["delete_after_days"]:
        extras.append(f"delete after {spec['delete_after_days']}d")
    return f"{name} ({', '.join(extras)})" + (f" → ${env}" if env else "")


class BucketsResource(Resource):
    """GCS buckets declared under resources.buckets. Data is never deleted: buckets are retained or removed empty."""

    key = "buckets"

    def _items(self, ctx: Context) -> dict[str, dict[str, Any]]:
        return dict(self._state(ctx).get("items") or {})

    def _save(self, ctx: Context, items: dict[str, dict[str, Any]]) -> None:
        self._set_state(ctx, {"items": dict(sorted(items.items()))} if items else None)

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        cfg = ctx.project.config
        items = self._items(ctx)
        ops: dict[str, Any] = {"create": [], "update": {}, "adopt": [], "release": []}
        details: list[str] = []
        blocked: list[str] = []
        declared = {b.name: key for key, b in cfg.resources.buckets.items()}
        for name, key in sorted(declared.items()):
            bucket, desired = cfg.resources.buckets[key], bucket_spec(ctx, key)
            live = ctx.clients.bucket_get(name)
            tracked = items.get(name)
            if live is None:
                ops["create"].append(name)
                note = " (missing, recreated empty)" if tracked else ""
                details.append(f"+ {_bucket_line(name, desired, bucket.env)}{note}")
                continue
            if live.get("forbidden"):
                blocked.append(f"{name} exists but the deployer cannot read it; the name may belong to another project")
                continue
            if live.get("project_number") and live["project_number"] != ctx.clients.project_number():
                blocked.append(f"{name} belongs to another project; bucket names are global, pick another name")
                continue
            if live["location"].upper() != desired["location"]:
                blocked.append(
                    f"{name} is in {live['location'].lower()}, agent.yaml says {desired['location'].lower()}: "
                    "a bucket's location can't change and agentless never replaces a bucket; use a new name"
                )
                continue
            diffs = _bucket_diff(desired, live)
            if not tracked:
                ops["adopt"].append(name)
                details.append(f"⇐ adopt {name} (exists in this project, kept on remove)")
            if diffs:
                ops["update"][name] = diffs
                details += [f"~ {name} {d}" for d in diffs]
        for name, entry in sorted(items.items()):
            if name in declared:
                continue
            ops["release"].append(name)
            if entry.get("created") and entry.get("deletionPolicy") == "delete":
                details.append(f"- {name}: delete if empty (deletionPolicy: delete), otherwise kept")
            else:
                details.append(f"- {name}: stop managing; bucket and data kept")
        pending = ops["create"] or ops["update"] or ops["adopt"] or ops["release"]
        reason = "; ".join(blocked) or None
        if not pending:
            summary = f"{len(declared)} managed bucket(s)" if declared else "none declared"
            return Change(self.key, Action.NOOP, summary, details, blocked=reason)
        if not declared:
            action = Action.DELETE
        elif items or ops["adopt"]:
            action = Action.UPDATE
        else:
            action = Action.CREATE
        counts = {"create": len(ops["create"]), "update": len(ops["update"]), "release": len(ops["release"])}
        summary = ", ".join(f"{n} to {verb}" for verb, n in counts.items() if n) or f"adopt {len(ops['adopt'])}"
        if ops["adopt"] and any(counts.values()):
            summary += f", adopt {len(ops['adopt'])}"
        return Change(self.key, action, summary, details, data=ops, blocked=reason)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        cfg = ctx.project.config
        declared = {b.name: key for key, b in cfg.resources.buckets.items()}
        items = self._items(ctx)
        for name, key in sorted(declared.items()):
            spec, policy = bucket_spec(ctx, key), cfg.resources.buckets[key].deletion_policy
            if name in change.data.get("create", []):
                ctx.clients.bucket_create(name, spec)
                items[name] = {"key": key, "created": True, "deletionPolicy": policy}
            else:
                if name in change.data.get("update", {}):
                    ctx.clients.bucket_update(name, spec)
                entry = items.get(name) or {"created": False}
                items[name] = {**entry, "key": key, "deletionPolicy": policy}
            self._save(ctx, items)  # per bucket, so a later failure never loses track of one just created
        for name in change.data.get("release", []):
            items.setdefault(name, {})["release"] = True  # handled in cleanup, after IAM and the engine moved on
        self._save(ctx, items)

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        items = self._items(ctx)
        for name in change.data.get("release", []):
            entry = items.pop(name, None) or {}
            self._release(ctx, name, entry)
            self._save(ctx, items)

    def _release(self, ctx: Context, name: str, entry: dict[str, Any]) -> None:
        if not (entry.get("created") and entry.get("deletionPolicy") == "delete"):
            ctx.echo(f"    {name}: kept (no longer managed by agentless)")
            return
        if not ctx.clients.bucket_is_empty(name):
            ctx.echo(f"    ⚠ {name}: not empty, kept (no longer managed by agentless)")
            return
        ctx.clients.bucket_delete(name)
        ctx.echo(f"    {name}: deleted (was empty)")

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        items = self._items(ctx)
        if not items:
            return Change(self.key, Action.NOOP, "none")
        details = []
        for name, entry in sorted(items.items()):
            if entry.get("created") and entry.get("deletionPolicy") == "delete":
                details.append(f"- {name}: delete if empty, otherwise kept")
            else:
                details.append(f"= {name}: kept with its data")
        return Change(self.key, Action.DELETE, f"release {len(items)} bucket(s)", details)

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        items = self._items(ctx)
        for name in sorted(items):
            self._release(ctx, name, items.pop(name))
            self._save(ctx, items)


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


def _why(k: IamKey, automatic: set[IamKey]) -> str:
    if k not in automatic:
        return ""
    return " (automatic: resources.buckets)" if k[0] == "bucket" else " (automatic: tracing)"


TRACING_ROLES = ("roles/cloudtrace.agent", "roles/logging.logWriter", "roles/monitoring.metricWriter")
INVOKER_ROLE = "roles/aiplatform.user"


def ge_service_agent(ctx: Context) -> str | None:
    """Discovery Engine service agent of the Gemini Enterprise app's project, which calls the published agent."""
    ge = ctx.project.config.publish.gemini_enterprise
    if ge is None or not ge.grant_service_agent:
        return None
    project = ge.app.split("/")[1]
    if not project.isdigit():
        if project != ctx.project.config.provider.project:
            return None  # an id we can't turn into a number; plan says so
        project = ctx.clients.project_number()
    return f"serviceAccount:service-{project}@gcp-sa-discoveryengine.iam.gserviceaccount.com"


class IamResource(Resource):
    """Role bindings for the runtime identity; only bindings agentless added are ever removed."""

    key = "iam"

    def desired(self, ctx: Context, member: str) -> set[IamKey]:
        """All bindings implied by agent.yaml, including secret, artifact and tracing access."""
        return self.declared(ctx, member) | self.automatic(ctx, member)

    def _auto_only(self, ctx: Context, member: str) -> set[IamKey]:
        return self.automatic(ctx, member) - self.declared(ctx, member)

    @staticmethod
    def declared(ctx: Context, member: str) -> set[IamKey]:
        """Bindings agent.yaml asks for, directly or through secrets and the artifacts bucket."""
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

    @staticmethod
    def automatic(ctx: Context, member: str) -> set[IamKey]:
        """Bindings agentless adds on its own: access to declared buckets, and what tracing needs.

        The platform service agent already holds the tracing roles through its own role, so it gets none of those.
        """
        cfg = ctx.project.config
        out: set[IamKey] = {
            ("bucket", b.name, BUCKET_ROLES[b.access], member)
            for b in cfg.resources.buckets.values()
            if b.access != "none"
        }
        if cfg.observability.tracing.enabled and cfg.identity.type != IdentityType.PLATFORM:
            out |= {("project", cfg.provider.project, role, member) for role in TRACING_ROLES}
        return out

    @staticmethod
    def shared(ctx: Context) -> set[IamKey]:
        """Grant-only bindings: added when missing, never recorded or revoked, since other agents may rely on them.

        Invokers from access.invokers, and the Gemini Enterprise app's service agent when the agent is published.
        """
        cfg = ctx.project.config
        project = cfg.provider.project
        out: set[IamKey] = {("project", project, INVOKER_ROLE, m) for m in cfg.access.invokers}
        if member := ge_service_agent(ctx):
            out.add(("project", project, INVOKER_ROLE, member))
        return out

    def _owned(self, ctx: Context) -> set[IamKey]:
        return {tuple(b) for b in self._state(ctx).get("bindings", [])}  # type: ignore[misc]

    def _save_owned(self, ctx: Context, owned: set[IamKey]) -> None:
        self._set_state(ctx, {"bindings": sorted(list(k) for k in owned)} if owned else None)

    def _diff(
        self, ctx: Context, desired: set[IamKey], shared: set[IamKey]
    ) -> tuple[set[IamKey], set[IamKey], set[IamKey], list[str], set[IamKey]]:
        """(to add, to remove, already granted outside agentless, missing target resources, grant-only to add)."""
        owned = self._owned(ctx)
        live: dict[tuple[str, str], dict[str, set[str]]] = {}
        missing: list[str] = []
        for rtype, name in sorted({(k[0], k[1]) for k in desired | owned | shared}):
            members = ctx.clients.iam_members(rtype, name)
            if members is None:
                missing.append(f"{rtype}/{name}")
            live[(rtype, name)] = members or {}

        def granted(k: IamKey) -> bool:
            return k[3] in live[(k[0], k[1])].get(k[2], set())

        to_add = {k for k in desired if not granted(k)}
        # An owned binding that became grant-only stops being tracked instead of being revoked.
        to_remove = {k for k in owned - desired - shared if granted(k)}
        foreign = {k for k in desired - owned if granted(k)}
        shared_add = {k for k in shared - desired if not granted(k)}
        return to_add, to_remove, foreign, missing, shared_add

    def _shared_notes(self, ctx: Context) -> list[str]:
        ge = ctx.project.config.publish.gemini_enterprise
        if ge and ge.grant_service_agent and ge_service_agent(ctx) is None:
            return [
                (
                    "! cannot derive the Gemini Enterprise service agent from the app's project id; use the project "
                    "number in publish.geminiEnterprise.app, or set grantServiceAgent: false and grant it yourself"
                )
            ]
        return []

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        member = runtime_member(ctx)
        shared = self.shared(ctx)
        if member is None:
            pending = self.desired(ctx, "<agent principal>")
            auto = self._auto_only(ctx, "<agent principal>")
            return Change(
                self.key,
                Action.CREATE,
                f"{len(pending)} binding(s), principal known after apply",
                [f"+ {k[2]} on {_target(k)}{_why(k, auto)}" for k in sorted(pending)]
                + [f"+ {k[2]} on {_target(k)} → {k[3]} (grant-only, kept on remove)" for k in sorted(shared)]
                + self._shared_notes(ctx),
            )
        desired = self.desired(ctx, member)
        auto = self._auto_only(ctx, member)
        to_add, to_remove, foreign, missing, shared_add = self._diff(ctx, desired, shared)
        # Buckets declared under resources.buckets are created by the buckets step, before IAM runs.
        created = {f"bucket/{b.name}" for b in ctx.project.config.resources.buckets.values()}
        missing_desired = sorted({_target(k) for k in desired if _target(k) in missing and _target(k) not in created})
        details = [f"+ {k[2]} on {_target(k)} → {k[3]}{_why(k, auto)}" for k in sorted(to_add)]
        details += [f"- {k[2]} on {_target(k)} → {k[3]}" for k in sorted(to_remove)]
        details += [f"= {k[2]} on {_target(k)} (already granted, not managed)" for k in sorted(foreign)]
        details += [f"+ {k[2]} on {_target(k)} → {k[3]} (grant-only, kept on remove)" for k in sorted(shared_add)]
        details += self._shared_notes(ctx)
        blocked = (
            f"target resources do not exist: {', '.join(missing_desired)} "
            "(declare buckets under resources.buckets to have agentless create them)"
            if missing_desired
            else None
        )
        if not to_add and not to_remove and not shared_add:
            return Change(
                self.key, Action.NOOP, f"{len(self._owned(ctx))} managed binding(s)", details, blocked=blocked
            )
        return Change(
            self.key,
            Action.UPDATE if self._owned(ctx) else Action.CREATE,
            f"{len(to_add) + len(shared_add)} to grant, {len(to_remove)} to revoke",
            details,
            blocked=blocked,
        )

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        member = runtime_member(ctx)
        if member is None:
            raise RuntimeError("agent principal is unknown; the agentIdentity step did not run")
        desired = self.desired(ctx, member)
        shared = self.shared(ctx)
        to_add, to_remove, _, missing, shared_add = self._diff(ctx, desired, shared)
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
        if shared_add:
            self._modify(ctx, shared_add, set())  # grant-only: never recorded, so remove never revokes them

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
        kept = [f"= {k[2]} on {_target(k)} → {k[3]} (grant-only, kept)" for k in sorted(self.shared(ctx))]
        if not owned:
            return Change(self.key, Action.NOOP, "no managed bindings", kept)
        return Change(
            self.key,
            Action.DELETE,
            f"revoke {len(owned)} binding(s)",
            [f"- {k[2]} on {_target(k)} → {k[3]}" for k in sorted(owned)] + kept,
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
                # Never deployed (bare shell or adopted): send everything except what the shell was created with.
                # identity_type and encryption_spec are fixed at creation; the API rejects them in an update mask.
                fields = [f for f in desired if f not in engine_spec.CODE_FIELDS + engine_spec.SHELL_FIELDS]
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
            if code and not ctx.options.code_only:
                ctx.reapply.update(_CARD_CONSUMERS)  # the agent card can change with the code

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
            ctx.reapply.update({"agentRegistry", "geminiEnterprise"})

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


# Resources that publish the engine's agent card; both skip their apply when nothing changed.
_CARD_CONSUMERS = ("agentRegistry", "geminiEnterprise")


def is_a2a(ctx: Context) -> bool:
    """Whether the agent serves an A2A card (agents-cli-manifest.yaml: create_params.is_a2a)."""
    return bool(ctx.project.manifest and ctx.project.manifest.is_a2a)


def agent_card(ctx: Context) -> dict[str, Any] | None:
    """Card the deployed engine serves; None before the engine exists or while an operation is pending."""
    engine = ctx.state.resources.get("engine") or {}
    if not engine.get("name") or engine.get("pending") or not is_a2a(ctx):
        return None
    region = ctx.project.config.provider.region
    return ctx.clients.a2a_card(engine_spec.a2a_card_url(region, engine["name"], ctx.project.agent_directory))


_NOT_A2A = "only A2A agents serve an agent card (agents-cli-manifest.yaml: create_params.is_a2a: true)"


class AgentRegistryResource(Resource):
    """The engine's A2A card registered as a service in the Agent Registry (where Agent Gateways govern it)."""

    key = "agentRegistry"

    def _name(self, ctx: Context) -> str | None:
        cfg = ctx.project.config
        reg = cfg.publish.agent_registry
        if reg is None:
            return None
        location = reg.location or cfg.provider.region
        service_id = reg.service_id or f"{cfg.service}-{cfg.provider.stage}"
        return f"projects/{cfg.provider.project}/locations/{location}/services/{service_id}"

    def _body(self, ctx: Context, name: str, card: dict[str, Any]) -> dict[str, Any]:
        reg = ctx.project.config.publish.agent_registry
        assert reg is not None
        return {
            "displayName": reg.display_name or card.get("name") or name.rsplit("/", 1)[-1],
            "description": reg.description or card.get("description") or "",
            "agentSpec": {"type": "A2A_AGENT_CARD", "content": card},
        }

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        st, name = self._state(ctx), self._name(ctx)
        if name is None:
            if st:
                return Change(self.key, Action.DELETE, st["name"], data={"cleanup": st["name"]})
            return Change(self.key, Action.NOOP, "not registered")
        blocked = None if is_a2a(ctx) else _NOT_A2A
        if not st:
            return Change(self.key, Action.CREATE, name, blocked=blocked)
        if st["name"] != name:
            details = [f"~ service: {st['name']} → {name}"]
            return Change(self.key, Action.REPLACE, name, details, data={"cleanup": st["name"]}, blocked=blocked)
        details, data = [], {}
        if st.get("previous"):  # a run that registered the replacement crashed before deleting the old one
            details.append(f"- {st['previous']} (left over from a rename)")
            data["cleanup"] = st["previous"]
        card = agent_card(ctx)
        if card is not None and _hash(self._body(ctx, name, card)) != st.get("payloadHash"):
            details.insert(0, "~ agent card or metadata changed")
        action = Action.UPDATE if details else Action.NOOP
        return Change(self.key, action, name, details, data=data, blocked=blocked)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        name = self._name(ctx)
        if name is None:
            return  # unregistered in cleanup
        card = agent_card(ctx)
        if card is None:
            ctx.echo("    engine not ready, card not registered; rerun `agentless deploy` once it is")
            return
        st, body = self._state(ctx), self._body(ctx, name, card)
        # Recomputed from state, not plan data: a refresh after a code deploy runs with empty data.
        moving = st["name"] if st.get("name") and st["name"] != name else None
        if st.get("name") == name and st.get("payloadHash") == _hash(body):
            return
        parent, service_id = name.split("/services/")
        if ctx.clients.registry_get(name) is None:
            ctx.clients.registry_create(parent, service_id, body)
        else:
            ctx.clients.registry_patch(name, body)  # also adopts a service a failed run created
        previous = moving or st.get("previous")
        self._set_state(ctx, {"name": name, "payloadHash": _hash(body), **({"previous": previous} if previous else {})})
        ctx.echo(f"    {name}")

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        old = change.data.get("cleanup")
        if not old:
            return
        if self._name(ctx) is None:
            ctx.clients.registry_delete(old)
            self._set_state(ctx, None)
            return
        st = dict(self._state(ctx))
        if st.get("previous") != old:
            return  # the replacement isn't registered yet; keep the old service until it is
        ctx.clients.registry_delete(old)
        st.pop("previous")
        self._set_state(ctx, st)

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        return Change(self.key, Action.DELETE, st["name"]) if st else Change(self.key, Action.NOOP, "not registered")

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        for name in filter(None, [st.get("previous"), st.get("name")]):
            ctx.clients.registry_delete(name)
        self._set_state(ctx, None)


class GeminiEnterpriseResource(Resource):
    """Registration of the engine as an agent in a Gemini Enterprise app, plus its OAuth authorization."""

    key = "geminiEnterprise"

    def _payload(
        self, ctx: Context, engine_name: str, authorization: str | None, card: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ge = ctx.project.config.publish.gemini_enterprise
        assert ge is not None
        agent = ctx.project.config.agent
        payload: dict[str, Any] = {
            "displayName": ge.display_name or ctx.project.config.display_name,
            "description": ge.description or agent.description or ctx.project.config.display_name,
            "icon": {"uri": _GE_ICON},
        }
        if ge.registration == "a2a":
            payload["a2a_agent_definition"] = {"json_agent_card": json.dumps(card or {}, sort_keys=True)}
            if authorization:
                payload["authorization_config"] = {"agent_authorization": authorization}
        else:
            payload["adk_agent_definition"] = {
                "tool_settings": {"tool_description": ge.tool_description or agent.description or ""},
                "provisioned_reasoning_engine": {"reasoning_engine": engine_name},
            }
            if authorization:
                payload["authorization_config"] = {"tool_authorizations": [authorization]}
        if ge.sharing:
            payload["sharing_config"] = {"scope": "ALL_USERS"}
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

    def _gateway(self, ctx: Context, details: list[str]) -> bool:
        """Whether the app's egress Agent Gateway must be set; never changes one that is already there."""
        ge = ctx.project.config.publish.gemini_enterprise
        if ge is None or ge.agent_gateway is None:
            return False
        current = ctx.clients.ge_app_gateway(ge.app)
        if current == ge.agent_gateway:
            return False
        if current:
            details.append(
                f"! the app already routes agent traffic through {current}; agentless leaves it "
                "(change it where the app is managed)"
            )
            return False
        details.append(f"+ app egress gateway: {ge.agent_gateway} (shared by every agent of the app, kept on remove)")
        return True

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        ge, st = ctx.project.config.publish.gemini_enterprise, self._state(ctx)
        if ge is None:
            if st:
                return Change(self.key, Action.DELETE, st.get("agentName", st.get("app", "")), data={"cleanup": st})
            return Change(self.key, Action.NOOP, "not published")
        details: list[str] = []
        problems = [f"registration: a2a: {_NOT_A2A}"] if ge.registration == "a2a" and not is_a2a(ctx) else []
        gateway = self._gateway(ctx, details)
        blocked = "; ".join(problems) or None
        if not st:
            return Change(self.key, Action.CREATE, ge.app, details, blocked=blocked)
        registration = st.get("registration", "adk")
        if st.get("app") != ge.app or registration != ge.registration:
            details = [
                f"~ {field}: {old} → {new}"
                for field, old, new in (("app", st.get("app"), ge.app), ("registration", registration, ge.registration))
                if old != new
            ] + details
            return Change(self.key, Action.REPLACE, ge.app, details, data={"cleanup": st}, blocked=blocked)
        data = {}
        if st.get("previous"):  # a run that registered the replacement crashed before removing the old one
            details.append(f"- {st['previous'].get('agentName', st['previous'].get('app'))} (left over from a move)")
            data["cleanup"] = st["previous"]
        engine = (ctx.state.resources.get("engine") or {}).get("name") or ""
        card = agent_card(ctx) if ge.registration == "a2a" else None
        changed = gateway or bool(data) or st.get("authorizationHash") != self._auth_hash(ctx)
        if ge.registration == "adk" or card is not None:
            changed |= st.get("payloadHash") != _hash(self._payload(ctx, engine, self._auth_name(ctx), card))
        action = Action.UPDATE if changed else Action.NOOP
        return Change(self.key, action, st.get("agentName", ge.app), details, data=data, blocked=blocked)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        ge = ctx.project.config.publish.gemini_enterprise
        if ge is None:
            return
        engine = (ctx.state.resources.get("engine") or {}).get("name")
        if not engine:
            raise RuntimeError("cannot publish to Gemini Enterprise: engine has no name yet")
        if ge.agent_gateway and ctx.clients.ge_app_gateway(ge.app) is None:
            ctx.clients.ge_set_app_gateway(ge.app, ge.agent_gateway)
            ctx.echo(f"    app egress gateway: {ge.agent_gateway}")
        card = None
        if ge.registration == "a2a":
            card = agent_card(ctx)
            if card is None:
                ctx.echo("    engine not ready, agent card unavailable; rerun `agentless deploy` once it is")
                return
        current = dict(self._state(ctx))
        # Recomputed from state, not plan data: a refresh after a code deploy or engine rename runs with empty data.
        moving = bool(current) and (
            current.get("app") != ge.app or current.get("registration", "adk") != ge.registration
        )
        if moving and current.get("previous"):
            _unpublish(ctx, current["previous"], keep=self._auth_name(ctx))  # an older move that never cleaned up
        st = {} if moving else current
        previous = {k: v for k, v in current.items() if k != "previous"} if moving else current.get("previous")
        authorization = self._auth_name(ctx)
        payload = self._payload(ctx, engine, authorization, card)
        unchanged = st.get("payloadHash") == _hash(payload) and st.get("authorizationHash") == self._auth_hash(ctx)
        if st.get("agentName") and unchanged:
            return  # a refresh after a code deploy that didn't change the card
        if ge.authorization and (st.get("authorizationHash") != self._auth_hash(ctx) or moving):
            ctx.clients.ge_upsert_authorization(ge.app, ge.authorization.id, ge.authorization.model_dump())
        agent_name = st.get("agentName") or ctx.clients.ge_find_agent(
            ge.app, engine=engine if ge.registration == "adk" else None, card_url=(card or {}).get("url")
        )
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
                "registration": ge.registration,
                "agentName": agent_name,
                "payloadHash": _hash(payload),
                "authorizationName": authorization,
                "authorizationHash": self._auth_hash(ctx),
                **({"agentGateway": ge.agent_gateway} if ge.agent_gateway else {}),
                **({"previous": previous} if previous else {}),
            },
        )
        ctx.echo(f"    {agent_name}")

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        old = change.data.get("cleanup")
        if not old:
            return
        if ctx.project.config.publish.gemini_enterprise and "previous" not in self._state(ctx):
            return  # the replacement wasn't registered (engine not ready); keep the old one until it is
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
    BucketsResource,
    IamResource,
    EngineResource,
    AgentRegistryResource,
    GeminiEnterpriseResource,
)
