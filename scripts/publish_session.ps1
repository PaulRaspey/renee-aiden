# scripts\publish_session.ps1 <session-id> [-NoConfirm]
#
# PowerShell twin of publish_session.bat. Ships a captured session to the
# public renee-sessions-public repo via the renee CLI.

param(
    [Parameter(Position = 0)]
    [string]$SessionId = "",
    [switch]$NoConfirm,
    [switch]$List
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..")
Push-Location $Root

try {
    $activate = Join-Path $Root ".venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        Write-Host "[publish] .venv not found at $Root\.venv"
        exit 2
    }
    . $activate
    $env:RENEE_SKIP_ENCRYPT_WARN = "1"

    if ($List) {
        python -m renee publish-list
        $rc = $LASTEXITCODE
    }
    elseif (-not $SessionId) {
        Write-Host "Usage: scripts\publish_session.ps1 <session-id> [-NoConfirm]"
        Write-Host "       scripts\publish_session.ps1 -List"
        $rc = 2
    }
    elseif ($NoConfirm) {
        python -m renee publish $SessionId
        $rc = $LASTEXITCODE
    }
    else {
        python -m renee publish --confirm $SessionId
        $rc = $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

exit $rc
