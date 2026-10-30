# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Behaviour change of `cloudRun.auth: webhook`: the invoker IAM check stays on, and `allUsers` holds
  `roles/run.invoker`,** the way runway deploys a webhook receiver. 0.10 and 0.11 turned the check off
  (`invokerIamDisabled: true`), which the `run.managed.requireInvokerIam` org policy forbids. The `allUsers` grant
  is owned by agentless: revoked on `remove` and when switching back to `auth: iam`. Under Domain Restricted Sharing,
  add your org's exemption tag in `cloudRun.tags`. The anonymous probe now runs right after the grant, or after each
  new revision of a public service, and a failure revokes `allUsers` instead of turning IAM back on. A service
  deployed by 0.10 or 0.11 gets its IAM check back on at the next deploy, then the grant (a few seconds of 403).

## [0.11.0] - 2026-10-30

### Added
- **Tags on Cloud Run services, `cloudRun.tags`.** For org policies that check a tag on the service itself, such as
  `run.allowedIngress` allowing `ingress: all` only on tagged services. A new service is created closed (ingress
  internal, invoker IAM check on), tagged, then given its settings, so it's never reachable before its tags are.
  On an existing service, new tags are bound before the update and dropped ones unbound after it. Tags bound outside
  agentless are never removed. The deployer needs `roles/resourcemanager.tagUser` on each tag value.

## [0.10.1] - 2026-10-29

### Changed
- **Plan colours like Terraform.** Detail lines now take the colour of their marker: green `+` added, yellow `~`
  changed, red `-` removed, so a dropped env var or revoked binding inside an update stands out. Replaces are
  red (destroy then recreate), `!` warnings bold yellow, and each count of the `Plan:` line is coloured.
- **Colours in CI logs.** Click stripped them when the output wasn't a terminal. They're now on in CI (`CI`,
  `GITLAB_CI`, `GITHUB_ACTIONS`…) and with `FORCE_COLOR` / `CLICOLOR_FORCE`, off with `NO_COLOR`, and
  `agentless --color` / `--no-color` overrides them.

## [0.10.0] - 2026-10-29

### Added
- **Webhook receivers on Cloud Run, `cloudRun.auth: webhook`.** For senders that can't get a Google token
  (GitLab, GitHub webhooks). The service runs with the invoker IAM check off (`invokerIamDisabled`, no `allUsers`
  grant), and the container lets in only requests whose `cloudRun.webhook.header` (default `x-gitlab-token`)
  matches the Secret Manager secret in `cloudRun.webhook.secret`. It gets `AGENTLESS_WEBHOOK_HEADER` and
  `AGENTLESS_WEBHOOK_SECRET`, and the runtime SA can read the secret. After each rollout agentless sends
  anonymous requests without the header and with a wrong value. Unless both get 401/403, it turns IAM back on and
  fails the deploy. `invoke` sends the secret instead of a Google token. `access.invokers`, `publish` and `iap`
  are rejected in this mode. The README has the FastAPI middleware.

### Fixed
- **Cloud Run `plan` reported an update on every run** with `auth: googleToken` or an ingress warning: plan notes
  now only come with a real change.

## [0.9.0] - 2026-10-29

### Added
- **Public Cloud Run endpoint with two checks, `cloudRun.auth: googleToken`.** The service stays behind Cloud Run
  IAM: the invoker check is never turned off and `allUsers` is still rejected. Callers send their ID token in
  `X-Serverless-Authorization`, and the container also verifies the Google token in `cloudRun.googleToken.header`
  (default `x-google-token`) against `allowedPrincipals` and `audiences`, passed as `AGENTLESS_*` env vars.
  `invoke` and agent card reads send both headers, and `invoke --app-token` overrides the app token. The README
  has the FastAPI middleware.

## [0.8.0] - 2026-10-29

### Security
- **Vulnerable dependencies updated:** certifi 2026.10.23 (GHSA-6q48-6c7w-9gw2, compromised emSign CAs),
  aiohttp 3.14.5, opentelemetry 1.45.1 and, through `google-cloud-aiplatform` 2.6.0, litellm 1.96.2.
  `.vex/agentless.openvex.json` marks litellm CVE-2026-114612 (LiteLLM proxy only) as not affected until
  aiplatform allows litellm >= 1.100.4.
- **Cloud Run sessions grant scoped to the engine:** the runtime SA gets `roles/aiplatform.user` on the companion
  session engine only, not on the project. Declare model access in `identity.roles` (as on Agent Runtime).
- **`deploy --code-only` on Cloud Run** keeps every live service setting (IAP and launch stage were dropped), and no
  longer lifts the block raised when the service has a public grant.
- **ID tokens** are only sent to `https://*.run.app` URLs, so a tampered state file can't redirect them.

### Fixed
- **Redeploying after `remove` failed** with `403 iam.serviceAccounts.get` on both targets: through `projects/-`, IAM
  answers 403 instead of 404 for a recently deleted service account. User-managed accounts are now looked up in
  the project their email names.
- **`remove` of a Cloud Run agent with a session engine stopped half-way:** the engine is deleted before IAM revokes
  the grant on it. A target that no longer exists now counts as revoked.

### Added
- **Cloud Run target.** `provider.name: cloud-run` deploys the agent as a Cloud Run service, configured by a new
  `cloudRun` block: ingress, IAP, Direct VPC egress, timeout, CPU throttling, session affinity and port.
  - **Build:** the source is built with Cloud Build into Artifact Registry, tagged by the hash of the source and the
    build args, and the service runs it pinned by digest. A prebuilt `cloudRun.image` skips the build.
  - **Sessions:** platform sessions and Memory Bank live in a companion bare engine, `sessionEngine`.
  - **Callers:** `access.invokers` get `roles/run.invoker`.
  - **Publishing:** Gemini Enterprise (A2A) and the Agent Registry register the service's agent card.
  - **Operations:** `logs`, `metrics`, `open` and `invoke` work against the service. Every call sends an ID token,
    including the one from plain user credentials.
- **Agents are never public.** `allUsers` and `allAuthenticatedUsers` are rejected in `access.invokers` on every
  target. On Cloud Run, the invoker IAM check is always on, and a public grant made outside agentless blocks
  `plan` and `deploy`.

### Changed
- **`google-cloud-aiplatform` 2.x** (`>=2.6.0,<3`). Agent Runtime calls go through the SDK's `runtimes`
  (renamed from `agent_engines`), with a fallback for 1.x.
- The manifest's `deployment_target` must match `provider.name`.
- `agentless invoke` exits 1 with the agent's error when the stream carries one, instead of printing nothing.
- Provider orchestration moved to `providers/base.py` (`BaseProvider`). Imports from
  `agentless.providers.agent_runtime.provider` still work.

## [0.7.1] - 2026-10-25

### Fixed
- **Bucket lifecycle rules are no longer erased** when a bucket is adopted or updated without `lifecycle` in
  `agent.yaml`. Rules set outside agentless are kept until `lifecycle` is declared. Once it is, `plan` also shows
  rules that `deleteAfterDays` can't express (prefix conditions, storage class transitions) as removed.

## [0.7.0] - 2026-10-22

### Added
- **`agentless eval`** evaluates the deployed engine of a stage on the Agent Platform Evaluation Service, prints each
  metric against its threshold, saves the report under `.agentless/<stage>/eval/` and exits 1 below a threshold.
  Needs the new `agentless-cli[eval]` extra.
- **Deploy gate:** `evaluation.afterDeploy` or `deploy --eval` runs the evaluation after a successful apply and fails
  the command below a threshold (nothing is rolled back). `--no-eval` skips it.
- **Online monitors:** `evaluation.monitors` declares Agent Platform online evaluators that score live traces. A new
  `evaluation` step (after `access`) creates, patches, suspends and deletes them, and recreates them with the
  engine. They require `observability.tracing.captureContent: true`, and set
  `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` on the engine.

## [0.6.0] - 2026-10-15

### Changed
- **`access.invokers` are granted on the reasoning engine, not the project.** They can call this agent and no other.
  The Gemini Enterprise service agent (`grantServiceAgent`) moves to the engine too. These bindings are now owned:
  removing an invoker revokes it. A new `access` step (after `engine`) manages them, and re-grants them on a
  recreated engine.
- **Upgrading from 0.5.0:** the project-level `roles/aiplatform.user` bindings 0.5.0 granted to invokers and to the
  Discovery Engine service agent were never tracked, so agentless leaves them in place. Remove them by hand if
  nothing else needs them.

### Added
- `access.role` (default `roles/aiplatform.user`). For least privilege, use a custom role with only
  `aiplatform.reasoningEngines.query`.
- `publish.geminiEnterprise.sharing` accepts `restricted` and `private` besides `allUsers`.

## [0.5.0] - 2026-10-15

### Added
- `publish.agentRegistry`: registers an A2A agent's card as an `A2A_AGENT_CARD` service in the Agent Registry, where
  Agent Gateways govern it. The card is read from the deployed engine; `plan` shows card drift, and a code deploy
  refreshes it in the same run. Renames register the new service before deleting the old one.
- `publish.geminiEnterprise.registration: a2a` registers the agent card in Gemini Enterprise instead of the ADK
  engine (default stays `adk`), with the authorization attached as `agentAuthorization`. Switching modes replaces
  the registration.
- `publish.geminiEnterprise.sharing: allUsers`, and `agentGateway`, which sets the app's egress Agent Gateway when
  the app has none. A different gateway already on the app is left alone with a warning, and nothing is reverted.
- `access.invokers`, and an automatic `roles/aiplatform.user` for the Gemini Enterprise app's Discovery Engine
  service agent (`grantServiceAgent`, on by default). Both are grant-only: added when missing, never revoked.
- `authorization.displayName`; the authorization now also sends its `scopes` field.
- The deployer needs `roles/agentregistry.editor` for `publish.agentRegistry`.

### Fixed
- `info` printed an `a2aCardUrl` without the `/api` passthrough segment; it now matches agents-cli.
- A Gemini Enterprise app move interrupted between registering the new agent and removing the old one no longer
  leaks the old registration: the next `plan` finishes the cleanup.

## [0.4.0] - 2026-10-08

### Added
- `resources.buckets` in `agent.yaml`: GCS buckets agentless creates and owns, for example for generated reports.
  Uniform access and public access prevention are always on. You can set location, storage class, versioning,
  lifecycle (`deleteAfterDays`) and CMEK. `access` grants a storage role to the runtime identity automatically,
  and `env` injects the bucket name into the agent.
- Buckets are never replaced and their data is never deleted. `deletionPolicy: retain` (the default) keeps a bucket
  on `remove`, or when it's dropped from `agent.yaml`. `delete` removes it only if it's empty. A location change is
  blocked. An existing bucket in the project is adopted and always kept.
- `info` lists the managed buckets. The deployer needs `roles/storage.admin` when buckets are declared.

## [0.3.0] - 2026-10-08

### Added
- `observability.tracing` in `agent.yaml` (`enabled`, `captureContent`) configures Cloud Trace export. YAML is the
  only source; use `${param:}` to vary it per stage or from `-p`.
- With tracing on, the runtime identity gets `roles/cloudtrace.agent`, `roles/logging.logWriter` and
  `roles/monitoring.metricWriter` automatically, for a service account and for an Agent Identity principal.
  `plan` marks them `(automatic: tracing)`. Turning tracing off revokes only what agentless granted.
- `agentless metrics [--since 1h] [--json]`: request count, 5xx rate and p50/p95 latency from Cloud Monitoring.
  The deployer needs `roles/monitoring.viewer`.
- `agentless open [console|logs|traces] [--print]`: Cloud Console links for the engine.

### Deprecated
- `agent.telemetry` still works and maps to `observability.tracing`, with a warning. Setting both is an error.

## [0.2.3] - 2026-10-07

### Fixed
- Switching an engine to `identity.type: agentIdentity` (or deploying a new one) no longer fails with
  `400 Cannot update encryption_spec in ReasoningEngine`. The first update of the minted engine sent
  `encryption_spec`, which is fixed when that engine is created. A deploy that failed this way can simply be re-run.

## [0.2.2] - 2026-10-05

### Fixed
- A service-account engine reports its service account email as its effective identity. agentless no longer
  mistakes that for an Agent Identity principal, which produced a `principal://<sa-email>` IAM member that GCP
  rejects with `400 … unknown type`, and kept `identity.type: agentIdentity` switches from minting a principal.
  State written by earlier versions is handled.

## [0.2.1] - 2026-10-05

### Fixed
- A blank `--impersonate-service-account`, `AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT` or `provider.deployer.impersonate`
  (empty, spaces, or only commas) now counts as not set and falls back to the next source and then ADC, instead of
  failing with "is empty".

## [0.2.0] - 2026-10-05

### Added
- Deploy as a service account: every GCP call, including `${secret:}` / `${tf:}` reads, the state bucket, `logs`
  and `invoke`, can impersonate a deployer. Set it with `--impersonate-service-account` (gcloud-style
  `delegate,…,target` chains), `AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT`, or `provider.deployer.impersonate` /
  `delegates` in `agent.yaml` (per stage through `${param:}`), in that order of precedence.
- `plan` and `deploy` headers, `validate`, `info`, the state lock and `updatedBy` show the impersonated deployer.
- A hint about `roles/iam.serviceAccountTokenCreator` when impersonation is refused.

## [0.1.1] - 2026-10-05

### Fixed
- Packaging skips `.venv/`, `venv/`, `__pycache__/` and `*.pyc` even when the agent has no `.gcloudignore` or
  `.gitignore`. Before, a local virtualenv was uploaded and the deploy failed on its interpreter symlink. Agents
  whose previous upload included these files rebuild once.
- A symlink pointing outside the agent directory (or nowhere) now fails `plan` and `package` with the file name and a
  fix, instead of failing midway through `deploy`.
- Source validation errors from the Agent Platform SDK print as a `✖` message instead of a traceback.

## [0.1.0] - 2026-10-05

First public release: wheel and sdist (`agentless-cli`) on the GitHub Release, and the image `ghcr.io/Aymen2518/agentless`.

### Added
- One declarative `agent.yaml` per agent, with stages, params and a Pydantic-validated schema exported as JSON
  Schema (`agentless schema`) for IDE completion.
- Variables: `${stage}`, `${opt:}`, `${param:}`, `${self:}`, `${env:}`, `${file():}`, `${secret:}` and `${tf:}` (Terraform
  outputs), with fallbacks, resolved only for the selected stage.
- Commands: `version`, `init`, `validate`, `print` (secrets masked), `package`, `plan` (with `--detailed-exitcode`),
  `deploy`, `info`, `logs`, `invoke`, `remove`, `unlock` and `schema`.
- Resources reconciled in order and removed in reverse: runtime service account, Agent Identity, IAM bindings
  (project, org, folder, bucket, dataset, secret, service account), the Agent Runtime reasoning engine, and Gemini
  Enterprise registration with OAuth authorization.
- Minimal engine updates: config changes use update masks, and code is re-uploaded only when the source hash changes.
- Memory Bank, PSC-I networking and CMEK settings, with guards against changes that would replace the engine.
- State per service and stage in GCS (or locally under `.agentless/`), with a generation-match lock and secret
  redaction.
- agents-cli compatibility: reads `agents-cli-manifest.yaml` and writes `deployment_metadata.json`.
- Plugins through pluggy entry points in the `agentless` group.
- Multi-arch container image and Claude Code skills for developing agentless and deploying agents with it.

[Unreleased]: https://github.com/Aymen2518/agentless/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/Aymen2518/agentless/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/Aymen2518/agentless/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Aymen2518/agentless/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Aymen2518/agentless/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Aymen2518/agentless/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/Aymen2518/agentless/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Aymen2518/agentless/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Aymen2518/agentless/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Aymen2518/agentless/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Aymen2518/agentless/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Aymen2518/agentless/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Aymen2518/agentless/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Aymen2518/agentless/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Aymen2518/agentless/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Aymen2518/agentless/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Aymen2518/agentless/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Aymen2518/agentless/releases/tag/v0.1.0
