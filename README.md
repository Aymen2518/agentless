# agentless

Serverless Framework–style deployments for ADK agents on **Google Cloud Agent Platform (Agent Runtime)** or
**Cloud Run**.

You build the agent with `agents-cli` (scaffold, playground, local eval). You describe *where and how* it runs in one
`agent.yaml`. Then:

```bash
agentless deploy --stage dev
```

agentless reads the file, works out what changed against what is deployed, and applies only that. The service
account, IAM bindings, reasoning engine, Memory Bank, PSC-I networking and Gemini Enterprise registration all come
from the YAML. There are no Terraform files per agent and no long `agents-cli deploy` flag lists.

- [What and why](#what-and-why)
- [How it works](#how-it-works)
- [Install](#install) and [quick start](#quick-start)
- [`agent.yaml` reference](#agentyaml)
- [Commands](#commands), [permissions](#permissions), [plugins](#plugins), [CI/CD](#cicd), [container image](#container-image)
- [Cloud Run target](#cloud-run-target)
- [Code layout](#code-layout), [extending agentless](#extending-agentless), [status and roadmap](#status-and-roadmap)
- [Development and AI skills](#development), [license](#license)

## What and why

Before agentless, deploying an agent to Agent Runtime meant juggling three tools:

1. **Terraform per agent** for the service account, IAM, bucket and a placeholder engine. This was the generated
   `deployment/terraform/` that agents-cli scaffolds into every agent repo.
2. **`agents-cli deploy`** with a dozen flags (`--service-account`, `--update-env-vars`, `--secrets`, sizing, PSC…)
   that lived in someone's shell history or a `DEPLOYMENT.md`.
3. **`agents-cli publish`** as a separate step for Gemini Enterprise.

Settings like labels and Memory Bank weren't exposed anywhere, and nothing showed you what a deploy would change.

agentless copies the model that made the [Serverless Framework](https://www.serverless.com/framework/docs) popular:

- **One declarative file per agent**, with stages and variables.
- **One command** that reconciles the cloud with that file.
- **A state record**, so deploys are incremental and `remove` only cleans up what the tool created.

On top of Serverless, it adds a real `plan` (diff) step and stricter safety rules. See
[How it maps to Serverless](#how-it-maps-to-serverless).

## How it works

```
agent.yaml ──► load ──► resolve ${…} ──► validate ──► package ──► lock ──► plan ──► apply ──► save state
               (stage)  (selected stage   (pydantic)   (files +    (GCS    (diff per  (in order;  (+ deployment_
                         only)                          sha256)     object) resource)  deferred    metadata.json)
                                                                                       deletes last)
```

**Resources**, applied in this order and destroyed in reverse:

| # | Resource | Owns |
|---|---|---|
| 1 | `serviceAccount` | runtime SA (created, or an existing one referenced) |
| 2 | `agentIdentity` | bare engine that mints the Agent Identity principal (only for `identity.type: agentIdentity`) |
| 3 | `iam` | role bindings for the runtime identity: project, org, buckets, secrets, datasets, SAs |
| 4 | `engine` | the reasoning engine: config (with update masks) and code (source tarball, rebuilt only when the hash changes) |
| 5 | `access` | who may call the agent: bindings on the engine itself (`access.invokers`, the GE service agent) |
| 6 | `evaluation` | online monitors: Agent Platform online evaluators that score the engine's live traces |
| 7 | `agentRegistry` | the agent's A2A card, registered as a service in the Agent Registry |
| 8 | `geminiEnterprise` | registration in a Gemini Enterprise app (ADK or A2A), plus its OAuth authorization |

**State** is one JSON object per service and stage. It lives in `gs://<stagingBucket>/agentless/<service>/<stage>/`,
or in `.agentless/<stage>/` locally when `stagingBucket` is unset. It records:

- what agentless created
- the last-applied engine spec (with secrets redacted)
- the source hash
- the IAM bindings agentless added
- any pending long-running operation

Each deploy diffs the desired config against this record and against live GCP reads.

**Who owns what:**

| Owner | Scope |
|---|---|
| **agentless** (`agent.yaml`) | everything per agent: SA, IAM grants, buckets the agent owns (reports, outputs), engine config and code, Memory Bank, PSC-I, Agent Registry and GE registration |
| **Your infrastructure-as-code** (Terraform, etc.) | per environment: APIs, deployer SA, state bucket, telemetry dataset, the Gemini Enterprise app and Agent Gateway, and shared data resources (datasets, secrets, buckets used by several agents) |
| **agents-cli** | developing the agent: scaffold, playground, local eval. agentless evaluates the *deployed* engine (`agentless eval`, online monitors). agents-cli reads the `deployment_metadata.json` agentless writes, so `run --url` and `publish` still work |

## Install

Releases are published on GitHub: the wheel and sdist are attached to each
[GitHub Release](https://github.com/Aymen2518/agentless/releases), and the CLI image is on GHCR. Pick a version from the releases page.

### Homebrew (macOS and Linux)

```bash
brew install Aymen2518/tap/agentless   # adds the tap and installs the latest formula
agentless version

brew upgrade agentless                 # later, to move to a newer release
brew uninstall agentless && brew untap Aymen2518/tap   # to remove it
```

The formula lives in [Aymen2518/homebrew-tap](https://github.com/Aymen2518/homebrew-tap). It installs the release
sdist into a private Python 3.11 virtualenv and pulls the dependencies from PyPI.

- **Builds locally:** there are no prebuilt bottles, so Homebrew needs up-to-date developer tools. If it stops with
  "Your Xcode … is too outdated", update Xcode from the App Store (or remove it and use the Command Line Tools).
- **One `agentless` on your PATH:** if you also installed it with uv or pipx, remove that copy
  (`uv tool uninstall agentless-cli`) so `~/.local/bin` doesn't shadow the Homebrew one. Check with `which agentless`.
- `brew upgrade` only sees a release once the formula in the tap has been bumped (see
  [docs/RELEASING.md](docs/RELEASING.md)).

### uv, pipx or Docker

```bash
uv tool install git+https://github.com/Aymen2518/agentless@v0.2.2
# or, from the release wheel
uv tool install https://github.com/Aymen2518/agentless/releases/download/v0.2.2/agentless_cli-0.2.2-py3-none-any.whl
# or with pipx
pipx install git+https://github.com/Aymen2518/agentless@v0.2.2
# or, without Python: see "Container image" below
docker run --rm ghcr.io/Aymen2518/agentless --help
```

The package is named `agentless-cli`; the command it installs is `agentless`. The `edge` image tag tracks `main`.

Auth uses Application Default Credentials: `gcloud auth application-default login` locally, Workload Identity
Federation in CI.

### Deploying as a service account

agentless can make every GCP call as a deployer service account instead of your own identity. That includes
`${secret:}` and `${tf:}` reads, the state bucket, `logs` and `invoke`. It uses the first of these that is set:

1. `--impersonate-service-account SA_EMAIL` on the command line
2. the `AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT` environment variable
3. `provider.deployer` in `agent.yaml`
4. otherwise, plain ADC

All three are optional. An empty or blank value counts as not set, so CI templates can always pass an optional
input through (`AGENTLESS_IMPERSONATE_SERVICE_ACCOUNT: ${{ inputs.deployer }}`) and get ADC when it's empty.

```yaml
provider:
  project: ${param:project}
  deployer:
    impersonate: ${param:deployer}          # one deployer per stage
    # delegates: [hop@acme-shared.iam.gserviceaccount.com]
stages:
  dev:  { params: { project: acme-agents-dev,  deployer: sa-deployer@acme-agents-dev.iam.gserviceaccount.com } }
  prod: { params: { project: acme-agents-prod, deployer: sa-deployer@acme-agents-prod.iam.gserviceaccount.com } }
```

- **Grant:** whoever runs agentless (you, or the CI identity) needs `roles/iam.serviceAccountTokenCreator` on the
  deployer service account, and on each delegate in a chain. The deployer itself needs the roles under
  [Permissions](#permissions).
- **Chains:** the flag and the env var take gcloud's form, `hop@…,target@…`, which impersonates the last account
  through the others. In YAML, use `delegates:`.
- **No GCP reads in `provider.deployer`:** it's chosen before anything authenticates, so it can use `${param:}`,
  `${opt:}`, `${env:}`, `${self:}` and `${file():}`, but not `${secret:}` or `${tf:}`.
- **Visible:** `plan` and `deploy` print `as <deployer>` in their header, `validate` says where the setting came from,
  and the state lock and `updatedBy` record it.
- `gcloud config set auth/impersonate_service_account` only affects gcloud, not agentless.
  `gcloud auth application-default login --impersonate-service-account=SA` works too, without any agentless setting.

## Quick start

```bash
cd my-agent/                       # an agents-cli project (agents-cli-manifest.yaml, Dockerfile, app/)
agentless init --project my-gcp-project
agentless plan --stage dev         # what would change
agentless deploy --stage dev       # apply it
agentless invoke -m "hello"        # talk to the deployed agent
agentless logs --since 30m --tail
agentless info
agentless remove --stage dev
```

## How it maps to Serverless

| Serverless | agentless |
|---|---|
| `serverless.yml` | `agent.yaml` (`service`, `provider`, `stages`, `agent`, `identity`, `network`, `memory`, `publish`, `custom`) |
| `${self:} ${env:} ${opt:} ${param:} ${file():}` / `${ssm:}` | same syntax, plus `${stage}`, `${secret:name[@version]}` (Secret Manager) and `${tf(gs://bucket/prefix):output}` (Terraform remote-state outputs) |
| `stages.<stage>.params` | same, typically the dev/uat/prod project map |
| CloudFormation stack = state | `gs://<stagingBucket>/agentless/<service>/<stage>/state.json`, locked through GCS generation preconditions |
| hash check, skips unchanged deploys | source fingerprint + last-applied spec; `--force` overrides |
| `deploy function` | `deploy --code-only`, which still records state, so there is no drift |
| `print`, `package`, `info`, `logs`, `invoke`, `remove` | same |
| — | `plan` (a real diff), `validate`, `schema`, `unlock` |
| plugins and lifecycle hooks | `pluggy` hooks via the `agentless` entry point (see [Plugins](#plugins)) |

## `agent.yaml`

[`examples/agent.yaml`](examples/agent.yaml) shows every option, and [`examples/minimal.yaml`](examples/minimal.yaml)
the smallest useful config. The JSON Schema in [`schema/agent.schema.json`](schema/agent.schema.json) gives IDE autocompletion
through the `# yaml-language-server: $schema=...` header.

```yaml
service: report-agent
provider:
  stage: ${opt:stage, 'dev'}
  project: ${param:project}
  region: europe-west1
  stagingBucket: ${param:stateBucket}
stages:
  dev:  { params: { project: acme-agents-dev, stateBucket: acme-agentless-state-dev } }
  prod: { params: { project: acme-agents-prod, stateBucket: "${env:STATE_BUCKET_PROD}" } }
agent:
  runtime: { cpu: "2", memory: 4Gi, minInstances: 0, maxInstances: 5 }
  environment: { REPORTS_BUCKET: "acme-reports-${stage}" }
  secrets: { SLACK_TOKEN: { secret: slack-token } }
identity:
  type: serviceAccount
  serviceAccount: { create: true, name: "sa-report-agent-${stage}" }
  roles:
    project: [roles/aiplatform.user, roles/logging.logWriter, roles/cloudtrace.agent]
    resources:
      - { type: bucket, name: "acme-reports-${stage}", roles: [roles/storage.objectAdmin] }
```

> **YAML gotcha:** quote any value containing `${...}` inside a flow mapping (`{ ... }`), because YAML reads the `{`.
> Block style (one key per line) needs no quotes.

### Blocks

| Block | Purpose | Agent Runtime field |
|---|---|---|
| `provider` | project, region, stage, state bucket, labels, deployer to impersonate | resource location, `labels` |
| `agent.runtime` | cpu, memory, min/max instances, concurrency, server mode | `spec.deploymentSpec.*` |
| `agent.environment` / `agent.secrets` | env vars and Secret Manager refs | `deploymentSpec.env` / `secretEnv` |
| `agent.build.args` | Docker build args | `sourceCodeSpec.imageSpec.buildArgs` |
| `agent.encryption.kmsKey` | CMEK | `encryptionSpec` (immutable) |
| `identity` | `platform` (default service agent), `serviceAccount` (created or existing), `agentIdentity` (preview) | `spec.serviceAccount` / `identityType` |
| `identity.roles` | project, organization, and per-resource roles (bucket, secret, BigQuery dataset, SA, folder) | IAM policies |
| `network.pscInterface` | network attachment + DNS peering | `deploymentSpec.pscInterfaceConfig` (immutable) |
| `memory` | sessions mode, artifacts bucket, Memory Bank models/TTL/topics | `contextSpec.memoryBankConfig` |
| `access` | members allowed to call the agent, and the role they get | reasoning engine IAM policy |
| `publish.agentRegistry` | register the A2A card in the Agent Registry | Agent Registry `services` |
| `publish.geminiEnterprise` | register in a GE app (ADK or A2A), sharing, app egress gateway, optional OAuth authorization with scopes | Discovery Engine `agents` / `authorizations` / app `agentGatewaySetting` |
| `resources.buckets` | GCS buckets agentless creates, with access for the agent and the name in an env var | Cloud Storage buckets, bucket IAM |
| `evaluation` | dataset, metrics and thresholds for `agentless eval` and the deploy gate; online monitors on live traffic | Evaluation Service `evaluationRuns` / `onlineEvaluators` |
| `observability.tracing` | Cloud Trace export (on by default), prompt/response capture (off by default), runtime roles granted automatically | env vars, same as agents-cli; project IAM |
| `cloudRun` | only with `provider.name: cloud-run`: prebuilt image or Artifact Registry repository, ingress, IAP, Direct VPC egress, timeout, CPU throttling, session affinity, port, tags | Cloud Run service (see [Cloud Run target](#cloud-run-target)) |

### Buckets

Declare the buckets an agent writes to, such as generated reports, and agentless creates them before the engine:

```yaml
resources:
  buckets:
    reports:
      name: acme-reports-${stage}      # globally unique
      location: europe-west1           # default: provider.region
      storageClass: STANDARD           # STANDARD | NEARLINE | COLDLINE | ARCHIVE
      versioning: false
      lifecycle: { deleteAfterDays: 90 }
      access: objectUser               # objectViewer | objectUser (default) | objectAdmin | none
      env: REPORTS_BUCKET              # the agent gets REPORTS_BUCKET=acme-reports-dev
      deletionPolicy: retain           # retain (default) | delete
      # kmsKey: projects/…/cryptoKeys/k  # CMEK; the Cloud Storage service agent needs access to the key
```

- **Always on:** uniform bucket-level access, public access prevention, and the `agentless-*` labels plus
  `provider.labels`. Labels set outside agentless are kept. Lifecycle rules are left alone until `lifecycle` is
  declared; from then on agentless owns all of them, and `plan` shows any other rule it would remove.
  `lifecycle: {}` clears them.
- **Access is automatic:** the `access` role is granted to the runtime identity (service account, Agent Identity
  principal or platform service agent). `plan` marks it `(automatic: resources.buckets)`.
- **Reference it elsewhere** with `${self:resources.buckets.reports.name}`.
- **Data is never deleted.** On `remove`, or when a bucket is dropped from `agent.yaml`, `retain` stops managing it
  and leaves it with its data. `delete` removes it only if it's empty, including old object versions; otherwise it's
  kept with a warning. Renaming a bucket creates a new, empty one. Objects are not copied.
- **No replacement:** a different `location` is blocked, because it would need a new bucket. `--allow-replace`
  doesn't override that.
- **Existing buckets:** a declared bucket that already exists in the project is adopted. Its settings are aligned,
  and it's always kept on `remove`. A name owned by another project, or one the deployer can't read, blocks the plan.

### Observability

Tracing is configured in `agent.yaml` only. That file stays the source of truth, so a deploy without some flag can't
silently turn tracing off again.

```yaml
observability:
  tracing:
    enabled: true            # default
    captureContent: false    # default; records prompts and responses in spans when true
```

- **Roles are automatic.** With tracing on, agentless grants the runtime identity `roles/cloudtrace.agent`,
  `roles/logging.logWriter` and `roles/monitoring.metricWriter` on the project. This applies to a service account
  and to an Agent Identity principal. `plan` marks those lines `(automatic: tracing)`. Turning tracing off revokes
  them, unless you also list them in `identity.roles`. Bindings granted outside agentless are never touched. The
  `platform` identity gets nothing extra, since its service agent already has them.
- **Per stage or from the CLI:** point the setting at a param, so it's still declared in the file:

  ```yaml
  observability:
    tracing:
      captureContent: ${param:captureContent, false}
  stages:
    dev: { params: { captureContent: true } }
  ```

  `agentless deploy --stage prod -p captureContent=true` then turns it on for one deploy, and `plan` shows the change.
- **Reading it back:**
  - `agentless logs` shows the engine's Cloud Logging entries.
  - `agentless metrics --since 1h` shows request count, 5xx rate and p50/p95 latency from Cloud Monitoring.
  - `agentless open console|logs|traces` opens the Console, or prints the URL with `--print`. `traces` opens the
    project's Trace explorer, because it can't be pre-filtered by URL.
- `agent.telemetry` (`enabled`, `captureMessageContent`) still works but is deprecated: `validate` and every command
  print a warning. Setting both is an error.

agentless also grants some access automatically. Each secret in `agent.secrets` gets `secretAccessor`, and
`memory.artifactsBucket` gets `storage.objectUser`.

### Evaluation

agentless evaluates the agent **as deployed**: the engine of the selected stage, with its real identity, tools and
data. Keep using `agents-cli eval` to iterate on the agent locally.

```yaml
observability:
  tracing: { enabled: true, captureContent: true }   # online monitors read prompts and responses from traces

evaluation:
  dataset: tests/eval/datasets/basic-dataset.json    # agents-cli EvaluationDataset, relative to agent.yaml
  dest: gs://acme-agent-evals/support-agent/${stage} # default gs://<stagingBucket>/agentless/<service>/<stage>/evals
  metrics:                                           # name: minimum average score (0 to 1); null = report only
    final_response_quality: 0.8
    tool_use_quality: 0.7
    safety: null
  afterDeploy: ${param:evalGate, false}              # run as a gate at the end of `deploy`
  monitors:                                          # online evaluation of live traffic
    quality:
      metrics: [safety, final_response_quality]
      samplingPercent: 10                            # share of traces scored
      maxSamplesPerRun: 100                          # cost cap per run (about every 10 minutes)
      filter: { minDurationSeconds: 2 }              # optional: duration and total token bounds
      enabled: true                                  # false suspends it and keeps the configuration
```

- **On demand:** `agentless eval --stage dev` runs the dataset against the deployed engine on the Agent Platform
  Evaluation Service (inference and grading server-side, the same runs as `agents-cli eval submit --resource-name`).
  It prints each metric's average against its threshold, saves the report under `.agentless/<stage>/eval/`, and exits
  1 when a threshold is missed. `--dataset` and `--metric name=threshold` override the YAML for one run.
- **Deploy gate:** with `afterDeploy: true` (or `deploy --eval`), `deploy` evaluates once the apply succeeded and
  exits 1 below a threshold. Nothing is rolled back: the new version stays deployed, and the pipeline stops before
  the next stage. A deploy with nothing to apply skips the gate unless `--eval` is passed; `--no-wait` always skips
  it. `--no-eval` turns it off for one run.
- **Metrics** are the Evaluation Service's predefined metrics: `safety`, `final_response_quality`,
  `tool_use_quality`, `general_quality`, `grounding`, `hallucination`, `instruction_following`,
  `multi_turn_task_success`, … A bare name uses the latest version; a spec name such as `safety_v1` pins one.
- **Online monitors** are reconciled like any resource: `plan` shows them, changes patch only the changed fields,
  `enabled: false` suspends, removing one from the YAML deletes it, and a recreated engine gets new monitors (the
  agent is fixed at creation). They need tracing with content capture, which `validate` checks, and agentless sets
  `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` on the engine. Scores show in the console (Agents ›
  Deployments › the agent › Dashboard › Evaluation) and in Cloud Monitoring.
- **Install the extra** for `agentless eval`: `uv tool install "agentless-cli[eval] @ git+https://github.com/Aymen2518/agentless@<tag>"`.
  It adds the SDK's evaluation dependencies (pandas and others). Online monitors need nothing extra.

```bash
# CI: deploy dev, gate on quality, then promote
agentless deploy --stage dev --eval && agentless deploy --stage prod
```

### Publishing

```yaml
access:
  invokers: [group:support-users@acme.com]
  role: projects/<project>/roles/agentInvoker   # default roles/aiplatform.user
publish:
  agentRegistry: {}                        # A2A agents: card registered as <service>-<stage> in provider.region
  geminiEnterprise:
    app: projects/<number>/locations/global/collections/default_collection/engines/<app-id>
    registration: adk                      # or a2a
    sharing: allUsers
    agentGateway: projects/<project>/locations/europe-west1/agentGateways/<gateway>
```

- **Agent Registry:** only A2A agents (`create_params.is_a2a` in `agents-cli-manifest.yaml`) serve a card, so `plan`
  blocks otherwise. agentless reads the card from the deployed engine and registers it as an `A2A_AGENT_CARD`
  service. An Agent Gateway whose `registries` include that location then governs the agent. `plan` reads the live
  card, so a changed card shows up as an update. After a code deploy the card is refreshed in the same run. A new
  `serviceId` or `location` registers the new service before deleting the old one.
- **`registration`:** `adk` (the default) lets Gemini Enterprise call the engine natively; agents-cli recommends it
  on Agent Runtime. `a2a` registers the agent card instead, attaches the OAuth authorization as
  `agentAuthorization`, and adopts a registration made by `agents-cli publish` by matching the card's `url`.
  Switching between the two replaces the registration.
- **`sharing`:** `allUsers` shares the agent with everyone in the app, `private` keeps it to its creator and
  admins, and `restricted` limits it to the users and groups given access in the console (the agent's **User
  permissions** tab). There is no public API for per-agent user or group access, so agentless can't manage that
  list. Leaving `sharing` out keeps whatever the app has, so console sharing isn't overwritten.
- **`agentGateway`** sets the app's egress Agent Gateway when the app has none. The setting belongs to the app and is
  shared by all its agents, so agentless never changes a different value (`plan` prints a warning and leaves it) and
  never reverts it on `remove`.
- **Access is granted on the engine, not the project.** `access.invokers` get `access.role` on the reasoning
  engine's own IAM policy, so they can call this agent and no other. When the agent is published, the Gemini
  Enterprise app's Discovery Engine service agent gets the same role, so the app can call the engine (opt out with
  `grantServiceAgent: false`). The service agent is derived from the project number in `app`. If `app` uses
  another project's id, `plan` asks for the number.
  - These bindings are owned like the runtime IAM: removing an invoker revokes it, and bindings added outside
    agentless are never touched. They're granted once the engine exists, and again on a recreated engine.
  - `access.role` defaults to `roles/aiplatform.user`. For least privilege, Google recommends a custom role holding
    only `aiplatform.reasoningEngines.query`; create it in your infrastructure-as-code and set
    `role: projects/<project>/roles/<id>`.
- `info` shows the registry service, the registration mode and the gateway. Its `a2aCardUrl` uses the engine's
  `/api/a2a/<agentDirectory>` passthrough, the same URL agents-cli uses.

### Variables

Resolution happens before validation, so errors point at the YAML path that failed. Fallbacks are lazy: they're only
evaluated when the main value is missing. Only the selected stage (and `default`) is resolved, so a dev deploy never
needs prod's environment variables.

| Syntax | Value |
|---|---|
| `${self:a.b}` or `${a.b}` | another value in the file |
| `${stage}` | selected stage |
| `${opt:name}` | CLI option (`--stage` …) |
| `${param:name}` | `--param name=v`, then `stages.<stage>.params`, then `stages.default.params` |
| `${env:NAME}` | environment variable |
| `${file(./x.yml):a.b}` | value from a YAML/JSON file |
| `${secret:name}` / `${secret:name@3}` / `${secret:projects/p/secrets/s/versions/1}` | Secret Manager payload (masked by `print`) |
| `${tf(gs://bucket/prefix):output}` | Terraform output from a GCS remote state (`<prefix>/default.tfstate`) |
| `${x, 'fallback'}` | fallback: quoted string, number, `true`/`false`/`null`, or another variable |

## What `deploy` does

1. **Load** `agent.yaml`, pick the stage, resolve variables, then validate (pydantic) against the agents-cli
   manifest. The manifest's target must match `provider.name` (`agent_runtime` or `cloud_run`) and the project
   needs a `Dockerfile` (unless a Cloud Run agent uses a prebuilt `cloudRun.image`).
2. **Package** the files exactly as agents-cli would (`.gcloudignore`, else `.gitignore`; `.venv/`, `venv/`,
   `__pycache__/` and `*.pyc` are always skipped) and take their sha256. Symlinks pointing outside the agent are rejected.
   `agent.yaml` and `deployment_metadata.json` don't count towards the hash. This step is offline.
3. **Lock** the state object, then plan each resource against the state and the live GCP view:
   `serviceAccount` → `agentIdentity` → `iam` → `engine` → `geminiEnterprise`.
4. **Apply** the changes in that order, saving state after each resource. Deferred deletions (old SA, old GE
   registration) run in reverse once everything else is done, so a service account is never deleted while it still
   has bindings.
5. **Write** `deployment_metadata.json` in agents-cli format, so `agents-cli run --url`, `deploy --status` and
   `publish` keep working.

### Engine updates are minimal

- A config-only change sends just the changed fields with an `update_mask`, and doesn't trigger a rebuild.
- A code change sends only `source_code_spec`.
- `--code-only` pushes code and leaves config changes for the next full deploy.
- After the first create, a small env-only update sets `APP_URL`, which the A2A card needs and which depends on the
  engine id.
- PSC-I, CMEK and identity type can't change in place. The plan stops (✋) unless you pass `--allow-replace`, which
  deletes and recreates the engine. That loses sessions and memories.

### Safety rules

- IAM is changed with read-modify-write plus etag retries. agentless only adds or removes the members it recorded as
  added itself. Bindings that already existed are reported as "not managed" and are never removed.
- `remove` deletes only what state says agentless created. Existing service accounts, data resources and foundation
  resources stay, and buckets agentless created are kept unless `deletionPolicy: delete` and they're empty.
- The only data resources agentless creates are buckets declared under `resources.buckets`. The plan blocks if any
  other resource you grant roles on doesn't exist. A bucket must have one owner: don't declare one that Terraform
  also manages.
- An engine already deployed by `agents-cli` with the same display name is **adopted** on the first deploy (an
  update, not a duplicate), and is owned from then on.
- Values from `${secret:}` used in `environment` or `build.args` are redacted in plan output and stored in state as
  a short hash. Prefer `agent.secrets`, which keeps the value in Secret Manager entirely.
- State is saved after each resource, and after each IAM target. A failed `remove` keeps state for what's left, so
  rerunning finishes the job.
- Online monitors run as the project's Agent Platform service agent. Anyone who can create online evaluators can
  attach one to any agent of the project, so keep `aiplatform.onlineEvaluators.create` to deployers.
- `--no-wait` records the pending operation in state. Run `deploy --status` to finalize it. Any other deploy is
  blocked until you do.

## Cloud Run target

Set `provider.name: cloud-run` to run the agent as a Cloud Run service instead of a reasoning engine. The agents-cli
project must be scaffolded for Cloud Run (`deployment_target: cloud_run`, the FastAPI server on port 8080).
[`examples/cloud-run.yaml`](examples/cloud-run.yaml) shows every option.

```yaml
provider:
  name: cloud-run
  project: ${param:project}
  region: europe-west1
  stagingBucket: acme-agentless-${stage}   # state, plus the source tarball Cloud Build reads
cloudRun:
  ingress: internal
  vpcEgress: { network: acme-vpc, subnetwork: agents-europe-west1 }
access:
  invokers: [group:support-tools@acme.example.com]
```

**Resources**, applied in this order and destroyed in reverse:

| # | Resource | Owns |
|---|---|---|
| 1 | `serviceAccount`, `buckets`, `iam` | same as on Agent Runtime; the runtime identity is always a service account |
| 2 | `sessionEngine` | a bare reasoning engine (no code) holding sessions and the Memory Bank, when `memory.sessions` is `agentPlatform` (the default) or `memory.memoryBank` is set. The service gets `GOOGLE_CLOUD_AGENT_ENGINE_ID`, and the runtime SA `roles/aiplatform.user` on that engine only (declare model access, e.g. `roles/aiplatform.user` on the project, in `identity.roles` as on Agent Runtime). Use `sessions: inMemory` to skip it |
| 3 | `build` | the image: the packaged source is uploaded to the staging bucket, built by Cloud Build and pushed to `<region>-docker.pkg.dev/<project>/<cloudRun.artifactRegistry>/<service>:<hash>`. The tag hashes the source and build args, so an image already in the registry is never rebuilt. Skipped when `cloudRun.image` is set (built in CI) |
| 4 | `service` | the Cloud Run service, `<service>-<stage>` unless `cloudRun.serviceName`. Its config is diffed against the last-applied spec. Each change is a new revision, and the image is pinned by digest. `APP_URL` is the service's `run.app` URL, which is known before creation. `cloudRun.tags` are bound on the service (see [Tags](#cloud-run-tags)) |
| 5 | `access` | `roles/run.invoker` on the service for `access.invokers` and the Gemini Enterprise service agent |
| 6 | `agentRegistry`, `geminiEnterprise` | the agent card served by the service (Gemini Enterprise registration is `a2a` on Cloud Run) |

**Never public** (unless you opt into [`auth: webhook`](#webhook-mode)). Every caller needs a Google identity that
holds `roles/run.invoker`:

- The service is always deployed with the invoker IAM check on. If someone turns it off, the next deploy turns it
  back on.
- `allUsers` and `allAuthenticatedUsers` are rejected in `access.invokers`.
- If someone grants them on the service outside agentless, `plan` and `deploy` stop until the binding is removed.
- `cloudRun.ingress: internal` limits where requests can come from, on top of IAM, never instead of it.
- `deploy --code-only` keeps every other live setting (IAP included), and a public grant still blocks it.
- ID tokens are only ever sent to `https://*.run.app` URLs.
- Callers on GCP get an ID token from the metadata server (`google.oauth2.id_token.fetch_id_token`).
- agentless's own calls (agent card, `invoke`) always send an ID token: the impersonated deployer's, a service
  account key's, the metadata server's, or the one your `gcloud auth application-default login` refresh returns.
  They never fall back to anonymous requests.

**Public endpoint with a second check (`cloudRun.auth: googleToken`).** For callers outside your network, keep
`ingress: all` and add an app-level check on top of IAM, never instead of it:

```yaml
cloudRun:
  auth: googleToken
  googleToken:
    header: x-google-token                       # default
    allowedPrincipals: [ops@acme.com, domain:acme.com]
    # audiences: [https://agent.acme.com]        # default: the service URL
access:
  invokers: [domain:acme.com]                    # layer 1: roles/run.invoker, still required
```

- **Layer 1, Cloud Run IAM:** the invoker check stays on and `allUsers` is still rejected. Callers send their ID
  token in `X-Serverless-Authorization`, which Cloud Run checks, so `Authorization` stays free for the app.
  Anonymous requests get a 403 before they reach the container.
- **Layer 2, the container:** it gets `AGENTLESS_AUTH_MODE`, `AGENTLESS_TOKEN_HEADER`, `AGENTLESS_TOKEN_AUDIENCES`
  and `AGENTLESS_ALLOWED_PRINCIPALS`, and must verify the Google token in that header. Setting these names in
  `agent.environment` or `agent.secrets` is rejected. agentless can't enforce this layer: if the container doesn't
  verify the token, only layer 1 applies.
- **agentless's own calls** (`invoke`, agent card) send the ID token in both headers, and drop both on a redirect
  to another host. `invoke --app-token` sends another token in the app header. List the deployer in
  `allowedPrincipals`: `plan` warns when the impersonated deployer isn't covered.
- With `gcloud auth application-default login`, your ID token's `aud` is the gcloud OAuth client, not the service
  URL. Impersonate a service account, or add that client to `googleToken.audiences`.
- Gemini Enterprise calls with an ID token only: `plan` warns, and the container has to accept those calls.
- **`x-google-token` reaching the container hasn't been verified live.** Google's front end strips some
  `X-Google-*` headers on other serverless products. Check with a service that echoes its headers, or pick
  another `googleToken.header` (`x-goog-*`, `x-cloud-*`, `x-forwarded-*`, `content-*`, `host` and `cookie` are
  rejected).
- `googleToken` can't be combined with `iap: true` (IAP already checks the caller) or `ingress: internal`.

The check in an agents-cli FastAPI app (`app/fast_api_app.py`, needs `cachecontrol`):

```python
import os

import cachecontrol
import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool

HEADER = os.environ.get("AGENTLESS_TOKEN_HEADER")
AUDIENCES = os.environ.get("AGENTLESS_TOKEN_AUDIENCES", "").split(",")
ALLOWED = set(os.environ.get("AGENTLESS_ALLOWED_PRINCIPALS", "").split(","))
# Google's signing certs are cached between requests instead of fetched on each one
_transport = google_requests.Request(session=cachecontrol.CacheControl(requests.Session()))


def _allowed(claims: dict) -> bool:
    if not claims.get("email_verified"):
        return False
    email = claims.get("email", "")
    if email in ALLOWED:
        return True
    domain = email.rpartition("@")[2]
    # Workspace users: the `hd` claim proves the account belongs to the domain. Service accounts carry no `hd`.
    if claims.get("hd") == domain or domain.endswith(".gserviceaccount.com"):
        return f"domain:{domain}" in ALLOWED
    return False


@app.middleware("http")
async def verify_google_token(request: Request, call_next):
    # The agent card stays behind Cloud Run IAM (layer 1) but needs no app token, so publishing can read it.
    if HEADER and not request.url.path.endswith("/.well-known/agent-card.json"):
        token = request.headers.get(HEADER, "").removeprefix("Bearer ").strip()
        try:
            claims = await run_in_threadpool(id_token.verify_oauth2_token, token, _transport, AUDIENCES)
        except ValueError:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        if not _allowed(claims):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)
```

<a id="webhook-mode"></a>
**Webhook receiver (`cloudRun.auth: webhook`).** GitLab and GitHub webhooks can't send a Google ID token, so they
can't pass Cloud Run IAM. This mode is the one exception to "never public": Cloud Run's invoker IAM check is off, and
the container lets in only requests that carry a shared secret.

```yaml
cloudRun:
  auth: webhook
  webhook:
    header: x-gitlab-token                       # default; GitLab sends the hook's secret token in X-Gitlab-Token
    secret: { secret: gitlab-webhook-token }     # Secret Manager; version defaults to latest
```

- **Cloud Run:** the service is deployed with `invokerIamDisabled: true`. That bypasses IAM without an `allUsers`
  grant, so Domain Restricted Sharing doesn't block it (the `run.managed.requireInvokerIam` org policy does).
  `allUsers` and `allAuthenticatedUsers` are still rejected and still block deploys. `access.invokers` and `publish`
  are rejected: IAM grants nothing here, and Gemini Enterprise and the Agent Registry can't send the secret.
- **The container** gets `AGENTLESS_AUTH_MODE=webhook`, `AGENTLESS_WEBHOOK_HEADER`, and `AGENTLESS_WEBHOOK_SECRET`
  (read from Secret Manager by Cloud Run, never by agentless). The runtime SA gets `secretAccessor` on that secret.
  The container must answer **401 or 403 to every request, on every path, WebSockets included,** unless the header matches. Compare the
  two values in constant time.
- **agentless checks it after every rollout (fail closed).** It calls `GET /` with no credentials, once without the
  header and once with a wrong value. If either answer isn't 401 or 403, it turns the invoker IAM check back on and
  the deploy fails. The next deploy tries again. The service is reachable anonymously only from when the revision
  serves until the check ends, a few seconds.
- **`invoke`** reads the secret from Secret Manager as the deployer (or takes `--app-token`) and sends only that
  header, with no Google token. The header is dropped on a redirect to another host.
- The secret is all that protects the service: rotate it in Secret Manager and in the webhook together. Keep
  `ingress: all` for SaaS senders. A self-hosted GitLab in your VPC can use `ingress: internal` on top.

The check in an agents-cli FastAPI app (`app/fast_api_app.py`). It must be a pure ASGI middleware: an
`@app.middleware("http")` never sees WebSocket connections, so the ADK `/run_live` socket would stay open. Serve
only the paths the sender needs, so a leaked secret can't drive the agent through the ADK API:

```python
import hmac
import os

ALLOWED_PATHS = {"/webhook"}


class WebhookGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or os.environ.get("AGENTLESS_AUTH_MODE") != "webhook":
            return await self.app(scope, receive, send)
        header = os.environ["AGENTLESS_WEBHOOK_HEADER"].encode()
        secret = os.environ.get("AGENTLESS_WEBHOOK_SECRET", "").encode()
        sent = dict(scope.get("headers") or []).get(header, b"")
        ok = bool(secret) and hmac.compare_digest(sent, secret)
        if ok and scope["path"] in ALLOWED_PATHS:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "http.response.start", "status": 401 if not ok else 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})


app.add_middleware(WebhookGate)
```

<a id="cloud-run-tags"></a>
**Tags (`cloudRun.tags`).** Some org policies check a Resource Manager tag on the service itself, for example
`run.allowedIngress` allowing `ingress: all` only on tagged services, or a Domain Restricted Sharing exception.

```yaml
cloudRun:
  ingress: all
  tags:
    "123456789012/allowIngressAllForCloudRun": allow-ingress-all   # <org or project id>/<key>: <value>
```

- **A new service is created closed:** ingress `internal` and the invoker IAM check on, then tagged, then given its
  real settings. A service can only be tagged once it exists, and it is never reachable before its tags are. If
  tagging fails, the service stays closed and the next deploy finishes the job.
- **On an existing service** new tags are bound before the update (a tag may be what allows it), and dropped ones
  are unbound after it. A tag change alone makes no new revision.
- **Only agentless's bindings are removed.** A tag bound outside agentless stays, even when it's also listed.
- **Permissions:** the deployer needs `roles/resourcemanager.tagUser` on each tag value, and the right to bind tags
  on the service (`run.services.createTagBinding`, in `roles/run.admin`).

**Not supported on Cloud Run yet:** `eval`, `evaluation.afterDeploy`, `evaluation.monitors`, `deploy --no-wait` /
`--status`, PSC-I (use `cloudRun.vpcEgress`), `agent.runtime.serverMode`, and `identity.type` other than
`serviceAccount`. Each one fails validation or the command with a clear message.

`logs`, `metrics`, `open` and `invoke` work against the service. `invoke` calls the ADK server's `/run_sse`.
`deployment_metadata.json` records `service_url` and `deployment_target: cloud_run`.

The Artifact Registry repository belongs to your environment's infrastructure-as-code: the plan stops if it
doesn't exist. `remove` deletes the service, its invoker bindings, the session engine (with its sessions and
memories) and the uploaded sources. Built images stay in the registry.

## Commands

| Command | |
|---|---|
| `init [path] --project P` | create `agent.yaml` from `agents-cli-manifest.yaml` |
| `validate` / `print` | check / show the resolved config (`print` masks values from `${secret:}`) |
| `package [-o out.tgz]` | list the files to upload and their hash |
| `plan [--detailed-exitcode]` | diff; exit code 2 when there are changes |
| `deploy [-y] [--force] [--code-only] [--allow-replace] [--no-wait] [--status] [--eval/--no-eval]` | apply, then optionally gate on `agentless eval` |
| `info [--json]` | engine, SA, principal, URLs, GE registration |
| `logs [--since 1h] [--severity WARNING] [--tail]` | Cloud Logging for the engine |
| `metrics [--since 1h] [--json]` | request count, 5xx rate, p50/p95 latency (Cloud Monitoring) |
| `open [console\|logs\|traces] [--print]` | Cloud Console page for the engine |
| `invoke -m "..." [--session ID] [--raw]` | `:streamQuery`, same as `agents-cli run --mode adk` |
| `eval [--dataset F] [--metric name=min] [--json] [-o out.json]` | evaluate the deployed engine; exit 1 below a threshold |
| `remove [-y]` | delete the stage (non-interactive runs need `--yes`) |
| `unlock` | release a stale lock |
| `schema [-o file]` | JSON Schema for `agent.yaml` |

Common options: `-c/--config`, `-s/--stage`, `-p/--param key=value`, `--impersonate-service-account SA_EMAIL` (see
[Deploying as a service account](#deploying-as-a-service-account)).

**Plan colours** follow Terraform: green `+` for what is created or added, yellow `~` for what changes, red `-`
and `±` for what is deleted or replaced. Detail lines (an env var, an IAM binding) take the colour of their marker,
`!` warnings are bold yellow and no-ops are dim. Colours are on in a terminal and in CI (GitLab, GitHub and others
render them in job logs), off with `NO_COLOR`, forced with `FORCE_COLOR`. `agentless --color` / `--no-color`
(before the command) overrides all of these.

Without a TTY (CI), `deploy` applies without asking, like `serverless deploy`. With a TTY it asks unless you pass
`-y`; destructive plans default to "no".

## Permissions

| Principal | Roles |
|---|---|
| Deployer (your user, a CI service account via Workload Identity Federation, or the impersonated `provider.deployer`) | `roles/aiplatform.admin`, `roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`, `roles/resourcemanager.projectIamAdmin`, `roles/storage.objectAdmin` on the state bucket, `roles/secretmanager.admin` (or `setIamPolicy`) on the agent's secrets, `roles/discoveryengine.editor` to publish (`roles/discoveryengine.admin` on the app for `agentGateway`), `roles/agentregistry.editor` for `publish.agentRegistry`, `roles/storage.admin` when `resources.buckets` is used, `roles/monitoring.viewer` for `agentless metrics`, `roles/storage.objectAdmin` on `evaluation.dest` for `agentless eval`. Org-level grants also need org IAM admin. Leave `identity.roles.organization` out until that's approved. |
| Vertex AI service agents (evaluation) | the Evaluation Service writes results to `evaluation.dest` and calls the engine; grant its service agent `roles/storage.objectAdmin` on that bucket if the run reports a permission error |
| Deployer, Cloud Run target | instead of `roles/aiplatform.admin`: `roles/run.admin` (also lets agentless read the agent card), `roles/cloudbuild.builds.editor`, `roles/artifactregistry.reader` on the repository, `roles/storage.objectAdmin` on the staging bucket, `roles/iam.serviceAccountUser` on the runtime SA (and on the build SA). Keep `roles/aiplatform.admin` when a session engine is used |
| Cloud Build service account | `roles/artifactregistry.writer` on the repository, `roles/storage.objectViewer` on the staging bucket, `roles/logging.logWriter` |
| Caller, when impersonating a deployer | `roles/iam.serviceAccountTokenCreator` on the deployer SA (and on each delegate) |
| Runtime SA or Agent Identity principal | what `identity.roles` lists, plus automatic grants: secrets, the artifacts bucket, declared buckets, and with tracing on `cloudtrace.agent`, `logging.logWriter`, `monitoring.metricWriter` |

Shared data resources and API enablement belong to your infrastructure-as-code (for example a Terraform
foundation layer). Buckets owned by a single agent can live in `resources.buckets` instead. agentless reads its outputs through `${tf(...)}` or params.

## Plugins

A plugin is an installed package with an entry point in the `agentless` group:

```toml
[project.entry-points.agentless]
my-policy = "my_agentless_policy"
```

```python
from agentless.hooks import hookimpl

@hookimpl
def agentless_after_load(project):
    if "owner" not in project.config.provider.labels:
        raise ValueError("label 'owner' is mandatory")

@hookimpl
def agentless_variable_sources():
    return {"vault": lambda ctx, arg, key: ...}   # ${vault:key}
```

Hooks: `agentless_variable_sources`, `agentless_after_load`, `agentless_after_package`, `agentless_after_plan`,
`agentless_before_apply`, `agentless_after_deploy`, `agentless_after_remove`. Plugin settings go under `custom:`.

## CI/CD

Deploy with keyless Workload Identity Federation. Replace the `<...>` placeholders with your pool, provider and
deployer service account.

**GitHub Actions:**
```yaml
jobs:
  deploy-dev:
    runs-on: ubuntu-latest
    permissions: { contents: read, id-token: write }
    steps:
      - uses: actions/checkout@v4
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>
          service_account: <deployer-sa>@<project>.iam.gserviceaccount.com
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from git+https://github.com/Aymen2518/agentless@v0.1.0 agentless deploy --stage dev
```

**GitLab CI**, with no gcloud needed. The job writes an `external_account` credentials file that exchanges the
GitLab OIDC token for the deployer service account:
```yaml
deploy:dev:
  image: { name: ghcr.io/Aymen2518/agentless:latest, entrypoint: [""] }
  variables:
    WIF_PROVIDER: projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>
    DEPLOYER_SA: <deployer-sa>@<project>.iam.gserviceaccount.com
    GOOGLE_APPLICATION_CREDENTIALS: /tmp/adc.json
  id_tokens:
    GCP_ID_TOKEN: { aud: "https://iam.googleapis.com/$WIF_PROVIDER" }
  script:
    - echo "$GCP_ID_TOKEN" > /tmp/oidc
    - >
      printf '{"type":"external_account","audience":"//iam.googleapis.com/%s","subject_token_type":"urn:ietf:params:oauth:token-type:jwt","token_url":"https://sts.googleapis.com/v1/token","credential_source":{"file":"/tmp/oidc"},"service_account_impersonation_url":"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/%s:generateAccessToken"}'
      "$WIF_PROVIDER" "$DEPLOYER_SA" > /tmp/adc.json
    - agentless plan --stage dev
    - agentless deploy --stage dev
```

Without a TTY, `deploy` applies without prompting. Use `plan --detailed-exitcode` (exit code 2 means there are
changes) to gate merge requests.

## Container image

The CLI is published as a container image, so CI jobs don't need Python:

```bash
docker run --rm \
  -v "$PWD:/workspace" \
  -v "$HOME/.config/gcloud:/home/agentless/.config/gcloud:ro" \
  ghcr.io/Aymen2518/agentless plan --stage dev
```

- Run it from your agents-cli project directory, which is mounted at `/workspace`.
- Credentials come from the mounted gcloud ADC locally, or from `GOOGLE_APPLICATION_CREDENTIALS` in CI.
- Build it yourself with `docker build -t agentless .`.
- Tagged releases (`v*`) are pushed to `ghcr.io` by `.github/workflows/release.yml`.

## Code layout

| Path | Responsibility |
|---|---|
| `src/agentless/cli.py` | typer commands, confirmation prompts, error-to-exit-code handling, `init` template |
| `src/agentless/config/schema.py` | pydantic model of `agent.yaml` (camelCase aliases, cross-field validation) |
| `src/agentless/config/variables.py` | `${source:key, fallback}` parser and lazy resolver (memoisation, cycle detection, skipped stages) |
| `src/agentless/config/sources.py` | built-in variable sources: `self`, `stage`, `opt`, `param`, `env`, `file`, `secret`, `tf` |
| `src/agentless/config/loader.py` | stage selection → resolve → validate → check against the agents-cli project; tracks secret values for masking |
| `src/agentless/compat/agents_cli.py` | reads `agents-cli-manifest.yaml`, writes `deployment_metadata.json` |
| `src/agentless/package/packager.py` | file selection with agents-cli ignore rules, content hash, reproducible tarball |
| `src/agentless/state/store.py` | `State`, plus GCS and local stores with locks |
| `src/agentless/plan/model.py` | `Action`, `Change`, `ChangeSet`, `DeployOptions`, `Context`, the `Resource` base class |
| `src/agentless/plan/render.py` | coloured plan output |
| `src/agentless/hooks.py` | pluggy hook specs and plugin loading |
| `src/agentless/providers/base.py` | target-agnostic orchestration: plan, deploy (lock → plan → confirm → apply → reapply → cleanup), remove, info |
| `src/agentless/providers/agent_runtime/provider.py` | the Agent Runtime provider: resource list, clients, `--status`, info, metadata |
| `src/agentless/providers/agent_runtime/resources.py` | the resources above |
| `src/agentless/providers/agent_runtime/spec.py` | `agent.yaml` → engine spec, diff, update masks, API payloads, secret redaction |
| `src/agentless/providers/agent_runtime/clients.py` | the only module that calls GCP (Agent Platform SDK, IAM, Resource Manager, Secret Manager, Storage, BigQuery, Discovery Engine REST) |
| `src/agentless/providers/agent_runtime/ops.py` | `logs`, `metrics`, console links and `invoke` |
| `src/agentless/providers/agent_runtime/evals.py` | `eval` and the deploy gate: metric names, evaluation runs, thresholds |
| `src/agentless/providers/cloud_run/` | the Cloud Run target: `provider.py`, `resources.py` (session engine, build, service, invokers), `spec.py` (service body), `clients.py` (Cloud Run Admin v2, Cloud Build, Artifact Registry REST, ID tokens), `ops.py` |
| `tests/fakes.py` | `FakeGcp`, an in-memory stand-in for `clients.py` used by the provider tests |
| `examples/`, `schema/` | reference configs, and the JSON Schema generated by `agentless schema` |

## Extending agentless

- **New `agent.yaml` field:**
  1. Add it to `config/schema.py`.
  2. Map it in `providers/agent_runtime/spec.py` (`desired_spec`, plus `UPDATE_MASKS` / `api_payload` if it's an
     engine field; `IMMUTABLE_FIELDS` if it can't change in place).
  3. Regenerate the schema with `agentless schema -o schema/agent.schema.json`.
  4. Document it in `examples/agent.yaml`.
- **New resource:**
  1. Subclass `plan.model.Resource` (`plan`, `apply`, `plan_destroy`, `destroy`, optionally `cleanup`).
  2. Put any GCP calls behind a method in `clients.py`, and mirror it in `tests/fakes.py`.
  3. Insert the class into `ALL_RESOURCES` at the right position in the dependency order.
- **New variable source:** add it to `build_sources()` in `config/sources.py`, or ship it as a plugin through the
  `agentless_variable_sources` hook.
- **New target:**
  1. Create `providers/<target>/` with a `BaseProvider` subclass, its resources and its clients (subclass
     `GcpClients` to inherit the shared calls). Reuse `ServiceAccountResource`, `BucketsResource` and
     `IamResource`. `providers/cloud_run/` is the worked example.
  2. Widen `Provider.name` in the schema and reject settings the target can't honour in `AgentConfig._target`.
  3. Pick the provider class in `cli._provider`.
- **Organisation policy:** write a plugin with `agentless_after_load` or `agentless_after_plan` that raises to
  reject a config or plan.

## Status and roadmap

**Verified:**
- 245 unit tests: the variable resolver, loader, packager, CLI, and the full provider lifecycle of both targets against
  `FakeGcp` (create, no-op, config-only and code-only updates, replace, adoption, identity switches, partial failures, publish,
  remove).
- ruff and ty are clean.
- Used end to end against real Agent Runtime deployments: create, update, invoke and remove of an agents-cli agent.

**Limits:**
- The Cloud Run target was deployed live once (create, rebuild, update, invoker grant, `invoke` with Agent Engine
  sessions, `logs`, `remove`). Direct IAP (beta), Direct VPC egress, `--code-only`, and the Agent Registry and
  Gemini Enterprise A2A registration of a Cloud Run card haven't been exercised against the live APIs yet.
- Engine drift is detected against the last-applied spec, not by re-reading every live field. Changes made in the
  Console are overwritten on the next deploy of that field.
- The engine code path uses the Agent Platform SDK's private `_create_config` / `_create` / `_update` methods, the
  same calls agents-cli makes. The SDK is pinned `<2`.
- Agent Identity is a preview feature on Agent Runtime.
- Gemini Enterprise publishing, the Agent Registry, the app gateway setting and Memory Bank haven't been exercised
  against the live APIs yet.

**Next:**
1. `eval` and online monitors for Cloud Run agents; live tests of IAP, VPC egress and publishing on Cloud Run.
2. Reusable CI templates (GitHub Action / GitLab component) wrapping `agentless deploy --stage $ENV`.
3. GCS state buckets for shared stages, so CI and colleagues see the same deployment.
4. Optional live drift detection on `plan`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check src
```

**AI skills.** Two [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) under `.claude/skills/`
capture the design decisions and lessons learned, so future work doesn't start from scratch:

| Skill | Use it when |
|---|---|
| [`agentless-development`](.claude/skills/agentless-development/SKILL.md) | changing agentless itself: architecture invariants, how to add fields/resources/targets, testing, gotchas, verified API shapes, Cloud Run notes |
| [`agentless-deploy-agent`](.claude/skills/agentless-deploy-agent/SKILL.md) | onboarding or operating an agent repo with agentless: writing `agent.yaml`, deploy workflow, repo hygiene, troubleshooting |

Claude Code loads them automatically when working in this repo. To use the deploy skill from an agent repo, copy or
symlink it into `~/.claude/skills/`.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and how to add a
resource or field.

## License

[Apache License 2.0](LICENSE).

agentless is an independent project. It isn't affiliated with or endorsed by Google, or by the Serverless
Framework.
