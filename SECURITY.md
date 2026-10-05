# Security policy

## Supported versions

Only the latest [GitHub Release](https://github.com/Aymen2518/agentless/releases) of `agentless-cli` (and the matching `ghcr.io/Aymen2518/agentless` image) receives
security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
[GitHub security advisories](https://github.com/Aymen2518/agentless/security/advisories/new).

Include the affected version, a description of the issue, steps to reproduce, and its impact. You should get a first
response within 7 days. Once a fix is released, the advisory is published with credit to the reporter unless you
ask otherwise.

## Scope

agentless runs with the caller's Google Cloud credentials and changes IAM, service accounts and Agent Runtime
resources. Issues that are especially relevant:

- secret values (`${secret:...}`) leaking into plan output, logs, state files or the source package
- IAM grants broader than what `agent.yaml` declares, or grants left behind by `remove`
- state or lock handling that lets two deploys corrupt each other
- the container image or release artifacts not matching the tagged source

Misconfigurations in your own `agent.yaml` or GCP project are out of scope.
