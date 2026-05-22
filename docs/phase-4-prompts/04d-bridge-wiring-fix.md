Task: fix the backend↔bridge wiring so the document-fetch flow runs end-to-end. Three real first-run issues were found during live testing. Fix them properly in code + config, with a self-diagnosing startup check. Autonomous run.

This is prompt 4d — a wiring/config fix after live testing the Delta flow. Chunks 1-4 + 4b + 4c are merged on main. The Delta adapter and browser bridge are built and individually working (bridge health check passes; adapter logic + tests pass). The gap is the plumbing between the Docker backend and the native-Windows bridge.

Required reading first:
- docker-compose.yml (the app service definition + volumes)
- tender-agent/.env and tender-agent/.env.example
- tender_agent/services/portal_orchestrator.py (where documents get written)
- tender_agent/services/bridge_client.py (how it reads the bridge URL + token)
- tender_agent/services/portals/adapters/delta_esourcing.py (download_documents — where files are staged/written)
- Any document-storage helper (search for DOCUMENT_STORAGE_DIR usage and any os.makedirs / open(... 'wb') / Path('data') references)

================================================================
THE THREE ISSUES FOUND IN LIVE TESTING
================================================================

ISSUE 1 — PermissionError: [Errno 13] Permission denied: 'data'
The fetch fails instantly when writing documents. The error references a RELATIVE path 'data', not the configured DOCUMENT_STORAGE_DIR (/var/tender-agent/documents). So somewhere in the download/staging code, a path is being built relative to the current working directory (bare 'data' or 'data/...') instead of from DOCUMENT_STORAGE_DIR (or the bridge download dir). Find every place a document or staging file path is constructed and make them ALL derive from configured, absolute, writable directories. No bare relative 'data' paths.

ISSUE 2 — document storage dir is neither mounted nor writable
DOCUMENT_STORAGE_DIR=/var/tender-agent/documents is set, but /var/tender-agent/ is not a mounted volume and not writable by the app user inside the container. Meanwhile docker-compose DOES mount a writable volume at /app/data/bridge-downloads (host: ${USERPROFILE}/.tender-agent/bridge-downloads).
Fix: standardise on a single documents location under /app/data that is (a) mounted to a host folder so files persist and the user can see them, and (b) writable by the app user. Recommended: /app/data/documents mounted to ${USERPROFILE:-${HOME}}/.tender-agent/documents on the host. Update DOCUMENT_STORAGE_DIR to this. Ensure the app user owns/can create it (Dockerfile: create the dir + chown to the app user; or makedirs with exist_ok at startup with correct perms).
ALSO clean up tender-agent/.env: it currently has DOCUMENT_STORAGE_DIR set twice (line 31 = /var/tender-agent/documents, line 56 = /app/data/bridge-downloads). Remove BOTH duplicates and set ONE correct line: DOCUMENT_STORAGE_DIR=/app/data/documents. Mirror in .env.example with a comment.

ISSUE 3 — bridge env vars not reaching the container
`docker compose exec app printenv` shows NO TENDER_AGENT_BRIDGE_* vars — the backend can't authenticate to the bridge. The .env has TENDER_AGENT_BRIDGE_TOKEN, but the docker-compose app service doesn't pass it (and TENDER_AGENT_BRIDGE_URL) into the container.
Fix: in docker-compose.yml app service `environment:` block, pass through:
  TENDER_AGENT_BRIDGE_URL: ${TENDER_AGENT_BRIDGE_URL:-http://host.docker.internal:8765}
  TENDER_AGENT_BRIDGE_TOKEN: ${TENDER_AGENT_BRIDGE_TOKEN}
Also ensure the app service has:
  extra_hosts:
    - "host.docker.internal:host-gateway"
so the Linux container can actually reach the bridge running on the Windows host. (host.docker.internal works automatically on Docker Desktop for Windows, but add extra_hosts for robustness.)
Document in .env.example that TENDER_AGENT_BRIDGE_TOKEN must match the browser-bridge/.env value, and TENDER_AGENT_BRIDGE_URL defaults to host.docker.internal:8765.

================================================================
SELF-DIAGNOSING STARTUP CHECK
================================================================
Add a lightweight startup/diagnostic so future first-runs explain themselves instead of failing cryptically. Two parts:

1. On app startup (or as a GET /system/preflight endpoint), check and report:
   - DOCUMENT_STORAGE_DIR exists and is writable (try creating + deleting a temp file). If not → clear log line + the endpoint returns the specific problem.
   - TENDER_AGENT_BRIDGE_TOKEN is set (non-empty). If not → clear message.
   - bridge reachable at TENDER_AGENT_BRIDGE_URL/health (best-effort, short timeout) → report up/down (NOT fatal; bridge may legitimately be off).
   Return a structured JSON: { documents_dir_writable: bool, bridge_token_set: bool, bridge_reachable: bool, details: [...] }.

2. In the orchestrator, BEFORE attempting a login-portal fetch, call the same checks. If documents dir isn't writable or bridge token missing, fail the task with a CLEAR, actionable detail (e.g. "Documents directory /app/data/documents is not writable — check the volume mount", or "Bridge token not configured — set TENDER_AGENT_BRIDGE_TOKEN in tender-agent/.env to match browser-bridge/.env"), not a raw PermissionError.

================================================================
DASHBOARD (small)
================================================================
The dashboard already has (or was specced to have) a "Bridge ●" health indicator. Ensure it calls a backend endpoint that proxies the bridge health (so it works through the /__api proxy and respects the token). If GET /system/bridge-health doesn't exist, add it (backend calls bridge /health using the configured URL+token, returns up/down). Indicator green when up, grey when down. No big UI work — just make the existing indicator real.

================================================================
TESTS
================================================================
- Path construction: every document/staging path derives from configured dirs; assert no bare-relative 'data' path is used (a test that the download code, given a fake DOCUMENT_STORAGE_DIR, writes only under it).
- Preflight check: writable dir → ok; non-writable (mock) → reports problem; missing token → reports problem; bridge unreachable (mock) → reports down but not fatal.
- Orchestrator: missing token / non-writable dir → task fails with the clear actionable message, not a raw exception.
- Keep all existing tests green.
Mocked; no real bridge, no real Delta, no network in CI.

================================================================
VERIFICATION (what you can do in the run)
================================================================
- docker compose config validates (the new environment + extra_hosts + volume parse correctly).
- After a rebuild, document the expected: `docker compose exec app printenv | grep BRIDGE` would show the two BRIDGE vars; DOCUMENT_STORAGE_DIR=/app/data/documents; and the documents dir is writable. (You can't fully verify the host-side mount in the autonomous env, but assert the compose + Dockerfile + startup makedirs are correct.)
- pytest green, ruff clean, tsc + npm build clean.

================================================================
SHIP
================================================================
Commits:
- fix(storage): derive all document paths from DOCUMENT_STORAGE_DIR; no relative 'data' paths
- fix(compose): mount writable /app/data/documents; pass bridge URL+token into app; extra_hosts host-gateway
- fix(env): single DOCUMENT_STORAGE_DIR; document bridge vars in .env.example
- feat(preflight): self-diagnosing startup/preflight checks + clear orchestrator errors
- fix(dashboard): real bridge-health indicator via backend proxy
- test: path safety + preflight + orchestrator clear-error tests

PR title: "fix: Phase 4 Chunk 4d — backend↔bridge wiring (storage path, bridge env, preflight checks)"
Description: list the three issues found in live testing and how each is fixed; note the user re-tests the Delta fetch on tender 3169 after merge + rebuild. Sentinel: "Phase 4 Chunk 4d — bridge wiring fixed. Ready for review."

RULES
- Single PR against main. Don't change the Delta adapter's flow logic (4c is correct) — only fix path construction where it touches storage. Don't store credentials. Mocked tests only. If blocked, "BLOCKED:" at top.

Begin.
