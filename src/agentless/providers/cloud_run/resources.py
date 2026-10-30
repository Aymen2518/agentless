"""Managed resources of a Cloud Run deployment, applied in order and destroyed in reverse.

The runtime service account, buckets, IAM and publishing are the Agent Runtime resources, unchanged.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from agentless.package.packager import write_archive
from agentless.plan.model import Action, Change, Context, Resource
from agentless.providers.agent_runtime import spec as engine_spec
from agentless.providers.agent_runtime.resources import (
    _CARD_CONSUMERS,
    AccessResource,
    AgentRegistryResource,
    BucketsResource,
    GeminiEnterpriseResource,
    IamResource,
    ServiceAccountResource,
    is_a2a,
    service_account_email,
)
from agentless.providers.cloud_run import spec as run_spec

_PUBLIC = ("allUsers", "allAuthenticatedUsers")
_KNOWN_AFTER_APPLY = "<known after apply>"
# Service fields the API sets; everything else is sent back unchanged by a --code-only update.
_OUTPUT_ONLY = frozenset(
    {
        "name",
        "uid",
        "generation",
        "createTime",
        "updateTime",
        "deleteTime",
        "expireTime",
        "creator",
        "lastModifier",
        "observedGeneration",
        "terminalCondition",
        "conditions",
        "latestReadyRevision",
        "latestCreatedRevision",
        "trafficStatuses",
        "uri",
        "urls",
        "reconciling",
        "satisfiesPzs",
    }
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SessionEngineResource(Resource):
    """Bare reasoning engine (no code) that holds the Cloud Run agent's sessions and Memory Bank."""

    key = "sessionEngine"

    @staticmethod
    def _desired(ctx: Context) -> dict[str, Any]:
        cfg = ctx.project.config
        return {
            "display_name": f"{cfg.display_name}-sessions",
            "description": f"Sessions and memories of the {cfg.run_service_name} Cloud Run agent",
            "labels": dict(
                sorted(
                    {
                        **cfg.provider.labels,
                        "agentless-service": cfg.service,
                        "agentless-stage": cfg.provider.stage,
                    }.items()
                )
            ),
            "context_spec": engine_spec.context_spec(ctx.project),
        }

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not ctx.project.config.session_engine:
            if st:
                return Change(self.key, Action.DELETE, st["name"], ["- sessions and memories are deleted with it"])
            return Change(self.key, Action.NOOP, "in-memory sessions")
        desired = self._desired(ctx)
        if st and ctx.clients.engine_get(st["name"]) is None:
            return Change(self.key, Action.CREATE, desired["display_name"], ["! deleted outside agentless; recreating"])
        if not st:
            found = ctx.clients.engine_find(desired["display_name"])
            if found:  # created by a run that crashed before saving state
                return Change(self.key, Action.UPDATE, found[0], ["~ reuse existing engine"], data={"adopt": found[0]})
            return Change(self.key, Action.CREATE, desired["display_name"], ["+ sessions engine (no code)"])
        changed = engine_spec.diff(st.get("spec"), desired)
        if not changed:
            return Change(self.key, Action.NOOP, st["name"])
        details = [line for f, (old, new) in changed.items() for line in engine_spec.describe(f, old, new)]
        return Change(self.key, Action.UPDATE, st["name"], details, data={"fields": sorted(changed)})

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if not ctx.project.config.session_engine:
            return
        desired, st = self._desired(ctx), self._state(ctx)
        if change.data.get("adopt"):
            st = {"name": change.data["adopt"], "spec": None}
        elif st and ctx.clients.engine_get(st["name"]) is None:
            st = {}
        config, masks = engine_spec.api_payload(
            desired, None if not st else sorted(engine_spec.diff(st.get("spec"), desired))
        )
        if not st:
            operation = ctx.clients.engine_create(config)
            st = {"name": operation.rpartition("/operations/")[0], "spec": None}
            self._set_state(ctx, st)  # named before waiting, so a crash leaves it tracked
            # GOOGLE_CLOUD_AGENT_ENGINE_ID points at the new engine, and the runtime SA's grant on it follows.
            ctx.reapply.update({"iam", "service"})
        else:
            config["update_mask"] = ",".join(masks)
            operation = ctx.clients.engine_update(st["name"], config)
        ctx.clients.wait(operation, poll=5)
        self._set_state(ctx, {"name": st["name"], "spec": desired})
        ctx.echo(f"    {st['name']}")

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "none")
        return Change(self.key, Action.DELETE, st["name"], ["- sessions and memories are deleted with it"])

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        if st:
            ctx.clients.engine_delete(st["name"])
        self._set_state(ctx, None)

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if change.action == Action.DELETE and not ctx.project.config.session_engine:
            self.destroy(ctx)  # after the service stopped pointing at it


class BuildResource(Resource):
    """Container image built from the agent source with Cloud Build and pushed to Artifact Registry."""

    key = "build"

    @staticmethod
    def _wanted(ctx: Context) -> bool:
        run = ctx.project.config.cloud_run
        return run is not None and not run.image

    @staticmethod
    def _fingerprint(ctx: Context) -> dict[str, Any]:
        assert ctx.package is not None
        return {
            "sourceHash": ctx.package.sha256,
            "buildArgs": dict(sorted(ctx.project.config.agent.build.args.items())),
        }

    @classmethod
    def tag(cls, ctx: Context) -> str:
        """Image tag of the current source and build args."""
        return _hash(cls._fingerprint(ctx))[:12]

    @staticmethod
    def _prefix(ctx: Context) -> str:
        cfg = ctx.project.config
        return f"agentless/{cfg.service}/{cfg.provider.stage}/source/"

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not self._wanted(ctx):
            if st:
                return Change(self.key, Action.DELETE, "uploaded sources", ["prebuilt cloudRun.image in use"])
            return Change(self.key, Action.NOOP, "prebuilt image")
        fingerprint = self._fingerprint(ctx)
        image = run_spec.image_uri(ctx.project, self.tag(ctx))
        run = ctx.project.config.cloud_run
        assert run is not None
        repo = run.artifact_registry
        if st and not ctx.options.force and all(st.get(k) == v for k, v in fingerprint.items()):
            return Change(self.key, Action.NOOP, st["image"])
        details = ["+ source changed" if st else "+ first build"]
        if st and st.get("buildArgs") != fingerprint["buildArgs"]:
            details = ["~ build args changed"]
        blocked = None
        if not ctx.clients.ar_repository_exists(repo):
            blocked = (
                f"Artifact Registry repository {repo} not found in {ctx.project.config.provider.region}; "
                "create it (it belongs to your environment's IaC) or set cloudRun.artifactRegistry"
            )
        return Change(self.key, Action.UPDATE if st else Action.CREATE, image, details, blocked=blocked)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if not self._wanted(ctx):
            return
        assert ctx.package is not None
        cfg = ctx.project.config
        fingerprint = self._fingerprint(ctx)
        image = run_spec.image_uri(ctx.project, self.tag(ctx))
        digest = None if ctx.options.force else ctx.clients.ar_image_digest(image)
        if digest:
            ctx.echo(f"    {image} already built")
        else:
            bucket = cfg.provider.staging_bucket
            assert bucket is not None  # schema: a source build needs provider.stagingBucket
            obj = f"{self._prefix(ctx)}{self.tag(ctx)}.tar.gz"
            with tempfile.TemporaryDirectory() as tmp:
                archive = write_archive(ctx.package, Path(tmp) / "source.tar.gz")
                ctx.clients.upload_source(bucket, obj, archive)
            ctx.echo(f"    building {image} (Cloud Build)")
            digest = ctx.clients.cloud_build(
                f"gs://{bucket}/{obj}",
                image,
                fingerprint["buildArgs"],
                cfg.cloud_run.build_service_account if cfg.cloud_run else None,
                ["agentless", cfg.run_service_name],
            )
        self._set_state(ctx, {**fingerprint, "image": image, "digest": digest})

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "nothing built")
        return Change(
            self.key, Action.DELETE, "uploaded sources", ["= images stay in Artifact Registry (clean them up there)"]
        )

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        bucket = ctx.project.config.provider.staging_bucket
        if bucket:
            ctx.clients.delete_objects(bucket, self._prefix(ctx))
        self._set_state(ctx, None)

    def cleanup(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if change.action == Action.DELETE and not self._wanted(ctx):
            self.destroy(ctx)


class ServiceResource(Resource):
    """The Cloud Run service: config diffed against the last-applied spec, image from the build or cloudRun.image."""

    key = "service"

    @staticmethod
    def _url(ctx: Context) -> str:
        return run_spec.service_url(ctx.project, ctx.clients.project_number())

    def _desired(self, ctx: Context, *, planning: bool) -> dict[str, Any]:
        wanted = ctx.project.config.session_engine  # a released engine is deleted only after the service drops it
        engine = (ctx.state.resources.get("sessionEngine") or {}).get("name") if wanted else None
        if planning and wanted and not engine:
            engine = _KNOWN_AFTER_APPLY
        spec = run_spec.desired_spec(
            ctx.project, service_account=service_account_email(ctx), url=self._url(ctx), session_engine=engine
        )
        return engine_spec.redact(spec, ctx.project.sensitive) if planning else spec

    @staticmethod
    def _image(ctx: Context) -> str | None:
        """Image to run, pinned by digest after a source build; None before the first build."""
        run = ctx.project.config.cloud_run
        if run and run.image:
            return run.image
        build = ctx.state.resources.get("build") or {}
        if not build.get("digest"):
            return None
        return f"{build['image'].rpartition(':')[0]}@{build['digest']}"

    @staticmethod
    def _notes(ctx: Context) -> list[str]:
        cfg = ctx.project.config
        notes: list[str] = []
        if cfg.publish.gemini_enterprise and cfg.cloud_run and cfg.cloud_run.ingress != "all":
            notes.append(
                f"! ingress {cfg.cloud_run.ingress}: Gemini Enterprise calls the agent from outside your VPC and "
                "may not reach it; use ingress all (IAM still applies) or route it through a load balancer"
            )
        if header := run_spec.app_token_header(ctx.project):
            assert cfg.cloud_run and cfg.cloud_run.google_token
            principals = ", ".join(cfg.cloud_run.google_token.allowed_principals)
            notes.append(
                f"= auth googleToken: Cloud Run checks roles/run.invoker (X-Serverless-Authorization), then the "
                f"container must verify the Google token in {header} (allowed: {principals})"
            )
            if not cfg.access.invokers:
                notes.append("! no access.invokers: only the deployer passes the IAM check")
            deployer = (ctx.project.deployer.impersonate or "").lower()
            allowed = cfg.cloud_run.google_token.allowed_principals
            if deployer and deployer not in allowed and f"domain:{deployer.rpartition('@')[2]}" not in allowed:
                notes.append(
                    f"! deployer {deployer} is not in googleToken.allowedPrincipals: `invoke` and agent card reads "
                    "will be refused unless the container exempts them"
                )
            if cfg.publish.gemini_enterprise:
                notes.append(
                    f"! Gemini Enterprise calls the agent with an ID token only, without {header}: the container "
                    "must accept it (its service agent holds roles/run.invoker) or GE calls will be refused"
                )
        if header := run_spec.webhook_header(ctx.project):
            notes.append(
                f"! auth webhook: Cloud Run's invoker IAM check is off; the container must reject every request "
                f"without the secret in {header} (checked after the rollout, IAM turned back on if not)"
            )
        return notes

    @staticmethod
    def _iam_disabled(ctx: Context) -> bool:
        return run_spec.webhook_header(ctx.project) is not None

    @staticmethod
    def _tags(ctx: Context) -> list[str]:
        """Namespaced tag values (`<parent>/<key>/<value>`) cloudRun.tags binds on the service."""
        run = ctx.project.config.cloud_run
        return sorted(f"{key}/{value}" for key, value in (run.tags if run else {}).items())

    def _tag_diff(self, ctx: Context, name: str, st: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """Tags to bind, and the bindings agentless made that are no longer wanted (value -> binding name).

        A wanted tag that was already bound outside agentless is left alone and never recorded as owned.
        """
        live = ctx.clients.run_service_tags(name)
        wanted = self._tags(ctx)
        return [t for t in wanted if t not in live], {
            t: live[t] for t in st.get("tags", []) if t not in wanted and t in live
        }

    def _code_changed(self, ctx: Context, st: dict[str, Any]) -> bool:
        run = ctx.project.config.cloud_run
        if run and run.image:
            return st.get("image") != run.image
        return st.get("build") != BuildResource.tag(ctx) or bool(ctx.options.force)

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        name = run_spec.service_name(ctx.project)
        st = self._state(ctx)
        live = ctx.clients.run_service_get(name)
        desired = self._desired(ctx, planning=True)
        if live is None:
            details = ["! deleted outside agentless; recreating"] if st else []
            gate = [] if self._iam_disabled(ctx) else ["= callers need roles/run.invoker (never public)"]
            tags = [f"+ tag {t} (bound before the service opens)" for t in self._tags(ctx)]
            details += [f"+ {self._url(ctx)}", *gate, *tags, *self._notes(ctx)]
            return Change(self.key, Action.CREATE, name, details)
        details: list[str] = []
        data: dict[str, Any] = {}
        if not st:
            labels = live.get("labels") or {}
            cfg = ctx.project.config
            if labels.get("agentless-service") != cfg.service or labels.get("agentless-stage") != cfg.provider.stage:
                return Change(
                    self.key,
                    Action.UPDATE,
                    name,
                    blocked=f"{name} exists and is not managed by agentless for {cfg.service}/{cfg.provider.stage}; "
                    "delete it or set cloudRun.serviceName",
                )
            details.append("~ adopt existing service")
            data["adopt"] = True
        if bool(live.get("invokerIamDisabled")) != self._iam_disabled(ctx):
            if self._iam_disabled(ctx):
                details.append("~ turn the invoker IAM check off (auth webhook)")
            else:
                details.append("! invoker IAM check was disabled outside agentless; turning it back on")
        changed = engine_spec.diff(st.get("spec"), desired) if st else {}
        for field, (old, new) in changed.items():
            details += engine_spec.describe(field, old, new)
        if st and self._code_changed(ctx, st):
            details.append("~ new image")
        add, remove = self._tag_diff(ctx, name, st)
        details += [f"+ tag {t}" for t in add] + [f"- tag {t}" for t in remove]
        if not details:
            return Change(self.key, Action.NOOP, name)  # notes only come with a change, so reruns stay no-ops
        return Change(self.key, Action.UPDATE, name, details + self._notes(ctx), data=data)

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        name = run_spec.service_name(ctx.project)
        image = self._image(ctx)
        if image is None:
            raise RuntimeError("no image to deploy: the build did not record one")
        live = ctx.clients.run_service_get(name)
        st = self._state(ctx)
        url = self._url(ctx)
        wanted_tags = self._tags(ctx)
        add, remove = self._tag_diff(ctx, name, st) if live is not None else (wanted_tags, {})
        tags = sorted((set(st.get("tags", [])) & set(wanted_tags)) | set(add))
        if ctx.options.code_only and live is not None and st.get("spec") is not None:
            # Only the image changes; every other setting (IAP, launch stage, scaling…) stays as it is live.
            body = {k: v for k, v in live.items() if k not in _OUTPUT_ONLY}
            body["template"]["containers"][0]["image"] = image
            body["invokerIamDisabled"] = self._iam_disabled(ctx)
            self._bind(ctx, name, add)
            ctx.clients.run_service_update(name, body)
            spec = st["spec"]
        else:
            desired = self._desired(ctx, planning=False)
            spec = engine_spec.redact(desired, ctx.project.sensitive)
            current = live is not None and bool(live.get("invokerIamDisabled")) == self._iam_disabled(ctx)
            if current and st.get("spec") == spec and st.get("image") == image and not add and not remove:
                return  # a refresh with nothing new: no extra revision
            body = run_spec.api_service(desired, image)
            if live is None:
                self._create(ctx, name, body, wanted_tags)
                ctx.reapply.add("access")  # a recreated service starts with an empty policy
            else:
                self._bind(ctx, name, add)  # before the update: a tag may be what allows the new settings
                ctx.clients.run_service_update(name, body)
        for binding in remove.values():  # after the update: the old settings may have needed it
            ctx.clients.run_service_untag(binding)
        self._check_webhook_gate(ctx, name, url, body)
        if st.get("image") != image:
            ctx.reapply.update(_CARD_CONSUMERS)  # the agent card can change with the code
        card_url = run_spec.a2a_card_url(url, ctx.project.agent_directory) if is_a2a(ctx) else None
        run = ctx.project.config.cloud_run
        build = None if run and run.image else BuildResource.tag(ctx)
        self._set_state(
            ctx,
            {
                "name": name,
                "url": url,
                **({"cardUrl": card_url} if card_url else {}),
                "image": image,
                **({"build": build} if build else {}),
                **({"tags": tags} if tags else {}),
                "spec": spec,
            },
        )
        ctx.echo(f"    {url}")

    @staticmethod
    def _bind(ctx: Context, name: str, tags: list[str]) -> None:
        for tag in tags:
            ctx.clients.run_service_tag(name, tag)
            ctx.echo(f"    tagged {tag}")

    def _create(self, ctx: Context, name: str, body: dict[str, Any], tags: list[str]) -> None:
        """Create the service; with tags, create it closed, tag it, then apply its settings.

        Org policies such as run.allowedIngress check the tags of the service itself, and a service can only be
        tagged once it exists. It starts with ingress internal and the invoker IAM check on, so it is never reachable
        before its tags are.
        """
        if not tags:
            ctx.clients.run_service_create(name, body)
            return
        closed = {**body, "ingress": run_spec.INGRESS["internal"], "invokerIamDisabled": False}
        ctx.clients.run_service_create(name, closed)
        self._bind(ctx, name, tags)
        if closed != body:
            ctx.clients.run_service_update(name, body)

    def _check_webhook_gate(self, ctx: Context, name: str, url: str, body: dict[str, Any]) -> None:
        """With auth: webhook, prove anonymous calls are refused; otherwise turn IAM back on and fail (fail closed)."""
        header = run_spec.webhook_header(ctx.project)
        if not header:
            return
        try:
            statuses = ctx.clients.webhook_probe(url, header)
        except BaseException:  # a failed or interrupted probe proves nothing: close the service, then re-raise
            ctx.clients.run_service_update(name, {**body, "invokerIamDisabled": False})
            raise
        if all(status in (401, 403) for status in statuses):
            ctx.echo(f"    anonymous calls refused ({', '.join(map(str, statuses))}); {header} gates the service")
            return
        ctx.clients.run_service_update(name, {**body, "invokerIamDisabled": False})
        raise RuntimeError(
            f"auth webhook: {url} answered {statuses[0]} without {header} and {statuses[1]} with a wrong one; "
            f"the container must answer 401 or 403 to every request without the secret (AGENTLESS_WEBHOOK_SECRET "
            "in AGENTLESS_WEBHOOK_HEADER), on every path including /. The invoker IAM check is back on."
        )

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "not deployed")
        return Change(self.key, Action.DELETE, st["name"])

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        if st:
            ctx.clients.run_service_delete(st["name"])
        self._set_state(ctx, None)


class InvokerResource(AccessResource):
    """Who may call the service: roles/run.invoker bindings on it. Public members are never granted or tolerated.

    With auth: webhook the IAM check is off and the container gates calls; an allUsers grant still blocks.
    """

    rtype = "runService"
    noun = "service"
    always_blocks = True  # a public service blocks even `--code-only`

    @staticmethod
    def _engine(ctx: Context) -> str | None:
        return (ctx.state.resources.get("service") or {}).get("name")

    @staticmethod
    def role(ctx: Context) -> str:  # noqa: D102
        return run_spec.INVOKER_ROLE

    @staticmethod
    def _public(ctx: Context, service: str | None) -> list[str]:
        members = ctx.clients.iam_members("runService", service) if service else None
        return sorted(f"{role} → {m}" for role, ms in (members or {}).items() for m in ms if m in _PUBLIC)

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        change = super().plan(ctx)
        if public := self._public(ctx, self._engine(ctx)):
            change.blocked = (
                f"the service is public ({', '.join(public)}), granted outside agentless; agents must only be "
                "callable through IAM: remove that binding, then deploy again"
            )
        return change

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        if public := self._public(ctx, self._engine(ctx)):
            raise RuntimeError(f"the service is public ({', '.join(public)}); remove that binding first")
        super().apply(ctx, change)


ALL_RESOURCES: tuple[type[Resource], ...] = (
    ServiceAccountResource,
    BucketsResource,
    IamResource,
    SessionEngineResource,
    BuildResource,
    ServiceResource,
    InvokerResource,
    AgentRegistryResource,
    GeminiEnterpriseResource,
)
