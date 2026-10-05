# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-05

First public release, published on PyPI as `agentless-cli` and as the image `ghcr.io/Aymen2518/agentless`.

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

[Unreleased]: https://github.com/Aymen2518/agentless/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Aymen2518/agentless/releases/tag/v0.1.0
