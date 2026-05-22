# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: build a wheel + install dependencies into a clean virtualenv.
# Anything we install during the build (build-essential, dev headers, pip
# cache) stays in this stage and never touches the runtime image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install dependencies first (the longest-changing layer) so that source-only
# rebuilds reuse this cached layer.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel

# Copy the source last so source-only edits don't bust the dependency layer.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

RUN /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
# Stage 2: minimal runtime. No compilers, no dev headers, no pip cache.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

# curl: ECS container health check curls http://localhost:8000/health
# tini: PID 1 with signal forwarding, so SIGTERM from ECS shuts uvicorn cleanly
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app src ./src
# Operator scripts (backfill_portal_discovery.py et al.) must ship in the image
# so they're runnable via `docker compose exec app python scripts/<name>.py`
# without a manual `docker cp`.
COPY --chown=app:app scripts ./scripts

# Pre-create the data dirs the app writes to (documents + bridge downloads) and
# own them as the app user. docker-compose bind-mounts the host folders over
# these subdirs at runtime; pre-creating /app/data app-owned means /app/data is
# never left root-owned (which is what made the old relative 'data' write fail
# with PermissionError) and writes succeed whether or not the mounts are present.
RUN mkdir -p /app/data/documents /app/data/bridge-downloads \
    && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "tender_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
