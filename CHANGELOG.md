# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Aymen2518/agentless/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Aymen2518/agentless/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Aymen2518/agentless/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Aymen2518/agentless/releases/tag/v0.1.0
