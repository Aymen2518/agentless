# Contributing to agentless

Thanks for helping. Bug reports, docs fixes and new features are all welcome.

## Setup

```bash
uv sync
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check src
```

The test suite needs no Google Cloud credentials. Provider tests run against `tests/fakes.py` (`FakeGcp`), an
in-memory stand-in for every GCP call.

## Before opening a pull request

- **Tests:** add or extend a test for the behaviour you change. Anything that touches several resources (rename,
  replace, recreate, partial failure) needs its own lifecycle test in `tests/unit/test_provider.py`.
- **Keep the fake honest:** if you add a method to `providers/agent_runtime/clients.py`, add its twin to
  `tests/fakes.py`, and make the fake fail where the real API would.
- **New `agent.yaml` fields:** update `examples/agent.yaml` and regenerate the schema with
  `uv run agentless schema -o schema/agent.schema.json`.
- **Real GCP:** never run `deploy` or `remove` in CI or against shared projects as part of a test. A read-only
  `agentless plan` against your own sandbox project is the recommended manual check.

## Where things live

`README.md` ("Code layout", "Extending agentless") maps each module and gives the steps for adding a field, a
resource, a variable source or a target. Design rules and known pitfalls are in
`.claude/skills/agentless-development/` (readable as plain Markdown, and loaded automatically by Claude Code).

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
