# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Delta cloud stage 2: the backend image now carries the Playwright Chromium
# runtime so the in-process bridge driver (services/bridge_in_process.py) can
# drive a real browser without a separate `browser-bridge` service. Chromium
# adds ~1 GB to the image vs python:3.12-slim, the only material trade-off.
#
# Base: Microsoft's official Playwright Python image, pinned to v1.47.0 to
# match the floor in tender-agent/pyproject.toml + browser-bridge's own
# version. Chromium + every Linux .so the headless browser needs ships in the
# base — DO NOT swap to vanilla python:3.12 or you'll spend the afternoon
# chasing libnss / libasound errors.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install dependencies first (the longest-changing layer) so source-only
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
# Runtime: same Playwright base (so Chromium is present) but no compilers,
# no dev headers, no pip cache.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

# curl: container health check curls http://localhost:8000/health
# tini: PID 1 with signal forwarding, so SIGTERM shuts uvicorn cleanly
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
COPY --chown=app:app scripts ./scripts

# Pre-create the data dirs the app writes to and own them as the app user.
# The bridge in-process driver lays persistent contexts under
# /app/data/bridge-sessions/<slug>/ and writes downloads to
# /app/data/bridge-downloads — same paths the Delta adapter's
# `_ingest_bridge_file` reads. In Azure these will be Azure Files mounts so
# cookies + downloads survive a container restart.
RUN mkdir -p \
        /app/data/documents \
        /app/data/bridge-downloads \
        /app/data/bridge-sessions \
    && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "tender_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
