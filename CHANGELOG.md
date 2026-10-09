# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Aymen2518/agentless/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Aymen2518/agentless/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Aymen2518/agentless/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Aymen2518/agentless/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Aymen2518/agentless/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Aymen2518/agentless/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Aymen2518/agentless/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Aymen2518/agentless/releases/tag/v0.1.0
