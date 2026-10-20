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
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
ARG GIT_SHA=dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ORDERS_VERSION=$GIT_SHA
USER 10001
EXPOSE 8000
# Access logs are off: the app writes its own structured request log.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
