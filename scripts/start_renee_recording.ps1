# scripts/start_renee_recording.ps1
#
# PowerShell twin of start_renee_recording.bat. Same behaviour: activate
# venv, run the Python runner that checks pod, launches the dashboard,
# opens the browser, runs the audio bridge with RENEE_RECORD=1, and
# triggers triage on Ctrl+C.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..")
Push-Location $Root

try {
    $activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        Write-Host "[start-renee-recording] .venv not found at $Root\.venv; aborting"
        exit 2
    }
    . $activate

    $env:RENEE_SKIP_ENCRYPT_WARN = "1"
    $env:RENEE_RECORD = "1"

    python -m src.capture.record_runner
    $rc = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $rc
