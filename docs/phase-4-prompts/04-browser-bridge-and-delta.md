Task: build the native-Windows browser bridge AND the first authenticated portal adapter (Delta eSourcing) on top of it. Autonomous overnight run. The user is asleep and will validate the Delta-specific layer manually on Friday.

This is prompt 4 of 12 in the Phase 4 build sequence. Chunks 1-3 merged on main (#32 discovery+registry, #34 adapter framework+platforms, #36 CF direct adapter).

Required reading before any code:
- docs/phase-4-design.md
- docs/phase-4-prompts/02-adapter-framework.md (the PortalAdapter framework you're extending)
- docs/phase-4-prompts/03-contracts-finder-adapter.md (how the orchestrator + fetch_tasks + documents panel already work)
- The live code: tender_agent/services/portals/ (base, results, registry, contracts_finder), tender_agent/services/portal_orchestrator.py, tender_agent/api/tender_fetch.py, tender_agent/services/browser.py (BrowserContextManager stub from chunk 2)

================================================================
THE PROBLEM THIS SOLVES
================================================================

Real ITT documents live behind authenticated portals (Delta eSourcing, ProContract, JAGGAER, etc.). The user does NOT want passwords stored anywhere. The agreed model:

- A VISIBLE Chrome window opens on the user's Windows desktop, showing the portal's real login page.
- The USER logs in themselves, like a normal app. The system never sees or stores the password.
- After login, the system drives that same authenticated browser session to navigate to the tender and download documents.
- The session PERSISTS (cookies) so the user doesn't re-login for every tender — only when the session expires.
- If the user queues several fetches and walks away, login-required fetches WAIT at a "waiting for you to log in" state until the user returns (they do not fail, they do not auto-login).

CRITICAL ARCHITECTURE CONSTRAINT: the backend runs in Docker (Linux container). A containerised browser cannot show a visible window on Windows. Therefore the browser must run NATIVELY on Windows, in a separate helper process OUTSIDE Docker, which the Docker backend drives over a local HTTP API.

================================================================
PART A — THE WINDOWS BROWSER BRIDGE (native, outside Docker)
================================================================

Create a NEW top-level component: `browser-bridge/` (sibling to tender-agent/ and tender-agent-dashboard/).

It is a standalone Python service that runs natively on Windows (NOT in Docker). It owns the real Chrome browser via Playwright.

1. `browser-bridge/pyproject.toml` — minimal deps: fastapi, uvicorn, playwright, pydantic. Python 3.12.

2. `browser-bridge/bridge/server.py` — a FastAPI app listening on 0.0.0.0:8765.

   Auth: every request requires header `X-Bridge-Token`. The token is read from env TENDER_AGENT_BRIDGE_TOKEN (the bridge and the backend share this value via the same .env). Reject mismatches with 401. This stops anything other than the backend driving the user's logged-in browser.

   Session model: one persistent Playwright context per platform_slug, stored under TENDER_AGENT_BRIDGE_STATE_DIR (default %USERPROFILE%\.tender-agent\bridge-sessions\{platform_slug}\). Persistent context = cookies/localStorage survive process restarts. Default HEADED (visible window). Env TENDER_AGENT_BRIDGE_HEADLESS=1 forces headless (used by the automated test).

   Endpoints (all require the token):
   - POST /session/open  { platform_slug, start_url } → opens (or reuses) a persistent context, opens a page, navigates to start_url. Returns { session_id, current_url, authenticated_guess }.
   - GET  /session/{slug}/status → { exists, current_url, authenticated_guess }
   - POST /session/{slug}/wait-for-login { success_url_pattern, login_url, timeout_seconds } → brings the visible window to front at login_url, then POLLS every 2s until the page URL matches success_url_pattern (regex) OR an authenticated marker is present, OR timeout. Returns { status: 'logged_in' | 'timeout' | 'error', current_url }. THIS IS THE HUMAN-IN-THE-LOOP STEP. Default timeout 600s (10 min); the orchestrator can re-call it to extend the wait indefinitely (option 2a — wait, don't fail).
   - POST /session/{slug}/navigate { url } → navigate, return { current_url, status_code, title }
   - GET  /session/{slug}/page-text → returns the rendered page's visible text (for the adapter to parse/locate)
   - GET  /session/{slug}/page-html → returns rendered HTML (for selector-based extraction)
   - POST /session/{slug}/find-links { pattern } → returns hrefs on the current page matching a regex (used to find document download links)
   - POST /session/{slug}/download { url, dest_filename } → downloads a URL within the authenticated session (uses the context's cookies), streams bytes back to the caller as base64 OR writes to a shared download dir. Prefer: writes to TENDER_AGENT_BRIDGE_DOWNLOAD_DIR and returns { path, size_bytes, mime_type }. (The backend reads from a shared volume — see Part B for how the file crosses back.)
   - POST /session/{slug}/click-download { selector } → clicks an element that triggers a download, captures the downloaded file via Playwright's download API, saves to the download dir, returns file metadata.
   - POST /session/{slug}/screenshot { label } → saves PNG to download dir, returns path.
   - POST /session/{slug}/close → close the context (keeps persisted state on disk).

   "authenticated_guess": a cheap heuristic — true if current URL is NOT the login page and no obvious login form is present. Real auth detection is per-adapter (Part E).

3. `browser-bridge/bridge/__main__.py` — `python -m bridge` starts uvicorn on 0.0.0.0:8765.

4. `browser-bridge/start-bridge.ps1` — a PowerShell launcher the user double-clicks / runs: sets up venv if missing, installs deps + `playwright install chromium`, reads .env for the token, starts the bridge. Prints a clear "Bridge running on http://localhost:8765 — leave this window open" message.

5. `browser-bridge/README.md` — plain-English: what it is, how to start it, that it must be running before fetching from login portals, that the window it opens is where you log in. Written for a non-programmer.

================================================================
PART B — BACKEND BRIDGE CLIENT (in Docker)
================================================================

6. `tender_agent/services/bridge_client.py` — an async httpx client that talks to the bridge.

   Base URL: env TENDER_AGENT_BRIDGE_URL, default `http://host.docker.internal:8765` (this is how a Docker container reaches a service on the Windows host). Sends the X-Bridge-Token header from env on every call.

   Methods mirror the bridge endpoints: open_session, session_status, wait_for_login, navigate, page_text, page_html, find_links, download, click_download, screenshot, close_session.

   `bridge_available() -> bool`: a fast health check (GET /health on the bridge, 2s timeout). Used so the orchestrator can give a clean error ("Browser bridge isn't running — start it with start-bridge.ps1") instead of a confusing timeout when the bridge is off.

7. Shared download dir crossing Docker↔Windows: the bridge writes downloads to a host folder (default %USERPROFILE%\.tender-agent\bridge-downloads\). Mount that SAME host folder into the backend container in docker-compose.yml as a volume (e.g. `${USERPROFILE}/.tender-agent/bridge-downloads:/app/data/bridge-downloads`). The bridge returns paths relative to that dir; the backend reads the file from the mounted volume, computes sha256, and moves/copies it into the existing tender-documents storage from chunk 3. Document the volume mount clearly; if the env var interpolation is awkward on Windows, provide a documented manual edit.

8. Add bridge config to .env.example: TENDER_AGENT_BRIDGE_URL, TENDER_AGENT_BRIDGE_TOKEN (with a note to set the same token in the bridge), and the download dir.

================================================================
PART C — A WAITING STATE IN THE FETCH FLOW
================================================================

9. Extend the fetch_tasks status enum (migration 0008) to add: `waiting_for_login`. Full set: queued, running, waiting_for_login, complete, failed.

   Add nullable columns to fetch_tasks: `login_url` (text), `bridge_session_slug` (text), `waiting_since` (timestamp).

10. The orchestrator's flow for a login-required platform becomes:
    a. Check bridge_available(). If not → fail task with detail "Browser bridge not running. Start start-bridge.ps1 on your PC, then retry." (clear, actionable).
    b. open_session(platform_slug, start_url=tender_url).
    c. Call the adapter's `is_authenticated(bridge, slug)` check.
    d. If NOT authenticated:
       - set task status = 'waiting_for_login', waiting_since=now, login_url=adapter's login URL.
       - call bridge.wait_for_login(...). This brings the visible window forward; the human logs in.
       - on 'logged_in' → continue. on 'timeout' → LEAVE task in waiting_for_login and re-call wait_for_login (extend). Implement as a bounded loop: re-wait up to a total of, say, 60 minutes, re-checking each cycle; only fail after that with detail "Login not completed — retry when you're ready." (This realises option 2a: wait, don't fail, while the user is away.)
    e. Once authenticated → adapter.locate_tender → adapter.download_documents (driving the bridge).
    f. Persist downloaded files via the chunk-3 path (sha256 dedup, tender_documents rows).
    g. status='complete'.

11. The push-notification system (already built) should fire a notification when a task ENTERS waiting_for_login ("Action needed: log in to {platform} to fetch documents for {tender title}"). Reuse the existing push channel. This is how the user knows to come back to the laptop.

================================================================
PART D — THE DELTA ESOURCING ADAPTER
================================================================

12. `tender_agent/services/portals/adapters/delta_esourcing.py` implementing PortalAdapter for platform_slug='delta_esourcing'.

    IMPORTANT HONESTY CONSTRAINT: you do NOT have a Delta account and cannot see Delta's real authenticated pages. Build this adapter against the KNOWN structure of Delta eSourcing (a JAGGAER-family platform) using reasonable, well-documented assumptions. Make every Delta-specific selector / URL pattern a NAMED CONSTANT at the top of the file in a clearly-labelled `DELTA_SELECTORS` / `DELTA_URLS` block, with a comment on each saying "VALIDATE ON FRIDAY — best-effort selector." This makes the Friday fix-up a matter of correcting a handful of constants, not rewriting logic.

    Methods:
    - matches_url: delegates to platform domain_patterns (delta-esourcing.com + subdomains), already in DB.
    - login_url: returns the Delta supplier login URL constant.
    - is_authenticated(bridge, slug): navigate to a known authenticated landing page; return True if not redirected to login and a known logged-in marker (e.g. a "My Account"/logout link) is present. Best-effort; constant-driven.
    - authenticate: NOT automated. Returns AuthResult(status='success') only AFTER the orchestrator's wait_for_login has confirmed login. The adapter itself never submits credentials.
    - locate_tender: from the tender's source data, derive the Delta tender page URL (or search Delta for the tender reference if no direct URL); navigate there via the bridge; confirm the tender detail page loaded.
    - register_interest: Delta typically gates documents behind an "Express Interest" click. Implement it BUT per the design's strict principle, the orchestrator must PAUSE before this — see item 13. The method itself performs the click when called.
    - download_documents: locate the documents/attachments area on the tender page, enumerate document links, download each via the authenticated session (bridge.click-download or bridge.download), return DownloadResult.

13. STRICT-PRINCIPLE PAUSE before register_interest: "Express Interest" is an interpretive action (it tells the buyer you're bidding). Per "machines act, humans interpret," the orchestrator must NOT auto-click it. Instead: if locate_tender reports documents are gated behind Express Interest, set the task to a NEW terminal-ish status `needs_user_confirmation` (add to enum in migration 0008) with detail explaining "Delta requires you to Express Interest before documents are released. This signals intent to bid. Confirm to proceed." The dashboard surfaces a Confirm button; only on explicit user confirm (a new endpoint POST /tenders/{id}/fetch-documents/{task_id}/confirm) does the orchestrator resume and call register_interest then download. Document this clearly. (For tonight's build, wire the full path; it gets exercised for real on Friday.)

================================================================
PART E — AUTOMATED TEST AGAINST A SAFE PRACTICE SITE
================================================================

14. The overnight run cannot have a human type a password. So prove the bridge MECHANICALLY against the public practice login at https://the-internet.herokuapp.com/login (designed for automation testing; creds tomsmith / SuperSecretPassword!).

    Create `browser-bridge/tests/test_bridge_live.py` (marked so CI can skip it; it needs network + a browser):
    - Start the bridge in headless mode (TENDER_AGENT_BRIDGE_HEADLESS=1).
    - open_session(slug='practice', start_url=login page).
    - SIMULATE the human: fill #username / #password, submit. (In real use a human does this in the visible window; here the test stands in for the human to prove the downstream pipeline.)
    - Assert wait-for-login style success detection works (success_url_pattern matches /secure).
    - Assert session persists: close, reopen, status shows authenticated_guess true (the /secure page still reachable without re-login).
    - Assert page-text on /secure contains the known success string.
    - Assert a file download through the session works (download the site's logo or any static asset; verify bytes land in the download dir).

    This proves: visible-window session open, login-success detection, session persistence, authenticated page read, and authenticated download — the entire mechanical bridge — without needing Delta or a real human.

15. Also a headed-mode sanity check in the verification run (Part G): open the practice site headed, confirm a window actually appears (capture a screenshot), so we know the visible-window path works on this machine's Chrome.

================================================================
PART F — DASHBOARD
================================================================

16. Extend the documents panel on /tenders/[id] (built in chunk 3) to handle the new states:
    - waiting_for_login: amber banner "Waiting for you to log in to {platform}. A browser window should be open on your PC — log in there, and this will continue automatically." + a "I've logged in / Retry" button (re-pokes the task) + a "Cancel" button.
    - needs_user_confirmation: distinct banner explaining Express Interest, with "Confirm & continue" (calls the confirm endpoint) and "Not now" (cancels).
    - bridge-not-running failure: red banner "Browser bridge isn't running. Open start-bridge.ps1 on your PC, then Retry."
    - All existing states (queued/running/complete/failed) keep working.

17. A small persistent "Bridge: ●" status indicator in the AppShell header (green if GET bridge health ok via a backend proxy endpoint /system/bridge-health, grey if down). One glance tells the user whether the bridge is up before they try to fetch.

================================================================
PART G — VERIFICATION (run what you can unattended)
================================================================

18. Backend + bridge unit/integration tests, all mocked (no real network in CI):
    - bridge client: each method calls the right endpoint with the token; bridge_available handles down-bridge gracefully.
    - orchestrator: waiting_for_login loop (mock bridge says "not logged in" twice then "logged in"); needs_user_confirmation path; bridge-down failure path; full success path with mocked bridge + mocked Delta adapter.
    - delta adapter: matches_url; is_authenticated true/false from mocked page markers; locate_tender URL derivation; download_documents enumerates + downloads via mocked bridge; the Express-Interest gate triggers needs_user_confirmation.
    - migration 0008 applies cleanly; new statuses + columns present.
    Target 30+ new tests across backend + bridge. CI green with the live practice-site test SKIPPED (it's in a network-marked group).

19. THE live practice-site proof (run it for real during the session, since this machine has Chrome + network):
    - Start the bridge (headless) and run test_bridge_live.py against the-internet.herokuapp.com. Capture the output into docs/screenshots/bridge-live-test.txt. This is the evidence the bridge mechanically works end-to-end.
    - Run the headed sanity check; save the window screenshot to docs/screenshots/bridge-headed-window.png.

20. Quality gates: pytest (backend) all green; bridge tests green; ruff clean (backend + bridge); npx tsc --noEmit clean; npm run build clean.

================================================================
PART H — SHIP
================================================================

21. Commits (in order):
    - feat(bridge): native Windows browser bridge service + token auth + persistent sessions
    - feat(bridge): wait-for-login, navigate, page read, download endpoints
    - feat(backend): bridge_client + host.docker.internal wiring + shared download volume
    - feat(db): migration 0008 — waiting_for_login + needs_user_confirmation statuses + columns
    - feat(orchestrator): login-wait loop + Express-Interest confirmation pause + push on waiting
    - feat(adapters): Delta eSourcing adapter (constant-driven selectors, Friday-validate markers)
    - feat(dashboard): waiting/confirm/bridge-down states + bridge health indicator
    - test: bridge + orchestrator + delta + live practice-site proof
    - docs: bridge README + live-test output + screenshots

22. PR title: "feat: Phase 4 Chunk 4 — Windows browser bridge + Delta eSourcing adapter (login-via-human)"

    Description MUST include:
    - The architecture in plain English: why the browser runs natively on Windows, how Docker reaches it, that the password is never stored.
    - A clear "WHAT'S PROVEN vs WHAT NEEDS FRIDAY" section:
      * PROVEN tonight (mechanical, against practice site): session open, human-login-success detection, session persistence, authenticated page read, authenticated download, the full waiting_for_login orchestration with a mocked bridge.
      * NEEDS FRIDAY (manual, against real Delta): the Delta-specific selectors/URLs in DELTA_SELECTORS/DELTA_URLS, the real visible-window human login to Delta, real document download from a real Delta tender, the Express-Interest confirmation flow.
    - The list of Delta constants that need Friday validation (so the user knows exactly what to check).
    - bridge-live-test.txt output embedded + the headed-window screenshot.
    - Setup steps for Friday: start the bridge, set the shared token, the docker-compose volume note.
    - Sentinel: "Phase 4 Chunk 4 — Browser bridge + Delta adapter complete (bridge proven; Delta pending Friday validation). Ready for review."

================================================================
RULES
================================================================

- Single PR.
- The adapter NEVER stores, logs, or transmits the user's password. Login happens only in the visible window, by the human. The bridge captures only cookies/session state on disk, never credentials.
- The bridge token must be required on every bridge endpoint. No unauthenticated access to the user's logged-in browser.
- Only navigate/download within domains matching the target platform's domain_patterns. Reject off-platform URLs.
- File caps from chunk 3 (100MB/doc, 50 docs/tender, sanitised filenames, sha256 dedup) still apply.
- Do NOT auto-click Express Interest or any "submit/express/bid" control. That path goes through needs_user_confirmation only.
- Do NOT attempt to log into Delta during the autonomous run. The Delta auth path is built but exercised by the human on Friday. The only live login test is the practice site, with the test simulating the human.
- Bridge is a NEW component; do not break tender-agent or tender-agent-dashboard. Existing tests stay green.
- If a real blocker emerges, "BLOCKED:" at top of PR, push branch, open draft.

================================================================
STOP CRITERIA
================================================================

Stop only when ALL true:
- PR open against main, ready for review (or draft + BLOCKED).
- CI green (live practice-site test skipped in CI but PRESENT).
- The live practice-site test was actually RUN during the session and its output captured in docs/screenshots/bridge-live-test.txt showing success.
- Headed-window screenshot captured.
- 30+ new tests pass; ruff + tsc + npm build clean.
- PR description has the "PROVEN vs NEEDS FRIDAY" section and the list of Delta constants to validate.
- Sentinel line present.

Begin.
