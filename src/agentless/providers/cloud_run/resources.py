"""Managed resources of a Cloud Run deployment, applied in order and destroyed in reverse.

The runtime service account, buckets, IAM and publishing are the Agent Runtime resources, unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from agentless.package.packager import write_archive
from agentless.plan.model import Action, Change, Context, Resource, owned_name, project_ref
from agentless.providers.agent_runtime import spec as engine_spec
from agentless.providers.agent_runtime.resources import (
    _CARD_CONSUMERS,
    AccessResource,
    AgentRegistryResource,
    BucketsResource,
    GeminiEnterpriseResource,
    IamResource,
    ServiceAccountResource,
    engine_pattern,
    is_a2a,
    service_account_email,
)
from agentless.providers.cloud_run import routing
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
            ctx.clients.engine_delete(owned_name(st["name"], engine_pattern(ctx), "the sessions engine"))
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
        same_repository = (st.get("image") or "").rpartition(":")[0] == image.rpartition(":")[0]
        if st and same_repository and not ctx.options.force and all(st.get(k) == v for k, v in fingerprint.items()):
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
        # Only ever an image agentless pushed to this deployment's own repository, never one state points elsewhere.
        repository = re.escape(run_spec.image_uri(ctx.project, "tag").rpartition(":")[0])
        image = owned_name(build.get("image", ""), rf"{repository}:[0-9a-f]+", "the built image")
        digest = owned_name(build["digest"], r"sha256:[0-9a-f]{64}", "the image digest")
        return f"{image.rpartition(':')[0]}@{digest}"

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
                f"! auth webhook: allUsers holds roles/run.invoker and the IAM check stays on; the container must "
                f"reject every request without the secret in {header} (checked after each rollout, allUsers revoked "
                "if not). Under Domain Restricted Sharing, the org's exemption tag must be in cloudRun.tags"
            )
        return notes

    @staticmethod
    def _tags(ctx: Context) -> list[str]:
        """Namespaced tag values (`<parent>/<key>/<value>`) cloudRun.tags binds on the service."""
        run = ctx.project.config.cloud_run
        return sorted(f"{key}/{value}" for key, value in (run.tags if run else {}).items())

    def _tag_diff(self, ctx: Context, name: str, st: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """Tags to bind, and the bindings agentless made that are no longer wanted (value -> binding name).

        A wanted tag that was already bound outside agentless is left alone and never recorded as owned.
        """
        wanted = self._tags(ctx)
        if not wanted and not st.get("tags"):
            return [], {}  # nothing to bind or unbind: no call (and no tag permission) needed
        live = ctx.clients.run_service_tags(name)
        return [t for t in wanted if t not in live], {
            t: live[t] for t in st.get("tags", []) if t not in wanted and t in live
        }

    def _code_changed(self, ctx: Context, st: dict[str, Any]) -> bool:
        run = ctx.project.config.cloud_run
        if run and run.image:
            return st.get("image") != run.image
        repository = run_spec.image_uri(ctx.project, "tag").rpartition(":")[0]
        moved = (st.get("image") or "").partition("@")[0] != repository  # cloudRun.artifactRegistry changed
        return st.get("build") != BuildResource.tag(ctx) or moved or bool(ctx.options.force)

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        name = run_spec.service_name(ctx.project)
        st = self._state(ctx)
        live = ctx.clients.run_service_get(name)
        desired = self._desired(ctx, planning=True)
        if live is None:
            details = ["! deleted outside agentless; recreating"] if st else []
            public = run_spec.webhook_header(ctx.project) is not None
            gate = [f"= callers need roles/run.invoker ({'allUsers holds it' if public else 'never public'})"]
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
        if live.get("invokerIamDisabled"):
            if run_spec.webhook_header(ctx.project):
                details.append("~ turn the invoker IAM check back on (auth webhook now grants allUsers)")
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
            body["invokerIamDisabled"] = False
            self._bind(ctx, name, add)
            ctx.clients.run_service_update(name, body)
            spec = st["spec"]
        else:
            desired = self._desired(ctx, planning=False)
            spec = engine_spec.redact(desired, ctx.project.sensitive)
            current = live is not None and not live.get("invokerIamDisabled")
            unchanged = current and st.get("spec") == spec and st.get("image") == image
            if unchanged and not add and not remove:
                return  # a refresh with nothing new: no extra revision
            body = run_spec.api_service(desired, image)
            if live is None:
                self._create(ctx, name, body, wanted_tags)
                ctx.reapply.add("access")  # a recreated service starts with an empty policy
            else:
                self._bind(ctx, name, add)  # before the update: a tag may be what allows the new settings
                if not run_spec.webhook_header(ctx.project) and _all_users_invoker(ctx, name):
                    # Leaving auth: webhook: the new revision has no secret gate, so close the service first
                    # (a foreign allUsers grant blocks the plan; this one is agentless's, `access` forgets it).
                    ctx.clients.iam_modify("runService", name, [], [(run_spec.INVOKER_ROLE, run_spec.ALL_USERS)])
                if not unchanged:  # a tag change alone makes no new revision
                    ctx.clients.run_service_update(name, body)
        for binding in remove.values():  # after the update: the old settings may have needed it
            ctx.clients.run_service_untag(binding)
        self._check_webhook_gate(ctx, name, url)
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

    @staticmethod
    def _check_webhook_gate(ctx: Context, name: str, url: str) -> None:
        """Probe a new revision of a public webhook service; before allUsers is granted, InvokerResource probes."""
        if run_spec.webhook_header(ctx.project) and _all_users_invoker(ctx, name):
            _probe_webhook(ctx, name, url)

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "not deployed")
        return Change(self.key, Action.DELETE, st["name"])

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = self._state(ctx)
        if st:
            pattern = rf"projects/{project_ref(ctx)}/locations/[a-z0-9-]+/services/[a-z][a-z0-9-]*"
            ctx.clients.run_service_delete(owned_name(st["name"], pattern, "the Cloud Run service"))
        self._set_state(ctx, None)


def _all_users_invoker(ctx: Context, name: str) -> bool:
    return run_spec.ALL_USERS in (ctx.clients.iam_members("runService", name) or {}).get(run_spec.INVOKER_ROLE, set())


def _probe_webhook(ctx: Context, name: str, url: str) -> None:
    """With auth: webhook, prove anonymous calls are refused; otherwise revoke allUsers and fail (fail closed)."""
    header = run_spec.webhook_header(ctx.project)
    assert header
    revoke = [(run_spec.INVOKER_ROLE, run_spec.ALL_USERS)]
    try:
        statuses = ctx.clients.webhook_probe(url, header)
    except BaseException:  # a failed or interrupted probe proves nothing: close the service, then re-raise
        ctx.clients.iam_modify("runService", name, [], revoke)
        raise
    if all(status in (401, 403) for status in statuses):
        ctx.echo(f"    anonymous calls refused ({', '.join(map(str, statuses))}); {header} gates the service")
        return
    ctx.clients.iam_modify("runService", name, [], revoke)
    raise RuntimeError(
        f"auth webhook: {url} answered {statuses[0]} without {header} and {statuses[1]} with a wrong one; "
        f"the container must answer 401 or 403 to every request without the secret (AGENTLESS_WEBHOOK_SECRET "
        "in AGENTLESS_WEBHOOK_HEADER), on every path including /. allUsers was revoked: the next deploy grants it "
        "again and probes."
    )


class LoadBalancerResource(Resource):
    """Serverless NEG and backend service of the agent, routed from an existing (shared) load balancer's URL map.

    The NEG and the backend service belong to the agent. In the URL map, agentless only adds and removes its own
    entries (see `routing`), with fingerprint retries, so other agents and the load balancer's IaC are never touched.
    """

    key = "loadBalancer"

    @staticmethod
    def _target(ctx: Context) -> dict[str, Any] | None:
        """Desired NEG, backend service and route; `scheme` is None until read from the URL map."""
        cfg = ctx.project.config
        lb = cfg.cloud_run.load_balancer if cfg.cloud_run else None
        if lb is None:
            return None
        project, region, svc = cfg.provider.project, cfg.provider.region, cfg.run_service_name
        scope = f"regions/{region}" if lb.regional else "global"
        return {
            "neg": f"projects/{project}/regions/{region}/networkEndpointGroups/{svc}-neg",
            "service": svc,
            "backendService": f"projects/{project}/{scope}/backendServices/{svc}-lb",
            "scheme": lb.load_balancing_scheme,
            "urlMap": f"projects/{project}/{scope}/urlMaps/{lb.url_map}",
            "hosts": list(lb.hosts),
            "pathPrefix": lb.path_prefix,
            "matcher": f"{routing.MATCHER_PREFIX}{svc}",
        }

    @staticmethod
    def _marker(ctx: Context) -> str:
        cfg = ctx.project.config
        return f"agentless {cfg.service}/{cfg.provider.stage}"

    def _route(self, ctx: Context, target: dict[str, Any]) -> routing.Route:
        return routing.Route(
            hosts=tuple(target["hosts"]),
            path_prefix=target["pathPrefix"],
            matcher=target["matcher"],
            backend=target["backendService"],
            description=self._marker(ctx),
        )

    def _resolve(self, ctx: Context, target: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """The live URL map and the target with its scheme filled in, or why it can't be."""
        name = target["urlMap"].rsplit("/", 1)[-1]
        url_map = ctx.clients.compute_get(target["urlMap"])
        if url_map is None:
            return None, (
                f"URL map {target['urlMap']} not found: create the load balancer first (it belongs to your "
                "environment's IaC) or fix cloudRun.loadBalancer.urlMap / regional"
            )
        default = url_map.get("defaultService") or ""
        map_scheme = None
        if "/backendServices/" in default:
            map_scheme = (ctx.clients.compute_get(routing.path(default)) or {}).get("loadBalancingScheme")
        if target["scheme"] and map_scheme and target["scheme"] != map_scheme:
            return None, (
                f"loadBalancingScheme {target['scheme']} doesn't match URL map {name}'s ({map_scheme}): a URL map "
                "can't mix schemes; drop cloudRun.loadBalancer.loadBalancingScheme"
            )
        target["scheme"] = target["scheme"] or map_scheme
        if target["scheme"] is None:
            return None, (
                f"can't tell the load balancing scheme of URL map {name} (its default is not a backend service): "
                "set cloudRun.loadBalancer.loadBalancingScheme"
            )
        return url_map, None

    @staticmethod
    def _owned_route(matcher: str, backend: str) -> routing.Route:
        """The entries of a matcher and backend service, to strip them (hosts and paths don't matter)."""
        return routing.Route(hosts=(), path_prefix=None, matcher=matcher, backend=backend, description="")

    def _survey(
        self, ctx: Context, st: dict[str, Any], target: dict[str, Any], url_map: dict[str, Any]
    ) -> dict[str, Any]:
        """What a run has to do, read from live resources and state; plan and apply both act on it.

        - foreign: a NEG or backend service with the agent's name that agentless didn't create (blocks);
        - deleteBackends / deleteNegs: recorded or live ones that no longer match (renamed, other scheme, other
          service): deleted, then created again;
        - detach: (URL map, owned entries) to strip before that, e.g. under the old names or from the old map;
        - problems: conflicts of the new route with the map, once the agent's old entries are left out.
        """
        marker = self._marker(ctx)
        live = {key: ctx.clients.compute_get(target[key]) for key in ("neg", "backendService")}
        foreign = [
            f"{target[key]} exists and is not managed by agentless for {marker.split()[1]}"
            for key, resource in live.items()
            if resource is not None and resource.get("description") != marker
        ]
        neg, backend = live["neg"], live["backendService"]
        bad_neg = neg is not None and (neg.get("cloudRun") or {}).get("service") != target["service"]
        bad_backend = backend is not None and (
            bad_neg
            or backend.get("loadBalancingScheme") != target["scheme"]
            or [routing.path(b.get("group", "")) for b in backend.get("backends", [])] != [target["neg"]]
        )
        delete_backends = [p for p in [st.get("backendService")] if p and p != target["backendService"]]
        delete_negs = [p for p in [st.get("neg")] if p and p != target["neg"]]
        if bad_backend and not foreign:
            delete_backends.append(target["backendService"])
        if bad_neg and not foreign:
            delete_negs.append(target["neg"])
        detach: list[tuple[str, routing.Route]] = []
        old = (st.get("matcher"), st.get("backendService"))
        new = (target["matcher"], target["backendService"])
        if st.get("urlMap") and old[0] and old[1] and (st["urlMap"] != target["urlMap"] or old != new):
            detach.append((st["urlMap"], self._owned_route(old[0], old[1])))
        if target["backendService"] in delete_backends:  # recreated under the same name: unroute it first
            detach.append((target["urlMap"], self._owned_route(*new)))
        probe = copy.deepcopy(url_map)
        for map_path, route in detach:
            if map_path == target["urlMap"]:
                routing.strip(probe, route)
        if old[0] and old[1]:
            routing.strip(probe, self._owned_route(old[0], old[1]))
        problems = routing.conflicts(probe, self._route(ctx, target))
        return {
            "foreign": foreign,
            "live": live,
            "deleteBackends": delete_backends,
            "deleteNegs": delete_negs,
            "detach": detach,
            "problems": problems,
        }

    @staticmethod
    def _notes(ctx: Context, target: dict[str, Any]) -> list[str]:
        run = ctx.project.config.cloud_run
        assert run is not None
        notes = [
            (
                "= callers through the load balancer still need roles/run.invoker and a Google ID token "
                f"(audience {ServiceResource._url(ctx)})"
            )
        ]
        if run.ingress == "all":
            notes.append(
                "! ingress all: the service also answers on its run.app URL; set ingress "
                "internal-and-cloud-load-balancing to serve it only through the load balancer"
            )
        elif run.ingress == "internal" and target["scheme"] != "INTERNAL_MANAGED":
            notes.append(
                "! ingress internal refuses traffic from an external load balancer; use "
                "internal-and-cloud-load-balancing"
            )
        return notes

    def plan(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        target = self._target(ctx)
        if target is None:
            if st:
                return Change(self.key, Action.DELETE, st.get("backendService", "load balancer"), self._gone(st))
            return Change(self.key, Action.NOOP, "not behind a load balancer")
        url_map, problem = self._resolve(ctx, target)
        summary = target["backendService"].rsplit("/", 1)[-1]
        action = Action.UPDATE if st else Action.CREATE
        if url_map is None:
            return Change(self.key, action, summary, blocked=problem)
        todo = self._survey(ctx, st, target, url_map)
        if todo["foreign"] or todo["problems"]:
            return Change(self.key, action, summary, blocked="; ".join(todo["foreign"] + todo["problems"]))
        details = [f"- routes from URL map {m.rsplit('/', 1)[-1]} ({r.matcher})" for m, r in todo["detach"]]
        details += [f"- backend service {p.rsplit('/', 1)[-1]} (recreated)" for p in todo["deleteBackends"]]
        details += [f"- serverless NEG {p.rsplit('/', 1)[-1]} (recreated)" for p in todo["deleteNegs"]]
        if todo["live"]["neg"] is None or target["neg"] in todo["deleteNegs"]:
            details.append(
                f"+ serverless NEG {target['neg'].rsplit('/', 1)[-1]} → Cloud Run service {target['service']}"
            )
        if todo["live"]["backendService"] is None or target["backendService"] in todo["deleteBackends"]:
            details.append(f"+ backend service {summary} ({target['scheme']})")
        route = self._route(ctx, target)
        if details or st.get("urlMap") != target["urlMap"] or not routing.in_place(url_map, route):
            details.append(routing.describe(route, target["urlMap"].rsplit("/", 1)[-1]))
        if not details and st and not st.get("spec"):
            details.append("~ finish an interrupted deploy")
        if not details:
            return Change(self.key, Action.NOOP, summary)
        return Change(self.key, action, summary, details + self._notes(ctx, target))

    def apply(self, ctx: Context, change: Change) -> None:  # noqa: D102
        target = self._target(ctx)
        if target is None:
            self.destroy(ctx)
            return
        url_map, problem = self._resolve(ctx, target)
        if url_map is None:
            raise RuntimeError(problem)
        st = dict(self._state(ctx))
        todo = self._survey(ctx, st, target, url_map)
        if todo["foreign"] or todo["problems"]:  # checked on a fresh read, before anything is unrouted
            raise RuntimeError("; ".join(todo["foreign"] + todo["problems"]))
        for map_path, route in todo["detach"]:
            if ctx.clients.compute_get(map_path) is not None and ctx.clients.url_map_edit(
                map_path, lambda m, r=route: routing.strip(m, r)
            ):
                ctx.echo(f"    removed {route.matcher} routes from {map_path}")
        if st.get("urlMap") and st["urlMap"] != target["urlMap"]:
            st.pop("urlMap")
            self._set_state(ctx, st)
        for key, paths in (("backendService", todo["deleteBackends"]), ("neg", todo["deleteNegs"])):
            for resource in paths:  # backend services first: they hold the NEG
                self._delete_owned(ctx, key, resource)
                if st.get(key) == resource:
                    st.pop(key)
                    if key == "backendService":
                        st.pop("scheme", None)
                    self._set_state(ctx, st)
        marker = self._marker(ctx)
        st["matcher"] = target["matcher"]
        for key, body in (
            ("neg", {"networkEndpointType": "SERVERLESS", "cloudRun": {"service": target["service"]}}),
            (
                "backendService",
                {"loadBalancingScheme": target["scheme"], "backends": [{"group": routing.link(target["neg"])}]},
            ),
        ):
            if ctx.clients.compute_get(target[key]) is None:
                collection, _, name = target[key].rpartition("/")
                ctx.clients.compute_insert(collection, {"name": name, "description": marker, **body})
                ctx.echo(f"    created {target[key]}")
            st[key] = target[key]
            if key == "backendService":
                st["scheme"] = target["scheme"]
            self._set_state(ctx, st)
        # Recorded before the edit: a crash in between leaves entries that the next run or `remove` strips.
        st["urlMap"] = target["urlMap"]
        self._set_state(ctx, st)
        route = self._route(ctx, target)
        if ctx.clients.url_map_edit(target["urlMap"], lambda m: routing.reconcile(m, route)):
            ctx.echo(f"    {routing.describe(route, target['urlMap'].rsplit('/', 1)[-1])[2:]}")
        self._set_state(ctx, {**st, "spec": target})

    @staticmethod
    def _gone(st: dict[str, Any]) -> list[str]:
        lines = []
        if st.get("urlMap"):
            lines.append(f"- routes from URL map {st['urlMap'].rsplit('/', 1)[-1]} (only the agent's own)")
        if st.get("backendService"):
            lines.append(f"- backend service {st['backendService'].rsplit('/', 1)[-1]}")
        if st.get("neg"):
            lines.append(f"- serverless NEG {st['neg'].rsplit('/', 1)[-1]}")
        return lines

    def _detach(self, ctx: Context, st: dict[str, Any]) -> None:
        """Strip the agent's entries from the URL map named in state (a URL map deleted since is fine)."""
        url_map = st.get("urlMap")
        if not url_map:
            return
        owned_name(url_map, self._pattern(ctx, "urlMap"), "the URL map")
        if ctx.clients.compute_get(url_map) is None:
            return
        matcher = owned_name(st.get("matcher", ""), rf"{routing.MATCHER_PREFIX}[a-z0-9-]+", "the path matcher")
        backend = st.get("backendService", "")
        route = self._owned_route(
            matcher, backend and owned_name(backend, self._pattern(ctx, "backendService"), "the backend service")
        )
        if ctx.clients.url_map_edit(url_map, lambda m: routing.strip(m, route)):
            ctx.echo(f"    removed routes from {url_map}")

    @staticmethod
    def _pattern(ctx: Context, key: str) -> str:
        """Shape of the NEG, backend service or URL map paths agentless records for this deployment."""
        scope = r"(?:global|regions/[a-z0-9-]+)"
        project = f"projects/{project_ref(ctx)}"
        return {
            "neg": rf"{project}/regions/[a-z0-9-]+/networkEndpointGroups/[a-z][-a-z0-9]*-neg",
            "backendService": rf"{project}/{scope}/backendServices/[a-z][-a-z0-9]*-lb",
            "urlMap": rf"{project}/{scope}/urlMaps/[a-z][-a-z0-9]*",
        }[key]

    def _delete_owned(self, ctx: Context, key: str, path: str) -> None:
        """Delete a NEG or backend service agentless created; one without its marker is never deleted."""
        owned_name(path, self._pattern(ctx, key), "the NEG" if key == "neg" else "the backend service")
        live = ctx.clients.compute_get(path)
        if live is None:
            return
        if live.get("description") != self._marker(ctx):
            raise RuntimeError(f"{path} was not created by agentless for this agent; refusing to delete it")
        ctx.clients.compute_delete(path)
        ctx.echo(f"    deleted {path}")

    def plan_destroy(self, ctx: Context) -> Change:  # noqa: D102
        st = self._state(ctx)
        if not st:
            return Change(self.key, Action.NOOP, "not behind a load balancer")
        return Change(self.key, Action.DELETE, st.get("backendService", "load balancer"), self._gone(st))

    def destroy(self, ctx: Context) -> None:  # noqa: D102
        st = dict(self._state(ctx))
        if not st:
            return
        self._detach(ctx, st)  # first: a backend service still routed can't be deleted
        st.pop("urlMap", None)
        self._set_state(ctx, st)
        for key in ("backendService", "neg"):  # the backend service holds the NEG
            if st.get(key):
                self._delete_owned(ctx, key, st[key])
                st.pop(key)
                self._set_state(ctx, st)
        self._set_state(ctx, None)


class InvokerResource(AccessResource):
    """Who may call the service: roles/run.invoker bindings on it. Public members are never granted or tolerated.

    The exception is auth: webhook, where allUsers is the one invoker and the container gates calls with the shared
    secret. allAuthenticatedUsers, or allUsers on another role, still blocks.
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
    def members(ctx: Context) -> list[str]:  # noqa: D102
        if run_spec.webhook_header(ctx.project):
            return [run_spec.ALL_USERS]  # access.invokers and publish are rejected with auth: webhook
        return AccessResource.members(ctx)

    def _public(self, ctx: Context, service: str | None) -> list[str]:
        members = ctx.clients.iam_members("runService", service) if service else None
        # allUsers as the webhook invoker is wanted; one agentless granted is revoked when leaving webhook mode.
        all_users = (run_spec.INVOKER_ROLE, run_spec.ALL_USERS)
        owned = (self.rtype, service, *all_users) in self._owned(ctx)
        allowed = {all_users} if run_spec.webhook_header(ctx.project) or owned else set()
        return sorted(
            f"{role} → {m}"
            for role, ms in (members or {}).items()
            for m in ms
            if m in _PUBLIC and (role, m) not in allowed
        )

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
        service = self._engine(ctx)
        webhook = service is not None and run_spec.webhook_header(ctx.project) is not None
        granted = webhook and _all_users_invoker(ctx, service)
        super().apply(ctx, change)
        if webhook and not granted and _all_users_invoker(ctx, service):  # just opened: ServiceResource did not probe
            _probe_webhook(ctx, service, ServiceResource._url(ctx))


ALL_RESOURCES: tuple[type[Resource], ...] = (
    ServiceAccountResource,
    BucketsResource,
    IamResource,
    SessionEngineResource,
    BuildResource,
    ServiceResource,
    LoadBalancerResource,
    InvokerResource,
    AgentRegistryResource,
    GeminiEnterpriseResource,
)
