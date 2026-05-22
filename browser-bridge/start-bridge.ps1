# start-bridge.ps1 — double-click or run this to start the Tender Agent browser bridge.
#
# What this does, in plain terms:
#   * Makes a small private Python environment the first time (one-off, ~1 min).
#   * Installs the browser it needs (Chrome via Playwright) the first time.
#   * Reads your shared token from .env.
#   * Starts the bridge and opens nothing yet — a browser window appears only
#     when the app asks you to log in to a portal.
#
# Leave the window this opens RUNNING while you use the document-fetch feature.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "=== Tender Agent browser bridge ===" -ForegroundColor Cyan

# 1. Create the venv if missing.
if (-not (Test-Path ".venv")) {
    Write-Host "First run: creating Python environment..." -ForegroundColor Yellow
    python -m venv .venv
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# 2. Install dependencies + the browser (idempotent; fast after first run).
Write-Host "Checking dependencies..." -ForegroundColor Yellow
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e .
& $py -m playwright install chromium

# 3. Load the shared token from .env (KEY=VALUE lines).
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$') {
            $name = $matches[1]
            $value = $matches[2].Trim('"')
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

if (-not $env:TENDER_AGENT_BRIDGE_TOKEN) {
    Write-Host "WARNING: TENDER_AGENT_BRIDGE_TOKEN is not set." -ForegroundColor Red
    Write-Host "Create browser-bridge\.env with the SAME token as the backend's .env, e.g.:" -ForegroundColor Red
    Write-Host "    TENDER_AGENT_BRIDGE_TOKEN=pick-a-long-random-string" -ForegroundColor Red
}

Write-Host ""
Write-Host "Bridge running on http://localhost:8765 — leave this window open." -ForegroundColor Green
Write-Host "A browser window will appear when you need to log in to a portal." -ForegroundColor Green
Write-Host "Press Ctrl+C here to stop the bridge." -ForegroundColor DarkGray
Write-Host ""

# 4. Start the bridge (foreground).
& $py -m bridge
