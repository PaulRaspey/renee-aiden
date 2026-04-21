@echo off
REM scripts/start_renee_recording.bat
REM
REM One-click recording session. Activates the venv, checks pod, starts
REM the dashboard if it is not already running, opens the browser, and
REM kicks off the audio bridge with RENEE_RECORD=1. On Ctrl+C it stops
REM the bridge and triggers triage on the most recent session directory.

setlocal
set SCRIPT_DIR=%~dp0
set ROOT=%SCRIPT_DIR%..
pushd "%ROOT%"

if not exist .venv\Scripts\activate.bat (
    echo [start-renee-recording] .venv not found at "%ROOT%\.venv"; aborting
    popd
    endlocal
    exit /b 2
)
call .venv\Scripts\activate.bat

set RENEE_SKIP_ENCRYPT_WARN=1
set RENEE_RECORD=1

python -m src.capture.record_runner
set RC=%ERRORLEVEL%

popd
endlocal
exit /b %RC%
