# Releasing

Releases are published on GitHub only:

- **GitHub Release:** wheel, sdist, `agent.schema.json` and `SHA256SUMS`, with build provenance attestations.
- **GHCR image:** `ghcr.io/aymen2518/agentless`, multi-arch (amd64 and arm64), with SBOM and provenance, signed
  with cosign.

Everything is driven by [`.github/workflows/release.yml`](../.github/workflows/release.yml).

## What gets published when

| Trigger | GitHub Release | Image tags |
|---|---|---|
| push to `main` | none | `edge`, `sha-<short>` |
| tag `vX.Y.ZaN`, `vX.Y.ZbN`, `vX.Y.ZrcN` | pre-release | `X.Y.ZrcN` only |
| tag `vX.Y.Z` | release, marked latest | `X.Y.Z`, `X.Y`, `X`, `latest` |

A tag must equal `v` + `agentless.__version__` exactly (PEP 440, for example `v0.2.0rc1` and not `v0.2.0-rc1`).
Otherwise the `verify` job fails before anything is published.

## Steps

1. **Bump the version** in `src/agentless/__init__.py` (`__version__`). It is the only place the version lives;
   `pyproject.toml` reads it. For a release candidate, use `X.Y.Zrc1`.
2. **Update `CHANGELOG.md`.** Move the `[Unreleased]` entries into a new `## [X.Y.Z] - YYYY-MM-DD` section and add the
   compare link at the bottom. The release notes are taken from this section. A pre-release such as `X.Y.Zrc1` uses
   its own `## [X.Y.Zrc1]` section if there is one, otherwise the `## [X.Y.Z]` section.
3. **Open a PR, get CI green, merge** to `main`.
4. **Tag the merged commit and push the tag:**

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "agentless X.Y.Z"      # or vX.Y.Zrc1
   git push origin vX.Y.Z
   ```

5. **Watch the run:** `gh run watch -R Aymen2518/agentless`. A failed run can be re-run; the release step uploads
   with `--clobber` if the release already exists.
6. **After a release candidate**, check it (below), then repeat from step 1 with `__version__ = "X.Y.Z"`.

## Check a release

```bash
# CLI from the release
uv tool install git+https://github.com/Aymen2518/agentless@vX.Y.Z
agentless version

# wheel provenance
gh release download vX.Y.Z -R Aymen2518/agentless -p '*.whl' -p SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing          # macOS: shasum -a 256 -c SHA256SUMS --ignore-missing
gh attestation verify agentless_cli-X.Y.Z-py3-none-any.whl -R Aymen2518/agentless

# image on both architectures, and its signature
docker run --rm --platform linux/amd64 ghcr.io/aymen2518/agentless:X.Y.Z version
docker run --rm --platform linux/arm64 ghcr.io/aymen2518/agentless:X.Y.Z version
cosign verify ghcr.io/aymen2518/agentless:X.Y.Z \
  --certificate-identity-regexp '^https://github.com/Aymen2518/agentless/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## One-time setup

- After the first image push, open the package settings on GitHub (Profile → Packages → `agentless` → Package
  settings), set visibility to **Public**, and check that the `agentless` repository has access.

## If a release goes wrong

- **Tag on the wrong commit, nothing published yet:** `git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`, then
  tag again.
- **Already published:** don't reuse the version. Delete or mark the GitHub Release as broken if needed, bump to
  the next patch version, and release again. Image tags `X.Y`, `X` and `latest` move with the new release.
