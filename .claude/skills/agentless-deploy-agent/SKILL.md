---
name: agentless-deploy-agent
description: Use when deploying or operating an ADK agent (agents-cli project) on Google Cloud Agent Platform with agentless — writing or editing agent.yaml, choosing identity/roles/secrets, running agentless plan/deploy/invoke/logs/remove, migrating an agent repo off agents-cli deploy + per-agent Terraform, or troubleshooting a blocked plan, lock, or pending operation. Not for changing agentless itself (use agentless-development).
---

# Deploying an agent with agentless

agentless reads `agent.yaml` next to an agents-cli project and reconciles GCP with it: the service account, IAM, the
Agent Runtime engine, Memory Bank, PSC-I and Gemini Enterprise. agents-cli is still the tool for building and
evaluating the agent. Field reference: `examples/agent.yaml` in the agentless repo. Smallest
starting point: `examples/minimal.yaml`.

Check the tool is installed with `agentless version`. If it's missing, run `uv tool install agentless-cli`
(or `pipx install agentless-cli`, or use the image `docker run --rm ghcr.io/Aymen2518/agentless`).

## Workflow

1. **Preconditions**
   - An agents-cli project: `agents-cli-manifest.yaml` with `deployment_target: agent_runtime`, a `Dockerfile`,
     and `app/`.
   - ADC: `gcloud auth application-default login` locally, or WIF in CI.
   - APIs enabled in the target project: `aiplatform`, `iam`, `cloudresourcemanager`, `storage`, `logging`,
     `cloudtrace`, `serviceusage`, `secretmanager` (if you use secrets), plus whatever the agent itself calls.
   - Data resources the agent uses (buckets, datasets, secrets) **already exist**. agentless grants access to them
     but never creates them, and the plan blocks if one is missing.
2. **Write `agent.yaml`.** Run `agentless init --project <dev-project>`, or copy `examples/agent.yaml` and adapt it:
   - **`stages.<stage>.params.project`:** one GCP project per stage. Try a sandbox project before shared ones.
   - **`identity`:** prefer `type: serviceAccount` with `create: true` and a name like `sa-<agent>-${stage}`
     (6–30 characters). Use `agentIdentity` only if you need a per-agent principal; it's a preview feature.
   - **`identity.roles`:** least privilege. Grant on the resource (`resources:`), not the whole project, where you
     can. Org-level roles need org-admin approval, so keep them commented out until approved.
   - **Secrets:** put them under `agent.secrets` (Secret Manager refs; `secretAccessor` is granted automatically).
     Don't use `${secret:}` inside `environment`.
   - **Quoting:** any `${...}` inside a one-line `{ ... }` must be quoted.
3. **Check offline:** `agentless validate`, then `agentless package` (look at the uploaded file list).
4. **Plan, which is read-only:** `agentless plan --stage dev`. Fix anything marked ✋ before deploying.
5. **Deploy:** `agentless deploy --stage dev` (it shows the plan and asks). It takes 5–10 minutes on first create.
6. **Use it:** `agentless info`, `agentless invoke -m "…"`, `agentless logs --since 30m --tail`.
7. **Tear down:** `agentless remove --stage dev` deletes only what agentless created. Buckets and existing SAs stay.

Always show the user the plan and get an explicit go-ahead before running `deploy` or `remove` on their behalf.

## State: local or shared
- **No `provider.stagingBucket`:** state lives in `.agentless/<stage>/state.json` next to `agent.yaml`. That's fine
  for a sandbox, but only that machine knows about the deployment. Don't delete that folder, or `remove` loses
  track.
- **For shared or CI stages,** set `stagingBucket` (per stage, through params) to a GCS bucket. State and the lock
  then live under `gs://<bucket>/agentless/<service>/<stage>/`.

## Repo hygiene when migrating an agent repo
(Learned migrating a real agents-cli agent.)
- **Delete the agents-cli-generated `deployment/terraform/`** (per-agent SA, IAM and engine). agentless owns those
  now. Move anything else it held, such as a telemetry log sink or buckets, to the foundation Terraform first.
- **Add `.gcloudignore`.** It must begin with `#!include:.gitignore`, then exclude `.claude/`, `.vscode/`, `docs/`,
  `tasks/`, `tests/`, `*.md` (with `!README.md` if the Dockerfile copies it), `.env.example` and
  `deployment_metadata.json`. This keeps internal docs out of the upload and stops edits to docs or tests from
  triggering a rebuild.
- **Git-ignore `.agentless/` and `deployment_metadata.json`.** Both are local output.
- **Rewrite `DEPLOYMENT.md`** around `agentless plan` / `deploy`. Mark `.env` / `.env.example` as local-only
  (deployed values come from `agent.yaml`).
- **Keep `agents-cli-manifest.yaml`.** agentless reads it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| ✋ `target resources do not exist: bucket/…` | create the bucket, dataset or secret first (Terraform foundation or gcloud), or drop the grant |
| ✋ `… cannot change in place; rerun with --allow-replace` | PSC-I, CMEK and identity type are fixed at creation. `--allow-replace` deletes and recreates the engine, and sessions and memories are lost |
| ✋ `operation … in progress` | a `--no-wait` or interrupted deploy: run `agentless deploy --status` |
| `… is locked by …` | another deploy is running; if it's stale, run `agentless unlock` |
| ✋ `N engines share this display name` | set `agent.displayName` to something unique |
| `variable error at stages.<x>…` | an env var or param that the chosen stage needs is missing; other stages are never resolved |
| 403 during apply | the deployer is missing roles (see the README "Permissions" table); state is kept, so fix it and rerun |
| `partial remove, still tracked: …` | some revokes or deletes failed; fix permissions and rerun `remove` |
| plan shows `~ source … (rebuild)` with no code change | the `.gcloudignore` or packaged file set changed; that's expected once |

An existing engine deployed with `agents-cli deploy` under the same display name is adopted (updated in place, not
duplicated) on the first agentless deploy.
