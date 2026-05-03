@echo off
REM scripts\start_session.bat
REM
REM ONE-BUTTON phone+Tailscale+RunPod session launcher.
REM
REM Pre-flights Tailscale + pod + Beacon, wakes the pod (idempotent),
REM starts the dashboard in the background, runs the mobile proxy with
REM HTTPS + QR in the foreground. Ctrl+C tears everything down cleanly.
REM
REM For desktop-mode (sounddevice on the OptiPlex with auto-recording),
REM use scripts\start_renee_recording.bat instead.

setlocal
set ROOT=%~dp0..
pushd "%ROOT%"

if not exist .venv\Scripts\activate.bat (
    echo [start-session] .venv not found at "%ROOT%\.venv"
    echo                 Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    popd
    endlocal
    exit /b 2
)
call .venv\Scripts\activate.bat

REM session_launcher sets RENEE_SKIP_ENCRYPT_WARN itself; nothing else to set here.
python scripts\session_launcher.py
set RC=%ERRORLEVEL%

popd
endlocal
exit /b %RC%
