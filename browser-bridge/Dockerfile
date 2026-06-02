# Browser bridge — Linux headless container (Delta cloud, stage 1)
#
# Stage 1 ONLY packages the existing bridge for Linux/headless. No code logic,
# FastAPI surface, Delta adapter, orchestrator or human-pause gate is changed.
# Deployment (Azure) lands in stage 2; the operator-exported storage_state.json
# upload path (login model B) lands in stage 3.
#
# Base image: Microsoft's official Playwright Python image. We pin to v1.47.0
# to match the floor declared in browser-bridge/pyproject.toml. The image ships
# Chromium + every Linux .so the headless browser needs, and tracks Playwright
# upstream — DO NOT replace it with a vanilla python:3.12 base or you'll spend
# the rest of the afternoon chasing libnss / libasound errors.
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

# Bridge code lives at /app. /data is the writable persistent area for sessions
# (Playwright `user_data_dir`) and downloads. In Azure these will be bind-
# mounted to Azure Files; locally they're whatever volume the operator mounts.
WORKDIR /app
RUN mkdir -p /data/bridge-sessions /data/bridge-downloads

# Install the bridge as a package via its own pyproject.toml. We copy the
# project file first so the (slow) dependency layer caches independent of the
# (fast-changing) source.
COPY pyproject.toml ./
COPY bridge ./bridge
RUN pip install --no-cache-dir .

# Container defaults — every one of these can be overridden at runtime.
# TENDER_AGENT_BRIDGE_TOKEN is intentionally NOT set here: it's a secret, must
# come from the deployment environment / Key Vault and MUST match the backend.
ENV TENDER_AGENT_BRIDGE_HEADLESS=true \
    TENDER_AGENT_BRIDGE_STATE_DIR=/data/bridge-sessions \
    TENDER_AGENT_BRIDGE_DOWNLOAD_DIR=/data/bridge-downloads \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

# uvicorn with no reload and a single worker — the Playwright SessionManager
# holds in-process state (browser contexts), so multi-worker would break
# session affinity.
CMD ["uvicorn", "bridge.server:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]

# ---------------------------------------------------------------------------
# Notes for stage 3 (NOT built here):
# To prime a Delta session in cloud, an operator will export the headed
# session's storage_state from their local browser via Playwright and copy it
# into the slug's user_data_dir on the mounted /data volume:
#   /data/bridge-sessions/delta/storage_state.json
# That route is documented but not wired in this stage — the bridge still
# accepts /session/open against an empty dir and will simply present a login.
# ---------------------------------------------------------------------------
