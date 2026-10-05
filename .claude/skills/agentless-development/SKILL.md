---
name: agentless-development
description: Use when changing the agentless codebase itself (the Serverless-style deploy CLI for ADK agents on Google Cloud Agent Platform), e.g. adding an agent.yaml field, a resource, a variable source, a deployment target such as Cloud Run, fixing a deploy bug, or reviewing agentless changes. Covers architecture invariants, where code goes, how to test with FakeGcp, the verification checklist, and known gotchas. Not for deploying an agent with agentless (use agentless-deploy-agent).
---

# agentless development

agentless turns `agent.yaml` into GCP resources: a service account, IAM bindings, a reasoning engine and a Gemini
Enterprise registration. It works plan-then-apply against a JSON state object. Read `README.md` ("How it works",
"Code layout") first. This skill covers what the README doesn't: the rules the code relies on, and how to change it
safely.

## Invariants (keep them true)

1. **State is saved incrementally.** Every resource calls `self._set_state(ctx, …)` (which runs `ctx.save()`)
   right after a GCP mutation. `IamResource` saves after *each target*. A crash at any point must leave state that
   names everything that exists.
2. **IAM only touches what agentless added.** Owned bindings are `(type, name, role, member)` tuples in state.
   Bindings that already existed are "foreign": reported, never recorded, never revoked. Never replace a whole
   policy; always read-modify-write with etag retries (`clients.iam_modify`).
3. **Order matters:** `serviceAccount → agentIdentity → iam → engine → geminiEnterprise` (`ALL_RESOURCES`). Deletions
   that must wait (an old SA, an old GE registration, a superseded engine) go in `change.data["cleanup"]` or
   `state["previous"]` and run in `cleanup()` in reverse order, after every apply. A SA is never deleted while it
   still has bindings.
4. **Plan is a preview; apply recomputes.** `apply()` re-derives its target from config and current state. A
   principal or engine name may only exist mid-run (Agent Identity), so apply can't rely on plan-time values.
   Cross-resource follow-ups use `ctx.reapply.add("<key>")`. The provider re-applies those after the forward pass
   (for example `iam` when the principal changes, `geminiEnterprise` when the engine is recreated).
5. **Deploy and remove hold the lock across plan and apply** (`AgentRuntimeProvider.deploy` / `remove`). `plan`
   alone is read-only and takes no lock.
6. **Secrets never reach output or state.** Values resolved through `${secret:}` are collected in
   `Project.sensitive`. `spec.redact()` hashes them in stored specs and plan diffs. The GE OAuth secret is only ever
   stored as a hash. `print` masks them.
7. **Only the selected stage is resolved.** `Resolver(skip=…)` leaves other `stages.*` untouched, so dev never needs
   prod's env vars.
8. **Offline commands never authenticate.** `validate`, `print`, `package`, `schema` and `init` must not build GCP
   clients. `provider.clients` and `provider.store` are lazy `cached_property`s; keep new clients lazy too.
9. **Every GCP call lives in `providers/<target>/clients.py`** and has a twin in `tests/fakes.py`. Resources never
   import Google SDKs.
10. **Engine updates are minimal.** Config fields go out with an `update_mask` built by `spec.api_payload`. Code
    (`source_code_spec`) is only sent when the source hash or the build args change. Fields fixed at creation
    (`IMMUTABLE_FIELDS`) force a blocked REPLACE unless `--allow-replace`. On a never-deployed engine (bare or
    adopted, `spec is None`) they're sent with the first update.

## Where changes go

| Change | Files |
|---|---|
| new YAML field | `config/schema.py` → `providers/agent_runtime/spec.py` (`desired_spec`, `UPDATE_MASKS`, `api_payload`, maybe `IMMUTABLE_FIELDS`) → `agentless schema -o schema/agent.schema.json` → `examples/agent.yaml` → README "Blocks" table |
| new resource | subclass `plan.model.Resource` in `resources.py`, insert it into `ALL_RESOURCES`, add client methods and fakes |
| new variable source | `config/sources.py::build_sources` (or the plugin hook `agentless_variable_sources`) |
| new CLI command/flag | `cli.py` (wrap with `@handle_errors`; offline commands must not touch `provider.clients`) |
| new target (Cloud Run…) | `providers/<target>/`, widen `Provider.name`, dispatch in `cli._provider`. See `references/cloud-run-target.md` |

## Testing

- `tests/unit/test_provider.py` drives the whole lifecycle through `make_provider()` (conftest), `FakeGcp` and a
  `LocalStateStore` in a temp copy of `tests/fixtures/sample-agent`. Edit the fixture YAML with the `edit` fixture.
- **The fake must fail like the real API**, or tests hide bugs. It raises on `engine_update` for missing engines,
  mints principals for `AGENT_IDENTITY` creates, and returns `None` from `iam_members` for missing targets. When you
  add a client method, make the fake enforce the same preconditions.
- Write at least one test per multi-step path: rename or replace, recreate after an external delete, a partial
  failure mid-apply (make a fake method raise once, then rerun), and `--code-only` / `--no-wait` interactions.
- Assert on recorded calls (`gcp.calls`, `gcp.kinds()`) for ordering, and on `update_mask` strings for minimal
  updates.

## Verification checklist (before calling a change done)

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check src
uv run pytest -q
```

Then, for anything touching GCP calls:

1. Run a **read-only** `agentless plan` against a real sandbox project, using an agents-cli pilot agent. `plan`
   only does GETs.
2. Get an independent review focused on multi-step paths. The first review found 12 bugs the fake had hidden.
3. Never run `deploy` or `remove` against a shared project without the user's explicit go-ahead.

## References

- `references/gotchas.md`: things that already broke once.
- `references/sdk-api-notes.md`: the request shapes, update masks and endpoints that were verified.
- `references/cloud-run-target.md`: the paused Cloud Run design (research done, decisions open).
