# agentless CLI image: docker run --rm -v "$PWD:/workspace" ghcr.io/Aymen2518/agentless plan --stage dev
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="agentless" \
      org.opencontainers.image.description="Serverless-style deployments for ADK agents on Google Cloud Agent Platform" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/Aymen2518/agentless"
RUN useradd --create-home --uid 10001 agentless
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER agentless
WORKDIR /workspace
ENTRYPOINT ["agentless"]
CMD ["--help"]
