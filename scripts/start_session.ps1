# scripts\start_session.ps1
#
# PowerShell twin of start_session.bat. Same behaviour: activate venv, run
# the Python orchestrator that does pre-flight (tailscale + pod + beacon)
# and orchestrates the dashboard + mobile proxy.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..")
Push-Location $Root

try {
    $activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        Write-Host "[start-session] .venv not found at $Root\.venv"
        Write-Host "                Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
        exit 2
    }
    . $activate

    python scripts\session_launcher.py
    $rc = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $rc
