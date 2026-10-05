# agentless — v1 implementation


## Phase 1 — Core
- [x] pyproject, layout, branch `feat/initial-framework`
- [x] pydantic schema (`config/schema.py`)
- [x] variable resolver + sources (`config/variables.py`, `config/sources.py`)
- [x] loader (stage selection → resolve → validate); only selected stage resolved
- [x] agents-cli compat (manifest read, deployment_metadata.json write)
- [x] packager (agents-cli ignore rules, sha256, agent.yaml/metadata excluded from hash)
- [x] `init` / `validate` / `print` (secret masking) / `package`

## Phase 2 — State & plan
- [x] GCS state store + generation-match lock (+ local store)
- [x] Resource ABC, ChangeSet, renderer
- [x] `plan` (+ `--detailed-exitcode`)

## Phase 3 — Deploy / remove
- [x] service account resource (create / existing / rename with deferred delete)
- [x] IAM bindings (project/org/folder/bucket/dataset/secret/SA), owned-member tracking, missing-target guard
- [x] engine resource (create/update with masks, code-only, adoption, LRO wait, `--no-wait`/`--status`)
- [x] `deploy` / `info` / `remove` / `unlock`

## Phase 4 — Extras
- [x] Memory Bank (context_spec)
- [x] PSC-I + replace guard (also CMEK, identity type)
- [x] agentIdentity (bare engine → IAM → code)
- [x] Gemini Enterprise publish (+ OAuth authorization with scopes)

## Phase 5 — Ops
- [x] `logs`, `invoke`
- [x] pluggy hooks
- [x] JSON Schema export (`schema/agent.schema.json`)

## Phase 6 — Pilot
- [x] pilot agent config; offline validate/package + read-only plan against a dev project
- [ ] first real dev deploy (needs user go-ahead; reports bucket must exist first)
- [ ] reusable CI templates (GitHub Action / GitLab component); README has job sketches

## Review
- 64 unit tests (variables, loader, packager, provider lifecycle against an in-memory GCP fake, CLI); ruff + ty clean.
- Real read-only `plan` on a dev project with a pilot agent: reads GCS state, SA and IAM, and lists engines.
  It correctly blocks on a missing reports bucket. A later sandbox deploy of the pilot succeeded.
- Found and fixed during verification: non-selected stages were resolved (prod env vars needed for dev), offline
  commands built GCP clients eagerly, storage/BQ clients lacked an explicit project, `typer.Exit` swallowed by the
  error handler, unquoted `${}` in YAML flow mappings (documented).
- Independent review found 12 issues in the multi-step paths (replace, recreate, rename, partial failure), all fixed
  with a regression test each:
  - switching to agentIdentity no longer orphans the old engine
  - IAM re-runs when the principal changes, and GE re-runs when the engine is recreated
  - IAM state is saved per target, and a partial `remove` keeps state
  - `${secret:}` values are redacted in plan output and state
  - a GE authorization is deleted when removed or renamed, and kept when only the app moves
  - PSC and CMEK are sent on the first deploy of a bare or adopted engine
  - `--code-only` defers SA and IAM changes
  - unset server mode is reset
  - quoted fallbacks stay strings, and `${self:a.b}` works when `a` is itself a variable
  - engine deletes wait for completion
  - an orphaned bare identity engine is reused
- Not yet verified against live write APIs: engine create/update payloads, GE registration, IAM writes.

## Open-source readiness
- [x] remove organisation-specific references (projects, naming, internal repos) from code, docs, examples, skills
- [x] Apache-2.0 LICENSE, pyproject metadata, CONTRIBUTING.md
- [x] CLI container image (Dockerfile) + GitHub workflows (CI, release to ghcr.io)
- [ ] create the GitHub repo, replace `OWNER` placeholders, tag `v0.1.0`
