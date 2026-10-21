# Stage 1: resolve and install dependencies into a virtualenv.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Stage 2: small runtime image with only the virtualenv and the app code.
FROM python:3.12-slim
# Links the GHCR package to this repository (and its public visibility).
LABEL org.opencontainers.image.source="https://github.com/glichy-gem/kubesentinel"
# git and ssh let the agent commit fixes to the GitOps repo with its deploy key.
# Versions follow the pinned base image's Debian release; Trivy scans the result.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
ARG GIT_SHA=dev
# SQLite lives on a mounted volume at /data; the root filesystem stays read-only.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 KS_VERSION=$GIT_SHA KS_DB_PATH=/data/kubesentinel.db
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
