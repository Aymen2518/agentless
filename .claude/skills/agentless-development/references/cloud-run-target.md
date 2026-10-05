# Cloud Run target: paused design notes (2026-10-04)

Status: the research is done. The user paused before choosing an approach, so **ask about these decisions before
building**.

## Open decisions
1. **How to build and ship the container:**
   - **A (recommended):** the Cloud Build API (upload the source tarball to the staging bucket, build, push to
     Artifact Registry), then the Cloud Run Admin API (`run_v2`) to create or update the service. Also allow an
     `image:` field for images built in CI (kaniko via `cicd-catalog/cloud-run/docker/build@1`). No gcloud
     dependency, and it fits the plan/diff/state model.
   - **B:** shell out to `gcloud run deploy --source .`, like agents-cli does. Least code, but
     needs gcloud, and diffs are coarse.
   - **C:** pre-built images only.
2. **Sessions and Memory Bank on Cloud Run:** should agentless create a companion bare Agent Engine (no code) when
   `memory` is configured, and inject `SESSION_SERVICE_URI=agentengine://<engine>`? The alternative is in-memory
   sessions, or a URI the user supplies.

## What agents-cli does (`deploy/cmd_deploy.py`, around lines 646–757)
- **Command:** `gcloud run deploy <svc> --source .` (or `--image`), with `--no-allow-unauthenticated`,
  `--no-cpu-throttling` and `--labels created-by=adk`.
- **Sizing defaults on create:** `--memory 4Gi --cpu 1 --min-instances 1 --max-instances 10 --concurrency 8`.
- **Optional flags:** `--port`, `--iap`, `--service-account`, `--async`.
- **Env vars:** `.env` plus overrides, `AGENT_VERSION`, `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`, and
  `APP_URL=https://{svc}-{projectNumber}.{region}.run.app`.
- **Secrets:** `--update-secrets ENV=SECRET:VER`.
- **Not supported:** no ingress, VPC or timeout flags. It writes nothing to `deployment_metadata.json`.
- **Create vs update:** a GET on `https://{region}-run.googleapis.com/v2/projects/{p}/locations/{r}/services/{svc}`;
  a 404 means create.

## Container contract (scaffold)
- Port 8080, run with `uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080`.
- A2A is served at `/a2a/<app>`, with the card at `/a2a/<app>/.well-known/agent-card.json`. The card URL comes
  from `APP_URL`.
- **Sessions:** `SESSION_SERVICE_URI`, then `GOOGLE_CLOUD_AGENT_ENGINE_ID` (with `GOOGLE_CLOUD_PROJECT` and
  `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION`), otherwise in-memory.
- **Artifacts:** `LOGS_BUCKET_NAME`.
- **The Cloud Run Terraform scaffold** also sets the `OTEL_*` env vars, `session_affinity = true`, and SA roles
  `aiplatform.user`, `logging.logWriter`, `cloudtrace.agent`, `storage.admin` and `serviceusage.serviceUsageConsumer`.

## Gemini Enterprise and callers
- **Cloud Run means A2A registration only:** `a2aAgentDefinition.jsonAgentCard`, matched on the card's `url`. The
  card is fetched with an ID token (audience = URL origin).
- **`run.invoker`:** GE (its service agent) and any other caller need `run.invoker` on the service. Neither
  agents-cli nor its Terraform grants it, so agentless should (for example a `cloudRun.invokers:` list).
- **`agents-cli run --url` against Cloud Run** uses an ID token from IAM `generateIdToken` or
  `gcloud auth print-identity-token`.

## Public webhook services
- Some agents are called by systems that can't present a Google identity, such as a Git hosting webhook. They have
  to be public (`--allow-unauthenticated`), and the auth boundary becomes an app-level shared secret (for example
  a webhook token header). So agentless must support `allowUnauthenticated: true`.
- Serve health checks on `/health` or `/`, not `/healthz`, which Cloud Run reserves.
- A `.gcloudignore` keeps tests and reports out of the source upload.
- CI often builds the image with kaniko or Cloud Build and pushes it to Artifact Registry. That's the case an
  `image:` field covers.

## Suggested shape (if option A is chosen)
- `provider.name: cloud-run`, plus a `cloudRun:` block:
  - `ingress` (`all`, `internal`, `internal-and-cloud-load-balancing`)
  - `allowUnauthenticated`, `invokers: [members]`, `iap`
  - `vpcEgress` (Direct VPC: network/subnet, egress)
  - `timeout`, `cpuThrottling`, `sessionAffinity`, `port`, `artifactRegistry` (repo), `image` (skip the build)
- Resources: `serviceAccount → iam → [build] → service → invokerIam → [companion engine] → geminiEnterprise
  (A2A)`. Reuse `ServiceAccountResource` and `IamResource` unchanged.
- Fields fixed at creation: none for Cloud Run (it creates revisions). Diff the service template the same way
  `spec.py` does, and write the service URL to state and to `deployment_metadata.json` (new key `service_url`).
