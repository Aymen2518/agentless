## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How it was tested

<!-- Unit tests, a plan/deploy against a sandbox project, screenshots of plan output... -->

## Checklist

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check src tests && uv run ruff format --check src tests` pass
- [ ] `uv run ty check src` passes
- [ ] `schema/agent.schema.json` regenerated (`uv run agentless schema -o schema/agent.schema.json`) if the config model changed
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for user-facing changes
- [ ] Docs / README updated if behaviour changed

## Manual steps

<!-- Anything a maintainer must do outside the code (settings, secrets, cloud setup). Write "None" if nothing. -->
