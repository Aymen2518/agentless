# Gotchas (each one broke something once)

## Config and variables
- **YAML flow mappings:** `{ name: foo-${stage} }` is a parse error, because YAML reads the `{`. Quote it (`"foo-${stage}"`)
  or use block style. Examples and `init` output must follow this.
- **Quoted fallbacks stay strings:** `${x, 'true'}` gives `"true"`, not `True` (`Expr.quoted`). Unquoted `true`,
  `3` and `null` are coerced.
- **Self references through variables:** `${self:a.b}` must work when `a` is itself `${file(...):}`.
  `Resolver._walk` resolves intermediate strings, so keep it that way.
- **Lone expressions keep their type:** `minInstances: "${param:n, 0}"` gives the int `2`. Embedding a dict or list
  inside a string is an error.
- **Stage check comes before resolution**, or an unknown stage shows up as a confusing "param not defined" error.

## CLI
- **`click.Exit` / `typer.Exit` subclass `RuntimeError`.** `handle_errors` must re-raise them before its generic
  `RuntimeError` branch, or exit codes such as `plan --detailed-exitcode` → 2 get swallowed.
- **Running `uv run` inside `tests/fixtures/sample-agent`** creates a stray `.venv` there, because the fixture has a
  `pyproject.toml`. Always run from the repo root.

## Google clients
- **Every SDK client takes `credentials=`** from `Project.deployer.credentials()` (or `GcpClients._credentials`).
  A client built without it silently uses ADC and bypasses impersonation. `tests/unit/test_impersonation.py`
  scans the source for this, and only `auth.py` may call `google.auth.default`.
- **`provider.deployer` is resolved before anything authenticates** (loader `offline=True`), so `${secret:}` and
  `${tf:}` are rejected there. Those reads themselves need the deployer's credentials.
- **`gcloud config set auth/impersonate_service_account` doesn't reach Python clients.** Only an
  `impersonated_service_account` ADC file or agentless's own setting do.
- **Always pass `project=`** to `storage.Client`, `bigquery.Client` and similar. User ADC often has no default
  project, which gives `OSError: Project was not passed`.
- **The Agent Platform SDK was renamed:** use `agentplatform.Client`, with `vertexai.Client` as the fallback. The
  old name emits a `FutureWarning`.
- **Agent Runtime create and update go through private SDK methods** (`_create_config`, `_create`, `_update`,
  `_get_agent_operation`), exactly as agents-cli does. The SDK is pinned to `google-cloud-aiplatform<2`; re-check
  them on every upgrade.
- **The SDK's public `update()` refuses deployment-spec changes** unless source code is passed. That's why
  agentless builds its own `spec` / `update_mask` payload and only borrows `_create_config` for `source_code_spec`.
- **gRPC prints `ev_poll_posix.cc … FD from fork parent` lines.** They're harmless noise.

## Agent Runtime behaviour
- **`GOOGLE_CLOUD_PROJECT` is reserved** in the engine's env, and the API rejects it. Schema validation blocks it.
- **`APP_URL` (needed for the A2A card) contains the engine id,** so a fresh create is followed by an env-only update.
- **PSC-I, CMEK and identity type are fixed at creation.** A bare Agent Identity shell gets CMEK at creation and PSC
  on its first full update.
- **`engine_find` filters by `display_name`.** Two engines with the same display name block the plan.
- **On Agent Runtime, sessions come from the platform-injected `GOOGLE_CLOUD_AGENT_ENGINE_ID`** (scaffold
  `services.py`). `memory.sessions: inMemory` sets `SESSION_SERVICE_URI=memory://`.

## Packaging
- **`.gcloudignore` replaces `.gitignore` completely** (agents-cli semantics). An agent repo's `.gcloudignore` must
  start with `#!include:.gitignore`, or `config.json` and local reports get uploaded.
- **Excluded from the hash** (so unchanged code doesn't trigger a rebuild): `agent.yaml` and
  `deployment_metadata.json`.
- **pathspec:** use the `"gitignore"` pattern factory. `"gitwildmatch"` is deprecated in pathspec 1.x.

## State and IAM
- **IAM targets that don't exist** (`iam_members` returns `None`) block the plan. agentless never creates data
  resources.
- **BigQuery datasets use legacy role names** (`READER`, `WRITER`, `OWNER`) in their access entries; `_bq_role`
  maps them back.
- **A newly created SA isn't visible to IAM** for a few seconds. `_retry` treats `BadRequest … does not exist` as
  transient.
