# Cloud Run target (implemented 2026-10-25)

Code: `src/agentless/providers/cloud_run/`. Tests: `tests/unit/test_cloud_run.py` against `FakeGcp`.
User docs: README "Cloud Run target", `examples/cloud-run.yaml`.

## Decisions taken
1. **Build and ship:** agentless builds the image itself with Cloud Build. The packaged source goes to
   `gs://<stagingBucket>/agentless/<svc>/<stage>/source/<tag>.tar.gz`, Cloud Build runs `docker build` and pushes to
   `<region>-docker.pkg.dev/<project>/<cloudRun.artifactRegistry>/<service>:<tag>`, and the service runs that image
   pinned by digest.
   - `<tag>` hashes the source and the build args, so an existing tag is reused instead of rebuilt.
   - `cloudRun.image` (built in CI) skips the build.
   - Everything goes over REST through `GcpClients._rest`; there is no gcloud and no extra SDK dependency.
2. **Sessions and Memory Bank:** `SessionEngineResource` creates a bare engine (no code) named
   `<display_name>-sessions`, with the Memory Bank `context_spec`.
   - It exists when `memory.sessions: agentPlatform` (the default) or `memoryBank` is set.
   - The service gets `GOOGLE_CLOUD_AGENT_ENGINE_ID` (the numeric id) and `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION`.
   - The runtime SA gets `roles/aiplatform.user` on that engine only (`IamResource.automatic`, rtype
     `reasoningEngine`); model access is declared in `identity.roles`, never granted project-wide. IAM runs before
     the engine exists, so creating it schedules an `iam` refresh, and a recreated engine doesn't block the plan.
   - Releasing it: the service drops the env var first, and the engine is deleted in `cleanup`.
3. **Never public:** the user decided that callers always go through IAM, internal-only services included.
   - There is no `allowUnauthenticated`, and `allUsers` / `allAuthenticatedUsers` are rejected in `access.invokers`.
   - Services are always sent with `invokerIamDisabled: false`; a check disabled outside agentless is turned back on.
   - A public grant made outside agentless blocks plan and deploy.
   - `ingress: internal` adds to IAM, never replaces it.
   - `auth: googleToken` (2026-10-29): the user wanted a public endpoint that keeps an IAM layer. Cloud Run IAM
     stays on, and callers send the ID token in `X-Serverless-Authorization`. The container verifies the token in
     `googleToken.header` against `AGENTLESS_ALLOWED_PRINCIPALS` / `AGENTLESS_TOKEN_AUDIENCES`
     (`spec.auth_env`; the loader rejects these names in `agent.environment` and `agent.secrets`). `id_token_session(app_header=…)` sends both headers,
     and `CloudRunClients.app_token_header` makes agent card reads do the same. `TokenSession.rebuild_auth` drops both headers on a cross-host redirect (requests only strips `Authorization`). Exclusive with `iap` and
     `ingress: internal`.
   - `auth: webhook` (2026-10-29, reworked 2026-10-30 for 0.12): the one opt-in exception, for GitLab/GitHub
     webhooks that can't mint a Google token. Deployed like runway's mr-terraform-agent: the IAM check stays on
     (`run.managed.requireInvokerIam` forbids `invokerIamDisabled: true`, which 0.10/0.11 used; a live `true` is
     turned back on) and `InvokerResource.members` is `[allUsers]`, an owned binding, so it's revoked on `remove`
     and when switching back to `auth: iam`. `_public` tolerates only `roles/run.invoker → allUsers`, in webhook
     mode or while revoking an owned one. Domain Restricted Sharing needs the org's exemption tag
     (`allUsersIngress`) in `cloudRun.tags`; tags are bound in `service`, before `access` grants. The container
     gets `AGENTLESS_WEBHOOK_HEADER` plus `AGENTLESS_WEBHOOK_SECRET` as a `secretKeyRef`; `IamResource.declared`
     grants the runtime SA accessor on it. `_probe_webhook` (`CloudRunClients.webhook_probe`: no header, then a
     random wrong value) runs once per rollout: in `InvokerResource.apply` right after `allUsers` is granted, or in
     `ServiceResource._check_webhook_gate` for a new revision of a service that's already public. Unless both are
     401/403 it revokes `allUsers` and raises (also on any exception): fail closed, and the next deploy grants and
     probes again. The probe treats Cloud Run IAM's own 403 (Google Frontend "does not have permission to get
     URL", while the grant propagates) as no answer and retries (`_PROBE_ATTEMPTS`), else 0. Leaving webhook mode,
     `ServiceResource.apply` revokes `allUsers` before the PATCH: the new revision has no gate. The schema rejects `access.invokers`, `publish` and `iap` with it. `invoke` uses
     `webhook_session` (secret header only, read via `sources.read_secret`, dropped on cross-host redirects).
     Fakes: `require_invoker_iam`, `all_users_tag`, and `webhook_probe` answering 403 until `allUsers` is granted.
4. **v1 scope:** invokers, GE (A2A only), Agent Registry, Direct VPC egress, ingress, IAP.

5. **Tags (`cloudRun.tags`, 2026-10-30):** for org policies that check a tag on the service (`run.allowedIngress`
   with `allowIngressAllForCloudRun`, as runway does). `ServiceResource._create` creates a tagged service closed
   (ingress internal, `invokerIamDisabled: false`), binds the tags, then PATCHes the real body. On updates,
   `_tag_diff` binds before the PATCH and unbinds after it. `state.service.tags` lists only the bindings agentless
   made; a wanted tag already bound outside agentless is never recorded, so it's never removed. Calls go to the
   regional Resource Manager v3 endpoint (`<region>-cloudresourcemanager.googleapis.com`), parent
   `//run.googleapis.com/projects/<number>/locations/<region>/services/<id>`. `FakeGcp.ingress_all_tag` simulates
   the org policy.

## Resource notes
- **`ServiceResource`:**
  - Its spec (`cloud_run/spec.desired_spec`) is a flat dict like the engine spec, so `spec.diff`, `describe` and
    `redact` work unchanged.
  - The image is tracked separately: `state.service.build` holds the build tag, or `state.service.image` for a
    prebuilt image.
  - `--code-only` GETs the live service and swaps only the container image, because the stored spec is redacted
    and can't be re-sent. It sends back every live field except output-only ones (`_OUTPUT_ONLY`), so IAP and the
    launch stage survive.
  - `InvokerResource.always_blocks`: its block (a public grant) survives `--code-only`.
  - Adoption requires the `agentless-service` / `agentless-stage` labels; otherwise it blocks.
- **`InvokerResource`** is `AccessResource` with `rtype = "runService"`, `noun = "service"` and the role fixed to
  `roles/run.invoker`.
- **Agent card:** `agent_card(ctx)` reads `state.service.cardUrl`
  (`<url>/a2a/<agent_directory>/.well-known/agent-card.json`), so `AgentRegistryResource` and
  `GeminiEnterpriseResource` are reused unchanged.
- **`APP_URL`** is `https://<svc>-<projectNumber>.<region>.run.app`. It's deterministic, so it's known before
  creation and there is no second update like on Agent Runtime.
- **ID tokens** (`cloud_run/clients.id_token_session`), only for `https://*.run.app` URLs (`audience` refuses any
  other, so a tampered state file can't redirect them):
  - An impersonated deployer uses `IDTokenCredentials`.
  - SA keys use `with_target_audience`.
  - Otherwise the metadata server is used.
  - User ADC sends the `id_token` its refresh returns. Cloud Run accepts it for user accounts holding `run.invoker`
    (verified live), like `gcloud auth print-identity-token`. The metadata server is the last resort.
  - `roles/run.admin` includes `run.routes.invoke`, so the deployer can read the card without an extra grant.

## Container contract (agents-cli Cloud Run scaffold)
- Port 8080: `uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080`.
- A2A is served at `/a2a/<app>`, with the card at `/a2a/<app>/.well-known/agent-card.json`; the card URL comes from
  `APP_URL`.
- ADK routes: `POST /apps/<app>/users/<user>/sessions`, then `POST /run_sse` (what `agentless invoke` calls).
- Sessions: `SESSION_SERVICE_URI`, then `GOOGLE_CLOUD_AGENT_ENGINE_ID` (+ `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION`), otherwise in-memory.
- Serve health checks on `/health` or `/`, not `/healthz`, which Cloud Run reserves.
- The manifest must say `deployment_target: cloud_run`; the loader rejects a mismatch with `provider.name`.

## Not supported yet (validation or the CLI rejects them)
`eval`, `evaluation.afterDeploy`, `evaluation.monitors`, `deploy --no-wait` / `--status`, PSC-I, `serverMode`,
`identity.type` other than `serviceAccount`, `access.role`, and GE `registration: adk`.

## Verified live (2026-10-25, a dummy agent in a sandbox project)
- `plan` reads: the service GET (404 → create), the Artifact Registry repository and tag GETs, the engine lookup, IAM.
- The Cloud Run Admin v2 create (`services?serviceId=`), the PATCH update with the whole body, LRO polling,
  `getIamPolicy` as a GET, `setIamPolicy`, and the delete.
- Cloud Build regional builds: the digest is read from `results.images[].digest`. **Poll the build
  (`projects/<p>/locations/<r>/builds/<id>`), not the returned operation:** a regional build's operation name 404s on
  `/v1/operations/...`.
- An Artifact Registry tag that already exists is reused without rebuilding (after a deploy failed mid-build).
- **APIs just enabled:** a build created minutes after enabling `cloudbuild.googleapis.com` failed with a 403
  ("The caller does not have permission"); the rerun a few minutes later worked.
- The session engine works: ADK sessions persisted across `invoke` calls. Remove deletes the service, the engine,
  the SA, its project bindings and the uploaded sources.
- An anonymous request to the service returns 403.

## Not verified against live APIs
- `cloudRun.tags`: the regional tagBindings list/create/delete on a Cloud Run service and the LRO polling on the
  regional endpoint.
- `auth: googleToken`: that Cloud Run accepts `X-Serverless-Authorization` from user and impersonated tokens and
  forwards the custom `x-google-token` header unchanged (the docs don't list `X-Google-*` as reserved). Check it
  with a header-echo service before relying on it.
- `auth: webhook`: the `allUsers` grant under Domain Restricted Sharing with the exemption tag, and the anonymous
  probe against a real service.
- Direct IAP (`iapEnabled` plus `launchStage: BETA`), Direct VPC egress, and `--code-only` (output-only fields left in
  the body).
- Gemini Enterprise A2A registration of a Cloud Run card URL, the Agent Registry, and GE's service agent calling the
  service with `run.invoker`.
