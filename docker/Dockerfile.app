# syntax=docker/dockerfile:1.7
# fabloom-app: the web demo (`fabloom serve`) with the local Batfish backend's client.
# Runs behind Caddy in docker-compose.yml; Batfish itself is a separate service.

# --- stage 1: resolve the locked environment ------------------------------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so a source change does not reinstall them. The uv cache is a build
# mount, never a layer: it would otherwise ship stale copies of every wheel.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra batfish --extra web
COPY src ./src
COPY conf ./conf
COPY benchmark ./benchmark
COPY gallery ./gallery
# Editable install, so REPO_ROOT resolves to /app and conf/, benchmark/ and gallery/ are found.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra batfish --extra web

# --- stage 2: runtime, without uv ------------------------------------------------------------
# Same base and path as the build stage, so the venv's interpreter links stay valid.
FROM python:3.12-slim
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/vkrishnabhatk/fabloom" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="Fabloom web demo"
WORKDIR /app
COPY --from=build /app /app
RUN useradd --uid 10001 --no-create-home --home-dir /app fabloom \
 && mkdir -p /data/runs /data/cache && chown -R fabloom /data
USER 10001
ENV PATH=/app/.venv/bin:$PATH \
    IMAGE_TAG=git-${GIT_SHA} \
    FABLOOM_RUNS_DIR=/data/runs \
    FABLOOM_GROUNDING_CACHE_DIR=/data/cache/grounding
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["fabloom", "serve", "--host", "0.0.0.0", "--port", "8000"]
