<#
.SYNOPSIS
    Connect Delta — capture your Delta login and send it to the cloud backend.

.DESCRIPTION
    Double-clickable / one-line wrapper around scripts/connect_delta.py for
    Windows operators. It just finds Python and runs the helper; all the real
    work (and all the friendly prompts) live in the Python script.

    One-time setup on this laptop:
        pip install playwright==1.47.0
        playwright install chromium

.EXAMPLE
    .\connect-delta.ps1
        Run with all the sensible defaults (talks to the deployed backend).

.EXAMPLE
    .\connect-delta.ps1 --timeout 900
        Allow 15 minutes to finish logging in. Any extra args are passed
        straight through to connect_delta.py.
#>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$helper = Join-Path $scriptDir "scripts\connect_delta.py"

if (-not (Test-Path $helper)) {
    Write-Error "Can't find scripts\connect_delta.py next to this wrapper."
    exit 1
}

# Prefer the Windows `py` launcher, fall back to `python`.
$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    & py -3 $helper @args
} else {
    $python = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $python) {
        Write-Error "Python isn't installed (or not on PATH). Install Python 3.12, then re-run."
        exit 1
    }
    & python $helper @args
}
exit $LASTEXITCODE
